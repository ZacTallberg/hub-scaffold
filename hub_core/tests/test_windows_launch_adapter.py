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

    def test_worker_window_ends_with_the_seat_but_the_seat_does_not_end(self):
        """No stale windows - AND no single-shot seat.

        This asserted `exit $workerExit`, which pinned the defect it was written beside: the child
        invoked the worker command ONCE and exited, so a seat's entire existence was a single run.
        The intent was only "do not leave a stale terminal behind" (the -NoExit check); the exit
        line was how that happened to be achieved, and encoding the mechanism froze it.

        The contract now: the window still dies with the seat, but the seat loops - see
        patterns/worker-longevity.md."""
        self.assertNotIn('"-NoExit"', self.launcher)
        self.assertIn("while (`$true)", self.launcher)
        self.assertNotIn("\nexit `$workerExit", self.launcher)

    def test_the_seat_measures_completions_not_aliveness(self):
        """A seat that holds a claim and finishes nothing is failing, and only the ledger can say
        so - a pid, a window and a heartbeat all read green while it happens."""
        for needle, why in (
            ("seat_productivity.py", "completions are read from the ledger"),
            ("--done-count", "the per-cycle completion delta is measured"),
            ("HUB_BARREN_CYCLES", "the barren count reaches the worker, whose context is fresh"),
            ("heartbeat", "liveness is stamped before each run"),
        ):
            self.assertIn(needle, self.launcher, why)

    def test_the_prompt_does_not_tell_the_worker_to_stop(self):
        """'Continue until no ready task remains' is a terminal condition written into the words
        the worker reads: the seat stops the moment the rail is briefly empty."""
        self.assertNotIn("Continue until no ready task remains", self.launcher)
        self.assertIn("COMPLETIONS", self.launcher)

    def test_registry_contains_paths_not_token_value(self):
        self.assertIn("-TokenFile", self.register)
        self.assertNotIn("HUB_WRITE_TOKEN", self.register)
        self.assertIn("HKCU:\\Software\\Classes", self.register)


if __name__ == "__main__":
    unittest.main()
