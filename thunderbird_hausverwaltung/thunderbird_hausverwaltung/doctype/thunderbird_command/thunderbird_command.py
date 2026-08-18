from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

ALLOWED_COMMANDS = {"show_messages", "compose_message", "sync_messages", "open_message"}


class ThunderbirdCommand(Document):
	def validate(self) -> None:
		if self.status not in {"Queued", "Delivered", "Completed", "Failed", "Expired"}:
			frappe.throw("Ungültiger Thunderbird-Befehlsstatus.")
		try:
			payload = json.loads(self.payload or "{}")
		except (TypeError, ValueError):
			frappe.throw("Der Thunderbird-Befehl enthält kein gültiges JSON.")
		if payload.get("command") not in ALLOWED_COMMANDS:
			frappe.throw("Nicht unterstützter Thunderbird-Befehl.")
