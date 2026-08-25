from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from ..doctype.mail_archive_account.mail_archive_account import normalize_account_addresses
from .classifier import _matches_business_context
from .embeddings import clean_message_text, cosine_similarity, normalize_vector
from .error_diagnostics import SYSTEM_LIKE_FOLDER_RE, _folder_token, _path_parts, _segment
from .evaluation import (
	_address_history_rankings,
	_embed,
	_embedding_text,
	_infer_own_addresses,
	_participant_addresses,
	_participant_coverage,
	_sender_gate_rankings,
	_split_rows,
)
from .filing import _find_indexed_message_doc, _normalize_rfc_message_id
from .providers.base import ArchiveMailbox, ArchiveMessage
from .providers.jmap import JMAPConfig, JMAPProvider
from .sync import _mailbox_truth, build_mailbox_paths, folder_record_name, message_record_name


class TestMailArchive(TestCase):
	def test_rfc_message_id_normalization_accepts_thunderbird_and_jmap_forms(self) -> None:
		self.assertEqual(_normalize_rfc_message_id(" <mail@example.test> "), "mail@example.test")
		self.assertEqual(_normalize_rfc_message_id("mail@example.test"), "mail@example.test")

	@patch("thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.filing.frappe.get_doc")
	@patch("thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.filing.frappe.get_all")
	def test_indexed_message_lookup_uses_normalized_id_before_jmap(
		self, get_all: Mock, get_doc: Mock
	) -> None:
		document = SimpleNamespace(name="MAM-1")
		get_all.return_value = [SimpleNamespace(name="MAM-1")]
		get_doc.return_value = document

		self.assertIs(_find_indexed_message_doc("Archiv", "<mail@example.test>"), document)
		get_all.assert_called_once_with(
			"Mail Archive Message",
			filters={
				"archive_account": "Archiv",
				"rfc_message_id": ["in", ["mail@example.test", "<mail@example.test>"]],
			},
			fields=["name"],
			order_by="received_at desc, name asc",
			limit_page_length=2,
		)
		get_doc.assert_called_once_with("Mail Archive Message", "MAM-1")

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

	def test_evaluation_split_is_chronological_per_folder_and_excludes_sparse_folders(self) -> None:
		rows = [
			SimpleNamespace(name=f"A-{index}", actual_mailbox_id="A", received_at=index, thread_id="")
			for index in range(10)
		]
		rows.extend(
			SimpleNamespace(name=f"B-{index}", actual_mailbox_id="B", received_at=index, thread_id="")
			for index in range(3)
		)
		train, test, info = _split_rows(rows, min_messages_per_folder=8, test_fraction=0.2)
		self.assertEqual([row.name for row in train], [f"A-{index}" for index in range(8)])
		self.assertEqual([row.name for row in test], ["A-8", "A-9"])
		self.assertEqual(info["excluded_sparse_messages"], 3)

	def test_evaluation_embedding_variants_keep_full_and_cleaned_body_separate(self) -> None:
		row = SimpleNamespace(
			subject="Neue Abrechnung",
			preview="Kurze Vorschau",
			full_text="Neuer Inhalt\n\nAm 1. Januar schrieb Person:\nAlter zitierter Inhalt",
		)
		self.assertIn("Kurze Vorschau", _embedding_text(row, "subject_preview"))
		self.assertNotIn("Alter zitierter Inhalt", _embedding_text(row, "subject_clean_text"))
		self.assertIn("Alter zitierter Inhalt", _embedding_text(row, "subject_full_text"))

	@patch("thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.evaluation.requests.post")
	def test_evaluation_passes_explicit_context_length_to_ollama(self, post: Mock) -> None:
		response = Mock(ok=True)
		response.json.return_value = {"embeddings": [[3.0, 4.0]]}
		post.return_value = response

		vectors, _seconds, fallback_count = _embed(
			["Nachricht"],
			model="qwen3-embedding:0.6b",
			base_url="http://ollama.example",
			num_ctx=32768,
		)

		self.assertEqual(fallback_count, 0)
		self.assertAlmostEqual(float(vectors[0][0]), 0.6)
		post.assert_called_once_with(
			"http://ollama.example/api/embed",
			json={
				"model": "qwen3-embedding:0.6b",
				"input": ["Nachricht"],
				"truncate": True,
				"options": {"num_ctx": 32768},
			},
			timeout=300,
		)

	@patch("thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.evaluation.requests.post")
	def test_evaluation_bounds_pathological_input_before_ollama(self, post: Mock) -> None:
		response = Mock(ok=True)
		response.json.return_value = {"embeddings": [[1.0, 0.0]]}
		post.return_value = response

		_embed(
			["0123456789"],
			model="embeddinggemma:300m-qat-q8_0",
			base_url="http://ollama.example",
			max_characters=6,
		)

		self.assertEqual(post.call_args.kwargs["json"]["input"], ["012345"])

	def test_sender_gate_uses_only_a_reliable_sender_history(self) -> None:
		train = [
			SimpleNamespace(sender_email="stable@example.test", actual_mailbox_id="A"),
			SimpleNamespace(sender_email="stable@example.test", actual_mailbox_id="A"),
			SimpleNamespace(sender_email="mixed@example.test", actual_mailbox_id="A"),
			SimpleNamespace(sender_email="mixed@example.test", actual_mailbox_id="B"),
		]
		test = [
			SimpleNamespace(sender_email="stable@example.test"),
			SimpleNamespace(sender_email="mixed@example.test"),
		]
		self.assertEqual(
			_sender_gate_rankings([["B", "A"], ["C", "A", "B"]], train, test, min_count=2, min_purity=0.7),
			[["A", "B"], ["C", "A", "B"]],
		)

	def test_participant_addresses_parse_jmap_json_and_fall_back_to_sender(self) -> None:
		row = SimpleNamespace(
			participants='{"from":[{"email":"FROM@EXAMPLE.TEST"}],"to":[{"email":"to@example.test"}],"cc":[]}',
			sender_email="ignored@example.test",
		)
		self.assertEqual(
			_participant_addresses(row),
			{
				"from": ("from@example.test",),
				"to": ("to@example.test",),
				"cc": (),
			},
		)
		fallback = SimpleNamespace(participants="", sender_email="Fallback@Example.test")
		self.assertEqual(_participant_addresses(fallback)["from"], ("fallback@example.test",))
		self.assertEqual(_participant_coverage([row, fallback])["recipient_coverage"], 0.5)

	def test_recipient_history_can_identify_a_folder(self) -> None:
		def row(label: str, recipient: str) -> SimpleNamespace:
			return SimpleNamespace(
				actual_mailbox_id=label,
				participants={"from": [], "to": [{"email": recipient}], "cc": []},
				sender_email="",
			)

		train = [row("A", "a@example.test"), row("A", "a@example.test"), row("B", "b@example.test")]
		self.assertEqual(
			_address_history_rankings(train, [row("?", "a@example.test")], mode="recipient")[0][0],
			"A",
		)

	def test_own_addresses_are_inferred_only_from_broad_two_way_history(self) -> None:
		train = []
		for index in range(50):
			folder = f"F-{index % 10}"
			outbound_external = f"outbound-{index}@example.test"
			inbound_external = f"inbound-{index}@example.test"
			train.append(
				SimpleNamespace(
					actual_mailbox_id=folder,
					participants={
						"from": [{"email": "own@example.test"}],
						"to": [{"email": outbound_external}],
						"cc": [],
					},
					sender_email="own@example.test",
				)
			)
			train.append(
				SimpleNamespace(
					actual_mailbox_id=folder,
					participants={
						"from": [{"email": inbound_external}],
						"to": [{"email": "own@example.test"}],
						"cc": [],
					},
					sender_email=inbound_external,
				)
			)
		own, diagnostics = _infer_own_addresses(train, max_inferred=1)
		self.assertEqual(own, {"own@example.test"})
		self.assertEqual(diagnostics["inferred_own_addresses"], 1)

	def test_error_diagnostics_anonymize_folders_and_split_hierarchies(self) -> None:
		self.assertRegex(_folder_token("private-mailbox-id"), r"^Ordner-[0-9a-f]{8}$")
		self.assertNotIn("private", _folder_token("private-mailbox-id"))
		self.assertEqual(
			_path_parts("Archiv\\Objekt/Vertrag"),
			("archiv", "objekt", "vertrag"),
		)

	def test_error_diagnostics_identify_legacy_system_folders(self) -> None:
		self.assertIsNotNone(SYSTEM_LIKE_FOLDER_RE.search("Archiv/Trash/Entwürfe"))
		self.assertIsNotNone(SYSTEM_LIKE_FOLDER_RE.search("Archiv/Posteingang"))
		self.assertIsNone(SYSTEM_LIKE_FOLDER_RE.search("Archiv/Objekte/Hauptstraße"))

	def test_error_diagnostic_segment_counts_hits_and_misses(self) -> None:
		self.assertEqual(
			_segment(
				[
					{"top1": 1, "top3": 1},
					{"top1": 0, "top3": 1},
					{"top1": 0, "top3": 0},
				]
			),
			{"n": 3, "top1": 0.3333, "top3": 0.6667, "top1_errors": 2, "top3_misses": 1},
		)
