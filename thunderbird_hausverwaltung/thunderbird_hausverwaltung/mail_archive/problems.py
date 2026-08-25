from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any

import frappe

from hausverwaltung.hausverwaltung.doctype.hausverwaltung_problem.hausverwaltung_problem import (
	sync_detected_problems,
)

from ..setup import backfill_known_property_folders, classify_existing_structure_folders
from .tagging import ambiguous_contract_addresses, build_tag_context

PROBLEM_SOURCE = "Mail-Archiv"
OLD_TENANT_FOLDER_RE = re.compile(r"^00[- ]*alte mieter$", re.IGNORECASE)


def _is_structure_folder(folder: Any) -> bool:
	return str(folder.folder_type or "") == "Strukturordner"


def _is_old_tenant_container(folder: Any) -> bool:
	return bool(OLD_TENANT_FOLDER_RE.match(str(folder.folder_name or "").strip()))


def _tenant_folder_candidates(
	tenant_root: Any, folders_by_parent: dict[str, list[Any]]
) -> list[tuple[Any, bool]]:
	"""Return current and historical tenant roots, excluding explicit structure folders."""
	result: list[tuple[Any, bool]] = []
	for child in folders_by_parent.get(str(tenant_root.provider_mailbox_id), []):
		if _is_old_tenant_container(child):
			for historical in folders_by_parent.get(str(child.provider_mailbox_id), []):
				if not _is_structure_folder(historical):
					result.append((historical, True))
			continue
		if _is_structure_folder(child):
			continue
		result.append((child, False))
	return result


def _finding(
	*,
	key: str,
	title: str,
	problem_type: str,
	description: str,
	severity: str = "Warnung",
	reference_doctype: str = "",
	reference_name: str = "",
	secondary_doctype: str = "",
	secondary_name: str = "",
	details: dict[str, Any] | None = None,
) -> dict[str, Any]:
	return {
		"key": key,
		"title": title,
		"problem_type": problem_type,
		"description": description,
		"severity": severity,
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"secondary_doctype": secondary_doctype,
		"secondary_name": secondary_name,
		"details": details or {},
	}


def _missing_property_folder_finding(immobilie: str, *, tenant_root: bool) -> dict[str, Any]:
	label = "Mieterordner" if tenant_root else "Immobilienordner"
	problem_type = "Mieterordner fehlt" if tenant_root else "Immobilienordner fehlt"
	return _finding(
		key=f"immobilie:{immobilie}:{'tenant' if tenant_root else 'property'}-folder-missing",
		title=f"{label} für {immobilie} fehlt",
		problem_type=problem_type,
		description=(
			f"Für die Immobilie {immobilie} ist kein {label} aus Stalwart hinterlegt. "
			"Ordnen Sie im Tab E-Mail-Archiv den passenden Ordner zu."
		),
		severity="Kritisch" if tenant_root else "Warnung",
		reference_doctype="Immobilie",
		reference_name=immobilie,
	)


