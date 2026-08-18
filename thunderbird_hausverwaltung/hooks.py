app_name = "thunderbird_hausverwaltung"
app_title = "Thunderbird Hausverwaltung"
app_publisher = "janis"
app_description = "Thunderbird bridge for the Frappe Hausverwaltung app"
app_email = ""
app_license = "MIT"

required_apps = ["hausverwaltung"]

app_include_js = [
	"/assets/thunderbird_hausverwaltung/js/thunderbird_bridge.js",
]

doctype_js = {
	"Mietvertrag": "public/js/mietvertrag.js",
	"Wohnung": "public/js/wohnung.js",
}

before_request = [
	"thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.allow_extension_cors",
]

override_whitelisted_methods = {
	"hausverwaltung.hausverwaltung.integrations.thunderbird_bridge.register_device": "thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.register_device",
	"hausverwaltung.hausverwaltung.integrations.thunderbird_bridge.list_devices": "thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.list_devices",
	"hausverwaltung.hausverwaltung.integrations.thunderbird_bridge.enqueue_search": "thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.enqueue_search",
	"hausverwaltung.hausverwaltung.integrations.thunderbird_bridge.enqueue_compose": "thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.enqueue_compose",
	"hausverwaltung.hausverwaltung.integrations.thunderbird_bridge.poll_command": "thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.poll_command",
	"hausverwaltung.hausverwaltung.integrations.thunderbird_bridge.acknowledge_command": "thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.acknowledge_command",
}

scheduler_events = {
	"hourly": [
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.cleanup_commands",
	],
}
