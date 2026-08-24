from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

import frappe
from frappe.utils import convert_utc_to_system_timezone, get_datetime, now_datetime

from .classifier import rebuild_folder_centroids
from .embeddings import (
	EmbeddingError,
	content_hash,
	get_embedding_client,
	message_embedding_text,
	vector_json,
)
from .providers import get_provider
from .providers.base import ArchiveMailbox, ArchiveMessage, ChangeStateUnavailable, MailArchiveProvider

EXCLUDED_TARGET_ROLES = {"inbox", "sent", "drafts", "junk", "trash", "submission"}
EMBEDDING_BATCH_SIZE = 32


def folder_record_name(account_name: str, mailbox_id: str) -> str:
	key = f"{account_name}\0{mailbox_id}".encode()
	return f"MAF-{hashlib.sha256(key).hexdigest()[:32]}"


def message_record_name(account_name: str, message_id: str) -> str:
	key = f"{account_name}\0{message_id}".encode()
	return f"MAM-{hashlib.sha256(key).hexdigest()[:32]}"


def build_mailbox_paths(mailboxes: Iterable[ArchiveMailbox]) -> dict[str, str]:
	by_id = {mailbox.id: mailbox for mailbox in mailboxes}
	cache: dict[str, str] = {}

	def resolve(mailbox_id: str, visiting: set[str]) -> str:
		if mailbox_id in cache:
			return cache[mailbox_id]
		mailbox = by_id[mailbox_id]
		if mailbox_id in visiting or not mailbox.parent_id or mailbox.parent_id not in by_id:
			path = mailbox.name
		else:
			path = f"{resolve(mailbox.parent_id, visiting | {mailbox_id})}/{mailbox.name}"
		cache[mailbox_id] = path
		return path

	for mailbox_id in by_id:
		resolve(mailbox_id, set())
	return cache


def _is_descendant(mailbox_id: str, root_id: str, by_id: dict[str, ArchiveMailbox]) -> bool:
	current = mailbox_id
	seen: set[str] = set()
	while current and current not in seen:
		seen.add(current)
		parent = by_id.get(current).parent_id if current in by_id else None
		if parent == root_id:
			return True
		current = parent or ""
	return False


def sync_folder_records(account: Any, mailboxes: list[ArchiveMailbox]) -> dict[str, Any]:
	paths = build_mailbox_paths(mailboxes)
	by_id = {mailbox.id: mailbox for mailbox in mailboxes}
	root_id = str(account.archive_root_mailbox_id or "").strip()
	if root_id and root_id not in by_id:
		raise ValueError("Die konfigurierte Archiv-Wurzel wurde auf dem Mailserver nicht gefunden.")
	existing_ids = set(
		frappe.get_all(
			"Mail Archive Folder",
			filters={"archive_account": account.name},
			pluck="provider_mailbox_id",
			limit_page_length=0,
		)
	)
	missing_ids = existing_ids - set(by_id)
	if missing_ids:
		frappe.db.set_value(
			"Mail Archive Folder",
			{"archive_account": account.name, "provider_mailbox_id": ["in", list(missing_ids)]},
			"selectable_target",
			0,
			update_modified=False,
		)
	for mailbox in mailboxes:
		name = folder_record_name(account.name, mailbox.id)
		role = str(mailbox.role or "").casefold()
		within_root = not root_id or _is_descendant(mailbox.id, root_id, by_id)
		values = {
			"archive_account": account.name,
			"provider_mailbox_id": mailbox.id,
			"folder_name": mailbox.name,
			"folder_path": paths[mailbox.id],
			"parent_mailbox_id": mailbox.parent_id or "",
			"role": mailbox.role or "",
			"last_synced": now_datetime(),
		}
		if frappe.db.exists("Mail Archive Folder", name):
			if not within_root or role in EXCLUDED_TARGET_ROLES:
				values["selectable_target"] = 0
			frappe.db.set_value("Mail Archive Folder", name, values, update_modified=False)
		else:
			values["selectable_target"] = int(within_root and role not in EXCLUDED_TARGET_ROLES)
			frappe.get_doc({"doctype": "Mail Archive Folder", "name": name, **values}).insert(
				ignore_permissions=True
			)
	return {
		"paths": paths,
		"folders": {
			row.provider_mailbox_id: row
			for row in frappe.get_all(
				"Mail Archive Folder",
				filters={"archive_account": account.name},
				fields=[
					"name",
					"provider_mailbox_id",
					"folder_path",
					"role",
					"selectable_target",
				],
			)
		},
	}


def _message_date(value: str | None) -> Any:
	if not value:
		return None
	try:
		result = get_datetime(value)
		if result.tzinfo:
			result = convert_utc_to_system_timezone(result).replace(tzinfo=None)
		return result
	except (TypeError, ValueError):
		return None


def _sender_email(message: ArchiveMessage) -> str:
	return next((item.get("email", "") for item in message.sender if item.get("email")), "")


