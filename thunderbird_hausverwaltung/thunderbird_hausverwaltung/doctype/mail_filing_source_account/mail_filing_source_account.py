from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document

from ..mail_archive_account.mail_archive_account import normalize_account_addresses


HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")


def normalize_watched_folders(value: str | None) -> list[str]:
	folders: list[str] = []
	seen: set[str] = set()
	for item in str(value or "").replace(";", "\n").splitlines():
		folder = item.strip()
		key = folder.casefold()
		if not folder or key in seen:
			continue
		folders.append(folder)
		seen.add(key)
	return folders


class MailFilingSourceAccount(Document):
	def validate(self) -> None:
		self.account_name = str(self.account_name or "").strip()
		self.server_host = str(self.server_host or "").strip().rstrip(".")
		self.username = str(self.username or "").strip()
		if not HOST_RE.fullmatch(self.server_host) or ".." in self.server_host:
			frappe.throw(_("IMAP-Server muss ein gültiger Hostname ohne Protokoll sein."))
		self.server_port = int(self.server_port or (993 if self.connection_security == "SSL/TLS" else 143))
		if not 1 <= self.server_port <= 65535:
			frappe.throw(_("Der IMAP-Port muss zwischen 1 und 65535 liegen."))
		addresses = normalize_account_addresses(self.email_addresses)
		self.email_addresses = "\n".join(addresses)
		folders = normalize_watched_folders(self.watched_folders)
		if not folders:
			frappe.throw(_("Mindestens ein IMAP-Ordner muss überwacht werden."))
		self.watched_folders = "\n".join(folders)
		self.max_messages_per_run = max(min(int(self.max_messages_per_run or 500), 5000), 1)
		for other in frappe.get_all(
			"Mail Filing Source Account",
			filters={"name": ["!=", self.name or ""]},
			fields=["name", "email_addresses"],
		):
			overlap = set(addresses).intersection(normalize_account_addresses(other.email_addresses))
			if overlap:
				frappe.throw(
					_("Die Quelladresse {0} ist bereits dem Postfach {1} zugeordnet.").format(
						frappe.bold(sorted(overlap)[0]), frappe.bold(other.name)
					)
				)

	def on_update(self) -> None:
		previous = self.get_doc_before_save()
		if previous and any(
			self.has_value_changed(fieldname)
			for fieldname in (
				"server_host",
				"server_port",
				"connection_security",
				"username",
				"watched_folders",
			)
		):
			self.db_set(
				{
					"sync_cursor": "",
					"tag_sync_completed": 0,
					"tag_sync_cursor": "",
					"sync_status": "Noch nicht synchronisiert",
					"last_error": "",
				},
				update_modified=False,
			)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		self.check_permission("write")
		from ...mail_archive.source_sync import IMAPSourceClient

		with IMAPSourceClient.from_account(self) as client:
			return client.test_connection()

	@frappe.whitelist()
	def sync_now(self) -> dict:
		self.check_permission("write")
		from ...mail_archive.source_sync import enqueue_source_account_sync

		return enqueue_source_account_sync(self.name)

	@frappe.whitelist()
	def reset_sync(self) -> dict:
		self.check_permission("write")
		self.db_set(
			{
				"sync_cursor": "",
				"tag_sync_completed": 0,
				"tag_sync_cursor": "",
				"sync_status": "Noch nicht synchronisiert",
				"last_error": "",
			},
			update_modified=False,
		)
		from ...mail_archive.source_sync import enqueue_source_account_sync

		return enqueue_source_account_sync(self.name)

	@frappe.whitelist()
	def rebuild_tags(self) -> dict:
		self.check_permission("write")
		self.db_set({"tag_sync_completed": 0, "tag_sync_cursor": ""}, update_modified=False)
		from ...mail_archive.source_sync import enqueue_source_account_sync

		return enqueue_source_account_sync(self.name)
