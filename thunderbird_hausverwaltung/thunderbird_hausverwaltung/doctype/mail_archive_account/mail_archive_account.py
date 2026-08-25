from __future__ import annotations

from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import validate_email_address


def normalize_account_addresses(value: str | None) -> list[str]:
	addresses: list[str] = []
	seen: set[str] = set()
	for item in str(value or "").replace(";", "\n").replace(",", "\n").splitlines():
		address = item.strip().casefold()
		if not address or address in seen:
			continue
		validate_email_address(address, throw=True)
		addresses.append(address)
		seen.add(address)
	return addresses


def _validate_http_url(value: str, label: str) -> None:
	parsed = urlparse(value)
	if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
		frappe.throw(_("{0} muss eine gültige HTTP(S)-URL ohne Zugangsdaten sein.").format(label))


class MailArchiveAccount(Document):
	def validate(self) -> None:
		self.account_name = str(self.account_name or "").strip()
		self.server_url = str(self.server_url or "").strip().rstrip("/")
		self.username = str(self.username or "").strip()
		self.provider_account_id = str(self.provider_account_id or "").strip()
		addresses = normalize_account_addresses(self.email_addresses)
		self.email_addresses = "\n".join(addresses)
		for other in frappe.get_all(
			"Mail Archive Account",
			filters={"name": ["!=", self.name or ""]},
			fields=["name", "email_addresses"],
		):
			overlap = set(addresses).intersection(normalize_account_addresses(other.email_addresses))
			if overlap:
				frappe.throw(
					_("Die E-Mail-Adresse {0} ist bereits dem Mail-Archiv-Konto {1} zugeordnet.").format(
						frappe.bold(sorted(overlap)[0]), frappe.bold(other.name)
					)
				)
		_validate_http_url(self.server_url, _("Server-URL"))
		if self.embedding_enabled:
			self.embedding_base_url = str(self.embedding_base_url or "").strip().rstrip("/")
			self.embedding_model = str(self.embedding_model or "").strip()
			if not self.embedding_base_url or not self.embedding_model:
				frappe.throw(_("Für Embeddings werden Endpunkt und Modell benötigt."))
			_validate_http_url(self.embedding_base_url, _("Embedding-Endpunkt"))
		self.request_timeout = max(int(self.request_timeout or 30), 5)
		self.embedding_timeout = max(int(self.embedding_timeout or 60), 5)
		self.max_messages_per_run = max(int(self.max_messages_per_run or 2000), 100)

	def on_update(self) -> None:
		previous = self.get_doc_before_save()
		if not previous:
			return
		index_inputs = (
			"provider",
			"server_url",
			"username",
			"provider_account_id",
			"archive_root_mailbox_id",
			"embedding_enabled",
			"embedding_provider",
			"embedding_base_url",
			"embedding_model",
		)
		if any(self.has_value_changed(fieldname) for fieldname in index_inputs):
			self.db_set(
				{
					"initial_sync_completed": 0,
					"sync_cursor": "",
					"last_email_state": "",
					"sync_status": "Noch nicht synchronisiert",
					"last_error": "",
				},
				update_modified=False,
			)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		self.check_permission("write")
		from ...mail_archive.providers import get_provider

		return get_provider(self).test_connection()

	@frappe.whitelist()
	def sync_now(self) -> dict:
		self.check_permission("write")
		from ...mail_archive.sync import enqueue_account_sync

		return enqueue_account_sync(self.name)

	@frappe.whitelist()
	def rebuild_folder_centroids(self) -> dict:
		self.check_permission("write")
		from ...mail_archive.classifier import rebuild_folder_centroids

		return rebuild_folder_centroids(self.name)

	@frappe.whitelist()
	def reindex_all(self) -> dict:
		self.check_permission("write")
		self.db_set(
			{
				"initial_sync_completed": 0,
				"sync_cursor": "",
				"last_email_state": "",
				"sync_status": "Noch nicht synchronisiert",
				"last_error": "",
			},
			update_modified=False,
		)
		from ...mail_archive.sync import enqueue_account_sync

		return enqueue_account_sync(self.name)

	@frappe.whitelist()
	def rebuild_tags(self) -> dict:
		self.check_permission("write")
		from ...mail_archive.tagging import reset_tag_sync
		from ...mail_archive.sync import enqueue_account_sync

		reset_tag_sync(self.name)
		return enqueue_account_sync(self.name)
