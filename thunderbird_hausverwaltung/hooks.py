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

app_include_css = [
	"/assets/thunderbird_hausverwaltung/css/thunderbird_timeline.css",
]

doctype_js = {
	"Immobilie": "public/js/immobilie.js",
	"Mietvertrag": "public/js/mietvertrag.js",
	"Wohnung": "public/js/wohnung.js",
}

page_js = {
	"immobilienbaumansich": "public/js/immobilienbaumansich.js",
}

after_migrate = [
	"thunderbird_hausverwaltung.thunderbird_hausverwaltung.setup.ensure_mail_archive_integration",
	"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.check_archive_problems",
]

hausverwaltung_problem_checks = [
	"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.check_archive_problems",
]

doc_events = {
	"Immobilie": {
		"on_update": [
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.enqueue_archive_problem_check",
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.tagging.invalidate_tag_sync",
		],
	},
	"Mietvertrag": {
		"on_update": [
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.enqueue_archive_problem_check",
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.tagging.invalidate_tag_sync",
		],
		"on_trash": [
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.enqueue_archive_problem_check",
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.tagging.invalidate_tag_sync",
		],
	},
	"Contact": {
		"on_update": [
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.enqueue_archive_problem_check",
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.tagging.invalidate_tag_sync",
		],
		"on_trash": [
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.enqueue_archive_problem_check",
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.tagging.invalidate_tag_sync",
		],
	},
	"Customer": {
		"on_update": [
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.enqueue_archive_problem_check",
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.tagging.invalidate_tag_sync",
		],
		"on_trash": [
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.enqueue_archive_problem_check",
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.tagging.invalidate_tag_sync",
		],
	},
	"Mail Archive Folder": {
		"on_update": [
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.enqueue_archive_problem_check",
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.tagging.invalidate_tag_sync",
		],
		"on_trash": [
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.enqueue_archive_problem_check",
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.tagging.invalidate_tag_sync",
		],
	},
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
	"cron": {
		"*/5 * * * *": [
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.source_sync.enqueue_enabled_source_account_syncs",
			"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.sync.enqueue_enabled_account_syncs",
		],
	},
	"hourly": [
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.integrations.thunderbird_bridge.cleanup_commands",
	],
	"daily": [
		"thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.problems.check_archive_problems",
	],
}
