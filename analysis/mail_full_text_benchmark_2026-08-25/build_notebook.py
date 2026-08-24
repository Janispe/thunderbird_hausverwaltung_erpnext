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
			"""# E-Mail-Ablage: BGE-M3 und Qwen3 Q8/F16 mit vollständigem Nachrichtentext

## tl;dr

- **F16 verbessert Qwen3 praktisch nicht:** Mit vollständigem Rohtext erreicht Q8 **62,54 % Top-1 / 77,81 % Top-3**, F16 **62,25 % / 77,81 %**.
- Die Differenz entspricht genau einer Testmail bei Top-1 und null Testmails bei Top-3. Sie ist weder praktisch noch statistisch relevant.
- F16 belegt als Ollama-Modell **1,2 GB statt 639 MB** und geladen auf der Test-GPU rund **2,2 statt 1,5 GB**. Es war in diesem einzelnen Lauf nicht langsamer, aber der geringe Zeitunterschied ist kein belastbarer Qualitätsvorteil.
- Empfehlung: **Q8 beibehalten**. F16 verdoppelt den Modellplatz annähernd, ohne die Ablagevorschläge messbar zu verbessern."""
		),
		nbformat.v4.new_markdown_cell(
			"""## Context & Methods

Fragestellung: Verbessert F16 gegenüber Q8 die Ordnerempfehlung von Qwen3-Embedding-0.6B, insbesondere mit vollständigem Nachrichtentext?

### Key Assumptions

- Datenstand: 25.08.2026, Zeitzone Europe/Berlin.
- Der aktuelle Archivordner gilt als Label.
- Pro Ordner bilden die älteren 80 % das Training und die jüngeren 20 % den Test.
- Ordner mit weniger als acht Nachrichten werden ausgeschlossen.
- Die Hauptkohorte „new thread“ enthält nur Testmails ohne bereits im Training vorkommenden Thread.
- BGE-M3, Qwen3 Q8 und Qwen3 F16 verwenden dieselben Nachrichten, Labels, Splits und Klassifikationsmethoden.
- Verglichen werden `subject_preview`, `subject_clean_text` und `subject_full_text`.
- Die Modelldatei unterstützt bei BGE-M3 maximal 8.192 und bei Qwen3 maximal 32.768 Tokens. Ollama 0.23.0 lud für die Benchmark-Aufrufe jedoch alle Varianten mit dem Standardkontext von 4.096 Tokens; längere Eingaben wurden dadurch abgeschnitten.
- Verwendet wurden die offiziellen Ollama-Tags `qwen3-embedding:0.6b` (Q8_0, ID `ac6da0dfba84`) und [`qwen3-embedding:0.6b-fp16`](https://ollama.com/library/qwen3-embedding:0.6b-fp16) (F16, ID `67a7592a8852`).
- Q8 und F16 wurden nacheinander auf derselben GPU und mit demselben Ollama-Endpunkt getestet. Laufzeiten einzelner Durchläufe können durch Warm-up und Caching schwanken.
- Der Stalwart-Endpunkt war während des Laufs nicht erreichbar. Daher stammen die Volltexte read-only aus dem lokalen Thunderbird-mbox-Cache und werden ausschließlich über RFC-Message-ID mit den ERPNext-Labels verbunden.
- Weder Nachrichtentexte noch Vektoren wurden an einen externen Dienst übertragen."""
		),
		nbformat.v4.new_markdown_cell("## Data"),
		nbformat.v4.new_code_cell(
			"""from pathlib import Path
import json

bge = json.loads(Path("benchmark_result.json").read_text(encoding="utf-8"))
qwen_q8 = json.loads(Path("qwen3_benchmark_result.json").read_text(encoding="utf-8"))
qwen_f16 = json.loads(Path("qwen3_f16_benchmark_result.json").read_text(encoding="utf-8"))
results_by_model = {
    "BGE-M3 F16": bge,
    "Qwen3-0.6B Q8": qwen_q8,
    "Qwen3-0.6B F16": qwen_f16,
}

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
assert all(
    bge[key] == result[key]
    for result in results_by_model.values()
    for key in split_keys
)
profile"""
		),
		nbformat.v4.new_markdown_cell(
			"""Von 16.000 indexierten Nachrichten konnten 2.082 nichtleere Volltexte aus dem lokalen Cache zugeordnet werden. Nach Ausschluss kleiner Ordner bleiben 1.950 Nachrichten in 59 Ordnern; davon sind 395 Testnachrichten und 347 neue Threads.

Die Rohtexte haben im Median 1.135 Zeichen und im 95. Perzentil 4.793 Zeichen. Extreme MIME-Ausreißer werden durch den tatsächlich geladenen Ollama-Kontext von 4.096 Tokens abgeschnitten."""
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
        f'{row["model"]:18} | {row["method"]:38} | {row["text"]}'
    )"""
		),
		nbformat.v4.new_markdown_cell("### Qwen3: Q8 gegen F16"),
		nbformat.v4.new_code_cell(
			"""precision_comparison = []
