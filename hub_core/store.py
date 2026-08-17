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


def durable_replace(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, CRASH-durable, matching the append path's fsync
    discipline: flush+fsync the tmp file's DATA before the rename (os.replace makes the rename
    atomic, but a rename can be journaled ahead of unfsynced data — a second crash then leaves a
    zero/partial file where the whole ledger used to be), then fsync the directory so the rename
    itself survives (POSIX; Windows cannot open a directory handle here and journals the rename)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return  # Windows: directories are not openable; NTFS journals the rename
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# Per-ledger-path verify_chain checkpoints (see verify_chain): process-level ON PURPOSE — a
# persisted checkpoint would let a tamper survive a process restart unseen.
_VERIFY_CHECKPOINT = {}

# ── LedgerLock (serialize-ledger-file-rewrites) ────────────────────────────────────────────
# EVERY path that writes events.jsonl — append, the reconcile healer's quarantine/rebuild, and
# the sync ingest's file replace — must hold this one lock across cursor-read -> validate ->
# file replace -> index rebuild -> commit. Without it, a sync replace or a healing rebuild can
# clobber the line an appender just acked+fsynced (the file loses it, the next rebuild-from-file
# then erases it from the index too), and an appender can allocate seq/prev_hash against a
# SQLite head the file has already moved past — the append-race fork class.
import threading as _threading

_LEDGER_RLOCKS = {}   # lock-file path -> threading.RLock (thread exclusion + reentrancy)
_LEDGER_DEPTH = {}    # lock-file path -> current re-entry depth in THIS process
_LEDGER_FDS = {}      # lock-file path -> the fd whose byte-range lock IS the critical section


