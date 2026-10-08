from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urljoin

import requests

from .base import (
	ArchiveMailbox,
	ArchiveMessage,
	ChangeSet,
	ChangeStateUnavailable,
	DraftNotCreatedError,
	MailArchiveProvider,
)

CORE_CAPABILITY = "urn:ietf:params:jmap:core"
MAIL_CAPABILITY = "urn:ietf:params:jmap:mail"
DEFAULT_TIMEOUT = 30
MAX_BODY_VALUE_BYTES = 50_000
MAILBOX_QUERY_PAGE_SIZE = 500
DRAFT_HEADER_NAME = "X-Hausverwaltung-Draft-ID"
DRAFT_HEADER_PROPERTY = f"header:{DRAFT_HEADER_NAME}:asText"


class JMAPError(RuntimeError):
	pass


class JMAPDraftNotCreatedError(DraftNotCreatedError, JMAPError):
	"""A JMAP creation failed before writing, or was explicitly notCreated."""


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


class _HTMLPlainText(HTMLParser):
	"""Extract visible text without rendering, executing markup or fetching resources."""

	_hidden_tags = {"head", "script", "style", "template", "noscript"}
	_block_tags = {
		"address",
		"article",
		"aside",
		"blockquote",
		"br",
		"dd",
		"div",
		"dl",
		"dt",
		"footer",
		"h1",
		"h2",
		"h3",
		"h4",
		"h5",
		"h6",
		"header",
		"hr",
		"li",
		"main",
		"ol",
		"p",
		"pre",
		"section",
		"table",
		"tr",
		"ul",
	}

	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.chunks: list[str] = []
		self.hidden: list[str] = []

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		if tag in self._hidden_tags:
			self.hidden.append(tag)
		if self.hidden:
			return
		if tag in self._block_tags:
			self.chunks.append("\n")
		elif tag in {"td", "th"}:
			self.chunks.append(" ")

	def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		self.handle_starttag(tag, attrs)
		self.handle_endtag(tag)

	def handle_endtag(self, tag: str) -> None:
		if self.hidden:
			if tag in self.hidden:
				index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
				del self.hidden[index:]
			return
		if tag in self._block_tags:
			self.chunks.append("\n")

	def handle_data(self, data: str) -> None:
		if not self.hidden:
			self.chunks.append(re.sub(r"\s+", " ", data))

	def text(self) -> str:
		return "\n".join(line.strip() for line in "".join(self.chunks).splitlines() if line.strip())


def _html_plain_text(value: str) -> str:
	parser = _HTMLPlainText()
	parser.feed(value)
	parser.close()
	return parser.text()


def _extract_text_body(message: dict[str, Any]) -> str:
	body_values = message.get("bodyValues") or {}
	parts = message.get("textBody") or []
	chunks: list[str] = []
	for part in parts:
		# RFC 8621 permits an HTML part in textBody when no plaintext alternative exists.
		# Preserve it in raw for an explicit caller decision instead of presenting markup as text.
		if str((part or {}).get("type") or "text/plain").casefold() != "text/plain":
			continue
		part_id = str((part or {}).get("partId") or "")
		value = body_values.get(part_id) or {}
		text = str(value.get("value") or "").strip()
		if text:
			chunks.append(text)
	if chunks:
		message["body_text_source"] = "plain"
		return "\n\n".join(chunks)
	# An HTML-only message may expose its part in textBody, htmlBody or both.
	seen: set[str] = set()
	for part in [*(message.get("htmlBody") or []), *parts]:
		if str((part or {}).get("type") or "").casefold() != "text/html":
			continue
		part_id = str((part or {}).get("partId") or "")
		if part_id in seen:
			continue
		seen.add(part_id)
		value = body_values.get(part_id) or {}
		text = _html_plain_text(str(value.get("value") or ""))
		if text:
			chunks.append(text)
	if chunks:
		message["body_text_source"] = "html"
	return "\n\n".join(chunks)


def _draft_header_value(value: str, label: str) -> str:
	value = str(value or "").strip()
	if not value or len(value) > 998 or any(ord(char) < 32 or ord(char) == 127 for char in value):
		raise JMAPError(f"{label} ist leer oder enthält unzulässige Zeichen.")
	return value


