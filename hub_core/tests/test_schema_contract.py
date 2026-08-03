"""High-signal schema tests for task states that must never become false green."""
import unittest
from pathlib import Path

from hub_core import Registry, validate


ROOT = Path(__file__).resolve().parents[2]


class SchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = Registry.from_dir(ROOT / "PROJECT" / "schema")

    def _task(self, **changes):
        task = {
            "id": "example:task:0001",
            "type": "task",
            "title": "Prove the boundary",
            "status": "todo",
            "version": 1,
        }
        task.update(changes)
        return task

    def test_done_requires_summary_and_evidence_pointer(self):
        errors = validate(
            self._task(status="done", verified_by=["tests passed"]), "task", self.registry
        )
        self.assertTrue(any("evidence_uri" in error for error in errors), errors)

    def test_blank_proof_fields_are_rejected(self):
        errors = validate(
            self._task(verification_command="   "), "task", self.registry
        )
        self.assertTrue(errors)
        errors = validate(
            self._task(status="done", verified_by=["   "], evidence_uri=["   "]),
            "task",
            self.registry,
        )
        self.assertTrue(errors)

    def test_substantive_done_state_validates(self):
        errors = validate(
            self._task(
                status="done",
                verified_by=["independent test run passed"],
                evidence_uri=["artifacts/test-result.txt"],
            ),
            "task",
            self.registry,
        )
        self.assertEqual([], errors)

    def test_accepted_adr_requires_substantive_prose(self):
        adr = {
            "id": "example:adr:0001",
            "type": "adr",
            "number": 1,
            "title": "Choose the storage model",
            "status": "accepted",
            "context_md": "   ",
            "decision_md": "Use the event ledger.",
            "consequences_md": "The ledger must be backed up.",
            "version": 1,
        }
        self.assertTrue(validate(adr, "adr", self.registry))
        adr["context_md"] = "Mutable task files had drifted from reality."
        self.assertEqual([], validate(adr, "adr", self.registry))


if __name__ == "__main__":
    unittest.main(verbosity=2)
