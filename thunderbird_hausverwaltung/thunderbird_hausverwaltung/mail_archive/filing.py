from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.synchronization import filelock

from ..doctype.mail_archive_account.mail_archive_account import normalize_account_addresses
from .classifier import create_suggestion
from .providers import get_provider
from .sync import sync_folder_records, upsert_messages


def _normalize_rfc_message_id(value: Any) -> str:
	result = str(value or "").strip()
	if len(result) >= 2 and result.startswith("<") and result.endswith(">"):
		result = result[1:-1].strip()
	return result


def _find_indexed_message_doc(account_name: str, rfc_message_id: str) -> Any | None:
	value = _normalize_rfc_message_id(rfc_message_id)
	if not value:
		return None
	# JMAP exposes Message-ID without angle brackets on some imported messages while
	# Thunderbird may include them. Accept both persisted representations.
	matches = frappe.get_all(
		"Mail Archive Message",
		filters={
			"archive_account": account_name,
			"rfc_message_id": ["in", [value, f"<{value}>"]],
		},
		fields=["name"],
		order_by="received_at desc, name asc",
		limit_page_length=2,
	)
	if len(matches) > 1:
		frappe.throw(
			_("Die RFC Message-ID ist im ERPNext-Archivindex nicht eindeutig."),
			frappe.ValidationError,
		)
	return frappe.get_doc("Mail Archive Message", matches[0].name) if matches else None


def _find_source_message_doc(source_account: str, rfc_message_id: str) -> Any | None:
	value = _normalize_rfc_message_id(rfc_message_id)
	if not value:
		return None
	matches = frappe.get_all(
		"Mail Filing Source Message",
		filters={
			"source_account": source_account,
			"rfc_message_id": ["in", [value, f"<{value}>"]],
		},
		fields=["name"],
		order_by="received_at desc, name asc",
		limit_page_length=2,
	)
	if len(matches) > 1:
		frappe.throw(
			_("Die RFC Message-ID ist im Quellpostfachindex nicht eindeutig."),
			frappe.ValidationError,
		)
	return frappe.get_doc("Mail Filing Source Message", matches[0].name) if matches else None


def _requested_addresses(account_addresses: Any = None) -> set[str]:
	if isinstance(account_addresses, str):
		try:
			decoded = json.loads(account_addresses)
		except ValueError:
			decoded = account_addresses
		account_addresses = decoded
	if isinstance(account_addresses, (list, tuple, set)):
		return set(normalize_account_addresses("\n".join(str(item) for item in account_addresses)))
	return set(normalize_account_addresses(str(account_addresses or "")))


def _select_account(account_addresses: Any = None) -> Any:
	requested = _requested_addresses(account_addresses)
	accounts = [
		frappe.get_doc("Mail Archive Account", name)
		for name in frappe.get_all("Mail Archive Account", filters={"enabled": 1}, pluck="name")
	]
	if not accounts:
		frappe.throw(_("Es ist kein aktives Mail-Archiv-Konto eingerichtet."))
	if requested:
		matches = [
			account
			for account in accounts
			if requested.intersection(normalize_account_addresses(account.email_addresses))
		]
		if len(matches) == 1:
			return matches[0]
		if len(matches) > 1:
			frappe.throw(_("Die Thunderbird-Kontoadressen passen zu mehreren Mail-Archiv-Konten."))
		frappe.throw(_("Für das Thunderbird-Konto wurde kein Mail-Archiv-Konto gefunden."))
	if len(accounts) == 1:
		return accounts[0]
	frappe.throw(_("Das Mail-Archiv-Konto ist ohne eindeutige Kontoadresse mehrdeutig."))


def _select_source_account(account_addresses: Any = None) -> Any | None:
	requested = _requested_addresses(account_addresses)
	if not requested:
		return None
	sources = [
		frappe.get_doc("Mail Filing Source Account", name)
		for name in frappe.get_all("Mail Filing Source Account", filters={"enabled": 1}, pluck="name")
	]
	matches = [
		source
		for source in sources
		if requested.intersection(normalize_account_addresses(source.email_addresses))
	]
	if len(matches) > 1:
		frappe.throw(_("Die Thunderbird-Kontoadressen passen zu mehreren Quellpostfächern."))
	return matches[0] if matches else None


