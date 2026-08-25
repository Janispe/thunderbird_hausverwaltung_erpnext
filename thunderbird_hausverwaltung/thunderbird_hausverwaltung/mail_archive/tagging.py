from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import date
from email.utils import parseaddr
from typing import Any, Callable

import frappe
from frappe.utils import get_datetime, now_datetime

from .providers.base import ArchiveMessage, MailArchiveProvider

PROPERTY_KEY_PREFIX = "hv-immobilie-"
CONTRACT_KEY_PREFIX = "hv-mietvertrag-"
NO_PROPERTY_KEY = "hv-keine-immobilie"
TAG_COLORS = ("#1F6FEB", "#8250DF", "#BF3989", "#0A7C66", "#9A6700", "#CF222E")


def property_keyword(immobilie: str) -> str:
	stable = str(immobilie or "").strip().casefold().encode("utf-8")
	return f"{PROPERTY_KEY_PREFIX}{hashlib.sha256(stable).hexdigest()[:16]}"


def contract_keyword(mietvertrag: str) -> str:
	stable = str(mietvertrag or "").strip().casefold().encode("utf-8")
	return f"{CONTRACT_KEY_PREFIX}{hashlib.sha256(stable).hexdigest()[:16]}"


def is_managed_property_keyword(keyword: str) -> bool:
	value = str(keyword or "").casefold()
	return value == NO_PROPERTY_KEY or value.startswith(PROPERTY_KEY_PREFIX)


def is_managed_contract_keyword(keyword: str) -> bool:
	return str(keyword or "").casefold().startswith(CONTRACT_KEY_PREFIX)


def contract_tag_label(bezeichnung: str | None, fallback: str) -> str:
	"""Keep contract tags compact because the property already has its own tag."""
	title = str(bezeichnung or fallback or "").strip()
	if " · " in title:
		_title_property, title = title.split(" · ", 1)
	return f"MV · {title}"


# Compatibility for callers that used the original property-only helper.
is_managed_keyword = is_managed_property_keyword


def keyword_patch(
	current: set[str],
	desired: set[str],
	*,
	managed_predicate: Callable[[str], bool] = is_managed_property_keyword,
) -> dict[str, bool | None]:
	managed = {keyword.casefold() for keyword in current if managed_predicate(keyword)}
	desired_keywords = {keyword.casefold() for keyword in desired if keyword}
	patch = {keyword: None for keyword in managed - desired_keywords}
	for keyword in desired_keywords - managed:
		patch[keyword] = True
	return patch


def _property_tag_definitions() -> list[dict[str, str]]:
	properties = frappe.get_all(
		"Immobilie",
		filters={"parent_immobilie": ["is", "not set"]},
		fields=["name", "bezeichnung"],
		order_by="name asc",
		limit_page_length=0,
	)
	return [
		{
			"key": property_keyword(str(row.name)),
			"tag": str(row.bezeichnung or row.name),
			"color": TAG_COLORS[index % len(TAG_COLORS)],
		}
		for index, row in enumerate(properties)
	]


def _property_colors() -> dict[str, str]:
	return {
		str(row.name): TAG_COLORS[index % len(TAG_COLORS)]
		for index, row in enumerate(
			frappe.get_all(
				"Immobilie",
				filters={"parent_immobilie": ["is", "not set"]},
				fields=["name"],
				order_by="name asc",
				limit_page_length=0,
			)
		)
	}


def _contract_tag_definitions() -> list[dict[str, str]]:
	colors = _property_colors()
	properties = {
		str(row.name): str(row.parent_immobilie or row.name)
		for row in frappe.get_all("Immobilie", fields=["name", "parent_immobilie"], limit_page_length=0)
	}
	return [
		{
			"key": contract_keyword(str(row.name)),
			"tag": contract_tag_label(row.bezeichnung, str(row.name)),
			"color": colors.get(properties.get(str(row.immobilie or ""), ""), "#6E7781"),
		}
		for row in frappe.get_all(
			"Mietvertrag",
			filters={"docstatus": 1},
			fields=["name", "bezeichnung", "immobilie"],
			order_by="bezeichnung asc, name asc",
			limit_page_length=0,
		)
	]


