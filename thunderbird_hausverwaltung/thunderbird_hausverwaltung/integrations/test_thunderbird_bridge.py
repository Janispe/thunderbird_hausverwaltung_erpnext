from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge import (
	_as_list,
	_is_active_contract_partner,
	_normalize_compose_payload,
	_normalize_search_payload,
	_preferred_contact_email,
)


class TestThunderbirdBridge(TestCase):
	def test_as_list_deduplicates_case_insensitively(self) -> None:
		self.assertEqual(_as_list([" Mieter ", "mieter", "Wichtig"]), ["Mieter", "Wichtig"])

	def test_normalize_search_payload_is_doctype_independent(self) -> None:
		payload = _normalize_search_payload(
			[" Mieter@Example.de ", "mieter@example.de"],
			["Mieter-42", "mieter-42"],
			"all",
			"Mieter Müller",
		)
		self.assertEqual(
			payload,
			{
				"command": "show_messages",
				"title": "Mieter Müller",
				"match": {
					"email_addresses": ["mieter@example.de"],
					"tags": ["Mieter-42"],
					"mode": "all",
				},
			},
		)

	def test_normalize_compose_payload(self) -> None:
		payload = _normalize_compose_payload(
			to=["mieter@example.de"],
			cc=["verwaltung@example.de"],
			subject="Ihre Anfrage",
			plain_text_body="Guten Tag,",
		)
		self.assertEqual(payload["command"], "compose_message")
		self.assertEqual(payload["compose"]["to"], ["mieter@example.de"])
		self.assertEqual(payload["compose"]["subject"], "Ihre Anfrage")

	def test_preferred_contact_email_uses_primary_address(self) -> None:
		contact = SimpleNamespace(
			email_id="legacy@example.de",
			email_ids=[
				SimpleNamespace(email_id="first@example.de", is_primary=0, idx=1),
				SimpleNamespace(email_id="primary@example.de", is_primary=1, idx=2),
			],
		)
		self.assertEqual(_preferred_contact_email(contact), "primary@example.de")

	def test_active_contract_partner_excludes_moved_out_rows(self) -> None:
		self.assertFalse(
			_is_active_contract_partner(
				SimpleNamespace(rolle="Hauptmieter", ausgezogen="2026-01-31"),
				"2026-02-01",
			)
		)
		self.assertFalse(
			_is_active_contract_partner(
				SimpleNamespace(rolle="Ausgezogen", ausgezogen=None),
				"2026-02-01",
			)
		)
		self.assertTrue(
			_is_active_contract_partner(
				SimpleNamespace(rolle="Partner", ausgezogen="2026-03-01"),
				"2026-02-01",
			)
		)
