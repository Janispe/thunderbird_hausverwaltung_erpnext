from __future__ import annotations

import frappe
from frappe.model.document import Document


class ThunderbirdDevice(Document):
	def validate(self) -> None:
		self.device_id = (self.device_id or "").strip()
		self.device_name = (self.device_name or "").strip()
		if not self.device_id:
			frappe.throw("Device-ID fehlt.")
		if not self.device_name:
			frappe.throw("Arbeitsplatzname fehlt.")
