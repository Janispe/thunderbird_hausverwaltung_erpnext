from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Any

import frappe
from frappe.utils import now_datetime

from .providers.base import ArchiveMessage, MailArchiveProvider

PROPERTY_KEY_PREFIX = "hv-immobilie-"
NO_PROPERTY_KEY = "hv-keine-immobilie"
TAG_COLORS = ("#1F6FEB", "#8250DF", "#BF3989", "#0A7C66", "#9A6700", "#CF222E")


def property_keyword(immobilie: str) -> str:
	stable = str(immobilie or "").strip().casefold().encode("utf-8")
	return f"{PROPERTY_KEY_PREFIX}{hashlib.sha256(stable).hexdigest()[:16]}"


def is_managed_keyword(keyword: str) -> bool:
	value = str(keyword or "").casefold()
	return value == NO_PROPERTY_KEY or value.startswith(PROPERTY_KEY_PREFIX)


def keyword_patch(current: set[str], desired: set[str]) -> dict[str, bool | None]:
	managed = {keyword.casefold() for keyword in current if is_managed_keyword(keyword)}
	desired_keywords = {keyword.casefold() for keyword in desired if keyword}
	patch = {keyword: None for keyword in managed - desired_keywords}
	for keyword in desired_keywords - managed:
		patch[keyword] = True
	return patch


def get_tag_definitions() -> list[dict[str, str]]:
	properties = frappe.get_all(
		"Immobilie",
		filters={"parent_immobilie": ["is", "not set"]},
		fields=["name", "bezeichnung"],
		order_by="name asc",
		limit_page_length=0,
	)
	definitions = [
		{
			"key": property_keyword(str(row.name)),
			"tag": str(row.bezeichnung or row.name),
			"color": TAG_COLORS[index % len(TAG_COLORS)],
		}
		for index, row in enumerate(properties)
	]
	definitions.append({"key": NO_PROPERTY_KEY, "tag": "Keine Immobilie", "color": "#6E7781"})
	return definitions


@dataclass(frozen=True)
class TagContext:
	property_by_mailbox: dict[str, str]

	def keywords_for_mailbox(self, mailbox_id: str) -> set[str]:
		immobilie = self.property_by_mailbox.get(str(mailbox_id or ""), "")
		return {property_keyword(immobilie)} if immobilie else set()


def build_tag_context(account_name: str) -> TagContext:
	folders = frappe.get_all(
		"Mail Archive Folder",
		filters={"archive_account": account_name, "provider_exists": 1},
		fields=[
			"name",
			"provider_mailbox_id",
			"parent_mailbox_id",
			"reference_doctype",
			"reference_name",
		],
		limit_page_length=0,
	)
	by_provider_id = {str(row.provider_mailbox_id): row for row in folders}
	provider_id_by_name = {str(row.name): str(row.provider_mailbox_id) for row in folders}

	properties = frappe.get_all(
		"Immobilie",
		fields=[
			"name",
			"parent_immobilie",
			"custom_immobilien_archivordner",
			"custom_mieter_archivordner",
		],
		limit_page_length=0,
	)
	property_roots = {
		str(row.name): str(row.parent_immobilie or row.name) for row in properties
	}
	explicit_roots: dict[str, str] = {}
	for row in properties:
		if row.parent_immobilie:
			continue
		for folder_name in (
			row.custom_immobilien_archivordner,
			row.custom_mieter_archivordner,
		):
			provider_id = provider_id_by_name.get(str(folder_name or ""), "")
			if provider_id:
				explicit_roots[provider_id] = str(row.name)

	contracts = {
		str(row.name): property_roots.get(str(row.immobilie or ""), str(row.immobilie or ""))
		for row in frappe.get_all(
			"Mietvertrag",
			filters={"docstatus": ["<", 2]},
			fields=["name", "immobilie"],
			limit_page_length=0,
		)
	}
	cache: dict[str, str] = {}

	def resolve(mailbox_id: str, visiting: set[str] | None = None) -> str:
		if mailbox_id in cache:
			return cache[mailbox_id]
		if mailbox_id in explicit_roots:
			cache[mailbox_id] = explicit_roots[mailbox_id]
			return cache[mailbox_id]
		visiting = visiting or set()
		if mailbox_id in visiting:
			return ""
		row = by_provider_id.get(mailbox_id)
		if not row:
			return ""
		reference_doctype = str(row.reference_doctype or "")
		reference_name = str(row.reference_name or "")
		if reference_doctype == "Mietvertrag" and contracts.get(reference_name):
			result = contracts[reference_name]
		elif reference_doctype == "Immobilie" and reference_name:
			result = property_roots.get(reference_name, reference_name)
		else:
			parent_id = str(row.parent_mailbox_id or "")
			result = resolve(parent_id, visiting | {mailbox_id}) if parent_id else ""
		cache[mailbox_id] = result
		return result

	return TagContext(
		property_by_mailbox={mailbox_id: resolve(mailbox_id) for mailbox_id in by_provider_id}
	)


