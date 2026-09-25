"""Tool-time lesson triggers: surface the lesson a command, a path or an error names, at the moment
it applies — deterministically, with no embeddings.

A knowledge record may carry ``applies_when`` (note schema): the literal signals that mean the rule
applies RIGHT NOW, derived from the rule and its story::

    {"errors": [...], "commands": [...], "paths": [...], "systems": [...],   # each <=5, <=160 chars
     "from_sha": ..., "derived_by": ..., "derived_at": ...}

They ride the knowledge feed to every mirror. A harness hook (PreToolUse / PostToolUseFailure)
runs ``python -m hub_core.client lesson-trigger --hook`` on every tool call, and this module
matches the event against the mirror's triggers:

  * ``<...>`` inside a trigger is a wildcard for ONE value: 1-200 non-space characters. Measured
    on real transcripts: a wildcard allowed to span spaces let ``git show <rev>:<path>`` match a
    whole unrelated command line;
  * ``errors``   match the tool's failure OUTPUT only (PostToolUseFailure's ``error``);
  * ``commands`` match the command about to run (PreToolUse, a shell tool);
  * ``paths``    match an edited file path as a glob anchored at a path-segment boundary;
  * ``systems``  never fire alone — they only order records that already matched;
  * a trigger carried by more than MAX_DF live records is ignored (it names nothing specific);
  * a trigger with fewer than MIN_LITERAL literal characters is ignored;
  * at most MAX_PER_EVENT records per event, once per session per record (a receipt), under the
    line PREFIX_LINE; nothing at all when nothing matches.

Nearest-neighbour search over error text was measured relevant about 4 times in 30 on the system
this was lifted from, and a reminder that is usually wrong teaches the reader to skip it — which is
why matching is literal.

THE CORPUS GUARD. The write side can drop a trigger shared by too many lessons, and MAX_DF drops
one carried by too many live records, but neither can see how often a trigger string appears in
the calls agents actually MAKE. The guard is a rate with no hand-set list: a trigger is ignored
when it matches more than GUARD_RATE of the kind of call it is matched against (commands vs shell
commands, paths vs edited paths, errors vs failed tool results) among this machine's last SAMPLE_M
tool calls. Calibrated on one machine's 20,000 calls: the two triggers that fired mostly on the
wrong case sat at 2.5-2.6% of commands; the error triggers that were right 8 times in 10 sat at
0-0.5% of failures; the groups are ~5x apart at every window from 5k to 100k calls, so the line is
their geometric middle, 1%. A kind with under MIN_KIND_CALLS calls in the sample is not rated.
Rating reads transcripts, so it never runs in the hook: the hook reads a small guard file and,
when the sample is a day old or the mirror brought unrated triggers, spawns ONE detached
``client triggers --refresh`` and holds the unrated triggers back meanwhile. Failure mode:
silence, never noise.

THE PRECISION SIGNAL. The injected line asks the agent to cite the id if it changes what it does.
A cite is the cheapest honest evidence an injected lesson mattered, so each injection is noted with
the transcript offset it landed at; later hook runs in the same session scan only NEW transcript
bytes for assistant content naming a pending id and append one ``cite`` line to an append-only
ledger. ``client triggers --stats`` prints fired / cited / precision per trigger and per record.
A compaction resets the session receipt, so a pending injection then counts as fired, not cited:
undercounting cites can only make a trigger look noisier than it is.

Transcript format read today: Claude Code (``~/.claude/projects/*/*.jsonl``). Another runtime is
one more ``iter_calls`` parser. Standard library only; every hook path fails silent.
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
import sys
import time

from pathlib import Path

from . import memory_feed, secretscan

KINDS = ("errors", "commands", "paths", "systems")
MAX_DF = 6
MAX_PER_EVENT = 2
MIN_LITERAL = 4
MAX_TRIGGERS_PER_KIND = 5
MAX_TRIGGER_CHARS = 160
PREFIX_LINE = "cite the id if it changes what you do"
RULE_CHARS = 500
CACHE_VERSION = 1
#: Stay under a typical hook's inline ceiling (10,000 chars) with room for the wrapping.
OUTPUT_MAX = 9500

SAMPLE_M = 20000
GUARD_RATE = 0.01
MIN_KIND_CALLS = 500
SAMPLE_MAX_AGE_S = 86400
ERROR_CHARS = 8000
REFRESH_LOCK_S = 600
GUARD_VERSION = 1
MAX_SCAN_BYTES = 8 * 1024 * 1024

_WILD = re.compile(r"<[^<>]{1,60}>")
COMMAND_TOOLS = frozenset({"Bash", "PowerShell"})
PATH_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


# ── where things live (all under the client's state dir unless overridden) ──

def state_dir() -> Path:
    raw = os.environ.get("HUB_CLIENT_STATE_DIR", "").strip()
    return Path(raw) if raw else Path(os.path.expanduser("~")) / ".hub-client"


def mirror_path() -> Path:
    """The local knowledge mirror: ``HUB_KNOWLEDGE_MIRROR`` (a knowledge-sync JSON file or a JSONL
    feed), else ``<state dir>/knowledge.json`` — the file ``client knowledge-sync --out`` writes."""
    raw = os.environ.get("HUB_KNOWLEDGE_MIRROR", "").strip()
    return Path(os.path.expanduser(raw)) if raw else state_dir() / "knowledge.json"


def _dir() -> Path:
    return state_dir() / "triggers"


def cache_path() -> Path:
    return _dir() / "index.json"


def sample_path() -> Path:
    return _dir() / "tool_call_sample.json"


def guard_path() -> Path:
    return _dir() / "guard.json"


def ledger_path() -> Path:
    return _dir() / "events.jsonl"


def transcripts_dir() -> Path:
    raw = os.environ.get("HUB_TRIGGER_TRANSCRIPTS", "").strip()
    return Path(os.path.expanduser(raw)) if raw else Path(os.path.expanduser("~")) / ".claude" / "projects"


def _atomic_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── building the index ──

def norm(s: str) -> str:
    """The document-frequency key: case- and whitespace-insensitive."""
    return " ".join(str(s or "").lower().split())


def literal_len(trigger: str) -> int:
    return len(_WILD.sub("", trigger).strip())


def _clean(values) -> list:
    if not isinstance(values, list):
        return []
    return [v.strip() for v in values[:MAX_TRIGGERS_PER_KIND]
            if isinstance(v, str) and v.strip() and len(v) <= MAX_TRIGGER_CHARS]


def trigger_records(live: dict) -> dict:
    """The live records that carry a usable ``applies_when``. A newer put WITHOUT triggers
    retires the old ones (the live fold already kept only the newest put)."""
    out = {}
    for rid, item in live.items():
        aw = item.get("applies_when")
        if not isinstance(aw, dict):
            continue
        trig = {k: _clean(aw.get(k)) for k in KINDS}
        if not any(trig.get(k) for k in ("errors", "commands", "paths")):
            continue
        out[rid] = {"id": rid, "type": str(item.get("type") or ""),
                    "title": str(item.get("title") or "").strip(),
                    "rule": str(item.get("rule") or "").strip(),
                    "text_sha": str(item.get("text_sha") or ""), "triggers": trig}
    return out


def build_index(records: dict) -> dict:
    """Apply the corpus-level filters (document frequency, literal length) once, at build."""
    df: dict = {}
    for rid, rec in records.items():
        for kind in KINDS:
            for t in rec["triggers"].get(kind, []):
                df.setdefault(norm(t), set()).add(rid)
    rejected, out = {}, []
    for rid, rec in sorted(records.items()):
        kept = {}
        for kind in KINDS:
            keep = []
            for t in rec["triggers"].get(kind, []):
                n = len(df.get(norm(t), ()))
                if n > MAX_DF:
                    rejected[t] = "carried by %d live records (> %d)" % (n, MAX_DF)
                elif literal_len(t) < MIN_LITERAL:
                    rejected[t] = "%d literal characters (< %d)" % (literal_len(t), MIN_LITERAL)
                else:
                    keep.append(t)
            kept[kind] = keep
        if any(kept[k] for k in ("errors", "commands", "paths")):
            out.append({**rec, "triggers": kept})
    return {"version": CACHE_VERSION, "records": out, "rejected": rejected}


def load_index(path: Path | None = None, cache: Path | None = None) -> dict:
    """The trigger index for the current mirror, rebuilt only when the mirror changed (keyed by
    its path, size and mtime). A missing mirror is an empty index: the hook then says nothing."""
    path = Path(path) if path is not None else mirror_path()
    cache = Path(cache) if cache is not None else cache_path()
    try:
        st = path.stat()
        ident = [str(path), st.st_size, st.st_mtime_ns]
    except OSError:
        return {"version": CACHE_VERSION, "records": [], "rejected": {}, "mirror": None}
    cached = _read_json(cache)
    if isinstance(cached, dict) and cached.get("version") == CACHE_VERSION and cached.get("mirror") == ident:
        return cached
    index = build_index(trigger_records(memory_feed.live_records(path)))
    index["mirror"] = ident
    try:
        _atomic_json(cache, index)
    except OSError:
        pass                                  # an unwritable cache costs a rebuild, never a miss
    return index


# ── matching ──

_RX_MEMO: dict = {}


def _lit(s: str) -> str:
    return r"\s+".join(re.escape(w) for w in re.split(r"\s+", s)) if s else ""


def _text_rx(trigger: str) -> re.Pattern:
    """A trigger as a case-insensitive regex: literal text, ``<...>`` = one non-space value, and
    runs of whitespace match any whitespace (output wraps and re-indents)."""
    rx = _RX_MEMO.get(trigger)
    if rx is None:
        parts, pos = [], 0
        for m in _WILD.finditer(trigger):
            parts.append(_lit(trigger[pos:m.start()]))
            parts.append(r"\S{1,200}?")
            pos = m.end()
        parts.append(_lit(trigger[pos:]))
        rx = re.compile("".join(parts), re.IGNORECASE | re.DOTALL)
        _RX_MEMO[trigger] = rx
    return rx


def _path_match(trigger: str, file_path: str) -> bool:
    """Glob match anchored at a path-segment boundary: ``app/x.py`` matches ``/srv/repo/app/x.py``
    and never ``/srv/repo/myapp/x.py``."""
    p = file_path.replace("\\", "/").lower()
    t = _WILD.sub("*", trigger.replace("\\", "/").strip().lower())
    while t.startswith("./"):                 # "./" only: ".gitlab-ci.yml" is a dotfile
        t = t[2:]
    if not t:
        return False
    if t.startswith("/") or re.match(r"^[a-z]:/", t):
        return fnmatch.fnmatchcase(p, t)
    return fnmatch.fnmatchcase(p, t) or fnmatch.fnmatchcase(p, "*/" + t)


class Hit:
    """One record that matched an event: the (kind, trigger) pairs that fired and the systems
    that break ties. A plain class: `dataclasses` imports `inspect`, a cost every tool call pays."""
    __slots__ = ("record", "matched", "systems")

    def __init__(self, record: dict, matched=None, systems=None):
        self.record = record
        self.matched = list(matched or [])
        self.systems = list(systems or [])

    def score(self):
        lit = max((literal_len(t) for _k, t in self.matched), default=0)
        return (len(self.matched), len(self.systems), lit)


def event_parts(event: dict) -> dict:
    """What each trigger kind may be matched against. A bare ``{"error"|"command"|"path": ...}``
    (the board route's query) is read the same way as a harness hook event."""
    name = str(event.get("hook_event_name") or "")
    tool = str(event.get("tool_name") or "")
    ti = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    parts = {"errors": "", "commands": "", "paths": "", "context": ""}
    if name == "PostToolUseFailure":
        parts["errors"] = str(event.get("error") or "")
    elif name == "PreToolUse":
        if tool in COMMAND_TOOLS:
            parts["commands"] = str(ti.get("command") or "")
        elif tool in PATH_TOOLS:
            parts["paths"] = str(ti.get("file_path") or ti.get("notebook_path") or "")
    elif not name:
        parts["errors"] = str(event.get("error") or "")
        parts["commands"] = str(event.get("command") or "")
        parts["paths"] = str(event.get("path") or "")
    parts["context"] = " ".join(str(x) for x in (
        parts["errors"], parts["commands"], parts["paths"], ti.get("command") or "",
        ti.get("file_path") or "", ti.get("description") or "", event.get("cwd") or "",
        event.get("context") or ""))
    return parts


def match(event: dict, index: dict) -> list:
    """Every record whose errors/commands/paths trigger matches this event, best first."""
    parts = event_parts(event)
    if not (parts["errors"] or parts["commands"] or parts["paths"]):
        return []
    hits = []
    for rec in index.get("records") or []:
        trig = rec["triggers"]
        matched = []
        if parts["errors"]:
            matched += [("errors", t) for t in trig.get("errors", ()) if _text_rx(t).search(parts["errors"])]
        if parts["commands"]:
            matched += [("commands", t) for t in trig.get("commands", ())
                        if _text_rx(t).search(parts["commands"])]
        if parts["paths"]:
            matched += [("paths", t) for t in trig.get("paths", ()) if _path_match(t, parts["paths"])]
        if not matched:
            continue
        systems = [s for s in trig.get("systems", ()) if _text_rx(s).search(parts["context"])]
        hits.append(Hit(rec, matched, systems))
    hits.sort(key=lambda h: h.score(), reverse=True)
    return hits


def receipt_key(rec: dict) -> str:
    return "trigger:%s#%s" % (rec["id"], rec.get("text_sha") or "")


def render(rec: dict, hit: Hit) -> str:
    title, rule = rec.get("title") or "", rec.get("rule") or ""
    if len(rule) > RULE_CHARS:
        rule = rule[:RULE_CHARS].rstrip() + "…"
    why = "; ".join('%s "%s"' % (k.rstrip("s"), t) for k, t in hit.matched[:3])
    stem = title.rstrip("…").rstrip(".").rstrip()
    if rule and (rule == title or (stem and rule.startswith(stem))):
        lines = ["[%s] %s" % (rec["id"], rule)]
    else:
        lines = ["[%s] %s" % (rec["id"], title)] + ([rule] if rule else [])
    lines.append("(fired on %s)" % why)
    return secretscan.redact("\n".join(lines))[0]


def select(event: dict, index: dict, held: set) -> tuple:
    """(chosen [(hit, key, block)], passed-over [(hit, reason)]) for one event."""
    chosen, passed = [], []
    for h in match(event, index):
        key = receipt_key(h.record)
        if key in held:
            passed.append((h, "already delivered this session"))
        elif len(chosen) >= MAX_PER_EVENT:
            passed.append((h, "over the %d-per-event cap" % MAX_PER_EVENT))
        else:
            chosen.append((h, key, render(h.record, h)))
    return chosen, passed


def envelope(blocks: list) -> str:
    return (PREFIX_LINE + "\n" + "\n\n".join(blocks)) if blocks else ""


def public_hit(h: Hit) -> dict:
    return {"id": h.record["id"], "type": h.record.get("type"), "title": h.record.get("title"),
            "rule": secretscan.redact(h.record.get("rule") or "")[0][:RULE_CHARS],
            "matched": [{"kind": k, "trigger": t} for k, t in h.matched],
            "systems": list(h.systems)}


# ── the per-session receipt ──

def _safe(sid) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", str(sid or ""))[:80] or "no-session"


def _receipt_path(sid) -> Path:
    return _dir() / "receipts" / (_safe(sid) + ".json")


def load_receipt(sid) -> dict:
    doc = _read_json(_receipt_path(sid))
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("keys", [])
    doc.setdefault("injected", {})
    return doc


def save_receipt(sid, doc: dict) -> None:
    try:
        doc["keys"] = list(dict.fromkeys(doc.get("keys") or []))[-2000:]
        _atomic_json(_receipt_path(sid), doc)
    except OSError:
        pass                                   # a lost receipt costs one repeat, never the call


def reset_session(sid) -> None:
    """A fresh or compacted context holds nothing: forget what this session was handed."""
    try:
        _receipt_path(sid).unlink()
    except OSError:
        pass


# ── the corpus guard ──

def iter_calls(path: str):
    """(ts, kind, text) for every rateable tool call in one Claude Code transcript: 'commands'
    (a shell command), 'paths' (an edited path) or 'errors' (a failed tool result)."""
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        for raw in fh:
            if b'"tool_use"' not in raw and b'"is_error":true' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            content = (d.get("message") or {}).get("content") if isinstance(d, dict) else None
            if not isinstance(content, list):
                continue
            ts = str(d.get("timestamp") or "")
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    name, ti = c.get("name"), c.get("input") or {}
                    if name in COMMAND_TOOLS and ti.get("command"):
                        yield ts, "commands", str(ti["command"])
                    elif name in PATH_TOOLS and (ti.get("file_path") or ti.get("notebook_path")):
                        yield ts, "paths", str(ti.get("file_path") or ti.get("notebook_path"))
                elif c.get("type") == "tool_result" and c.get("is_error"):
                    body = c.get("content")
                    if isinstance(body, list):
                        body = "\n".join(x.get("text", "") for x in body if isinstance(x, dict))
                    yield ts, "errors", str(body or "")[:ERROR_CHARS]


def build_sample(m: int = SAMPLE_M, root: Path | None = None) -> dict:
    """The last ``m`` rateable tool calls on this machine, newest transcripts first; a transcript
    older than the oldest call already kept cannot contribute, so reading stops. Texts are
    REDACTED: the sample is a second copy of transcript text on disk."""
    files = sorted(glob.glob(str((root or transcripts_dir()) / "*" / "*.jsonl")),
                   key=os.path.getmtime, reverse=True)
    calls: list = []
    for f in files:
        if len(calls) >= m:
            mt = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(os.path.getmtime(f)))
            if mt < calls[-1][0][:19]:
                break
        calls.extend(iter_calls(f))
        if len(calls) >= m:
            calls.sort(key=lambda c: c[0], reverse=True)
            del calls[m:]
    calls.sort(key=lambda c: c[0], reverse=True)
    calls = calls[:m]
    counts = {k: sum(1 for c in calls if c[1] == k) for k in ("commands", "paths", "errors")}
    return {"built_at": time.time(), "m": m, "counts": counts, "files": len(files),
            "span": [calls[-1][0] if calls else "", calls[0][0] if calls else ""],
            "calls": [[k, secretscan.redact(t)[0]] for _ts, k, t in calls]}


