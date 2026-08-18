(() => {
	"use strict";

	const page = frappe.pages.immobilienbaumansich;
	if (!page?.on_page_load || page.__thunderbird_mail_buttons) return;
	page.__thunderbird_mail_buttons = true;
	const original_on_page_load = page.on_page_load;

	function add_mail_buttons(wrapper) {
		$(wrapper)
			.find(".mieter-link")
			.each(function () {
				const tenant_link = $(this);
				if (tenant_link.next(".tb-compose-tenant").length) return;
				const contact = tenant_link.data("mieter");
				const mietvertrag = tenant_link
					.closest("tr.wohnung-row")
					.find(".mietvertrag-link")
					.data("mietvertrag");
				if (!contact || !mietvertrag) return;

				const button = $("<button>", {
					type: "button",
					class: "btn btn-xs btn-default tb-compose-tenant",
					title: __("Neue E-Mail an {0}", [tenant_link.text().trim()]),
					"aria-label": __("Neue E-Mail an {0}", [tenant_link.text().trim()]),
					text: "✉",
				}).css({ marginLeft: "6px", padding: "1px 6px", lineHeight: "1.35" });

				button.on("click", async function (event) {
					event.preventDefault();
					event.stopPropagation();
					if (!window.hv_thunderbird?.compose_message) {
						frappe.throw(__("Die Thunderbird-Brücke ist nicht geladen. Bitte die Seite neu laden."));
					}
					button.prop("disabled", true);
					try {
						const response = await frappe.call({
							method:
								"thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.get_mietvertrag_contact_compose_context",
							args: { mietvertrag, contact },
							freeze: true,
							freeze_message: __("E-Mail-Adresse wird geladen …"),
						});
						const context = response.message || {};
						await window.hv_thunderbird.compose_message({
							to: context.to || [],
							subject: context.subject || "",
						});
					} finally {
						button.prop("disabled", false);
					}
				});
				tenant_link.after(button);
			});
	}

	page.on_page_load = function (wrapper) {
		original_on_page_load.call(this, wrapper);
		const observer = new MutationObserver(() => add_mail_buttons(wrapper));
		observer.observe(wrapper, { childList: true, subtree: true });
		$(wrapper).one("remove", () => observer.disconnect());
		add_mail_buttons(wrapper);
	};
})();
