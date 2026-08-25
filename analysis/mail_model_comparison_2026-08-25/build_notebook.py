from __future__ import annotations

from pathlib import Path

import nbformat
from nbclient import NotebookClient


DIRECTORY = Path(__file__).parent
OUTPUT = DIRECTORY / "mail_model_comparison.ipynb"


notebook = nbformat.v4.new_notebook(
	metadata={
		"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
		"language_info": {"name": "python", "version": "3"},
	},
	cells=[
		nbformat.v4.new_markdown_cell(
			"""# Lokale Embedding-Modelle für deutsche Archivmails

## tl;dr

- **Snowflake Arctic Embed 2 ist der beste neue Kandidat:** Mit begrenztem Rohtext erreicht es **62,54 % Top-1 / 78,67 % Top-3 / 71,72 % Macro-Top-1**.
- Qwen3-0.6B Q8 erreicht auf exakt demselben Lauf **62,54 % / 77,52 % / 69,83 %**. Snowflake gewinnt damit vier zusätzliche Top-3-Treffer, ist aber bei Top-1 identisch.
- Snowflake war mit **22,6 ms je Mail rund 35 % schneller** als Qwen3-0.6B mit 34,9 ms. Wegen des kleinen Stichprobenunterschieds ist das eine Empfehlung für einen Shadow-Test, noch kein Beweis für einen automatischen Modellwechsel.
- Qwen3-4B ist hier trotz besserer öffentlicher Benchmarks nicht besser: **60,23 % Top-1**, 111,6 ms je Mail. Jina DE ist sehr schnell, bleibt mit **60,81 % Top-1** aber ebenfalls hinter der Baseline.
- EmbeddingGemma konnte den Volltextlauf wegen wiederholter Kontextfehler nicht zuverlässig abschließen und wird deshalb nicht in die Qualitätsrangliste aufgenommen."""
		),
		nbformat.v4.new_markdown_cell(
			"""## Context & Methods

Fragestellung: Welches lokal betreibbare Embedding-Modell liefert für überwiegend deutsche Hausverwaltungs-E-Mails die besten Thunderbird-Ablagevorschläge?

### Key Assumptions

- Datenstand: 25.08.2026, Zeitzone Europe/Berlin.
- Derselbe zeitliche Split wie in den vorherigen Läufen: je Ordner ältere 80 % Training, jüngere 20 % Test; Ordner mit weniger als acht Nachrichten ausgeschlossen.
- Hauptkohorte sind 347 Testmails aus neuen Threads. Dadurch kann eine bereits bekannte Thread-ID das Ergebnis nicht künstlich verbessern.
- Alle Modelle sehen dieselben Nachrichten, Labels, Metadaten und Textvarianten.
- Jede Eingabe wird vor Ollama auf 6.000 Zeichen begrenzt. Damit sind mehr als 95 % der verfügbaren Mails vollständig enthalten und der 20-MB-MIME-Ausreißer kann den Lauf nicht blockieren.
- Für die direkte Rangliste wird bei jedem Modell dieselbe Methode `participant-gate-0.5-then-centroid` verwendet. Schwellenwerte werden nicht pro Modell auf dem Testsatz optimiert.
- Ollama 0.23.0 lief auf einer NVIDIA GeForce RTX 5070 mit 12.227 MiB VRAM. Die vollständig geladenen Kontexte wurden während der Läufe mit `ollama ps` geprüft.
- Nachrichtentexte und Vektoren wurden nicht an externe Dienste übertragen und sind nicht Bestandteil dieses Artefakts.

### Offizielle Modellquellen

- [Qwen3-Embedding-4B Modellkarte](https://huggingface.co/Qwen/Qwen3-Embedding-4B) und [Ollama-Tags](https://ollama.com/library/qwen3-embedding/tags)
- [Jina Embeddings v2 Base DE Modellkarte](https://huggingface.co/jinaai/jina-embeddings-v2-base-de) und [Ollama-Modell](https://ollama.com/jina/jina-embeddings-v2-base-de)
- [Snowflake Arctic Embed 2 Modellkarte](https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0) und [Ollama-Tags](https://ollama.com/library/snowflake-arctic-embed2/tags)
- [EmbeddingGemma bei Ollama](https://ollama.com/library/embeddinggemma/tags)"""
		),
		nbformat.v4.new_markdown_cell("## Data"),
		nbformat.v4.new_code_cell(
			"""from pathlib import Path
import json
import math

files = {
    "Qwen3 0.6B Q8": "qwen3_06b_q8_result.json",
    "Qwen3 4B Q4": "qwen3_4b_q4_result.json",
    "Jina v2 Base DE": "jina_v2_base_de_result.json",
    "Snowflake Arctic 2": "snowflake_arctic_embed2_result.json",
}
results = {label: json.loads(Path(filename).read_text(encoding="utf-8")) for label, filename in files.items()}
embeddinggemma_failure = json.loads(Path("embeddinggemma_failure.json").read_text(encoding="utf-8"))

baseline = results["Qwen3 0.6B Q8"]
profile = {
    "indexed_messages": baseline["source_messages_before_full_text_filter"],
    "matched_nonempty_full_text": baseline["full_text_profile"]["nonempty_full_text"],
    "eligible_messages": baseline["source_messages"],
    "train_messages": baseline["train_messages"],
    "test_messages": baseline["test_messages"],
    "new_thread_test_messages": baseline["new_thread_test_messages"],
    "eligible_folders": baseline["eligible_folders"],
    "input_max_characters": baseline["embedding_input_max_characters"],
}
profile"""
		),
		nbformat.v4.new_markdown_cell(
			"""Von 16.000 indexierten Archivnachrichten konnten 2.082 nichtleere Volltexte mit dem lokalen Thunderbird-Cache verbunden werden. Nach dem Ausschluss kleiner Ordner bleiben 1.950 Nachrichten in 59 Ordnern: 1.555 Training, 395 Test und davon 347 neue Threads.

Der Cache deckt damit nur rund 13 % des ERPNext-Index ab. Die relative Modellreihenfolge ist auf diesem identischen Ausschnitt vergleichbar; absolute Produktionsquoten können sich auf dem vollständigen JMAP-Snapshot ändern."""
		),
		nbformat.v4.new_markdown_cell("## Results"),
		nbformat.v4.new_code_cell(
			"""method = "participant-gate-0.5-then-centroid"
variants = ["subject_preview", "subject_clean_text", "subject_full_text"]

def selected_row(result, variant):
    return next(
        row for row in result["results"]
        if row["method"] == method and row["text_variant"] == variant
    )

comparison = []
for model, result in results.items():
    for variant in variants:
        row = selected_row(result, variant)
        metrics = row["metrics"]["new_thread"]
        comparison.append({
            "model": model,
            "text": variant,
            "top1": metrics["top1"],
            "top3": metrics["top3"],
            "mrr": metrics["mrr"],
            "macro_top1": metrics["macro_top1"],
            "ms_per_message": row["embedding_ms_per_message"],
            "fallbacks": row["embedding_fallbacks"],
            "dimension": row["dimension"],
        })

for row in comparison:
    print(
        f'{row["model"]:20} | {row["text"]:20} | '
        f'{row["top1"]:6.2%} Top-1 | {row["top3"]:6.2%} Top-3 | '
        f'{row["macro_top1"]:6.2%} Macro | {row["ms_per_message"]:6.1f} ms | '
        f'{row["fallbacks"]:2d} Fallbacks'
    )"""
		),
		nbformat.v4.new_markdown_cell("### Direkter Rohtextvergleich"),
		nbformat.v4.new_code_cell(
			"""raw_rows = [row for row in comparison if row["text"] == "subject_full_text"]
raw_rows.sort(key=lambda row: (-row["top1"], -row["top3"], -row["macro_top1"]))
for row in raw_rows:
    top1_count = round(row["top1"] * profile["new_thread_test_messages"])
    top3_count = round(row["top3"] * profile["new_thread_test_messages"])
    print(
        f'{row["model"]:20} | {top1_count:3d}/347 Top-1 | {top3_count:3d}/347 Top-3 | '
        f'{row["macro_top1"]:6.2%} Macro | {row["ms_per_message"]:6.1f} ms'
    )

qwen = next(row for row in raw_rows if row["model"] == "Qwen3 0.6B Q8")
snowflake = next(row for row in raw_rows if row["model"] == "Snowflake Arctic 2")
print(f'Snowflake gegen Qwen: {100 * (snowflake["top1"] - qwen["top1"]):+.2f} pp Top-1, '
      f'{100 * (snowflake["top3"] - qwen["top3"]):+.2f} pp Top-3, '
      f'{100 * (snowflake["macro_top1"] - qwen["macro_top1"]):+.2f} pp Macro-Top-1.')
print(f'Snowflake ist {1 - snowflake["ms_per_message"] / qwen["ms_per_message"]:.1%} schneller als Qwen3-0.6B.')"""
		),
		nbformat.v4.new_markdown_cell("### Anhänge als Untergruppe"),
		nbformat.v4.new_code_cell(
			"""for model, result in results.items():
    row = selected_row(result, "subject_full_text")
    with_attachment = row["metrics"]["with_attachment"]
    without_attachment = row["metrics"]["without_attachment"]
    print(
        f'{model:20} | mit Anhang {with_attachment["top1"]:6.2%}/{with_attachment["top3"]:6.2%} '
        f'| ohne Anhang {without_attachment["top1"]:6.2%}/{without_attachment["top3"]:6.2%}'
    )"""
		),
		nbformat.v4.new_markdown_cell(
			"""Die Anhangsgruppen beziehen sich auf alle 395 Testnachrichten, nicht nur auf neue Threads. Snowflake ist besonders bei Mails ohne Anhang stark; bei Mails mit Anhang bleibt Qwen3-0.6B besser. `has_attachment` ist hier nur ein Auswertungssegment und noch kein Klassifikationsmerkmal."""
		),
		nbformat.v4.new_markdown_cell("### Betriebseigenschaften und EmbeddingGemma"),
		nbformat.v4.new_code_cell(
			"""model_specs = {
    "Qwen3 0.6B Q8": {"file_mb": 639, "loaded_mb": 1500, "context": 4096},
    "Qwen3 4B Q4": {"file_mb": 2500, "loaded_mb": 3900, "context": 4096},
    "Jina v2 Base DE": {"file_mb": 322, "loaded_mb": 358, "context": 4096},
    "Snowflake Arctic 2": {"file_mb": 1200, "loaded_mb": 1200, "context": 4096},
}
for model, spec in model_specs.items():
    raw = next(row for row in raw_rows if row["model"] == model)
    print(f'{model:20} | Datei {spec["file_mb"]:4d} MB | geladen {spec["loaded_mb"]:4d} MB | '
          f'Kontext {spec["context"]:4d} | {raw["fallbacks"]} Rohtext-Fallbacks')

print("EmbeddingGemma:", embeddinggemma_failure["status"], "-", embeddinggemma_failure["reason"])
print("EmbeddingGemma-Rohtexte oder Vektoren gespeichert:", embeddinggemma_failure["raw_message_content_stored"])"""
		),
		nbformat.v4.new_markdown_cell("### Validation checks"),
		nbformat.v4.new_code_cell(
			"""split_keys = [
    "source_messages_before_full_text_filter", "source_messages", "train_messages",
    "test_messages", "new_thread_test_messages", "eligible_folders",
    "excluded_sparse_messages", "attachment_messages", "embedding_input_max_characters",
]
for result in results.values():
    assert all(result[key] == baseline[key] for key in split_keys)
    assert result["full_text_profile"] == baseline["full_text_profile"]
    assert result["embedding_input_max_characters"] == 6000
    assert result["train_messages"] + result["test_messages"] + result["excluded_sparse_messages"] == result["source_messages"]
    for row in result["results"]:
        for metrics in row["metrics"].values():
            assert 0 <= metrics["top1"] <= metrics["top3"] <= 1
            assert 0 <= metrics["macro_top1"] <= 1

assert embeddinggemma_failure["completed_result"] is False
assert embeddinggemma_failure["raw_message_content_stored"] is False

n = profile["new_thread_test_messages"]
approx_margin = 1.96 * math.sqrt(0.78 * 0.22 / n)
print(f"Konsistenzprüfungen bestanden; identischer Split und Text-Cap. Ungefähre 95%-Fehlerspanne einer Top-3-Quote nahe 78 %: ±{approx_margin:.1%}.")"""
		),
		nbformat.v4.new_markdown_cell(
			"""## Takeaways

1. **Snowflake Arctic Embed 2 ist der beste Shadow-Test-Kandidat.** Es bindet die Qwen-Baseline bei Top-1, liefert vier zusätzliche Top-3-Treffer, eine bessere Macro-Quote und ist schneller.
2. **Der Vorsprung ist klein.** +1,15 Prozentpunkte Top-3 liegen klar innerhalb der ungefähren Stichprobenunsicherheit. Ohne gespeicherte Einzelvorhersagen ist kein gepaarter McNemar-Test möglich.
3. **Qwen3-4B lohnt sich hier nicht.** Das größere Modell ist mehr als dreimal so langsam wie Qwen3-0.6B und verliert acht Top-1-Treffer. Öffentliche MTEB-Werte übertragen sich nicht automatisch auf diese Ordnerklassifikation.
4. **Jina DE ist der Effizienzkandidat.** Es ist rund dreimal so schnell wie Qwen3-0.6B und sehr klein, verliert aber sechs Top-1-Treffer und zeigte einzelne Kontext-Fallbacks.
5. **EmbeddingGemma passt nicht zum aktuellen Volltextpfad.** Der 2k-Kontext und Ollamas Verhalten bei tokenreichen Mails machen es ohne tokenbasiertes Vortrunkieren oder Chunking betrieblich unattraktiv.

### Was das Internet-Research zusätzlich zeigt

Qwens offizielle Modellkarte berichtet deutlich bessere allgemeine MTEB-Werte für 4B als für 0.6B und empfiehlt aufgabenspezifische englische Instructions. Der lokale Test zeigt trotzdem keinen Vorteil. Die dokumentierte Instruction ist primär für Query-gegen-Dokument-Retrieval gedacht; unser symmetrischer Ordner-Centroid-Ansatz sollte deshalb nicht ungeprüft auf beiden Seiten damit verändert werden.

### Metadaten mit dem größten nächsten Hebel

- Absender- und Empfängerdomänen als Rückfall für neue Einzeladressen.
- Explizite Richtung eingehend/ausgehend und der externe Korrespondent.
- Anhanganzahl, Dateiname, MIME-Typ, Dateigröße und später lokal extrahierter Dokumenttext.
- `List-ID`, `In-Reply-To`, `References`, Priorität und Keywords.
- Hierarchische Entscheidung: erst Objekt/Mietvertrag, danach Unterordner.
- Nur verifizierte ERPNext-Bezüge zu Kontakt, Mietvertrag und Wohnung; Mehrdeutigkeit darf nicht geraten werden.

Die vorhandene Teilnehmerhistorie allein erreicht bereits 51,59 % Top-1 und 66,86 % Top-3. Mehr strukturierte Metadaten versprechen deshalb wahrscheinlich einen größeren Gewinn als ein noch größeres Embedding-Modell.

### Validierungsurteil: mit Vorbehalten verwendbar

Die Modellvergleiche sind auf demselben Split und Text-Cap reproduzierbar. Wegen der nur rund 13-prozentigen lokalen Textabdeckung und des kleinen Snowflake-Vorsprungs sollte vor einem Produktionswechsel der vollständige JMAP-Snapshot getestet werden. Bis dahin: Snowflake parallel Vorschläge erzeugen lassen, Nutzerentscheidungen protokollieren und niemals automatisch verschieben."""
		),
	],
)

NotebookClient(notebook, timeout=180, kernel_name="python3").execute(cwd=str(DIRECTORY))
nbformat.write(notebook, OUTPUT)
print(OUTPUT)
