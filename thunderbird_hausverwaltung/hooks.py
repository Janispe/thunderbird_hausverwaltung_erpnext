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

scheduler_events = {
	"hourly": [
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.cleanup_commands",
	],
}
