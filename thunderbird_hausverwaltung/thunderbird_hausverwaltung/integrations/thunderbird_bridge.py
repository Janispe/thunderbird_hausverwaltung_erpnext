from __future__ import annotations

import json
import re
import uuid
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, getdate, now_datetime, today, validate_email_address

ALLOWED_ROLES = {"Hausverwalter", "System Manager"}
COMMAND_TTL_MINUTES = 10
REDELIVERY_AFTER_SECONDS = 30
MAX_DELIVERY_ATTEMPTS = 5
MAX_EMAIL_ADDRESSES = 50
MAX_TAGS = 50
MAX_COMPOSE_BODY_LENGTH = 100_000
REALTIME_EVENT = "thunderbird_command_available"
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
MOZ_EXTENSION_ORIGIN_PATTERN = re.compile(
	r"^moz-extension://[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
	re.IGNORECASE,
)
BRIDGE_API_PATH_PREFIXES = (
	"/api/method/thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.",
	"/api/method/hausverwaltung.hausverwaltung.integrations.thunderbird_bridge.",
)


def _is_thunderbird_extension_request(origin: str, path: str) -> bool:
	return bool(
		MOZ_EXTENSION_ORIGIN_PATTERN.fullmatch(str(origin or ""))
		and any(str(path or "").startswith(prefix) for prefix in BRIDGE_API_PATH_PREFIXES)
	)


def allow_extension_cors() -> None:
	"""Allow only Thunderbird extension origins to call this app's bridge API."""
	request = getattr(frappe.local, "request", None)
	if not request:
		return
	origin = request.headers.get("Origin", "")
	if _is_thunderbird_extension_request(origin, request.path):
		frappe.local.allow_cors = origin


def _require_bridge_user() -> str:
	user = (frappe.session.user or "").strip()
	if not user or user == "Guest":
		frappe.throw(_("Anmeldung erforderlich."), frappe.PermissionError)
	if not ALLOWED_ROLES.intersection(set(frappe.get_roles(user))):
		frappe.throw(
			_('Für die Thunderbird-Brücke wird die Rolle "Hausverwalter" oder "System Manager" benötigt.'),
			frappe.PermissionError,
		)
	return user


def _normalize_device_id(value: Any) -> str:
	device_id = str(value or "").strip()
	if not DEVICE_ID_PATTERN.fullmatch(device_id):
		frappe.throw(_("Ungültige Thunderbird Device-ID."))
	return device_id


def _as_list(value: Any) -> list[str]:
	if value is None or value == "":
		return []
	if isinstance(value, str):
		try:
			decoded = json.loads(value)
		except ValueError:
			decoded = [item.strip() for item in value.split(",")]
		value = decoded
	if not isinstance(value, (list, tuple)):
		frappe.throw(_("Suchkriterien müssen als Liste übergeben werden."))
	result: list[str] = []
	seen: set[str] = set()
	for item in value:
		normalized = str(item or "").strip()
		key = normalized.casefold()
		if normalized and key not in seen:
			seen.add(key)
			result.append(normalized)
	return result


def _normalize_search_payload(
	email_addresses: Any = None,
	tags: Any = None,
	mode: str = "any",
	title: str | None = None,
) -> dict[str, Any]:
	emails = [email.casefold() for email in _as_list(email_addresses)]
	if len(emails) > MAX_EMAIL_ADDRESSES:
		frappe.throw(
			_("Es können höchstens {0} E-Mail-Adressen durchsucht werden.").format(MAX_EMAIL_ADDRESSES)
		)
	for email in emails:
		validate_email_address(email, throw=True)
	normalized_tags = _as_list(tags)
	if len(normalized_tags) > MAX_TAGS:
		frappe.throw(_("Es können höchstens {0} Tags durchsucht werden.").format(MAX_TAGS))
	if any(len(tag) > 140 for tag in normalized_tags):
		frappe.throw(_("Ein Thunderbird-Tag darf höchstens 140 Zeichen lang sein."))
	if not emails and not normalized_tags:
		frappe.throw(_("Mindestens eine E-Mail-Adresse oder ein Thunderbird-Tag ist erforderlich."))
	normalized_mode = "all" if str(mode).strip().lower() == "all" else "any"
	return {
		"command": "show_messages",
		"title": (str(title or "ERPNext E-Mail-Suche").strip() or "ERPNext E-Mail-Suche")[:180],
		"match": {
			"email_addresses": emails,
			"tags": normalized_tags,
			"mode": normalized_mode,
		},
	}


