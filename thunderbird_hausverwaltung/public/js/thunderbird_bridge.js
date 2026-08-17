(() => {
	"use strict";

	const method = "thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge";

	async function show_messages(options = {}) {
		const email_addresses = Array.isArray(options.email_addresses) ? options.email_addresses : [];
		const tags = Array.isArray(options.tags) ? options.tags : [];
		if (!email_addresses.length && !tags.length) {
			frappe.throw(__("Mindestens eine E-Mail-Adresse oder ein Thunderbird-Tag ist erforderlich."));
		}
		const response = await frappe.call({
			method: `${method}.enqueue_search`,
			args: {
				email_addresses,
				tags,
				mode: options.mode === "all" ? "all" : "any",
				title: options.title || __("ERPNext E-Mail-Suche"),
				device_id: options.device_id || null,
			},
			freeze: true,
			freeze_message: __("Suchauftrag wird an Thunderbird gesendet …"),
		});
		frappe.show_alert({ message: __("Suchauftrag an Thunderbird gesendet."), indicator: "green" });
		return response.message;
	}

	async function list_devices() {
		const response = await frappe.call({ method: `${method}.list_devices` });
		return response.message || [];
	}

	async function compose_message(options = {}) {
		const to = Array.isArray(options.to) ? options.to : [];
		const cc = Array.isArray(options.cc) ? options.cc : [];
		const bcc = Array.isArray(options.bcc) ? options.bcc : [];
		if (!to.length && !cc.length && !bcc.length) {
			frappe.throw(__("Mindestens ein Empfänger ist erforderlich."));
		}
		const response = await frappe.call({
			method: `${method}.enqueue_compose`,
			args: {
				to,
				cc,
				bcc,
				subject: options.subject || "",
				plain_text_body: options.plain_text_body || "",
				device_id: options.device_id || null,
			},
			freeze: true,
			freeze_message: __("Nachrichtenentwurf wird in Thunderbird geöffnet …"),
		});
		frappe.show_alert({ message: __("Nachrichtenentwurf an Thunderbird gesendet."), indicator: "green" });
		return response.message;
	}

	const bridge = Object.freeze({ show_messages, compose_message, list_devices });
	window.thunderbird_hausverwaltung = bridge;
	window.hv_thunderbird = bridge;
})();
