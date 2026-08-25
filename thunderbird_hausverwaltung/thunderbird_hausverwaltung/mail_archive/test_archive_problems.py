from datetime import date
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from .providers.base import ArchiveMessage
from .problems import _tenant_folder_candidates, _unassigned_tenant_address_findings
from .problem_types import UnassignedTenantAddressProblemType
from .tagging import (
	NO_PROPERTY_KEY,
	ContractAddressPeriod,
	TagContext,
	ambiguous_contract_addresses,
	contract_keyword,
	_normalize_email,
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
	@patch(
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problem_types.frappe.has_permission",
		return_value=True,
	)
	@patch(
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problem_types.frappe.get_all",
		return_value=[SimpleNamespace(mieter="CONTACT-1")],
	)
	def test_unknown_address_problem_type_describes_its_own_ui(
		self, _get_all: Mock, _has_permission: Mock
	) -> None:
		ui = UnassignedTenantAddressProblemType().get_ui(
			SimpleNamespace(status="Offen"),
			{
				"email_address": "unknown@example.test",
				"message_count": 2,
				"accounts": ["Archiv"],
				"contracts": ["MV-1"],
				"folders": ["Archiv/Mieter/Müller"],
				"examples": [{"subject": "Frage", "folder": "Archiv/Mieter/Müller"}],
			},
		)

		self.assertEqual(
			[item["key"] for item in ui["actions"]],
			["assign_contact_email", "add_own_address"],
		)
		assign_fields = {item["fieldname"]: item for item in ui["actions"][0]["fields"]}
		self.assertEqual(assign_fields["contract"]["filters"], {"name": ["in", ["MV-1"]]})
		self.assertEqual(assign_fields["contact"]["filters"], {"name": ["in", ["CONTACT-1"]]})

	@patch(
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problem_types.frappe.get_doc"
	)
	@patch(
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problem_types.frappe.db.exists",
		return_value=True,
	)
	def test_assign_email_only_writes_to_a_contract_partner(
		self, _exists: Mock, get_doc: Mock
	) -> None:
		contact = SimpleNamespace(
			email_ids=[],
			check_permission=Mock(),
			append=Mock(),
			save=Mock(),
		)
		get_doc.return_value = contact

		result = UnassignedTenantAddressProblemType._assign_contact_email(
			{"contracts": ["MV-1"]},
			"unknown@example.test",
			{"contract": "MV-1", "contact": "CONTACT-1"},
		)

		contact.check_permission.assert_called_once_with("write")
		contact.append.assert_called_once_with(
			"email_ids", {"email_id": "unknown@example.test"}
		)
		contact.save.assert_called_once_with()
		self.assertTrue(result["recheck"])

	def test_email_normalization_removes_legacy_wrapping_quotes(self) -> None:
		self.assertEqual(_normalize_email("'Tenant@Example.test'"), "tenant@example.test")
		self.assertEqual(
			_normalize_email('Mieter \"Beispiel\" <Tenant@Example.test>'),
			"tenant@example.test",
		)
		self.assertEqual(_normalize_email("@example.test"), "")
		self.assertEqual(_normalize_email("not-an-email"), "")

	def test_unknown_address_creates_one_aggregated_problem_across_messages(self) -> None:
		messages = [
			{
				"name": "MAIL-1",
				"archive_account": "Archiv",
				"actual_mailbox_id": "FOLDER",
				"actual_folder_path": "Archiv/Mieter/Müller",
				"subject": "Erste Nachricht",
				"sender_email": "",
				"participants": '{"from":[{"email":"UNKNOWN@example.test"}],'
				'"to":[{"email":"office@example.test"}],"cc":[]}',
			},
			{
				"name": "MAIL-2",
				"archive_account": "Archiv",
				"actual_mailbox_id": "CHILD",
				"actual_folder_path": "Archiv/Mieter/Müller/Unterlagen",
				"subject": "Zweite Nachricht",
				"sender_email": "unknown@example.test",
				"participants": "",
			},
		]
		findings = _unassigned_tenant_address_findings(
			messages,
			contract_by_mailbox={
				("Archiv", "FOLDER"): "MV-1",
				("Archiv", "CHILD"): "MV-1",
			},
			known_tenant_addresses={"tenant@example.test"},
			own_addresses={"office@example.test"},
		)

		self.assertEqual(len(findings), 1)
		self.assertEqual(findings[0]["details"]["email_address"], "unknown@example.test")
		self.assertEqual(findings[0]["details"]["message_count"], 2)
		self.assertEqual(findings[0]["details"]["contracts"], ["MV-1"])
		self.assertEqual(findings[0]["reference_name"], "MAIL-1")

	def test_known_tenant_own_and_unmapped_addresses_do_not_create_problems(self) -> None:
		messages = [
			{
				"name": "MAIL-1",
				"archive_account": "Archiv",
				"actual_mailbox_id": "FOLDER",
				"participants": {
					"from": [{"email": "tenant@example.test"}],
					"to": [{"email": "office@example.test"}],
				},
			},
			{
				"name": "MAIL-2",
				"archive_account": "Archiv",
				"actual_mailbox_id": "OTHER",
				"sender_email": "unknown@example.test",
				"participants": "",
			},
		]
		self.assertEqual(
			_unassigned_tenant_address_findings(
				messages,
				contract_by_mailbox={("Archiv", "FOLDER"): "MV-1"},
				known_tenant_addresses={"tenant@example.test"},
				own_addresses={"office@example.test"},
			),
			[],
		)

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