def _normalize_recipients(value: Any) -> list[str]:
	recipients = _as_list(value)
	if len(recipients) > MAX_EMAIL_ADDRESSES:
		frappe.throw(
			_("Es können höchstens {0} Empfänger je Feld verwendet werden.").format(MAX_EMAIL_ADDRESSES)
		)
	for recipient in recipients:
		validate_email_address(recipient, throw=True)
	return recipients


def _normalize_compose_payload(
	to: Any = None,
	cc: Any = None,
	bcc: Any = None,
	subject: str | None = None,
	plain_text_body: str | None = None,
) -> dict[str, Any]:
	to_recipients = _normalize_recipients(to)
	cc_recipients = _normalize_recipients(cc)
	bcc_recipients = _normalize_recipients(bcc)
	if not to_recipients and not cc_recipients and not bcc_recipients:
		frappe.throw(_("Mindestens ein Empfänger ist erforderlich."))
	body = str(plain_text_body or "")
	if len(body) > MAX_COMPOSE_BODY_LENGTH:
		frappe.throw(
			_("Der Nachrichtentext darf höchstens {0} Zeichen lang sein.").format(MAX_COMPOSE_BODY_LENGTH)
		)
	return {
		"command": "compose_message",
		"compose": {
			"to": to_recipients,
			"cc": cc_recipients,
			"bcc": bcc_recipients,
			"subject": str(subject or "")[:998],
			"plain_text_body": body,
		},
	}


def _row_value(row: Any, fieldname: str) -> Any:
	if isinstance(row, dict):
		return row.get(fieldname)
	return getattr(row, fieldname, None)


def _is_active_contract_partner(row: Any, reference_date: Any = None) -> bool:
	if str(_row_value(row, "rolle") or "").strip() == "Ausgezogen":
		return False
	moved_out = _row_value(row, "ausgezogen")
	if not moved_out:
		return True
	return getdate(moved_out) > getdate(reference_date or today())


def _preferred_contact_email(contact: Any) -> str:
	rows = list(_row_value(contact, "email_ids") or [])
	rows.sort(
		key=lambda row: (
			0 if cint(_row_value(row, "is_primary")) else 1,
			cint(_row_value(row, "idx")) or 999_999,
		)
	)
	for row in rows:
		email = str(_row_value(row, "email_id") or "").strip()
		if email:
			return email
	return str(_row_value(contact, "email_id") or "").strip()


def _all_contact_emails(contact: Any) -> list[str]:
	rows = list(_row_value(contact, "email_ids") or [])
	rows.sort(key=lambda row: cint(_row_value(row, "idx")) or 999_999)
	candidates = [str(_row_value(row, "email_id") or "").strip() for row in rows]
	candidates.append(str(_row_value(contact, "email_id") or "").strip())

	emails: list[str] = []
	seen: set[str] = set()
	for email in candidates:
		key = email.casefold()
		if email and key not in seen:
			emails.append(email)
			seen.add(key)
	return emails


def _contract_contact_names(contracts: list[Any]) -> list[str]:
	contact_names: list[str] = []
	seen: set[str] = set()
	for contract in contracts:
		for partner in _row_value(contract, "mieter") or []:
			contact_name = str(_row_value(partner, "mieter") or "").strip()
			if contact_name and contact_name not in seen:
				contact_names.append(contact_name)
				seen.add(contact_name)
	return contact_names


