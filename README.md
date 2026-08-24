# Thunderbird Hausverwaltung

Optionale Thunderbird-Bridge für die Frappe-App `hausverwaltung`.

Die App stellt eine benutzergebundene Befehlswarteschlange bereit, über die
ERPNext Nachrichten in Thunderbird suchen und neue Nachrichtenentwürfe öffnen
kann. Frappe Socket.IO signalisiert neue Aufträge in Echtzeit; die Datenbank-
warteschlange sorgt dafür, dass während einer Unterbrechung nichts verloren geht.
Die Ausführung erfolgt durch ein separat installiertes Thunderbird-Add-on.

## Installation

```bash
bench get-app /pfad/zu/thunderbird_hausverwaltung
bench --site <site> install-app thunderbird_hausverwaltung
bench build --app thunderbird_hausverwaltung
```

Die App `hausverwaltung` muss auf der Site bereits installiert sein.

Für WebExtensions muss der Reverse Proxy `/socket.io` so weiterleiten, dass Frappe bei der
API-Token-authentifizierten Verbindung dieselbe `Host`- und `Origin`-Adresse sieht. Das
Frappe-Docker-Beispiel dieser Installation setzt dafür im Socket.IO-Location-Block beide Header
auf die interne Frontend-Adresse. Ohne diese Einstellung meldet Frappe `Invalid origin`.

## Mietvertrag

Auf einem gespeicherten `Mietvertrag` steht unter **Thunderbird → E-Mail verfassen** ein Button
bereit. Er übernimmt für jeden aktiven Vertragspartner die primäre E-Mail-Adresse (ersatzweise die
erste hinterlegte Adresse), entfernt Dubletten und öffnet einen neuen Thunderbird-Entwurf. Partner
mit der Rolle `Ausgezogen` oder einem bereits erreichten Auszugsdatum werden nicht angeschrieben.

Der Entwurf wird niemals automatisch versendet. Das Thunderbird-Add-on muss mit demselben
ERPNext-Benutzer verbunden sein, der den Button verwendet.

Über **Thunderbird → E-Mails anzeigen** werden Nachrichten gesucht, bei denen eine beliebige
E-Mail-Adresse eines aktuellen oder historischen Vertragspartners als Absender oder Empfänger
vorkommt. Dabei werden alle am jeweiligen `Contact` hinterlegten E-Mail-Adressen berücksichtigt
und Dubletten entfernt.

## Wohnung

Auf einer gespeicherten `Wohnung` steht ebenfalls **Thunderbird → E-Mails anzeigen** zur
Verfügung. Die Suche ermittelt zuerst alle Mietverträge, die genau dieser Wohnung zugeordnet sind,
und berücksichtigt anschließend sämtliche aktuellen und historischen Vertragspartner sowie alle
E-Mail-Adressen ihrer `Contact`-Datensätze. Dadurch bleiben frühere Mietverhältnisse durchsuchbar,
ohne einen `Customer` über mehrere Mietverträge hinweg wiederzuverwenden.

Die App erlaubt CORS-Anfragen nur von `moz-extension://`-Ursprüngen und nur für ihre
Thunderbird-API. Bereits installierte Add-on-Versionen können über die früheren API-Pfade der
App `hausverwaltung` weiterarbeiten; diese werden auf die ausgelagerten Endpunkte umgeleitet.

Beim Einreihen eines Auftrags sendet die App das benutzerbezogene Realtime-Ereignis
`thunderbird_command_available`. Das Ereignis enthält keine E-Mail-Daten, sondern weckt nur das
Add-on. Dieses holt und quittiert den dauerhaft gespeicherten Auftrag anschließend über die API.

## Intelligente Archivablage

Die App kann ein zentrales Mailarchiv indexieren und für die aktuell in Thunderbird angezeigte
Nachricht nachvollziehbare Ablageziele vorschlagen. Die Fachlogik ist vom Mailserver getrennt. Der
erste Provider verwendet den offenen JMAP-Standard; Stalwart ist daher eine mögliche, aber keine
hart codierte Serverabhängigkeit. Weitere Provider können die Schnittstelle unter
`mail_archive/providers` implementieren.

### Einrichtung

1. In ERPNext einen **Mail Archive Account** anlegen.
2. `JMAP`, Server-URL, Benutzername und Passwort beziehungsweise App-Passwort eintragen. Frappe
   speichert Password-Felder verschlüsselt.
3. Unter **E-Mail-Adressen** alle Identitäten dieses Kontos eintragen. Darüber ordnet das Backend
   das lokale Thunderbird-Konto eindeutig dem Archivkonto zu; bei Mehrdeutigkeit wird nie geraten.
4. Optional die stabile JMAP Account-ID und eine Archiv-Wurzel-Mailbox-ID eintragen. Ohne Wurzel
   werden neue benutzerdefinierte Ordner als mögliche Ablageziele angelegt; Systemordner wie Inbox,
   Sent, Drafts, Junk und Trash sind ausgeschlossen.
5. Für lokale, datenschutzfreundliche Embeddings beispielsweise Ollama mit
   `http://<ollama-host>:11434` und `nomic-embed-text` konfigurieren. Alternativ wird ein
   OpenAI-kompatibler `/v1/embeddings`-Endpunkt unterstützt.
6. **Verbindung testen** und anschließend **Jetzt synchronisieren** ausführen.
7. Unter **Mail Archive Folder** die tatsächlich zulässigen Ziele prüfen und bei Bedarf einen
   expliziten Bezug zu `Mietvertrag`, `Wohnung`, `Customer` oder einem anderen Dokument setzen.

Nach einem `bench migrate` übernimmt der Scheduler stündlich die inkrementelle Synchronisation.
Ein großer Erstindex wird mit einem dauerhaften Cursor über mehrere Läufe fortgesetzt. JMAP
`Email/changes` sorgt danach dafür, dass nur Änderungen abgeholt werden.

### Daten- und Entscheidungsmodell

- Stalwart beziehungsweise der konfigurierte Mailserver bleibt Quelle und Speicher der vollständigen
  Nachricht. ERPNext speichert nur Metadaten, eine kurze Vorschau und das Embedding.
- Jede Nachricht und jeder Ordner wird über die stabile Provider-ID kontobezogen identifiziert;
  RFC Message-IDs dienen zur Korrelation mit Thunderbird.
- Pro Nachricht wird ein normalisiertes Embedding gespeichert. Pro Zielordner wird daraus ein
  Zentroid aufgebaut, sodass eine Anfrage nicht alle historischen Nachrichten vergleichen muss.
- Thread-Treffer, eindeutige frühere Absenderablagen, semantische Ähnlichkeit und ein expliziter
  ERPNext-Dokumentbezug werden kombiniert und als Begründung ausgegeben.
- Ein Mietvertrag wird nur verstärkt, wenn Kontaktadresse und Nachrichtendatum genau einen Vertrag
  ergeben. Es gibt keinen Customer-Fallback und keine Auflösung mehrdeutiger historischer Verträge.
- Verschoben wird ausschließlich nach Benutzerbestätigung. Annahme, Korrektur, Verwerfen, Modell und
  Kandidaten werden in **Mail Filing Suggestion** revisionsfähig protokolliert.

Das Thunderbird-Add-on zeigt in der Nachrichtenansicht die Aktion **Ablage vorschlagen**. Es sendet
nur RFC Message-ID und eigene Kontoadressen an ERPNext. ERPNext liest die Nachricht selbst über den
konfigurierten Provider und verschiebt sie nach Bestätigung serverseitig; Thunderbird übernimmt die
Änderung über seine normale Kontosynchronisation.
