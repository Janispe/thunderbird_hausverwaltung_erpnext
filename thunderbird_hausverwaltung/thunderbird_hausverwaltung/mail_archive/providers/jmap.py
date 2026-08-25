from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from .base import (
	ArchiveMailbox,
	ArchiveMessage,
	ChangeSet,
	ChangeStateUnavailable,
	MailArchiveProvider,
)

CORE_CAPABILITY = "urn:ietf:params:jmap:core"
MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"
DEFAULT_TIMEOUT = 30
MAX_BODY_VALUE_BYTES = 50_000
MAILBOX_QUERY_PAGE_SIZE = 500


class JMAPError(RuntimeError):
	pass


@dataclass(frozen=True)
class JMAPConfig:
	server_url: str
	username: str
	password: str
	account_id: str = ""
	verify_ssl: bool = True
	timeout: int = DEFAULT_TIMEOUT


def _addresses(values: Any) -> tuple[dict[str, str], ...]:
	result: list[dict[str, str]] = []
	for value in values or []:
		if not isinstance(value, dict):
			continue
		result.append(
			{
				"name": str(value.get("name") or "").strip(),
				"email": str(value.get("email") or "").strip().casefold(),
			}
		)
	return tuple(result)


def _string_tuple(value: Any) -> tuple[str, ...]:
	if isinstance(value, str):
		value = [value]
	return tuple(str(item).strip() for item in (value or []) if str(item or "").strip())


def _extract_text_body(message: dict[str, Any]) -> str:
	body_values = message.get("bodyValues") or {}
	parts = message.get("textBody") or []
	chunks: list[str] = []
	for part in parts:
		part_id = str((part or {}).get("partId") or "")
		value = body_values.get(part_id) or {}
		text = str(value.get("value") or "").strip()
		if text:
			chunks.append(text)
	return "\n\n".join(chunks)