def _email_addresses_for_contracts(contracts: list[Any]) -> list[str]:
	email_addresses: list[str] = []
	seen: set[str] = set()
	for contact_name in _contract_contact_names(contracts):
		if not frappe.db.exists("Contact", contact_name):
			continue
		for email in _all_contact_emails(frappe.get_doc("Contact", contact_name)):
			key = email.casefold()
			if key in seen:
				continue
			validate_email_address(email, throw=True)
			email_addresses.append(email)
			seen.add(key)
	return email_addresses


def _device_registration_updates(device: Any, device_name: str, extension_version: str) -> dict[str, str]:
	updates = {}
	if str(_row_value(device, "device_name") or "") != device_name:
		updates["device_name"] = device_name
	if str(_row_value(device, "extension_version") or "") != extension_version:
		updates["extension_version"] = extension_version
	return updates


@frappe.whitelist()
def get_mietvertrag_compose_context(mietvertrag: str) -> dict[str, Any]:
	_require_bridge_user()
	mietvertrag = str(mietvertrag or "").strip()
	if not mietvertrag or not frappe.db.exists("Mietvertrag", mietvertrag):
		frappe.throw(_("Mietvertrag nicht gefunden."), frappe.DoesNotExistError)

	contract = frappe.get_doc("Mietvertrag", mietvertrag)
	contract.check_permission("read")

	recipients: list[str] = []
	seen: set[str] = set()
	for partner in contract.get("mieter") or []:
		if not _is_active_contract_partner(partner):
			continue
		contact_name = str(_row_value(partner, "mieter") or "").strip()
		if not contact_name or not frappe.db.exists("Contact", contact_name):
			continue
		email = _preferred_contact_email(frappe.get_doc("Contact", contact_name))
		key = email.casefold()
		if email and key not in seen:
			validate_email_address(email, throw=True)
			recipients.append(email)
			seen.add(key)

	if not recipients:
		frappe.throw(
			_("Für die aktiven Vertragspartner dieses Mietvertrags ist keine E-Mail-Adresse hinterlegt.")
		)

	return {
		"mietvertrag": contract.name,
		"to": recipients,
		"subject": _("Mietvertrag {0}").format(contract.name),
	}


@frappe.whitelist()
def get_mietvertrag_search_context(mietvertrag: str) -> dict[str, Any]:
	_require_bridge_user()
	mietvertrag = str(mietvertrag or "").strip()
	if not mietvertrag or not frappe.db.exists("Mietvertrag", mietvertrag):
		frappe.throw(_("Mietvertrag nicht gefunden."), frappe.DoesNotExistError)

	contract = frappe.get_doc("Mietvertrag", mietvertrag)
	contract.check_permission("read")

	email_addresses = _email_addresses_for_contracts([contract])

	if not email_addresses:
		frappe.throw(_("Für die Vertragspartner dieses Mietvertrags ist keine E-Mail-Adresse hinterlegt."))

	return {
		"mietvertrag": contract.name,
		"email_addresses": email_addresses,
		"title": _("E-Mails zu Mietvertrag {0}").format(contract.name),
	}


@frappe.whitelist()
def get_wohnung_search_context(wohnung: str) -> dict[str, Any]:
	_require_bridge_user()
	wohnung = str(wohnung or "").strip()
	if not wohnung or not frappe.db.exists("Wohnung", wohnung):
		frappe.throw(_("Wohnung nicht gefunden."), frappe.DoesNotExistError)

	unit = frappe.get_doc("Wohnung", wohnung)
	unit.check_permission("read")

	contract_names = frappe.get_list(
		"Mietvertrag",
		filters={"wohnung": unit.name},
		pluck="name",
		order_by="creation asc",
	)
	contracts = []
	for contract_name in contract_names:
		contract = frappe.get_doc("Mietvertrag", contract_name)
		contract.check_permission("read")
		contracts.append(contract)

	email_addresses = _email_addresses_for_contracts(contracts)
	if not email_addresses:
		frappe.throw(
			_("Für die Mietverträge dieser Wohnung ist keine Vertragspartner-E-Mail-Adresse hinterlegt.")
		)

	return {
		"wohnung": unit.name,
		"mietvertraege": contract_names,
		"email_addresses": email_addresses,
		"title": _("E-Mails zu Wohnung {0}").format(unit.name),
	}


