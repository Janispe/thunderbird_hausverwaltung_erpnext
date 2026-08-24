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
			"""# E-Mail-Ablage: Qwen3 mit 4k/32k Kontext und Q8/F16

## tl;dr

- **32k bringt beim vollständigen Rohtext keinen Qualitätsgewinn:** Qwen3 Q8 erreicht mit 4.096 und 32.768 Kontext jeweils **62,54 % Top-1 / 77,81 % Top-3**.
- Der 32k-Lauf benötigt geladen rund **6,3 statt 1,5 GB GPU-Speicher** und war beim Rohtext **8,4 % langsamer** (39,9 statt 36,8 ms je Nachricht).
- Nur 33 von 2.082 verfügbaren Volltexten überschreiten 8.000 Zeichen, sieben 16.000 Zeichen und fünf 32.000 Zeichen. Der zusätzliche Kontext kann auf dieser Kohorte daher kaum helfen.
- **F16 verbessert Qwen3 praktisch nicht:** Mit vollständigem Rohtext erreicht Q8 **62,54 % Top-1 / 77,81 % Top-3**, F16 **62,25 % / 77,81 %**.
- Empfehlung: **Qwen3-Embedding-0.6B Q8 mit 4k Kontext beibehalten**. Weder F16 noch 32k rechtfertigen ihren zusätzlichen Speicherbedarf."""
		),
		nbformat.v4.new_markdown_cell(
			"""## Context & Methods

Fragestellung: Verbessern F16 oder ein expliziter 32k-Kontext gegenüber Qwen3-Embedding-0.6B Q8 mit Ollamas 4k-Standardkontext die Ordnerempfehlung mit vollständigem Nachrichtentext?

### Key Assumptions

- Datenstand: 25.08.2026, Zeitzone Europe/Berlin.
- Der aktuelle Archivordner gilt als Label.
- Pro Ordner bilden die älteren 80 % das Training und die jüngeren 20 % den Test.
- Ordner mit weniger als acht Nachrichten werden ausgeschlossen.
- Die Hauptkohorte „new thread“ enthält nur Testmails ohne bereits im Training vorkommenden Thread.
- BGE-M3, Qwen3 Q8 mit 4k und 32k sowie Qwen3 F16 verwenden dieselben Nachrichten, Labels, Splits und Klassifikationsmethoden.
- Verglichen werden `subject_preview`, `subject_clean_text` und `subject_full_text`.
- Die Modelldatei unterstützt bei BGE-M3 maximal 8.192 und bei Qwen3 maximal 32.768 Tokens. BGE-M3 sowie Qwen3 Q8/F16 liefen zunächst mit Ollamas Standardkontext von 4.096 Tokens. Der zusätzliche Qwen-Q8-Lauf setzte über die Embed-API explizit `options.num_ctx=32768`; `ollama ps` bestätigte 32.768 Kontext.
- Verwendet wurden die offiziellen Ollama-Tags `qwen3-embedding:0.6b` (Q8_0, ID `ac6da0dfba84`) und [`qwen3-embedding:0.6b-fp16`](https://ollama.com/library/qwen3-embedding:0.6b-fp16) (F16, ID `67a7592a8852`).
- Q8 4k, F16 4k und Q8 32k wurden nacheinander auf derselben GPU und mit demselben Ollama-Endpunkt getestet. Laufzeiten einzelner Durchläufe können durch Warm-up und Caching schwanken.
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
qwen_32k = json.loads(Path("qwen3_32k_benchmark_result.json").read_text(encoding="utf-8"))
results_by_model = {
    "BGE-M3 F16": bge,
    "Qwen3-0.6B Q8 4k": qwen_q8,
    "Qwen3-0.6B F16 4k": qwen_f16,
    "Qwen3-0.6B Q8 32k": qwen_32k,
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

Die Rohtexte haben im Median 1.135 Zeichen und im 95. Perzentil 4.793 Zeichen. Nur 33 Nachrichten überschreiten 8.000 Zeichen, acht 12.000, sieben 16.000 und fünf 32.000 Zeichen; drei MIME-Ausreißer überschreiten 100.000 Zeichen. Zeichen sind nicht identisch mit Tokens, die Verteilung zeigt aber, dass nur ein sehr kleiner Teil vom größeren Kontext profitieren kann."""
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
		nbformat.v4.new_markdown_cell("### Qwen3 Q8: 4k gegen 32k Kontext"),
		nbformat.v4.new_code_cell(
			"""context_comparison = []
for variant in variants:
    rows = {}
    for context, result in (("4k", qwen_q8), ("32k", qwen_32k)):
        rows[context] = next(
            item for item in result["results"]
            if item["method"] == "participant-gate-0.5-then-centroid"
            and item["text_variant"] == variant
        )
    metrics_4k = rows["4k"]["metrics"]["new_thread"]
    metrics_32k = rows["32k"]["metrics"]["new_thread"]
    context_comparison.append({
        "text": variant,
        "4k_top1": metrics_4k["top1"],
        "32k_top1": metrics_32k["top1"],
        "delta_top1_pp": 100 * (metrics_32k["top1"] - metrics_4k["top1"]),
        "4k_top3": metrics_4k["top3"],
        "32k_top3": metrics_32k["top3"],
        "delta_top3_pp": 100 * (metrics_32k["top3"] - metrics_4k["top3"]),
        "4k_ms": rows["4k"]["embedding_ms_per_message"],
        "32k_ms": rows["32k"]["embedding_ms_per_message"],
    })

for row in context_comparison:
    print(
        f'{row["text"]:20} | Top-1 4k/32k {row["4k_top1"]:6.2%}/{row["32k_top1"]:6.2%} '
        f'({row["delta_top1_pp"]:+.2f} pp) | Top-3 {row["4k_top3"]:6.2%}/{row["32k_top3"]:6.2%} '
        f'({row["delta_top3_pp"]:+.2f} pp) | {row["4k_ms"]:.1f}/{row["32k_ms"]:.1f} ms'
    )

raw = next(row for row in context_comparison if row["text"] == "subject_full_text")
slowdown = raw["32k_ms"] / raw["4k_ms"] - 1
print(f"Rohtext: 32k war {slowdown:.1%} langsamer; geladen 6,3 statt 1,5 GB GPU-Speicher.")"""
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
		nbformat.v4.new_markdown_cell("### Metadaten: bereits stark, aber noch ausbaufähig"),
		nbformat.v4.new_code_cell(
			"""coverage = qwen_q8["participant_coverage"]
print(f'Empfänger vorhanden: {coverage["recipient_coverage"]:.2%}; irgendein Teilnehmer vorhanden: {coverage["participant_coverage"]:.2%}.')

metadata_methods = [
    "participant-history-idf", "correspondent-history-idf", "sender-majority",
    "recipient-history-idf", "thread-majority",
]
for method in metadata_methods:
    row = next(item for item in qwen_q8["results"] if item["method"] == method)
    metrics = row["metrics"]["new_thread"]
    print(f'{method:29} | {metrics["top1"]:6.2%} Top-1 | {metrics["top3"]:6.2%} Top-3 | {metrics["macro_top1"]:6.2%} Macro')"""
		),
		nbformat.v4.new_markdown_cell(
			"""Absender, Empfänger (`To`) und `Cc` werden bereits genutzt; die kombinierte Teilnehmerhistorie erreicht ohne Embeddings 51,59 % Top-1. Empfänger sind also nicht nur „auch wichtig“, sondern ein wesentlicher Teil des aktuellen Hybridverfahrens.

Die aussichtsreichsten zusätzlichen Merkmale sind:

1. **Absender- und Empfängerdomänen** als Rückfall für bisher unbekannte Einzeladressen.
2. **Richtung und externer Korrespondent** (eingehend/ausgehend) expliziter gewichten.
3. **Anhanganzahl, Dateinamen, MIME-Typen und Dateigrößen**; später optional lokal extrahierter PDF-/Dokumenttext.
4. **Mail-Header** wie `List-ID`, `In-Reply-To`, `References`, Priorität und Keywords.
5. **Hierarchischer Ordnerpfad**: zuerst Objekt/Vertrag, danach Unterordner. Das passt besser zur Baumstruktur als eine flache 59-Klassen-Entscheidung.
6. **Verifizierte ERPNext-Zuordnung** zu Kontakt, Mietvertrag und Wohnung. Mehrdeutige Treffer dürfen nicht geraten werden; insbesondere bleibt die 1:1-Zuordnung Customer ↔ Mietvertrag maßgeblich.

Monat/Saison und Wochentag kann man mitprüfen, sie sollten wegen Überanpassungsgefahr aber erst nach den obigen Merkmalen kommen."""
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
assert qwen_32k["embedding_context_length"] == 32768
assert "embedding_context_length" not in qwen_q8

sample_size = profile["new_thread_test_messages"]
approximate_margin = 1.96 * ((0.62 * 0.38 / sample_size) ** 0.5)
print(f"Konsistenzprüfungen bestanden; identischer Split; ungefähre 95%-Fehlerspanne einer einzelnen Top-1-Quote: ±{approximate_margin:.1%}.")"""
		),
		nbformat.v4.new_markdown_cell(
			"""## Takeaways

1. **32k bringt beim Rohtext exakt keinen Treffergewinn:** Top-1 und Top-3 bleiben gegenüber 4k unverändert. Beim bereinigten Text entspricht der kleine Top-1-Gewinn von 0,29 Prozentpunkten nur einer Nachricht; Top-3 bleibt gleich.
2. **Der Preis ist deutlich:** Qwen Q8 belegt mit 32k rund 6,3 statt 1,5 GB geladenen GPU-Speicher und war beim Rohtext in diesem Lauf 8,4 % langsamer.
3. **F16 bringt ebenfalls keinen Qualitätsgewinn:** Beim Rohtext verliert F16 gegenüber Q8 genau 0,29 Prozentpunkte Top-1 und bleibt bei Top-3 identisch.
4. **Q8/4k bleibt die sinnvolle Produktionseinstellung:** Die vollständige Rohtexteingabe erreicht damit 62,54 % Top-1 und 77,81 % Top-3; die wenigen sehr langen Nachrichten rechtfertigen keinen globalen 32k-Kontext.
5. **Metadaten sind ein zweites, komplementäres Signal:** Die kombinierte Teilnehmerhistorie erreicht allein bereits 51,59 % Top-1. Mehr Metadaten versprechen aktuell mehr als zusätzliche Kontextlänge.

### Sinnvolle nächste Modellkandidaten

1. **[Qwen3-Embedding-4B](https://huggingface.co/Qwen/Qwen3-Embedding-4B)** als Qualitätskandidat. Es bleibt in derselben Modellfamilie und ist über [offizielle Ollama-Tags](https://ollama.com/library/qwen3-embedding/tags) einfach betreibbar, benötigt aber deutlich mehr Speicher und Rechenzeit.
2. **[jina-embeddings-v2-base-de](https://huggingface.co/jinaai/jina-embeddings-v2-base-de)** als deutscher Spezialist: 161 Mio. Parameter, 8.192 Kontext, Apache-2.0. Dafür wäre ein lokales Sentence-Transformers-/TEI-Backend oder eine geprüfte Ollama-Konvertierung nötig.
3. **[EmbeddingGemma](https://ollama.com/library/embeddinggemma/tags)** als kleiner, schneller Ollama-Baseline-Kandidat. Es ist multilingual, hat aber nur 2.048 Kontext und sollte deshalb mit bereinigtem Text oder Chunking getestet werden.
4. **[Nomic Embed Text v2 MoE](https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe)** oder **[GTE Multilingual Base](https://huggingface.co/Alibaba-NLP/gte-multilingual-base)** als kompakte mehrsprachige Apache-2.0-Kandidaten. Beide erfordern voraussichtlich einen zusätzlichen lokalen Modell-Backendpfad.

`multilingual-e5-large` ist wegen 512 Tokens für vollständige Mails weniger passend. Jina Embeddings v3 wird trotz guter deutscher Fähigkeiten wegen der nichtkommerziellen Lizenz nicht für den produktiven Einsatz in der Hausverwaltung priorisiert.

### Validierungsurteil: mit Vorbehalten verwendbar

Die Berechnungen, Volltextprofile und Splits sind identisch. Der lokale Cache deckt jedoch nur rund 13 % der 16.000 indexierten Nachrichten ab und kann geöffnete oder offline gespeicherte Mails überrepräsentieren. Der Vergleich reicht für die klare Entscheidung gegen globales 32k und gegen F16; absolute Modellquoten und neue Modellkandidaten sollten nach Wiederherstellung von JMAP auf dem vollständigen Snapshot erneut geprüft werden."""
		),
	],
)

NotebookClient(notebook, timeout=180, kernel_name="python3").execute(cwd=str(DIRECTORY))
nbformat.write(notebook, OUTPUT)
print(OUTPUT)
