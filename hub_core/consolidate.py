"""Make the board's memory LEARN: fold what it knows twice, and say when each rule applies.

WHY THIS EXISTS. On the instance this was lifted from, about one new lesson in eight restated a
record already on the board: the same fact filed as a finding and then as a lesson by one author
on one day, or re-learned days later by a different agent because the first record never reached
them. The adjudicator (`hub_core.adjudicate`) reads suspected overlaps and records a verdict ON
the lesson, but a verdict nobody acts on changes nothing: a duplicate stays two records, both
served, both ranked, and the stack grows. It also sets lesson-vs-finding pairs aside as
`other-kind`, which is the commonest double filing there is. This pass is the half that ACTS:

  1. CANDIDATES, from one of two bases (the same relative criterion either way -- an absolute
     cosine means nothing across a model swap). LOCAL (`candidates`): every pair over unit
     vectors this machine's embedder produced, kept when the pair is an outlier for BOTH ends'
     own similarity distribution (z >= Z_MIN, the write-time tagger's criterion). HUB
     (`candidates_from_hub`): each record's neighbours from the hub's own `related.json`,
     computed on the hub, which holds the vectors -- semantic outliers plus exact-text lexical
     matches -- for a machine with no embedder. Two findings are never a candidate: there is no
     rule to fold into.
  2. A VERDICT from a judge model per pair not already judged at these exact texts (cached by
     text hash, so a run never re-reads what an earlier run read).
  3. A PLAN: duplicates fold into ONE canonical lesson (the most foundational, then the oldest,
     so every pointer to it stays good) that carries `reinforced_by` -- who else learned it,
     when, and their story, so no evidence is lost and the count becomes the rule's weight. A
     correction supersedes the rule it corrects. A contradiction is recorded on the lesson's
     `related` as a settled verdict, where `recall` prints it for a person. Nothing else.
  4. `applies_when` for every live lesson: the literal error text, the DEFECTIVE FORM of a
     command, the paths and the systems whose appearance means THIS rule is relevant now.
     Nearest-neighbour search over a failed command's error text rarely returns the record that
     explains it, so delivery at the moment of need has to match explicit triggers, not
     similarity. This field is what a tool-time hook matches on.
  5. A RE-LEARN count per ISO week: how many new lessons restated an existing one. The loop is
     working when that number falls.

WHAT IT REFUSES TO DO.
  * It never guesses: an unparseable verdict leaves the pair unjudged and counted.
  * It never deletes: a folded lesson is `superseded` with a pointer; the record's full prior
    state is written to the run log BEFORE each write, and `revert(<run>)` restores every
    record the run touched -- or `only` the ids named, because one wrong action in a run of
    sixty must be undoable without undoing the fifty-nine a reviewer approved.
  * An APPLY judges nothing new and derives no new triggers: it writes only the plan an earlier
    dry run printed and a person read. A reviewer's hold-backs (`exclude`) drop every action
    that touches a held record, printed as HELD BY REVIEWER.
  * It refuses a duplicate cluster larger than MAX_CLUSTER (chained "duplicates" are a sign of
    a loose verdict, not a rule said six times) and writes at most `apply_limit` records a run.

Stdlib only. The pass never touches a store: the caller injects `write(id, fields,
expected_version, idem) -> (ok, detail)` (the client posts through the served note route, the
one mutation entrance) and `get_current(id) -> dict | None` for revert. The judge is any
OpenAI-compatible chat endpoint (`HUB_JUDGE_URL` / `HUB_JUDGE_MODEL` / `HUB_JUDGE_TOKEN`, shared
with `adjudicate`).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import heapq
import json
import math
import re
import sqlite3
import time
from operator import mul
from pathlib import Path

from . import adjudicate as _adj
from . import knowledge as _k

AGENT = "consolidator"
KINDS = ("lesson", "finding")
Z_MIN = 3.0                  # the write-time tagger's relative criterion
COS_MIN = 0.60               # a floor, not the criterion
TOP_K = 3                    # neighbours kept per record
MAX_JUDGE = 400              # judge-model calls for verdicts per run
MAX_TRIGGERS = 300           # judge-model calls for applies_when per run
MAX_APPLY = 60               # hub writes per run
MAX_CLUSTER = 5
REINFORCED_MAX = 200
RELATED_MAX = 12             # the note schema's cap on `related`
VERDICTS = ("duplicate", "correction", "contradiction", "related", "unrelated")
TRIGGER_KEYS = ("errors", "commands", "paths", "systems")
#: Part of every trigger's cache key: bumping it re-derives every lesson's applies_when under the
#: current prompt. v2: command triggers name the defective form with <...> placeholders.
TRIGGER_VERSION = "trig-v2"
#: A trigger this generic fires on everything and teaches the reader to ignore the block.
GENERIC = {
    "git", "python", "pip", "error", "failed", "failure", "exception", "traceback", "timeout",
    "test", "tests", "deploy", "push", "commit", "ci", "pipeline", "job", "hub", "board",
    "django", "windows", "powershell", "bash", "curl", "http", "api", "json", "file", "files",
    "code", "agent", "session", "app", "apps", "server", "host", "run", "script", "command",
    "log", "logs", "sql", "database", "db", "model", "llm", "migrate", "git clone", "git push",
    "git pull", "npm install", "pip install", "no such file or directory",
    "the system cannot find the path specified", "permission denied", "access is denied",
    "not found", "command not found", "connection refused", "timed out", "readme.md",
}

_sumprod = getattr(math, "sumprod", None)


def _dot(a, b) -> float:
    return _sumprod(a, b) if _sumprod is not None else sum(map(mul, a, b))


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def kind_of(ent) -> str:
    return _k.knowledge_kind(ent) or ""


def when(ent) -> str:
    """When the record was FIRST written (the fold stamps provenance.created_at once)."""
    return str((ent.get("provenance") or {}).get("created_at") or "")


def author(ent) -> str:
    """Who learned it: provenance.created_by (stamped once). provenance.agent is the LAST
    writer -- the adjudicator or this pass after a verdict -- so it is the fallback only."""
    prov = ent.get("provenance") or {}
    return str(prov.get("created_by") or prov.get("agent") or "").strip().lower()


def rule_of(ent) -> str:
    return _norm(ent.get("title"))


def story_of(ent) -> str:
    return _norm(ent.get("body_md"))


def key_text(ent) -> str:
    """The text a verdict or a vector was computed from; its hash keys every cache."""
    return rule_of(ent) + "\n" + story_of(ent)


# --------------------------------------------------------------------------- #
# The local cache: verdicts, triggers and vectors, keyed by text hash
# --------------------------------------------------------------------------- #
def open_cache(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS consolidate_verdict ("
                 "a TEXT, a_sha TEXT, b TEXT, b_sha TEXT, verdict TEXT, keep TEXT, reason TEXT,"
                 " model TEXT, at TEXT, PRIMARY KEY (a, a_sha, b, b_sha))")
    conn.execute("CREATE TABLE IF NOT EXISTS consolidate_trigger ("
                 "id TEXT, sha TEXT, body TEXT, model TEXT, at TEXT, PRIMARY KEY (id, sha))")
    conn.execute("CREATE TABLE IF NOT EXISTS consolidate_vector ("
                 "text_sha TEXT, model TEXT, vec TEXT, at TEXT, PRIMARY KEY (text_sha, model))")
    conn.commit()
    return conn


def cached_vectors(conn, shas, model: str) -> dict:
    out = {}
    for s in set(shas):
        row = conn.execute("SELECT vec FROM consolidate_vector WHERE text_sha=? AND model=?",
                           (s, model)).fetchone()
        if row:
            out[s] = json.loads(row[0])
    return out


def store_vectors(conn, pairs, model: str) -> None:
    conn.executemany("INSERT OR REPLACE INTO consolidate_vector VALUES (?,?,?,?)",
                     [(s, model, json.dumps(v), _now()) for s, v in pairs])
    conn.commit()


# --------------------------------------------------------------------------- #
# 1. Candidates
# --------------------------------------------------------------------------- #
def live(notes) -> dict:
    """`{id: note}` for every live lesson and finding the board serves."""
    notes = [n for n in notes if isinstance(n, dict)]
    superseded = _k.superseded_ids({"entities": {n.get("id"): n for n in notes}})
    out = {}
    for ent in notes:
        if kind_of(ent) not in KINDS or _k.is_dead(ent, superseded):
            continue
        eid = str(ent.get("id") or "")
        if eid and rule_of(ent):
            out[eid] = ent
    return out


def candidates(records: dict, vectors: dict) -> list:
    """`[(a_id, b_id, cos, z_a, z_b)]` -- pairs that stand out for BOTH ends.

    Every pair once over the unit vectors; each record's mean and spread of similarity to
    everything else is accumulated in the same loop, so the relative criterion costs no second
    pass. With n records the largest z any single pair can reach is about sqrt(n - 2), so a board
    of fewer than eleven records cannot produce a candidate at Z_MIN = 3: `run` says so."""
    ids = [e for e in records if e in vectors]
    vecs = [vectors[e] for e in ids]
    n = len(ids)
    s1 = [0.0] * n
    s2 = [0.0] * n
    heaps = [[] for _ in range(n)]
    for i in range(n):
        vi = vecs[i]
        hi = heaps[i]
        for j in range(i + 1, n):
            c = _dot(vi, vecs[j])
            s1[i] += c
            s2[i] += c * c
            s1[j] += c
            s2[j] += c * c
            if c < COS_MIN:
                continue
            for h, other in ((hi, j), (heaps[j], i)):
                if len(h) < TOP_K:
                    heapq.heappush(h, (c, other))
                elif c > h[0][0]:
                    heapq.heapreplace(h, (c, other))
    stats = []
    for i in range(n):
        m = s1[i] / max(1, n - 1)
        var = max(0.0, s2[i] / max(1, n - 1) - m * m)
        stats.append((m, math.sqrt(var) or 1e-9))
    seen, out = set(), []
    for i in range(n):
        for c, j in heaps[i]:
            key = (min(i, j), max(i, j))
            if key in seen:
                continue
            za = (c - stats[i][0]) / stats[i][1]
            zb = (c - stats[j][0]) / stats[j][1]
            if min(za, zb) < Z_MIN:
                continue
            a, b = ids[key[0]], ids[key[1]]
            if kind_of(records[a]) != "lesson" and kind_of(records[b]) != "lesson":
                continue                     # two findings: no rule to fold into
            seen.add(key)
            out.append((a, b, round(c, 4), round(za, 2), round(zb, 2)))
    out.sort(key=lambda p: -p[2])
    return out


def candidates_from_hub(records: dict, lookup_related, *, limit: int = 0) -> tuple:
    """`([(a_id, b_id, similarity, None, None, basis)], lookups, blind)` -- every pair the hub's
    `related.json` says stands out, once. `lookup_related(id) -> (data, metadata)` is that
    route's shape. Lessons are looked up first. `blind` counts lookups where NEITHER basis ran on
    the hub (its metadata says so): a pass that found nothing for want of a basis must say so,
    never read as "no duplicates"."""
    order = sorted(records, key=lambda e: (kind_of(records[e]) != "lesson", when(records[e]), e))
    seen, out, lookups, blind = set(), [], 0, 0
    for eid in order:
        if limit and lookups >= limit:
            break
        try:
            data, meta = lookup_related(eid)
        except Exception:                                    # noqa: BLE001 - one record, not the pass
            continue
        lookups += 1
        meta = meta or {}
        if (meta.get("semantic") or {}).get("semantic") is False and \
                (meta.get("lexical") or {}).get("weighted") is False:
            blind += 1
        hits = [(h, "semantic") for h in (data.get("semantic") or []) if isinstance(h, dict)]
        hits += [(h, "exact") for h in (data.get("lexical") or [])
                 if isinstance(h, dict) and h.get("exact")]
        for h, basis in hits:
            other = h.get("id")
            if other not in records or other == eid:
                continue
            if kind_of(records[eid]) != "lesson" and kind_of(records[other]) != "lesson":
                continue
            key = tuple(sorted((eid, other)))
            if key in seen:
                continue
            seen.add(key)
            sim = h.get("similarity") if h.get("similarity") is not None else h.get("cosine")
            out.append((key[0], key[1], float(sim) if isinstance(sim, (int, float)) else 1.0,
                        None, None, basis))
    out.sort(key=lambda p: -p[2])
    return out, lookups, blind


# --------------------------------------------------------------------------- #
# 2. Verdicts
# --------------------------------------------------------------------------- #
JUDGE_PROMPT = """Two records from a team's shared engineering memory may say the same thing. Read both in full.