def _gkey(kind: str, trigger: str) -> str:
    return "%s\x1f%s" % (kind, trigger)


def _literal_key(trigger: str) -> str:
    words = [w for p in _WILD.split(trigger) for w in re.split(r"\s+", p) if w]
    return max(words, key=len).lower() if words else ""


def rate_triggers(pairs, sample: dict, examples: int = 0) -> dict:
    """{key(kind, trigger): [hits, kind_calls, [examples...]]} over the sample."""
    by_kind: dict = {"commands": [], "paths": [], "errors": []}
    for kind, text in sample.get("calls") or []:
        if kind in by_kind:
            by_kind[kind].append((text, text.lower()))
    out = {}
    for kind, t in pairs:
        calls = by_kind.get(kind) or []
        seen = []
        if kind == "paths":
            hits = 0
            for text, _low in calls:
                if _path_match(t, text):
                    hits += 1
                    if len(seen) < examples:
                        seen.append(text[:160])
        else:
            lit, rx = _literal_key(t), _text_rx(t)
            hits = 0
            for text, low in calls:
                if lit in low and rx.search(text):
                    hits += 1
                    if len(seen) < examples:
                        seen.append(" ".join(text.split())[:160])
        out[_gkey(kind, t)] = [hits, len(calls)] + ([seen] if examples else [])
    return out