def _get_owned_device(device_id: str, user: str, *, require_enabled: bool = True):
	if not frappe.db.exists("Thunderbird Device", device_id):
		frappe.throw(_("Der Thunderbird-Arbeitsplatz ist nicht registriert."), frappe.DoesNotExistError)
	device = frappe.get_doc("Thunderbird Device", device_id)
	if device.user != user:
		frappe.throw(
			_("Der Thunderbird-Arbeitsplatz gehört zu einem anderen Benutzer."), frappe.PermissionError
		)
	if require_enabled and not device.enabled:
		frappe.throw(_("Der Thunderbird-Arbeitsplatz wurde deaktiviert."), frappe.PermissionError)
	return device


@frappe.whitelist()
def register_device(
	device_id: str, device_name: str = "Thunderbird", extension_version: str = ""
) -> dict[str, Any]:
	user = _require_bridge_user()
	device_id = _normalize_device_id(device_id)
	device_name = (str(device_name or "Thunderbird").strip() or "Thunderbird")[:140]
	extension_version = str(extension_version or "").strip()[:32]

	if frappe.db.exists("Thunderbird Device", device_id):
		device = _get_owned_device(device_id, user, require_enabled=False)
		if not device.enabled:
			frappe.throw(
				_("Dieser Thunderbird-Arbeitsplatz wurde in ERPNext deaktiviert."), frappe.PermissionError
			)
		# Registration and the initial catch-up sync can overlap while the options page is open.
		# Avoid a competing write; poll_command updates last_seen during that sync.
		if updates := _device_registration_updates(device, device_name, extension_version):
			device.db_set(updates, update_modified=True)
	else:
		device = frappe.get_doc(
			{
				"doctype": "Thunderbird Device",
				"device_id": device_id,
				"device_name": device_name,
				"user": user,
				"enabled": 1,
				"last_seen": now_datetime(),
				"extension_version": extension_version,
			}
		).insert(ignore_permissions=True)

	return {
		"device_id": device.name,
		"device_name": device.device_name,
		"user": user,
		"enabled": bool(device.enabled),
		"site": frappe.local.site,
		"realtime_event": REALTIME_EVENT,
	}


@frappe.whitelist()
def list_devices() -> list[dict[str, Any]]:
	user = _require_bridge_user()
	return frappe.get_all(
		"Thunderbird Device",
		filters={"user": user, "enabled": 1},
		fields=["device_id", "device_name", "last_seen", "extension_version"],
		order_by="last_seen desc",
	)


@frappe.whitelist()
def enqueue_search(
	email_addresses: Any = None,
	tags: Any = None,
	mode: str = "any",
	title: str | None = None,
	device_id: str | None = None,
) -> dict[str, Any]:
	user = _require_bridge_user()
	payload = _normalize_search_payload(email_addresses, tags, mode, title)
	normalized_device_id = ""
	if device_id:
		normalized_device_id = _normalize_device_id(device_id)
		_get_owned_device(normalized_device_id, user)

	return _enqueue_command(user, payload, normalized_device_id)


@frappe.whitelist()
def enqueue_compose(
	to: Any = None,
	cc: Any = None,
	bcc: Any = None,
	subject: str | None = None,
	plain_text_body: str | None = None,
	device_id: str | None = None,
) -> dict[str, Any]:
	user = _require_bridge_user()
	payload = _normalize_compose_payload(to, cc, bcc, subject, plain_text_body)
	normalized_device_id = ""
	if device_id:
		normalized_device_id = _normalize_device_id(device_id)
		_get_owned_device(normalized_device_id, user)
	return _enqueue_command(user, payload, normalized_device_id)


