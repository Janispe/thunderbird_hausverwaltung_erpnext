from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any

import frappe
import numpy as np

from thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.embeddings import (
	clean_message_text,
)
from thunderbird_hausverwaltung.thunderbird_hausverwaltung.mail_archive.evaluation import (
	_address_gate_rankings,
	_address_history_model,
	_address_scores,
	_embed,
	_embedding_text,
	_infer_own_addresses,
	_load_full_texts_from_file,
	_normalize_rows,
	_participant_addresses,
	_split_rows,
)

REPLY_RE = re.compile(r"^(re|aw)\s*:", re.IGNORECASE)
FORWARD_RE = re.compile(r"^(fwd?|wg)\s*:", re.IGNORECASE)
SYSTEM_LIKE_FOLDER_RE = re.compile(
	r"(^|[\\/])(inbox|sent|drafts?|trash|spam|junk|posteingang|gesendet|entw[uü]rfe?|papierkorb)([\\/]|$)",
	re.IGNORECASE,
)


def _folder_token(mailbox_id: str) -> str:
	return f"Ordner-{hashlib.sha256(mailbox_id.encode()).hexdigest()[:8]}"


def _path_parts(path: str) -> tuple[str, ...]:
	return tuple(part.strip().casefold() for part in re.split(r"[\\/]+", path) if part.strip())


def _segment(cases: list[dict[str, Any]]) -> dict[str, int | float]:
	n = len(cases)
	if not n:
		return {"n": 0, "top1": 0.0, "top3": 0.0, "top1_errors": 0, "top3_misses": 0}
	top1 = sum(case["top1"] for case in cases)
	top3 = sum(case["top3"] for case in cases)
	return {
		"n": n,
		"top1": round(top1 / n, 4),
		"top3": round(top3 / n, 4),
		"top1_errors": n - top1,
		"top3_misses": n - top3,
	}


def _grouped(cases: list[dict[str, Any]], fieldname: str) -> dict[str, dict[str, int | float]]:
	groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for case in cases:
		groups[str(case[fieldname])].append(case)
	return {label: _segment(rows) for label, rows in sorted(groups.items())}


def _flagged(cases: list[dict[str, Any]]) -> dict[str, dict[str, int | float]]:
	labels = sorted({flag for case in cases for flag in case["flags"]})
	return {label: _segment([case for case in cases if label in case["flags"]]) for label in labels}