def check_archive_problems() -> dict[str, Any]:
	"""Check property roots and tenant-folder contract mappings against current ERP data."""
	if not frappe.db.table_exists("Hausverwaltung Problem"):
		return {"status": "problem-doctype-missing", "detected": 0}
	if not frappe.db.has_column("Immobilie", "custom_immobilien_archivordner"):
		return {"status": "custom-fields-missing", "detected": 0}

	backfill_known_property_folders()
	classify_existing_structure_folders()

	folders = frappe.get_all(
		"Mail Archive Folder",
		fields=[
			"name",
			"archive_account",
			"provider_mailbox_id",
			"parent_mailbox_id",
			"folder_name",
			"folder_path",
			"provider_exists",
			"folder_type",
			"reference_doctype",
			"reference_name",
		],
		limit_page_length=0,
	)
	folders_by_name = {str(folder.name): folder for folder in folders}
	folders_by_parent: dict[str, list[Any]] = defaultdict(list)
	for folder in folders:
		folders_by_parent[str(folder.parent_mailbox_id or "")].append(folder)

	contracts = {
		str(row.name): row
		for row in frappe.get_all(
			"Mietvertrag",
			filters={"docstatus": ["<", 2]},
			fields=["name", "immobilie", "status", "von", "bis"],
			limit_page_length=0,
		)
	}
	all_properties = frappe.get_all(
		"Immobilie",
		fields=[
			"name",
			"parent_immobilie",
			"custom_immobilien_archivordner",
			"custom_mieter_archivordner",
		],
		limit_page_length=0,
	)
	property_roots = {str(row.name): str(row.parent_immobilie or row.name) for row in all_properties}
	properties = [row for row in all_properties if not row.parent_immobilie]

	findings: list[dict[str, Any]] = []
	if not frappe.db.exists("Mail Filing Source Account", {"enabled": 1}):
		findings.append(
			_finding(
				key="source-account:ionos-missing",
				title="IONOS-Postfach für automatische Mietvertragstags fehlt",
				problem_type="IONOS-Quellpostfach fehlt",
				description=(
					"Richten Sie ein aktives Mail Filing Source Account für das IONOS-Postfach ein. "
					"Ohne IMAP-Zugang kann ERPNext dort keine Mietvertragstags setzen."
				),
				severity="Kritisch",
			)
		)
	for address, contract_names in ambiguous_contract_addresses(build_tag_context()).items():
		address_key = hashlib.sha256(address.encode()).hexdigest()[:16]
		findings.append(
			_finding(
				key=f"tenant-email:{address_key}:ambiguous-contracts",
				title=f"Mieter-E-Mail-Adresse ist mehrdeutig: {address}",
				problem_type="Mieter-E-Mail-Adresse mehreren Mietverträgen zugeordnet",
				description=(
					"Die E-Mail-Adresse ist im selben Zeitraum mehreren Mietverträgen zugeordnet. "
					"ERPNext setzt deshalb ohne eindeutigen Mieterordner keinen Mietvertragstag."
				),
				reference_doctype="Mietvertrag",
				reference_name=contract_names[0],
				details={"email_address": address, "contracts": list(contract_names)},
			)
		)
	folder_usage: dict[str, list[tuple[str, str]]] = defaultdict(list)
	for immobilie in properties:
		for fieldname, role in (
			("custom_immobilien_archivordner", "Immobilienordner"),
			("custom_mieter_archivordner", "Mieterordner"),
		):
			folder_name = str(immobilie.get(fieldname) or "")
			if folder_name:
				folder_usage[folder_name].append((str(immobilie.name), role))

	for folder_name, usages in folder_usage.items():
		if len(usages) < 2:
			continue
		folder = folders_by_name.get(folder_name)
		findings.append(
			_finding(
				key=f"folder:{folder_name}:duplicate-property-use",
				title=f"Archivordner mehrfach verwendet: {folder.folder_name if folder else folder_name}",
				problem_type="Archivordner mehrfach zugeordnet",
				description="Derselbe Stalwart-Ordner ist mehrfach als Immobilien- oder Mieterordner hinterlegt.",
				severity="Kritisch",
				reference_doctype="Mail Archive Folder",
				reference_name=folder_name,
				details={"usages": usages},
			)
		)

	for immobilie in properties:
		immobilie_name = str(immobilie.name)
		property_folder_name = str(immobilie.custom_immobilien_archivordner or "")
		tenant_root_name = str(immobilie.custom_mieter_archivordner or "")
		if not property_folder_name:
			findings.append(_missing_property_folder_finding(immobilie_name, tenant_root=False))
		if not tenant_root_name:
			findings.append(_missing_property_folder_finding(immobilie_name, tenant_root=True))

		linked_folders: list[tuple[str, str, Any | None]] = [
			("Immobilienordner", property_folder_name, folders_by_name.get(property_folder_name)),
			("Mieterordner", tenant_root_name, folders_by_name.get(tenant_root_name)),
		]
		for role, linked_name, linked_folder in linked_folders:
			if not linked_name:
				continue
			if not linked_folder or not linked_folder.provider_exists:
				findings.append(
					_finding(
						key=f"immobilie:{immobilie_name}:{role}:folder-not-on-server",
						title=f"{role} für {immobilie_name} fehlt auf dem Mailserver",
						problem_type="Archivordner nicht auf Mailserver vorhanden",
						description=(
							f"Der in ERPNext hinterlegte {role} wurde bei der letzten Stalwart-Synchronisierung nicht gefunden."
						),
						severity="Kritisch",
						reference_doctype="Immobilie",
						reference_name=immobilie_name,
						secondary_doctype="Mail Archive Folder" if linked_folder else "",
						secondary_name=linked_name if linked_folder else "",
					)
				)

		property_folder = folders_by_name.get(property_folder_name)
		tenant_root = folders_by_name.get(tenant_root_name)
		if property_folder and tenant_root and property_folder.archive_account != tenant_root.archive_account:
			findings.append(
				_finding(
					key=f"immobilie:{immobilie_name}:archive-account-mismatch",
					title=f"Archivordner von {immobilie_name} liegen in verschiedenen Konten",
					problem_type="Unterschiedliche Archivkonten",
					description="Immobilienordner und Mieterordner müssen zum selben Mail-Archiv-Konto gehören.",
					severity="Kritisch",
					reference_doctype="Immobilie",
					reference_name=immobilie_name,
				)
			)

		if not tenant_root or not tenant_root.provider_exists:
			continue

		assigned_contracts: set[str] = set()
		current_contract_folders: dict[str, list[str]] = defaultdict(list)
		for folder, historical in _tenant_folder_candidates(tenant_root, folders_by_parent):
			reference_doctype = str(folder.reference_doctype or "")
			reference_name = str(folder.reference_name or "")
			if not reference_doctype or not reference_name:
				findings.append(
					_finding(
						key=f"folder:{folder.name}:tenant-contract-missing",
						title=f"Mieterordner ohne Mietvertrag: {folder.folder_name}",
						problem_type="Mieterordner ohne Mietvertrag",
						description=(
							f"Der Ordner {folder.folder_path} liegt im Mieterbereich von {immobilie_name}, "
							"ist aber keinem Mietvertrag zugeordnet."
						),
						reference_doctype="Mail Archive Folder",
						reference_name=folder.name,
						secondary_doctype="Immobilie",
						secondary_name=immobilie_name,
					)
				)
				continue
			if reference_doctype != "Mietvertrag" or reference_name not in contracts:
				findings.append(
					_finding(
						key=f"folder:{folder.name}:invalid-contract-reference",
						title=f"Ungültiger Mietvertragsbezug: {folder.folder_name}",
						problem_type="Ungültiger Mietvertragsbezug",
						description="Der Mieterordner verweist nicht auf einen vorhandenen Mietvertrag.",
						severity="Kritisch",
						reference_doctype="Mail Archive Folder",
						reference_name=folder.name,
						details={"reference_doctype": reference_doctype, "reference_name": reference_name},
					)
				)
				continue

			contract = contracts[reference_name]
			assigned_contracts.add(reference_name)
			if not historical:
				current_contract_folders[reference_name].append(str(folder.name))
			contract_property = str(contract.immobilie or "")
			if property_roots.get(contract_property, contract_property) != immobilie_name:
				findings.append(
					_finding(
						key=f"folder:{folder.name}:wrong-property",
						title=f"Mieterordner gehört zur falschen Immobilie: {folder.folder_name}",
						problem_type="Mietvertrag und Ordnerbereich widersprechen sich",
						description=(
							f"Der Ordner liegt unter {immobilie_name}, der zugeordnete Mietvertrag gehört aber zu "
							f"{contract.immobilie or 'keiner Immobilie'}."
						),
						severity="Kritisch",
						reference_doctype="Mail Archive Folder",
						reference_name=folder.name,
						secondary_doctype="Mietvertrag",
						secondary_name=reference_name,
					)
				)

		for contract_name, folder_names in current_contract_folders.items():
			if len(folder_names) < 2:
				continue
			findings.append(
				_finding(
					key=f"contract:{contract_name}:multiple-current-folders",
					title=f"Mehrere aktuelle Mieterordner für {contract_name}",
					problem_type="Mehrere aktuelle Ordner für Mietvertrag",
					description="Mehrere aktuelle Mieter-Stammordner verweisen auf denselben Mietvertrag.",
					reference_doctype="Mietvertrag",
					reference_name=contract_name,
					secondary_doctype="Immobilie",
					secondary_name=immobilie_name,
					details={"folders": folder_names},
				)
			)

		for contract in contracts.values():
			if (
				property_roots.get(str(contract.immobilie or ""), str(contract.immobilie or ""))
				== immobilie_name
				and contract.status == "Läuft"
				and contract.name not in assigned_contracts
			):
				findings.append(
					_finding(
						key=f"contract:{contract.name}:tenant-folder-missing",
						title=f"Laufender Mietvertrag ohne Mieterordner: {contract.name}",
						problem_type="Laufender Mietvertrag ohne Mieterordner",
						description=(
							"Für den laufenden Mietvertrag wurde im konfigurierten Mieterbereich kein zugeordneter "
							"Mieter-Stammordner gefunden."
						),
						reference_doctype="Mietvertrag",
						reference_name=contract.name,
						secondary_doctype="Immobilie",
						secondary_name=immobilie_name,
					)
				)

	summary = sync_detected_problems(PROBLEM_SOURCE, findings)
	summary["by_type"] = dict(Counter(item["problem_type"] for item in findings))
	summary["status"] = "ok"
	return summary


def enqueue_archive_problem_check(doc: Any | None = None, method: str | None = None) -> None:
	del doc, method
	if frappe.flags.in_install or frappe.flags.in_migrate:
		return
	frappe.enqueue(
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.check_archive_problems",
		queue="short",
		job_id=f"mail-archive-problems::{frappe.local.site}",
		deduplicate=True,
		enqueue_after_commit=True,
	)
