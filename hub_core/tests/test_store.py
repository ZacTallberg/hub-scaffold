"""Self-tests for the hub_core event store — the canonical source of truth + the false-green killer.
Stdlib unittest, NO Django, NO third-party deps (runs anywhere hub_core runs):
    python -m unittest hub_core.tests.test_store     (from a dir with hub_core on sys.path)
Covers: OCC (fresh/stale/recovery), per-aggregate idempotency, crash recovery (clean + torn trailing
line), verify_chain (clean + seq + tamper), and the append-only DB trigger. These are the behaviours
the doctrine's anti-false-green guarantees rest on, so they are pinned here."""
import os
import tempfile
import unittest

from hub_core.store import ConflictError, EventStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hubcore_test_")
        self.s = EventStore(self.dir)

    def tearDown(self):
        try:
            self.s.close()
        except Exception:
            pass

    def _add(self, agg="t:task:1", **kw):
        return self.s.append(aggregate=agg, type=kw.get("type", "x"),
                             payload=kw.get("payload", {"id": agg}),
                             expected_version=kw.get("expected_version"),
                             idem_key=kw.get("idem_key"))

    # ---- OCC ----
    def test_occ_fresh_then_stale(self):
        self._add(expected_version=0)                       # create -> v1
        self._add(expected_version=1)                       # update -> v2
        with self.assertRaises(ConflictError):
            self._add(expected_version=1)                   # stale expected -> conflict
        self.assertEqual(self.s.head_version("t:task:1"), 2)

    def test_occ_none_skips_check(self):
        self._add(expected_version=0)
        ev = self._add(expected_version=None)               # None = no OCC at the store layer
        self.assertEqual(ev["result_version"], 2)

    # ---- idempotency ----
    def test_idem_replay_is_noop(self):
        a = self._add(expected_version=0, idem_key="k")
        b = self._add(expected_version=0, idem_key="k")     # replay -> same event, no new seq
        self.assertEqual(a["event_id"], b["event_id"])
        self.assertEqual(self.s.verify_chain()["count"], 1)

    def test_idem_scoped_per_aggregate(self):
        a = self._add(agg="t:task:1", expected_version=0, idem_key="shared")
        b = self._add(agg="t:task:2", expected_version=0, idem_key="shared")  # same key, diff agg -> distinct
        self.assertNotEqual(a["event_id"], b["event_id"])

    # ---- crash recovery ----
    def test_recover_clean_reopen(self):
        self._add(expected_version=0)
        self._add(expected_version=1)
        self.s.close()
        s2 = EventStore(self.dir)                           # reopen -> heals, verifies
        self.assertTrue(s2.verify_chain()["ok"])
        self.assertEqual(s2.verify_chain()["count"], 2)
        s2.close()

    def test_recover_torn_trailing_line(self):
        self._add(expected_version=0)
        self._add(expected_version=1)
        self.s.close()
        with open(os.path.join(self.dir, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write('{"seq":3,"event_id":"x","incomplete_torn')   # power-loss mid-fsync
        s2 = EventStore(self.dir)                           # must NOT crash; quarantines the torn line
        v = s2.verify_chain()
        self.assertTrue(v["ok"], v["errors"])
        self.assertEqual(v["count"], 2)
        s2.close()

    # ---- tamper-evidence ----
    def test_verify_detects_payload_tamper(self):
        self._add(expected_version=0, payload={"id": "t:task:1", "n": 1})
        self._add(expected_version=1, payload={"id": "t:task:1", "n": 2})
        jl = os.path.join(self.dir, "events.jsonl")
        with open(jl, encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        lines[0] = lines[0].replace('"n":1', '"n":999').replace('"n": 1', '"n": 999')
        with open(jl, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        v = self.s.verify_chain()
        self.assertFalse(v["ok"])
        self.assertTrue(any("hash mismatch" in e for e in v["errors"]))

    def test_append_only_trigger_blocks_mutation(self):
        self._add(expected_version=0)
        with self.assertRaises(Exception):
            self.s._db.execute("UPDATE events SET type='hacked' WHERE seq=1")
        with self.assertRaises(Exception):
            self.s._db.execute("DELETE FROM events WHERE seq=1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