def _lock_byte_exclusive(fd):
    """Take the OPERATING SYSTEM's exclusive lock on byte 0 of `fd`, without blocking. Raises
    OSError when another handle holds it."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_byte(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


class LedgerLock:
    """Cross-process writer lock on <hub_dir>/.ledger.lock.

    THE EXCLUSION IS THE KERNEL'S, NOT A PROTOCOL WE MAINTAIN. Every hand-rolled version of this
    lock died the same death: the file's mere EXISTENCE meant "held", so a holder that crashed
    wedged the hub, and every cure for that — a wall-clock grace, then a holder PID plus a
    released-marker — gave a waiter permission to DELETE somebody else's lock file. Deleting is
    unconditional: `_breakable()` inspects the file and `unlink()` then removes whatever is there
    an instant later, so a waiter that judged a stale marker deletes the LIVE lock a rival won in
    between and both proceed. Measured under load, two appenders sat inside this section together
    for a tenth of a second at a time, which is how a rival re-armed the append-only trigger under
    another connection's rebuild and aborted its DELETE with `events is append-only`. No
    filesystem offers a conditional delete, so no amount of care makes break-and-recreate sound.

    A byte-range lock does not need one. The kernel owns the exclusion and releases it when the
    holding handle closes — including when the holder is killed — so there is no staleness to
    infer, no liveness to probe, no marker to misread and nothing to break. The lock FILE is never
    deleted; it is a handle to lock, not a flag to test. It carries the holder's pid as a comment
    for whoever has to diagnose a wedged board, and nothing reads it back.

    Still reentrant WITHIN a process (the sync ingest replaces the file, then opens a store whose
    reconcile takes the same lock), guarded by a threading.RLock so concurrent request threads
    exclude each other, and bounded by a far-off ceiling so a genuinely wedged holder surfaces an
    error instead of hanging forever."""
    CEILING_S = 900.0       # a holder this long is wedged, not busy

    def __init__(self, hub_dir, timeout=None):
        self.path = Path(hub_dir) / ".ledger.lock"
        self.timeout = self.CEILING_S if timeout is None else timeout
        self._key = str(self.path)
        self._rlock = _LEDGER_RLOCKS.setdefault(self._key, _threading.RLock())
        # How many times acquire had to back off: a COUNTER, not a clock, so what it reports is
        # contention rather than how busy the box was while it waited.
        self.attempts = 0

    def _depth_on_this_file(self):
        """Is this process inside the critical section for THIS lock file, under any spelling of
        its path? Scoped to the file on purpose: one process legitimately holds several boards'
        locks at once (independent mounted apps can use different temporary HUB_DIRs), and
        asking 'is any depth held anywhere' called that a self-deadlock and refused a lock that
        was never contended."""
        mine = os.path.normcase(os.path.abspath(str(self.path)))
        return any(depth > 0 for key, depth in _LEDGER_DEPTH.items()
                   if os.path.normcase(os.path.abspath(key)) == mine)

    def __enter__(self):
        import time as _t
        self._rlock.acquire()
        if _LEDGER_DEPTH.get(self._key, 0) == 0:
            if self._depth_on_this_file():
                # Byte-range locks are per-HANDLE, so this process waiting on its own held lock
                # under a second spelling would block on itself until the ceiling.
                self._rlock.release()
                raise RuntimeError(
                    f"ledger lock self-deadlock: pid {os.getpid()} is inside the section under "
                    f"another key and waiting for {self.path} — every caller must open the store "
                    "with the SAME hub-dir spelling")
            deadline = _t.time() + self.timeout
            fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
            while True:
                try:
                    _lock_byte_exclusive(fd)
                    break
                except OSError:
                    if _t.time() > deadline:
                        os.close(fd)
                        self._rlock.release()
                        raise TimeoutError(f"ledger lock busy: {self.path}")
                    self.attempts += 1
                    _t.sleep(0.002)
            try:      # for a human reading a wedged board; nothing ever reads it back. Byte 0 is
                      # the locked one and is kept; the rest is rewritten so a shorter pid cannot
                      # leave the previous holder's digits trailing behind it.
                os.ftruncate(fd, 1)
                os.lseek(fd, 1, os.SEEK_SET)
                os.write(fd, (" held by pid %d" % os.getpid()).encode("ascii"))
            except OSError:
                pass
            _LEDGER_FDS[self._key] = fd
        _LEDGER_DEPTH[self._key] = _LEDGER_DEPTH.get(self._key, 0) + 1
        return self

    def __exit__(self, *exc):
        depth = _LEDGER_DEPTH.get(self._key, 1) - 1
        _LEDGER_DEPTH[self._key] = depth
        if depth == 0:
            fd = _LEDGER_FDS.pop(self._key, None)
            if fd is not None:
                try:
                    _unlock_byte(fd)
                except OSError:
                    pass            # closing the handle drops the lock regardless
                finally:
                    os.close(fd)
        self._rlock.release()
        return False


def jsonl_tail_hash(jsonl_path):
    """The `hash` of the file's last non-empty line ('' for an empty/absent file, None when the
    tail line does not parse) — the sync ingest's in-lock guard that the FILE tail still equals
    the head it validated against."""
    import json as _json
    try:
        size = os.path.getsize(jsonl_path)
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - 65536))
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    if not lines:
        return ""
    try:
        return _json.loads(lines[-1]).get("hash") or ""
    except ValueError:
        return None

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
        # SCHEMA SETUP HOLDS THE LEDGER LOCK, because `_init_db` ARMS the append-only trigger and
        # `CREATE TRIGGER` is database-level DDL rather than connection-local. Unlocked, any store
        # OPEN re-armed the trigger while another process sat between its own `_drop_trigger()` and
        # its rebuild's `DELETE FROM events`, and that DELETE then aborted with `events is
        # append-only` during a concurrent open/reconcile race. `_reconcile` already takes this
        # lock and LedgerLock is reentrant
        # within a process, so taking it here simply widens the same critical section to cover the
        # arming that was previously outside it. This deliberately does NOT touch the rebuild's data
        # path: an earlier attempt to remove the drop entirely (rebuild into side tables and swap by
        # DDL) lost an acked event, and serializing the window is worth far more than a cleverer
        # rebuild that cannot be trusted with the ledger.
        with LedgerLock(self.root):
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

    def _read_tail_line(self):
        """The last non-empty line of the JSONL, reading only the file's tail bytes."""
        try:
            size = os.path.getsize(self.jsonl)
            with open(self.jsonl, "rb") as f:
                f.seek(max(0, size - 65536))
                chunk = f.read().decode("utf-8", "replace")
        except OSError:
            return None
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        return lines[-1] if lines else None

    def _reconcile(self):
        """Heal the index from the canonical JSONL. CONTENT-AWARE: rebuilds whenever the chain-head
        hash differs (catches a stale OR forged-but-same-row-count DB), not only on a row-count gap.
        TORN-LINE TOLERANT: a power-loss-mid-fsync leaves a partial FINAL line — quarantine it
        (truncate the log to the last good line); a non-final parse failure is real corruption -> raise.

        FAST PATH (snapshot-audit-fast-lanes): every store OPEN ran this, and it parsed the entire
        ledger each time — a full rescan per hot read, growing with the board forever. Currency is
        now an O(tail) check first: byte size recorded at the last write + the tail line's hash
        against the indexed chain head. Any mismatch (torn tail included: its hash won't parse or
        match) falls through to the full parse-and-heal; a mid-chain divergence at equal
        size+head stays the served_index guard's beat, exactly as it was under the old fast path.

        The HEAL runs under LedgerLock (serialize-ledger-file-rewrites) and RE-CHECKS currency
        inside it: healing against a snapshot of the file while an appender or the sync ingest
        rewrites it would resurrect the very fork/lost-write class the lock closes."""
        if self._reconcile_current():
            return
        with LedgerLock(self.root):
            if self._reconcile_current():
                return
            self._reconcile_heal()

    def _reconcile_current(self):
        """O(tail) currency check: True iff the index already reflects the file."""
        import json
        try:
            size = os.path.getsize(self.jsonl)
        except OSError:
            size = -1
        if size < 0 or str(size) != (self._meta_get("jsonl_size") or ""):
            return False
        head = self._meta_get("chain_head") or ""
        if size == 0 and not head and self._index_count() == 0:
            return True
        tail = self._read_tail_line()
        if not tail:
            return False
        try:
            tail_hash = json.loads(tail).get("hash") or ""
        except ValueError:
            return False
        return bool(head) and tail_hash == head

    def _reconcile_heal(self):
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
            durable_replace(self.jsonl, "\n".join(good) + ("\n" if good else ""))
        events = self._linearize_forks(events)
        jsonl_head = events[-1]["hash"] if events else ""
        if (not torn) and self._index_count() == len(events) and (self._meta_get("chain_head") or "") == jsonl_head:
            self._stamp_jsonl_size()   # legacy db without the size meta: stamp so the O(tail) path takes over
            return
        c = self._db
        # BEGIN IMMEDIATE *before* the trigger drop, and the drop INSIDE that transaction.
        # The old shape dropped in autocommit and only then opened a DEFERRED transaction, which
        # takes no write lock until its first write - so a concurrent process merely OPENING the
        # store (open installs the append-only triggers, and opens do not take the ledger lock)
        # could recreate them inside the drop->DELETE window and abort the heal with 'events is
        # append-only'. Holding the write lock first makes drop + rebuild + reinstall one atomic
        # unit: a rival's CREATE TRIGGER waits on busy_timeout and then no-ops, and any exit that
        # is not a COMMIT rolls the DROP back, so the ledger is never left unguarded.
        #
        # Prevention and recovery both stay. With the ledger lock now the kernel's no rival should
        # be in here at all, and this transaction closes the open-installs-triggers window that was
        # never under it - but a re-arm that still happens is RECOVERABLE, so re-drop and rebuild
        # once. What must never happen is proceeding into a DELETE that may still be guarded, or a
        # corrupting race reading as a passing run, so a re-arm that does not clear is raised by
        # name rather than left to surface downstream as a bare IntegrityError.
        trouble = None
        for attempt in range(2):
            try:
                c.execute("BEGIN IMMEDIATE")
                self._drop_trigger()
                armed = [r["name"] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN "
                    "('events_no_update','events_no_delete')").fetchall()]
                if armed:
                    # Inside the write lock this can no longer be a rival's open: this
                    # connection's own DROP no-opped, or the lock did not exclude a rival holder.
                    trouble = "still armed after _drop_trigger() (%s)" % ", ".join(armed)
                    c.execute("ROLLBACK")
                else:
                    c.execute("DELETE FROM events")
                    c.execute("DELETE FROM heads")
                    c.execute("DELETE FROM idem")
                    for ev in events:
                        self._index_event(ev)
                    self._stamp_jsonl_size()
                    self._install_trigger()
                    c.execute("COMMIT")
                    return
            except sqlite3.IntegrityError as e:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if "append-only" not in str(e):
                    raise
                trouble = "re-armed during the rebuild (%s)" % e
            except BaseException:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            if attempt == 0:
                print("NOTE hub_core.store: append-only trigger %s - re-dropping and rebuilding "
                      "once" % trouble)
        raise RuntimeError(
            "reconcile heal: append-only trigger %s on BOTH attempts - a concurrent reconcile "
            "keeps re-arming it, or the ledger lock did not exclude a rival holder for this "
            "critical section" % trouble)

    def _linearize_forks(self, events):
        """Heal an append-race FORK the way _reconcile already heals a torn line: two writers can
        read the same head and both append seq N. A race LOSER is an honestly-appended event whose
        seq/prev_hash are merely stale, so
        it is re-chained in FILE ORDER (new seq + prev_hash, hash recomputed) — deterministic, so
        concurrent healers produce identical bytes. Fail-closed: an event that does not self-verify
        against its OWN claimed fields, or whose claimed prev_hash names no event in this log, is
        not a race loser but corruption/forgery — refuse to heal, raise. Nothing is ever dropped."""
        healed, dirty, prev, known, seen_seqs = [], False, "", set(), set()
        for i, ev in enumerate(events):
            want_seq = i + 1
            if not dirty and ev.get("seq") == want_seq and ev.get("prev_hash") == prev:
                healed.append(ev)
            else:
                claimed = {k: ev.get(k) for k in _HASH_FIELDS}
                if sha256_hex(ev.get("prev_hash", "") + canonical(claimed)) != ev.get("hash"):
                    raise ValueError(
                        "corrupt event log: line %d does not verify against its own fields; "
                        "refusing to auto-heal (not an append race)" % (i + 1))
                if ev.get("prev_hash", "") not in known and ev.get("prev_hash", "") != "":
                    raise ValueError(
                        "corrupt event log: line %d chains off an unknown parent hash; "
                        "refusing to auto-heal (not an append race)" % (i + 1))
                # The race SIGNATURE gates entry into heal mode: the first rewritten event must
                # collide with an already-kept seq (two writers, one head). Everything after it is
                # a suffix re-chain of honest events. A non-colliding disruption is not a race —
                # that is verify_chain's beat, never healed here.
                if not dirty and ev.get("seq") in seen_seqs:
                    print("NOTE hub_core.store: append-race fork at line %d (seq %s, aggregate %s)"
                          " — linearizing the tail in file order; nothing dropped"
                          % (i + 1, ev.get("seq"), ev.get("aggregate")))
                elif not dirty:
                    raise ValueError(
                        "corrupt event log: line %d is out of chain without a seq collision; "
                        "refusing to auto-heal (not an append race)" % (i + 1))
                known.add(ev.get("hash"))  # later suffix events chain off the ORIGINAL hash
                ev = dict(ev)
                ev["seq"] = want_seq
                ev["prev_hash"] = prev
                ev["hash"] = sha256_hex(prev + canonical({k: ev.get(k) for k in _HASH_FIELDS}))
                healed.append(ev)
                dirty = True
            prev = healed[-1]["hash"]
            known.add(prev)
            seen_seqs.add(healed[-1]["seq"])
        if dirty:
            durable_replace(self.jsonl, "\n".join(canonical(e) for e in healed) + "\n")
            print("NOTE hub_core.store: healed ledger written (%d events, chain re-verified on rebuild)"
                  % len(healed))
        return healed

    def _stamp_jsonl_size(self):
        try:
            size = os.path.getsize(self.jsonl)
        except OSError:
            return
        self._db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('jsonl_size',?)", (str(size),))

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

    def has_idem(self, aggregate, idem_key) -> bool:
        """True if this idem_key was already recorded for the aggregate, i.e. a re-send would replay
        (a safe no-op) rather than write. Lets a caller distinguish an idempotent retry from a genuine
        unversioned update, which must still be refused."""
        if not idem_key:
            return False
        r = self._db.execute("SELECT 1 FROM idem WHERE aggregate=? AND idem_key=?",
                             (aggregate, idem_key)).fetchone()
        return r is not None

    def _last_seq_and_hash(self):
        r = self._db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return (r["seq"], r["hash"]) if r else (0, "")

    def _jsonl_tail_seq(self) -> int:
        """seq of the last PARSABLE line of the canonical log (0 on empty). A torn final line is
        skipped exactly as reconcile treats it. The allocator compares this against the index:
        the two live in different durability domains (fsync'd file vs sqlite txn), so the index
        can be BEHIND the file after a crash or a failed index commit — and an allocator that
        trusts the stale index mints a duplicate seq straight into the canonical log (the
        2026-08-02 fleet-outage fork)."""
        import json
        for line in reversed(self._jsonl_lines()):
            try:
                return json.loads(line)["seq"]
            except Exception:
                continue
        return 0

    def append(self, *, aggregate, type, payload, expected_version=None, agent_id=None,
               session_id=None, parent_event_id=None, actor_kind="agent", model_version=None,
               repo_build=None, git_sha=None, idem_key=None) -> dict:
        """Append one event with OCC + idempotency + hash-chain. Returns the stored event.

        Raises ConflictError if expected_version != current head for the aggregate.
        Replaying the same idem_key is a safe no-op that returns the original event.

        Runs under LedgerLock (serialize-ledger-file-rewrites): the sqlite BEGIN IMMEDIATE
        serializes appenders against each other, but only the file lock serializes them against
        the sync ingest's whole-file replace and the reconcile healer's rebuild — without it, a
        replace clobbers the acked line an appender just fsynced, and seq/prev_hash get
        allocated against a SQLite head the file has already moved past.
        """
        import json
        with LedgerLock(self.root):
            return self._append_locked(
                aggregate=aggregate, type=type, payload=payload,
                expected_version=expected_version, agent_id=agent_id, session_id=session_id,
                parent_event_id=parent_event_id, actor_kind=actor_kind,
                model_version=model_version, repo_build=repo_build, git_sha=git_sha,
                idem_key=idem_key)

    def append_batch(self, operations) -> list[dict]:
        """Commit multiple aggregate events at one canonical-file boundary.

        Failure handling must update its source task and create its repair task all-or-none. A
        sequence of ordinary appends cannot promise that: a process can die between acknowledged
        lines. This path builds the complete hash-chained suffix, atomically replaces the JSONL
        with old bytes + that suffix, then indexes every row in one SQLite transaction. A crash
        after the replace but before the index commit self-heals from the canonical file exactly as
        a single append does.

        Every operation has the same keyword shape as ``append``. Idempotent replay succeeds only
        when the entire batch is already present; a partial replay is named rather than silently
        manufacturing a mixed commit.
        """
        import json

        ops = [dict(op) for op in (operations or [])]
        if not ops:
            return []
        with LedgerLock(self.root):
            head_hash = self._meta_get("chain_head") or ""
            if jsonl_tail_hash(self.jsonl) != head_hash:
                self._reconcile_heal()
            c = self._db
            c.execute("BEGIN IMMEDIATE")
            if self._jsonl_tail_seq() > self._last_seq_and_hash()[0]:
                c.execute("ROLLBACK")
                self._reconcile()
                c.execute("BEGIN IMMEDIATE")
            try:
                replay = []
                for op in ops:
                    idem = op.get("idem_key")
                    row = (c.execute("SELECT raw FROM events WHERE aggregate=? AND idem_key=?",
                                     (op["aggregate"], idem)).fetchone() if idem else None)
                    replay.append(json.loads(row["raw"]) if row else None)
                if any(replay):
                    if not all(replay):
                        c.execute("ROLLBACK")
                        raise ValueError("partial idempotent batch exists; refusing a mixed commit")
                    c.execute("COMMIT")
                    return replay

                last_seq, prev_hash = self._last_seq_and_hash()
                heads = {}
                seen_idem = set()
                events = []
                for offset, op in enumerate(ops, 1):
                    aggregate = op["aggregate"]
                    head = heads.get(aggregate, self.head_version(aggregate))
                    expected = op.get("expected_version")
                    if expected is not None and expected != head:
                        c.execute("ROLLBACK")
                        raise ConflictError(aggregate, expected, head)
                    idem = op.get("idem_key")
                    idem_pair = (aggregate, idem)
                    if idem and idem_pair in seen_idem:
                        c.execute("ROLLBACK")
                        raise ValueError("duplicate aggregate/idempotency key inside batch")
                    seen_idem.add(idem_pair)
                    ev = {
                        "seq": last_seq + offset,
                        "event_id": str(uuid.uuid4()),
                        "ts": _now_iso(),
                        "agent_id": op.get("agent_id"),
                        "session_id": op.get("session_id"),
                        "parent_event_id": op.get("parent_event_id"),
                        "actor_kind": op.get("actor_kind", "agent"),
                        "type": op["type"],
                        "aggregate": aggregate,
                        "base_version": head,
                        "result_version": head + 1,
                        "payload": op.get("payload") or {},
                        "model_version": op.get("model_version"),
                        "repo_build": op.get("repo_build"),
                        "git_sha": op.get("git_sha"),
                        "idem_key": idem,
                        "prev_hash": prev_hash,
                    }
                    ev["hash"] = sha256_hex(prev_hash + canonical(
                        {key: ev[key] for key in _HASH_FIELDS}))
                    events.append(ev)
                    heads[aggregate] = head + 1
                    prev_hash = ev["hash"]

                old = self.jsonl.read_text(encoding="utf-8")
                if old and not old.endswith("\n"):
                    old += "\n"
                durable_replace(self.jsonl, old + "".join(canonical(ev) + "\n" for ev in events))
                try:
                    for ev in events:
                        self._index_event(ev)
                    self._stamp_jsonl_size()
                    c.execute("COMMIT")
                except Exception:
                    try:
                        c.execute("ROLLBACK")
                    except Exception:
                        pass
                    self._reconcile()
                    recovered = []
                    for ev in events:
                        row = self._db.execute("SELECT raw FROM events WHERE event_id=?",
                                               (ev["event_id"],)).fetchone()
                        if not row:
                            raise
                        recovered.append(json.loads(row["raw"]))
                    return recovered
                return events
            except ConflictError:
                raise
            except Exception:
                try:
                    c.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def _append_locked(self, *, aggregate, type, payload, expected_version, agent_id,
                       session_id, parent_event_id, actor_kind, model_version, repo_build,
                       git_sha, idem_key):
        import json
        # Tail-consistency guard: a crashed sync/heal can leave the FILE ahead of this handle's
        # SQLite view (the lock died with the crasher). Re-heal before allocating seq/prev_hash
        # so the chain extends the canonical tail, never a stale index head.
        head_hash = self._meta_get("chain_head") or ""
        tail = jsonl_tail_hash(self.jsonl)
        if tail != head_hash:
            self._reconcile_heal()
        c = self._db
        c.execute("BEGIN IMMEDIATE")  # serialize writers
        # ALLOCATE FROM THE CANONICAL SOURCE: with the write lock held, the index must agree with
        # the jsonl tail before any seq is minted. If the file is ahead (an earlier append's index
        # commit failed or crashed after the fsync), re-sync the index from the file and retake
        # the lock — never allocate off a stale view.
        if self._jsonl_tail_seq() > self._last_seq_and_hash()[0]:
            c.execute("ROLLBACK")
            self._reconcile()
            c.execute("BEGIN IMMEDIATE")
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
            try:
                self._index_event(ev)
                self._stamp_jsonl_size()
                c.execute("COMMIT")
            except Exception:
                # The event IS durable in the canonical log; forgetting it here is what turns the
                # next allocation into a duplicate-seq fork. Re-sync the index from the file and
                # return the event as it now stands (reconcile may have re-chained it).
                try:
                    c.execute("ROLLBACK")
                except Exception:
                    pass
                self._reconcile()
                r = self._db.execute("SELECT raw FROM events WHERE event_id=?",
                                     (ev["event_id"],)).fetchone()
                if r:
                    return json.loads(r["raw"])
                raise
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

    def events_after(self, seq: int, limit: int = 100):
        """Return canonical events after ``seq`` without replaying the full ledger.

        The push stream reads this derived index only after a mutation signal or reconnect; it
        never samples it on an interval. A bounded query keeps recovery work finite while callers
        advance the cursor through the exact canonical head.
        """
        import json

        try:
            cursor = max(0, int(seq))
        except (TypeError, ValueError):
            cursor = 0
        size = max(1, min(int(limit), 500))
        rows = self._db.execute(
            "SELECT raw FROM events WHERE seq>? ORDER BY seq LIMIT ?",
            (cursor, size),
        ).fetchall()
        return [json.loads(row["raw"]) for row in rows]

    def latest_cursor(self) -> dict:
        """Return the current public live cursor without exposing event payloads."""
        row = self._db.execute(
            "SELECT seq, hash, raw FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"seq": 0, "hash": "", "ts": None}
        import json

        event = json.loads(row["raw"])
        return {"seq": row["seq"], "hash": row["hash"], "ts": event.get("ts")}

    def verify_chain(self) -> dict:
        """Replay the JSONL, recomputing the hash-chain. Tamper-evidence: checks prev_hash linkage,
        recomputed per-event hash, AND seq monotonicity (no gap/reorder/dupe). Tolerates a torn
        trailing line (incomplete write) but flags any non-final unparseable line as corruption.

        INCREMENTAL: a fully-verified pass checkpoints (byte offset, sha256 of the prefix BYTES,
        last hash, next seq) per ledger path, process-level. A warm call re-hashes the raw prefix
        bytes ONCE (C speed — no json parse, no canonicalization) and chain-verifies only the
        events beyond the checkpoint, so an idle or once-appended ledger costs O(new events)
        instead of O(all). ANY byte change inside the prefix (a mid-chain edit, a quarantine or
        sync rewrite) misses the prefix hash and falls back to the full replay — tampering is
        caught warm, not only on a cold open. The checkpoint is deliberately in-memory: persisting
        it would let a tamper survive a restart unseen."""
        import hashlib
        import json
        try:
            blob = self.jsonl.read_bytes()
        except OSError:
            blob = b""
        key = str(self.jsonl)
        cp = _VERIFY_CHECKPOINT.get(key)
        prev = ""
        n = 0
        expect_seq = 1
        skip = 0
        if cp and len(blob) >= cp["offset"] and \
                hashlib.sha256(blob[:cp["offset"]]).hexdigest() == cp["prefix_sha"]:
            prev, n, expect_seq, skip = cp["prev"], cp["count"], cp["next_seq"], cp["count"]
        errs = []
        # errors="replace": a power-cut can tear a line mid-character; mangled bytes must surface
        # as the torn-line error below, never as a decode crash.
        raw = [ln.strip() for ln in blob.decode("utf-8", "replace").split("\n") if ln.strip()]
        for i, line in enumerate(raw[skip:], skip + 1):
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
        if not errs:
            _VERIFY_CHECKPOINT[key] = {"offset": len(blob),
                                       "prefix_sha": hashlib.sha256(blob).hexdigest(),
                                       "prev": prev, "count": n, "next_seq": expect_seq}
        return {"ok": not errs, "count": n, "errors": errs}
