from __future__ import annotations

import hashlib
import imaplib
import json
import re
import ssl
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from ..doctype.mail_filing_source_account.mail_filing_source_account import normalize_watched_folders
from .classifier import build_suggestions
from .embeddings import (
	EmbeddingError,
	content_hash,
	get_embedding_client,
	message_embedding_text,
	vector_json,
)
from .filing import _normalize_rfc_message_id
from .providers.base import ArchiveMessage
from .sync import _message_date
from .tagging import TagContext, apply_contract_tags, build_tag_context


MAX_SOURCE_BODY_CHARACTERS = 64_000


def source_message_record_name(source_account: str, folder: str, uidvalidity: str, uid: str | int) -> str:
	key = f"{source_account}\0{folder}\0{uidvalidity}\0{uid}".encode()
	return f"MFSM-{hashlib.sha256(key).hexdigest()[:32]}"


def _decode_header(value: str | None) -> str:
	parts: list[str] = []
	for item, charset in decode_header(str(value or "")):
		if isinstance(item, bytes):
			try:
				parts.append(item.decode(charset or "utf-8", errors="replace"))
			except LookupError:
				parts.append(item.decode("utf-8", errors="replace"))
		else:
			parts.append(item)
	return "".join(parts).strip()


def _addresses(values: list[str]) -> tuple[dict[str, str], ...]:
	result: list[dict[str, str]] = []
	seen: set[str] = set()
	for name, address in getaddresses(values):
		email_address = str(address or "").strip().casefold()
		if not email_address or email_address in seen:
			continue
		result.append({"name": _decode_header(name), "email": email_address})
		seen.add(email_address)
	return tuple(result)


def _message_text(message: EmailMessage) -> str:
	parts: list[str] = []
	if message.is_multipart():
		for part in message.walk():
			if part.get_content_maintype() == "multipart":
				continue
			if part.get_content_disposition() == "attachment" or part.get_content_type() != "text/plain":
				continue
			try:
				parts.append(str(part.get_content()))
			except (LookupError, UnicodeError):
				payload = part.get_payload(decode=True) or b""
				parts.append(payload.decode("utf-8", errors="replace"))
	else:
		try:
			parts.append(str(message.get_content()))
		except (LookupError, UnicodeError):
			payload = message.get_payload(decode=True) or b""
			parts.append(payload.decode("utf-8", errors="replace"))
	return "\n\n".join(part.strip() for part in parts if part.strip())[:MAX_SOURCE_BODY_CHARACTERS]


def _received_at(message: EmailMessage) -> str | None:
	try:
		value = parsedate_to_datetime(str(message.get("Date") or ""))
		return value.isoformat() if value else None
	except (TypeError, ValueError, OverflowError):
		return None


def parse_imap_message(
	raw_message: bytes, *, uid: str, folder: str, keywords: tuple[str, ...] = ()
) -> ArchiveMessage:
	parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
	sender = _addresses(parsed.get_all("From", []))
	to = _addresses(parsed.get_all("To", []))
	cc = _addresses(parsed.get_all("Cc", []))
	body = _message_text(parsed)
	attachments = list(parsed.iter_attachments())
	rfc_message_id = _normalize_rfc_message_id(parsed.get("Message-ID"))
	return ArchiveMessage(
		id=str(uid),
		thread_id="",
		mailbox_ids=(folder,),
		keywords=tuple(str(keyword).casefold() for keyword in keywords),
		rfc_message_ids=(rfc_message_id,) if rfc_message_id else (),
		in_reply_to=tuple(
			filter(None, (_normalize_rfc_message_id(item) for item in parsed.get_all("In-Reply-To", [])))
		),
		references=tuple(
			filter(
				None,
				(
					_normalize_rfc_message_id(item)
					for value in parsed.get_all("References", [])
					for item in str(value).split()
				),
			)
		),
		subject=_decode_header(parsed.get("Subject")),
		sender=sender,
		to=to,
		cc=cc,
		received_at=_received_at(parsed),
		preview=re.sub(r"\s+", " ", body).strip()[:2000],
		text_body=body,
		has_attachment=bool(attachments),
		raw={
			"attachment_count": len(attachments),
			"attachment_names": [
				_decode_header(item.get_filename()) for item in attachments if item.get_filename()
			],
		},
	)


