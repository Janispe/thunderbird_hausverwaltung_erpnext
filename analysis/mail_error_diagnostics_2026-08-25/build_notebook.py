from __future__ import annotations

import json
from pathlib import Path

import nbformat as nbf

BASE = Path(__file__).resolve().parent
DATA_FILE = BASE / "error_diagnostics.json"
OUTPUT_FILE = BASE / "mail_error_diagnostics.ipynb"


def code(source: str):
	return nbf.v4.new_code_cell(source.strip())


def markdown(source: str):
	return nbf.v4.new_markdown_cell(source.strip())


def main() -> None:
	result = json.loads(DATA_FILE.read_text(encoding="utf-8"))
	overall = result["overall"]

	notebook = nbf.v4.new_notebook()
	notebook["metadata"]["kernelspec"] = {
		"display_name": "Python 3",
		"language": "python",
		"name": "python3",
	}
	notebook["metadata"]["language_info"] = {"name": "python", "version": "3"}
	notebook["cells"] = [
		markdown(
			f"""
# TL;DR: Unbekannte Teilnehmer und uneindeutige Ordner verursachen die meisten Fehler

Der zeitliche Backtest von **{overall['n']} Mails aus neuen Threads** erreicht mit Snowflake Arctic Embed 2 plus Teilnehmerhistorie **{overall['top1']:.1%} Top-1** und **{overall['top3']:.1%} Top-3**. Von {overall['top1_errors']} falschen Erstvorschlägen liegen 110 im Eltern-, Kind- oder direkten Geschwisterordner. Der wichtigste Risikofaktor ist fehlende Teilnehmerhistorie: nur 44,8 % Top-1.

Die Auswertung speichert weder Mailtexte noch reale Betreffzeilen, Adressen oder Ordnerpfade.
"""
		),
		markdown(
			"""
## Context & Methods

- Ältere 80 % je geeignetem Ordner dienen als Training, jüngere 20 % als Test.
- Die Hauptkohorte enthält nur Testmails, deren Thread im Training nicht vorkommt.
- Pro Ordner wird ein Zentroid aus Snowflake-Arctic-Embed-2-Vektoren gebildet.
- Eine ausreichend eindeutige Historie aus `From`, `To` und `Cc` darf die semantische Rangfolge umsortieren (Gate 0,5).
- Der komplette lokal verfügbare Nachrichtentext wird verwendet, aber auf 6.000 Zeichen je Mail begrenzt.
- Segmentvergleiche sind diagnostisch und nicht kausal; einige Merkmale überlappen.
"""
		),
		code(
			"""
from pathlib import Path
import json
import pandas as pd
from IPython.display import display

base = Path.cwd()
if not (base / "error_diagnostics.json").exists():
    base = Path("analysis/mail_error_diagnostics_2026-08-25")
result = json.loads((base / "error_diagnostics.json").read_text(encoding="utf-8"))

def segment_frame(segment_name):
    rows = []
    for label, values in result["segments"][segment_name].items():
        rows.append({"Segment": label, **values})
    frame = pd.DataFrame(rows)
    return frame.rename(columns={
        "n": "Mails", "top1": "Top-1", "top3": "Top-3",
        "top1_errors": "Top-1-Fehler", "top3_misses": "Top-3-Fehler",
    })

def format_rates(frame):
    return frame.style.format({"Top-1": "{:.1%}", "Top-3": "{:.1%}"})
"""
		),
		markdown("## Data"),
		code(
			"""
coverage = pd.DataFrame([{
    "Indexierte Mails": result["indexed_messages"],
    "Mit lokalem Volltext": result["usable_full_text_messages"],
    "Volltextabdeckung": result["usable_full_text_messages"] / result["indexed_messages"],
    "Training": result["train_messages"],
    "Test": result["test_messages"],
    "Neue Threads": result["new_thread_test_messages"],
    "Geeignete Ordner": result["split"]["eligible_folders"],
}])
display(coverage.style.format({"Volltextabdeckung": "{:.1%}"}))
"""
		),
		code(
			"""
# Reproduzierbare Konsistenzprüfungen
assert result["train_messages"] + result["test_messages"] + result["split"]["excluded_sparse_messages"] == result["usable_full_text_messages"]
assert result["overall"]["n"] == result["new_thread_test_messages"]
assert result["overall"]["top1_errors"] == round(result["overall"]["n"] * (1 - result["overall"]["top1"]))
assert result["overall"]["top3_misses"] == round(result["overall"]["n"] * (1 - result["overall"]["top3"]))
for segment in ["attachment", "direction", "body_length", "folder_train_size", "semantic_margin", "participant_history", "target_kind", "folder_depth"]:
    assert sum(row["n"] for row in result["segments"][segment].values()) == result["overall"]["n"], segment
assert sum(row["n"] for row in result["segments"]["hierarchy_relation"].values()) == result["overall"]["top1_errors"]
assert result["overall"]["top1"] <= result["overall"]["top3"]
print("Alle Konsistenzprüfungen bestanden.")
"""
		),
		markdown("## Results"),
		markdown(
			"""
### 1. Teilnehmerhistorie ist der stärkste Trennfaktor

Wenn das Gate greift, ist der erste Vorschlag fast doppelt so häufig richtig wie bei völlig unbekannten Teilnehmern. Gegenüber reiner Semantik verbessert das Teilnehmer-Gate 222 Mails und verschlechtert 19.
"""
		),
		code('display(format_rates(segment_frame("participant_history")))'),
		code(
			"""
gate = result["participant_gate"]
pd.DataFrame([{
    "Durch Gate verbessert": gate["helped_top1"],
    "Durch Gate verschlechtert": gate["harmed_top1"],
    "Top-1 nur Semantik": gate["semantic_only"]["top1"],
    "Top-1 kombiniert": result["overall"]["top1"],
}]).style.format({"Top-1 nur Semantik": "{:.1%}", "Top-1 kombiniert": "{:.1%}"})
"""
		),
		markdown(
			"""
### 2. Lange und weitergeleitete Inhalte sind schwieriger

Mittellange Volltexte funktionieren am besten. Oberhalb der 6.000-Zeichen-Grenze fällt Top-1 auf 52,0 %. Weiterleitungen erreichen 58,2 %. Anhänge allein sind kein starker Top-3-Nachteil; problematisch werden sie vor allem bei wenig Begleittext.
"""
		),
		code('display(format_rates(segment_frame("body_length")))'),
		code(
			"""
flags = segment_frame("overlapping_flags")
focus = flags[flags["Segment"].isin([
    "Weiterleitungsbetreff", "Anhang mit wenig Begleittext",
    "Überwiegend Zitat/Signatur", "Antwortbetreff",
])]
display(format_rates(focus))
"""
		),
		markdown(
			"""
### 3. Viele Fehler sind hierarchische Nahfehler

110 von 249 falschen Erstvorschlägen (44,2 %) liegen im Eltern-, Kind- oder direkten Geschwisterordner. Nur 19 Fehler wechseln den Hauptzweig. Der gemeinsame Archiv-Wurzelordner macht „gleicher Hauptzweig“ allerdings zu einer groben Kennzahl.
"""
		),
		code(
			"""
hierarchy = segment_frame("hierarchy_relation")
hierarchy["Anteil aller Top-1-Fehler"] = hierarchy["Mails"] / result["overall"]["top1_errors"]
display(hierarchy[["Segment", "Mails", "Anteil aller Top-1-Fehler", "Top-3"]].style.format({
    "Anteil aller Top-1-Fehler": "{:.1%}", "Top-3": "{:.1%}",
}))
"""
		),
		code('display(format_rates(segment_frame("folder_depth")))'),
		markdown(
			"""
### 4. Kleine Ordner sind nicht pauschal das Problem

Die kleinste auswertbare Trainingsgruppe schneidet in diesem Snapshot sogar gut ab. Das ist kein Beleg, dass wenig Daten helfen; die Kohorte umfasst nur 34 Testmails und enthält vermutlich besonders eindeutige Teilnehmer oder Themen. Historische systemähnliche Altordner sind dagegen deutlich schlechter erkannt und erzeugen vermeidbares Labelrauschen.
"""
		),
		code('display(format_rates(segment_frame("folder_train_size")))'),
		code('display(format_rates(segment_frame("target_kind")))'),
		markdown(
			"""
## Takeaways

1. **Teilnehmerhistorie produktiv nutzen:** eigene Adressen explizit konfigurieren und `From`, `To`, `Cc` gemeinsam auswerten.
2. **Unsicherheit sichtbar machen:** ohne Teilnehmerhistorie und bei kleinem Embedding-Abstand lieber mehrere Vorschläge oder „unsicher“ anzeigen; niemals automatisch verschieben.
3. **Ordnerbaum berücksichtigen:** zuerst Objekt/Hauptordner, danach Unterordner bewerten; direkte Nachbarn getrennt nachranken.
4. **Anhänge erschließen:** Dateiname, MIME-Typ und später extrahierten Anhangtext als zusätzliche Merkmale testen.
5. **Weiterleitungen bereinigen:** neuen Begleittext, weitergeleitete Header und eingebettete Ursprungsnachricht getrennt gewichten.
6. **Pseudo-Systemordner prüfen:** alte Entwurfs-, Papierkorb- oder Posteingangsordner nur als Ziele zulassen, wenn sie fachlich wirklich gewollt sind.

### Grenzen

Nur 4.503 von 17.087 indexierten Mails hatten im lokalen Thunderbird-Cache verwertbaren Volltext. Die Segmentergebnisse können deshalb durch den Cachebestand verzerrt sein. Der Lauf misst historische Ablageentscheidungen, die teilweise selbst inkonsistent oder mehrdeutig sein können. Das derzeit konfigurierte Archivkonto hat Embeddings noch deaktiviert; die Ergebnisse beschreiben den getesteten Zielansatz, nicht den momentan laufenden Produktivklassifikator.
"""
		),
	]

	nbf.write(notebook, OUTPUT_FILE)


if __name__ == "__main__":
	main()
