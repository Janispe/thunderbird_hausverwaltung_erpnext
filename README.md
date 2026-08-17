# Thunderbird Hausverwaltung

Optionale Thunderbird-Bridge für die Frappe-App `hausverwaltung`.

Die App stellt eine benutzergebundene Befehlswarteschlange bereit, über die
ERPNext Nachrichten in Thunderbird suchen und neue Nachrichtenentwürfe öffnen
kann. Die Ausführung erfolgt durch ein separat installiertes Thunderbird-Add-on.

## Installation

```bash
bench get-app /pfad/zu/thunderbird_hausverwaltung
bench --site <site> install-app thunderbird_hausverwaltung
bench build --app thunderbird_hausverwaltung
```

Die App `hausverwaltung` muss auf der Site bereits installiert sein.

