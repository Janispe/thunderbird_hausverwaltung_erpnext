from types import SimpleNamespace
from unittest import TestCase

from .problems import _tenant_folder_candidates


def folder(
	mailbox_id: str,
	parent_id: str,
	name: str,
	*,
	folder_type: str = "Automatisch",
) -> SimpleNamespace:
	return SimpleNamespace(
		provider_mailbox_id=mailbox_id,
		parent_mailbox_id=parent_id,
		folder_name=name,
		folder_type=folder_type,
	)


class TestArchiveProblems(TestCase):
	def test_tenant_candidates_include_current_and_historical_roots_only(self) -> None:
		root = folder("ROOT", "", "Mieter G", folder_type="Mieter-Wurzelordner")
		current = folder("CURRENT", "ROOT", "01-VH-Mieter")
		old_container = folder("OLD", "ROOT", "00-Alte Mieter", folder_type="Strukturordner")
		historical = folder("HIST", "OLD", "01-VH-Altmieter")
		ignored = folder("IGNORE", "ROOT", "Hauswart", folder_type="Strukturordner")
		ignored_child = folder("IGNORE-CHILD", "IGNORE", "Hauswart alt")
		folders_by_parent = {
			"ROOT": [current, old_container, ignored],
			"OLD": [historical],
			"IGNORE": [ignored_child],
		}

		self.assertEqual(
			[
				(item.provider_mailbox_id, historical)
				for item, historical in _tenant_folder_candidates(root, folders_by_parent)
			],
			[("CURRENT", False), ("HIST", True)],
		)

	def test_explicit_structure_folder_suppresses_false_positive(self) -> None:
		root = folder("ROOT", "", "Mieter W", folder_type="Mieter-Wurzelordner")
		legal = folder("LEGAL", "ROOT", "Rechtsberatung", folder_type="Strukturordner")

		self.assertEqual(_tenant_folder_candidates(root, {"ROOT": [legal]}), [])
