from __future__ import annotations

import re

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

KNOWN_PROPERTY_FOLDERS = {
	"Gropiusstr.": ("Haus G", "Mieter G"),
	"Kirchhofstr.": ("Haus K", "Mieter K"),
	"Leinestr.": ("Haus L", "Mieter L"),
	"Wilhelmshavener": ("Haus W", "Mieter W"),
	"Warthestr. 65": ("Haus Warthe", ""),
}

STRUCTURE_FOLDER_PATTERNS = (
	re.compile(r"^00[- ]*alte mieter$", re.IGNORECASE),
	re.compile(r"^hauswart$", re.IGNORECASE),
	re.compile(r"^rechtsberatung(?:[. ]+schuldner)?$", re.IGNORECASE),
)


def ensure_mail_archive_integration() -> None:
	create_custom_fields(
		{
			"Immobilie": [
				{
					"fieldname": "custom_mail_archive_tab",
					"fieldtype": "Tab Break",
					"label": "E-Mail-Archiv",
					"insert_after": "hausmeister",
				},
				{
					"fieldname": "custom_immobilien_archivordner",
					"fieldtype": "Link",
					"label": "Immobilienordner",
					"options": "Mail Archive Folder",
					"description": "Stabiler Stalwart-Ordner für allgemeine E-Mails und Unterlagen dieser Immobilie.",
					"in_standard_filter": 1,
					"insert_after": "custom_mail_archive_tab",
				},
				{
					"fieldname": "custom_mieter_archivordner",
					"fieldtype": "Link",
					"label": "Mieterordner",
					"options": "Mail Archive Folder",
					"description": "Stalwart-Wurzelordner, unter dem die Ordner der Mietverträge liegen.",
					"in_standard_filter": 1,
					"insert_after": "custom_immobilien_archivordner",
				},
			]
		},
		update=True,
	)
	backfill_known_property_folders()
	classify_existing_structure_folders()


def _unique_folder_by_name(folder_name: str) -> str:
	if not folder_name or not frappe.db.table_exists("Mail Archive Folder"):
		return ""
	rows = frappe.get_all(
		"Mail Archive Folder",
		filters={"folder_name": folder_name, "provider_exists": 1},
		pluck="name",
		limit_page_length=2,
	)
	return str(rows[0]) if len(rows) == 1 else ""


def backfill_known_property_folders() -> dict[str, int]:
	if not frappe.db.has_column("Immobilie", "custom_immobilien_archivordner"):
		return {"updated": 0}
	updated = 0
	for immobilie, (property_folder_name, tenant_folder_name) in KNOWN_PROPERTY_FOLDERS.items():
		if not frappe.db.exists("Immobilie", immobilie):
			continue
		values = frappe.db.get_value(
			"Immobilie",
			immobilie,
			["custom_immobilien_archivordner", "custom_mieter_archivordner"],
			as_dict=True,
		)
		changes: dict[str, str] = {}
		property_folder = _unique_folder_by_name(property_folder_name)
		tenant_folder = _unique_folder_by_name(tenant_folder_name)
		if not values.custom_immobilien_archivordner and property_folder:
			changes["custom_immobilien_archivordner"] = property_folder
		if not values.custom_mieter_archivordner and tenant_folder:
			changes["custom_mieter_archivordner"] = tenant_folder
		if not changes:
			continue
		frappe.db.set_value("Immobilie", immobilie, changes, update_modified=False)
		if changes.get("custom_immobilien_archivordner"):
			frappe.db.set_value(
				"Mail Archive Folder",
				changes["custom_immobilien_archivordner"],
				{
					"folder_type": "Immobilienordner",
					"reference_doctype": "Immobilie",
					"reference_name": immobilie,
				},
				update_modified=False,
			)
		if changes.get("custom_mieter_archivordner"):
			frappe.db.set_value(
				"Mail Archive Folder",
				changes["custom_mieter_archivordner"],
				{
					"folder_type": "Mieter-Wurzelordner",
					"reference_doctype": "Immobilie",
					"reference_name": immobilie,
				},
				update_modified=False,
			)
		updated += 1
	return {"updated": updated}


def classify_existing_structure_folders() -> dict[str, int]:
	if not frappe.db.has_column("Mail Archive Folder", "folder_type"):
		return {"updated": 0}
	updated = 0
	rows = frappe.get_all(
		"Mail Archive Folder",
		filters={"folder_type": ["in", ["", "Automatisch"]]},
		fields=["name", "folder_name", "reference_doctype"],
		limit_page_length=0,
	)
	for row in rows:
		folder_type = ""
		if any(pattern.match(str(row.folder_name or "").strip()) for pattern in STRUCTURE_FOLDER_PATTERNS):
			folder_type = "Strukturordner"
		elif row.reference_doctype == "Mietvertrag":
			folder_type = "Mieter-Stammordner"
		if not folder_type:
			continue
		frappe.db.set_value(
			"Mail Archive Folder", row.name, "folder_type", folder_type, update_modified=False
		)
		updated += 1
	return {"updated": updated}
