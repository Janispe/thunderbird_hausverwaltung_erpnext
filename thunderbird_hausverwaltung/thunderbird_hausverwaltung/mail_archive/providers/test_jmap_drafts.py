from __future__ import annotations

from unittest import TestCase
from unittest.mock import Mock

import requests

from .base import ArchiveMailbox, DraftNotCreatedError, MailArchiveProvider
from .jmap import (
	CORE_CAPABILITY,
	DRAFT_HEADER_NAME,
	DRAFT_HEADER_PROPERTY,
	MAIL_CAPABILITY,
	JMAPConfig,
	JMAPError,
	JMAPProvider,
)


class TestJMAPDrafts(TestCase):
	def setUp(self) -> None:
		self.provider = JMAPProvider(JMAPConfig("https://mail.example", "user", "secret", account_id="A1"))
		self.provider._session = {
			"capabilities": {CORE_CAPABILITY: {}, MAIL_CAPABILITY: {}},
			"apiUrl": "https://mail.example/jmap",
			"accounts": {"A1": {"isReadOnly": False, "accountCapabilities": {MAIL_CAPABILITY: {}}}},
		}
		self.provider.list_mailboxes = Mock(
			return_value=[
				ArchiveMailbox(
					id="D1",
					name="Entwürfe",
					role="drafts",
					my_rights={"mayReadItems": True, "mayAddItems": True, "maySetKeywords": True},
				)
			]
		)
		self.arguments = {
			"mailbox_id": "D1",
			"sender": "verwaltung@example.test",
			"recipients": ["mieter@example.test"],
			"subject": "Re: Reparatur",
			"text_body": "Guten Tag,\nwir haben Ihren Hinweis erhalten.\n",
			"draft_token": "hv-draft-123",
			"rfc_message_id": "<hv-draft-123@example.test>",
		}

	def test_create_payload_is_an_unsent_plaintext_reply_with_recovery_header(self) -> None:
		self.provider.http.post = Mock(
			return_value=Mock(
				ok=True,
				json=Mock(
					return_value={
						"methodResponses": [["Email/set", {"created": {"draft": {"id": "E1"}}}, "0"]]
					}
				),
			)
		)
		message_id = self.provider.create_draft(
			**self.arguments,
			cc=["beirat@example.test"],
			in_reply_to=("<original@example.test>",),
			references=("older@example.test", "<original@example.test>"),
		)
		self.assertEqual(message_id, "E1")
		self.provider.http.post.assert_called_once_with(
			"https://mail.example/jmap",
			json={
				"using": [CORE_CAPABILITY, MAIL_CAPABILITY],
				"methodCalls": [
					[
						"Email/set",
						{
							"accountId": "A1",
							"create": {
								"draft": {
									"mailboxIds": {"D1": True},
									"keywords": {"$draft": True},
									"from": [{"email": "verwaltung@example.test"}],
									"to": [{"email": "mieter@example.test"}],
									"cc": [{"email": "beirat@example.test"}],
									"subject": "Re: Reparatur",
									"messageId": ["hv-draft-123@example.test"],
									"inReplyTo": ["original@example.test"],
									"references": ["older@example.test", "original@example.test"],
									DRAFT_HEADER_PROPERTY: "hv-draft-123",
									"textBody": [{"partId": "body", "type": "text/plain"}],
									"bodyValues": {"body": {"value": self.arguments["text_body"]}},
								},
							},
						},
						"0",
					]
				],
			},
			timeout=30,
		)

	def test_new_draft_omits_optional_reply_headers(self) -> None:
		self.provider._single = Mock(return_value={"created": {"draft": {"id": "E1"}}})
		self.provider.create_draft(**self.arguments)
		message = self.provider._single.call_args.args[1]["create"]["draft"]
		for key in ("cc", "inReplyTo", "references", "headers", "bodyStructure"):
			self.assertNotIn(key, message)

	def test_account_readonly_prevents_creation(self) -> None:
		self.provider._session["accounts"]["A1"]["isReadOnly"] = True
		self.provider._single = Mock()
		with self.assertRaisesRegex(DraftNotCreatedError, "schreibgeschützt"):
			self.provider.create_draft(**self.arguments)
		self.provider._single.assert_not_called()

	def test_account_without_mail_capability_prevents_creation(self) -> None:
		self.provider._session["accounts"]["A1"]["accountCapabilities"] = {}
		self.provider._single = Mock()
		with self.assertRaisesRegex(DraftNotCreatedError, "keine Mail-Funktionen"):
			self.provider.create_draft(**self.arguments)
		self.provider._single.assert_not_called()

	def test_missing_or_wrong_mailbox_role_prevents_creation(self) -> None:
		for mailboxes in ([], [ArchiveMailbox(id="D1", name="Gesendet", role="sent")]):
			with self.subTest(mailboxes=mailboxes):
				self.provider.list_mailboxes.return_value = mailboxes
				self.provider._single = Mock()
				with self.assertRaisesRegex(DraftNotCreatedError, "kein verfügbarer JMAP-Entwurfsordner"):
					self.provider.create_draft(**self.arguments)
				self.provider._single.assert_not_called()

	def test_missing_advertised_mailbox_right_prevents_creation(self) -> None:
		for right in ("mayReadItems", "mayAddItems", "maySetKeywords"):
			with self.subTest(right=right):
				rights = {"mayReadItems": True, "mayAddItems": True, "maySetKeywords": True}
				rights[right] = False
				self.provider.list_mailboxes.return_value = [
					ArchiveMailbox(id="D1", name="Entwürfe", role="drafts", my_rights=rights)
				]
				self.provider._single = Mock()
				with self.assertRaisesRegex(DraftNotCreatedError, "fehlen die erforderlichen"):
					self.provider.create_draft(**self.arguments)
				self.provider._single.assert_not_called()

	def test_older_provider_responses_without_rights_remain_usable(self) -> None:
		self.provider._session["accounts"]["A1"] = {"name": "Archiv"}
		self.provider.list_mailboxes.return_value = [ArchiveMailbox(id="D1", name="Entwürfe", role="drafts")]
		self.provider._single = Mock(return_value={"created": {"draft": {"id": "E1"}}})
		self.assertEqual(self.provider.create_draft(**self.arguments), "E1")

	def test_server_creation_rejection_is_definitive_and_sanitized_without_retry(self) -> None:
		for error_type, expected in (
			("overQuota", "Speicherlimit"),
			("forbidden", "fehlender Rechte"),
			("unknownServerError", "abgelehnt"),
		):
			with self.subTest(error_type=error_type):
				self.provider._single = Mock(
					return_value={
						"notCreated": {
							"draft": {"type": error_type, "description": "SECRET https://private.example"}
						}
					}
				)
				with self.assertRaisesRegex(DraftNotCreatedError, expected) as caught:
					self.provider.create_draft(**self.arguments)
				self.assertNotIn("SECRET", str(caught.exception))
				self.assertNotIn("private.example", str(caught.exception))
				self.provider._single.assert_called_once()

	def test_missing_created_message_id_is_an_error(self) -> None:
		self.provider._single = Mock(return_value={"created": {"draft": {}}})
		with self.assertRaisesRegex(JMAPError, "keine Message-ID") as caught:
			self.provider.create_draft(**self.arguments)
		self.assertNotIsInstance(caught.exception, DraftNotCreatedError)

	def test_invalid_recipient_token_and_message_id_are_rejected_before_network_access(self) -> None:
		for change in (
			{"recipients": []},
			{"sender": "verwaltung@example.test\r\nBcc: fremd@example.test"},
			{"draft_token": "hv-token\ninvalid"},
			{"rfc_message_id": "<invalid message@example.test>"},
		):
			with self.subTest(change=change):
				self.provider._single = Mock()
				with self.assertRaises(DraftNotCreatedError):
					self.provider.create_draft(**(self.arguments | change))
				self.provider._single.assert_not_called()

	def test_failed_preflight_read_is_definitive_without_exposing_remote_details(self) -> None:
		self.provider.list_mailboxes.side_effect = JMAPError("SECRET: untrusted remote description")
		self.provider._single = Mock()
		with self.assertRaisesRegex(DraftNotCreatedError, "vor der Entwurfserstellung") as caught:
			self.provider.create_draft(**self.arguments)
		self.assertNotIn("SECRET", str(caught.exception))
		self.provider._single.assert_not_called()

	def test_ambiguous_or_malformed_set_rejection_never_proves_no_creation(self) -> None:
		responses = (
			{"notCreated": {"draft": {"type": "alreadyExists", "existingId": "E1"}}},
			{"notCreated": {"draft": {"type": "AlreadyExists"}}},
			{"notCreated": {"draft": {"type": "overQuota "}}},
			{
				"created": {"draft": {"id": "E1"}},
				"notCreated": {"draft": {"type": "overQuota"}},
			},
			{"created": {"other": {"id": "E1"}}, "notCreated": {"draft": {"type": "overQuota"}}},
			{"notCreated": {"draft": {"description": "overQuota"}}},
			{"notCreated": {"draft": {"type": 1}}},
			{"notCreated": {"draft": None}},
			{"notCreated": {"draft": {"type": "overQuota"}, "other": {"type": "overQuota"}}},
			{"notCreated": []},
			{"created": [], "notCreated": {"draft": {"type": "overQuota"}}},
			{"accountId": "OTHER", "notCreated": {"draft": {"type": "overQuota"}}},
			{"created": {"draft": {"id": 1}}},
			None,
		)
		for response in responses:
			with self.subTest(response=response):
				self.provider._single = Mock(return_value=response)
				with self.assertRaises(JMAPError) as caught:
					self.provider.create_draft(**self.arguments)
				self.assertNotIsInstance(caught.exception, DraftNotCreatedError)
				self.provider._single.assert_called_once()

	def test_write_transport_http_and_json_errors_remain_uncertain(self) -> None:
		for response_or_exception in (
			requests.Timeout("SECRET timeout"),
			requests.ConnectionError("SECRET connection"),
			Mock(ok=False, status_code=403),
			Mock(ok=False, status_code=503),
			Mock(ok=True, json=Mock(side_effect=ValueError("SECRET JSON"))),
			Mock(ok=True, json=Mock(return_value={"methodResponses": []})),
		):
			with self.subTest(response_or_exception=response_or_exception):
				self.provider.http.post = Mock()
				if isinstance(response_or_exception, Exception):
					self.provider.http.post.side_effect = response_or_exception
				else:
					self.provider.http.post.return_value = response_or_exception
				with self.assertRaises(JMAPError) as caught:
					self.provider.create_draft(**self.arguments)
				self.assertNotIsInstance(caught.exception, DraftNotCreatedError)
				self.provider.http.post.assert_called_once()
				methods = self.provider.http.post.call_args.kwargs["json"]["methodCalls"]
				self.assertEqual([method[0] for method in methods], ["Email/set"])

	def test_uncorrelated_not_created_response_is_not_a_definitive_rejection(self) -> None:
		rejection = {"notCreated": {"draft": {"type": "overQuota"}}}
		for responses in (
			[["Email/get", rejection, "0"]],
			[["Email/set", rejection, "OTHER"]],
			[["Email/set", rejection, "0"], ["Email/set", rejection, "0"]],
		):
			with self.subTest(responses=responses):
				self.provider.http.post = Mock(
					return_value=Mock(ok=True, json=Mock(return_value={"methodResponses": responses}))
				)
				with self.assertRaises(JMAPError) as caught:
					self.provider.create_draft(**self.arguments)
				self.assertNotIsInstance(caught.exception, DraftNotCreatedError)
				self.provider.http.post.assert_called_once()

	def test_recovery_query_includes_sent_messages_and_exposes_exact_token(self) -> None:
		self.provider._single = Mock(
			side_effect=[
				{"ids": ["E1", "E2"], "total": 2},
				{
					"state": "S1",
					"list": [
						{
							"id": "E1",
							"mailboxIds": {"D1": True},
							"keywords": {"$draft": True},
							DRAFT_HEADER_PROPERTY: "hv-draft-123",
						},
						{"id": "E2", "mailboxIds": {"sent": True}, DRAFT_HEADER_PROPERTY: "hv-draft-123"},
					],
				},
			]
		)
		messages = self.provider.find_draft_messages("hv-draft-123")
		self.assertEqual([message.id for message in messages], ["E1", "E2"])
		self.assertEqual(messages[1].raw[DRAFT_HEADER_PROPERTY], "hv-draft-123")
		self.assertEqual(
			self.provider._single.call_args_list[0].args,
			(
				"Email/query",
				{
					"accountId": "A1",
					"filter": {"header": [DRAFT_HEADER_NAME, "hv-draft-123"]},
					"sort": [{"property": "receivedAt", "isAscending": False}],
					"limit": 3,
					"calculateTotal": True,
				},
			),
		)
		self.assertIn(DRAFT_HEADER_PROPERTY, self.provider._single.call_args_list[1].args[1]["properties"])
		self.assertTrue(
			all(call.args[0] in {"Email/query", "Email/get"} for call in self.provider._single.call_args_list)
		)

	def test_recovery_rejects_more_than_two_matches_before_fetching(self) -> None:
		for result in ({"ids": ["E1", "E2", "E3"]}, {"ids": ["E1"], "total": 8}):
			with self.subTest(result=result):
				self.provider._single = Mock(return_value=result)
				with self.assertRaisesRegex(JMAPError, "mehr als zwei Treffer"):
					self.provider.find_draft_messages("hv-draft-123")
				self.provider._single.assert_called_once()

	def test_recovery_verifies_complete_header_token_instead_of_substring(self) -> None:
		self.provider._single = Mock(
			side_effect=[
				{"ids": ["E1"], "total": 1},
				{"list": [{"id": "E1", DRAFT_HEADER_PROPERTY: "hv-draft-123-other"}]},
			]
		)
		self.assertEqual(self.provider.find_draft_messages("hv-draft-123"), [])

	def test_recovery_fails_when_query_result_cannot_be_fetched(self) -> None:
		self.provider._single = Mock(side_effect=[{"ids": ["E1"]}, {"list": [], "notFound": ["E1"]}])
		with self.assertRaisesRegex(JMAPError, "nicht alle gefundenen Nachrichten"):
			self.provider.find_draft_messages("hv-draft-123")

	def test_recovery_fails_when_server_omits_the_custom_header(self) -> None:
		self.provider._single = Mock(side_effect=[{"ids": ["E1"]}, {"list": [{"id": "E1"}]}])
		with self.assertRaisesRegex(JMAPError, "keine prüfbare Entwurfskennung"):
			self.provider.find_draft_messages("hv-draft-123")

	def test_mailbox_listing_preserves_server_rights(self) -> None:
		self.provider.list_mailboxes = JMAPProvider.list_mailboxes.__get__(self.provider)
		rights = {"mayAddItems": False}
		self.provider._single = Mock(
			side_effect=[
				{"ids": ["D1"], "total": 1},
				{"list": [{"id": "D1", "role": "drafts", "myRights": rights}]},
			]
		)
		self.assertEqual(self.provider.list_mailboxes()[0].my_rights, rights)
		self.assertIn("myRights", self.provider._single.call_args.args[1]["properties"])

	def test_html_only_mail_preserves_body_metadata_and_extracts_visible_plaintext(self) -> None:
		self.provider._single = Mock(
			return_value={
				"list": [
					{
						"id": "E1",
						"replyTo": [{"email": "mieter@example.test"}],
						"sentAt": "2026-10-08T09:00:00Z",
						"textBody": [{"partId": "html", "type": "text/html"}],
						"htmlBody": [{"partId": "html", "type": "text/html"}],
						"bodyStructure": {"partId": "html", "type": "text/html"},
						"bodyValues": {"html": {"value": "<p>Hinweis zur Reparatur</p>"}},
					}
				]
			}
		)
		messages, _state = self.provider.get_messages(["E1"])
		self.assertEqual(messages[0].text_body, "Hinweis zur Reparatur")
		self.assertEqual(messages[0].raw["body_text_source"], "html")
		self.assertEqual(messages[0].raw["replyTo"], [{"email": "mieter@example.test"}])
		self.assertEqual(messages[0].raw["sentAt"], "2026-10-08T09:00:00Z")
		self.assertEqual(messages[0].raw["htmlBody"][0]["type"], "text/html")
		properties = self.provider._single.call_args.args[1]["properties"]
		for property_name in ("replyTo", "sentAt", "htmlBody", "bodyStructure"):
			self.assertIn(property_name, properties)

	def test_html_fallback_preserves_quotes_and_line_breaks_and_suppresses_hidden_code(self) -> None:
		message = self.provider._to_message(
			{
				"id": "E1",
				"htmlBody": [{"partId": "html", "type": "text/html"}],
				"bodyValues": {
					"html": {
						"value": "<head><title>HIDDEN TITLE</title><style>HIDDEN STYLE</style></head>"
						"<div>Guten Tag,<br>die &quot;Heizung&quot; ist defekt.</div><blockquote>Alter Text &amp; Antwort</blockquote>"
						"<script>HIDDEN SCRIPT</script><p>Vielen Dank!</p>"
					}
				},
			}
		)
		self.assertEqual(
			message.text_body, 'Guten Tag,\ndie "Heizung" ist defekt.\nAlter Text & Antwort\nVielen Dank!'
		)
		self.assertNotIn("HIDDEN", message.text_body)

	def test_plaintext_is_preferred_over_html_alternative(self) -> None:
		message = self.provider._to_message(
			{
				"id": "E1",
				"textBody": [{"partId": "plain", "type": "text/plain"}],
				"htmlBody": [{"partId": "html", "type": "text/html"}],
				"bodyValues": {"plain": {"value": "Plain version"}, "html": {"value": "<p>HTML version</p>"}},
			}
		)
		self.assertEqual(message.text_body, "Plain version")
		self.assertEqual(message.raw["body_text_source"], "plain")

	def test_new_optional_operations_do_not_make_legacy_providers_abstract(self) -> None:
		legacy = type(
			"LegacyProvider",
			(MailArchiveProvider,),
			{name: lambda *args, **kwargs: None for name in MailArchiveProvider.__abstractmethods__},
		)()
		with self.assertRaises(NotImplementedError):
			legacy.create_draft(**self.arguments)
		with self.assertRaises(NotImplementedError):
			legacy.find_draft_messages("hv-draft-123")


