import unittest
from pathlib import Path


class LaunchUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        frontend = Path(__file__).resolve().parents[1] / "frontend"
        cls.js = (frontend / "hub.js").read_text(encoding="utf-8")
        cls.html = (frontend / "hub_shell.html").read_text(encoding="utf-8")

    def test_browser_never_asks_for_or_stores_the_general_write_token(self):
        combined = (self.js + self.html).lower()
        self.assertNotIn("x-write-token", combined)
        self.assertNotIn("localstorage.setitem(\"hub-write", combined)
        self.assertNotIn("unlock", combined)

    def test_launch_is_prearmed_before_click(self):
        init = self.js[self.js.index("function initLaunchControls"):self.js.index("/* ============================ TABS")]
        self.assertIn("prepareLaunch(anchor);", init)
        self.assertIn('["pointerenter", "focusin", "touchstart"]', self.js)
        self.assertIn('meta name="csrf-token"', self.html)

    def test_ready_click_preserves_user_activation_without_popup(self):
        ready = self.js[self.js.index("function launchClick"):self.js.index("function initLaunchControls")]
        ready_branch = ready[:ready.index("event.preventDefault()")]
        self.assertNotIn("event.preventDefault()", ready_branch)
        self.assertNotIn("fetch(", ready_branch)
        self.assertNotIn("window.open", self.js)


if __name__ == "__main__":
    unittest.main()