RECORD A ({a_kind}, written {a_at} by {a_by})
{a_rule}
Story: {a_why}

RECORD B ({b_kind}, written {b_at} by {b_by})
{b_rule}
Story: {b_why}

Decide how they relate:
- duplicate: the same rule or fact for the same situation, whatever the wording. Keeping both adds nothing a reader needs.
- correction: they are about the same situation, but one fixes, narrows or updates the other, so only one should stand.
- contradiction: in the same situation they tell the reader to do opposite things.
- related: same subject, but each says something the other does not; both should stand.
- unrelated: they only share words.

"keep" names the record a reader should be given: "A", "B", or "both" (for related/unrelated/contradiction).

Reply with JSON only: {{"verdict": "<one of the five>", "keep": "A|B|both", "reason": "<one sentence>"}}"""


def parse_judgement(text):
    """`{verdict, keep, reason}` or None. Never coerces: an unknown verdict, an unknown keep, or
    a self-contradictory pair (duplicate/correction with keep=both) is None, not the nearest."""
    raw = str(text or "")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    low = {str(k).strip().lower(): v for k, v in obj.items()}
    verdict = str(low.get("verdict") or "").strip().lower()
    keep = str(low.get("keep") or "").strip().upper()
    if verdict not in VERDICTS or keep not in ("A", "B", "BOTH"):
        return None
    if verdict in ("duplicate", "correction") and keep == "BOTH":
        return None
    return {"verdict": verdict, "keep": keep.lower() if keep == "BOTH" else keep,
            "reason": _norm(low.get("reason"))[:400] or "(no reason given)"}


def judge(a, b, cfg=None):
    prompt = JUDGE_PROMPT.format(
        a_kind=kind_of(a), a_at=when(a)[:10] or "?", a_by=author(a) or "?",
        a_rule=rule_of(a), a_why=story_of(a)[:1500] or "(none)",
        b_kind=kind_of(b), b_at=when(b)[:10] or "?", b_by=author(b) or "?",
        b_rule=rule_of(b), b_why=story_of(b)[:1500] or "(none)")
    reply = _adj.chat([{"role": "user", "content": prompt}], max_tokens=220, cfg=cfg)
    return parse_judgement(reply), reply


# --------------------------------------------------------------------------- #
# 4. applies_when
# --------------------------------------------------------------------------- #
TRIGGER_PROMPT = """This rule is from a team's shared engineering memory. An agent should be shown it at the exact moment it applies: when a command it runs prints a certain error, when it is about to run a certain command, or when it touches a certain file.

