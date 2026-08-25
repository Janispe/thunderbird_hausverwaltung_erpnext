from __future__ import annotations

import json
import math
from pathlib import Path

import nbformat
from nbclient import NotebookClient


DIRECTORY = Path(__file__).parent
OUTPUT = DIRECTORY / "mail_model_refresh.ipynb"


notebook = nbformat.v4.new_notebook(
	metadata={
		"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
		"language_info": {"name": "python", "version": "3"},
	},
	cells=[
		nbformat.v4.new_markdown_cell(
			"""# Aktualisierter Mail-Ablagebenchmark

## tl;dr

- Auf dem gewachsenen Volltextbestand bleibt **Snowflake Arctic Embed 2** die beste Wahl.
- Mit Teilnehmerhistorie bei 0,5 erreicht Snowflake auf 426 neuen Threads **65,96 % Top-1 und 80,75 % Top-3**. Qwen3-Embedding 0.6B erreicht **64,79 % und 77,93 %**.
- Das sind für Snowflake **5 zusätzliche richtige Erstvorschläge und 12 zusätzliche Top-3-Treffer**.
- Falls möglichst oft der richtige Ordner unter drei Vorschlägen stehen soll, erreicht Snowflake mit Teilnehmer-Schwelle 0,7 **82,16 % Top-3**, verliert dabei aber acht richtige Erstvorschläge gegenüber 0,5.
- Empfehlung: Snowflake mit Teilnehmer-Schwelle 0,5 als Standard; weiterhin nur Vorschläge, keine automatische Ablage."""
		),
		nbformat.v4.new_markdown_cell(
			"""## Context & Methods

Der Lauf wiederholt die zwei zuvor stärksten Modelle auf dem inzwischen größeren ERPNext-Archivindex.

### Key Assumptions

- Datenstand: 25.08.2026, Europe/Berlin.
- Ältere 80 % je Ordner bilden das Training, jüngere 20 % den Test.
- Ordner mit weniger als acht verfügbaren Volltextnachrichten werden ausgeschlossen.
- Die Hauptkohorte enthält ausschließlich Testmails aus Threads, die im Training nicht vorkamen.
- Beide Modelle erhalten dieselben E-Mails, Labels, Metadaten und vollständigen Nachrichtentexte.
- Eingaben sind auf 6.000 Zeichen begrenzt; damit werden mindestens 95 % der verfügbaren Volltexte nicht gekürzt.
- Volltexte stammen aus dem lokalen Thunderbird-Cache. Sie wurden weder persistiert noch in dieses Artefakt übernommen.
- Primäre, vorab festgelegte Methode ist `participant-gate-0.5-then-centroid`. Die Schwelle 0,7 wird nur als transparenter Top-3-Trade-off gezeigt."""
		),
		nbformat.v4.new_markdown_cell("## Data"),
		nbformat.v4.new_code_cell(
			"""from pathlib import Path
import json
import math

current_files = {
    "Snowflake Arctic Embed 2": "snowflake_arctic_embed2_result.json",
    "Qwen3 Embedding 0.6B": "qwen3_06b_q8_result.json",
}
current = {name: json.loads(Path(path).read_text(encoding="utf-8")) for name, path in current_files.items()}
previous_dir = Path("../mail_model_comparison_2026-08-25")
previous = {
    "Snowflake Arctic Embed 2": json.loads((previous_dir / "snowflake_arctic_embed2_result.json").read_text(encoding="utf-8")),
    "Qwen3 Embedding 0.6B": json.loads((previous_dir / "qwen3_06b_q8_result.json").read_text(encoding="utf-8")),
}

baseline = current["Snowflake Arctic Embed 2"]
profile = {
    "indexed_messages": baseline["source_messages_before_full_text_filter"],
    "matched_rows": baseline["full_text_profile"]["fetched_messages"],
    "nonempty_full_text": baseline["source_messages"],
    "train_messages": baseline["train_messages"],
    "test_messages": baseline["test_messages"],
    "new_thread_test_messages": baseline["new_thread_test_messages"],
    "eligible_folders": baseline["eligible_folders"],
    "excluded_sparse_messages": baseline["excluded_sparse_messages"],
}
profile"""
		),
		nbformat.v4.new_markdown_cell(
			"""Von 17.087 indexierten Archivnachrichten besitzen 2.473 einen zugeordneten, nichtleeren lokalen Volltext. Nach dem ordnerweisen Split stehen 1.866 Trainings- und 474 Testmails aus 64 geeigneten Ordnern zur Verfügung; 426 Testmails stammen aus neuen Threads.

Gegenüber dem vorherigen Lauf wächst die auswertbare Volltextmenge von 2.082 auf 2.473 E-Mails (+18,8 %), die Haupttestkohorte von 347 auf 426 E-Mails (+22,8 %)."""
		),
		nbformat.v4.new_markdown_cell("## Results"),
		nbformat.v4.new_code_cell(
			"""def row_for(result, method):
    return next(row for row in result["results"] if row["method"] == method)

primary_method = "participant-gate-0.5-then-centroid"
for model, result in current.items():
    row = row_for(result, primary_method)
    metrics = row["metrics"]["new_thread"]
    n = metrics["n"]
    print(
        f'{model:29} | {round(metrics["top1"] * n):3d}/{n} Top-1 ({metrics["top1"]:.2%}) | '
        f'{round(metrics["top3"] * n):3d}/{n} Top-3 ({metrics["top3"]:.2%}) | '
        f'Macro {metrics["macro_top1"]:.2%} | {row["embedding_ms_per_message"]:.1f} ms/Mail'
    )"""
		),
		nbformat.v4.new_markdown_cell("### Veränderung gegenüber dem vorherigen Lauf"),
		nbformat.v4.new_code_cell(
			"""for model in current:
    now = row_for(current[model], primary_method)["metrics"]["new_thread"]
    before = row_for(previous[model], primary_method)["metrics"]["new_thread"]
    print(
        f'{model:29} | Top-1 {100 * (now["top1"] - before["top1"]):+.2f} pp | '
        f'Top-3 {100 * (now["top3"] - before["top3"]):+.2f} pp | '
        f'Macro {100 * (now["macro_top1"] - before["macro_top1"]):+.2f} pp'
    )"""
		),
		nbformat.v4.new_markdown_cell("### Snowflake: Top-1 gegen Top-3 abwägen"),
		nbformat.v4.new_code_cell(
			"""snowflake = current["Snowflake Arctic Embed 2"]
for method in ("participant-gate-0.5-then-centroid", "participant-gate-0.7-then-centroid"):
    metrics = row_for(snowflake, method)["metrics"]["new_thread"]
    n = metrics["n"]
    print(
        f'{method:39} | {round(metrics["top1"] * n):3d}/{n} Top-1 | '
        f'{round(metrics["top3"] * n):3d}/{n} Top-3 | Macro {metrics["macro_top1"]:.2%}'
    )"""
		),
		nbformat.v4.new_markdown_cell("### Anhänge als Untergruppe"),
		nbformat.v4.new_code_cell(
			"""for model, result in current.items():
    metrics = row_for(result, primary_method)["metrics"]
    with_attachment = metrics["with_attachment"]
    without_attachment = metrics["without_attachment"]
    print(
        f'{model:29} | mit Anhang {with_attachment["top1"]:.2%}/{with_attachment["top3"]:.2%} | '
        f'ohne Anhang {without_attachment["top1"]:.2%}/{without_attachment["top3"]:.2%}'
    )"""
		),
		nbformat.v4.new_markdown_cell(
			"""Qwen hat bei Mails mit Anhang einen kleinen Top-1-Vorteil, Snowflake ist bei Mails ohne Anhang deutlich stärker. Ein Zwei-Modell-Betrieb wäre dafür jedoch unverhältnismäßig: Auf allen 474 Testmails würde die Auswahl nach Anhang nur drei zusätzliche Top-1-Treffer gegenüber Snowflake allein liefern."""
		),
		nbformat.v4.new_markdown_cell("### Validation checks"),
		nbformat.v4.new_code_cell(
			"""comparison_keys = [
    "source_messages_before_full_text_filter", "source_messages", "train_messages",
    "test_messages", "new_thread_test_messages", "eligible_folders",
    "excluded_sparse_messages", "embedding_input_max_characters",
]
first = next(iter(current.values()))
for result in current.values():
    assert all(result[key] == first[key] for key in comparison_keys)
    assert result["full_text_profile"] == first["full_text_profile"]
    assert result["embedding_input_max_characters"] == 6000
    assert result["train_messages"] + result["test_messages"] + result["excluded_sparse_messages"] == result["source_messages"]
    assert len({row["text_variant"] for row in result["results"] if row["model"] != "rules-only"}) == 1
    for row in result["results"]:
        for metrics in row["metrics"].values():
            assert 0 <= metrics["top1"] <= metrics["top3"] <= 1
            assert 0 <= metrics["macro_top1"] <= 1

snow = row_for(current["Snowflake Arctic Embed 2"], primary_method)["metrics"]["new_thread"]
qwen = row_for(current["Qwen3 Embedding 0.6B"], primary_method)["metrics"]["new_thread"]
assert snow["top1"] > qwen["top1"] and snow["top3"] > qwen["top3"]

n = profile["new_thread_test_messages"]
margin = 1.96 * math.sqrt(0.8 * 0.2 / n)
print(f"Konsistenzprüfungen bestanden. Ungefähre 95%-Fehlerspanne einer Quote nahe 80 %: ±{margin:.1%}.")"""
		),
		nbformat.v4.new_markdown_cell(
			"""## Takeaways

1. **Snowflake bleibt die beste Standardwahl.** Der Vorteil gegenüber Qwen ist im größeren Lauf konsistenter: +1,17 Prozentpunkte Top-1, +2,82 Prozentpunkte Top-3 und +2,35 Prozentpunkte Macro-Top-1.
2. **Teilnehmerhistorie ist wichtig.** Snowflake steigt gegenüber dem reinen Ordner-Zentroid von 59,62 % auf 65,96 % Top-1.
3. **Schwelle 0,5 ist der ausgewogene Standard.** Schwelle 0,7 gewinnt sechs zusätzliche Top-3-Treffer, verliert aber acht Top-1-Treffer.
4. **Nicht automatisch verschieben.** Der erste Vorschlag ist bei rund einem Drittel der neuen Threads weiterhin falsch.
5. **Datenabdeckung bleibt die größte Einschränkung.** Lokale Volltexte decken nur 14,5 % des ERPNext-Index ab. Die Richtung ist belastbarer als vorher, aber noch kein Vollarchivtest.

### Validierungsurteil: für Vorschläge verwendbar

Der identische Split, die gleiche Textbegrenzung und die gleiche Metadatenlogik machen den Modellvergleich fair. Die Snowflake-Empfehlung ist robust genug für den Vorschlagsbetrieb. Ein automatisches Verschieben wäre mit der beobachteten Top-1-Fehlerrate weiterhin nicht vertretbar."""
		),
	],
)

NotebookClient(notebook, timeout=180, kernel_name="python3").execute(cwd=str(DIRECTORY))
nbformat.write(notebook, OUTPUT)
print(OUTPUT)
