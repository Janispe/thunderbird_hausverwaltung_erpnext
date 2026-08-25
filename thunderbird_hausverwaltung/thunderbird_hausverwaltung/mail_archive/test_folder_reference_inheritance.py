from types import SimpleNamespace
from unittest import TestCase

from .classifier import _effective_folder_reference, _matches_business_context


class TestFolderReferenceInheritance(TestCase):
	def test_descendant_inherits_nearest_contract_reference(self) -> None:
		root = SimpleNamespace(
			provider_mailbox_id="ROOT",
			parent_mailbox_id="",
			reference_doctype="Mietvertrag",
			reference_name="MV-1",
		)
		child = SimpleNamespace(
			provider_mailbox_id="CHILD",
			parent_mailbox_id="ROOT",
			reference_doctype="",
			reference_name="",
		)
		folders = {"ROOT": root, "CHILD": child}

		self.assertEqual(_effective_folder_reference(child, folders), ("Mietvertrag", "MV-1"))
		self.assertTrue(
			_matches_business_context(
				child,
				{"mietvertrag": "MV-1", "wohnung": "W-1", "customer": "C-1"},
				folders,
			)
		)

	def test_explicit_descendant_reference_overrides_parent(self) -> None:
		root = SimpleNamespace(
			provider_mailbox_id="ROOT",
			parent_mailbox_id="",
			reference_doctype="Mietvertrag",
			reference_name="MV-1",
		)
		child = SimpleNamespace(
			provider_mailbox_id="CHILD",
			parent_mailbox_id="ROOT",
			reference_doctype="Mietvertrag",
			reference_name="MV-2",
		)

		self.assertEqual(
			_effective_folder_reference(child, {"ROOT": root, "CHILD": child}),
			("Mietvertrag", "MV-2"),
		)

	def test_cyclic_parent_links_do_not_loop(self) -> None:
		first = SimpleNamespace(
			provider_mailbox_id="A",
			parent_mailbox_id="B",
			reference_doctype="",
			reference_name="",
		)
		second = SimpleNamespace(
			provider_mailbox_id="B",
			parent_mailbox_id="A",
			reference_doctype="",
			reference_name="",
		)

		self.assertEqual(_effective_folder_reference(first, {"A": first, "B": second}), ("", ""))