def get_tag_definitions() -> list[dict[str, str]]:
	definitions = _property_tag_definitions()
	definitions.append({"key": NO_PROPERTY_KEY, "tag": "Keine Immobilie", "color": "#6E7781"})
	definitions.extend(_contract_tag_definitions())
	return definitions


@dataclass(frozen=True)
class ContractAddressPeriod:
	contract: str
	start: date | None
	end: date | None


@dataclass(frozen=True)
class TagContext:
	property_by_mailbox: dict[str, str]
	contract_by_mailbox: dict[str, str]
	contracts_by_address: dict[str, tuple[ContractAddressPeriod, ...]]
	account_addresses: frozenset[str]

	def keywords_for_mailbox(self, mailbox_id: str) -> set[str]:
		immobilie = self.property_by_mailbox.get(str(mailbox_id or ""), "")
		return {property_keyword(immobilie)} if immobilie else set()

	def contract_for_message(self, message: ArchiveMessage, mailbox_id: str = "") -> str:
		folder_contract = self.contract_by_mailbox.get(str(mailbox_id or ""), "")
		if folder_contract:
			return folder_contract

		periods: list[ContractAddressPeriod] = []
		seen_periods: set[tuple[str, date | None, date | None]] = set()
		for group in (message.sender, message.to, message.cc):
			for participant in group:
				address = _normalize_email(participant.get("email"))
				if not address or address in self.account_addresses:
					continue
				for period in self.contracts_by_address.get(address, ()):
					key = (period.contract, period.start, period.end)
					if key in seen_periods:
						continue
					periods.append(period)
					seen_periods.add(key)

		contracts = {period.contract for period in periods}
		if len(contracts) == 1:
			return next(iter(contracts))
		message_day = _message_day(message.received_at)
		if not message_day:
			return ""
		matching = {
			period.contract
			for period in periods
			if (not period.start or period.start <= message_day)
			and (not period.end or message_day <= period.end)
		}
		return next(iter(matching)) if len(matching) == 1 else ""

	def contract_keywords_for_message(self, message: ArchiveMessage, mailbox_id: str = "") -> set[str]:
		contract = self.contract_for_message(message, mailbox_id)
		return {contract_keyword(contract)} if contract else set()


def _normalize_email(value: Any) -> str:
	raw = str(value or "").strip()
	if not raw:
		return ""
	parsed = parseaddr(raw)[1] or raw
	return parsed.strip().casefold()


def _message_day(value: Any) -> date | None:
	if not value:
		return None
	try:
		return get_datetime(value).date()
	except (TypeError, ValueError):
		return None


def _period_start(*values: Any) -> date | None:
	valid = [get_datetime(value).date() for value in values if value]
	return max(valid) if valid else None


def _period_end(*values: Any) -> date | None:
	valid = [get_datetime(value).date() for value in values if value]
	return min(valid) if valid else None


def _configured_account_addresses() -> frozenset[str]:
	addresses: set[str] = set()
	for doctype in ("Mail Archive Account", "Mail Filing Source Account"):
		if not frappe.db.table_exists(doctype):
			continue
		for value in frappe.get_all(doctype, pluck="email_addresses", limit_page_length=0):
			for item in str(value or "").replace(";", "\n").replace(",", "\n").splitlines():
				if address := _normalize_email(item):
					addresses.add(address)
	return frozenset(addresses)