def _enqueue_command(user: str, payload: dict[str, Any], device_id: str = "") -> dict[str, Any]:
	command_id = str(uuid.uuid4())
	doc = frappe.get_doc(
		{
			"doctype": "Thunderbird Command",
			"command_id": command_id,
			"status": "Queued",
			"user": user,
			"device_id": device_id,
			"expires_on": add_to_date(now_datetime(), minutes=COMMAND_TTL_MINUTES),
			"payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
			"delivery_attempts": 0,
		}
	).insert(ignore_permissions=True)

	# The database row is the durable queue. Realtime only wakes connected clients so they can
	# claim the row immediately; reconnecting clients perform one catch-up request.
	frappe.publish_realtime(
		REALTIME_EVENT,
		message={"device_id": doc.device_id or None},
		user=user,
		after_commit=True,
	)

	return {
		"command_id": doc.name,
		"status": doc.status,
		"device_id": doc.device_id or None,
		"expires_on": doc.expires_on,
	}


def _find_next_command(user: str, device_id: str):
	now = now_datetime()
	common_filters = {
		"user": user,
		"expires_on": [">", now],
		"delivery_attempts": ["<", MAX_DELIVERY_ATTEMPTS],
	}
	or_filters = [["device_id", "=", device_id], ["device_id", "in", ["", None]]]

	rows = frappe.get_all(
		"Thunderbird Command",
		filters={**common_filters, "status": "Queued"},
		or_filters=or_filters,
		fields=["name"],
		order_by="creation asc",
		limit=1,
	)
	if not rows:
		redelivery_before = add_to_date(now, seconds=-REDELIVERY_AFTER_SECONDS)
		rows = frappe.get_all(
			"Thunderbird Command",
			filters={**common_filters, "status": "Delivered", "delivered_on": ["<", redelivery_before]},
			or_filters=or_filters,
			fields=["name"],
			order_by="delivered_on asc",
			limit=1,
		)
	return frappe.get_doc("Thunderbird Command", rows[0].name) if rows else None


@frappe.whitelist()
def poll_command(device_id: str) -> dict[str, Any] | None:
	user = _require_bridge_user()
	device_id = _normalize_device_id(device_id)
	device = _get_owned_device(device_id, user)
	device.db_set("last_seen", now_datetime(), update_modified=False)

	command = _find_next_command(user, device_id)
	if not command:
		return None

	# A broadcast command is claimed by the first device which receives it.
	command.db_set(
		{
			"device_id": device_id,
			"status": "Delivered",
			"delivered_on": now_datetime(),
			"delivery_attempts": cint(command.delivery_attempts) + 1,
		},
		update_modified=True,
	)
	payload = json.loads(command.payload)
	payload["id"] = command.name
	return payload


@frappe.whitelist()
def acknowledge_command(command_id: str, success: Any = True, result: Any = None) -> dict[str, Any]:
	user = _require_bridge_user()
	command_id = str(command_id or "").strip()
	if not command_id or not frappe.db.exists("Thunderbird Command", command_id):
		frappe.throw(_("Thunderbird-Befehl nicht gefunden."), frappe.DoesNotExistError)
	command = frappe.get_doc("Thunderbird Command", command_id)
	if command.user != user:
		frappe.throw(_("Der Thunderbird-Befehl gehört zu einem anderen Benutzer."), frappe.PermissionError)

	if isinstance(result, str):
		try:
			result = json.loads(result)
		except ValueError:
			result = {"message": result}
	result_json = json.dumps(result or {}, ensure_ascii=False, separators=(",", ":"))
	if len(result_json) > 100_000:
		frappe.throw(_("Das Thunderbird-Ergebnis ist zu groß."))

	completed = bool(cint(success)) if not isinstance(success, bool) else success
	command.db_set(
		{
			"status": "Completed" if completed else "Failed",
			"completed_on": now_datetime(),
			"result": result_json,
		},
		update_modified=True,
	)
	return {"command_id": command.name, "status": command.status}


def cleanup_commands() -> None:
	cutoff = add_to_date(now_datetime(), days=-30)
	frappe.db.delete("Thunderbird Command", {"modified": ["<", cutoff]})
