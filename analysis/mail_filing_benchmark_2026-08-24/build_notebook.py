from __future__ import annotations

from pathlib import Path

import nbformat
from nbclient import NotebookClient


OUTPUT = Path(__file__).with_name("mail_filing_benchmark.ipynb")

rows = [
	{
		"model": "BGE-M3",
		"method": "Teilnehmer-Gate 0,5 → Zentroid",
		"text": "Von/An/Cc + Betreff + Vorschau",
		"top1_new": 0.7396,
		"top3_new": 0.8171,
		"macro_top1_new": 0.7003,
		"top1_all": 0.7571,
		"ms_per_message": 5.445,
	},
	{
		"model": "BGE-M3",
		"method": "Korrespondenz-Gate 0,5 → Zentroid",
		"text": "Richtung + Betreff + Vorschau",
		"top1_new": 0.7388,
		"top3_new": 0.8347,
		"macro_top1_new": 0.7051,
		"top1_all": 0.7538,
		"ms_per_message": 5.445,
	},
	{
		"model": "Regeln",
		"method": "Teilnehmerhistorie mit IDF",
		"text": "Von/An/Cc",
		"top1_new": 0.7052,
		"top3_new": 0.7696,
		"macro_top1_new": 0.6361,
		"top1_all": 0.7199,
		"ms_per_message": 0.0,
	},
	{
		"model": "Regeln",
		"method": "Korrespondenzpartner mit IDF",
		"text": "Richtungsabhängige Adressen",
		"top1_new": 0.6832,
		"top3_new": 0.7454,
		"macro_top1_new": 0.6207,
		"top1_all": 0.6994,
		"ms_per_message": 0.0,
	},
	{
		"model": "Regeln",
		"method": "Empfängerhistorie mit IDF",
		"text": "An/Cc",
		"top1_new": 0.4023,
		"top3_new": 0.4726,
		"macro_top1_new": 0.3071,
		"top1_all": 0.4013,
		"ms_per_message": 0.0,
	},
	{
		"model": "BGE-M3",
		"method": "Sender-Gate 0,7 → Zentroid",
		"text": "Betreff + Vorschau",
		"top1_new": 0.5457,
		"top3_new": 0.6759,
		"macro_top1_new": 0.5193,
		"top1_all": 0.5686,
		"ms_per_message": 5.292,
	},
	{
		"model": "BGE-M3",
		"method": "Sender-Gate 0,5 → Zentroid",
		"text": "Betreff + Vorschau",
		"top1_new": 0.5421,
		"top3_new": 0.6832,
		"macro_top1_new": 0.5168,
		"top1_all": 0.5654,
		"ms_per_message": 5.292,
	},
	{
		"model": "Regeln",
		"method": "Absender-Mehrheit",
		"text": "Metadaten",
		"top1_new": 0.3899,
		"top3_new": 0.4492,
		"macro_top1_new": 0.3385,
		"top1_all": 0.3936,
		"ms_per_message": 0.0,
	},
	{
		"model": "BGE-M3",
		"method": "Ordner-Zentroid",
		"text": "Betreff + Vorschau",
		"top1_new": 0.3475,
		"top3_new": 0.5062,
		"macro_top1_new": 0.3341,
		"top1_all": 0.3731,
		"ms_per_message": 5.292,
	},
	{
		"model": "BGE-M3",
		"method": "gewichtete 7 Nachbarn",
		"text": "Betreff + Vorschau",
		"top1_new": 0.2999,
		"top3_new": 0.4301,
		"macro_top1_new": 0.2903,
		"top1_all": 0.3301,
		"ms_per_message": 5.292,
	},
	{
		"model": "Qwen3 Embedding 0.6B",
		"method": "Ordner-Zentroid",
		"text": "Betreff + Vorschau",
		"top1_new": 0.2612,
		"top3_new": 0.4301,
		"macro_top1_new": 0.2713,
		"top1_all": 0.2897,
		"ms_per_message": 10.260,
	},
	{
		"model": "BGE-M3",
		"method": "Ordner-Zentroid",
		"text": "nur Betreff",
		"top1_new": 0.2238,
		"top3_new": 0.3233,
		"macro_top1_new": 0.2605,
		"top1_all": 0.2494,
		"ms_per_message": 4.886,
	},
	{
		"model": "Nomic Embed Text",
		"method": "Ordner-Zentroid",
		"text": "Betreff + Vorschau",
		"top1_new": 0.1149,
		"top3_new": 0.1939,
		"macro_top1_new": 0.1230,
		"top1_all": 0.1237,
		"ms_per_message": 5.995,
	},
]

