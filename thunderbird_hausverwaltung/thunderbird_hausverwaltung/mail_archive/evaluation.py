from __future__ import annotations

import gzip
import json
import math
import time
from collections import Counter, defaultdict
from typing import Any

import frappe
import numpy as np
import requests

from .embeddings import clean_message_text
from .providers import get_provider

DEFAULT_MODELS = ("nomic-embed-text", "bge-m3", "qwen3-embedding:0.6b")
FULL_TEXT_VARIANTS = {"subject_clean_text", "subject_full_text"}


def _email_from_participant(value: Any) -> str:
	if isinstance(value, dict):
		value = value.get("email", "")
	return str(value or "").strip().casefold()


def _participant_addresses(row: Any) -> dict[str, tuple[str, ...]]:
	"""Return normalized From/To/Cc addresses stored by the JMAP importer."""
	participants = getattr(row, "participants", None)
	if isinstance(participants, str):
		try:
			participants = json.loads(participants)
		except (TypeError, ValueError):
			participants = {}
	if not isinstance(participants, dict):
		participants = {}
	result: dict[str, tuple[str, ...]] = {}
	for role in ("from", "to", "cc"):
		values = participants.get(role) or []
		if not isinstance(values, (list, tuple)):
			values = [values]
		addresses = tuple(dict.fromkeys(filter(None, (_email_from_participant(item) for item in values))))
		result[role] = addresses
	if not result["from"]:
		sender = _email_from_participant(getattr(row, "sender_email", ""))
		result["from"] = (sender,) if sender else ()
	return result


def _participant_coverage(rows: list[Any]) -> dict[str, int | float]:
	counts = Counter()
	for row in rows:
		addresses = _participant_addresses(row)
		for role in ("from", "to", "cc"):
			if addresses[role]:
				counts[f"with_{role}"] += 1
		if addresses["to"] or addresses["cc"]:
			counts["with_recipient"] += 1
		if addresses["from"] or addresses["to"] or addresses["cc"]:
			counts["with_any_participant"] += 1
	total = len(rows)
	return {
		"messages": total,
		**{
			key: counts[key]
			for key in ("with_from", "with_to", "with_cc", "with_recipient", "with_any_participant")
		},
		"recipient_coverage": round(counts["with_recipient"] / total, 4) if total else 0.0,
		"participant_coverage": round(counts["with_any_participant"] / total, 4) if total else 0.0,
	}


def _infer_own_addresses(
	train: list[Any],
	*,
	configured: set[str] | None = None,
	max_inferred: int = 3,
) -> tuple[set[str], dict[str, int]]:
	"""Infer mailbox identities from broad two-way use, using training rows only."""
	from_counts: Counter[str] = Counter()
	to_counts: Counter[str] = Counter()
	folders: dict[str, set[str]] = defaultdict(set)
	for row in train:
		label = str(row.actual_mailbox_id)
		addresses = _participant_addresses(row)
		for address in addresses["from"]:
			from_counts[address] += 1
			folders[address].add(label)
		for address in (*addresses["to"], *addresses["cc"]):
			to_counts[address] += 1
			folders[address].add(label)
	minimum_folder_coverage = max(10, int(len({str(row.actual_mailbox_id) for row in train}) * 0.2))
	candidates = [
		address
		for address in set(from_counts) | set(to_counts)
		if from_counts[address] >= 10
		and to_counts[address] >= 10
		and len(folders[address]) >= minimum_folder_coverage
	]
	candidates.sort(
		key=lambda address: (
			-min(from_counts[address], to_counts[address]),
			-len(folders[address]),
			-(from_counts[address] + to_counts[address]),
			address,
		)
	)
	configured_addresses = {_email_from_participant(item) for item in (configured or set())}
	configured_addresses.discard("")
	selected = configured_addresses | set(candidates[:max_inferred])
	return selected, {
		"configured_own_addresses": len(configured_addresses),
		"inferred_own_addresses": len(set(candidates[:max_inferred]) - configured_addresses),
		"own_address_candidates": len(candidates),
	}


