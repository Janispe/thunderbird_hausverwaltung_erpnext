frappe.ui.form.on("Mail Archive Account", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Verbindung testen"), async () => {
			const result = await frm.call("test_connection");
			frappe.msgprint(
				__("Verbunden mit {0}; {1} Ordner gefunden.", [
					result.message.account_name,
					result.message.mailboxes,
				]),
			);
		}, __("Mail-Archiv"));
		frm.add_custom_button(__("Jetzt synchronisieren"), async () => {
			await frm.call("sync_now");
			frappe.show_alert(__("Synchronisierung wurde eingereiht."));
		}, __("Mail-Archiv"));
		frm.add_custom_button(__("Ordner-Zentroide neu aufbauen"), async () => {
			const result = await frm.call("rebuild_folder_centroids");
			frappe.show_alert(__("{0} Ordner wurden aktualisiert.", [result.message.updated]));
		}, __("Mail-Archiv"));
		frm.add_custom_button(__("Schlagwörter neu aufbauen"), async () => {
			await frm.call("rebuild_tags");
			frappe.show_alert(__("Die Schlagwort-Synchronisierung wurde eingereiht."));
		}, __("Mail-Archiv"));
		frm.add_custom_button(__("Vollständig neu indexieren"), async () => {
			await frm.call("reindex_all");
			frappe.show_alert(__("Vollständige Neuindexierung wurde eingereiht."));
		}, __("Mail-Archiv"));
	},
});
