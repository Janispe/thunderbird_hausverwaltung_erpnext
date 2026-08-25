from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
RESULT = json.loads((BASE / "error_diagnostics.json").read_text(encoding="utf-8"))
OUTPUT = BASE / "artifact.json"
GENERATED_AT = datetime.now().astimezone().replace(microsecond=0).isoformat()


def table_rows(segment: str, labels: list[str] | None = None) -> list[dict]:
	rows = []
	for label, values in RESULT["segments"][segment].items():
		if labels is None or label in labels:
			rows.append({"segment": label, **values})
	return rows


def main() -> None:
	overall = RESULT["overall"]
	participant = RESULT["segments"]["participant_history"]
	hierarchy = RESULT["segments"]["hierarchy_relation"]
	near_errors = sum(
		hierarchy[label]["n"]
		for label in [
			"Vorschlag ist ein Unterordner des tatsächlichen Ordners",
			"Vorschlag ist ein übergeordneter Ordner",
			"benachbarter Ordner mit gleichem Elternordner",
		]
	)
	content_rows = table_rows("body_length") + table_rows(
		"overlapping_flags",
		[
			"Weiterleitungsbetreff",
			"Anhang mit wenig Begleittext",
			"Überwiegend Zitat/Signatur",
			"Antwortbetreff",
		],
	)
	for row in content_rows:
		row["dimension"] = "Textlänge" if "Zeichen" in row["segment"] else "überlappendes Merkmal"

	folder_rows = table_rows("folder_depth") + table_rows("target_kind")
	for row in folder_rows:
		row["dimension"] = "Ordnertiefe" if "Ebenen" in row["segment"] else "Zieltyp"

	hierarchy_rows = []
	for label, values in hierarchy.items():
		hierarchy_rows.append(
			{
				"relation": label,
				"errors": values["n"],
				"error_share": values["n"] / overall["top1_errors"],
				"recovered_top3": values["top3"],
			}
		)

	source = {
		"id": "diagnostic-run",
		"label": "Lokaler ERPNext/Thunderbird/Snowflake-Backtest vom 25. August 2026",
		"query": {
			"engine": "Frappe Python + MariaDB + lokales Ollama",
			"language": "Python",
			"query": "mail_archive.error_diagnostics.run(account_name='Stalwart Test Archiv', model='snowflake-arctic-embed2', input_max_characters=6000)",
			"sql": (
				"SELECT name, provider_message_id, rfc_message_id, thread_id, subject, sender_email, participants, "
				"received_at, preview, has_attachment, actual_mailbox_id, actual_folder_path "
				"FROM `tabMail Archive Message` WHERE archive_account = 'Stalwart Test Archiv' "
				"AND status = 'Archiviert' AND actual_mailbox_id <> '' ORDER BY received_at ASC, name ASC;\n"
				"SELECT embedding_enabled, embedding_provider, embedding_model, embedding_base_url "
				"FROM `tabMail Archive Account` WHERE name = 'Stalwart Test Archiv';"
			),
			"description": "Zeitlicher Fehler-Backtest mit vollständigem lokal verfügbarem Nachrichtentext und Teilnehmer-Gate.",
			"executed_at": GENERATED_AT,
			"tables_used": ["frontend.tabMail Archive Message", "frontend.tabMail Archive Folder", "frontend.tabMail Archive Account"],
			"filters": [
				"status = Archiviert",
				"tatsächlicher Zielordner vorhanden",
				"mindestens 8 Nachrichten pro Ordner",
				"ältere 80 % Training, jüngere 20 % Test",
				"Hauptkohorte enthält nur neue Threads",
			],
			"metric_definitions": [
				"Top-1 = erster Vorschlag entspricht dem historisch gespeicherten Ordner",
				"Top-3 = historisch gespeicherter Ordner ist unter den ersten drei Vorschlägen",
				"Embedding-Abstand = Kosinus-Score des ersten minus Score des zweiten Zentroids vor Teilnehmer-Umsortierung",
			],
			"notes": [
				"Das einzige Diagramm vergleicht die drei disjunkten Teilnehmer-Kohorten; überlappende Fehlermerkmale bleiben bewusst tabellarisch.",
				"Mailtexte, echte Betreffzeilen, Adressen und Ordnerpfade wurden aus dem Artefakt entfernt.",
			],
		},
	}

	artifact = {
		"surface": "report",
		"manifest": {
			"version": 1,
			"surface": "report",
			"title": "Unbekannte Teilnehmer sind das größte Ablagerisiko",
			"description": "Fehlerdiagnose für Snowflake-Arctic-Embed-2, Volltext und Teilnehmerhistorie im Mailarchiv.",
			"generatedAt": GENERATED_AT,
			"blocks": [
				{"id": "title", "type": "markdown", "body": "# Unbekannte Teilnehmer sind das größte Ablagerisiko"},
				{
					"id": "summary",
					"type": "markdown",
					"sourceId": "diagnostic-run",
					"body": (
						"## Executive Summary\n\n"
						f"- **Ohne bekannte Teilnehmer fällt Top-1 auf {participant['Keine Historie']['top1']:.1%}.** Wenn die Absender-/Empfängerhistorie eindeutig genug ist, erreicht derselbe Ansatz {participant['Gate greift']['top1']:.1%}.\n"
						f"- **{near_errors} von {overall['top1_errors']} Erstfehlern ({near_errors / overall['top1_errors']:.1%}) sind hierarchische Nahfehler:** Eltern-, Kind- oder direkte Geschwisterordner. Nur 19 Fehler wechseln den Hauptzweig.\n"
						"- **Weiterleitungen, sehr lange Texte und Anhänge mit wenig Begleittext sind schwieriger.** Antworten mit bekannter Korrespondenz sind dagegen überdurchschnittlich gut.\n"
						"- **Die Live-Konfiguration nutzt diese Embeddings noch nicht.** Beim Snapshot waren Embeddings im Archivkonto deaktiviert und kein Modell eingetragen."
					),
				},
				{"id": "headline", "type": "metric-strip", "cardIds": ["top1", "top3", "errors", "unknown"]},
				{
					"id": "participant-text",
					"type": "markdown",
					"sourceId": "diagnostic-run",
					"body": (
						"## Teilnehmerhistorie trennt sichere von schwierigen Mails\n\n"
						f"Das Teilnehmer-Gate verbessert den ersten Vorschlag bei {RESULT['participant_gate']['helped_top1']} Mails und verschlechtert ihn bei {RESULT['participant_gate']['harmed_top1']}. "
						f"Reine Semantik erreicht nur {RESULT['participant_gate']['semantic_only']['top1']:.1%} Top-1; gemeinsam sind es {overall['top1']:.1%}. "
						"Absender und Empfänger sind deshalb keine bloßen Zusatzmetadaten, sondern das stärkste verfügbare Ablagesignal."
					),
				},
				{"id": "participant-chart", "type": "chart", "chartId": "participant-chart"},
				{"id": "participant-table", "type": "table", "tableId": "participant-table"},
				{
					"id": "content-text",
					"type": "markdown",
					"body": (
						"## Volltext hilft, muss aber strukturiert werden\n\n"
						"Mittellange Texte zwischen 1.001 und 4.000 Zeichen schneiden am besten ab. Bei Texten oberhalb der 6.000-Zeichen-Grenze sinkt Top-1 auf 52,0 %. "
						"Weiterleitungen erreichen 58,2 %, Anhänge mit wenig Begleittext 62,7 %. Das spricht für getrennte Gewichtung von neuem Text, Zitaten, weitergeleiteten Headern sowie Dateiname/MIME-Typ und später Anhangtext."
					),
				},
				{"id": "content-table", "type": "table", "tableId": "content-table"},
				{
					"id": "hierarchy-text",
					"type": "markdown",
					"body": (
						"## Ordnerhierarchie verursacht viele Nahfehler\n\n"
						f"Von {overall['top1_errors']} falschen Erstvorschlägen liegen {near_errors} unmittelbar in der Nähe des tatsächlichen Ordners. "
						"Ein zweistufiges Modell – erst Objekt/Hauptordner, dann Unterordner – kann diese Fälle gezielter trennen. "
						"Die Kennzahl „gleicher Hauptzweig“ ist vorsichtig zu lesen, weil viele Pfade denselben breiten Archiv-Wurzelordner teilen."
					),
				},
				{"id": "hierarchy-table", "type": "table", "tableId": "hierarchy-table"},
				{
					"id": "folder-text",
					"type": "markdown",
					"body": (
						"## Flache Sammelordner und Pseudo-Systemordner sind schwer\n\n"
						"Sehr tiefe, spezifische Ordner funktionieren besser als flache Sammelordner. Historische Ordner mit Namen wie Posteingang, Gesendet, Entwürfe oder Papierkorb erreichen in dieser kleinen Kohorte nur 36,4 % Top-1. "
						"Diese Ziele sollten geprüft und gegebenenfalls aus der Vorschlagsmenge entfernt werden. Kleine Ordner sind in diesem Snapshot dagegen nicht pauschal schlechter; die kleinste Kohorte ist mit 34 Testmails aber zu klein für eine allgemeine Schlussfolgerung."
					),
				},
				{"id": "folder-table", "type": "table", "tableId": "folder-table"},
				{
					"id": "examples",
					"type": "markdown",
					"body": (
						"## Typische Fehlermuster ohne personenbezogene Beispiele\n\n"
						"- Weitergeleitete Angebote oder Berichte werden nach dem Inhalt statt nach dem historisch gewählten allgemeinen Sammelordner abgelegt.\n"
						"- Rechnungen oder Leistungsnachweise mit knappem Begleittext werden einem thematisch verwandten Objekt- oder Projektordner zugeordnet; die entscheidende Information steckt vermutlich im Anhang.\n"
						"- Newsletter und Marktinformationen liegen historisch teils in allgemeinen, teils in fachlichen Ordnern – hier ist das Trainingslabel selbst uneindeutig.\n"
						"- Historische Entwurfs-/Papierkorbordner konkurrieren mit der inhaltlich plausiblen Ablage."
					),
				},
				{
					"id": "next",
					"type": "markdown",
					"body": (
						"## Empfohlene nächste Schritte\n\n"
						"1. Eigene Mailadressen explizit konfigurieren und Teilnehmerhistorie in den produktiven Vorschlagsdienst übernehmen.\n"
						"2. Bei fehlender Historie und Embedding-Abstand unter 0,03 als unsicher markieren und mehrere Optionen anbieten; weiterhin niemals automatisch verschieben.\n"
						"3. Historische Pseudo-Systemordner gemeinsam prüfen und ungewollte Ziele deaktivieren.\n"
						"4. Hierarchisch klassifizieren: zuerst Objekt/Hauptzweig, danach den Unterordner.\n"
						"5. Dateiname, MIME-Typ, Anhanganzahl und extrahierten Anhangtext separat evaluieren.\n"
						"6. Weiterleitungen und Zitatketten segmentieren und den neuesten Text höher gewichten."
					),
				},
				{
					"id": "limits",
					"type": "markdown",
					"sourceId": "diagnostic-run",
					"body": (
						"## Grenzen der Aussage\n\n"
						f"Nur {RESULT['usable_full_text_messages']:,} von {RESULT['indexed_messages']:,} indexierten Mails ({RESULT['usable_full_text_messages'] / RESULT['indexed_messages']:.1%}) hatten im lokalen Thunderbird-Cache verwertbaren Volltext. "
						"Der Cachebestand kann die Ergebnisse verzerren. Die Segmente überlappen und zeigen Zusammenhänge, keine Ursachen. Historische Ablageentscheidungen können inkonsistent sein. "
						"Die 0,5-Gate-Schwelle stammt aus der vorherigen Exploration und wurde in diesem Lauf nicht neu auf einem separaten Validierungssatz bestimmt."
					),
				},
				{
					"id": "questions",
					"type": "markdown",
					"body": (
						"## Offene Entscheidungen\n\n"
						"- Sollen allgemeine Sammelordner wie Verwaltung/Anzeigen weiterhin exakte Zielordner sein?\n"
						"- Welche alten Posteingang-/Gesendet-/Entwurfs-/Papierkorbordner sind fachlich gewollt?\n"
						"- Soll Thunderbird bei niedriger Sicherheit drei Vorschläge oder ausdrücklich „kein sicherer Vorschlag“ zeigen?"
					),
				},
			],
			"charts": [
				{
					"id": "participant-chart",
					"title": "Top-1 und Top-3 nach Teilnehmerhistorie",
					"subtitle": "785 spätere Mails aus neuen Threads; drei disjunkte Kohorten.",
					"type": "bar",
					"dataset": "participant_chart",
					"sourceId": "diagnostic-run",
					"valueFormat": "percent",
					"layout": "full",
					"encodings": {
						"x": {"field": "segment", "type": "ordinal", "label": "Teilnehmerhistorie"},
						"y": {"field": "value", "type": "quantitative", "label": "Trefferquote", "format": "percent"},
						"color": {"field": "metric", "type": "nominal", "label": "Kennzahl"},
					},
				},
			],
			"cards": [
				{"id": "top1", "dataset": "headline", "sourceId": "diagnostic-run", "description": "Erster Vorschlag stimmt bei neuen Threads.", "metrics": [{"label": "Top-1", "field": "top1", "format": "percent"}]},
				{"id": "top3", "dataset": "headline", "sourceId": "diagnostic-run", "description": "Richtiger Ordner unter den ersten drei Vorschlägen.", "metrics": [{"label": "Top-3", "field": "top3", "format": "percent"}]},
				{"id": "errors", "dataset": "headline", "sourceId": "diagnostic-run", "description": "Falsche Erstvorschläge in der Hauptkohorte.", "metrics": [{"label": "Top-1-Fehler", "field": "errors", "format": "number"}]},
				{"id": "unknown", "dataset": "headline", "sourceId": "diagnostic-run", "description": "Top-1 bei völlig unbekannten Teilnehmern.", "metrics": [{"label": "Ohne Historie", "field": "unknown_top1", "format": "percent"}]},
			],
			"tables": [
				{
					"id": "participant-table", "title": "Treffer nach Teilnehmerhistorie", "subtitle": "Disjunkte Kohorten; neue Threads.", "dataset": "participant", "sourceId": "diagnostic-run", "density": "spacious", "layout": "full",
					"columns": [
						{"field": "segment", "label": "Historie", "type": "text"}, {"field": "n", "label": "Mails", "format": "number"}, {"field": "top1", "label": "Top-1", "format": "percent"}, {"field": "top3", "label": "Top-3", "format": "percent"}, {"field": "top1_errors", "label": "Top-1-Fehler", "format": "number"},
					],
				},
				{
					"id": "content-table", "title": "Treffer nach Texteigenschaft", "subtitle": "Textlängen sind disjunkt; Merkmalszeilen können sich überlappen.", "dataset": "content", "sourceId": "diagnostic-run", "density": "compact", "layout": "full",
					"columns": [
						{"field": "dimension", "label": "Dimension", "type": "text"}, {"field": "segment", "label": "Segment", "type": "text"}, {"field": "n", "label": "Mails", "format": "number"}, {"field": "top1", "label": "Top-1", "format": "percent"}, {"field": "top3", "label": "Top-3", "format": "percent"},
					],
				},
				{
					"id": "hierarchy-table", "title": "Beziehung zwischen falschem Erstvorschlag und Ziel", "subtitle": "Nur die 249 Top-1-Fehler; Top-3 zeigt die Wiedergewinnung im Vorschlagssatz.", "dataset": "hierarchy", "sourceId": "diagnostic-run", "density": "spacious", "layout": "full",
					"columns": [
						{"field": "relation", "label": "Beziehung", "type": "text"}, {"field": "errors", "label": "Fehler", "format": "number"}, {"field": "error_share", "label": "Fehleranteil", "format": "percent"}, {"field": "recovered_top3", "label": "Ziel in Top-3", "format": "percent"},
					],
				},
				{
					"id": "folder-table", "title": "Treffer nach Ordnerstruktur", "subtitle": "Ordnertiefe und Zieltyp sind getrennte Dimensionen.", "dataset": "folders", "sourceId": "diagnostic-run", "density": "compact", "layout": "full",
					"columns": [
						{"field": "dimension", "label": "Dimension", "type": "text"}, {"field": "segment", "label": "Segment", "type": "text"}, {"field": "n", "label": "Mails", "format": "number"}, {"field": "top1", "label": "Top-1", "format": "percent"}, {"field": "top3", "label": "Top-3", "format": "percent"},
					],
				},
			],
			"sources": [source],
		},
		"snapshot": {
			"version": 1,
			"generatedAt": GENERATED_AT,
			"status": "ready",
			"accessIssues": [],
			"datasets": {
				"headline": [{"top1": overall["top1"], "top3": overall["top3"], "errors": overall["top1_errors"], "unknown_top1": participant["Keine Historie"]["top1"]}],
				"participant": table_rows("participant_history"),
				"participant_chart": [
					{"segment": label, "metric": metric, "value": values[key], "n": values["n"]}
					for label, values in participant.items()
					for metric, key in [("Top-1", "top1"), ("Top-3", "top3")]
				],
				"content": content_rows,
				"hierarchy": hierarchy_rows,
				"folders": folder_rows,
			},
		},
		"sources": [source],
	}

	OUTPUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
	main()