def apply_managed_tags(
	provider: MailArchiveProvider,
	messages: list[ArchiveMessage],
	folders: dict[str, Any],
	context: TagContext,
) -> dict[str, Any]:
	updates: dict[str, dict[str, bool | None]] = {}
	by_tag: Counter[str] = Counter()
	considered = 0
	for message in messages:
		targets = [
			mailbox_id
			for mailbox_id in message.mailbox_ids
			if mailbox_id in folders and folders[mailbox_id].selectable_target
		]
		if len(targets) != 1:
			continue
		considered += 1
		desired = context.keywords_for_mailbox(targets[0])
		if desired:
			by_tag.update(desired)
		else:
			by_tag["untagged"] += 1
		patch = keyword_patch(set(message.keywords), desired)
		if patch:
			updates[message.id] = patch
	provider.patch_keywords(updates)
	return {"considered": considered, "updated": len(updates), "by_tag": dict(by_tag)}


def backfill_account_tags(
	account: Any,
	provider: MailArchiveProvider,
	folders: dict[str, Any],
	context: TagContext,
) -> dict[str, Any]:
	limit = max(int(account.max_messages_per_run or 2000), 100)
	filters: dict[str, Any] = {
		"archive_account": account.name,
		"status": ["!=", "Gelöscht"],
		"actual_mailbox_id": ["!=", ""],
	}
	cursor = str(account.tag_sync_cursor or "")
	if cursor:
		filters["name"] = [">", cursor]
	rows = frappe.get_all(
		"Mail Archive Message",
		filters=filters,
		fields=["name", "provider_message_id"],
		order_by="name asc",
		limit_page_length=limit,
	)
	provider_ids = [str(row.provider_message_id) for row in rows if row.provider_message_id]
	messages, _state = provider.get_messages(provider_ids)
	summary = apply_managed_tags(provider, messages, folders, context)
	completed = len(rows) < limit
	account.db_set(
		{
			"tag_sync_completed": int(completed),
			"tag_sync_cursor": "" if completed else str(rows[-1].name),
			"last_tag_sync_on": now_datetime(),
		},
		update_modified=False,
	)
	summary.update({"scanned": len(rows), "completed": completed})
	return summary


def reset_tag_sync(account_name: str) -> None:
	frappe.db.set_value(
		"Mail Archive Account",
		account_name,
		{"tag_sync_completed": 0, "tag_sync_cursor": ""},
		update_modified=False,
	)


def invalidate_tag_sync(doc: Any | None = None, method: str | None = None) -> None:
	del method
	if frappe.flags.in_install or frappe.flags.in_migrate:
		return
	account_names: list[str]
	if getattr(doc, "doctype", "") == "Mail Archive Folder" and getattr(doc, "archive_account", ""):
		account_names = [str(doc.archive_account)]
	else:
		account_names = frappe.get_all(
			"Mail Archive Account", filters={"enabled": 1}, pluck="name", limit_page_length=0
		)
	for account_name in account_names:
		reset_tag_sync(account_name)
		frappe.enqueue(
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.sync.sync_account",
			queue="long",
			job_id=f"mail-archive-sync::{frappe.local.site}::{account_name}",
			deduplicate=True,
			enqueue_after_commit=True,
			account_name=account_name,
		)
