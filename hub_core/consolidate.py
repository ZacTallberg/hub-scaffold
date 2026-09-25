"""Make the board's memory LEARN: fold what it knows twice, and say when each rule applies.

WHY THIS EXISTS. On the instance this was lifted from, about one new lesson in eight restated a
record that was already on the board -- the same fact filed as a finding and then as a lesson by
one author on one day, or re-learned days later by a different agent because the first record
never reached them. The adjudication pass (hub_core.adjudicate) already READS suspected overlaps
and records a verdict on the lesson; nothing ACTED on the verdicts, so a duplicate stayed two
records, both served, both ranked, and the stack grew. This pass is the half that acts:

  1. CANDIDATES. For every live lesson and finding, the hub's own ``related.json`` (computed on
     the hub, which holds the vectors): semantic neighbours that are outliers for this corpus
     (the relative z criterion -- an absolute cosine means nothing across a model swap) and
     exact-text lexical matches. A pair of two findings is skipped: there is no rule to fold into.
  2. A VERDICT per pair from the judge model (HUB_JUDGE_URL / HUB_JUDGE_MODEL, the same seam the
     adjudication pass uses) -- duplicate / correction / contradiction / related / unrelated, and
     which record a reader should be given -- cached by the hash of both texts, so a night never
     re-reads what it read yesterday.
  3. A PLAN. Duplicates fold into ONE canonical lesson (foundational first, then the oldest, so
     every pointer to it stays good) that carries ``reinforced_by`` -- who else learned it, when,
     and their story -- so no evidence is lost and the count becomes the rule's weight; the folded
     records are superseded with a pointer. A correction supersedes the rule it corrects. A
     contradiction is recorded on the lesson (``related``, adjudicated) for a person to reconcile.
     A cluster larger than MAX_CLUSTER is REPORTED, never folded: chained "duplicates" are a sign
     of a loose verdict, not of a rule said six times.
  4. ``applies_when`` for every live lesson: the literal error text, command fragments, paths and
     systems whose appearance means THIS rule applies now. Nearest-neighbour search over a failed
     command's error text returned a relevant record about 4 times in 30 on the origin instance,
     so delivery at the moment of need has to match explicit triggers, not similarity; this field
     is what a tool-time hook matches on.
  5. A RE-LEARN READING per ISO week: lessons filed, how many restated an older record (another
     author or a later day: the knowledge existed and did not reach them) and how many were the
     same author double-filing within a day. The loop is working when that number falls.

WHAT IT REFUSES TO DO. It never guesses: an unparseable verdict leaves the pair unjudged and
counted. It never deletes: a folded record is ``superseded`` with a pointer, its prior fields are
written to a local run log first, and ``--revert <run>`` restores every field the run changed. It
applies at most MAX_APPLY writes per run. The DEFAULT IS A DRY RUN that prints every proposed
action with both texts, so a person reads the proposals before a run with ``--apply`` writes.
Every write goes through the served note API (optimistic concurrency, schema, realtime) -- never
the ledger file.

Pure over injected callables, like hub_core.adjudicate; the client verb ``consolidate`` wires it
to the served API. Stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from pathlib import Path

from . import adjudicate as _adj

KINDS = ("lesson", "finding")
MAX_JUDGE = 400              # judge calls for verdicts per run
MAX_TRIGGERS = 300           # judge calls for applies_when per run
MAX_APPLY = 60               # ledger writes per run
MAX_CLUSTER = 5
REINFORCED_KEEP = 200
VERDICTS = ("duplicate", "correction", "contradiction", "related", "unrelated")
TRIGGER_KEYS = ("errors", "commands", "paths", "systems")
#: A trigger this generic fires on everything and teaches the reader to ignore the block.
GENERIC = {
    "git", "python", "pip", "error", "failed", "failure", "exception", "traceback", "timeout",
    "test", "tests", "deploy", "push", "commit", "ci", "pipeline", "job", "hub", "django",
    "windows", "linux", "powershell", "bash", "curl", "http", "api", "json", "file", "files",
    "code", "agent", "session", "app", "apps", "server", "host", "run", "script", "command",
    "log", "logs", "sql", "database", "db", "model", "llm", "migrate", "git clone", "git push",
    "git pull", "npm install", "pip install", "no such file or directory", "permission denied",
    "access is denied", "not found", "command not found", "connection refused", "timed out",
}


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def kind_of(ent) -> str:
    tags = {str(t).lower() for t in (ent.get("tags") or [])}
    return next((k for k in KINDS if k in tags), "")


def when(ent) -> str:
    """When the record was FIRST written (provenance.created_at); agent is the last writer."""
    return str((ent.get("provenance") or {}).get("created_at") or "")


def author(ent) -> str:
    prov = ent.get("provenance") or {}
    return str(prov.get("created_by") or prov.get("agent") or "").strip().lower()


def rule_of(ent) -> str:
    return _norm(ent.get("title"))


def story_of(ent) -> str:
    return _norm(ent.get("body_md"))


def _key_text(ent) -> str:
    return rule_of(ent) + "\n" + story_of(ent)


def live(notes) -> dict:
    """{id: note} for every live lesson and finding."""
    out = {}
    for ent in notes or []:
        if not isinstance(ent, dict) or ent.get("type") != "note":
            continue
        if str(ent.get("status") or "standing") != "standing" or not kind_of(ent):
            continue
        if ent.get("id") and rule_of(ent):
            out[ent["id"]] = ent
    return out


# ── 1. candidates ────────────────────────────────────────────────────────────────────────────

def candidates(records: dict, lookup_related, *, limit: int = 0) -> tuple:
    """``([(a, b, similarity, basis)], lookups)`` -- every pair the hub says stands out, once.

    ``lookup_related(id) -> ({"lexical": [...], "semantic": [...]}, metadata)`` is the shape of
    ``related.json``. Lessons are looked up first (a pair of findings is never a fold)."""
    order = sorted(records, key=lambda e: (kind_of(records[e]) != "lesson", when(records[e]), e))
    seen, out, lookups = set(), [], 0
    for eid in order:
        if limit and lookups >= limit:
            break
        try:
            data, _meta = lookup_related(eid)
        except Exception:                                    # noqa: BLE001 - one record, not the pass
            continue
        lookups += 1
        hits = [(h, "semantic") for h in (data.get("semantic") or []) if isinstance(h, dict)]
        hits += [(h, "exact") for h in (data.get("lexical") or []) if isinstance(h, dict) and h.get("exact")]
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
            out.append((key[0], key[1], float(sim) if isinstance(sim, (int, float)) else 1.0, basis))
    out.sort(key=lambda p: -p[2])
    return out, lookups


# ── 2. verdicts ──────────────────────────────────────────────────────────────────────────────

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
    """``{verdict, keep, reason}`` or None. Shape-tolerant; never coerces: an unknown verdict,
    or a duplicate/correction that keeps both (self-contradictory), is refused, not repaired."""
    m = re.search(r"\{.*\}", str(text or ""), re.S)
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
    return {"verdict": verdict, "keep": "both" if keep == "BOTH" else keep,
            "reason": _norm(low.get("reason"))[:400] or "(no reason given)"}


def judge(a, b, chat=None):
    chat = chat or _adj.chat
    prompt = JUDGE_PROMPT.format(
        a_kind=kind_of(a), a_at=when(a)[:10] or "?", a_by=author(a) or "?",
        a_rule=rule_of(a), a_why=story_of(a)[:1500] or "(none)",
        b_kind=kind_of(b), b_at=when(b)[:10] or "?", b_by=author(b) or "?",
        b_rule=rule_of(b), b_why=story_of(b)[:1500] or "(none)")
    reply = chat([{"role": "user", "content": prompt}], max_tokens=220)
    return parse_judgement(reply), reply


# ── 4. applies_when ──────────────────────────────────────────────────────────────────────────

TRIGGER_PROMPT = """This rule is from a team's shared engineering memory. An agent should be shown it at the exact moment it applies: when a command it runs prints a certain error, when it is about to run a certain command, or when it touches a certain file.