class TestJMAPDraftAttachments(TestCase):
	def setUp(self):
		TestJMAPDrafts.setUp(self)
		self.provider._session["uploadUrl"] = "https://mail.example/upload/{accountId}"
		self.attachments = [
			{"filename": "Abrechnung.pdf", "content_type": "application/pdf", "content": b"%PDF-1.7\x00\xff"}
		]
		self.provider._single = Mock(return_value={"created": {"draft": {"id": "E1"}}})
		self.provider.http.post = Mock(
			return_value=Mock(
				status_code=201,
				json=Mock(
					return_value={
						"accountId": "A1",
						"blobId": "B1",
						"size": len(self.attachments[0]["content"]),
						"type": "application/pdf",
					}
				),
			)
		)

	def test_blob_upload_precedes_unsent_creation_and_keeps_binary(self):
		self.provider.http.post.side_effect = lambda *a, **k: self._before_creation()
		self.assertEqual(self.provider.create_draft(**self.arguments, attachments=self.attachments), "E1")
		self.provider.http.post.assert_called_once_with(
			"https://mail.example/upload/A1",
			data=self.attachments[0]["content"],
			headers={"Content-Type": "application/pdf", "Accept": "application/json"},
			timeout=30,
			allow_redirects=False,
		)
		method, args = self.provider._single.call_args.args
		self.assertEqual(method, "Email/set")
		message = args["create"]["draft"]
		self.assertEqual(
			message["attachments"],
			[
				{
					"blobId": "B1",
					"name": "Abrechnung.pdf",
					"type": "application/pdf",
					"disposition": "attachment",
				}
			],
		)
		self.assertEqual(message["keywords"], {"$draft": True})
		self.assertIn("textBody", message)
		self.assertNotIn("bodyStructure", message)

	def _before_creation(self):
		self.provider._single.assert_not_called()
		return Mock(
			status_code=201,
			json=Mock(
				return_value={
					"accountId": "A1",
					"blobId": "B1",
					"size": len(self.attachments[0]["content"]),
					"type": "application/pdf",
				}
			),
		)

	def test_upload_failures_prove_no_email_created(self):
		for response in [
			Mock(status_code=403),
			Mock(status_code=302),
			Mock(status_code=201, json=Mock(side_effect=ValueError())),
			Mock(
				status_code=201,
				json=Mock(
					return_value={"accountId": "other", "blobId": "B1", "size": 10, "type": "application/pdf"}
				),
			),
		]:
			with self.subTest(response=response):
				self.provider.http.post.return_value = response
				with self.assertRaises(DraftNotCreatedError):
					self.provider.create_draft(**self.arguments, attachments=self.attachments)
				self.provider._single.assert_not_called()
		self.provider.http.post.side_effect = requests.Timeout("sensitive URL")
		with self.assertRaises(DraftNotCreatedError) as raised:
			self.provider.create_draft(**self.arguments, attachments=self.attachments)
		self.assertNotIn("sensitive", str(raised.exception))
		self.provider._single.assert_not_called()

	def test_second_upload_failure_never_creates_partial_draft(self):
		self.provider.http.post.side_effect = [self._before_creation(), requests.ConnectionError()]
		with self.assertRaises(DraftNotCreatedError):
			self.provider.create_draft(**self.arguments, attachments=self.attachments * 2)
		self.provider._single.assert_not_called()

	def test_server_limits_and_bad_metadata_prevent_upload(self):
		self.provider._session["capabilities"][CORE_CAPABILITY]["maxSizeUpload"] = 1
		with self.assertRaises(DraftNotCreatedError):
			self.provider.create_draft(**self.arguments, attachments=self.attachments)
		self.provider.http.post.assert_not_called()
		del self.provider._session["capabilities"][CORE_CAPABILITY]["maxSizeUpload"]
		for values in [
			self.attachments * 11,
			[self.attachments[0] | {"filename": "../a"}],
			[self.attachments[0] | {"content": "decoded"}],
			{},
		]:
			with self.assertRaises(DraftNotCreatedError):
				self.provider.create_draft(**self.arguments, attachments=values)
		self.provider.http.post.assert_not_called()
		self.provider._single.assert_not_called()

	def test_foreign_upload_host_and_missing_template_prevent_upload(self):
		for url in [
			None,
			"https://mail.example/upload",
			"https://other.example/upload/{accountId}",
			"https://mail.example/upload/{accountId}/{unknown}",
		]:
			self.provider._session["uploadUrl"] = url
			with self.assertRaises(DraftNotCreatedError):
				self.provider.create_draft(**self.arguments, attachments=self.attachments)
		self.provider.http.post.assert_not_called()
		self.provider._single.assert_not_called()

	def test_empty_attachments_keep_original_creation_path(self):
		self.provider.create_draft(**self.arguments, attachments=[])
		self.provider.http.post.assert_not_called()
		self.assertNotIn("attachments", self.provider._single.call_args.args[1]["create"]["draft"])