notebook = nbformat.v4.new_notebook(
	metadata={
		"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
		"language_info": {"name": "python", "version": "3"},
	},
	cells=[
		nbformat.v4.new_markdown_cell(
			"""# E-Mail-Ablage: Teilnehmer- und Empfängerhistorie

## tl;dr

- Auf **1.367 Testmails aus neuen Threads** erreicht die gemeinsame Historie aus Absender, `To` und `Cc` bereits **70,5 % Top-1 ohne Embeddings**.
- Mit BGE-M3 als Rückfall steigt Top-1 auf **74,0 %** und Top-3 auf **81,7 %**.
- Nur Empfänger (40,2 %) ist ähnlich stark wie nur Absender (39,0 %). Der große Gewinn entsteht durch die richtungsunabhängige Kombination aller Beteiligten.
- Der Befund ist für Vorschläge stark genug, aber noch nicht für automatische Ablage: Der Stalwart-Erstindex ist unvollständig und die Gate-Schwelle wurde auf demselben Testsatz gewählt."""
		),
		nbformat.v4.new_markdown_cell(
			"""## Context & Methods

Entscheidung: Welche lokale Methode soll Thunderbird Ordner vorschlagen lassen?

### Key Assumptions

- Der aktuelle Stalwart-Ordner gilt als Label.
- Pro Ordner sind die älteren 80 % Training und die jüngeren 20 % Test.
- Ordner mit weniger als acht indexierten Nachrichten werden in diesem ersten Lauf ausgeschlossen.
- Die Hauptauswertung „new thread“ entfernt Testmails, deren Thread bereits im Training vorkommt.
- Häufig über viele Ordner auftauchende Adressen werden per inverser Ordnerhäufigkeit abgewertet; `Cc` zählt halb so stark wie `To` oder `From`.
- Eigene Mailboxidentitäten werden ausschließlich aus dem Trainingsanteil abgeleitet.
- Die Gate-Schwelle wurde explorativ auf demselben Testsatz verglichen und muss später auf einem getrennten Validierungssatz festgelegt werden."""
		),
		nbformat.v4.new_markdown_cell("## Data"),
		nbformat.v4.new_code_cell(
			"""profile = {
    "source_messages": 8000,
    "unique_provider_ids": 8000,
    "folders_with_labels": 245,
    "eligible_folders": 177,
    "train_messages": 6206,
    "test_messages": 1560,
    "new_thread_test_messages": 1367,
    "sparse_folders": 68,
    "excluded_sparse_messages": 234,
    "blank_subject": 60,
    "blank_preview": 40,
    "blank_sender": 338,
    "recipient_metadata_coverage": 0.9758,
    "participant_metadata_coverage": 0.9770,
    "configured_own_addresses": 1,
    "inferred_own_addresses": 3,
    "initial_sync_completed": False,
}
profile"""
		),
		nbformat.v4.new_markdown_cell("## Results"),
		nbformat.v4.new_code_cell("benchmark_rows = " + repr(rows) + "\nlen(benchmark_rows)"),
		nbformat.v4.new_code_cell(
			"""for row in sorted(benchmark_rows, key=lambda item: item["top1_new"], reverse=True):
    print(
        f'{row["top1_new"]:6.1%} Top-1 | {row["top3_new"]:6.1%} Top-3 | '
        f'{row["model"]:22} | {row["method"]:30} | {row["text"]}'
    )"""
		),
		nbformat.v4.new_markdown_cell("### Validation checks"),
		nbformat.v4.new_code_cell(
			"""assert profile["train_messages"] + profile["test_messages"] + profile["excluded_sparse_messages"] == profile["source_messages"]
assert profile["source_messages"] == profile["unique_provider_ids"]
assert profile["new_thread_test_messages"] <= profile["test_messages"]
assert 0 <= profile["recipient_metadata_coverage"] <= profile["participant_metadata_coverage"] <= 1
for row in benchmark_rows:
    assert 0 <= row["top1_new"] <= row["top3_new"] <= 1
    assert 0 <= row["macro_top1_new"] <= 1
print("Alle Konsistenzprüfungen bestanden.")"""
		),
		nbformat.v4.new_markdown_cell(
			"""## Takeaways

1. Für die nächste Implementierung ist **Teilnehmerhistorie (`From`, `To`, `Cc`) mit BGE-M3-Rückfall** der beste Kandidat.
2. Ohne Ollama ist die Teilnehmerhistorie mit 70,5 % Top-1 bereits sinnvoll; BGE-M3 bringt weitere 3,4 Prozentpunkte.
3. Vor einer automatischen Verschiebung muss der Benchmark nach vollständigem Erstindex mit einem getrennten Validierungssatz wiederholt werden.
4. Bis dahin sollte Thunderbird drei Vorschläge anzeigen und der Benutzer bestätigt die Ablage."""
		),
	],
)

client = NotebookClient(notebook, timeout=120, kernel_name="python3")
client.execute(cwd=str(OUTPUT.parent))
nbformat.write(notebook, OUTPUT)
print(OUTPUT)