for variant in variants:
    rows = {}
    for precision, result in (("Q8", qwen_q8), ("F16", qwen_f16)):
        rows[precision] = next(
            item for item in result["results"]
            if item["method"] == "participant-gate-0.5-then-centroid"
            and item["text_variant"] == variant
        )
    q8_metrics = rows["Q8"]["metrics"]["new_thread"]
    f16_metrics = rows["F16"]["metrics"]["new_thread"]
    precision_comparison.append({
        "text": variant,
        "q8_top1": q8_metrics["top1"],
        "f16_top1": f16_metrics["top1"],
        "delta_top1_pp": 100 * (f16_metrics["top1"] - q8_metrics["top1"]),
        "q8_top3": q8_metrics["top3"],
        "f16_top3": f16_metrics["top3"],
        "delta_top3_pp": 100 * (f16_metrics["top3"] - q8_metrics["top3"]),
        "q8_ms": rows["Q8"]["embedding_ms_per_message"],
        "f16_ms": rows["F16"]["embedding_ms_per_message"],
    })

for row in precision_comparison:
    print(
        f'{row["text"]:20} | Top-1 Q8/F16 {row["q8_top1"]:6.2%}/{row["f16_top1"]:6.2%} '
        f'({row["delta_top1_pp"]:+.2f} pp) | Top-3 {row["q8_top3"]:6.2%}/{row["f16_top3"]:6.2%} '
        f'({row["delta_top3_pp"]:+.2f} pp) | {row["q8_ms"]:.1f}/{row["f16_ms"]:.1f} ms'
    )

print("Ollama-Speicher: Q8 639 MB Datei / 1,5 GB geladen; F16 1,2 GB Datei / 2,2 GB geladen; Kontext jeweils 4.096 Tokens.")"""
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
        f'{row["model"]:18} | {row["text"]:20} | '
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

assert all(
    bge["full_text_profile"] == result["full_text_profile"]
    for result in results_by_model.values()
)

sample_size = profile["new_thread_test_messages"]
approximate_margin = 1.96 * ((0.62 * 0.38 / sample_size) ** 0.5)
print(f"Konsistenzprüfungen bestanden; identischer Split; ungefähre 95%-Fehlerspanne einer einzelnen Top-1-Quote: ±{approximate_margin:.1%}.")"""
		),
		nbformat.v4.new_markdown_cell(
			"""## Takeaways

1. **F16 bringt keinen Qualitätsgewinn:** Beim Rohtext verliert F16 gegenüber Q8 genau 0,29 Prozentpunkte Top-1 und bleibt bei Top-3 identisch. Bei Vorschau und bereinigtem Text gewinnt F16 nur 0,29 beziehungsweise 0,29 Punkte Top-1.
2. **Die Präzisionsunterschiede entsprechen höchstens zwei Testmails:** Bei 347 neuen Threads sind die beobachteten Abweichungen viel kleiner als die ungefähre ±5,1-Prozentpunkte-Stichprobenunsicherheit.
3. **Q8 ist speichereffizienter:** Die Modelldatei benötigt 639 MB statt 1,2 GB; geladen wurden rund 1,5 statt 2,2 GB GPU-Speicher. Die einmalig beobachtete F16-Laufzeit war ähnlich oder etwas niedriger, ist ohne Wiederholung aber kein belastbarer Geschwindigkeitsvergleich.
4. **Der Volltextbefund bleibt bestehen:** Beide Qwen-Präzisionen erreichen mit Rohtext 77,81 % Top-3 und schlagen ihre Vorschau. Für das Thunderbird-Add-on ist Q8 daher der bessere praktische Kompromiss.
5. **„Volltext“ bezeichnet die vollständige Eingabe:** Ollama hat davon höchstens 4.096 Tokens verarbeitet. Ein späterer Chunking- oder expliziter Langkontext-Test wäre eine andere Versuchsfrage.

### Validierungsurteil: mit Vorbehalten verwendbar

Die Berechnungen, Volltextprofile und Splits sind identisch. Der lokale Cache deckt jedoch nur rund 13 % der 16.000 indexierten Nachrichten ab und kann geöffnete oder offline gespeicherte Mails überrepräsentieren. Der Qualitätsvergleich Q8/F16 ist auf dieser Kohorte deutlich genug für die Empfehlung Q8; absolute Modellquoten sollten nach Wiederherstellung von JMAP auf dem vollständigen Snapshot erneut geprüft werden."""
		),
	],
)

NotebookClient(notebook, timeout=180, kernel_name="python3").execute(cwd=str(DIRECTORY))
nbformat.write(notebook, OUTPUT)
print(OUTPUT)