def _row_address_weights(
	row: Any,
	*,
	mode: str,
	own_addresses: set[str] | None = None,
) -> dict[str, float]:
	addresses = _participant_addresses(row)
	weighted: dict[str, float] = defaultdict(float)
	if mode == "recipient":
		role_weights = {"to": 1.0, "cc": 0.5}
	elif mode == "participant":
		role_weights = {"from": 1.0, "to": 1.0, "cc": 0.5}
	elif mode == "correspondent":
		own = own_addresses or set()
		outbound = bool(set(addresses["from"]) & own)
		if outbound:
			role_weights = {"to": 1.0, "cc": 0.5}
		else:
			role_weights = {"from": 1.0}
	else:
		raise ValueError(f"Unbekannter Adressmodus: {mode}")
	for role, role_weight in role_weights.items():
		for address in addresses[role]:
			if address not in (own_addresses or set()):
				weighted[address] += role_weight
	return dict(weighted)


def _address_history_model(
	train: list[Any],
	*,
	mode: str,
	use_idf: bool,
	own_addresses: set[str] | None = None,
) -> tuple[dict[str, Counter[str]], dict[str, float], list[str]]:
	counts: dict[str, Counter[str]] = defaultdict(Counter)
	address_folders: dict[str, set[str]] = defaultdict(set)
	folder_counts: Counter[str] = Counter()
	for row in train:
		label = str(row.actual_mailbox_id)
		folder_counts[label] += 1
		for address, weight in _row_address_weights(row, mode=mode, own_addresses=own_addresses).items():
			counts[address][label] += weight
			address_folders[address].add(label)
	folder_total = max(len(folder_counts), 1)
	idf = {
		address: math.log((1 + folder_total) / (1 + len(folders))) + 1.0 if use_idf else 1.0
		for address, folders in address_folders.items()
	}
	global_order = [label for label, _count in folder_counts.most_common()]
	return counts, idf, global_order


def _address_scores(
	row: Any,
	*,
	mode: str,
	counts: dict[str, Counter[str]],
	idf: dict[str, float],
	own_addresses: set[str] | None = None,
) -> tuple[dict[str, float], int]:
	scores: dict[str, float] = defaultdict(float)
	support = 0
	for address, row_weight in _row_address_weights(row, mode=mode, own_addresses=own_addresses).items():
		history = counts[address]
		total = sum(history.values())
		if not total:
			continue
		support = max(support, int(round(total)))
		for label, count in history.items():
			scores[label] += row_weight * idf.get(address, 1.0) * count / total
	return dict(scores), support


def _address_history_rankings(
	train: list[Any],
	test: list[Any],
	*,
	mode: str,
	use_idf: bool = True,
	own_addresses: set[str] | None = None,
	top_k: int = 10,
) -> list[list[str]]:
	counts, idf, global_order = _address_history_model(
		train, mode=mode, use_idf=use_idf, own_addresses=own_addresses
	)
	result: list[list[str]] = []
	for row in test:
		scores, _support = _address_scores(
			row,
			mode=mode,
			counts=counts,
			idf=idf,
			own_addresses=own_addresses,
		)
		ordered = [label for label, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]
		ordered.extend(label for label in global_order if label not in ordered)
		result.append(ordered[:top_k])
	return result


def _address_gate_rankings(
	semantic_rankings: list[list[str]],
	train: list[Any],
	test: list[Any],
	*,
	mode: str,
	min_count: int = 2,
	min_purity: float = 0.7,
	own_addresses: set[str] | None = None,
	top_k: int = 10,
) -> list[list[str]]:
	counts, idf, _global_order = _address_history_model(
		train, mode=mode, use_idf=True, own_addresses=own_addresses
	)
	result: list[list[str]] = []
	for row, semantic_order in zip(test, semantic_rankings):
		scores, support = _address_scores(
			row,
			mode=mode,
			counts=counts,
			idf=idf,
			own_addresses=own_addresses,
		)
		ordered = [label for label, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]
		total_score = sum(scores.values())
		purity = scores[ordered[0]] / total_score if ordered and total_score else 0.0
		if support >= min_count and purity >= min_purity:
			ordered.extend(label for label in semantic_order if label not in ordered)
			result.append(ordered[:top_k])
		else:
			result.append(semantic_order[:top_k])
	return result


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
	denominator = np.linalg.norm(matrix, axis=1, keepdims=True)
	denominator[denominator == 0] = 1.0
	return matrix / denominator


