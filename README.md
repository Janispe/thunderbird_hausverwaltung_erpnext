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

## Mietvertrag

Auf einem gespeicherten `Mietvertrag` steht unter **Thunderbird → E-Mail verfassen** ein Button
bereit. Er übernimmt für jeden aktiven Vertragspartner die primäre E-Mail-Adresse (ersatzweise die
erste hinterlegte Adresse), entfernt Dubletten und öffnet einen neuen Thunderbird-Entwurf. Partner
mit der Rolle `Ausgezogen` oder einem bereits erreichten Auszugsdatum werden nicht angeschrieben.

Der Entwurf wird niemals automatisch versendet. Das Thunderbird-Add-on muss mit demselben
ERPNext-Benutzer verbunden sein, der den Button verwendet.

Die App erlaubt CORS-Anfragen nur von `moz-extension://`-Ursprüngen und nur für ihre
Thunderbird-API. Bereits installierte Add-on-Versionen können über die früheren API-Pfade der
App `hausverwaltung` weiterarbeiten; diese werden auf die ausgelagerten Endpunkte umgeleitet.
