from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import Any

import frappe

from hausverwaltung.hausverwaltung.doctype.hausverwaltung_problem.hausverwaltung_problem import (
	sync_detected_problems,
)

from ..setup import backfill_known_property_folders, classify_existing_structure_folders
from .tagging import _normalize_email, ambiguous_contract_addresses, build_tag_context

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
	problem_code: str = "",
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
		"problem_code": problem_code,
		"description": description,
		"severity": severity,
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"secondary_doctype": secondary_doctype,
		"secondary_name": secondary_name,
		"details": details or {},
	}


def _message_participant_addresses(message: Any) -> set[str]:
	participants = message.get("participants") if hasattr(message, "get") else None
	if isinstance(participants, str):
		try:
			participants = json.loads(participants)
		except (TypeError, ValueError):
			participants = {}
	if not isinstance(participants, dict):
		participants = {}

	addresses: set[str] = set()
	for role in ("from", "to", "cc"):
		for participant in participants.get(role) or []:
			value = participant.get("email") if isinstance(participant, dict) else participant
			if address := _normalize_email(value):
				addresses.add(address)
	if not addresses:
		sender = message.get("sender_email") if hasattr(message, "get") else None
		if address := _normalize_email(sender):
			addresses.add(address)
	return addresses


def _unassigned_tenant_address_findings(
	messages: list[Any],
	*,
	contract_by_mailbox: dict[tuple[str, str], str],
	known_tenant_addresses: set[str],
	own_addresses: set[str],
) -> list[dict[str, Any]]:
	by_address: dict[str, dict[str, Any]] = {}
	for message in messages:
		account = str(message.get("archive_account") or "")
		mailbox_id = str(message.get("actual_mailbox_id") or "")
		contract = contract_by_mailbox.get((account, mailbox_id), "")
		if not contract:
			continue
		for address in sorted(_message_participant_addresses(message)):
			if address in known_tenant_addresses or address in own_addresses:
				continue
			entry = by_address.setdefault(
				address,
				{
					"count": 0,
					"accounts": set(),
					"contracts": set(),
					"folders": set(),
					"examples": [],
					"first_message": str(message.get("name") or ""),
				},
			)
			entry["count"] += 1
			entry["accounts"].add(account)
			entry["contracts"].add(contract)
			if folder_path := str(message.get("actual_folder_path") or ""):
				entry["folders"].add(folder_path)
			if len(entry["examples"]) < 10:
				entry["examples"].append(
					{
						"message": str(message.get("name") or ""),
						"subject": str(message.get("subject") or ""),
						"folder": str(message.get("actual_folder_path") or ""),
					}
				)

	findings: list[dict[str, Any]] = []
	for address, entry in sorted(by_address.items()):
		contracts = sorted(entry["contracts"])
		accounts = sorted(entry["accounts"])
		folders = sorted(entry["folders"])
		count = int(entry["count"])
		address_key = hashlib.sha256(address.encode()).hexdigest()[:16]
		findings.append(
			_finding(
				key=f"tenant-email:{address_key}:unassigned",
				title=f"E-Mail-Adresse keinem Mieter zugeordnet: {address}",
				problem_type="E-Mail-Adresse keinem Mieter zugeordnet",
				problem_code="mail.unassigned_tenant_address",
				description=(
					f"Die E-Mail-Adresse {address} kommt in {count} Nachricht(en) innerhalb von "
					f"{len(folders)} zugeordneten Mieterordner(n) vor, ist in ERPNext aber keinem "
					"Vertragspartner eines Mietvertrags zugeordnet. Ordnen Sie die Adresse dem passenden "
					"Kontakt zu oder setzen Sie dieses Problem auf Akzeptiert, wenn die Adresse nicht zu "
					"einem Mieter gehört."
				),
				reference_doctype="Mail Archive Message" if entry["first_message"] else "",
				reference_name=entry["first_message"],
				secondary_doctype="Mietvertrag" if contracts else "",
				secondary_name=contracts[0] if contracts else "",
				details={
					"email_address": address,
					"message_count": count,
					"accounts": accounts,
					"contracts": contracts,
					"folders": folders,
					"examples": entry["examples"],
				},
			)
		)
	return findings