def _contracts_by_address(contracts: dict[str, Any]) -> dict[str, tuple[ContractAddressPeriod, ...]]:
	by_address: dict[str, list[ContractAddressPeriod]] = {}
	contract_names = list(contracts)
	if not contract_names:
		return {}
	partners = frappe.get_all(
		"Vertragspartner",
		filters={
			"parenttype": "Mietvertrag",
			"parentfield": "mieter",
			"parent": ["in", contract_names],
		},
		fields=["parent", "mieter", "eingezogen", "ausgezogen"],
		limit_page_length=0,
	)
	contact_names = sorted({str(row.mieter) for row in partners if row.mieter})
	emails_by_contact: dict[str, set[str]] = {}
	if contact_names:
		for row in frappe.get_all(
			"Contact Email",
			filters={"parent": ["in", contact_names]},
			fields=["parent", "email_id"],
			limit_page_length=0,
		):
			if address := _normalize_email(row.email_id):
				emails_by_contact.setdefault(str(row.parent), set()).add(address)
	for row in partners:
		contract = contracts.get(str(row.parent))
		if not contract:
			continue
		period = ContractAddressPeriod(
			contract=str(contract.name),
			start=_period_start(contract.von, row.eingezogen),
			end=_period_end(contract.bis, row.ausgezogen),
		)
		for address in emails_by_contact.get(str(row.mieter), set()):
			by_address.setdefault(address, []).append(period)

	# Customer is the debtor identity of exactly one contract, so its primary
	# address is a safe fallback when the linked Contact has no email child row.
	customers = {str(row.kunde): str(row.name) for row in contracts.values() if row.kunde}
	if customers:
		for row in frappe.get_all(
			"Customer",
			filters={"name": ["in", list(customers)]},
			fields=["name", "email_id"],
			limit_page_length=0,
		):
			address = _normalize_email(row.email_id)
			contract = contracts.get(customers.get(str(row.name), ""))
			if not address or not contract:
				continue
			if any(period.contract == str(contract.name) for period in by_address.get(address, [])):
				continue
			by_address.setdefault(address, []).append(
				ContractAddressPeriod(
					contract=str(contract.name),
					start=_period_start(contract.von),
					end=_period_end(contract.bis),
				)
			)

	result: dict[str, tuple[ContractAddressPeriod, ...]] = {}
	for address, periods in by_address.items():
		unique = {(period.contract, period.start, period.end): period for period in periods}
		result[address] = tuple(unique.values())
	return result


def ambiguous_contract_addresses(
	context: TagContext,
) -> dict[str, tuple[str, ...]]:
	result: dict[str, tuple[str, ...]] = {}
	for address, periods in context.contracts_by_address.items():
		contracts = sorted({period.contract for period in periods})
		if len(contracts) < 2:
			continue
		overlaps: set[str] = set()
		for index, first in enumerate(periods):
			for second in periods[index + 1 :]:
				if first.contract == second.contract:
					continue
				start = max(first.start or date.min, second.start or date.min)
				end = min(first.end or date.max, second.end or date.max)
				if start <= end:
					overlaps.update((first.contract, second.contract))
		if overlaps:
			result[address] = tuple(sorted(overlaps))
	return result


