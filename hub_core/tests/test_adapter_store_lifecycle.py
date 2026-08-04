"""Regression coverage for request-scoped EventStore ownership in the Django adapter."""
import ast
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "adapters" / "django" / "hub"


def _load_hub_app():
    spec = importlib.util.spec_from_file_location("portable_hub_app", ADAPTER / "hub_app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeStore:
    def __init__(self):
        self.closed = False

    def events(self):
        if self.closed:
            raise AssertionError("closed store was reused")
        return []

    def close(self):
        self.closed = True


class AdapterStoreLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _load_hub_app()

    def test_current_state_closes_only_an_owned_store(self):
        owned = FakeStore()
        with mock.patch.object(self.app, "store", return_value=owned):
            self.app.current_state()
        self.assertTrue(owned.closed)

        caller_owned = FakeStore()
        self.app.current_state(caller_owned)
        self.assertFalse(caller_owned.closed)

    def test_run_audit_closes_only_an_owned_store_even_on_failure(self):
        owned = FakeStore()
        with mock.patch.object(self.app, "store", return_value=owned), \
             mock.patch.object(self.app, "_run_audit_with_store", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self.app.run_audit()
        self.assertTrue(owned.closed)

        caller_owned = FakeStore()
        with mock.patch.object(self.app, "_run_audit_with_store", return_value={"ok": True}):
            self.app.run_audit(caller_owned)
        self.assertFalse(caller_owned.closed)

    def test_read_and_write_request_helpers_close_their_stores(self):
        """Keep the adapter assertion framework-free: inspect only the narrow ownership helpers."""
        expected = {
            "hub_api.py": {"_snapshot"},
            "hub_write.py": {"_append", "decision"},
        }
        for filename, functions in expected.items():
            tree = ast.parse((ADAPTER / filename).read_text(encoding="utf-8"))
            by_name = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
            for name in functions:
                calls = [
                    node for node in ast.walk(by_name[name])
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "close"
                ]
                self.assertTrue(calls, f"{filename}:{name} must close its owned EventStore")


if __name__ == "__main__":
    unittest.main()
