from __future__ import annotations

from pathlib import Path

import nbformat
from nbclient import NotebookClient


DIRECTORY = Path(__file__).parent
OUTPUT = DIRECTORY / "mail_full_text_benchmark.ipynb"


notebook = nbformat.v4.new_notebook(
	metadata={
		"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
		"language_info": {"name": "python", "version": "3"},
	},
	cells=[
		nbformat.v4.new_markdown_cell(
			"""# E-Mail-Ablage: Vorschau gegen vollständigen Nachrichtentext

## tl;dr

- Beim besten Hybrid aus Teilnehmerhistorie und BGE-M3 erzielt **Betreff + Vorschau 62,0 % Top-1**; der vollständige Rohtext erreicht **61,4 %**. Das ist kein belastbarer Top-1-Gewinn.
- Der Rohtext verbessert in derselben Variante **Top-3 von 75,2 % auf 77,5 %** und die gleichgewichtete Ordnerquote, benötigt aber **2,8-mal so viel Embedding-Zeit**.
- Bei Testnachrichten mit Anhang steigt Top-3 von **71,9 % auf 76,6 %**. Der Inhalt oder Dateiname des Anhangs wurde dabei noch nicht analysiert.
- Empfehlung: Vorschau vorerst beibehalten. Volltext erst nach einem vollständigen JMAP-Lauf erneut bewerten; danach gezielt Dateinamen und extrahierte Dokumenttexte testen."""
		),
		nbformat.v4.new_markdown_cell(
			"""## Context & Methods

Fragestellung: Verbessert der gesamte Nachrichtentext die Ordnerempfehlung gegenüber der bisherigen Kombination aus Betreff und Vorschau?

### Key Assumptions

- Datenstand: 25.08.2026, Zeitzone Europe/Berlin.
- Der aktuelle Archivordner gilt als Label.
- Pro Ordner bilden die älteren 80 % das Training und die jüngeren 20 % den Test.
- Ordner mit weniger als acht Nachrichten werden ausgeschlossen.
- Die Hauptkohorte „new thread“ enthält nur Testmails ohne bereits im Training vorkommenden Thread.
- Verglichen werden `subject_preview`, `subject_clean_text` und `subject_full_text` mit demselben BGE-M3-Modell und demselben Split.
- BGE-M3 hat ein Kontextfenster von 8.192 Tokens. Sehr lange Rohtexte werden vom lokalen Ollama-Endpunkt am Modellfenster abgeschnitten.
- Der Stalwart-Endpunkt war während des Laufs nicht erreichbar. Daher stammen die Volltexte read-only aus dem lokalen Thunderbird-mbox-Cache und werden ausschließlich über RFC-Message-ID mit den ERPNext-Labels verbunden.
- Weder Nachrichtentexte noch Vektoren wurden an einen externen Dienst übertragen."""
		),
		nbformat.v4.new_markdown_cell("## Data"),
		nbformat.v4.new_code_cell(
			"""from pathlib import Path
import json

result = json.loads(Path("benchmark_result.json").read_text(encoding="utf-8"))
profile = {
    "indexed_messages": result["source_messages_before_full_text_filter"],
    "matched_nonempty_full_text": result["full_text_profile"]["nonempty_full_text"],
    "eligible_full_text_messages": result["source_messages"],
    "train_messages": result["train_messages"],
    "test_messages": result["test_messages"],
    "new_thread_test_messages": result["new_thread_test_messages"],
    "eligible_folders": result["eligible_folders"],
    "attachment_messages": result["attachment_messages"],
}
profile"""
		),
		nbformat.v4.new_markdown_cell(
			"""Von 16.000 indexierten Nachrichten konnten 2.082 nichtleere Volltexte aus dem lokalen Cache zugeordnet werden. Nach Ausschluss kleiner Ordner bleiben 1.950 Nachrichten in 59 Ordnern; davon sind 395 Testnachrichten und 347 neue Threads.

Die Rohtexte haben im Median 1.136 Zeichen, im 95. Perzentil 4.752 und im 99. Perzentil 8.995 Zeichen. Sieben Texte überschreiten 16.000 Zeichen; drei überschreiten 100.000 Zeichen. Ein extremer MIME-Ausreißer mit rund 20 MB wird durch das Modellfenster abgeschnitten."""
		),
		nbformat.v4.new_markdown_cell("## Results"),
		nbformat.v4.new_code_cell(
			"""variants = ["subject_preview", "subject_clean_text", "subject_full_text"]
methods = ["participant-gate-0.5-then-centroid", "folder-centroid", "weighted-7nn"]
comparison = []
for method in methods:
    for variant in variants:
        row = next(
            item for item in result["results"]
            if item["model"] == "bge-m3"
            and item["method"] == method
            and item["text_variant"] == variant
        )
        metrics = row["metrics"]["new_thread"]
        comparison.append({
            "method": method,
            "text": variant,
            "top1": metrics["top1"],
            "top3": metrics["top3"],
            "macro_top1": metrics["macro_top1"],
            "ms_per_message": row["embedding_ms_per_message"],
        })

for row in comparison:
    print(
        f'{row["top1"]:6.1%} Top-1 | {row["top3"]:6.1%} Top-3 | '
        f'{row["macro_top1"]:6.1%} Macro | {row["ms_per_message"]:5.1f} ms | '
        f'{row["method"]:38} | {row["text"]}'
    )"""
		),
		nbformat.v4.new_markdown_cell("### Nachrichten mit und ohne Anhang"),
		nbformat.v4.new_code_cell(
			"""attachment_rows = []
for variant in variants:
    row = next(
        item for item in result["results"]
        if item["model"] == "bge-m3"
        and item["method"] == "participant-gate-0.5-then-centroid"
        and item["text_variant"] == variant
    )
    attachment_rows.append({
        "text": variant,
        "with_attachment_top1": row["metrics"]["with_attachment"]["top1"],
        "with_attachment_top3": row["metrics"]["with_attachment"]["top3"],
        "without_attachment_top1": row["metrics"]["without_attachment"]["top1"],
        "without_attachment_top3": row["metrics"]["without_attachment"]["top3"],
    })

for row in attachment_rows:
    print(
        f'{row["text"]:20} | mit Anhang {row["with_attachment_top1"]:6.1%}/{row["with_attachment_top3"]:6.1%} '
        f'| ohne Anhang {row["without_attachment_top1"]:6.1%}/{row["without_attachment_top3"]:6.1%}'
    )"""
		),
		nbformat.v4.new_markdown_cell(
			"""Die Anhangsauswertung ist eine Untergruppenanalyse des gesamten Testsatzes. `has_attachment` wurde nicht als Klassifikationsmerkmal verwendet. Anzahl, Dateiname, MIME-Typ und Dokumentinhalt stehen in diesem Lauf nicht zur Verfügung."""
		),
		nbformat.v4.new_markdown_cell("### Validation checks"),
		nbformat.v4.new_code_cell(
			"""assert profile["train_messages"] + profile["test_messages"] + result["excluded_sparse_messages"] == profile["eligible_full_text_messages"]
assert profile["new_thread_test_messages"] <= profile["test_messages"]
assert result["full_text_profile"]["empty_full_text"] + result["full_text_profile"]["nonempty_full_text"] == profile["indexed_messages"]
for row in result["results"]:
    for cohort in row["metrics"].values():
        assert 0 <= cohort["top1"] <= cohort["top3"] <= 1
        assert 0 <= cohort["macro_top1"] <= 1

sample_size = profile["new_thread_test_messages"]
approximate_margin = 1.96 * ((0.62 * 0.38 / sample_size) ** 0.5)
print(f"Konsistenzprüfungen bestanden; ungefähre 95%-Fehlerspanne einer einzelnen Top-1-Quote: ±{approximate_margin:.1%}.")"""
		),
		nbformat.v4.new_markdown_cell(
			"""## Takeaways

1. **Kein belastbarer Top-1-Vorteil:** Im stärksten Hybrid liegt der Rohtext 0,6 Prozentpunkte und der bereinigte Text 2,0 Punkte unter der Vorschau. Diese Differenzen sind bei 347 neuen Threads kleiner als die Stichprobenunsicherheit.
2. **Leicht besseres Top-3:** Beide Volltextvarianten gewinnen 2,3 Prozentpunkte Top-3. Der reine Zentroid profitiert vom Rohtext ebenfalls leicht; gewichtete Nachbarn profitieren stärker.
3. **Mehr Rechenaufwand:** Volltext benötigt rund 20,9 statt 7,4 ms je Embedding und ist damit 2,8-mal langsamer.
4. **Anhänge bleiben interessant:** Bei Nachrichten mit Anhang gewinnt Volltext etwa 4,8 Prozentpunkte Top-3. Das begründet einen eigenen Test mit Dateinamen und extrahiertem PDF-/Office-Text, beweist aber noch keinen Nutzen des Anhanginhalts.

### Validierungsurteil: mit Vorbehalten verwendbar

Die Berechnungen und Splits sind konsistent, aber der lokale Cache deckt nur rund 13 % der 16.000 indexierten Nachrichten ab und kann geöffnete oder offline gespeicherte Mails überrepräsentieren. Vor einer Produktiventscheidung muss derselbe Lauf nach Wiederherstellung von JMAP auf dem vollständigen Snapshot wiederholt werden."""
		),
	],
)

NotebookClient(notebook, timeout=180, kernel_name="python3").execute(cwd=str(DIRECTORY))
nbformat.write(notebook, OUTPUT)
print(OUTPUT)
