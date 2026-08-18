frappe.ui.form.on("Mietvertrag", {
	refresh(frm) {
		if (frm.is_new()) return;

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
						"thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.get_mietvertrag_search_context",
					args: { mietvertrag: frm.doc.name },
					freeze: true,
					freeze_message: __("E-Mail-Adressen werden geladen …"),
				});
				const context = response.message || {};
				await window.hv_thunderbird.show_messages({
					email_addresses: context.email_addresses || [],
					title: context.title || __("E-Mails zum Mietvertrag"),
					mode: "any",
				});
			},
			__("Thunderbird")
		);

		frm.add_custom_button(
			__("E-Mail verfassen"),
			async () => {
				if (!window.hv_thunderbird?.compose_message) {
					frappe.throw(
						__("Die Thunderbird-Brücke ist nicht geladen. Bitte die Seite neu laden.")
					);
				}

				const response = await frappe.call({
					method:
						"thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.get_mietvertrag_compose_context",
					args: { mietvertrag: frm.doc.name },
					freeze: true,
					freeze_message: __("E-Mail-Adressen werden geladen …"),
				});
				const context = response.message || {};
				await window.hv_thunderbird.compose_message({
					to: context.to || [],
					subject: context.subject || "",
				});
			},
			__("Thunderbird")
		);
	},
});
