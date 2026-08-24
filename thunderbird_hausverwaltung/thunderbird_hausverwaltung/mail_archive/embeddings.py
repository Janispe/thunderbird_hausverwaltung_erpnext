from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

import requests

from .providers.base import ArchiveMessage

MAX_EMBEDDING_TEXT_LENGTH = 16_000
QUOTE_HEADER_RE = re.compile(
	r"^(am .+ schrieb .+:|on .+ wrote:|von:\s|from:\s|gesendet:\s|sent:\s|betreff:\s|subject:\s)",
	re.IGNORECASE,
)


class EmbeddingError(RuntimeError):
	pass


@dataclass(frozen=True)
class EmbeddingConfig:
	provider: str
	base_url: str
	model: str
	api_key: str = ""
	timeout: int = 60


def normalize_vector(values: list[float]) -> list[float]:
	vector = [float(value) for value in values]
	norm = math.sqrt(sum(value * value for value in vector))
	if not vector or not norm or not math.isfinite(norm):
		raise EmbeddingError("Der Embedding-Dienst lieferte keinen gültigen Vektor.")
	return [value / norm for value in vector]


def cosine_similarity(first: list[float], second: list[float]) -> float:
	if not first or len(first) != len(second):
		return 0.0
	return max(min(sum(a * b for a, b in zip(first, second, strict=True)), 1.0), -1.0)


def vector_json(vector: list[float]) -> str:
	return json.dumps(vector, separators=(",", ":"), ensure_ascii=True)


def parse_vector(value: str | None) -> list[float]:
	if not value:
		return []
	try:
		decoded = json.loads(value)
	except (TypeError, ValueError):
		return []
	if not isinstance(decoded, list):
		return []
	try:
		return [float(item) for item in decoded]
	except (TypeError, ValueError):
		return []


def clean_message_text(text: str) -> str:
	"""Remove quoted history and signatures while keeping the newly written message."""
	lines: list[str] = []
	blank = False
	for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
		line = raw_line.strip()
		if line == "--" or line.startswith("-- "):
			break
		if line.startswith(">") or QUOTE_HEADER_RE.match(line):
			break
		if not line:
			if lines and not blank:
				lines.append("")
			blank = True
			continue
		blank = False
		lines.append(line)
	return "\n".join(lines).strip()


def message_embedding_text(message: ArchiveMessage) -> str:
	sender = ", ".join(
		filter(None, (f"{item.get('name', '')} <{item.get('email', '')}>".strip() for item in message.sender))
	)
	body = clean_message_text(message.text_body) or message.preview
	text = f"Betreff: {message.subject}\nAbsender: {sender}\n\n{body}".strip()
	return text[:MAX_EMBEDDING_TEXT_LENGTH]


def content_hash(text: str, model_key: str) -> str:
	return hashlib.sha256(f"{model_key}\0{text}".encode("utf-8")).hexdigest()


class EmbeddingClient:
	def __init__(self, config: EmbeddingConfig) -> None:
		self.config = config

	@property
	def model_key(self) -> str:
		endpoint_hash = hashlib.sha256(self.config.base_url.rstrip("/").encode()).hexdigest()[:12]
		return f"{self.config.provider}:{self.config.model}:{endpoint_hash}"

	def embed(self, texts: list[str]) -> list[list[float]]:
		if not texts:
			return []
		if self.config.provider == "Ollama":
			return self._embed_ollama(texts)
		if self.config.provider == "OpenAI-kompatibel":
			return self._embed_openai_compatible(texts)
		raise EmbeddingError(f"Nicht unterstützter Embedding-Provider: {self.config.provider}")

	def _headers(self) -> dict[str, str]:
		headers = {"Accept": "application/json", "Content-Type": "application/json"}
		if self.config.api_key:
			headers["Authorization"] = f"Bearer {self.config.api_key}"
		return headers

	def _post(self, url: str, payload: dict[str, Any]) -> requests.Response:
		try:
			response = requests.post(
				url,
				headers=self._headers(),
				json=payload,
				timeout=max(self.config.timeout, 5),
			)
		except (requests.ConnectionError, requests.Timeout) as exc:
			raise EmbeddingError("Der Embedding-Dienst ist nicht erreichbar.") from exc
		return response

	def _embed_ollama(self, texts: list[str]) -> list[list[float]]:
		base_url = self.config.base_url.rstrip("/")
		response = self._post(f"{base_url}/api/embed", {"model": self.config.model, "input": texts})
		if response.status_code == 404:
			# Compatibility with older Ollama releases.
			vectors = []
			for text in texts:
				legacy = self._post(
					f"{base_url}/api/embeddings", {"model": self.config.model, "prompt": text}
				)
				self._raise_response(legacy)
				vectors.append(normalize_vector(legacy.json().get("embedding") or []))
			return vectors
		self._raise_response(response)
		vectors = response.json().get("embeddings") or []
		if len(vectors) != len(texts):
			raise EmbeddingError("Der Embedding-Dienst lieferte eine unvollständige Antwort.")
		return [normalize_vector(vector) for vector in vectors]

	def _embed_openai_compatible(self, texts: list[str]) -> list[list[float]]:
		response = self._post(
			f"{self.config.base_url.rstrip('/')}/v1/embeddings",
			{"model": self.config.model, "input": texts},
		)
		self._raise_response(response)
		data = sorted(response.json().get("data") or [], key=lambda item: item.get("index", 0))
		if len(data) != len(texts):
			raise EmbeddingError("Der Embedding-Dienst lieferte eine unvollständige Antwort.")
		return [normalize_vector(item.get("embedding") or []) for item in data]

	def _raise_response(self, response: requests.Response) -> None:
		if response.ok:
			return
		if response.status_code in {401, 403}:
			raise EmbeddingError("Die Anmeldung am Embedding-Dienst ist fehlgeschlagen.")
		raise EmbeddingError(f"Embedding-Aufruf fehlgeschlagen (HTTP {response.status_code}).")


def get_embedding_client(account: Any) -> EmbeddingClient:
	api_key = ""
	if getattr(account, "embedding_api_key", None):
		api_key = account.get_password("embedding_api_key") or ""
	return EmbeddingClient(
		EmbeddingConfig(
			provider=str(account.embedding_provider or "Ollama"),
			base_url=str(account.embedding_base_url or "").strip(),
			model=str(account.embedding_model or "").strip(),
			api_key=api_key,
			timeout=max(int(account.embedding_timeout or 60), 5),
		)
	)
