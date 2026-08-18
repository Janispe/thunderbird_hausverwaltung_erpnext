frappe.ui.form.on("Wohnung", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(
			__("Schriftverkehr"),
			() => window.hv_thunderbird.open_timeline("Wohnung", frm.doc.name),
			__("Thunderbird")
		);

		frm.add_custom_button(
			__("E-Mails anzeigen"),
			async () => {
				if (!window.hv_thunderbird?.show_messages) {
					frappe.throw(
						__("Die Thunderbird-Brücke ist nicht geladen. Bitte die Seite neu laden.")
					);
				}

				const response = await frappe.call({
					method:
						"thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.get_wohnung_search_context",
					args: { wohnung: frm.doc.name },
					freeze: true,
					freeze_message: __("Mietverträge und E-Mail-Adressen werden geladen …"),
				});
				const context = response.message || {};
				await window.hv_thunderbird.show_messages({
					email_addresses: context.email_addresses || [],
					title: context.title || __("E-Mails zur Wohnung"),
					mode: "any",
				});
			},
			__("Thunderbird")
		);
	},
});