class IMAPSourceClient:
	def __init__(self, account: Any) -> None:
		self.account = account
		self.connection: imaplib.IMAP4 | None = None

	@classmethod
	def from_account(cls, account: Any) -> "IMAPSourceClient":
		return cls(account)

	def __enter__(self) -> "IMAPSourceClient":
		context = ssl.create_default_context()
		if not self.account.verify_ssl:
			context.check_hostname = False
			context.verify_mode = ssl.CERT_NONE
		if self.account.connection_security == "SSL/TLS":
			self.connection = imaplib.IMAP4_SSL(
				self.account.server_host,
				int(self.account.server_port),
				ssl_context=context,
				timeout=30,
			)
		else:
			self.connection = imaplib.IMAP4(
				self.account.server_host, int(self.account.server_port), timeout=30
			)
			self.connection.starttls(ssl_context=context)
		password = self.account.get_password("password") or ""
		status, _data = self.connection.login(self.account.username, password)
		if status != "OK":
			raise RuntimeError("IMAP-Anmeldung fehlgeschlagen.")
		return self

	def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
		if not self.connection:
			return
		try:
			self.connection.logout()
		except (imaplib.IMAP4.error, OSError):
			pass
		self.connection = None

	def _require_connection(self) -> imaplib.IMAP4:
		if not self.connection:
			raise RuntimeError("Die IMAP-Verbindung ist nicht geöffnet.")
		return self.connection

	def test_connection(self) -> dict[str, Any]:
		connection = self._require_connection()
		folders = normalize_watched_folders(self.account.watched_folders)
		for folder in folders:
			status, _data = connection.select(folder, readonly=True)
			if status != "OK":
				raise RuntimeError(f"IMAP-Ordner nicht gefunden: {folder}")
		return {
			"server": self.account.server_host,
			"watched_folders": len(folders),
			"capabilities": sorted(
				item.decode(errors="replace") if isinstance(item, bytes) else str(item)
				for item in connection.capabilities
			),
		}

	def select_folder(self, folder: str, *, readonly: bool = False) -> str:
		connection = self._require_connection()
		status, _data = connection.select(folder, readonly=readonly)
		if status != "OK":
			raise RuntimeError(f"IMAP-Ordner nicht gefunden: {folder}")
		_response_name, response_values = connection.response("UIDVALIDITY")
		if not response_values:
			raise RuntimeError(f"IMAP-Ordner liefert keine UIDVALIDITY: {folder}")
		value = response_values[-1]
		return value.decode(errors="replace") if isinstance(value, bytes) else str(value)

	def search_uids(self, criteria: str) -> list[str]:
		status, data = self._require_connection().uid("search", None, criteria)
		if status != "OK":
			raise RuntimeError("IMAP-Suche fehlgeschlagen.")
		return [item.decode() for item in (data[0] or b"").split()]

	def fetch_message(self, uid: str, folder: str) -> ArchiveMessage:
		status, data = self._require_connection().uid("fetch", str(uid), "(UID FLAGS BODY.PEEK[])")
		if status != "OK":
			raise RuntimeError(f"IMAP-Nachricht {uid} konnte nicht geladen werden.")
		raw_message = next(
			(item[1] for item in data if isinstance(item, tuple) and len(item) > 1),
			None,
		)
		if not raw_message:
			raise RuntimeError(f"IMAP-Nachricht {uid} enthielt keinen Nachrichtentext.")
		metadata = next(
			(item[0] for item in data if isinstance(item, tuple) and item),
			b"",
		)
		if isinstance(metadata, str):
			metadata = metadata.encode()
		match = re.search(rb"\bFLAGS\s+\(([^)]*)\)", metadata or b"", re.IGNORECASE)
		keywords = tuple(
			item.decode(errors="replace") for item in ((match.group(1) if match else b"").split()) if item
		)
		return parse_imap_message(raw_message, uid=str(uid), folder=folder, keywords=keywords)

	def patch_keywords(self, updates: dict[str, dict[str, bool | None]]) -> None:
		connection = self._require_connection()
		for uid, changes in updates.items():
			for keyword, enabled in changes.items():
				value = str(keyword or "").strip().casefold()
				if not re.fullmatch(r"[a-z0-9$._-]+", value):
					raise RuntimeError(f"Ungültiges IMAP-Schlüsselwort: {keyword}")
				operation = "+FLAGS.SILENT" if enabled else "-FLAGS.SILENT"
				status, data = connection.uid("store", str(uid), operation, f"({value})")
				if status != "OK":
					detail = next(
						(
							item.decode(errors="replace") if isinstance(item, bytes) else str(item)
							for item in data or []
							if item
						),
						"Unbekannter Fehler",
					)
					raise RuntimeError(
						f"IMAP-Schlüsselwort {value} konnte für UID {uid} nicht gespeichert werden: {detail}"
					)


def _participants(message: ArchiveMessage) -> str:
	return json.dumps(
		{"from": message.sender, "to": message.to, "cc": message.cc},
		ensure_ascii=False,
		separators=(",", ":"),
	)


def _sender_email(message: ArchiveMessage) -> str:
	return next((item.get("email", "") for item in message.sender if item.get("email")), "")