def _embedding_text(row: Any, variant: str) -> str:
	subject = str(row.subject or "").strip()
	preview = str(row.preview or "").strip()
	full_text = str(getattr(row, "full_text", "") or "").strip()
	if variant == "subject":
		return subject or "(ohne Betreff)"
	if variant == "subject_preview":
		return f"Betreff: {subject}\n\n{preview}".strip()
	if variant == "subject_clean_text":
		body = clean_message_text(full_text) or preview
		return f"Betreff: {subject}\n\n{body}".strip()
	if variant == "subject_full_text":
		return f"Betreff: {subject}\n\n{full_text or preview}".strip()
	raise ValueError(f"Unbekannte Textvariante: {variant}")


def _body_value_was_truncated(message: Any) -> bool:
	body_values = (getattr(message, "raw", None) or {}).get("bodyValues") or {}
	return any(bool(value.get("isTruncated")) for value in body_values.values() if isinstance(value, dict))


def _load_full_texts(rows: list[Any], account: Any) -> dict[str, int | float]:
	"""Load bodies transiently from JMAP; raw message text is never persisted by the benchmark."""
	provider_ids = [str(row.provider_message_id) for row in rows if row.provider_message_id]
	messages, _state = get_provider(account).get_messages(provider_ids)
	by_id = {str(message.id): message for message in messages}
	lengths: list[int] = []
	missing = 0
	truncated = 0
	for row in rows:
		message = by_id.get(str(row.provider_message_id))
		full_text = str(message.text_body if message else "")
		row["full_text"] = full_text
		if not message:
			missing += 1
		if message and _body_value_was_truncated(message):
			truncated += 1
		lengths.append(len(full_text))
	nonempty = [length for length in lengths if length]
	return {
		"source": "jmap",
		"requested_messages": len(rows),
		"fetched_messages": len(messages),
		"missing_messages": missing,
		"nonempty_full_text": len(nonempty),
		"empty_full_text": len(rows) - len(nonempty),
		"jmap_truncated_messages": truncated,
		"median_characters": int(np.median(nonempty)) if nonempty else 0,
		"p95_characters": int(np.percentile(nonempty, 95)) if nonempty else 0,
		"max_characters": max(nonempty, default=0),
	}


def _normalize_message_id(value: Any) -> str:
	return str(value or "").strip().removeprefix("<").removesuffix(">").casefold()


def _load_full_texts_from_file(rows: list[Any], path: str) -> dict[str, int | float]:
	"""Load a transient local-cache export without returning or persisting message contents."""
	with gzip.open(path, "rt", encoding="utf-8") as handle:
		body_by_message_id = json.load(handle)
	if not isinstance(body_by_message_id, dict):
		raise ValueError("Der Volltext-Export hat nicht das erwartete Format.")
	lengths: list[int] = []
	matched = 0
	for row in rows:
		message_id = _normalize_message_id(row.rfc_message_id)
		full_text = str(body_by_message_id.get(message_id) or "")
		row["full_text"] = full_text
		matched += int(message_id in body_by_message_id)
		lengths.append(len(full_text))
	nonempty = [length for length in lengths if length]
	return {
		"source": "thunderbird-local-cache",
		"requested_messages": len(rows),
		"fetched_messages": matched,
		"missing_messages": len(rows) - matched,
		"nonempty_full_text": len(nonempty),
		"empty_full_text": len(rows) - len(nonempty),
		"jmap_truncated_messages": 0,
		"median_characters": int(np.median(nonempty)) if nonempty else 0,
		"p95_characters": int(np.percentile(nonempty, 95)) if nonempty else 0,
		"max_characters": max(nonempty, default=0),
	}