def index_pairs(index: dict) -> set:
    return {(k, t) for r in index.get("records") or []
            for k in ("errors", "commands", "paths") for t in r["triggers"].get(k, [])}


def verdict(hits: int, n: int, rate: float = GUARD_RATE) -> bool:
    """True = drop: it matches too large a share of the calls of its kind to mean anything."""
    return n >= MIN_KIND_CALLS and hits / n > rate


def sample_stale(now: float | None = None) -> bool:
    try:
        return (now or time.time()) - sample_path().stat().st_mtime > SAMPLE_MAX_AGE_S
    except OSError:
        return True


def refresh(force_sample: bool = False, index: dict | None = None) -> dict:
    """Rebuild the sample when it is a day old (or forced), then rate every trigger in the current
    index that the guard has not rated against THIS sample."""
    lock = sample_path().with_suffix(".refreshing")
    try:
        sample = None if force_sample or sample_stale() else _read_json(sample_path())
        if not sample:
            sample = build_sample()
            _atomic_json(sample_path(), sample)
        guard = _read_json(guard_path()) or {}
        if (guard.get("version") != GUARD_VERSION or guard.get("sample_built_at") != sample["built_at"]
                or guard.get("rate") != GUARD_RATE):
            guard = {"version": GUARD_VERSION, "sample_built_at": sample["built_at"], "rate": GUARD_RATE,
                     "m": sample["m"], "counts": sample["counts"], "span": sample["span"], "rated": {}}
        index = index if index is not None else load_index()
        todo = [p for p in index_pairs(index) if _gkey(*p) not in guard["rated"]]
        guard["rated"].update(rate_triggers(todo, sample))
        guard["rated_at"] = time.time()
        _atomic_json(guard_path(), guard)
        return {"sample": {k: sample.get(k) for k in ("m", "counts", "span", "files")},
                "rated_now": len(todo), "rated_total": len(guard["rated"]),
                "guard_file": str(guard_path()).replace("\\", "/")}
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def request_refresh() -> bool:
    """Spawn ONE detached local refresher unless one started in the last REFRESH_LOCK_S."""
    import subprocess
    lock = sample_path().with_suffix(".refreshing")
    try:
        if time.time() - lock.stat().st_mtime < REFRESH_LOCK_S:
            return False
    except OSError:
        pass
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()), encoding="utf-8")
        root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
        flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0   # DETACHED | NEW_GROUP
        subprocess.Popen([sys.executable, "-m", "hub_core.client", "triggers", "--refresh"],
                         cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
                         start_new_session=(os.name != "nt"))
        return True
    except Exception:                                   # noqa: BLE001
        return False


