from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ChangeStateUnavailable(RuntimeError):
	"""The provider can no longer calculate changes from a persisted state."""


@dataclass(frozen=True)
class ArchiveMailbox:
	id: str
	name: str
	parent_id: str | None = None
	role: str | None = None
	sort_order: int = 0
	total_emails: int = 0
	unread_emails: int = 0


@dataclass(frozen=True)
class ArchiveMessage:
	id: str
	thread_id: str
	mailbox_ids: tuple[str, ...]
	rfc_message_ids: tuple[str, ...] = ()
	in_reply_to: tuple[str, ...] = ()
	references: tuple[str, ...] = ()
	subject: str = ""
	sender: tuple[dict[str, str], ...] = ()
	to: tuple[dict[str, str], ...] = ()
	cc: tuple[dict[str, str], ...] = ()
	received_at: str | None = None
	preview: str = ""
	text_body: str = ""
	has_attachment: bool = False
	raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class ChangeSet:
	created: tuple[str, ...]
	updated: tuple[str, ...]
	destroyed: tuple[str, ...]
	new_state: str
	has_more_changes: bool = False


class MailArchiveProvider(ABC):
	"""Stable boundary between filing logic and a concrete mail protocol."""

	@abstractmethod
	def test_connection(self) -> dict[str, Any]:
		pass

	@abstractmethod
	def list_mailboxes(self) -> list[ArchiveMailbox]:
		pass

	@abstractmethod
	def query_message_ids(
		self, *, mailbox_id: str | None = None, position: int = 0, limit: int = 500
	) -> tuple[list[str], int | None, str | None]:
		pass

	@abstractmethod
	def get_messages(self, ids: list[str]) -> tuple[list[ArchiveMessage], str | None]:
		pass

	@abstractmethod
	def get_current_state(self) -> str:
		pass

	@abstractmethod
	def find_message_by_rfc_id(self, rfc_message_id: str) -> ArchiveMessage | None:
		pass

	@abstractmethod
	def get_changes(self, since_state: str, *, max_changes: int = 500) -> ChangeSet:
		pass

	@abstractmethod
	def move_message(self, message_id: str, target_mailbox_id: str) -> None:
		pass
