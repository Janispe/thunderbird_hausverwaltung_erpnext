from __future__ import annotations

from typing import TYPE_CHECKING

from .base import MailArchiveProvider

if TYPE_CHECKING:
	from frappe.model.document import Document


def get_provider(account: "Document") -> MailArchiveProvider:
	provider = str(account.provider or "").strip()
	if provider == "JMAP":
		from .jmap import JMAPProvider

		return JMAPProvider.from_account(account)
	raise ValueError(f"Nicht unterstützter Mail-Archiv-Provider: {provider}")


__all__ = ["MailArchiveProvider", "get_provider"]