RULE
{rule}
Story: {why}

List the LITERAL strings whose appearance means this rule applies right now:
- errors: exact text that appears in the error or output (an exception name, an error code, a message fragment), copied as the tool prints it.
- commands: the DEFECTIVE FORM of a command line, never the bare tool. Write the parts that vary as <...> placeholders; each <...> matches one space-free value. Right: "grep -c <pat> <file> || echo 0", "git show <rev>:.<dotfile>", "sudo -S <cmd> <<", "git stash pop". Wrong: "grep -c", "git show", "migrate" (they match every use of the tool, and the rule is about one misuse). Leave commands empty if the rule is not about a particular misuse. A flag or setting that is only wrong on one kind of command is written WITH that command ("APP_AUTH_REQUIRED=false <...> manage.py test", never the bare "APP_AUTH_REQUIRED=false"), because the bare flag is also set on every command where it is harmless. When the misuse can be typed in more than one shell, give one trigger per form: POSIX "APP_AUTH_REQUIRED=false <...> manage.py test" and PowerShell "$env:APP_AUTH_REQUIRED=<...> manage.py test".
- paths: file names or globs whose editing is the moment (e.g. ".gitlab-ci.yml", "*/migrations/*.py", "deploy/credentials.json").
- systems: specific product or service names it is about (e.g. "mssql-django", "orders-db").