class JMAPProvider(MailArchiveProvider):
	def __init__(self, config: JMAPConfig) -> None:
		self.config = config
		self.http = requests.Session()
		self.http.auth = (config.username, config.password)
		self.http.verify = config.verify_ssl
		self._session: dict[str, Any] | None = None
		self._account_id = config.account_id

	@classmethod
	def from_account(cls, account: Any) -> "JMAPProvider":
		return cls(
			JMAPConfig(
				server_url=str(account.server_url or "").strip(),
				username=str(account.username or "").strip(),
				password=account.get_password("password", raise_exception=True),
				account_id=str(account.provider_account_id or "").strip(),
				verify_ssl=bool(account.verify_ssl),
				timeout=max(int(account.request_timeout or DEFAULT_TIMEOUT), 5),
			)
		)

	@property
	def session(self) -> dict[str, Any]:
		if self._session is None:
			url = urljoin(self.config.server_url.rstrip("/") + "/", "/.well-known/jmap")
			try:
				response = self.http.get(
					url,
					headers={"Accept": "application/json"},
					timeout=self.config.timeout,
				)
			except (requests.ConnectionError, requests.Timeout) as exc:
				raise JMAPError("Der JMAP-Server ist nicht erreichbar.") from exc
			self._raise_http_error(response)
			try:
				self._session = response.json()
			except ValueError as exc:
				raise JMAPError("Die JMAP-Session-Antwort ist kein gültiges JSON.") from exc
			capabilities = self._session.get("capabilities") or {}
			if CORE_CAPABILITY not in capabilities or MAIL_CAPABILITY not in capabilities:
				raise JMAPError("Der Server bietet nicht die benötigten JMAP-Mail-Funktionen an.")
		return self._session

	@property
	def account_id(self) -> str:
		if self._account_id:
			if self._account_id not in (self.session.get("accounts") or {}):
				raise JMAPError("Die konfigurierte JMAP Account-ID ist für diesen Zugang nicht verfügbar.")
			return self._account_id
		primary = (self.session.get("primaryAccounts") or {}).get(MAIL_CAPABILITY)
		if not primary:
			raise JMAPError("Der JMAP-Zugang hat kein primäres Mail-Konto.")
		self._account_id = str(primary)
		return self._account_id

	def _raise_http_error(self, response: requests.Response) -> None:
		if response.ok:
			return
		if response.status_code in {401, 403}:
			raise JMAPError("JMAP-Anmeldung fehlgeschlagen.")
		if response.status_code in {502, 503, 504}:
			raise JMAPError("Der JMAP-Server ist vorübergehend nicht verfügbar.")
		raise JMAPError(f"JMAP-Aufruf fehlgeschlagen (HTTP {response.status_code}).")

	def _call(self, method_calls: list[list[Any]]) -> dict[str, Any]:
		payload = {"using": [CORE_CAPABILITY, MAIL_CAPABILITY], "methodCalls": method_calls}
		try:
			response = self.http.post(self.session["apiUrl"], json=payload, timeout=self.config.timeout)
		except (requests.ConnectionError, requests.Timeout) as exc:
			raise JMAPError("Der JMAP-Server ist nicht erreichbar.") from exc
		self._raise_http_error(response)
		try:
			data = response.json()
		except ValueError as exc:
			raise JMAPError("Die JMAP-Antwort ist kein gültiges JSON.") from exc
		for response_name, result, _call_id in data.get("methodResponses") or []:
			if response_name == "error":
				description = result.get("description") or result.get("type") or "Unbekannter Fehler"
				if result.get("type") == "cannotCalculateChanges":
					raise ChangeStateUnavailable(description)
				raise JMAPError(f"JMAP-Methodenfehler: {description}")
		return data

	def _single(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
		data = self._call([[method, arguments, "0"]])
		responses = data.get("methodResponses") or []
		if not responses:
			raise JMAPError(f"JMAP lieferte keine Antwort für {method}.")
		return responses[0][1]

	def test_connection(self) -> dict[str, Any]:
		account = (self.session.get("accounts") or {}).get(self.account_id) or {}
		mailboxes = self.list_mailboxes()
		return {
			"provider": "JMAP",
			"account_id": self.account_id,
			"account_name": account.get("name") or self.account_id,
			"mailboxes": len(mailboxes),
		}

	def list_mailboxes(self) -> list[ArchiveMailbox]:
		# Mailbox/get without explicit IDs is capped by Stalwart's getMaxResults (500 by
		# default). Large imported archives can exceed that limit, so query every stable ID
		# first and fetch the objects in server-advertised batches.
		mailbox_ids: list[str] = []
		position = 0
		while True:
			query = self._single(
				"Mailbox/query",
				{
					"accountId": self.account_id,
					"position": position,
					"limit": MAILBOX_QUERY_PAGE_SIZE,
					"calculateTotal": True,
				},
			)
			page = [str(mailbox_id) for mailbox_id in query.get("ids") or []]
			if not page:
				break
			mailbox_ids.extend(page)
			position += len(page)
			total = query.get("total")
			if total is not None and position >= int(total):
				break

		core = (self.session.get("capabilities") or {}).get(CORE_CAPABILITY) or {}
		get_batch_size = max(int(core.get("maxObjectsInGet") or MAILBOX_QUERY_PAGE_SIZE), 1)
		rows: list[dict[str, Any]] = []
		properties = [
			"id",
			"name",
			"parentId",
			"role",
			"sortOrder",
			"totalEmails",
			"unreadEmails",
		]
		for offset in range(0, len(mailbox_ids), get_batch_size):
			result = self._single(
				"Mailbox/get",
				{
					"accountId": self.account_id,
					"ids": mailbox_ids[offset : offset + get_batch_size],
					"properties": properties,
				},
			)
			rows.extend(result.get("list") or [])
		return [
			ArchiveMailbox(
				id=str(row["id"]),
				name=str(row.get("name") or row["id"]),
				parent_id=row.get("parentId"),
				role=row.get("role"),
				sort_order=int(row.get("sortOrder") or 0),
				total_emails=int(row.get("totalEmails") or 0),
				unread_emails=int(row.get("unreadEmails") or 0),
			)
			for row in rows
		]

	def query_message_ids(
		self, *, mailbox_id: str | None = None, position: int = 0, limit: int = 500
	) -> tuple[list[str], int | None, str | None]:
		arguments: dict[str, Any] = {
			"accountId": self.account_id,
			"position": max(position, 0),
			"limit": max(min(limit, 1000), 1),
			"sort": [{"property": "receivedAt", "isAscending": False}],
			"calculateTotal": True,
		}
		if mailbox_id:
			arguments["filter"] = {"inMailbox": mailbox_id}
		result = self._single("Email/query", arguments)
		return list(result.get("ids") or []), result.get("total"), result.get("queryState")

	def get_messages(self, ids: list[str]) -> tuple[list[ArchiveMessage], str | None]:
		if not ids:
			return [], None
		messages: list[ArchiveMessage] = []
		state: str | None = None
		for offset in range(0, len(ids), 200):
			result = self._single(
				"Email/get",
				{
					"accountId": self.account_id,
					"ids": ids[offset : offset + 200],
					"properties": [
						"id",
						"threadId",
						"mailboxIds",
						"keywords",
						"receivedAt",
						"hasAttachment",
						"subject",
						"preview",
						"from",
						"to",
						"cc",
						"messageId",
						"inReplyTo",
						"references",
						"textBody",
						"bodyValues",
					],
					"fetchAllBodyValues": True,
					"maxBodyValueBytes": MAX_BODY_VALUE_BYTES,
				},
			)
			state = result.get("state") or state
			messages.extend(self._to_message(row) for row in result.get("list") or [])
		return messages, state

	def get_current_state(self) -> str:
		result = self._single(
			"Email/get",
			{"accountId": self.account_id, "ids": [], "properties": ["id"]},
		)
		return str(result.get("state") or "")

	def _to_message(self, row: dict[str, Any]) -> ArchiveMessage:
		return ArchiveMessage(
			id=str(row["id"]),
			thread_id=str(row.get("threadId") or ""),
			mailbox_ids=tuple((row.get("mailboxIds") or {}).keys()),
			keywords=tuple(
				str(keyword).casefold()
				for keyword, enabled in (row.get("keywords") or {}).items()
				if enabled
			),
			rfc_message_ids=_string_tuple(row.get("messageId")),
			in_reply_to=_string_tuple(row.get("inReplyTo")),
			references=_string_tuple(row.get("references")),
			subject=str(row.get("subject") or ""),
			sender=_addresses(row.get("from")),
			to=_addresses(row.get("to")),
			cc=_addresses(row.get("cc")),
			received_at=row.get("receivedAt"),
			preview=str(row.get("preview") or ""),
			text_body=_extract_text_body(row),
			has_attachment=bool(row.get("hasAttachment")),
			raw=row,
		)

	def find_message_by_rfc_id(self, rfc_message_id: str) -> ArchiveMessage | None:
		value = str(rfc_message_id or "").strip()
		if not value:
			return None
		result = self._single(
			"Email/query",
			{
				"accountId": self.account_id,
				"filter": {"header": ["Message-ID", value]},
				"sort": [{"property": "receivedAt", "isAscending": False}],
				"limit": 2,
			},
		)
		ids = result.get("ids") or []
		if len(ids) > 1:
			raise JMAPError("Die RFC Message-ID ist auf dem Mailserver nicht eindeutig.")
		messages, _state = self.get_messages(ids)
		return messages[0] if messages else None

	def get_changes(self, since_state: str, *, max_changes: int = 500) -> ChangeSet:
		result = self._single(
			"Email/changes",
			{
				"accountId": self.account_id,
				"sinceState": since_state,
				"maxChanges": max(min(max_changes, 5000), 1),
			},
		)
		return ChangeSet(
			created=tuple(result.get("created") or []),
			updated=tuple(result.get("updated") or []),
			destroyed=tuple(result.get("destroyed") or []),
			new_state=str(result.get("newState") or since_state),
			has_more_changes=bool(result.get("hasMoreChanges")),
		)

	def move_message(self, message_id: str, target_mailbox_id: str) -> None:
		result = self._single(
			"Email/set",
			{
				"accountId": self.account_id,
				"update": {message_id: {"mailboxIds": {target_mailbox_id: True}}},
			},
		)
		if result.get("notUpdated"):
			error = (result["notUpdated"].get(message_id) or {}).get("description") or "Unbekannter Fehler"
			raise JMAPError(f"Die Nachricht konnte nicht verschoben werden: {error}")

	def patch_keywords(self, updates: dict[str, dict[str, bool | None]]) -> None:
		if not updates:
			return
		core = (self.session.get("capabilities") or {}).get(CORE_CAPABILITY) or {}
		batch_size = max(int(core.get("maxObjectsInSet") or 500), 1)
		items = list(updates.items())
		for offset in range(0, len(items), batch_size):
			batch = {
				message_id: {
					f"keywords/{keyword}": enabled for keyword, enabled in keyword_updates.items()
				}
				for message_id, keyword_updates in items[offset : offset + batch_size]
			}
			result = self._single(
				"Email/set",
				{
					"accountId": self.account_id,
					"update": batch,
				},
			)
			if result.get("notUpdated"):
				message_id, failure = next(iter(result["notUpdated"].items()))
				description = (failure or {}).get("description") or (failure or {}).get("type")
				raise JMAPError(
					f"Schlagwörter für Nachricht {message_id} konnten nicht gespeichert werden: "
					f"{description or 'Unbekannter Fehler'}"
				)
