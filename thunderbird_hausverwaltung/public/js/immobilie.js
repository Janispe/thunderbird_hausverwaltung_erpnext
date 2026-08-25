frappe.ui.form.on("Immobilie", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		frm.add_custom_button(
			__("Archivprobleme prüfen"),
			async () => {
				await frappe.call({
					method: "hausverwaltung.hausverwaltung.doctype.hausverwaltung_problem.hausverwaltung_problem.run_problem_checks",
					freeze: true,
					freeze_message: __("Archivordner werden geprüft …"),
				});
				frappe.set_route("List", "Hausverwaltung Problem", {
					bezug_doctype: "Immobilie",
					bezug_name: frm.doc.name,
					status: ["in", ["Offen", "In Bearbeitung"]],
				});
			},
			__("E-Mail-Archiv"),
		);
	},
});