def _embed(
	texts: list[str],
	*,
	model: str,
	base_url: str,
	batch_size: int = 64,
	timeout: int = 300,
	num_ctx: int | None = None,
	max_characters: int = 0,
) -> tuple[np.ndarray, float, int]:
	started = time.perf_counter()
	if max_characters > 0:
		texts = [text[:max_characters] for text in texts]
	vectors: list[list[float]] = []
	fallback_count = 0

	def embed_batch(batch: list[str]) -> list[list[float]]:
		nonlocal fallback_count
		payload: dict[str, Any] = {"model": model, "input": batch, "truncate": True}
		if num_ctx:
			payload["options"] = {"num_ctx": int(num_ctx)}
		response = requests.post(
			f"{base_url.rstrip('/')}/api/embed",
			json=payload,
			timeout=timeout,
		)
		if response.ok:
			result = response.json().get("embeddings") or []
			if len(result) != len(batch):
				raise RuntimeError(f"{model} lieferte eine unvollständige Embedding-Antwort")
			return result
		# Some Ollama/model combinations can produce a NaN for one unusual subject. Bisecting
		# keeps the benchmark usable and makes the fallback affect only that individual row.
		if len(batch) > 1:
			middle = len(batch) // 2
			return [*embed_batch(batch[:middle]), *embed_batch(batch[middle:])]
		fallback_count += 1
		fallback_payload: dict[str, Any] = {
			"model": model,
			"input": ["(nicht auswertbarer E-Mail-Text)"],
			"truncate": True,
		}
		if num_ctx:
			fallback_payload["options"] = {"num_ctx": int(num_ctx)}
		fallback = requests.post(
			f"{base_url.rstrip('/')}/api/embed",
			json=fallback_payload,
			timeout=timeout,
		)
		fallback.raise_for_status()
		return fallback.json().get("embeddings") or []

	for offset in range(0, len(texts), batch_size):
		vectors.extend(embed_batch(texts[offset : offset + batch_size]))
	matrix = _normalize_rows(np.asarray(vectors, dtype=np.float32))
	return matrix, time.perf_counter() - started, fallback_count


def _split_rows(
	rows: list[Any], *, min_messages_per_folder: int, test_fraction: float
) -> tuple[list[Any], list[Any], dict[str, Any]]:
	by_folder: dict[str, list[Any]] = defaultdict(list)
	for row in rows:
		by_folder[str(row.actual_mailbox_id)].append(row)

	train: list[Any] = []
	test: list[Any] = []
	excluded_sparse = 0
	for folder_rows in by_folder.values():
		if len(folder_rows) < min_messages_per_folder:
			excluded_sparse += len(folder_rows)
			continue
		folder_rows.sort(key=lambda item: (item.received_at, item.name))
		test_count = max(1, int(round(len(folder_rows) * test_fraction)))
		test_count = min(test_count, len(folder_rows) - 2)
		train.extend(folder_rows[:-test_count])
		test.extend(folder_rows[-test_count:])

	train.sort(key=lambda item: (item.received_at, item.name))
	test.sort(key=lambda item: (item.received_at, item.name))
	train_threads = {str(row.thread_id) for row in train if row.thread_id}
	new_thread_count = sum(1 for row in test if not row.thread_id or str(row.thread_id) not in train_threads)
	return (
		train,
		test,
		{
			"eligible_folders": len({str(row.actual_mailbox_id) for row in train}),
			"excluded_sparse_messages": excluded_sparse,
			"new_thread_test_messages": new_thread_count,
		},
	)


