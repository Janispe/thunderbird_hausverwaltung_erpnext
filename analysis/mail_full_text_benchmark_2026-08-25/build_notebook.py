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
			"""# E-Mail-Ablage: BGE-M3 und Qwen3 mit vollständigem Nachrichtentext

## tl;dr

- Mit vollständigem Rohtext erzielt **Qwen3-Embedding-0.6B 62,5 % Top-1 und 77,8 % Top-3**. BGE-M3 erreicht auf demselben Split **61,4 % und 77,5 %**.
- Qwens Vorsprung gegenüber BGE-M3 mit Rohtext beträgt nur **1,2 Prozentpunkte Top-1** und **0,3 Punkte Top-3**. Das liegt deutlich innerhalb der ungefähren Stichprobenunsicherheit von ±5,1 Prozentpunkten.
- Qwen3 profitiert stärker vom gesamten Rohtext als von Betreff + Vorschau, benötigt lokal jedoch **36,8 ms statt 9,4 ms je Nachricht**. BGE-M3 benötigt mit Rohtext 20,9 ms und mit Vorschau 7,4 ms.
- Empfehlung: Qwen3-Volltext als aussichtsreichen Kandidaten behalten, aber noch nicht zum eindeutigen Sieger erklären. Die Entscheidung sollte mit dem vollständigen JMAP-Snapshot wiederholt werden."""
		),
		nbformat.v4.new_markdown_cell(
			"""## Context & Methods

Fragestellung: Verbessert der gesamte Nachrichtentext die Ordnerempfehlung und verhält sich Qwen3-Embedding-0.6B dabei anders als BGE-M3?

### Key Assumptions

- Datenstand: 25.08.2026, Zeitzone Europe/Berlin.
- Der aktuelle Archivordner gilt als Label.
- Pro Ordner bilden die älteren 80 % das Training und die jüngeren 20 % den Test.
- Ordner mit weniger als acht Nachrichten werden ausgeschlossen.
- Die Hauptkohorte „new thread“ enthält nur Testmails ohne bereits im Training vorkommenden Thread.
- Beide Modelle verwenden dieselben Nachrichten, Labels, Splits und Klassifikationsmethoden.
- Verglichen werden `subject_preview`, `subject_clean_text` und `subject_full_text`.
- Das lokale BGE-M3 hat 8.192 Tokens Kontext; Qwen3-Embedding-0.6B hat 32.768. Sehr lange Texte werden am jeweiligen Modellfenster abgeschnitten.
- Der Stalwart-Endpunkt war während des Laufs nicht erreichbar. Daher stammen die Volltexte read-only aus dem lokalen Thunderbird-mbox-Cache und werden ausschließlich über RFC-Message-ID mit den ERPNext-Labels verbunden.
- Weder Nachrichtentexte noch Vektoren wurden an einen externen Dienst übertragen."""
		),
		nbformat.v4.new_markdown_cell("## Data"),
		nbformat.v4.new_code_cell(
			"""from pathlib import Path
import json

bge = json.loads(Path("benchmark_result.json").read_text(encoding="utf-8"))
qwen = json.loads(Path("qwen3_benchmark_result.json").read_text(encoding="utf-8"))
results_by_model = {"BGE-M3": bge, "Qwen3-Embedding-0.6B": qwen}

profile = {
    "indexed_messages": bge["source_messages_before_full_text_filter"],
    "matched_nonempty_full_text": bge["full_text_profile"]["nonempty_full_text"],
    "eligible_full_text_messages": bge["source_messages"],
    "train_messages": bge["train_messages"],
    "test_messages": bge["test_messages"],
    "new_thread_test_messages": bge["new_thread_test_messages"],
    "eligible_folders": bge["eligible_folders"],
    "attachment_messages": bge["attachment_messages"],
}

split_keys = [
    "source_messages_before_full_text_filter", "source_messages", "train_messages",
    "test_messages", "new_thread_test_messages", "eligible_folders",
    "excluded_sparse_messages", "attachment_messages",
]
assert all(bge[key] == qwen[key] for key in split_keys)
profile"""
		),
		nbformat.v4.new_markdown_cell(
			"""Von 16.000 indexierten Nachrichten konnten 2.082 nichtleere Volltexte aus dem lokalen Cache zugeordnet werden. Nach Ausschluss kleiner Ordner bleiben 1.950 Nachrichten in 59 Ordnern; davon sind 395 Testnachrichten und 347 neue Threads.

Die Rohtexte haben im Median 1.136 Zeichen, im 95. Perzentil 4.752 und im 99. Perzentil 8.995 Zeichen. Sieben Texte überschreiten 16.000 Zeichen; drei überschreiten 100.000 Zeichen. Extreme MIME-Ausreißer werden durch das jeweilige Modellfenster abgeschnitten."""
		),
		nbformat.v4.new_markdown_cell("## Results"),
		nbformat.v4.new_code_cell(
			"""variants = ["subject_preview", "subject_clean_text", "subject_full_text"]
methods = ["participant-gate-0.5-then-centroid", "folder-centroid", "weighted-7nn"]
comparison = []
for model_label, result in results_by_model.items():
    for method in methods:
        for variant in variants:
            row = next(
                item for item in result["results"]
                if item["method"] == method and item["text_variant"] == variant
            )
            metrics = row["metrics"]["new_thread"]
            comparison.append({
                "model": model_label,
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
        f'{row["model"]:23} | {row["method"]:38} | {row["text"]}'
    )"""
		),
		nbformat.v4.new_markdown_cell("### Nachrichten mit und ohne Anhang"),
		nbformat.v4.new_code_cell(
			"""attachment_rows = []
for model_label, result in results_by_model.items():
    for variant in variants:
        row = next(
            item for item in result["results"]
            if item["method"] == "participant-gate-0.5-then-centroid"
            and item["text_variant"] == variant
        )
        attachment_rows.append({
            "model": model_label,
            "text": variant,
            "with_attachment_top1": row["metrics"]["with_attachment"]["top1"],
            "with_attachment_top3": row["metrics"]["with_attachment"]["top3"],
            "without_attachment_top1": row["metrics"]["without_attachment"]["top1"],
            "without_attachment_top3": row["metrics"]["without_attachment"]["top3"],
        })

for row in attachment_rows:
    print(
        f'{row["model"]:23} | {row["text"]:20} | '
        f'mit Anhang {row["with_attachment_top1"]:6.1%}/{row["with_attachment_top3"]:6.1%} '
        f'| ohne Anhang {row["without_attachment_top1"]:6.1%}/{row["without_attachment_top3"]:6.1%}'
    )"""
		),
		nbformat.v4.new_markdown_cell(
			"""Die Anhangsauswertung ist eine Untergruppenanalyse des gesamten Testsatzes. `has_attachment` wurde nicht als Klassifikationsmerkmal verwendet. Anzahl, Dateiname, MIME-Typ und Dokumentinhalt stehen in diesem Lauf nicht zur Verfügung."""
		),
		nbformat.v4.new_markdown_cell("### Validation checks"),
		nbformat.v4.new_code_cell(
			"""for result in results_by_model.values():
    assert profile["train_messages"] + profile["test_messages"] + result["excluded_sparse_messages"] == profile["eligible_full_text_messages"]
    assert profile["new_thread_test_messages"] <= profile["test_messages"]
    assert result["full_text_profile"]["empty_full_text"] + result["full_text_profile"]["nonempty_full_text"] == profile["indexed_messages"]
    for row in result["results"]:
        for cohort in row["metrics"].values():
            assert 0 <= cohort["top1"] <= cohort["top3"] <= 1
            assert 0 <= cohort["macro_top1"] <= 1

sample_size = profile["new_thread_test_messages"]
approximate_margin = 1.96 * ((0.62 * 0.38 / sample_size) ** 0.5)
print(f"Konsistenzprüfungen bestanden; identischer Split; ungefähre 95%-Fehlerspanne einer einzelnen Top-1-Quote: ±{approximate_margin:.1%}.")"""
		),
		nbformat.v4.new_markdown_cell(
			"""## Takeaways

1. **Qwen3 mit Rohtext gewinnt nominell:** Im Teilnehmer-Hybrid erreicht es 62,5 % Top-1 und 77,8 % Top-3. Gegenüber Qwens Vorschau sind das +4,0 beziehungsweise +2,9 Prozentpunkte.
2. **Kein belastbarer Modellsieger:** Gegenüber BGE-M3 mit Rohtext beträgt Qwens Vorteil nur +1,2 Punkte Top-1 und +0,3 Punkte Top-3. BGE-M3 hat mit 70,1 % sogar die leicht bessere gleichgewichtete Ordnerquote als Qwen3 mit 69,8 %. Alle Unterschiede liegen innerhalb der ungefähren ±5,1-Prozentpunkte-Unsicherheit.
3. **Deutlicher Laufzeitunterschied:** Qwen3-Rohtext benötigt lokal 36,8 ms pro Embedding, BGE-M3-Rohtext 20,9 ms und BGE-M3-Vorschau 7,4 ms. Für einmalige Archivindexierung kann das akzeptabel sein, für häufige Neuindexierungen ist es relevant.
4. **Top-3 ist für das Add-on besonders wichtig:** Weil Thunderbird mehrere Vorschläge zeigt, ist Qwen3-Rohtext gegenüber BGE-M3-Vorschau mit +2,6 Punkten Top-3 interessant, aber noch nicht abschließend belegt.
5. **Anhänge bleiben eine eigene Dimension:** Qwen3-Rohtext erreicht bei Nachrichten mit Anhang 63,5 % Top-1 und 77,3 % Top-3. Inhalt und Dateiname der Anhänge wurden nicht eingebettet.

### Validierungsurteil: mit Vorbehalten verwendbar

Die Berechnungen und Splits sind konsistent, aber der lokale Cache deckt nur rund 13 % der 16.000 indexierten Nachrichten ab und kann geöffnete oder offline gespeicherte Mails überrepräsentieren. Vor einer Produktiventscheidung muss derselbe Lauf nach Wiederherstellung von JMAP auf dem vollständigen Snapshot wiederholt werden."""
		),
	],
)

NotebookClient(notebook, timeout=180, kernel_name="python3").execute(cwd=str(DIRECTORY))
nbformat.write(notebook, OUTPUT)
print(OUTPUT)