def upsert_source_message(
	source_account: Any,
	archive_account: Any,
	message: ArchiveMessage,
	*,
	folder: str,
	uidvalidity: str,
) -> Any:
	name = source_message_record_name(source_account.name, folder, uidvalidity, message.id)
	existing = (
		frappe.db.get_value(
			"Mail Filing Source Message",
			name,
			["content_hash", "embedding", "embedding_model", "status"],
			as_dict=True,
		)
		if frappe.db.exists("Mail Filing Source Message", name)
		else None
	)
	client = get_embedding_client(archive_account) if archive_account.embedding_enabled else None
	text = message_embedding_text(message)
	model_key = client.model_key if client else ""
	hash_value = content_hash(text, model_key) if client and text else ""
	embedding = (
		existing.embedding
		if existing and existing.content_hash == hash_value and existing.embedding_model == model_key
		else ""
	)
	if client and text and not embedding:
		try:
			embedding = vector_json(client.embed([text])[0])
		except EmbeddingError:
			frappe.logger("mail_archive").warning(
				"Embedding service unavailable while indexing source mailbox",
				exc_info=True,
			)
	values = {
		"source_account": source_account.name,
		"archive_account": archive_account.name,
		"imap_folder": folder,
		"imap_uidvalidity": uidvalidity,
		"imap_uid": str(message.id),
		"rfc_message_id": message.rfc_message_ids[0] if message.rfc_message_ids else "",
		"thread_id": message.thread_id,
		"status": existing.status if existing and existing.status == "Abgelegt" else "Offen",
		"subject": message.subject[:998],
		"sender_email": _sender_email(message),
		"participants": _participants(message),
		"received_at": _message_date(message.received_at),
		"preview": message.preview[:2000],
		"has_attachment": int(message.has_attachment),
		"attachment_count": int(message.raw.get("attachment_count") or 0),
		"attachment_names": json.dumps(
			message.raw.get("attachment_names") or [], ensure_ascii=False, separators=(",", ":")
		),
		"body_text": message.text_body,
		"content_hash": hash_value,
		"embedding_model": model_key if embedding else "",
		"embedding_dimension": len(json.loads(embedding)) if embedding else 0,
		"embedding": embedding,
		"last_synced": now_datetime(),
	}
	if existing:
		frappe.db.set_value("Mail Filing Source Message", name, values, update_modified=False)
	else:
		frappe.get_doc({"doctype": "Mail Filing Source Message", "name": name, **values}).insert(
			ignore_permissions=True
		)
	document = frappe.get_doc("Mail Filing Source Message", name)
	result = build_suggestions(archive_account, document, limit=3)
	frappe.db.set_value(
		"Mail Filing Source Message",
		name,
		{
			"model_version": result["model_version"],
			"reasoning": result["reasoning"][:2000],
			"candidates": json.dumps(result["candidates"], ensure_ascii=False, separators=(",", ":")),
		},
		update_modified=False,
	)
	return frappe.get_doc("Mail Filing Source Message", name)


def _cursor(value: str | None) -> dict[str, Any]:
	try:
		decoded = json.loads(value or "{}")
	except ValueError:
		return {"folders": {}}
	return decoded if isinstance(decoded, dict) else {"folders": {}}


def backfill_source_tags(
	source_account: Any,
	client: IMAPSourceClient,
	context: TagContext,
) -> dict[str, Any]:
	state = _cursor(getattr(source_account, "tag_sync_cursor", ""))
	folder_state = state.setdefault("folders", {})
	completed = set(state.get("completed") or [])
	processed = 0
	updated = 0
	limit = max(int(source_account.max_messages_per_run or 500), 1)
	folders = normalize_watched_folders(source_account.watched_folders)
	for folder in folders:
		if folder in completed or processed >= limit:
			continue
		uidvalidity = client.select_folder(folder)
		current = folder_state.get(folder) or {}
		last_uid = int(current.get("last_uid") or 0) if current.get("uidvalidity") == uidvalidity else 0
		uids = sorted((int(uid) for uid in client.search_uids("ALL") if int(uid) > last_uid))
		batch_uids = uids[: limit - processed]
		messages = [client.fetch_message(str(uid), folder) for uid in batch_uids]
		if messages:
			summary = apply_contract_tags(client, messages, context)
			updated += int(summary["updated"])
			processed += len(messages)
			last_uid = batch_uids[-1]
		folder_state[folder] = {"uidvalidity": uidvalidity, "last_uid": last_uid}
		if len(batch_uids) == len(uids):
			completed.add(folder)
	finished = set(folders).issubset(completed)
	source_account.db_set(
		{
			"tag_sync_completed": int(finished),
			"tag_sync_cursor": ""
			if finished
			else json.dumps(
				{"folders": folder_state, "completed": sorted(completed)},
				ensure_ascii=False,
				separators=(",", ":"),
			),
			"last_tag_sync_on": now_datetime(),
		},
		update_modified=False,
	)
	return {"scanned": processed, "updated": updated, "completed": finished}


