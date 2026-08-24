from __future__ import annotations

import json
import hashlib
import math
from collections import Counter
from typing import Any

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime

from .embeddings import cosine_similarity, normalize_vector, parse_vector, vector_json

CLASSIFIER_VERSION = "hybrid-centroid-v1"


def rebuild_folder_centroids(
	account_name: str, mailbox_ids: list[str] | set[str] | None = None
) -> dict[str, int]:
	account = frappe.get_doc("Mail Archive Account", account_name)
	endpoint_hash = hashlib.sha256(str(account.embedding_base_url or "").rstrip("/").encode()).hexdigest()[
		:12
	]
	model_key = f"{account.embedding_provider}:{account.embedding_model}:{endpoint_hash}"
	filters: dict[str, Any] = {"archive_account": account.name}
	if mailbox_ids is not None:
		mailbox_ids = list(mailbox_ids)
		if not mailbox_ids:
			return {"updated": 0}
		filters["provider_mailbox_id"] = ["in", mailbox_ids]
	folders = frappe.get_all(
		"Mail Archive Folder",
		filters=filters,
		fields=["name", "provider_mailbox_id"],
	)
	updated = 0
	for folder in folders:
		rows = frappe.get_all(
			"Mail Archive Message",
			filters={
				"archive_account": account.name,
				"actual_mailbox_id": folder.provider_mailbox_id,
				"status": "Archiviert",
				"embedding_model": model_key,
			},
			fields=["embedding"],
			limit_page_length=0,
		)
		vectors = [parse_vector(row.embedding) for row in rows]
		vectors = [vector for vector in vectors if vector]
		dimension = len(vectors[0]) if vectors else 0
		vectors = [vector for vector in vectors if len(vector) == dimension]
		if vectors:
			total = [sum(values) for values in zip(*vectors, strict=True)]
			centroid = normalize_vector(total)
		else:
			total = []
			centroid = []
		frappe.db.set_value(
			"Mail Archive Folder",
			folder.name,
			{
				"sample_count": len(vectors),
				"embedding_dimension": dimension,
				"embedding_model": model_key if vectors else "",
				"centroid_sum": vector_json(total) if total else "",
				"centroid_embedding": vector_json(centroid) if centroid else "",
			},
			update_modified=False,
		)
		updated += 1
	return {"updated": updated}


def _folder_counts(account: str, fieldname: str, value: str, excluded_message: str) -> Counter[str]:
	if not value:
		return Counter()
	rows = frappe.get_all(
		"Mail Archive Message",
		filters={
			"archive_account": account,
			fieldname: value,
			"status": "Archiviert",
			"name": ["!=", excluded_message],
		},
		pluck="actual_mailbox_id",
		limit_page_length=5000,
	)
	return Counter(item for item in rows if item)


def _reliability(sample_count: int) -> float:
	return min(math.log1p(max(sample_count, 0)) / math.log(21), 1.0)


def _unique_contract_context(sender_email: str, received_at: Any) -> dict[str, str] | None:
	"""Resolve a contract only when contact plus message date is unambiguous.

	Customer is deliberately not used as a fallback: in this domain it is the accounting identity
	of exactly one Mietvertrag, not a reusable person identity.
	"""
	if not sender_email or not frappe.db.table_exists("Vertragspartner"):
		return None
	contacts = set(
		frappe.get_all(
			"Contact Email", filters={"email_id": sender_email}, pluck="parent", limit_page_length=0
		)
	)
	contacts.update(
		frappe.get_all("Contact", filters={"email_id": sender_email}, pluck="name", limit_page_length=0)
	)
	if not contacts:
		return None
	rows = frappe.get_all(
		"Vertragspartner",
		filters={"mieter": ["in", list(contacts)], "parenttype": "Mietvertrag"},
		fields=["parent", "rolle", "eingezogen", "ausgezogen"],
		limit_page_length=0,
	)
	message_date = getdate(received_at) if received_at else getdate()
	contracts: set[str] = set()
	for row in rows:
		if row.eingezogen and getdate(row.eingezogen) > message_date:
			continue
		if row.ausgezogen and getdate(row.ausgezogen) <= message_date:
			continue
		if row.rolle == "Ausgezogen" and not row.eingezogen and not row.ausgezogen:
			continue
		contracts.add(row.parent)
	if len(contracts) != 1:
		return None
	contract_name = contracts.pop()
	values = frappe.db.get_value("Mietvertrag", contract_name, ["wohnung", "kunde"], as_dict=True)
	return {
		"mietvertrag": contract_name,
		"wohnung": str(values.wohnung or "") if values else "",
		"customer": str(values.kunde or "") if values else "",
	}


def _matches_business_context(folder: Any, context: dict[str, str] | None) -> bool:
	if not context or not folder.reference_doctype or not folder.reference_name:
		return False
	return bool(
		(folder.reference_doctype == "Mietvertrag" and folder.reference_name == context["mietvertrag"])
		or (folder.reference_doctype == "Wohnung" and folder.reference_name == context["wohnung"])
		or (folder.reference_doctype == "Customer" and folder.reference_name == context["customer"])
	)


