"""The hub event store: an append-only, hash-chained, OCC + idempotent event log.

Canonical truth = PROJECT/.hub/events.jsonl (one canonical-JSON event per line, append-only).
PROJECT/.hub/events.db (SQLite) is a DERIVED index used as the transactional gatekeeper for
optimistic-concurrency head versions + idempotency, and for fast queries. The index is rebuilt
from the JSONL on init, so a crash between the JSONL append and the index commit self-heals
(the JSONL is the durable source). Stdlib only (works in Django and in single-file WSGI).
"""
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .canonical import canonical, sha256_hex


class ConflictError(Exception):
    """Optimistic-concurrency violation: expected_version != current head."""

    def __init__(self, aggregate, expected, current):
        super().__init__(f"OCC conflict on {aggregate}: expected v{expected}, head is v{current}")
        self.aggregate = aggregate
        self.expected = expected
        self.current = current


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Fields hashed into the chain, in a fixed order (hash excluded).
_HASH_FIELDS = (
    "seq", "event_id", "ts", "agent_id", "session_id", "parent_event_id", "actor_kind",
    "type", "aggregate", "base_version", "result_version", "payload", "model_version",
    "repo_build", "git_sha", "idem_key", "prev_hash",
)


class EventStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.root / "events.jsonl"
        self.db_path = self.root / "events.db"
        self.jsonl.touch(exist_ok=True)
        self._db = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._init_db()
        self._reconcile()

    def close(self):
        self._db.close()

    def _init_db(self):
        c = self._db
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "seq INTEGER PRIMARY KEY, event_id TEXT, ts TEXT, aggregate TEXT, type TEXT,"
            "base_version INTEGER, result_version INTEGER, hash TEXT, prev_hash TEXT,"
            "idem_key TEXT, raw TEXT)"
        )
        c.execute("CREATE TABLE IF NOT EXISTS heads (aggregate TEXT PRIMARY KEY, version INTEGER)")
        # idempotency is scoped PER AGGREGATE (doctrine): the same idem_key on two aggregates is distinct.
        c.execute("CREATE TABLE IF NOT EXISTS idem (aggregate TEXT, idem_key TEXT, seq INTEGER, PRIMARY KEY(aggregate, idem_key))")
        # migrate a legacy idem table (idem_key PK, no aggregate col) — it's a derived index, reconcile rebuilds it.
        cols = [r["name"] for r in c.execute("PRAGMA table_info(idem)").fetchall()]
        if "aggregate" not in cols:
            c.execute("DROP TABLE idem")
            c.execute("CREATE TABLE idem (aggregate TEXT, idem_key TEXT, seq INTEGER, PRIMARY KEY(aggregate, idem_key))")
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_events_agg ON events(aggregate)")
        self._install_trigger()

    def _install_trigger(self):
        """events is APPEND-ONLY: a BEFORE UPDATE/DELETE trigger RAISE(ABORT)s any in-place mutation
        (tamper-evidence at the DB layer, doctrine sec6). Dropped only around the reconcile rebuild."""
        c = self._db
        c.execute("CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events "
                  "BEGIN SELECT RAISE(ABORT, 'events is append-only'); END")
        c.execute("CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events "
                  "BEGIN SELECT RAISE(ABORT, 'events is append-only'); END")

    def _drop_trigger(self):
        self._db.execute("DROP TRIGGER IF EXISTS events_no_update")
        self._db.execute("DROP TRIGGER IF EXISTS events_no_delete")

    def _meta_get(self, key):
        r = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def _index_count(self) -> int:
        r = self._db.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return r["n"]

    def _jsonl_lines(self):
        out = []
        with open(self.jsonl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(line)
        return out

    def _reconcile(self):
        """Heal the index from the canonical JSONL. CONTENT-AWARE: rebuilds whenever the chain-head
        hash differs (catches a stale OR forged-but-same-row-count DB), not only on a row-count gap.
        TORN-LINE TOLERANT: a power-loss-mid-fsync leaves a partial FINAL line — quarantine it
        (truncate the log to the last good line); a non-final parse failure is real corruption -> raise."""
        import json
        raw = self._jsonl_lines()
        events, torn = [], False
        for i, line in enumerate(raw):
            try:
                events.append(json.loads(line))
            except Exception:
                if i == len(raw) - 1:
                    torn = True  # incomplete trailing write
                    break
                raise ValueError("corrupt event log at line %d (not the final line); refusing to auto-heal" % (i + 1))
        if torn:
            good = raw[:len(events)]
            tmp = self.jsonl.with_suffix(".jsonl.tmp")
            tmp.write_text(("\n".join(good) + ("\n" if good else "")), encoding="utf-8")
            os.replace(tmp, self.jsonl)
        jsonl_head = events[-1]["hash"] if events else ""
        if (not torn) and self._index_count() == len(events) and (self._meta_get("chain_head") or "") == jsonl_head:
            return
        c = self._db
        self._drop_trigger()
        try:
            c.execute("BEGIN")
            c.execute("DELETE FROM events")
            c.execute("DELETE FROM heads")
            c.execute("DELETE FROM idem")
            for ev in events:
                self._index_event(ev)
            c.execute("COMMIT")
        finally:
            self._install_trigger()

    def _index_event(self, ev):
        # plain INSERT (never REPLACE) so the append-only trigger holds; seqs are unique + monotonic.
        self._db.execute(
            "INSERT INTO events(seq,event_id,ts,aggregate,type,base_version,result_version,hash,prev_hash,idem_key,raw)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ev["seq"], ev["event_id"], ev["ts"], ev["aggregate"], ev["type"], ev["base_version"],
             ev["result_version"], ev["hash"], ev["prev_hash"], ev.get("idem_key"), canonical(ev)),
        )
        self._db.execute("INSERT OR REPLACE INTO heads(aggregate,version) VALUES(?,?)", (ev["aggregate"], ev["result_version"]))
        if ev.get("idem_key"):
            self._db.execute("INSERT OR REPLACE INTO idem(aggregate,idem_key,seq) VALUES(?,?,?)",
                             (ev["aggregate"], ev["idem_key"], ev["seq"]))
        self._db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('chain_head',?)", (ev["hash"],))

    def head_version(self, aggregate) -> int:
        r = self._db.execute("SELECT version FROM heads WHERE aggregate=?", (aggregate,)).fetchone()
        return r["version"] if r else 0

    def _last_seq_and_hash(self):
        r = self._db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return (r["seq"], r["hash"]) if r else (0, "")

    def append(self, *, aggregate, type, payload, expected_version=None, agent_id=None,
               session_id=None, parent_event_id=None, actor_kind="agent", model_version=None,
               repo_build=None, git_sha=None, idem_key=None) -> dict:
        """Append one event with OCC + idempotency + hash-chain. Returns the stored event.

        Raises ConflictError if expected_version != current head for the aggregate.
        Replaying the same idem_key is a safe no-op that returns the original event.
        """
        import json
        c = self._db
        c.execute("BEGIN IMMEDIATE")  # serialize writers
        try:
            if idem_key:
                r = c.execute("SELECT raw FROM events WHERE aggregate=? AND idem_key=?",
                              (aggregate, idem_key)).fetchone()
                if r:
                    c.execute("COMMIT")
                    return json.loads(r["raw"])
            head = self.head_version(aggregate)
            if expected_version is not None and expected_version != head:
                c.execute("ROLLBACK")
                raise ConflictError(aggregate, expected_version, head)
            last_seq, prev_hash = self._last_seq_and_hash()
            ev = {
                "seq": last_seq + 1,
                "event_id": str(uuid.uuid4()),
                "ts": _now_iso(),
                "agent_id": agent_id,
                "session_id": session_id,
                "parent_event_id": parent_event_id,
                "actor_kind": actor_kind,
                "type": type,
                "aggregate": aggregate,
                "base_version": head,
                "result_version": head + 1,
                "payload": payload,
                "model_version": model_version,
                "repo_build": repo_build,
                "git_sha": git_sha,
                "idem_key": idem_key,
                "prev_hash": prev_hash,
            }
            ev["hash"] = sha256_hex(prev_hash + canonical({k: ev[k] for k in _HASH_FIELDS}))
            # durable append to the canonical log FIRST (fsync), then index within the txn
            with open(self.jsonl, "a", encoding="utf-8") as f:
                f.write(canonical(ev) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._index_event(ev)
            c.execute("COMMIT")
            return ev
        except ConflictError:
            raise
        except Exception:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def events(self, aggregate=None):
        """All events (optionally for one aggregate), oldest first, as dicts."""
        import json
        if aggregate:
            rows = self._db.execute("SELECT raw FROM events WHERE aggregate=? ORDER BY seq", (aggregate,)).fetchall()
        else:
            rows = self._db.execute("SELECT raw FROM events ORDER BY seq").fetchall()
        return [json.loads(r["raw"]) for r in rows]

    def verify_chain(self) -> dict:
        """Replay the JSONL, recomputing the hash-chain. Tamper-evidence: checks prev_hash linkage,
        recomputed per-event hash, AND seq monotonicity (no gap/reorder/dupe). Tolerates a torn
        trailing line (incomplete write) but flags any non-final unparseable line as corruption."""
        import json
        prev = ""
        errs = []
        n = 0
        expect_seq = 1
        raw = self._jsonl_lines()
        for i, line in enumerate(raw, 1):
            try:
                ev = json.loads(line)
            except Exception:
                errs.append(f"line {i}: {'torn trailing line (incomplete write)' if i == len(raw) else 'unparseable (corruption)'}")
                break
            n += 1
            if ev.get("seq") != expect_seq:
                errs.append(f"line {i}: seq {ev.get('seq')} != expected {expect_seq} (gap/reorder/dupe)")
            expect_seq = (ev.get("seq") if isinstance(ev.get("seq"), int) else expect_seq) + 1
            if ev.get("prev_hash", "") != prev:
                errs.append(f"line {i} seq {ev.get('seq')}: prev_hash mismatch")
            recomputed = sha256_hex(prev + canonical({k: ev.get(k) for k in _HASH_FIELDS}))
            if recomputed != ev.get("hash"):
                errs.append(f"line {i} seq {ev.get('seq')}: hash mismatch (tampered)")
            prev = ev.get("hash", "")
        return {"ok": not errs, "count": n, "errors": errs}