def guarded(index: dict, guard: dict | None) -> tuple:
    """(index with guarded and unrated triggers removed, report)."""
    rated = (guard or {}).get("rated") or {}
    rate = (guard or {}).get("rate", GUARD_RATE)
    dropped, held, kept = {}, [], []
    for rec in index.get("records") or []:
        trig = {}
        for kind, values in rec["triggers"].items():
            if kind == "systems":
                trig[kind] = values
                continue
            keep = []
            for t in values:
                r = rated.get(_gkey(kind, t))
                if r is None:
                    held.append(t)
                elif verdict(r[0], r[1], rate=rate):
                    dropped[t] = {"kind": kind, "hits": r[0], "calls": r[1], "rate": round(r[0] / r[1], 5)}
                else:
                    keep.append(t)
            trig[kind] = keep
        if any(trig.get(k) for k in ("errors", "commands", "paths")):
            kept.append({**rec, "triggers": trig})
    return ({**index, "records": kept},
            {"state": "no guard yet" if guard is None else "applied", "dropped": dropped,
             "held": sorted(set(held)), "rate": (guard or {}).get("rate"), "m": (guard or {}).get("m"),
             "counts": (guard or {}).get("counts"), "span": (guard or {}).get("span")})


def load_guarded(mirror: Path | None = None) -> tuple:
    """What the hook matches against; asks for a refresh when the sample is a day old or the
    mirror brought triggers the guard has not rated."""
    index = load_index(mirror)
    if not index.get("records"):
        return index, {"state": "no triggers in the mirror", "dropped": {}, "held": []}
    guard = _read_json(guard_path())
    out, report = guarded(index, guard)
    if report["held"] or guard is None or sample_stale():
        report["refresh_requested"] = request_refresh()
    return out, report