def build_suggestions(account: Any, message_doc: Any, limit: int = 3) -> dict[str, Any]:
	message_vector = parse_vector(message_doc.embedding)
	folders = frappe.get_all(
		"Mail Archive Folder",
		filters={"archive_account": account.name, "selectable_target": 1},
		fields=[
			"name",
			"provider_mailbox_id",
			"folder_path",
			"sample_count",
			"embedding_model",
			"centroid_embedding",
			"reference_doctype",
			"reference_name",
		],
		order_by="folder_path asc",
	)
	thread_counts = _folder_counts(account.name, "thread_id", message_doc.thread_id, message_doc.name)
	sender_counts = _folder_counts(account.name, "sender_email", message_doc.sender_email, message_doc.name)
	thread_total = sum(thread_counts.values())
	sender_total = sum(sender_counts.values())
	thread_unambiguous = len(thread_counts) == 1 and thread_total > 0
	business_context = _unique_contract_context(message_doc.sender_email, message_doc.received_at)
	candidates: list[dict[str, Any]] = []
	for folder in folders:
		semantic = 0.0
		centroid = parse_vector(folder.centroid_embedding)
		if (
			message_vector
			and centroid
			and len(message_vector) == len(centroid)
			and folder.embedding_model == message_doc.embedding_model
		):
			semantic = max(cosine_similarity(message_vector, centroid), 0.0)
		thread_prior = thread_counts[folder.provider_mailbox_id] / thread_total if thread_total else 0.0
		sender_prior = sender_counts[folder.provider_mailbox_id] / sender_total if sender_total else 0.0
		business_match = _matches_business_context(folder, business_context)
		if thread_unambiguous and thread_prior:
			score = 0.92 + 0.08 * semantic
		elif business_match:
			semantic_weight = 0.45 * _reliability(folder.sample_count)
			score = 0.35 + semantic_weight * semantic + 0.15 * sender_prior + 0.05 * thread_prior
		else:
			semantic_weight = 0.72 * _reliability(folder.sample_count)
			score = semantic_weight * semantic + 0.18 * sender_prior + 0.10 * thread_prior
		reasons: list[str] = []
		if thread_prior:
			reasons.append(f"{thread_counts[folder.provider_mailbox_id]} Nachricht(en) desselben Threads")
		if sender_prior:
			reasons.append(
				f"{sender_counts[folder.provider_mailbox_id]} frühere Nachricht(en) dieses Absenders"
			)
		if semantic:
			reasons.append(f"{round(semantic * 100)} % semantische Ähnlichkeit zum Ordnerprofil")
		if business_match:
			reasons.append(f"eindeutiger Mietvertrag {business_context['mietvertrag']} zum Nachrichtendatum")
		if folder.reference_doctype and folder.reference_name:
			reasons.append(f"zugeordnet zu {folder.reference_doctype} {folder.reference_name}")
		if not reasons and not folder.sample_count:
			continue
		candidates.append(
			{
				"folder": folder.name,
				"mailbox_id": folder.provider_mailbox_id,
				"path": folder.folder_path,
				"score": round(max(min(score, 1.0), 0.0), 4),
				"confidence": round(max(min(score, 1.0), 0.0) * 100, 1),
				"reason": "; ".join(reasons),
				"reference": (
					{"doctype": folder.reference_doctype, "name": folder.reference_name}
					if folder.reference_doctype and folder.reference_name
					else None
				),
				"samples": int(folder.sample_count or 0),
			}
		)
	candidates.sort(key=lambda item: (-item["score"], item["path"].casefold()))
	candidates = candidates[: max(min(int(limit or 3), 10), 1)]
	return {
		"candidates": candidates,
		"model_version": f"{CLASSIFIER_VERSION}/{message_doc.embedding_model or 'rules-only'}",
		"reasoning": candidates[0]["reason"] if candidates else _("Noch keine belastbaren Archivbeispiele."),
	}


def create_suggestion(account: Any, message_doc: Any, limit: int = 3) -> dict[str, Any]:
	result = build_suggestions(account, message_doc, limit=limit)
	candidates = result["candidates"]
	first = candidates[0] if candidates else None
	doc = frappe.get_doc(
		{
			"doctype": "Mail Filing Suggestion",
			"archive_account": account.name,
			"archive_message": message_doc.name,
			"requested_by": frappe.session.user,
			"status": "Vorgeschlagen",
			"model_version": result["model_version"],
			"proposed_folder": first["folder"] if first else "",
			"confidence": first["confidence"] if first else 0,
			"reasoning": result["reasoning"],
			"candidates": json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
		}
	).insert(ignore_permissions=True)
	return {
		"suggestion_id": doc.name,
		"message": {
			"subject": message_doc.subject,
			"sender": message_doc.sender_email,
			"received_at": message_doc.received_at,
		},
		"candidates": candidates,
		"model_version": result["model_version"],
		"generated_at": str(now_datetime()),
	}