def _rank_metrics(
	truth: list[str], rankings: list[list[str]], *, folders: list[str]
) -> dict[str, float | int]:
	if not truth:
		return {"n": 0, "top1": 0.0, "top3": 0.0, "mrr": 0.0, "macro_top1": 0.0}
	top1_hits = [int(bool(ranking) and ranking[0] == expected) for expected, ranking in zip(truth, rankings)]
	top3_hits = [int(expected in ranking[:3]) for expected, ranking in zip(truth, rankings)]
	reciprocal_ranks = []
	for expected, ranking in zip(truth, rankings):
		try:
			reciprocal_ranks.append(1.0 / (ranking.index(expected) + 1))
		except ValueError:
			reciprocal_ranks.append(0.0)
	by_folder: dict[str, list[int]] = defaultdict(list)
	for expected, hit in zip(truth, top1_hits):
		by_folder[expected].append(hit)
	macro = sum(sum(hits) / len(hits) for hits in by_folder.values()) / max(len(by_folder), 1)
	return {
		"n": len(truth),
		"folders": len(set(folders)),
		"top1": round(sum(top1_hits) / len(truth), 4),
		"top3": round(sum(top3_hits) / len(truth), 4),
		"mrr": round(sum(reciprocal_ranks) / len(truth), 4),
		"macro_top1": round(macro, 4),
	}


def _centroid_rankings(
	train_vectors: np.ndarray,
	test_vectors: np.ndarray,
	train_labels: list[str],
	*,
	top_k: int = 10,
) -> list[list[str]]:
	folders = sorted(set(train_labels))
	label_index = {label: index for index, label in enumerate(folders)}
	sums = np.zeros((len(folders), train_vectors.shape[1]), dtype=np.float32)
	counts = np.zeros(len(folders), dtype=np.int32)
	for vector, label in zip(train_vectors, train_labels):
		index = label_index[label]
		sums[index] += vector
		counts[index] += 1
	centroids = _normalize_rows(sums)
	rankings: list[list[str]] = []
	for offset in range(0, len(test_vectors), 256):
		scores = test_vectors[offset : offset + 256] @ centroids.T
		indices = np.argsort(-scores, axis=1)[:, :top_k]
		rankings.extend([[folders[index] for index in row] for row in indices])
	return rankings


def _knn_rankings(
	train_vectors: np.ndarray,
	test_vectors: np.ndarray,
	train_labels: list[str],
	*,
	neighbors: int = 7,
	top_k: int = 10,
) -> list[list[str]]:
	rankings: list[list[str]] = []
	neighbor_count = min(neighbors, len(train_vectors))
	for offset in range(0, len(test_vectors), 128):
		scores = test_vectors[offset : offset + 128] @ train_vectors.T
		indices = np.argpartition(-scores, neighbor_count - 1, axis=1)[:, :neighbor_count]
		for score_row, index_row in zip(scores, indices):
			folder_scores: dict[str, float] = defaultdict(float)
			for index in index_row:
				folder_scores[train_labels[int(index)]] += max(float(score_row[int(index)]), 0.0)
			rankings.append(
				[
					label
					for label, _score in sorted(folder_scores.items(), key=lambda item: (-item[1], item[0]))[
						:top_k
					]
				]
			)
	return rankings


def _history_rankings(
	train: list[Any], test: list[Any], *, fieldname: str, top_k: int = 10
) -> list[list[str]]:
	counts: dict[str, Counter[str]] = defaultdict(Counter)
	for row in train:
		value = str(getattr(row, fieldname) or "").casefold()
		if value:
			counts[value][str(row.actual_mailbox_id)] += 1
	global_order = [
		label for label, _count in Counter(str(row.actual_mailbox_id) for row in train).most_common()
	]
	rankings = []
	for row in test:
		value = str(getattr(row, fieldname) or "").casefold()
		ordered = [label for label, _count in counts[value].most_common()]
		ordered.extend(label for label in global_order if label not in ordered)
		rankings.append(ordered[:top_k])
	return rankings


