frappe.ui.form.on("Mail Filing Source Account", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Verbindung testen"), async () => {
			const result = await frm.call("test_connection");
			frappe.msgprint(
				__("Verbunden mit {0}; {1} Ordner werden überwacht.", [
					result.message.server,
					result.message.watched_folders,
				]),
			);
		}, __("Quellpostfach"));
		frm.add_custom_button(__("Jetzt synchronisieren"), async () => {
			await frm.call("sync_now");
			frappe.show_alert(__("Synchronisierung wurde eingereiht."));
		}, __("Quellpostfach"));
		frm.add_custom_button(__("Mietvertragstags neu aufbauen"), async () => {
			await frm.call("rebuild_tags");
			frappe.show_alert(__("Die Schlagwort-Synchronisierung wurde eingereiht."));
		}, __("Quellpostfach"));
		frm.add_custom_button(__("Cursor zurücksetzen"), async () => {
			await frm.call("reset_sync");
			frappe.show_alert(__("Vollständige Synchronisierung wurde eingereiht."));
		}, __("Quellpostfach"));
	},
});