Only strings specific to THIS rule. A string that would match most commands or errors ("git", "error", "python", "timeout", "deploy", "migrate", "git clone") is wrong: leave it out, or make it specific ("git stash pop", "migrate <app> zero"). Error text must be what will appear AGAIN the next time someone hits this, never a one-off from the incident (a test name, a job id, a sha, a timestamp). A product or service name belongs in systems only when the rule is ABOUT that system. A path belongs in paths only when editing that file is itself the moment the rule matters. Empty lists are right when nothing specific applies. At most 5 per list.

Reply with JSON only: {{"errors": [], "commands": [], "paths": [], "systems": []}}"""


def parse_triggers(text):
    """`{errors, commands, paths, systems}` or None. Drops what would fire on everything: generic
    strings, incident debris (a sha, a timestamp), and a command trigger that names a TOOL rather
    than a MISUSE -- measured on real tool calls, two bare-tool triggers ("grep -c", "git show
    <rev>:<path>") matched hundreds of calls each and sank a hook's precision from about 64% to
    about 20%. A command is kept only if it carries a <placeholder>, a shell operator, `$`, an
    assignment, or three words."""
    raw = str(text or "")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    low = {str(k).strip().lower(): v for k, v in obj.items()}
    out = {}
    for key in TRIGGER_KEYS:
        vals = low.get(key) or []
        if not isinstance(vals, list):
            return None
        kept = []
        for v in vals:
            s = _norm(v)[:160]
            if len(s) < 4 or s.lower().strip("\"'`.") in GENERIC:
                continue
            if re.fullmatch(r"[0-9a-f]{7,40}", s) or re.search(r"\d{4}-\d\d-\d\dT\d\d", s):
                continue
            if key == "commands" and not (re.search(r"<[^>]+>|\|\||&&|\||<<|>|\$|=", s)
                                          or len(s.split()) >= 3):
                continue
            if s.lower() not in {k.lower() for k in kept}:
                kept.append(s)
        out[key] = kept[:5]
    return out


def triggers_for(ent, cfg=None):
    reply = _adj.chat([{"role": "user", "content": TRIGGER_PROMPT.format(
        rule=rule_of(ent), why=story_of(ent)[:1500] or "(none)")}], max_tokens=300, cfg=cfg)
    return parse_triggers(reply), reply


# --------------------------------------------------------------------------- #
# 3. Plan
# --------------------------------------------------------------------------- #
_TIER_RANK = {"foundational": 0, "normal": 2, "": 2}


def canonical(members, records):
    """The lesson every pointer should keep resolving to: the most foundational, then the oldest
    (first learned). Only a lesson can be canonical -- a finding carries no rule."""
    lessons = [m for m in members if kind_of(records[m]) == "lesson"]
    return sorted(lessons, key=lambda e: (_TIER_RANK.get(str(records[e].get("tier") or ""), 2),
                                          when(records[e]) or "9999", e))[0]


def plan(records, judged):
    """`judged`: [(a, b, judgement)]. Returns self-describing actions."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    actions = []
    for a, b, j in judged:
        if j["verdict"] == "duplicate":
            parent[find(a)] = find(b)
        elif j["verdict"] == "correction":
            loser, winner = (a, b) if j["keep"] == "B" else (b, a)
            if kind_of(records[loser]) == "lesson" and kind_of(records[winner]) == "lesson":
                actions.append({"op": "supersede", "old": loser, "new": winner, "reason": j["reason"]})
            else:
                actions.append({"op": "report", "why": "correction across kinds", "a": a, "b": b,
                                "reason": j["reason"]})
        elif j["verdict"] == "contradiction":
            lesson = a if kind_of(records[a]) == "lesson" else b
            other = b if lesson == a else a
            actions.append({"op": "contradiction", "lesson": lesson, "other": other,
                            "reason": j["reason"]})
    clusters = {}
    for x in list(parent):
        clusters.setdefault(find(x), set()).add(x)
    superseded = {act["old"] for act in actions if act["op"] == "supersede"}
    for members in clusters.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        if len(members) > MAX_CLUSTER:
            actions.append({"op": "report", "why": "cluster of %d is too large to trust" % len(members),
                            "members": members})
            continue
        if not any(kind_of(records[m]) == "lesson" for m in members):
            continue
        canon = canonical(members, records)
        actions.append({"op": "fold", "canonical": canon,
                        "members": [m for m in members if m != canon and m not in superseded]})
    return actions


def hold_back(actions, held, out=print):
    """Drop every action that touches a record a reviewer held back, printing each."""
    held = set(held or ())
    if not held:
        return list(actions)
    keep = []
    for act in actions:
        touched = {act.get(k) for k in ("canonical", "old", "new", "lesson", "other", "a", "b")}
        touched |= set(act.get("members") or [])
        if touched & held:
            out("HELD BY REVIEWER " + json.dumps(act))
            continue
        keep.append(act)
    return keep


def _parse_ts(ts):
    try:
        return _dt.datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _week(ts) -> str:
    d = _parse_ts(ts)
    if not d:
        return ""
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def relearn_stats(records, judged) -> dict:
    """Per ISO week: lessons filed, and how many restated an OLDER record (a duplicate verdict).
    `relearned` = a different author, or more than a day later -- the knowledge existed and did
    not reach them. `double_filed` = same author within a day -- a capture-hygiene miss."""
    weeks = {}
    for ent in records.values():
        if kind_of(ent) != "lesson":
            continue
        w = _week(when(ent))
        if w:
            weeks.setdefault(w, {"lessons": 0, "relearned": 0, "double_filed": 0})["lessons"] += 1
    counted = set()
    for a, b, j in judged:
        if j["verdict"] != "duplicate":
            continue
        older, newer = sorted((a, b), key=lambda e: when(records[e]) or "")
        if kind_of(records[newer]) != "lesson" or newer in counted:
            continue
        counted.add(newer)
        w = _week(when(records[newer]))
        if not w:
            continue
        da, db = _parse_ts(when(records[older])), _parse_ts(when(records[newer]))
        gap = abs((db - da).total_seconds()) if da and db else None
        same = author(records[older]) and author(records[older]) == author(records[newer])
        bucket = weeks.setdefault(w, {"lessons": 0, "relearned": 0, "double_filed": 0})
        if same and gap is not None and gap < 86400:
            bucket["double_filed"] += 1
        else:
            bucket["relearned"] += 1
    return dict(sorted(weeks.items()))


# --------------------------------------------------------------------------- #
# 5. Apply / revert
# --------------------------------------------------------------------------- #
#: The empty form of every field a run may ADD. A hub write MERGES over the stored record, so a
#: revert that re-sends the prior payload without a key the run added leaves that key standing;
#: revert therefore states each changed key explicitly, as its prior value or this empty form.
_EMPTY = {"tags": [], "reinforced_by": [], "applies_when": {}, "related": []}


def reinforcement(ent) -> dict:
    return {"agent": author(ent) or "?", "as": kind_of(ent) or "lesson",
            "at": when(ent)[:20] or "", "why": story_of(ent)[:600],
            "id": str(ent.get("id")), "rule": rule_of(ent)[:400]}


def apply(actions, records, triggers, run_id, *, write, log_dir, model="", out=print,
          limit=MAX_APPLY) -> dict:
    """Write the plan through `write`. Every write logs the record's full prior state first, so
    a crash between the log line and the write loses nothing a revert needs."""
    t = {"written": 0, "refused": 0, "skipped_limit": 0, "unchanged": 0}
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / ("%s.jsonl" % run_id)
    with open(path, "a", encoding="utf-8") as log:
        def do(ent, fields, why):
            if t["written"] >= limit:
                t["skipped_limit"] += 1
                return
            eid = str(ent.get("id"))
            log.write(json.dumps({"run": run_id, "id": eid, "kind": kind_of(ent), "why": why,
                                  "fields": sorted(fields), "prior_version": ent.get("version"),
                                  "prior": {k: v for k, v in ent.items()
                                            if k not in ("version", "provenance")}}) + "\n")
            log.flush()
            ok, detail = write(eid, fields, ent.get("version"),
                               "consolidate:%s:%s:v%s" % (run_id, eid, ent.get("version")))
            if ok:
                t["written"] += 1
                records[eid] = dict(ent, **fields, version=(ent.get("version") or 0) + 1)
                out("WROTE %s (%s)" % (eid, why))
            else:
                t["refused"] += 1
                out("WRITE REFUSED %s %s" % (eid, str(detail)[:300]))

        for act in actions:
            if act["op"] == "fold":
                canon = records[act["canonical"]]
                have = {r.get("id") for r in (canon.get("reinforced_by") or [])}
                added = [reinforcement(records[m]) for m in act["members"] if m not in have]
                if added:
                    rb = (list(canon.get("reinforced_by") or []) + added)[-REINFORCED_MAX:]
                    do(canon, {"reinforced_by": rb},
                       "fold: reinforced by %s" % ",".join(a["id"] for a in added))
                for m in act["members"]:
                    ent = records[m]
                    mark = "folded-into:%s" % act["canonical"]
                    if mark in (ent.get("tags") or []):
                        t["unchanged"] += 1
                        continue
                    tags = [x for x in (ent.get("tags") or []) if not str(x).startswith("folded-into:")]
                    fields = {"tags": tags + [mark, "consolidate:%s" % run_id]}
                    if kind_of(ent) == "lesson":
                        fields["status"] = "superseded"
                        fields["superseded_by"] = act["canonical"]
                    do(ent, fields, "fold into %s" % act["canonical"])
            elif act["op"] == "supersede":
                ent = records[act["old"]]
                do(ent, {"status": "superseded", "superseded_by": act["new"],
                         "tags": list(ent.get("tags") or []) + ["consolidate:%s" % run_id]},
                   "corrected by %s: %s" % (act["new"], act["reason"]))
            elif act["op"] == "contradiction":
                ent = records[act["lesson"]]
                related = [dict(r) for r in (ent.get("related") or []) if isinstance(r, dict)]
                if any(r.get("id") == act["other"] for r in related) or len(related) >= RELATED_MAX:
                    t["unchanged"] += 1
                    continue
                related.append({"id": act["other"], "kind": kind_of(records[act["other"]]),
                                "basis": "semantic", "adjudicated": True, "verdict": "contradiction",
                                "reason": act["reason"], "adjudicated_by": model or "model",
                                "adjudicated_at": _now()})
                do(ent, {"related": related}, "contradiction with %s" % act["other"])
        # A fold retires the member, so its triggers would retire with it: the canonical rule
        # inherits them (a member's path trigger can be the only one that ever fires).
        inherit = {}
        for act in actions:
            if act["op"] == "fold":
                inherit.setdefault(act["canonical"], []).extend(act["members"])
        for canon, members in inherit.items():
            if canon not in triggers:
                continue
            base_sha, body = triggers[canon]
            merged = {k: list(body.get(k) or []) for k in TRIGGER_KEYS}
            for m in members:
                for k in TRIGGER_KEYS:
                    for v in ((triggers.get(m) or (None, {}))[1].get(k) or []):
                        if v.lower() not in {x.lower() for x in merged[k]} and len(merged[k]) < 5:
                            merged[k].append(v)
            triggers[canon] = (base_sha + ":" + ",".join(sorted(members))[:40], merged)
        for eid, (tsha, body) in triggers.items():
            ent = records.get(eid)
            if not ent or kind_of(ent) != "lesson" or ent.get("status") in _k.DEAD_STATUS:
                continue
            if (ent.get("applies_when") or {}).get("from_sha") == tsha:
                continue
            do(ent, {"applies_when": dict(body, from_sha=tsha, derived_by=model or "model",
                                          derived_at=_now())}, "applies_when")
    t["log"] = str(path)
    return t


def revert(run_id, *, log_dir, get_current, write, only=(), out=print) -> dict:
    """Restore every record a run wrote -- or only the ids in `only` -- to the state it had
    before the run's FIRST write to it. Every key the run wrote is stated explicitly (prior
    value, or the empty form; a lesson's status defaults to `standing`) because a write merges.
    A leftover `superseded_by` on a restored lesson is inert: liveness is `status` and the
    `supersedes` pointers, and an idref cannot be written empty.

    The idempotency key is per REVERT, not per (run, id): a second, corrective revert of the
    same record must write, not replay the first as a no-op."""
    path = Path(log_dir) / ("%s.jsonl" % run_id)
    if not path.exists():
        raise ValueError("no such run log: %s" % path)
    first, fields = {}, {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if only and row["id"] not in only:
            continue
        first.setdefault(row["id"], row)
        fields.setdefault(row["id"], set()).update(row.get("fields") or [])
    missing = sorted(set(only or ()) - set(first))
    if missing:
        out("REVERT: not written by run %s: %s" % (run_id, missing))
    t = {"restored": 0, "refused": 0, "gone": 0, "not_in_run": missing}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for eid, row in first.items():
        cur = get_current(eid)
        if not cur:
            t["gone"] += 1
            continue
        prior = row.get("prior") or {}
        body = {}
        for key in sorted(fields[eid] | {"status", "tags"}):
            if key == "superseded_by":
                if prior.get(key):
                    body[key] = prior[key]
                continue
            if key == "status":
                body[key] = prior.get("status") or "standing"
                continue
            if key in prior:
                body[key] = prior[key]
            elif key in _EMPTY:
                body[key] = _EMPTY[key]
        ok, detail = write(eid, body, cur.get("version"),
                           "consolidate-revert:%s:%s:%s" % (run_id, eid, stamp))
        if ok:
            t["restored"] += 1
            out("RESTORED %s status=%s" % (eid, body.get("status")))
        else:
            t["refused"] += 1
            out("REVERT REFUSED %s %s" % (eid, str(detail)[:300]))
    return t


# --------------------------------------------------------------------------- #
# The trigger line: parse it completely or refuse
# --------------------------------------------------------------------------- #
class TriggerRefused(ValueError):
    """The line asks for something this parse does not produce; nothing may run."""


def parse_trigger_line(line: str) -> dict:
    """`apply [limit=N] [except <id>,<id>]` | `revert <run> [only <id>,<id>]` | anything else
    (a dry run). Every token after `apply`/`revert` must be understood: an ignored hold-back or
    cap must stop the run, never be written through (on the source instance a reviewer's
    hold-back was silently dropped and the held record was superseded)."""
    words = str(line or "").split()
    verb = words[0].lower() if words else ""
    spec = {"mode": "dry-run", "apply_limit": None, "exclude": [], "only": [], "run": ""}
    if verb not in ("apply", "revert"):
        return spec
    rest = words[1:]
    if verb == "revert":
        if not rest or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", rest[0]):
            raise TriggerRefused("revert needs a run id (YYYYMMDDTHHMMSSZ), got %r" % (rest[:1],))
        spec.update(mode="revert", run=rest[0])
        rest = rest[1:]
    else:
        spec["mode"] = "apply"
    i = 0
    while i < len(rest):
        tok = rest[i]
        if verb == "apply" and tok.lower().startswith("limit="):
            if not re.fullmatch(r"limit=[0-9]+", tok, re.I) or spec["apply_limit"] is not None:
                raise TriggerRefused("unreadable limit %r" % tok)
            spec["apply_limit"] = int(tok.split("=", 1)[1])
            i += 1
            continue
        list_word = "except" if verb == "apply" else "only"
        if tok.lower() == list_word:
            ids_ = [x for x in (rest[i + 1].split(",") if i + 1 < len(rest) else []) if x]
            if not ids_ or any(x.count(":") < 2 for x in ids_) or spec["exclude" if verb == "apply" else "only"]:
                raise TriggerRefused("%s needs a comma-separated list of full record ids" % list_word)
            spec["exclude" if verb == "apply" else "only"] = ids_
            i += 2
            continue
        raise TriggerRefused("token %r is not understood by this parse" % tok)
    return spec


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #
def run(records, vectors, conn, *, write, log_dir, apply_writes=False, judge_limit=MAX_JUDGE,
        trigger_limit=MAX_TRIGGERS, apply_limit=MAX_APPLY, exclude=(), cfg=None, out=print,
        pairs=None) -> dict:
    """One pass over `records` ({id: note}, from `live`) with `vectors` ({id: unit vector}), or
    with `pairs` already found by `candidates_from_hub` (then `vectors` is unused).

    An APPLY judges nothing new and derives no new triggers: it writes only what an earlier dry
    run printed and a person read (the verdict and trigger caches live in `conn`, so an apply
    must use the same cache as the dry run it applies). Otherwise pairs judged in the same run as
    the apply would be written unread and the review would be a formality."""
    cfg = cfg or _adj.judge_config()
    model = cfg.get("model") or "model"
    if apply_writes:
        judge_limit, trigger_limit = 0, 0
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    t = {"run": run_id, "records": len(records), "with_vector": len(vectors or {}),
         "mode": "apply" if apply_writes else "dry-run",
         "basis": "hub" if pairs is not None else "local"}
    t0 = time.time()
    if pairs is None:
        pairs = [p + ("vector",) for p in candidates(records, vectors)]
        out("CANDIDATES %d pairs from %d records in %.1fs" % (len(pairs), len(vectors),
                                                              time.time() - t0))
    else:
        out("CANDIDATES %d pairs from %d records (the hub's related.json)" % (len(pairs),
                                                                          len(records)))
    t["pairs"] = len(pairs)
    if t["basis"] == "local" and len(vectors) < 11:
        t["note"] = ("%d records with vectors: a pair can reach z >= %.0f only on a board of about "
                     "eleven or more, so no candidate here is a statement about this board's size, "
                     "not about its duplicates" % (len(vectors), Z_MIN))
        out("NOTE " + t["note"])

    model_ok, model_detail = None, ""
    judged, t["judged_cached"], t["judged_now"], t["unparsed"], t["unjudged"] = [], 0, 0, 0, 0
    for a, b, cos, za, zb, basis in pairs:
        ea, eb = records[a], records[b]
        # A is always the OLDER record, so "keep" and every printed pair read the same way.
        if (when(ea) or "") > (when(eb) or ""):
            a, b, ea, eb = b, a, eb, ea
        sa, sb = sha(key_text(ea)), sha(key_text(eb))
        row = conn.execute("SELECT verdict, keep, reason FROM consolidate_verdict WHERE a=? AND a_sha=? "
                           "AND b=? AND b_sha=?", (a, sa, b, sb)).fetchone()
        if row:
            judged.append((a, b, {"verdict": row[0], "keep": row[1], "reason": row[2]}))
            t["judged_cached"] += 1
            continue
        if t["judged_now"] >= judge_limit:
            t["unjudged"] += 1
            continue
        if model_ok is None:
            model_ok, model_detail = _adj.model_reachable(cfg)
            out("MODEL %s -- %s" % ("OK" if model_ok else "UNAVAILABLE", model_detail))
        if not model_ok:
            t["unjudged"] += 1
            continue
        try:
            j, reply = judge(ea, eb, cfg)
        except Exception as exc:                                  # noqa: BLE001
            j, reply = None, "%s: %s" % (type(exc).__name__, exc)
        t["judged_now"] += 1
        if j is None:
            t["unparsed"] += 1
            out("UNPARSED %s ~ %s: %r" % (a, b, str(reply)[:160]))
            continue
        conn.execute("INSERT OR REPLACE INTO consolidate_verdict VALUES (?,?,?,?,?,?,?,?,?)",
                     (a, sa, b, sb, j["verdict"], j["keep"], j["reason"], model, _now()))
        conn.commit()
        judged.append((a, b, j))
        where = ("z=%.1f/%.1f" % (za, zb)) if za is not None and zb is not None else basis
        out("VERDICT %-13s keep=%-4s cos=%.3f %s\n   A %s [%s %s] %s\n   B %s [%s %s] %s\n   why: %s"
            % (j["verdict"], j["keep"], cos, where,
               a, author(ea), when(ea)[:10], rule_of(ea)[:220],
               b, author(eb), when(eb)[:10], rule_of(eb)[:220], j["reason"]))
    t["verdicts"] = {}
    for _a, _b, j in judged:
        t["verdicts"][j["verdict"]] = t["verdicts"].get(j["verdict"], 0) + 1

    actions = hold_back(plan(records, judged), exclude, out=out)
    t["actions"] = {}
    for act in actions:
        t["actions"][act["op"]] = t["actions"].get(act["op"], 0) + 1
        out("ACTION " + json.dumps(act))

    triggers, t["triggers_now"], t["triggers_cached"], t["triggers_unparsed"] = {}, 0, 0, 0
    t["triggers_underived"] = 0
    lessons = sorted((e for e in records.values() if kind_of(e) == "lesson"),
                     key=lambda e: when(e) or "", reverse=True)
    for ent in lessons:
        eid = str(ent.get("id"))
        tsha = sha(key_text(ent) + "\n" + TRIGGER_VERSION)
        have = str((ent.get("applies_when") or {}).get("from_sha") or "")
        if have == tsha or have.startswith(tsha + ":"):
            continue                 # derived from this text already (":" = inherited from a fold)
        row = conn.execute("SELECT body FROM consolidate_trigger WHERE id=? AND sha=?", (eid, tsha)).fetchone()
        if row:
            triggers[eid] = (tsha, json.loads(row[0]))
            t["triggers_cached"] += 1
            continue
        if t["triggers_now"] >= trigger_limit:
            t["triggers_underived"] += 1
            continue
        if model_ok is None:
            model_ok, model_detail = _adj.model_reachable(cfg)
            out("MODEL %s -- %s" % ("OK" if model_ok else "UNAVAILABLE", model_detail))
        if not model_ok:
            t["triggers_underived"] += 1
            continue
        try:
            body, reply = triggers_for(ent, cfg)
        except Exception as exc:                                  # noqa: BLE001
            body, reply = None, "%s: %s" % (type(exc).__name__, exc)
        t["triggers_now"] += 1
        if body is None:
            t["triggers_unparsed"] += 1
            out("TRIGGER UNPARSED %s: %r" % (eid, str(reply)[:160]))
            continue
        conn.execute("INSERT OR REPLACE INTO consolidate_trigger VALUES (?,?,?,?,?)",
                     (eid, tsha, json.dumps(body), model, _now()))
        conn.commit()
        triggers[eid] = (tsha, body)
        if t["triggers_now"] <= 40:
            out("TRIGGER %s | %s\n   %s" % (eid, rule_of(ent)[:160], json.dumps(body)))
    # A hold-back means "do not touch this record" -- its applies_when included.
    for eid in sorted(set(exclude or ()) & set(triggers)):
        triggers.pop(eid)
        out("HELD BY REVIEWER applies_when %s" % eid)
    t["triggers_empty"] = sum(1 for _s, b in triggers.values() if not any(b.get(k) for k in TRIGGER_KEYS))
    t["model"] = model_detail or "not needed"
    # Judging or deriving left undone for want of a model is LOUD (the caller exits non-zero):
    # a scheduled pass that quietly settles nothing is how a board keeps its duplicates forever.
    t["needs_model"] = model_ok is False and bool(t["unjudged"] or t["triggers_underived"])

    t["relearn"] = relearn_stats(records, judged)
    out("RELEARN " + json.dumps(t["relearn"]))
    if apply_writes:
        t["apply"] = apply(actions, records, triggers, run_id, write=write, log_dir=log_dir,
                           model=model, out=out, limit=apply_limit)
    return t