def get_filing_suggestions(
	header_message_id: str, account_addresses: Any = None, limit: int = 3
) -> dict[str, Any]:
	value = _normalize_rfc_message_id(header_message_id)
	if not value:
		frappe.throw(_("Die Nachricht hat keine RFC Message-ID und kann nicht eindeutig zugeordnet werden."))
	source_account = _select_source_account(account_addresses)
	if source_account:
		account = frappe.get_doc("Mail Archive Account", source_account.archive_account)
		message_doc = _find_source_message_doc(source_account.name, value)
		if not message_doc:
			from .source_sync import sync_source_message_by_rfc_id

			message_doc = sync_source_message_by_rfc_id(source_account.name, value)
		if not message_doc:
			frappe.throw(_("Die Nachricht wurde in den überwachten Quellordnern nicht gefunden."))
	else:
		account = _select_account(account_addresses)
		message_doc = _find_indexed_message_doc(account.name, value)
		if not message_doc:
			provider = get_provider(account)
			message = provider.find_message_by_rfc_id(value)
			if not message:
				frappe.throw(_("Die Nachricht wurde auf dem konfigurierten Mailserver nicht gefunden."))
			mailboxes = provider.list_mailboxes()
			folder_context = sync_folder_records(account, mailboxes)
			upsert_messages(account, [message], folder_context["folders"], embedding_optional=True)
			message_doc = frappe.get_doc(
				"Mail Archive Message",
				frappe.db.get_value(
					"Mail Archive Message",
					{"archive_account": account.name, "provider_message_id": message.id},
					"name",
				),
			)
	result = create_suggestion(account, message_doc, limit=limit)
	result["source_account"] = source_account.name if source_account else ""
	result["available_folders"] = frappe.get_all(
		"Mail Archive Folder",
		filters={"archive_account": account.name, "selectable_target": 1},
		fields=["name as folder", "folder_path as path"],
		order_by="folder_path asc",
	)
	return result


def _can_decide(suggestion: Any) -> bool:
	return suggestion.requested_by == frappe.session.user or "System Manager" in frappe.get_roles()


def confirm_filing(suggestion_id: str, folder: str) -> dict[str, Any]:
	suggestion_id = str(suggestion_id or "").strip()
	folder = str(folder or "").strip()
	with filelock(f"mail-filing-{suggestion_id}", timeout=10):
		return _confirm_filing_locked(suggestion_id, folder)


def _confirm_filing_locked(suggestion_id: str, folder: str) -> dict[str, Any]:
	if not suggestion_id or not frappe.db.exists("Mail Filing Suggestion", suggestion_id):
		frappe.throw(_("Der Ablagevorschlag wurde nicht gefunden."), frappe.DoesNotExistError)
	suggestion = frappe.get_doc("Mail Filing Suggestion", suggestion_id)
	if not _can_decide(suggestion):
		frappe.throw(_("Der Ablagevorschlag gehört zu einem anderen Benutzer."), frappe.PermissionError)
	if suggestion.status in {"Angenommen", "Korrigiert"} and suggestion.chosen_folder == folder:
		target = frappe.get_doc("Mail Archive Folder", folder)
		return {
			"status": suggestion.status,
			"folder": target.name,
			"folder_path": target.folder_path,
			"recorded": True,
		}
	if suggestion.status not in {"Vorgeschlagen", "Fehler"}:
		frappe.throw(_("Über diesen Ablagevorschlag wurde bereits entschieden."))
	if not folder or not frappe.db.exists("Mail Archive Folder", folder):
		frappe.throw(_("Der Archivordner wurde nicht gefunden."), frappe.DoesNotExistError)
	target = frappe.get_doc("Mail Archive Folder", folder)
	if target.archive_account != suggestion.archive_account or not target.selectable_target:
		frappe.throw(_("Dieser Ordner ist für den Ablagevorschlag nicht zulässig."), frappe.PermissionError)
	decision_status = "Angenommen" if suggestion.proposed_folder == target.name else "Korrigiert"
	suggestion.db_set(
		{
			"status": decision_status,
			"chosen_folder": target.name,
			"decided_on": now_datetime(),
			"error": "",
		},
		update_modified=True,
	)
	if suggestion.source_message:
		frappe.db.set_value(
			"Mail Filing Source Message",
			suggestion.source_message,
			"status",
			"Abgelegt",
			update_modified=False,
		)
	return {
		"status": decision_status,
		"folder": target.name,
		"folder_path": target.folder_path,
		"recorded": True,
	}


def dismiss_filing(suggestion_id: str) -> dict[str, str]:
	suggestion_id = str(suggestion_id or "").strip()
	with filelock(f"mail-filing-{suggestion_id}", timeout=10):
		return _dismiss_filing_locked(suggestion_id)


def _dismiss_filing_locked(suggestion_id: str) -> dict[str, str]:
	if not suggestion_id or not frappe.db.exists("Mail Filing Suggestion", suggestion_id):
		frappe.throw(_("Der Ablagevorschlag wurde nicht gefunden."), frappe.DoesNotExistError)
	suggestion = frappe.get_doc("Mail Filing Suggestion", suggestion_id)
	if not _can_decide(suggestion):
		frappe.throw(_("Der Ablagevorschlag gehört zu einem anderen Benutzer."), frappe.PermissionError)
	if suggestion.status not in {"Vorgeschlagen", "Fehler"}:
		frappe.throw(_("Über diesen Ablagevorschlag wurde bereits entschieden."))
	suggestion.db_set(
		{"status": "Verworfen", "decided_on": now_datetime(), "error": ""}, update_modified=True
	)
	return {"status": "Verworfen"}
