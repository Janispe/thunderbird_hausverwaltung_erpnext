from __future__ import annotations

from unittest import TestCase

from thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge import (
	_as_list,
	_normalize_compose_payload,
	_normalize_search_payload,
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
