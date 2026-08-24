from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from ..doctype.mail_archive_account.mail_archive_account import normalize_account_addresses
from .classifier import _matches_business_context
from .embeddings import clean_message_text, cosine_similarity, normalize_vector
from .providers.base import ArchiveMailbox, ArchiveMessage
from .providers.jmap import JMAPConfig, JMAPProvider
from .sync import _mailbox_truth, build_mailbox_paths, folder_record_name, message_record_name


class TestMailArchive(TestCase):
	def test_account_addresses_are_normalized_and_deduplicated(self) -> None:
		self.assertEqual(
			normalize_account_addresses(" Archiv@Example.de\narchiv@example.de;team@example.de "),
			["archiv@example.de", "team@example.de"],
		)

	def test_mailbox_paths_use_stable_parent_ids(self) -> None:
		mailboxes = [
			ArchiveMailbox(id="root", name="Archiv"),
			ArchiveMailbox(id="object", name="Hauptstraße 1", parent_id="root"),
			ArchiveMailbox(id="contract", name="MV-1", parent_id="object"),
		]
		self.assertEqual(
			build_mailbox_paths(mailboxes),
			{
				"root": "Archiv",
				"object": "Archiv/Hauptstraße 1",
				"contract": "Archiv/Hauptstraße 1/MV-1",
			},
		)

	def test_record_names_are_stable_and_account_scoped(self) -> None:
		self.assertEqual(folder_record_name("Konto", "M1"), folder_record_name("Konto", "M1"))
		self.assertNotEqual(folder_record_name("Konto", "M1"), folder_record_name("Konto", "M2"))
		self.assertNotEqual(message_record_name("A", "E1"), message_record_name("B", "E1"))

	def test_clean_message_text_removes_signature_and_quoted_history(self) -> None:
		self.assertEqual(
			clean_message_text("Guten Tag,\n\ndas ist neu.\n\n-- \nSignatur\n> alter Text"),
			"Guten Tag,\n\ndas ist neu.",
		)
		self.assertEqual(
			clean_message_text("Antwort\n\nAm 20.08.2026 schrieb Max Mustermann:\nAlter Text"),
			"Antwort",
		)

	def test_vectors_are_normalized_before_cosine_scoring(self) -> None:
		first = normalize_vector([3, 4])
		self.assertAlmostEqual(cosine_similarity(first, first), 1.0)
		self.assertAlmostEqual(cosine_similarity(first, normalize_vector([-4, 3])), 0.0)

	def test_jmap_message_conversion_keeps_stable_ids_and_plain_text(self) -> None:
		provider = JMAPProvider(JMAPConfig("https://mail.example", "user", "secret"))
		message = provider._to_message(
			{
				"id": "E1",
				"threadId": "T1",
				"mailboxIds": {"inbox": True},
				"messageId": ["<mail@example.de>"],
				"from": [{"name": "Mieter", "email": "MIETER@EXAMPLE.DE"}],
				"textBody": [{"partId": "1"}],
				"bodyValues": {"1": {"value": "Nachrichtentext"}},
			}
		)
		self.assertEqual(message.id, "E1")
		self.assertEqual(message.thread_id, "T1")
		self.assertEqual(message.rfc_message_ids, ("<mail@example.de>",))
		self.assertEqual(message.sender[0]["email"], "mieter@example.de")
		self.assertEqual(message.text_body, "Nachrichtentext")

	def test_jmap_discovery_always_uses_well_known_root(self) -> None:
		provider = JMAPProvider(JMAPConfig("https://mail.example/prefix", "user", "secret"))
		response = Mock(ok=True)
		response.json.return_value = {
			"capabilities": {
				"urn:ietf:params:jmap:core": {},
				"urn:ietf:params:jmap:mail": {},
			},
			"apiUrl": "https://mail.example/jmap",
			"accounts": {"A1": {"name": "Archiv"}},
			"primaryAccounts": {"urn:ietf:params:jmap:mail": "A1"},
		}
		provider.http.get = Mock(return_value=response)
		self.assertEqual(provider.account_id, "A1")
		provider.http.get.assert_called_once_with(
			"https://mail.example/.well-known/jmap",
			headers={"Accept": "application/json"},
			timeout=30,
		)

	def test_jmap_mailboxes_are_queried_and_fetched_in_bounded_batches(self) -> None:
		provider = JMAPProvider(JMAPConfig("https://mail.example", "user", "secret", account_id="A1"))
		provider._session = {
			"capabilities": {
				"urn:ietf:params:jmap:core": {"maxObjectsInGet": 500},
				"urn:ietf:params:jmap:mail": {},
			},
			"accounts": {"A1": {"name": "Archiv"}},
		}
		all_ids = [f"M{index}" for index in range(704)]
		get_batch_sizes: list[int] = []

		def respond(method, arguments):
			if method == "Mailbox/query":
				position = arguments["position"]
				return {
					"ids": all_ids[position : position + arguments["limit"]],
					"total": len(all_ids),
				}
			if method == "Mailbox/get":
				ids = arguments["ids"]
				get_batch_sizes.append(len(ids))
				return {
					"list": [
						{
							"id": mailbox_id,
							"name": mailbox_id,
							"parentId": None,
							"role": None,
						}
						for mailbox_id in ids
					]
				}
			raise AssertionError(method)

		provider._single = Mock(side_effect=respond)
		self.assertEqual(len(provider.list_mailboxes()), 704)
		self.assertEqual(get_batch_sizes, [500, 204])

	def test_multiple_target_mailboxes_are_never_used_as_training_truth(self) -> None:
		message = ArchiveMessage(id="E1", thread_id="T1", mailbox_ids=("M1", "M2"))
		folders = {
			"M1": SimpleNamespace(selectable_target=1, provider_mailbox_id="M1", folder_path="A", role=""),
			"M2": SimpleNamespace(selectable_target=1, provider_mailbox_id="M2", folder_path="B", role=""),
		}
		self.assertEqual(_mailbox_truth(message, folders), ("", "", "Nicht klassifizierbar"))

	def test_business_context_only_matches_the_exact_contract_identity(self) -> None:
		context = {"mietvertrag": "MV-1", "wohnung": "W-1", "customer": "C-MV-1"}
		self.assertTrue(
			_matches_business_context(
				SimpleNamespace(reference_doctype="Mietvertrag", reference_name="MV-1"), context
			)
		)
		self.assertFalse(
			_matches_business_context(
				SimpleNamespace(reference_doctype="Customer", reference_name="C-MV-2"), context
			)
		)