RULE
{rule}
Story: {why}

List the LITERAL strings whose appearance means this rule applies right now:
- errors: exact text that appears in the error or output (an exception name, an error code, a message fragment), copied as the tool prints it.
- commands: fragments of the command line that are the moment to recall it (e.g. "git stash", "migrate <app> zero", "sudo -S").
- paths: file names or globs whose editing is the moment (e.g. ".gitlab-ci.yml", "*/migrations/*.py").
- systems: specific product or service names the rule is about.

Only strings specific to THIS rule. A string that would match most commands or errors ("git", "error", "python", "timeout", "deploy", "git clone") is wrong: leave it out, or make it specific. Error text must be what will appear AGAIN the next time someone hits this, never a one-off from the incident (a test name, a job id, a sha, a timestamp). A name belongs in systems only when the rule is ABOUT that system; a path belongs in paths only when editing that file is itself the moment. Empty lists are right when nothing specific applies. At most 5 per list.

Reply with JSON only: {{"errors": [], "commands": [], "paths": [], "systems": []}}"""


def parse_triggers(text):
    m = re.search(r"\{.*\}", str(text or ""), re.S)
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
            # Incident debris: a sha or a timestamp will never be seen again.
            if re.fullmatch(r"[0-9a-f]{7,40}", s) or re.search(r"\d{4}-\d\d-\d\dT\d\d", s):
                continue
            if s.lower() not in {k.lower() for k in kept}:
                kept.append(s)
        out[key] = kept[:5]
    return out


def triggers_for(ent, chat=None):
    chat = chat or _adj.chat
    reply = chat([{"role": "user", "content": TRIGGER_PROMPT.format(
        rule=rule_of(ent), why=story_of(ent)[:1500] or "(none)")}], max_tokens=300)
    return parse_triggers(reply), reply


# ── 3. the plan ──────────────────────────────────────────────────────────────────────────────

def canonical(members, records) -> str:
    """The lesson every pointer should keep resolving to: foundational first, then the oldest.
    Only a lesson can be canonical -- a finding carries no rule to fold into."""
    lessons = [m for m in members if kind_of(records[m]) == "lesson"]
    return sorted(lessons, key=lambda e: (str(records[e].get("tier") or "") != "foundational",
                                          when(records[e]) or "9999", e))[0]


def plan(records, judged) -> list:
    """``judged``: [(a, b, judgement)]. A list of self-describing actions."""
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
            actions.append({"op": "contradiction", "lesson": lesson,
                            "other": b if lesson == a else a, "reason": j["reason"]})
    clusters = {}
    for x in list(parent):
        clusters.setdefault(find(x), set()).add(x)
    superseded = {act["old"] for act in actions if act["op"] == "supersede"}
    for members in clusters.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        if len(members) > MAX_CLUSTER:
            actions.append({"op": "report", "why": "a cluster of %d is too large to trust" % len(members),
                            "members": members})
            continue
        if not any(kind_of(records[m]) == "lesson" for m in members):
            continue
        canon = canonical(members, records)
        rest = [m for m in members if m != canon and m not in superseded]
        if rest:
            actions.append({"op": "fold", "canonical": canon, "members": rest})
    return actions


def _week(ts) -> str:
    try:
        d = _dt.datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def _gap_s(a, b):
    try:
        da = _dt.datetime.strptime(str(a)[:19], "%Y-%m-%dT%H:%M:%S")
        db = _dt.datetime.strptime(str(b)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return abs((db - da).total_seconds())


def relearn_stats(records, judged) -> dict:
    """Per ISO week: lessons filed, how many RE-LEARNED an older record (another author, or a
    later day), and how many were one author DOUBLE-FILING within a day."""
    weeks = {}
    for ent in records.values():
        if kind_of(ent) == "lesson" and _week(when(ent)):
            weeks.setdefault(_week(when(ent)), {"lessons": 0, "relearned": 0, "double_filed": 0})["lessons"] += 1
    counted = set()
    for a, b, j in judged:
        if j["verdict"] != "duplicate":
            continue
        older, newer = sorted((a, b), key=lambda e: when(records[e]) or "")
        if kind_of(records[newer]) != "lesson" or newer in counted or not _week(when(records[newer])):
            continue
        counted.add(newer)
        bucket = weeks.setdefault(_week(when(records[newer])),
                                  {"lessons": 0, "relearned": 0, "double_filed": 0})
        gap = _gap_s(when(records[older]), when(records[newer]))
        same = author(records[older]) and author(records[older]) == author(records[newer])
        bucket["double_filed" if same and gap is not None and gap < 86400 else "relearned"] += 1
    return dict(sorted(weeks.items()))


# ── 5. apply / revert ────────────────────────────────────────────────────────────────────────

def _reinforcement(ent) -> dict:
    return {"id": str(ent.get("id")), "agent": author(ent) or "?", "as": "fold",
            "at": when(ent)[:20], "rule": rule_of(ent)[:400], "why": story_of(ent)[:600]}


def apply(actions, records, triggers, run_id, *, write, log_path, limit=MAX_APPLY, out=print) -> dict:
    """Write the plan through ``write(id, fields, expected_version) -> (ok, detail)``. Before
    every write, the PRIOR value of each field it changes is appended to ``log_path``, so
    ``revert`` can restore it."""
    tally = {"written": 0, "refused": 0, "skipped_limit": 0}
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        def do(ent, fields, why):
            if tally["written"] >= limit:
                tally["skipped_limit"] += 1
                return
            log.write(json.dumps({"run": run_id, "id": ent["id"], "why": why,
                                  "prior": {k: ent.get(k) for k in fields}}) + "\n")
            log.flush()
            ok, detail = write(ent["id"], fields, ent.get("version"))
            if ok:
                tally["written"] += 1
                records[ent["id"]] = dict(ent, **fields, version=(ent.get("version") or 0) + 1)
            else:
                tally["refused"] += 1
                out("WRITE REFUSED %s: %s" % (ent["id"], str(detail)[:300]))

        for act in actions:
            if act["op"] == "fold":
                canon = records[act["canonical"]]
                have = {r.get("id") for r in (canon.get("reinforced_by") or []) if isinstance(r, dict)}
                added = [_reinforcement(records[m]) for m in act["members"] if m not in have]
                if added:
                    do(canon, {"reinforced_by": (list(canon.get("reinforced_by") or []) + added)[-REINFORCED_KEEP:]},
                       "fold: reinforced by %s" % ",".join(x["id"] for x in added))
                for m in act["members"]:
                    ent = records[m]
                    tags = [t for t in (ent.get("tags") or []) if not str(t).startswith("folded-into")]
                    do(ent, {"status": "superseded", "superseded_by": act["canonical"],
                             "tags": tags + ["folded", "consolidate-%s" % run_id.lower()]},
                       "fold into %s" % act["canonical"])
            elif act["op"] == "supersede":
                ent = records[act["old"]]
                do(ent, {"status": "superseded", "superseded_by": act["new"],
                         "tags": list(ent.get("tags") or []) + ["consolidate-%s" % run_id.lower()]},
                   "corrected by %s: %s" % (act["new"], act["reason"]))
            elif act["op"] == "contradiction":
                ent = records[act["lesson"]]
                related = [dict(r) for r in (ent.get("related") or []) if isinstance(r, dict)]
                if any(r.get("id") == act["other"] for r in related) or len(related) >= 12:
                    continue
                related.append({"id": act["other"], "kind": kind_of(records[act["other"]]),
                                "basis": "semantic", "adjudicated": True, "verdict": "contradiction",
                                "reason": act["reason"], "adjudicated_by": "consolidate",
                                "adjudicated_at": now_iso()})
                do(ent, {"related": related}, "contradiction with %s" % act["other"])
        for eid, (sha, body) in triggers.items():
            ent = records.get(eid)
            if not ent or kind_of(ent) != "lesson" or ent.get("status") != "standing":
                continue
            if (ent.get("applies_when") or {}).get("from_sha") == sha:
                continue
            do(ent, {"applies_when": dict(body, from_sha=sha, derived_by="consolidate",
                                          derived_at=now_iso())}, "applies_when")
    tally["log"] = str(log_path)
    return tally


def revert(log_path, *, get_entity, write, out=print) -> dict:
    """Restore every field a run changed to its value before the run's FIRST write of it. A
    field that did not exist before is written EMPTY (the note writer merges; it cannot unset),
    and `status` returns to what it was. A `superseded_by` pointer the run added stays behind
    as history: with `status` back to standing it retires nothing (only status and a live
    record's `supersedes` do)."""
    first = {}
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        slot = first.setdefault(row["id"], {})
        for k, v in (row.get("prior") or {}).items():
            slot.setdefault(k, v)
    empty = {"reinforced_by": [], "tags": [], "related": [], "applies_when": {},
             "status": "standing", "superseded_by": None}
    tally = {"restored": 0, "refused": 0, "gone": 0}
    for eid, prior in first.items():
        cur = get_entity(eid)
        if not cur:
            tally["gone"] += 1
            continue
        fields = {k: (v if v is not None else empty.get(k)) for k, v in prior.items()}
        fields = {k: v for k, v in fields.items() if v is not None}
        ok, detail = write(eid, fields, cur.get("version"))
        if ok:
            tally["restored"] += 1
        else:
            tally["refused"] += 1
            out("REVERT REFUSED %s: %s" % (eid, str(detail)[:300]))
    return tally


# ── the pass ─────────────────────────────────────────────────────────────────────────────────

def run(notes, *, lookup_related, write, cache: dict, run_id: str, log_path, apply_writes=False,
        judge_limit=MAX_JUDGE, trigger_limit=MAX_TRIGGERS, apply_limit=MAX_APPLY,
        lookup_limit=0, chat=None, out=print) -> dict:
    """One consolidation pass. ``cache`` is the caller's persistent verdict/trigger store (a
    dict it saves afterwards): ``{"verdicts": {key: judgement}, "triggers": {key: body}}``."""
    records = live(notes)
    cache.setdefault("verdicts", {})
    cache.setdefault("triggers", {})
    tally = {"run": run_id, "records": len(records), "mode": "apply" if apply_writes else "dry-run"}
    pairs, tally["lookups"] = candidates(records, lookup_related, limit=lookup_limit)
    tally["pairs"] = len(pairs)
    out("CANDIDATES %d pairs from %d records (%d looked up)" % (len(pairs), len(records), tally["lookups"]))
    model = {"ok": None, "detail": ""}

    def model_ok():
        if model["ok"] is None:
            if chat is not None:
                model["ok"], model["detail"] = True, "injected"
            else:
                model["ok"], model["detail"] = _adj.model_reachable()
            out("MODEL %s -- %s" % ("OK" if model["ok"] else "UNAVAILABLE", model["detail"]))
        return model["ok"]

    judged = []
    tally.update(judged_cached=0, judged_now=0, unparsed=0, needs_model=0)
    for a, b, sim, basis in pairs:
        ea, eb = records[a], records[b]
        if (when(ea) or "") > (when(eb) or ""):            # A is always the OLDER record
            a, b, ea, eb = b, a, eb, ea
        key = "%s|%s|%s|%s" % (a, _sha(_key_text(ea)), b, _sha(_key_text(eb)))
        hit = cache["verdicts"].get(key)
        if hit:
            judged.append((a, b, hit))
            tally["judged_cached"] += 1
            continue
        if tally["judged_now"] >= judge_limit:
            continue
        if not model_ok():
            tally["needs_model"] += 1
            continue
        try:
            j, reply = judge(ea, eb, chat=chat)
        except Exception as exc:                             # noqa: BLE001
            j, reply = None, "%s: %s" % (type(exc).__name__, exc)
        tally["judged_now"] += 1
        if j is None:
            tally["unparsed"] += 1
            out("UNPARSED %s ~ %s: %r" % (a, b, str(reply)[:160]))
            continue
        cache["verdicts"][key] = j
        judged.append((a, b, j))
        out("VERDICT %-13s keep=%-4s sim=%.3f (%s)\n   A %s [%s %s] %s\n   B %s [%s %s] %s\n   why: %s"
            % (j["verdict"], j["keep"], sim, basis, a, author(ea), when(ea)[:10], rule_of(ea)[:220],
               b, author(eb), when(eb)[:10], rule_of(eb)[:220], j["reason"]))
    tally["verdicts"] = {}
    for _a, _b, j in judged:
        tally["verdicts"][j["verdict"]] = tally["verdicts"].get(j["verdict"], 0) + 1
    actions = plan(records, judged)
    tally["actions"] = {}
    for act in actions:
        tally["actions"][act["op"]] = tally["actions"].get(act["op"], 0) + 1
        out("ACTION " + json.dumps(act))

    triggers = {}
    tally.update(triggers_now=0, triggers_cached=0, triggers_unparsed=0)
    for ent in sorted((e for e in records.values() if kind_of(e) == "lesson"),
                      key=lambda e: when(e) or "", reverse=True):
        sha = _sha(_key_text(ent))
        if (ent.get("applies_when") or {}).get("from_sha") == sha:
            continue
        key = "%s|%s" % (ent["id"], sha)
        if key in cache["triggers"]:
            triggers[ent["id"]] = (sha, cache["triggers"][key])
            tally["triggers_cached"] += 1
            continue
        if tally["triggers_now"] >= trigger_limit:
            continue
        if not model_ok():
            tally["needs_model"] += 1
            break
        try:
            body, reply = triggers_for(ent, chat=chat)
        except Exception as exc:                             # noqa: BLE001
            body, reply = None, "%s: %s" % (type(exc).__name__, exc)
        tally["triggers_now"] += 1
        if body is None:
            tally["triggers_unparsed"] += 1
            out("TRIGGER UNPARSED %s: %r" % (ent["id"], str(reply)[:160]))
            continue
        cache["triggers"][key] = body
        triggers[ent["id"]] = (sha, body)
        if tally["triggers_now"] <= 40:
            out("TRIGGER %s | %s\n   %s" % (ent["id"], rule_of(ent)[:160], json.dumps(body)))
    tally["triggers_empty"] = sum(1 for _s, body in triggers.values()
                                  if not any(body.get(k) for k in TRIGGER_KEYS))
    tally["relearn"] = relearn_stats(records, judged)
    out("RELEARN " + json.dumps(tally["relearn"]))
    tally["model"] = model["detail"] or "not needed"
    if apply_writes:
        tally["apply"] = apply(actions, records, triggers, run_id, write=write, log_path=log_path,
                               limit=apply_limit, out=out)
    return tally