# ── the precision signal ──

def _session_tag(sid) -> str:
    return hashlib.sha256(str(sid or "").encode("utf-8")).hexdigest()[:12]


def _append(rows: list) -> None:
    if not rows:
        return
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))


def short_id(rid: str) -> str:
    """What an agent writes when it cites: the last id segment."""
    return str(rid).rsplit(":", 1)[-1]


def record_fire(sid, transcript_path, fired: list, receipt: dict) -> None:
    """fired: [(record id, [triggers])] that LANDED in the emitted text."""
    if not fired:
        return
    try:
        offset = os.path.getsize(transcript_path) if transcript_path else 0
    except OSError:
        offset = 0
    now = time.time()
    for rid, trig in fired:
        receipt["injected"][rid] = {"triggers": trig, "at": now, "offset": offset}
    _append([{"t": round(now, 1), "ev": "fire", "id": rid, "triggers": trig, "s": _session_tag(sid)}
             for rid, trig in fired])


def _assistant_text(line: dict) -> str:
    if line.get("type") != "assistant":
        return ""
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text") or ""))
        elif isinstance(c, dict) and c.get("type") == "tool_use":
            parts.append(json.dumps(c.get("input") or {}, ensure_ascii=False))
    return "\n".join(parts)


def scan_cites(sid, transcript_path, receipt: dict) -> int:
    """Count cites of this session's pending injections in transcript bytes not yet scanned."""
    pending = {k: v for k, v in (receipt.get("injected") or {}).items() if not v.get("cited")}
    if not sid or not transcript_path or not pending:
        return 0
    start = min(int(v.get("scan", v.get("offset", 0)) or 0) for v in pending.values())
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(start)
            data = fh.read(MAX_SCAN_BYTES)
    except OSError:
        return 0
    end = data.rfind(b"\n")
    if end < 0:
        return 0
    cited, pos = {}, start
    for raw in data[:end + 1].split(b"\n"):
        line_start = pos
        pos += len(raw) + 1
        if not raw.strip() or b'"assistant"' not in raw:
            continue
        try:
            text = _assistant_text(json.loads(raw))
        except ValueError:
            continue
        for rid, v in pending.items():
            if rid not in cited and line_start >= int(v.get("offset") or 0) and short_id(rid) in text:
                cited[rid] = v
    for rid, v in pending.items():
        v["scan"] = start + end + 1
        if rid in cited:
            v["cited"] = True
    _append([{"t": round(time.time(), 1), "ev": "cite", "id": rid, "triggers": v.get("triggers") or [],
              "s": _session_tag(sid)} for rid, v in cited.items()])
    return len(cited)