def build_tag_context(account_name: str = "") -> TagContext:
	folders = (
		frappe.get_all(
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
		if account_name
		else []
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
	property_roots = {str(row.name): str(row.parent_immobilie or row.name) for row in properties}
	explicit_roots: dict[str, str] = {}
	for row in properties:
		if row.parent_immobilie:
			continue
		for folder_name in (row.custom_immobilien_archivordner, row.custom_mieter_archivordner):
			provider_id = provider_id_by_name.get(str(folder_name or ""), "")
			if provider_id:
				explicit_roots[provider_id] = str(row.name)

	contract_rows = frappe.get_all(
		"Mietvertrag",
		filters={"docstatus": 1},
		fields=["name", "immobilie", "kunde", "von", "bis"],
		limit_page_length=0,
	)
	contracts = {str(row.name): row for row in contract_rows}
	contract_properties = {
		name: property_roots.get(str(row.immobilie or ""), str(row.immobilie or ""))
		for name, row in contracts.items()
	}
	property_cache: dict[str, str] = {}
	contract_cache: dict[str, str] = {}

	def resolve_property(mailbox_id: str, visiting: set[str] | None = None) -> str:
		if mailbox_id in property_cache:
			return property_cache[mailbox_id]
		if mailbox_id in explicit_roots:
			property_cache[mailbox_id] = explicit_roots[mailbox_id]
			return property_cache[mailbox_id]
		visiting = visiting or set()
		if mailbox_id in visiting:
			return ""
		row = by_provider_id.get(mailbox_id)
		if not row:
			return ""
		reference_doctype = str(row.reference_doctype or "")
		reference_name = str(row.reference_name or "")
		if reference_doctype == "Mietvertrag" and contract_properties.get(reference_name):
			result = contract_properties[reference_name]
		elif reference_doctype == "Immobilie" and reference_name:
			result = property_roots.get(reference_name, reference_name)
		else:
			parent_id = str(row.parent_mailbox_id or "")
			result = resolve_property(parent_id, visiting | {mailbox_id}) if parent_id else ""
		property_cache[mailbox_id] = result
		return result

	def resolve_contract(mailbox_id: str, visiting: set[str] | None = None) -> str:
		if mailbox_id in contract_cache:
			return contract_cache[mailbox_id]
		visiting = visiting or set()
		if mailbox_id in visiting:
			return ""
		row = by_provider_id.get(mailbox_id)
		if not row:
			return ""
		reference_doctype = str(row.reference_doctype or "")
		reference_name = str(row.reference_name or "")
		if reference_doctype == "Mietvertrag" and reference_name in contracts:
			result = reference_name
		else:
			parent_id = str(row.parent_mailbox_id or "")
			result = resolve_contract(parent_id, visiting | {mailbox_id}) if parent_id else ""
		contract_cache[mailbox_id] = result
		return result

	return TagContext(
		property_by_mailbox={mailbox_id: resolve_property(mailbox_id) for mailbox_id in by_provider_id},
		contract_by_mailbox={mailbox_id: resolve_contract(mailbox_id) for mailbox_id in by_provider_id},
		contracts_by_address=_contracts_by_address(contracts),
		account_addresses=_configured_account_addresses(),
	)


def apply_managed_tags(
	provider: MailArchiveProvider,
	messages: list[ArchiveMessage],
	folders: dict[str, Any],
	context: TagContext,
) -> dict[str, Any]:
	updates: dict[str, dict[str, bool | None]] = {}
	by_tag: Counter[str] = Counter()
	for message in messages:
		targets = [
			mailbox_id
			for mailbox_id in message.mailbox_ids
			if mailbox_id in folders and folders[mailbox_id].selectable_target
		]
		target = targets[0] if len(targets) == 1 else ""
		property_desired = context.keywords_for_mailbox(target) if target else set()
		contract_desired = context.contract_keywords_for_message(message, target)
		desired = property_desired | contract_desired
		by_tag.update(desired or {"untagged"})
		current = set(message.keywords)
		patch = keyword_patch(current, property_desired)
		patch.update(
			keyword_patch(
				current,
				contract_desired,
				managed_predicate=is_managed_contract_keyword,
			)
		)
		if patch:
			updates[message.id] = patch
	provider.patch_keywords(updates)
	return {"considered": len(messages), "updated": len(updates), "by_tag": dict(by_tag)}


def apply_contract_tags(provider: Any, messages: list[ArchiveMessage], context: TagContext) -> dict[str, Any]:
	updates: dict[str, dict[str, bool | None]] = {}
	by_tag: Counter[str] = Counter()
	for message in messages:
		desired = context.contract_keywords_for_message(message)
		by_tag.update(desired or {"untagged"})
		patch = keyword_patch(set(message.keywords), desired, managed_predicate=is_managed_contract_keyword)
		if patch:
			updates[message.id] = patch
	provider.patch_keywords(updates)
	return {"considered": len(messages), "updated": len(updates), "by_tag": dict(by_tag)}


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


def reset_source_tag_sync(account_name: str) -> None:
	if not frappe.db.has_column("Mail Filing Source Account", "tag_sync_completed"):
		return
	frappe.db.set_value(
		"Mail Filing Source Account",
		account_name,
		{"tag_sync_completed": 0, "tag_sync_cursor": ""},
		update_modified=False,
	)


def invalidate_tag_sync(doc: Any | None = None, method: str | None = None) -> None:
	del method
	if frappe.flags.in_install or frappe.flags.in_migrate:
		return
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
	if getattr(doc, "doctype", "") == "Mail Archive Folder":
		return
	for source_name in frappe.get_all(
		"Mail Filing Source Account", filters={"enabled": 1}, pluck="name", limit_page_length=0
	):
		reset_source_tag_sync(source_name)
		frappe.enqueue(
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.source_sync.sync_source_account",
			queue="long",
			job_id=f"mail-source-sync::{frappe.local.site}::{source_name}",
			deduplicate=True,
			enqueue_after_commit=True,
			account_name=source_name,
		)