def _participants(message: ArchiveMessage) -> str:
	return json.dumps(
		{"from": message.sender, "to": message.to, "cc": message.cc},
		ensure_ascii=False,
		separators=(",", ":"),
	)


def _mailbox_truth(message: ArchiveMessage, folders: dict[str, Any]) -> tuple[str, str, str]:
	targets = [
		folders[mailbox_id]
		for mailbox_id in message.mailbox_ids
		if mailbox_id in folders and folders[mailbox_id].selectable_target
	]
	if len(targets) == 1:
		return targets[0].provider_mailbox_id, targets[0].folder_path, "Archiviert"
	roles = {
		str(folders[mailbox_id].role or "").casefold()
		for mailbox_id in message.mailbox_ids
		if mailbox_id in folders
	}
	if "inbox" in roles:
		return "", "", "Posteingang"
	return "", "", "Nicht klassifizierbar"


def upsert_messages(
	account: Any,
	messages: list[ArchiveMessage],
	folders: dict[str, Any],
	*,
	embedding_optional: bool = False,
) -> tuple[int, set[str]]:
	client = get_embedding_client(account) if account.embedding_enabled else None
	prepared: list[dict[str, Any]] = []
	for message in messages:
		name = message_record_name(account.name, message.id)
		existing = (
			frappe.db.get_value(
				"Mail Archive Message",
				name,
				["content_hash", "embedding", "embedding_model", "actual_mailbox_id"],
				as_dict=True,
			)
			if frappe.db.exists("Mail Archive Message", name)
			else None
		)
		mailbox_id, folder_path, status = _mailbox_truth(message, folders)
		text = message_embedding_text(message)
		model_key = client.model_key if client else ""
		hash_value = content_hash(text, model_key) if client and text else ""
		prepared.append(
			{
				"message": message,
				"name": name,
				"existing": existing,
				"mailbox_id": mailbox_id,
				"folder_path": folder_path,
				"status": status,
				"text": text,
				"model_key": model_key,
				"content_hash": hash_value,
				"embedding": (
					existing.embedding
					if existing
					and existing.content_hash == hash_value
					and existing.embedding_model == model_key
					else ""
				),
			}
		)
	if client:
		needs_embedding = [item for item in prepared if item["text"] and not item["embedding"]]
		try:
			for offset in range(0, len(needs_embedding), EMBEDDING_BATCH_SIZE):
				batch = needs_embedding[offset : offset + EMBEDDING_BATCH_SIZE]
				vectors = client.embed([item["text"] for item in batch])
				for item, vector in zip(batch, vectors, strict=True):
					item["embedding"] = vector_json(vector)
		except EmbeddingError:
			if not embedding_optional:
				raise
			frappe.logger("mail_archive").warning(
				"Embedding service unavailable; filing suggestion continues with deterministic signals",
				exc_info=True,
			)
	touched_mailboxes: set[str] = set()
	for item in prepared:
		message = item["message"]
		existing = item["existing"]
		if existing and existing.actual_mailbox_id:
			touched_mailboxes.add(existing.actual_mailbox_id)
		if item["mailbox_id"]:
			touched_mailboxes.add(item["mailbox_id"])
		values = {
			"archive_account": account.name,
			"provider_message_id": message.id,
			"rfc_message_id": message.rfc_message_ids[0] if message.rfc_message_ids else "",
			"thread_id": message.thread_id,
			"status": item["status"],
			"subject": message.subject[:998],
			"sender_email": _sender_email(message),
			"participants": _participants(message),
			"received_at": _message_date(message.received_at),
			"preview": message.preview[:2000],
			"has_attachment": int(message.has_attachment),
			"mailbox_ids": json.dumps(message.mailbox_ids, separators=(",", ":")),
			"actual_mailbox_id": item["mailbox_id"],
			"actual_folder_path": item["folder_path"],
			"content_hash": item["content_hash"],
			"embedding_model": item["model_key"] if item["embedding"] else "",
			"embedding_dimension": len(json.loads(item["embedding"])) if item["embedding"] else 0,
			"embedding": item["embedding"],
			"last_synced": now_datetime(),
		}
		if existing:
			frappe.db.set_value("Mail Archive Message", item["name"], values, update_modified=False)
		else:
			frappe.get_doc({"doctype": "Mail Archive Message", "name": item["name"], **values}).insert(
				ignore_permissions=True
			)
	return len(prepared), touched_mailboxes


def _delete_messages(account_name: str, ids: Iterable[str]) -> set[str]:
	touched: set[str] = set()
	for provider_id in ids:
		name = message_record_name(account_name, provider_id)
		mailbox_id = frappe.db.get_value("Mail Archive Message", name, "actual_mailbox_id")
		if mailbox_id:
			touched.add(mailbox_id)
		if frappe.db.exists("Mail Archive Message", name):
			frappe.db.set_value(
				"Mail Archive Message",
				name,
				{"status": "Gelöscht", "actual_mailbox_id": "", "actual_folder_path": ""},
				update_modified=False,
			)
	return touched