def _hybrid_rankings(
	centroid_rankings: list[list[str]],
	train: list[Any],
	test: list[Any],
	*,
	top_k: int = 10,
) -> list[list[str]]:
	sender_counts: dict[str, Counter[str]] = defaultdict(Counter)
	thread_counts: dict[str, Counter[str]] = defaultdict(Counter)
	for row in train:
		label = str(row.actual_mailbox_id)
		if row.sender_email:
			sender_counts[str(row.sender_email).casefold()][label] += 1
		if row.thread_id:
			thread_counts[str(row.thread_id)][label] += 1

	result: list[list[str]] = []
	for row, semantic_order in zip(test, centroid_rankings):
		semantic_score = {label: 1.0 / (rank + 1) for rank, label in enumerate(semantic_order)}
		sender = sender_counts[str(row.sender_email or "").casefold()]
		thread = thread_counts[str(row.thread_id or "")]
		sender_total = sum(sender.values())
		thread_total = sum(thread.values())
		labels = set(semantic_order) | set(sender) | set(thread)
		scores = {}
		for label in labels:
			sender_prior = sender[label] / sender_total if sender_total else 0.0
			thread_prior = thread[label] / thread_total if thread_total else 0.0
			if len(thread) == 1 and thread_total:
				score = 0.92 * thread_prior + 0.08 * semantic_score.get(label, 0.0)
			else:
				score = 0.72 * semantic_score.get(label, 0.0) + 0.18 * sender_prior + 0.10 * thread_prior
			scores[label] = score
		result.append(
			[label for label, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]]
		)
	return result


def _sender_gate_rankings(
	semantic_rankings: list[list[str]],
	train: list[Any],
	test: list[Any],
	*,
	min_count: int = 2,
	min_purity: float = 0.7,
	top_k: int = 10,
) -> list[list[str]]:
	sender_counts: dict[str, Counter[str]] = defaultdict(Counter)
	for row in train:
		if row.sender_email:
			sender_counts[str(row.sender_email).casefold()][str(row.actual_mailbox_id)] += 1
	result: list[list[str]] = []
	for row, semantic_order in zip(test, semantic_rankings):
		counts = sender_counts[str(row.sender_email or "").casefold()]
		total = sum(counts.values())
		ordered = [label for label, _count in counts.most_common()]
		purity = counts[ordered[0]] / total if ordered and total else 0.0
		if total >= min_count and purity >= min_purity:
			ordered.extend(label for label in semantic_order if label not in ordered)
			result.append(ordered[:top_k])
		else:
			result.append(semantic_order[:top_k])
	return result


def _evaluate_cohorts(
	truth: list[str],
	rankings: list[list[str]],
	test: list[Any],
	train: list[Any],
) -> dict[str, Any]:
	all_metrics = _rank_metrics(truth, rankings, folders=truth)
	train_threads = {str(row.thread_id) for row in train if row.thread_id}
	new_indices = [
		index
		for index, row in enumerate(test)
		if not row.thread_id or str(row.thread_id) not in train_threads
	]
	new_truth = [truth[index] for index in new_indices]
	new_rankings = [rankings[index] for index in new_indices]
	attachment_indices = [index for index, row in enumerate(test) if bool(row.has_attachment)]
	without_attachment_indices = [index for index, row in enumerate(test) if not bool(row.has_attachment)]

	def metrics_at(indices: list[int]) -> dict[str, float | int]:
		cohort_truth = [truth[index] for index in indices]
		cohort_rankings = [rankings[index] for index in indices]
		return _rank_metrics(cohort_truth, cohort_rankings, folders=cohort_truth)

	return {
		"all_test": all_metrics,
		"new_thread": _rank_metrics(new_truth, new_rankings, folders=new_truth),
		"with_attachment": metrics_at(attachment_indices),
		"without_attachment": metrics_at(without_attachment_indices),
	}


def profile_participants(account_name: str) -> dict[str, int | float]:
	"""Return privacy-safe aggregate coverage of the stored participant metadata."""
	rows = frappe.get_all(
		"Mail Archive Message",
		filters={
			"archive_account": account_name,
			"status": "Archiviert",
			"actual_mailbox_id": ["!=", ""],
		},
		fields=["sender_email", "participants"],
		limit_page_length=0,
	)
	return _participant_coverage(rows)


