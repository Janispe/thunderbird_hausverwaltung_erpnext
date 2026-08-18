from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge import (
	REALTIME_EVENT,
	_all_contact_emails,
	_as_list,
	_contract_contact_names,
	_device_registration_updates,
	_is_active_contract_partner,
	_is_thunderbird_extension_request,
	_normalize_compose_payload,
	_normalize_search_payload,
	_preferred_contact_email,
)


class TestThunderbirdBridge(TestCase):
	def test_contract_contact_names_include_historical_partners_and_deduplicate(self) -> None:
		contracts = [
			SimpleNamespace(
				mieter=[
					SimpleNamespace(mieter="CONTACT-OLD", rolle="Ausgezogen"),
					SimpleNamespace(mieter="CONTACT-SHARED", rolle="Partner"),
				]
			),
			SimpleNamespace(
				mieter=[
					SimpleNamespace(mieter="CONTACT-SHARED", rolle="Hauptmieter"),
					SimpleNamespace(mieter="CONTACT-NEW", rolle="Hauptmieter"),
				]
			),
		]
		self.assertEqual(
			_contract_contact_names(contracts),
			["CONTACT-OLD", "CONTACT-SHARED", "CONTACT-NEW"],
		)

	def test_all_contact_emails_includes_every_address_and_deduplicates(self) -> None:
		contact = SimpleNamespace(
			email_id="PRIMARY@example.de",
			email_ids=[
				SimpleNamespace(email_id="second@example.de", idx=2),
				SimpleNamespace(email_id="primary@example.de", idx=1),
				SimpleNamespace(email_id="", idx=3),
			],
		)
		self.assertEqual(
			_all_contact_emails(contact),
			["primary@example.de", "second@example.de"],
		)

	def test_realtime_event_name_is_stable(self) -> None:
		self.assertEqual(REALTIME_EVENT, "thunderbird_command_available")

	def test_repeated_device_registration_does_not_write(self) -> None:
		device = SimpleNamespace(device_name="Thunderbird", extension_version="0.2.0")
		self.assertEqual(_device_registration_updates(device, "Thunderbird", "0.2.0"), {})
		self.assertEqual(
			_device_registration_updates(device, "Büro", "0.2.1"),
			{"device_name": "Büro", "extension_version": "0.2.1"},
		)

	def test_cors_is_limited_to_thunderbird_bridge_requests(self) -> None:
		origin = "moz-extension://2ba4abe6-0a3e-4e49-94bc-67afb971af41"
		self.assertTrue(
			_is_thunderbird_extension_request(
				origin,
				"/api/method/thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations."
				"thunderbird_bridge.register_device",
			)
		)
		self.assertTrue(
			_is_thunderbird_extension_request(
				origin,
				"/api/method/hausverwaltung.hausverwaltung.integrations.thunderbird_bridge.poll_command",
			)
		)
		self.assertFalse(_is_thunderbird_extension_request("https://example.com", "/api/method/login"))
		self.assertFalse(_is_thunderbird_extension_request(origin, "/api/method/frappe.auth.get_logged_user"))

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