def _missing_property_folder_finding(immobilie: str, *, tenant_root: bool) -> dict[str, Any]:
	label = "Mieterordner" if tenant_root else "Immobilienordner"
	problem_type = "Mieterordner fehlt" if tenant_root else "Immobilienordner fehlt"
	return _finding(
		key=f"immobilie:{immobilie}:{'tenant' if tenant_root else 'property'}-folder-missing",
		title=f"{label} für {immobilie} fehlt",
		problem_type=problem_type,
		problem_code=("mail.tenant_root_missing" if tenant_root else "mail.property_folder_missing"),
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
				problem_code="mail.ionos_source_missing",
				description=(
					"Richten Sie ein aktives Mail Filing Source Account für das IONOS-Postfach ein. "
					"Ohne IMAP-Zugang kann ERPNext dort keine Mietvertragstags setzen."
				),
				severity="Kritisch",
			)
		)
	global_tag_context = build_tag_context()
	for address, contract_names in ambiguous_contract_addresses(global_tag_context).items():
		address_key = hashlib.sha256(address.encode()).hexdigest()[:16]
		findings.append(
			_finding(
				key=f"tenant-email:{address_key}:ambiguous-contracts",
				title=f"Mieter-E-Mail-Adresse ist mehrdeutig: {address}",
				problem_type="Mieter-E-Mail-Adresse mehreren Mietverträgen zugeordnet",
				problem_code="mail.ambiguous_tenant_address",
				description=(
					"Die E-Mail-Adresse ist im selben Zeitraum mehreren Mietverträgen zugeordnet. "
					"ERPNext setzt deshalb ohne eindeutigen Mieterordner keinen Mietvertragstag."
				),
				reference_doctype="Mietvertrag",
				reference_name=contract_names[0],
				details={"email_address": address, "contracts": list(contract_names)},
			)
		)

	enabled_accounts = set(
		frappe.get_all(
			"Mail Archive Account", filters={"enabled": 1}, pluck="name", limit_page_length=0
		)
	)
	contract_by_mailbox: dict[tuple[str, str], str] = {}
	for account_name in sorted(enabled_accounts):
		account_context = build_tag_context(str(account_name))
		for mailbox_id, contract_name in account_context.contract_by_mailbox.items():
			if not contract_name:
				continue
			contract_by_mailbox[(str(account_name), str(mailbox_id))] = contract_name
	if contract_by_mailbox:
		messages = frappe.get_all(
			"Mail Archive Message",
			filters={
				"status": ["!=", "Gelöscht"],
				"actual_mailbox_id": [
					"in",
					sorted({mailbox_id for _account, mailbox_id in contract_by_mailbox}),
				],
			},
			fields=[
				"name",
				"archive_account",
				"actual_mailbox_id",
				"actual_folder_path",
				"subject",
				"sender_email",
				"participants",
			],
			order_by="received_at desc, name asc",
			limit_page_length=0,
		)
		messages = [
			message
			for message in messages
			if (str(message.archive_account), str(message.actual_mailbox_id)) in contract_by_mailbox
		]
		findings.extend(
			_unassigned_tenant_address_findings(
				messages,
				contract_by_mailbox=contract_by_mailbox,
				known_tenant_addresses=set(global_tag_context.contracts_by_address),
				own_addresses=set(global_tag_context.account_addresses),
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
				problem_code="mail.folder_duplicate_assignment",
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
						problem_code="mail.folder_missing_on_server",
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
					problem_code="mail.archive_account_mismatch",
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
						problem_code="mail.tenant_folder_unassigned",
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
						problem_code="mail.invalid_contract_reference",
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
						problem_code="mail.folder_wrong_property",
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
					problem_code="mail.multiple_current_folders",
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
						problem_code="mail.running_contract_missing_folder",
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