def run_benchmark(
	account_name: str,
	models: list[str] | tuple[str, ...] | None = None,
	text_variants: list[str] | tuple[str, ...] | None = None,
	ollama_url: str = "http://172.17.0.1:11434",
	min_messages_per_folder: int = 8,
	test_fraction: float = 0.2,
	full_text_batch_size: int = 8,
	full_text_path: str = "",
	require_full_text: bool = False,
	embedding_context_length: int = 0,
	embedding_input_max_characters: int = 0,
) -> dict[str, Any]:
	"""Backtest archived folder labels without changing messages or account settings."""
	models = tuple(models or DEFAULT_MODELS)
	text_variants = tuple(text_variants or ("subject_preview",))
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
	source_messages_before_full_text_filter = len(rows)
	account = frappe.get_doc("Mail Archive Account", account_name)
	full_text_profile: dict[str, int | float] = {}
	if FULL_TEXT_VARIANTS.intersection(text_variants):
		full_text_profile = (
			_load_full_texts_from_file(rows, full_text_path)
			if full_text_path
			else _load_full_texts(rows, account)
		)
		if require_full_text:
			rows = [row for row in rows if str(getattr(row, "full_text", "") or "").strip()]
	train, test, split_info = _split_rows(
		rows, min_messages_per_folder=min_messages_per_folder, test_fraction=test_fraction
	)
	if not train or not test:
		raise RuntimeError("Nicht genügend indexierte Archivnachrichten für einen Backtest")
	truth = [str(row.actual_mailbox_id) for row in test]
	train_labels = [str(row.actual_mailbox_id) for row in train]
	results: list[dict[str, Any]] = []
	configured_own_addresses = {
		_email_from_participant(item)
		for item in str(getattr(account, "email_addresses", "") or "").replace(";", "\n").splitlines()
	}
	configured_own_addresses.discard("")
	own_addresses, own_address_diagnostics = _infer_own_addresses(
		train, configured=configured_own_addresses, max_inferred=3
	)

	for fieldname, method in (("sender_email", "sender-majority"), ("thread_id", "thread-majority")):
		started = time.perf_counter()
		rankings = _history_rankings(train, test, fieldname=fieldname)
		results.append(
			{
				"model": "rules-only",
				"text_variant": "metadata",
				"method": method,
				"embedding_seconds": 0.0,
				"scoring_seconds": round(time.perf_counter() - started, 3),
				"metrics": _evaluate_cohorts(truth, rankings, test, train),
			}
		)

	for mode in ("recipient", "participant", "correspondent"):
		started = time.perf_counter()
		rankings = _address_history_rankings(
			train,
			test,
			mode=mode,
			use_idf=True,
			own_addresses=own_addresses if mode == "correspondent" else None,
		)
		results.append(
			{
				"model": "rules-only",
				"text_variant": "metadata",
				"method": f"{mode}-history-idf",
				"embedding_seconds": 0.0,
				"scoring_seconds": round(time.perf_counter() - started, 3),
				"metrics": _evaluate_cohorts(truth, rankings, test, train),
			}
		)

	for variant in text_variants:
		texts = [_embedding_text(row, variant) for row in [*train, *test]]
		for model in models:
			vectors, embedding_seconds, fallback_count = _embed(
				texts,
				model=model,
				base_url=ollama_url,
				batch_size=full_text_batch_size if variant in FULL_TEXT_VARIANTS else 64,
				num_ctx=embedding_context_length or None,
				max_characters=embedding_input_max_characters,
			)
			train_vectors = vectors[: len(train)]
			test_vectors = vectors[len(train) :]

			started = time.perf_counter()
			centroid = _centroid_rankings(train_vectors, test_vectors, train_labels)
			centroid_seconds = time.perf_counter() - started
			results.append(
				{
					"model": model,
					"text_variant": variant,
					"method": "folder-centroid",
					"embedding_seconds": round(embedding_seconds, 3),
					"embedding_ms_per_message": round(embedding_seconds * 1000 / len(texts), 3),
					"scoring_seconds": round(centroid_seconds, 3),
					"dimension": int(vectors.shape[1]),
					"embedding_fallbacks": fallback_count,
					"metrics": _evaluate_cohorts(truth, centroid, test, train),
				}
			)

			for purity in (0.5, 0.7, 0.9):
				started = time.perf_counter()
				gated = _sender_gate_rankings(centroid, train, test, min_count=2, min_purity=purity)
				results.append(
					{
						"model": model,
						"text_variant": variant,
						"method": f"sender-gate-{purity:.1f}-then-centroid",
						"embedding_seconds": round(embedding_seconds, 3),
						"embedding_ms_per_message": round(embedding_seconds * 1000 / len(texts), 3),
						"scoring_seconds": round(time.perf_counter() - started, 3),
						"dimension": int(vectors.shape[1]),
						"embedding_fallbacks": fallback_count,
						"metrics": _evaluate_cohorts(truth, gated, test, train),
					}
				)

			for mode in ("recipient", "participant", "correspondent"):
				for purity in (0.5, 0.7, 0.9):
					started = time.perf_counter()
					gated = _address_gate_rankings(
						centroid,
						train,
						test,
						mode=mode,
						min_count=2,
						min_purity=purity,
						own_addresses=own_addresses if mode == "correspondent" else None,
					)
					results.append(
						{
							"model": model,
							"text_variant": variant,
							"method": f"{mode}-gate-{purity:.1f}-then-centroid",
							"embedding_seconds": round(embedding_seconds, 3),
							"embedding_ms_per_message": round(embedding_seconds * 1000 / len(texts), 3),
							"scoring_seconds": round(time.perf_counter() - started, 3),
							"dimension": int(vectors.shape[1]),
							"embedding_fallbacks": fallback_count,
							"metrics": _evaluate_cohorts(truth, gated, test, train),
						}
					)

			started = time.perf_counter()
			knn = _knn_rankings(train_vectors, test_vectors, train_labels)
			results.append(
				{
					"model": model,
					"text_variant": variant,
					"method": "weighted-7nn",
					"embedding_seconds": round(embedding_seconds, 3),
					"embedding_ms_per_message": round(embedding_seconds * 1000 / len(texts), 3),
					"scoring_seconds": round(time.perf_counter() - started, 3),
					"dimension": int(vectors.shape[1]),
					"embedding_fallbacks": fallback_count,
					"metrics": _evaluate_cohorts(truth, knn, test, train),
				}
			)

			started = time.perf_counter()
			hybrid = _hybrid_rankings(centroid, train, test)
			results.append(
				{
					"model": model,
					"text_variant": variant,
					"method": "centroid-sender-thread-hybrid",
					"embedding_seconds": round(embedding_seconds, 3),
					"embedding_ms_per_message": round(embedding_seconds * 1000 / len(texts), 3),
					"scoring_seconds": round(time.perf_counter() - started, 3),
					"dimension": int(vectors.shape[1]),
					"embedding_fallbacks": fallback_count,
					"metrics": _evaluate_cohorts(truth, hybrid, test, train),
				}
			)

	results.sort(
		key=lambda item: (
			-item["metrics"]["new_thread"]["top1"],
			-item["metrics"]["new_thread"]["top3"],
			item["model"],
			item["method"],
		)
	)
	return {
		"account": account_name,
		"source": "ERPNext Mail Archive Message snapshot plus transient JMAP bodies",
		"source_messages": len(rows),
		"source_messages_before_full_text_filter": source_messages_before_full_text_filter,
		"train_messages": len(train),
		"test_messages": len(test),
		"test_fraction_per_folder": test_fraction,
		"min_messages_per_folder": min_messages_per_folder,
		"embedding_context_length": embedding_context_length or None,
		"embedding_input_max_characters": embedding_input_max_characters or None,
		"own_address_diagnostics": own_address_diagnostics,
		"participant_coverage": _participant_coverage(rows),
		"attachment_messages": sum(bool(row.has_attachment) for row in rows),
		"full_text_profile": full_text_profile,
		**split_info,
		"results": results,
	}