def sync_source_account(account_name: str) -> dict[str, Any]:
	source_account = frappe.get_doc("Mail Filing Source Account", account_name)
	if not source_account.enabled:
		return {"status": "disabled", "stored": 0}
	archive_account = frappe.get_doc("Mail Archive Account", source_account.archive_account)
	source_account.db_set({"sync_status": "Läuft", "last_error": ""}, update_modified=False)
	state = _cursor(source_account.sync_cursor)
	folder_state = state.setdefault("folders", {})
	stored = 0
	tag_context = build_tag_context()
	tag_summary: dict[str, Any] = {"completed": bool(getattr(source_account, "tag_sync_completed", 0))}
	try:
		with IMAPSourceClient.from_account(source_account) as client:
			for folder in normalize_watched_folders(source_account.watched_folders):
				if stored >= int(source_account.max_messages_per_run):
					break
				uidvalidity = client.select_folder(folder)
				current = folder_state.get(folder) or {}
				last_uid = (
					int(current.get("last_uid") or 0) if current.get("uidvalidity") == uidvalidity else 0
				)
				uids = [uid for uid in client.search_uids(f"UID {last_uid + 1}:*") if int(uid) > last_uid]
				remaining = int(source_account.max_messages_per_run) - stored
				for uid in uids[:remaining]:
					message = client.fetch_message(uid, folder)
					apply_contract_tags(client, [message], tag_context)
					upsert_source_message(
						source_account,
						archive_account,
						message,
						folder=folder,
						uidvalidity=uidvalidity,
					)
					last_uid = max(last_uid, int(uid))
					stored += 1
				folder_state[folder] = {"uidvalidity": uidvalidity, "last_uid": last_uid}
			if not getattr(source_account, "tag_sync_completed", 0):
				tag_summary = backfill_source_tags(source_account, client, tag_context)
		source_account.db_set(
			{
				"sync_cursor": json.dumps(state, ensure_ascii=False, separators=(",", ":")),
				"last_sync_on": now_datetime(),
				"sync_status": "Erfolgreich",
				"last_error": "",
			},
			update_modified=False,
		)
		return {"status": "success", "stored": stored, "tags": tag_summary}
	except Exception as exc:
		frappe.db.rollback()
		source_account = frappe.get_doc("Mail Filing Source Account", account_name)
		source_account.db_set(
			{"sync_status": "Fehler", "last_error": str(exc)[:10000]}, update_modified=False
		)
		frappe.db.commit()
		raise


def sync_source_message_by_rfc_id(account_name: str, rfc_message_id: str) -> Any | None:
	source_account = frappe.get_doc("Mail Filing Source Account", account_name)
	archive_account = frappe.get_doc("Mail Archive Account", source_account.archive_account)
	value = _normalize_rfc_message_id(rfc_message_id)
	if not value:
		return None
	matches: list[tuple[str, str, str]] = []
	with IMAPSourceClient.from_account(source_account) as client:
		tag_context = build_tag_context()
		for folder in normalize_watched_folders(source_account.watched_folders):
			uidvalidity = client.select_folder(folder)
			escaped = value.replace("\\", "\\\\").replace('"', '\\"')
			uids = client.search_uids(f'HEADER Message-ID "{escaped}"')
			matches.extend((folder, uidvalidity, uid) for uid in uids)
		if len(matches) > 1:
			frappe.throw(
				_("Die RFC Message-ID ist in den überwachten Quellordnern nicht eindeutig."),
				frappe.ValidationError,
			)
		if not matches:
			return None
		folder, uidvalidity, uid = matches[0]
		client.select_folder(folder)
		message = client.fetch_message(uid, folder)
		apply_contract_tags(client, [message], tag_context)
		return upsert_source_message(
			source_account,
			archive_account,
			message,
			folder=folder,
			uidvalidity=uidvalidity,
		)


def enqueue_source_account_sync(account_name: str) -> dict[str, str]:
	frappe.db.set_value(
		"Mail Filing Source Account", account_name, "sync_status", "Eingereiht", update_modified=False
	)
	frappe.enqueue(
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.source_sync.sync_source_account",
		queue="long",
		job_id=f"mail-source-sync::{frappe.local.site}::{account_name}",
		deduplicate=True,
		account_name=account_name,
	)
	return {"status": "queued", "account": account_name}


def enqueue_enabled_source_account_syncs() -> None:
	for account_name in frappe.get_all("Mail Filing Source Account", filters={"enabled": 1}, pluck="name"):
		enqueue_source_account_sync(account_name)
