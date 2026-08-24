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


def _select_account(account_addresses: Any = None) -> Any:
	if isinstance(account_addresses, str):
		try:
			decoded = json.loads(account_addresses)
		except ValueError:
			decoded = account_addresses
		account_addresses = decoded
	if isinstance(account_addresses, (list, tuple, set)):
		requested = set(normalize_account_addresses("\n".join(str(item) for item in account_addresses)))
	else:
		requested = set(normalize_account_addresses(str(account_addresses or "")))
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


def get_filing_suggestions(
	header_message_id: str, account_addresses: Any = None, limit: int = 3
) -> dict[str, Any]:
	value = _normalize_rfc_message_id(header_message_id)
	if not value:
		frappe.throw(_("Die Nachricht hat keine RFC Message-ID und kann nicht eindeutig zugeordnet werden."))
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
	if suggestion.status not in {"Vorgeschlagen", "Fehler"}:
		frappe.throw(_("Über diesen Ablagevorschlag wurde bereits entschieden."))
	if not folder or not frappe.db.exists("Mail Archive Folder", folder):
		frappe.throw(_("Der Archivordner wurde nicht gefunden."), frappe.DoesNotExistError)
	target = frappe.get_doc("Mail Archive Folder", folder)
	if target.archive_account != suggestion.archive_account or not target.selectable_target:
		frappe.throw(_("Dieser Ordner ist für den Ablagevorschlag nicht zulässig."), frappe.PermissionError)
	message = frappe.get_doc("Mail Archive Message", suggestion.archive_message)
	account = frappe.get_doc("Mail Archive Account", suggestion.archive_account)
	old_mailbox_id = str(message.actual_mailbox_id or "")
	try:
		get_provider(account).move_message(message.provider_message_id, target.provider_mailbox_id)
	except Exception as exc:
		suggestion.db_set({"status": "Fehler", "error": str(exc)[:10000]}, update_modified=True)
		raise
	message.db_set(
		{
			"status": "Archiviert",
			"mailbox_ids": json.dumps([target.provider_mailbox_id], separators=(",", ":")),
			"actual_mailbox_id": target.provider_mailbox_id,
			"actual_folder_path": target.folder_path,
			"last_synced": now_datetime(),
		},
		update_modified=False,
	)
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
	mailbox_ids = sorted({old_mailbox_id, target.provider_mailbox_id} - {""})
	frappe.enqueue(
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.classifier.rebuild_folder_centroids",
		queue="short",
		enqueue_after_commit=True,
		job_id=f"mail-centroids::{frappe.local.site}::{suggestion.name}",
		deduplicate=True,
		account_name=account.name,
		mailbox_ids=mailbox_ids,
	)
	return {
		"status": decision_status,
		"folder": target.name,
		"folder_path": target.folder_path,
		"moved": True,
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