def run(
	account_name: str,
	full_text_path: str,
	model: str = "snowflake-arctic-embed2",
	ollama_url: str = "http://172.17.0.1:11434",
	input_max_characters: int = 6000,
	include_private_examples: bool = False,
) -> dict[str, Any]:
	"""Diagnose the best benchmark path without persisting mail text or embeddings."""
	rows = frappe.get_all(
		"Mail Archive Message",
		filters={
			"archive_account": account_name,
			"status": "Archiviert",
			"actual_mailbox_id": ["!=", ""],
		},
		fields=[
			"name",
			"provider_message_id",
			"rfc_message_id",
			"thread_id",
			"subject",
			"sender_email",
			"participants",
			"received_at",
			"preview",
			"has_attachment",
			"actual_mailbox_id",
			"actual_folder_path",
		],
		order_by="received_at asc, name asc",
		limit_page_length=0,
	)
	indexed_messages = len(rows)
	full_text_profile = _load_full_texts_from_file(rows, full_text_path)
	rows = [row for row in rows if str(getattr(row, "full_text", "") or "").strip()]
	train, test, split_info = _split_rows(rows, min_messages_per_folder=8, test_fraction=0.2)
	train_labels = [str(row.actual_mailbox_id) for row in train]
	texts = [_embedding_text(row, "subject_full_text") for row in [*train, *test]]
	vectors, embedding_seconds, fallback_count = _embed(
		texts,
		model=model,
		base_url=ollama_url,
		batch_size=8,
		max_characters=input_max_characters,
	)
	train_vectors = vectors[: len(train)]
	test_vectors = vectors[len(train) :]

	folders = sorted(set(train_labels))
	label_index = {label: index for index, label in enumerate(folders)}
	sums = np.zeros((len(folders), train_vectors.shape[1]), dtype=np.float32)
	train_counts: Counter[str] = Counter()
	for vector, label in zip(train_vectors, train_labels):
		sums[label_index[label]] += vector
		train_counts[label] += 1
	centroids = _normalize_rows(sums)
	scores = test_vectors @ centroids.T
	semantic_indices = np.argsort(-scores, axis=1)
	semantic_rankings = [[folders[int(index)] for index in row[:10]] for row in semantic_indices]

	account = frappe.get_doc("Mail Archive Account", account_name)
	configured = {
		item.strip().casefold()
		for item in str(getattr(account, "email_addresses", "") or "").replace(";", "\n").splitlines()
		if item.strip()
	}
	own_addresses, own_address_diagnostics = _infer_own_addresses(train, configured=configured)
	final_rankings = _address_gate_rankings(
		semantic_rankings,
		train,
		test,
		mode="participant",
		min_count=2,
		min_purity=0.5,
		own_addresses=own_addresses,
	)
	participant_counts, participant_idf, _global_order = _address_history_model(
		train, mode="participant", use_idf=True, own_addresses=own_addresses
	)

	path_by_folder: dict[str, str] = {}
	for row in [*train, *test]:
		path_by_folder[str(row.actual_mailbox_id)] = str(row.actual_folder_path or "")
	train_threads = {str(row.thread_id) for row in train if row.thread_id}
	new_thread_indices = [
		index
		for index, row in enumerate(test)
		if not row.thread_id or str(row.thread_id) not in train_threads
	]

	cases: list[dict[str, Any]] = []
	private_examples: list[dict[str, Any]] = []
	for index in new_thread_indices:
		row = test[index]
		truth = str(row.actual_mailbox_id)
		ranking = final_rankings[index]
		semantic_ranking = semantic_rankings[index]
		body = str(getattr(row, "full_text", "") or "")
		cleaned = clean_message_text(body)
		subject = str(row.subject or "").strip()
		participant_scores, participant_support = _address_scores(
			row,
			mode="participant",
			counts=participant_counts,
			idf=participant_idf,
			own_addresses=own_addresses,
		)
		participant_total = sum(participant_scores.values())
		participant_purity = (
			max(participant_scores.values()) / participant_total if participant_total else 0.0
		)
		gate_used = participant_support >= 2 and participant_purity >= 0.5

		addresses = _participant_addresses(row)
		from_addresses = set(addresses["from"])
		if from_addresses & own_addresses:
			direction = "ausgehend"
		elif from_addresses:
			direction = "eingehend"
		else:
			direction = "unbekannt"

		body_length = len(body)
		if body_length <= 250:
			length_bucket = "≤250 Zeichen"
		elif body_length <= 1000:
			length_bucket = "251–1.000 Zeichen"
		elif body_length <= 4000:
			length_bucket = "1.001–4.000 Zeichen"
		elif body_length <= input_max_characters:
			length_bucket = "4.001–6.000 Zeichen"
		else:
			length_bucket = ">6.000 Zeichen (gekürzt)"

		folder_size = train_counts[truth]
		if folder_size <= 10:
			folder_size_bucket = "6–10 Trainingsmails"
		elif folder_size <= 25:
			folder_size_bucket = "11–25 Trainingsmails"
		elif folder_size <= 100:
			folder_size_bucket = "26–100 Trainingsmails"
		else:
			folder_size_bucket = ">100 Trainingsmails"

		margin = float(scores[index, semantic_indices[index, 0]] - scores[index, semantic_indices[index, 1]])
		if margin < 0.01:
			margin_bucket = "<0,01 (sehr unsicher)"
		elif margin < 0.03:
			margin_bucket = "0,01–0,03"
		elif margin < 0.07:
			margin_bucket = "0,03–0,07"
		else:
			margin_bucket = "≥0,07 (klarer Abstand)"

		flags: list[str] = []
		if not subject:
			flags.append("Ohne Betreff")
		if REPLY_RE.match(subject):
			flags.append("Antwortbetreff")
		if FORWARD_RE.match(subject):
			flags.append("Weiterleitungsbetreff")
		if body_length <= 250:
			flags.append("Sehr kurzer Text")
		if bool(row.has_attachment) and len(cleaned) <= 250:
			flags.append("Anhang mit wenig Begleittext")
		if body_length > input_max_characters:
			flags.append("Text oberhalb der 6.000-Zeichen-Grenze")
		if body_length >= 500 and len(cleaned) / max(body_length, 1) < 0.2:
			flags.append("Überwiegend Zitat/Signatur")
		if participant_support == 0:
			flags.append("Keine bekannte Teilnehmerhistorie")
		if folder_size <= 10:
			flags.append("Sehr kleiner Zielordner")

		actual_parts = _path_parts(path_by_folder.get(truth, ""))
		predicted_parts = _path_parts(path_by_folder.get(ranking[0], "")) if ranking else ()
		if truth == (ranking[0] if ranking else ""):
			hierarchy_relation = "exakt"
		elif len(predicted_parts) > len(actual_parts) and predicted_parts[: len(actual_parts)] == actual_parts:
			hierarchy_relation = "Vorschlag ist ein Unterordner des tatsächlichen Ordners"
		elif len(actual_parts) > len(predicted_parts) and actual_parts[: len(predicted_parts)] == predicted_parts:
			hierarchy_relation = "Vorschlag ist ein übergeordneter Ordner"
		elif len(actual_parts) > 1 and actual_parts[:-1] == predicted_parts[:-1]:
			hierarchy_relation = "benachbarter Ordner mit gleichem Elternordner"
		elif actual_parts and predicted_parts and actual_parts[0] == predicted_parts[0]:
			hierarchy_relation = "gleicher Hauptzweig"
		else:
			hierarchy_relation = "anderer Hauptzweig"
		actual_path = path_by_folder.get(truth, "")
		target_kind = (
			"Systemähnlicher Altordner"
			if SYSTEM_LIKE_FOLDER_RE.search(actual_path)
			else "Fachlicher Archivordner"
		)
		if len(actual_parts) <= 2:
			folder_depth = "1–2 Ebenen"
		elif len(actual_parts) == 3:
			folder_depth = "3 Ebenen"
		elif len(actual_parts) == 4:
			folder_depth = "4 Ebenen"
		else:
			folder_depth = "≥5 Ebenen"

		case = {
			"top1": int(bool(ranking) and ranking[0] == truth),
			"top3": int(truth in ranking[:3]),
			"attachment": "mit Anhang" if bool(row.has_attachment) else "ohne Anhang",
			"direction": direction,
			"length_bucket": length_bucket,
			"folder_size_bucket": folder_size_bucket,
			"margin_bucket": margin_bucket,
			"participant_history": (
				"Gate greift" if gate_used else ("Keine Historie" if participant_support == 0 else "Zu unsicher")
			),
			"target_kind": target_kind,
			"folder_depth": folder_depth,
			"semantic_top1": int(bool(semantic_ranking) and semantic_ranking[0] == truth),
			"gate_helped": int(ranking[0] == truth and semantic_ranking[0] != truth),
			"gate_harmed": int(ranking[0] != truth and semantic_ranking[0] == truth),
			"hierarchy_relation": hierarchy_relation,
			"actual_folder": _folder_token(truth),
			"predicted_folder": _folder_token(ranking[0]) if ranking else "kein Vorschlag",
			"flags": flags,
			"margin": round(margin, 4),
		}
		cases.append(case)
		if include_private_examples and not case["top3"]:
			private_examples.append(
				{
					"erpnext_name": str(row.name),
					"received_at": str(row.received_at),
					"subject": subject or "(ohne Betreff)",
					"actual_folder_path": path_by_folder.get(truth, ""),
					"predicted_folder_paths": [path_by_folder.get(item, "") for item in ranking[:3]],
					"flags": flags,
					"semantic_margin": round(margin, 4),
				}
			)

	confusions = Counter(
		(case["actual_folder"], case["predicted_folder"])
		for case in cases
		if not case["top1"]
	)
	folder_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
	for case in cases:
		folder_rows[case["actual_folder"]].append(case)
	folder_difficulty = [
		{
			"folder": folder,
			"test_messages": len(folder_cases),
			"top1": _segment(folder_cases)["top1"],
			"top1_errors": _segment(folder_cases)["top1_errors"],
		}
		for folder, folder_cases in folder_rows.items()
		if len(folder_cases) >= 3
	]
	folder_difficulty.sort(key=lambda item: (item["top1"], -item["test_messages"], item["folder"]))

	result: dict[str, Any] = {
		"account": account_name,
		"model": model,
		"method": "participant-gate-0.5-then-centroid",
		"text_variant": "subject_full_text",
		"indexed_messages": indexed_messages,
		"usable_full_text_messages": len(rows),
		"train_messages": len(train),
		"test_messages": len(test),
		"new_thread_test_messages": len(cases),
		"embedding_input_max_characters": input_max_characters,
		"embedding_seconds": round(embedding_seconds, 3),
		"embedding_ms_per_message": round(embedding_seconds * 1000 / len(texts), 3),
		"embedding_fallbacks": fallback_count,
		"full_text_profile": full_text_profile,
		"own_address_diagnostics": own_address_diagnostics,
		"split": split_info,
		"overall": _segment(cases),
		"segments": {
			"attachment": _grouped(cases, "attachment"),
			"direction": _grouped(cases, "direction"),
			"body_length": _grouped(cases, "length_bucket"),
			"folder_train_size": _grouped(cases, "folder_size_bucket"),
			"semantic_margin": _grouped(cases, "margin_bucket"),
			"participant_history": _grouped(cases, "participant_history"),
			"target_kind": _grouped(cases, "target_kind"),
			"folder_depth": _grouped(cases, "folder_depth"),
			"hierarchy_relation": _grouped(
				[case for case in cases if not case["top1"]], "hierarchy_relation"
			),
			"overlapping_flags": _flagged(cases),
		},
		"participant_gate": {
			"helped_top1": sum(case["gate_helped"] for case in cases),
			"harmed_top1": sum(case["gate_harmed"] for case in cases),
			"semantic_only": _segment(
				[{**case, "top1": case["semantic_top1"]} for case in cases]
			),
		},
		"top_confusions_anonymized": [
			{"actual": actual, "predicted": predicted, "count": count}
			for (actual, predicted), count in confusions.most_common(15)
		],
		"hardest_folders_anonymized": folder_difficulty[:15],
	}
	if include_private_examples:
		private_examples.sort(key=lambda item: (-item["semantic_margin"], item["received_at"]))
		result["private_examples"] = private_examples[:25]
	return result