def aggregate(path: Path | None = None) -> dict:
    by_t, by_r, fired, cited = {}, {}, 0, 0
    try:
        fh = open(path or ledger_path(), encoding="utf-8")
    except OSError:
        return {"fired": 0, "cited": 0, "by_trigger": {}, "by_record": {}}
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            i = 0 if r.get("ev") == "fire" else 1 if r.get("ev") == "cite" else None
            if i is None:
                continue
            fired += i == 0
            cited += i == 1
            by_r.setdefault(r.get("id"), [0, 0])[i] += 1
            for t in r.get("triggers") or []:
                by_t.setdefault(t, [0, 0])[i] += 1
    return {"fired": fired, "cited": cited, "by_trigger": by_t, "by_record": by_r}


# ── the hook ──

def run_hook(event: dict, mirror: Path | None = None) -> str:
    """One PreToolUse / PostToolUseFailure event -> the JSON the harness reads ("" = say nothing).
    Only what fits the emitted text is receipted and counted as fired."""
    name = str(event.get("hook_event_name") or "")
    if name not in ("PreToolUse", "PostToolUseFailure"):
        return ""
    sid, transcript = event.get("session_id"), event.get("transcript_path")
    receipt = load_receipt(sid) if sid else {"keys": [], "injected": {}}
    try:
        scan_cites(sid, transcript, receipt)
    except Exception:                                   # noqa: BLE001
        pass
    index, _report = load_guarded(mirror)
    chosen = []
    if index.get("records"):
        chosen, _passed = select(event, index, set(receipt.get("keys") or []))
    out = ""
    if chosen:
        text = envelope([block for _h, _k, block in chosen])[:OUTPUT_MAX]
        landed = [(h, key) for h, key, block in chosen if block in text]
        if landed:
            out = json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}})
            receipt["keys"] = list(receipt.get("keys") or []) + [key for _h, key in landed]
            try:
                record_fire(sid, transcript, [(h.record["id"], [t for _k, t in h.matched])
                                              for h, _key in landed], receipt)
            except Exception:                           # noqa: BLE001
                pass
    if sid and (chosen or receipt.get("injected")):
        save_receipt(sid, receipt)
    return out


def hook_main() -> int:
    """The per-tool-call entry point: harness event JSON on stdin, hook JSON (or nothing) on
    stdout, exit 0 on ANY fault. `client install-hooks` loads this module WITHOUT running
    ``hub_core/__init__`` (which imports the schema validator): measured, that import is ~0.4 s,
    and a hook that runs on every tool call must not pay for code it never uses."""
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
        out = run_hook(event) if isinstance(event, dict) else ""
        if out:
            sys.stdout.buffer.write(out.encode("utf-8"))
            sys.stdout.flush()
    except Exception:                                   # noqa: BLE001 - never block a tool call
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(hook_main())
