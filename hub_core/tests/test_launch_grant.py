import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from hub_core import launch_grant
from hub_core.process_lock import ProcessFileLock


class LaunchGrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HUB_DIR": self.tmp.name}, clear=False)
        self.env.start()
        os.environ.pop("HUB_ATTEST_SECRET", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_grant_is_bound_and_single_use(self):
        token = launch_grant.encode(launch_grant.mint(task="sample:task:0001", count=2))
        self.assertEqual((True, 2), launch_grant.consume(token, task="sample:task:0001", count=2))
        ok, reason = launch_grant.consume(token, task="sample:task:0001", count=2)
        self.assertFalse(ok)
        self.assertIn("already used", reason)

    def test_task_count_and_signature_cannot_be_reaimed(self):
        token = launch_grant.encode(launch_grant.mint(task="sample:task:0001", count=1))
        self.assertFalse(launch_grant.consume(token, task="sample:task:0002", count=1)[0])
        self.assertFalse(launch_grant.consume(token, task="sample:task:0001", count=2)[0])
        tampered = launch_grant.decode(token)
        tampered["count"] = 8
        self.assertFalse(launch_grant.consume(launch_grant.encode(tampered),
                                              task="sample:task:0001", count=8)[0])

    def test_remote_issuer_is_exact_and_never_grant_controlled(self):
        issuer = "https://hub.example.test/hub/api/launch-grant/consume"
        token = launch_grant.encode(launch_grant.mint(issuer=issuer))
        calls = []

        def remote(*args):
            calls.append(args)
            return True, 1

        ok, count = launch_grant.consume_authoritative(
            token, trusted_issuer=issuer, remote_consumer=remote
        )
        self.assertEqual((True, 1), (ok, count))
        self.assertEqual(issuer, calls[0][0])
        self.assertFalse(launch_grant.consume_authoritative(
            token,
            trusted_issuer="https://other.example.test/hub/api/launch-grant/consume",
            remote_consumer=remote,
        )[0])
        self.assertEqual(1, len(calls))

    def test_issuer_requires_https_except_loopback(self):
        with self.assertRaises(ValueError):
            launch_grant.mint(issuer="http://hub.example.test/hub/api/launch-grant/consume")
        grant = launch_grant.mint(issuer="http://127.0.0.1:8000/hub/api/launch-grant/consume")
        self.assertEqual("http://127.0.0.1:8000/hub/api/launch-grant/consume", grant["issuer"])

    def test_first_secret_initialization_is_cross_process_consistent(self):
        env = os.environ.copy()
        env["HUB_DIR"] = self.tmp.name
        env.pop("HUB_ATTEST_SECRET", None)
        root = Path(__file__).resolve().parents[2]
        processes = [subprocess.Popen(
            [sys.executable, "-m", "hub_core.launch_grant", "--mint", "--task", f"sample:task:{i:04d}"],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        ) for i in range(1, 7)]
        tokens = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(0, process.returncode, stderr)
            tokens.append(stdout.strip())
        secret = Path(self.tmp.name, ".attest-secret").read_text(encoding="utf-8").strip()
        self.assertEqual(64, len(secret))
        for i, token in enumerate(tokens, 1):
            self.assertTrue(launch_grant.consume(token, task=f"sample:task:{i:04d}")[0])

    def test_dead_process_lock_is_reclaimed(self):
        lock_dir = Path(self.tmp.name) / "locks"
        lock_dir.mkdir()
        path = lock_dir / ".test.lock"
        path.write_text("999999999", encoding="ascii")
        with ProcessFileLock(lock_dir, name=path.name, timeout=2):
            self.assertEqual(str(os.getpid()), path.read_text(encoding="ascii"))

    def test_adapter_can_bind_the_resolved_board_without_global_env_changes(self):
        other = Path(self.tmp.name) / "resolved-board"
        with launch_grant.using_hub_dir(other):
            token = launch_grant.encode(launch_grant.mint())
            self.assertTrue(launch_grant.consume(token)[0])
        self.assertTrue((other / ".attest-secret").exists())
        self.assertEqual(self.tmp.name, os.environ["HUB_DIR"])

    def test_remote_consume_never_redirects_the_write_token(self):
        seen = []

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("X-Write-Token"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/stolen")
                self.end_headers()

            def log_message(self, *_args):
                pass

        source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()
        try:
            with mock.patch.dict(os.environ, {"HUB_WRITE_TOKEN": "redirect-secret"}):
                ok, reason = launch_grant._consume_remote(
                    f"http://127.0.0.1:{source.server_port}/hub/api/launch-grant/consume",
                    "grant", "start", "", 1,
                )
            self.assertFalse(ok)
            self.assertIn("HTTP 302", reason)
            self.assertEqual([], seen)
        finally:
            source.shutdown()
            sink.shutdown()
            source.server_close()
            sink.server_close()

    def test_explicit_token_file_wins_over_ambient_process_token(self):
        token_file = Path(self.tmp.name) / "write-token"
        token_file.write_text("configured-token\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HUB_WRITE_TOKEN": "ambient-token"}):
            self.assertEqual("configured-token", launch_grant._read_write_token(token_file))


if __name__ == "__main__":
    unittest.main()
