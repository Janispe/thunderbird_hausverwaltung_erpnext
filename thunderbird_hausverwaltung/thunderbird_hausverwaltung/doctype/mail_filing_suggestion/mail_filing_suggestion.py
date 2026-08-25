import frappe
from frappe import _
from frappe.model.document import Document


class MailFilingSuggestion(Document):
	def validate(self) -> None:
		if bool(self.archive_message) == bool(self.source_message):
			frappe.throw(_("Ein Ablagevorschlag muss genau eine Archiv- oder Quellnachricht referenzieren."))
