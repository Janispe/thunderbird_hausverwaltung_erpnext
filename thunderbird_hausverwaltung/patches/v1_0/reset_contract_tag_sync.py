import frappe


def execute() -> None:
	if frappe.db.table_exists("Mail Archive Account"):
		frappe.db.sql(
			"""
			UPDATE `tabMail Archive Account`
			SET tag_sync_completed = 0, tag_sync_cursor = ''
			"""
		)
	if frappe.db.table_exists("Mail Filing Source Account") and frappe.db.has_column(
		"Mail Filing Source Account", "tag_sync_completed"
	):
		frappe.db.sql(
			"""
			UPDATE `tabMail Filing Source Account`
			SET tag_sync_completed = 0, tag_sync_cursor = ''
			"""
		)
