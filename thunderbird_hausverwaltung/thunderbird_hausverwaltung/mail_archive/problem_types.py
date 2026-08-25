from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from hausverwaltung.hausverwaltung.problem_types import parse_problem_details

from ..doctype.mail_archive_account.mail_archive_account import normalize_account_addresses
from .tagging import _normalize_email


TYPE_CODE = "mail.unassigned_tenant_address"


class UnassignedTenantAddressProblemType:
	code = TYPE_CODE
	label = _("E-Mail-Adresse keinem Mieter zugeordnet")
	category = _("E-Mail-Archiv")
	icon = "mail"

	def get_definition(self) -> dict[str, str]:
		return {
			"code": self.code,
			"label": str(self.label),
			"category": str(self.category),
			"icon": self.icon,
		}

	def get_ui(self, problem: Any, details: dict[str, Any]) -> dict[str, Any]:
		address = _normalize_email(details.get("email_address"))
		contracts = sorted({str(item) for item in details.get("contracts") or [] if item})
		accounts = sorted({str(item) for item in details.get("accounts") or [] if item})
		folders = sorted({str(item) for item in details.get("folders") or [] if item})
		contact_names = sorted(
			{
				str(row.mieter)
				for row in (
					frappe.get_all(
						"Vertragspartner",
						filters={
							"parenttype": "Mietvertrag",
							"parentfield": "mieter",
							"parent": ["in", contracts],
						},
						fields=["mieter"],
						limit_page_length=0,
					)
					if contracts
					else []
				)
				if row.mieter
			}
		)
		examples = [
			{
				"subject": str(item.get("subject") or _("(ohne Betreff)")),
				"folder": str(item.get("folder") or ""),
				"message": str(item.get("message") or ""),
			}
			for item in details.get("examples") or []
			if isinstance(item, dict)
		]

		actions: list[dict[str, Any]] = []
		if (
			problem.status in {"Offen", "In Bearbeitung"}
			and address
			and contracts
			and contact_names
			and frappe.has_permission("Contact", "write")
		):
			actions.append(
				{
					"key": "assign_contact_email",
					"label": _("E-Mail eintragen"),
					"variant": "primary",
					"dialog_title": _("E-Mail-Adresse beim Vertragspartner eintragen"),
					"fields": [
						{
							"fieldname": "email_address",
							"fieldtype": "Data",
							"label": _("E-Mail-Adresse"),
							"default": address,
							"read_only": 1,
						},
						{
							"fieldname": "contract",
							"fieldtype": "Link",
							"label": _("Mietvertrag"),
							"options": "Mietvertrag",
							"default": contracts[0],
							"reqd": 1,
							"filters": {"name": ["in", contracts]},
						},
						{
							"fieldname": "contact",
							"fieldtype": "Link",
							"label": _("Vertragspartner"),
							"options": "Contact",
							"default": contact_names[0] if len(contact_names) == 1 else "",
							"reqd": 1,
							"filters": {"name": ["in", contact_names]},
						},
					],
				}
			)
		if (
			problem.status in {"Offen", "In Bearbeitung"}
			and address
			and accounts
			and frappe.has_permission("Mail Archive Account", "write")
		):
			actions.append(
				{
					"key": "add_own_address",
					"label": _("Eigene Adresse"),
					"variant": "secondary",
					"dialog_title": _("Als eigene Verwaltungsadresse hinterlegen"),
					"fields": [
						{
							"fieldname": "email_address",
							"fieldtype": "Data",
							"label": _("E-Mail-Adresse"),
							"default": address,
							"read_only": 1,
						},
						{
							"fieldname": "archive_account",
							"fieldtype": "Link",
							"label": _("Mail-Archiv-Konto"),
							"options": "Mail Archive Account",
							"default": accounts[0],
							"reqd": 1,
							"filters": {"name": ["in", accounts]},
						},
					],
				}
			)

		return {
			"sections": [
				{
					"type": "metrics",
					"items": [
						{"label": _("E-Mail-Adresse"), "value": address},
						{
							"label": _("Nachrichten"),
							"value": int(details.get("message_count") or 0),
						},
						{"label": _("Mieterordner"), "value": len(folders)},
						{"label": _("Mietverträge"), "value": len(contracts)},
					],
				},
				{
					"type": "text",
					"title": _("Was ist zu tun?"),
					"value": (
						_("Tragen Sie die Adresse bei einem Vertragspartner ein, wenn sie zu einem Mieter gehört. ")
						+ _("Verwaltungsadressen können als eigene Adresse hinterlegt werden; andere Absender werden akzeptiert.")
					),
				},
				{"type": "list", "title": _("Gefunden in"), "items": folders},
				{
					"type": "table",
					"title": _("Beispielnachrichten"),
					"columns": [
						{"key": "subject", "label": _("Betreff")},
						{"key": "folder", "label": _("Ordner")},
					],
					"rows": examples,
				},
			],
			"actions": actions,
		}

	def execute_action(
		self, problem: Any, action: str, values: dict[str, Any]
	) -> dict[str, Any]:
		details = parse_problem_details(problem.details_json)
		address = _normalize_email(details.get("email_address"))
		if not address:
			frappe.throw(_("Im Problem ist keine gültige E-Mail-Adresse hinterlegt."))
		if action == "assign_contact_email":
			return self._assign_contact_email(details, address, values)
		if action == "add_own_address":
			return self._add_own_address(details, address, values)
		frappe.throw(_("Unbekannte Aktion für diesen Problemtyp."))

	@staticmethod
	def _assign_contact_email(
		details: dict[str, Any], address: str, values: dict[str, Any]
	) -> dict[str, Any]:
		contract = str(values.get("contract") or "")
		contact = str(values.get("contact") or "")
		allowed_contracts = {str(item) for item in details.get("contracts") or []}
		if contract not in allowed_contracts:
			frappe.throw(_("Der ausgewählte Mietvertrag gehört nicht zu diesem Problem."))
		if not frappe.db.exists(
			"Vertragspartner",
			{
				"parenttype": "Mietvertrag",
				"parentfield": "mieter",
				"parent": contract,
				"mieter": contact,
			},
		):
			frappe.throw(_("Der ausgewählte Kontakt ist kein Vertragspartner dieses Mietvertrags."))

		contact_doc = frappe.get_doc("Contact", contact)
		contact_doc.check_permission("write")
		if not any(_normalize_email(row.email_id) == address for row in contact_doc.email_ids or []):
			contact_doc.append("email_ids", {"email_id": address})
			contact_doc.save()
		return {
			"message": _("Die E-Mail-Adresse wurde beim Vertragspartner eingetragen."),
			"recheck": True,
		}

	@staticmethod
	def _add_own_address(
		details: dict[str, Any], address: str, values: dict[str, Any]
	) -> dict[str, Any]:
		account_name = str(values.get("archive_account") or "")
		if account_name not in {str(item) for item in details.get("accounts") or []}:
			frappe.throw(_("Das ausgewählte Archivkonto gehört nicht zu diesem Problem."))
		account = frappe.get_doc("Mail Archive Account", account_name)
		account.check_permission("write")
		addresses = normalize_account_addresses(account.email_addresses)
		if address not in addresses:
			addresses.append(address)
			account.email_addresses = "\n".join(addresses)
			account.save()
		return {
			"message": _("Die Adresse wurde als eigene Verwaltungsadresse hinterlegt."),
			"recheck": True,
		}


def get_problem_types() -> dict[str, str]:
	return {
		TYPE_CODE: (
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problem_types."
			"UnassignedTenantAddressProblemType"
		)
	}
