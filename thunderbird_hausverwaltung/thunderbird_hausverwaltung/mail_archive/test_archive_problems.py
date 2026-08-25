from datetime import date
from types import SimpleNamespace
from unittest import TestCase

from .providers.base import ArchiveMessage
from .problems import _tenant_folder_candidates
from .tagging import (
	NO_PROPERTY_KEY,
	ContractAddressPeriod,
	TagContext,
	ambiguous_contract_addresses,
	contract_keyword,
	is_managed_contract_keyword,
	is_managed_keyword,
	keyword_patch,
	property_keyword,
)


def folder(
	mailbox_id: str,
	parent_id: str,
	name: str,
	*,
	folder_type: str = "Automatisch",
) -> SimpleNamespace:
	return SimpleNamespace(
		provider_mailbox_id=mailbox_id,
		parent_mailbox_id=parent_id,
		folder_name=name,
		folder_type=folder_type,
	)


class TestArchiveProblems(TestCase):
	def test_property_keywords_are_stable_and_only_replace_managed_tags(self) -> None:
		desired = property_keyword("Gropiusstr.")
		self.assertEqual(desired, property_keyword("gropiusstr."))
		self.assertTrue(is_managed_keyword(desired))
		self.assertEqual(
			keyword_patch({"$seen", NO_PROPERTY_KEY, "privat"}, {desired}),
			{NO_PROPERTY_KEY: None, desired: True},
		)

	def test_matching_property_keyword_needs_no_server_update(self) -> None:
		desired = property_keyword("Leinestr.")
		self.assertEqual(keyword_patch({"$seen", desired}, {desired}), {})

	def test_unknown_property_removes_old_fallback_tag_without_replacement(self) -> None:
		self.assertEqual(keyword_patch({"$seen", NO_PROPERTY_KEY}, set()), {NO_PROPERTY_KEY: None})

	def test_contract_keywords_are_stable_and_do_not_replace_property_or_private_tags(self) -> None:
		desired = contract_keyword("MV-1")
		self.assertEqual(desired, contract_keyword("mv-1"))
		self.assertTrue(is_managed_contract_keyword(desired))
		self.assertEqual(
			keyword_patch(
				{"$seen", "privat", "hv-immobilie-1", contract_keyword("MV-ALT")},
				{desired},
				managed_predicate=is_managed_contract_keyword,
			),
			{contract_keyword("MV-ALT"): None, desired: True},
		)

	def test_unique_tenant_address_resolves_to_its_contract(self) -> None:
		context = TagContext(
			property_by_mailbox={},
			contract_by_mailbox={},
			contracts_by_address={
				"tenant@example.test": (ContractAddressPeriod("MV-1", date(2025, 1, 1), None),)
			},
			account_addresses=frozenset({"office@example.test"}),
		)
		message = ArchiveMessage(
			id="E1",
			thread_id="T1",
			mailbox_ids=(),
			sender=({"email": "TENANT@example.test", "name": ""},),
			to=({"email": "office@example.test", "name": ""},),
			received_at="2024-01-01T10:00:00Z",
		)
		self.assertEqual(context.contract_for_message(message), "MV-1")

	def test_message_date_disambiguates_reused_address(self) -> None:
		context = TagContext(
			property_by_mailbox={},
			contract_by_mailbox={},
			contracts_by_address={
				"tenant@example.test": (
					ContractAddressPeriod("MV-ALT", date(2018, 1, 1), date(2020, 12, 31)),
					ContractAddressPeriod("MV-NEU", date(2021, 1, 1), None),
				)
			},
			account_addresses=frozenset(),
		)
		message = ArchiveMessage(
			id="E1",
			thread_id="T1",
			mailbox_ids=(),
			sender=({"email": "tenant@example.test", "name": ""},),
			received_at="2019-06-01T10:00:00Z",
		)
		self.assertEqual(context.contract_for_message(message), "MV-ALT")

	def test_ambiguous_address_remains_untagged_but_contract_folder_wins(self) -> None:
		periods = (
			ContractAddressPeriod("MV-1", date(2020, 1, 1), None),
			ContractAddressPeriod("MV-2", date(2020, 1, 1), None),
		)
		context = TagContext(
			property_by_mailbox={},
			contract_by_mailbox={"FOLDER": "MV-2"},
			contracts_by_address={"shared@example.test": periods},
			account_addresses=frozenset(),
		)
		message = ArchiveMessage(
			id="E1",
			thread_id="T1",
			mailbox_ids=(),
			sender=({"email": "shared@example.test", "name": ""},),
			received_at="2026-01-01T10:00:00Z",
		)
		self.assertEqual(context.contract_for_message(message), "")
		self.assertEqual(context.contract_for_message(message, "FOLDER"), "MV-2")
		self.assertEqual(
			ambiguous_contract_addresses(context),
			{"shared@example.test": ("MV-1", "MV-2")},
		)

	def test_tenant_candidates_include_current_and_historical_roots_only(self) -> None:
		root = folder("ROOT", "", "Mieter G", folder_type="Mieter-Wurzelordner")
		current = folder("CURRENT", "ROOT", "01-VH-Mieter")
		old_container = folder("OLD", "ROOT", "00-Alte Mieter", folder_type="Strukturordner")
		historical = folder("HIST", "OLD", "01-VH-Altmieter")
		ignored = folder("IGNORE", "ROOT", "Hauswart", folder_type="Strukturordner")
		ignored_child = folder("IGNORE-CHILD", "IGNORE", "Hauswart alt")
		folders_by_parent = {
			"ROOT": [current, old_container, ignored],
			"OLD": [historical],
			"IGNORE": [ignored_child],
		}

		self.assertEqual(
			[
				(item.provider_mailbox_id, historical)
				for item, historical in _tenant_folder_candidates(root, folders_by_parent)
			],
			[("CURRENT", False), ("HIST", True)],
		)

	def test_explicit_structure_folder_suppresses_false_positive(self) -> None:
		root = folder("ROOT", "", "Mieter W", folder_type="Mieter-Wurzelordner")
		legal = folder("LEGAL", "ROOT", "Rechtsberatung", folder_type="Strukturordner")

		self.assertEqual(_tenant_folder_candidates(root, {"ROOT": [legal]}), [])
