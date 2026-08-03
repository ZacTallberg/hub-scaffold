import unittest
from pathlib import Path


class WindowsLaunchAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        cls.launcher = (root / "adapters" / "windows" / "launch-worker.ps1").read_text(encoding="utf-8")
        cls.register = (root / "adapters" / "windows" / "register-worker-protocol.ps1").read_text(encoding="utf-8")

    def test_no_process_starts_before_authoritative_grant_check(self):
        verdict = self.launcher.index("--consume-authoritative")
        first_start = self.launcher.index("Start-Process")
        self.assertLess(verdict, first_start)
        self.assertIn("if ($grantExit -ne 0)", self.launcher)

    def test_worker_windows_end_with_worker_process(self):
        self.assertNotIn('"-NoExit"', self.launcher)
        self.assertIn("exit `$workerExit", self.launcher)

    def test_registry_contains_paths_not_token_value(self):
        self.assertIn("-TokenFile", self.register)
        self.assertNotIn("HUB_WRITE_TOKEN", self.register)
        self.assertIn("HKCU:\\Software\\Classes", self.register)


if __name__ == "__main__":
    unittest.main()