def _initial_sync(
	account: Any, provider: MailArchiveProvider, folders: dict[str, Any]
) -> tuple[int, set[str], bool, str]:
	target_ids = sorted(
		(mailbox_id for mailbox_id, folder in folders.items() if folder.selectable_target),
		key=lambda mailbox_id: folders[mailbox_id].folder_path.casefold(),
	)
	try:
		cursor = json.loads(account.sync_cursor or "{}")
	except ValueError:
		cursor = {}
	positions = cursor.get("positions") or {}
	completed = set(cursor.get("completed") or [])
	processed = 0
	touched: set[str] = set()
	# Capture the Email state before the first page. Changes during a multi-run initial scan are then
	# replayed by Email/changes after completion instead of being silently included in a newer state.
	baseline_state = str(account.last_email_state or "") or provider.get_current_state()
	if not account.last_email_state and baseline_state:
		account.db_set("last_email_state", baseline_state, update_modified=False)
	for mailbox_id in target_ids:
		if mailbox_id in completed or processed >= account.max_messages_per_run:
			continue
		position = max(int(positions.get(mailbox_id) or 0), 0)
		limit = min(int(account.max_messages_per_run) - processed, 500)
		ids, total, _query_state = provider.query_message_ids(
			mailbox_id=mailbox_id, position=position, limit=limit
		)
		messages, _state = provider.get_messages(ids)
		stored, changed = upsert_messages(account, messages, folders)
		processed += stored
		touched.update(changed)
		position += len(ids)
		if len(ids) < limit or total is not None and position >= total:
			completed.add(mailbox_id)
			positions.pop(mailbox_id, None)
		else:
			positions[mailbox_id] = position
			break
	finished = set(target_ids).issubset(completed)
	if finished:
		account.db_set("sync_cursor", "", update_modified=False)
	else:
		account.db_set(
			"sync_cursor",
			json.dumps(
				{"positions": positions, "completed": sorted(completed)},
				separators=(",", ":"),
			),
			update_modified=False,
		)
	return processed, touched, finished, baseline_state


def _incremental_sync(
	account: Any, provider: MailArchiveProvider, folders: dict[str, Any]
) -> tuple[int, set[str], str]:
	changes = provider.get_changes(
		str(account.last_email_state), max_changes=int(account.max_messages_per_run)
	)
	ids = list(dict.fromkeys((*changes.created, *changes.updated)))
	messages, _state = provider.get_messages(ids)
	stored, touched = upsert_messages(account, messages, folders)
	touched.update(_delete_messages(account.name, changes.destroyed))
	return stored, touched, changes.new_state


def sync_account(account_name: str) -> dict[str, Any]:
	account = frappe.get_doc("Mail Archive Account", account_name)
	if not account.enabled:
		return {"status": "disabled", "stored": 0}
	account.db_set({"sync_status": "Läuft", "last_error": ""}, update_modified=False)
	try:
		provider = get_provider(account)
		mailboxes = provider.list_mailboxes()
		folder_context = sync_folder_records(account, mailboxes)
		folders = folder_context["folders"]
		if account.initial_sync_completed and account.last_email_state:
			try:
				stored, touched, state = _incremental_sync(account, provider, folders)
				finished = True
			except ChangeStateUnavailable:
				account.db_set(
					{"initial_sync_completed": 0, "last_email_state": "", "sync_cursor": ""},
					update_modified=False,
				)
				stored, touched, finished, state = _initial_sync(account, provider, folders)
		else:
			stored, touched, finished, state = _initial_sync(account, provider, folders)
		if touched:
			rebuild_folder_centroids(account.name, touched)
		account.db_set(
			{
				"provider_account_id": getattr(provider, "account_id", account.provider_account_id),
				"initial_sync_completed": int(finished),
				"last_email_state": state,
				"last_sync_on": now_datetime(),
				"sync_status": "Erfolgreich",
				"last_error": "",
			},
			update_modified=False,
		)
		return {"status": "success", "stored": stored, "initial_sync_completed": finished}
	except Exception as exc:
		frappe.db.rollback()
		account = frappe.get_doc("Mail Archive Account", account_name)
		account.db_set({"sync_status": "Fehler", "last_error": str(exc)[:10000]}, update_modified=False)
		frappe.db.commit()
		raise


def enqueue_account_sync(account_name: str) -> dict[str, str]:
	frappe.db.set_value(
		"Mail Archive Account", account_name, "sync_status", "Eingereiht", update_modified=False
	)
	frappe.enqueue(
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.sync.sync_account",
		queue="long",
		job_id=f"mail-archive-sync::{frappe.local.site}::{account_name}",
		deduplicate=True,
		account_name=account_name,
	)
	return {"status": "queued", "account": account_name}


def enqueue_enabled_account_syncs() -> None:
	for account_name in frappe.get_all("Mail Archive Account", filters={"enabled": 1}, pluck="name"):
		enqueue_account_sync(account_name)