def _draft_message_id(value: str) -> str:
	value = _draft_header_value(value, "Die RFC Message-ID")
	if value.startswith("<") and value.endswith(">"):
		value = value[1:-1]
	# RFC 8621 MessageIds are parsed values: the server adds the angle brackets.
	if "@" not in value or any(char.isspace() or char in "<>" for char in value):
		raise JMAPError("Die RFC Message-ID ist ungültig.")
	return value


def _draft_address(value: str) -> dict[str, str]:
	value = _draft_header_value(value, "Die E-Mail-Adresse")
	if "@" not in value or any(char.isspace() or char in "<>,;" for char in value):
		raise JMAPError("Die E-Mail-Adresse ist ungültig.")
	return {"email": value}


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
		if (
			len(responses) != 1
			or not isinstance(responses[0], (list, tuple))
			or len(responses[0]) != 3
			or responses[0][0] != method
			or responses[0][2] != "0"
			or not isinstance(responses[0][1], dict)
		):
			raise JMAPError(f"JMAP lieferte keine eindeutig zugeordnete Antwort für {method}.")
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
			"myRights",
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
				my_rights=dict(row.get("myRights") or {}),
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
						"replyTo",
						"to",
						"cc",
						"messageId",
						"inReplyTo",
						"references",
						"sentAt",
						DRAFT_HEADER_PROPERTY,
						"textBody",
						"htmlBody",
						"bodyStructure",
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
				str(keyword).casefold() for keyword, enabled in (row.get("keywords") or {}).items() if enabled
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

	def _require_drafts_mailbox(self, mailbox_id: str) -> None:
		account = (self.session.get("accounts") or {}).get(self.account_id) or {}
		if account.get("isReadOnly"):
			raise JMAPDraftNotCreatedError("Das JMAP-Konto ist schreibgeschützt.")
		capabilities = account.get("accountCapabilities")
		if capabilities is not None and MAIL_CAPABILITY not in capabilities:
			raise JMAPDraftNotCreatedError("Das JMAP-Konto unterstützt keine Mail-Funktionen.")
		mailbox = next((item for item in self.list_mailboxes() if item.id == mailbox_id), None)
		if mailbox is None or mailbox.role != "drafts":
			raise JMAPDraftNotCreatedError("Das Ziel ist kein verfügbarer JMAP-Entwurfsordner.")
		if mailbox.my_rights and not all(
			mailbox.my_rights.get(right) is True
			for right in ("mayReadItems", "mayAddItems", "maySetKeywords")
		):
			raise JMAPDraftNotCreatedError(
				"Für den Entwurfsordner fehlen die erforderlichen Lese- oder Schreibrechte."
			)

	def create_draft(
		self,
		*,
		mailbox_id: str,
		sender: str,
		recipients: list[str],
		subject: str,
		text_body: str,
		draft_token: str,
		rfc_message_id: str,
		cc: list[str] | None = None,
		in_reply_to: tuple[str, ...] = (),
		references: tuple[str, ...] = (),
	) -> str:
		try:
			mailbox_id = _draft_header_value(mailbox_id, "Die Entwurfsordner-ID")
			draft_token = _draft_header_value(draft_token, "Die Entwurfskennung")
			if not recipients:
				raise JMAPError("Ein E-Mail-Entwurf benötigt mindestens einen Empfänger.")
			message: dict[str, Any] = {
				"mailboxIds": {mailbox_id: True},
				"keywords": {"$draft": True},
				"from": [_draft_address(sender)],
				"to": [_draft_address(value) for value in recipients],
				"subject": str(subject or ""),
				"messageId": [_draft_message_id(rfc_message_id)],
				DRAFT_HEADER_PROPERTY: draft_token,
				"textBody": [{"partId": "body", "type": "text/plain"}],
				"bodyValues": {"body": {"value": str(text_body or "")}},
			}
			if cc:
				message["cc"] = [_draft_address(value) for value in cc]
			if in_reply_to:
				message["inReplyTo"] = [_draft_message_id(value) for value in in_reply_to]
			if references:
				message["references"] = [_draft_message_id(value) for value in references]
		except JMAPError as exc:
			# These validators only produce locally generated messages and cannot write.
			raise JMAPDraftNotCreatedError(str(exc)) from exc
		except Exception as exc:
			raise JMAPDraftNotCreatedError("Die Eingaben für den E-Mail-Entwurf sind ungültig.") from exc
		try:
			self._require_drafts_mailbox(mailbox_id)
			account_id = self.account_id
		except DraftNotCreatedError:
			raise
		except Exception as exc:
			# A failed session/mailbox read occurs before the creation call. Never expose
			# remote descriptions, URLs or transport details through the retryable error.
			raise JMAPDraftNotCreatedError(
				"Das Postfach konnte vor der Entwurfserstellung nicht geprüft werden."
			) from exc
		# Email/set only stores mail; delivery requires the separate EmailSubmission API.
		result = self._single("Email/set", {"accountId": account_id, "create": {"draft": message}})
		if not isinstance(result, dict) or result.get("accountId", account_id) != account_id:
			raise JMAPError("JMAP lieferte keine prüfbare Antwort zur Entwurfserstellung.")
		created, not_created = result.get("created"), result.get("notCreated")
		if any(value is not None and not isinstance(value, dict) for value in (created, not_created)):
			raise JMAPError("JMAP lieferte keine prüfbare Antwort zur Entwurfserstellung.")
		created, not_created = created or {}, not_created or {}
		if not_created:
			failure = not_created.get("draft")
			if (
				created
				or set(not_created) != {"draft"}
				or not isinstance(failure, dict)
				or not isinstance(failure.get("type"), str)
				or not failure["type"].strip()
				or any(char.isspace() or ord(char) < 32 for char in failure["type"])
				or failure["type"].casefold() == "alreadyexists"
			):
				raise JMAPError("JMAP lieferte keine eindeutige Ablehnung der Entwurfserstellung.")
			reasons = {
				"overQuota": "Das Speicherlimit des Postfachs ist erreicht.",
				"forbidden": "Der Mailserver hat die Entwurfserstellung wegen fehlender Rechte abgelehnt.",
				"mailboxReadOnly": "Der Entwurfsordner ist auf dem Mailserver schreibgeschützt.",
				"invalidProperties": "Der Mailserver hat ungültige Entwurfsfelder abgelehnt.",
				"invalidEmail": "Der Mailserver hat das Nachrichtenformat des Entwurfs abgelehnt.",
				"tooLarge": "Der E-Mail-Entwurf überschreitet das Größenlimit des Mailservers.",
			}
			raise JMAPDraftNotCreatedError(
				reasons.get(failure["type"], "Der Mailserver hat die Entwurfserstellung abgelehnt.")
			)
		item = created.get("draft")
		message_id = item.get("id") if isinstance(item, dict) and set(created) == {"draft"} else None
		if not isinstance(message_id, str) or not message_id:
			raise JMAPError("JMAP bestätigte keine Message-ID für den E-Mail-Entwurf.")
		return message_id

	def find_draft_messages(self, draft_token: str) -> list[ArchiveMessage]:
		draft_token = _draft_header_value(draft_token, "Die Entwurfskennung")
		result = self._single(
			"Email/query",
			{
				"accountId": self.account_id,
				"filter": {"header": [DRAFT_HEADER_NAME, draft_token]},
				"sort": [{"property": "receivedAt", "isAscending": False}],
				"limit": 3,
				"calculateTotal": True,
			},
		)
		ids = result.get("ids") or []
		if len(ids) > 2 or int(result.get("total") or 0) > 2:
			raise JMAPError(
				"Die Entwurfskennung ist auf dem Mailserver nicht eindeutig (mehr als zwei Treffer)."
			)
		messages, _state = self.get_messages(ids)
		if len(messages) != len(ids) or {message.id for message in messages} != set(ids):
			raise JMAPError("Die Entwurfssuche konnte nicht alle gefundenen Nachrichten prüfen.")
		if any(not isinstance(message.raw.get(DRAFT_HEADER_PROPERTY), str) for message in messages):
			raise JMAPError("Die Entwurfssuche lieferte keine prüfbare Entwurfskennung.")
		# Header filters use text matching, so verify the complete token before recovery.
		return [message for message in messages if message.raw.get(DRAFT_HEADER_PROPERTY) == draft_token]

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
				message_id: {f"keywords/{keyword}": enabled for keyword, enabled in keyword_updates.items()}
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
