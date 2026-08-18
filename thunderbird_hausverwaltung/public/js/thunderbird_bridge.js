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

	async function sync_timeline(reference_doctype, reference_name) {
		const response = await frappe.call({
			method: `${method}.enqueue_timeline_sync`,
			args: { reference_doctype, reference_name },
			freeze: true,
			freeze_message: __("Schriftverkehr-Abgleich wird an Thunderbird gesendet …"),
		});
		frappe.show_alert({ message: __("Thunderbird gleicht den Schriftverkehr ab."), indicator: "blue" });
		return response.message;
	}

	async function get_timeline(reference_doctype, reference_name) {
		const response = await frappe.call({
			method: `${method}.get_timeline`,
			args: { reference_doctype, reference_name },
		});
		return response.message || [];
	}

	async function open_timeline_message(message_name) {
		await frappe.call({
			method: `${method}.enqueue_open_timeline_message`,
			args: { message_name },
		});
		frappe.show_alert({ message: __("Nachricht wird in Thunderbird geöffnet."), indicator: "green" });
	}

	function parse_addresses(value) {
		try {
			const result = JSON.parse(value || "[]");
			return Array.isArray(result) ? result.join(", ") : "";
		} catch (_error) {
			return "";
		}
	}

	function month_heading(value) {
		if (!value) return __("Ohne Datum");
		return new Intl.DateTimeFormat("de-DE", { month: "long", year: "numeric" }).format(new Date(value));
	}

	function render_timeline(container, messages) {
		container.empty();
		container.append(
			$("<div>", {
				class: "tb-timeline-note",
				text: __("Prototyp: Die Zuordnung erfolgt anhand der beim Mietvertrag gespeicherten E-Mail-Adressen."),
			})
		);
		if (!messages.length) {
			container.append(
				$("<div>", { class: "tb-timeline-empty" }).append(
					$("<div>", { class: "tb-timeline-empty-icon", text: "✉" }),
					$("<h4>", { text: __("Noch kein Schriftverkehr abgeglichen") }),
					$("<p>", { text: __("Starte den Abgleich, um passende E-Mail-Metadaten aus Thunderbird zu übernehmen.") })
				)
			);
			return;
		}

		let current_month = null;
		const timeline = $("<div>", { class: "tb-timeline" }).appendTo(container);
		for (const message of messages) {
			const heading = month_heading(message.message_date);
			if (heading !== current_month) {
				current_month = heading;
				timeline.append($("<h5>", { class: "tb-timeline-month", text: heading }));
			}
			const direction = message.direction || "Unbekannt";
			const item = $("<article>", { class: `tb-timeline-item direction-${direction.toLowerCase()}` });
			const marker = $("<span>", { class: "tb-timeline-marker", title: direction });
			const card = $("<div>", { class: "tb-timeline-card" });
			const when = message.message_date
				? new Intl.DateTimeFormat("de-DE", { dateStyle: "medium", timeStyle: "short" }).format(new Date(message.message_date))
				: __("Datum unbekannt");
			const meta = $("<div>", { class: "tb-timeline-meta" }).append(
				$("<span>", { class: "tb-timeline-direction", text: direction }),
				$("<time>", { text: when })
			);
			const title = $("<button>", {
				type: "button",
				class: "tb-timeline-subject",
				text: message.subject || __("(ohne Betreff)"),
				title: __("In Thunderbird öffnen"),
			}).on("click", () => open_timeline_message(message.name));
			const counterpart = direction === "Ausgang"
				? parse_addresses(message.recipients)
				: message.sender || parse_addresses(message.recipients);
			card.append(
				meta,
				title,
				$("div", { class: "tb-timeline-participants", text: counterpart || __("Teilnehmer unbekannt") }),
				$("div", { class: "tb-timeline-folder", text: message.folder_path || "" })
			);
			item.append(marker, card);
			timeline.append(item);
		}
	}

	async function open_timeline(reference_doctype, reference_name) {
		const dialog = new frappe.ui.Dialog({
			title: __("Schriftverkehr: {0}", [reference_name]),
			size: "extra-large",
			fields: [{ fieldtype: "HTML", fieldname: "timeline" }],
			primary_action_label: __("Mit Thunderbird abgleichen"),
			primary_action: async () => {
				await sync_timeline(reference_doctype, reference_name);
				dialog.get_primary_btn().prop("disabled", true).text(__("Abgleich läuft …"));
			},
		});
		const container = dialog.fields_dict.timeline.$wrapper;
		const load = async () => render_timeline(container, await get_timeline(reference_doctype, reference_name));
		const realtime_handler = (event = {}) => {
			if (event.reference_doctype === reference_doctype && event.reference_name === reference_name) {
				void load();
				if (event.completed !== undefined) {
					dialog
						.get_primary_btn()
						.prop("disabled", false)
						.text(__("Mit Thunderbird abgleichen"));
				}
			}
		};
		frappe.realtime.on("thunderbird_timeline_updated", realtime_handler);
		dialog.$wrapper.on("hidden.bs.modal", () => frappe.realtime.off("thunderbird_timeline_updated", realtime_handler));
		dialog.show();
		container.html(`<div class="tb-timeline-loading">${__("Schriftverkehr wird geladen …")}</div>`);
		await load();
	}

	const bridge = Object.freeze({
		show_messages,
		compose_message,
		list_devices,
		open_timeline,
		sync_timeline,
	});
	window.thunderbird_hausverwaltung = bridge;
	window.hv_thunderbird = bridge;
})();
