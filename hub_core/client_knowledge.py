"""Knowledge verbs for `python -m hub_core.client`: record what was learned, find it again,
settle overlaps, and deliver it before each prompt.

A LESSON is a rule earned from a mistake; a FINDING is a fact discovered about how a system
behaves; a METHOD is a procedure the team follows; a REVIEW is a question only a person may
answer; a GAP is an ownable deficiency with a severity. Filing everything as one kind is how a
board loses the difference, so each has its own verb::

    python -m hub_core.client share "Retry connection failures, never HTTP answers" \\
        --why "a retried 500 double-wrote an order" --tier foundational
    python -m hub_core.client finding "The import stalls when the queue exceeds 10k" --note "..."
    python -m hub_core.client method "Verify a fix at the deployed artifact" --category verification
    python -m hub_core.client review "May the export include personal data?" --note "..."
    python -m hub_core.client gap "Half the services report no errors" --severity P1 --note "..."
    python -m hub_core.client recall example:note:l-3f8a1c2b4d5e      # or a phrase
    python -m hub_core.client capabilities --q "retry"
    python -m hub_core.client prompt-context --hook < hook.json         # from a prompt hook
    python -m hub_core.client knowledge-sync --out ~/.hub-client/knowledge.json
    python -m hub_core.client adjudicate                                 # needs HUB_JUDGE_URL

Registered by `client._parser`; kept in its own module so the core client stays small.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

from . import client as _c


def _agent(arguments: argparse.Namespace) -> str:
    return _c._agent(arguments)


def _utf8_stdout() -> None:
    """Record text carries dashes, arrows and accented names; a console or hook pipe whose
    codec cannot encode one must not kill the verb halfway through its output."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ── writes ──

def _payload_share(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"agent": _agent(arguments), "rule": arguments.rule}
    for key in ("why", "tier", "verify", "verified_as_of", "supersedes"):
        if getattr(arguments, key, None):
            payload[key] = getattr(arguments, key)
    if arguments.tag:
        payload["tags"] = arguments.tag
    return "lesson", payload


def _record_payload(kind: str):
    def build(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {"agent": _agent(arguments), "title": arguments.title}
        for key in ("note", "evidence", "category", "verify", "verified_as_of"):
            if getattr(arguments, key, None):
                payload[key] = getattr(arguments, key)
        if arguments.tag:
            payload["tags"] = arguments.tag
        if arguments.relates_to:
            payload["relates_to"] = arguments.relates_to
        if arguments.expected_version is not None:
            payload["expected_version"] = arguments.expected_version
        return kind, payload
    return build


def _payload_gap(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"agent": _agent(arguments), "title": arguments.title,
                               "severity": arguments.severity, "status": "open"}
    text = gap_text(getattr(arguments, "note", None), getattr(arguments, "evidence", None))
    if text:
        payload["evidence"] = text
    if getattr(arguments, "source", None):
        payload["source"] = arguments.source
    return "gap", payload


def gap_text(note, evidence) -> str:
    """A gap keeps its text in `evidence` (the schema has no note field). `--note` is accepted so
    the gap verb reads like finding/method/review; with both, the note leads and the evidence
    follows under its own label, so neither is dropped."""
    note, evidence = str(note or "").strip(), str(evidence or "").strip()
    if note and evidence:
        return note + "\n\nEvidence: " + evidence
    return note or evidence


# ── recall ──

def looks_like_an_id(ref: str) -> bool:
    """An id has no whitespace and two or three colon-separated segments; a title has a space.
    That is the whole discriminator, and it fails toward the search — the branch that cannot
    build a bad URL (a title containing `Note:` must never be read as `<type>:<local>`)."""
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+){1,2}", ref or ""))


def print_overlap(ent: dict[str, Any]) -> None:
    """What the write-time tagger suspected this record duplicates, corrects or contradicts —
    printed where somebody asking about the rule is already looking. A suspicion nobody is shown
    is worse than the refusal it replaced, and an unadjudicated tag must never read as a verdict."""
    related = [h for h in (ent.get("related") or []) if isinstance(h, dict)]
    partial = ent.get("related_partial") or {}
    if partial.get("missing"):
        print("\n(overlap check incomplete: %s did not run — %s)"
              % (", ".join(partial["missing"]), partial.get("reason") or "no reason recorded"))
    if not related:
        return
    settled = [h for h in related if h.get("adjudicated")]
    if len(settled) == len(related):
        print("\noverlap (suspected at write time, then read in full and settled):")
    elif settled:
        print("\noverlap (%d of %d settled; the rest are only suspected — read both and judge):"
              % (len(settled), len(related)))
    else:
        print("\npossible overlap (suspected at write time; NOT adjudicated — read both and judge):")
    for h in related:
        bits = []
        if h.get("similarity") is not None:
            bits.append("similarity %s" % h["similarity"])
        if h.get("z") is not None:
            bits.append("z %s" % h["z"])
        if h.get("basis"):
            bits.append(str(h["basis"]))
        if h.get("shared"):
            bits.append("shared: " + ", ".join(str(t) for t in h["shared"][:5]))
        verdict = ("%s: " % str(h["verdict"]).upper()) if h.get("adjudicated") and h.get("verdict") else ""
        exact = "EXACT TEXT " if h.get("exact") else ""
        print("  - %s%s%s  (%s)" % (verdict, exact, h.get("id", "?"), " · ".join(bits) or "no detail"))
        if h.get("adjudicated") and h.get("reason"):
            print("      why: %s  [%s]" % (h["reason"], h.get("adjudicated_by") or "?"))
    if any(h.get("exact") for h in related):
        print("  ^ an EXACT-TEXT match is a duplicate rule: supersede one rather than leaving both live.")
    if any(h.get("verdict") == "contradiction" for h in settled):
        print("  ^ a CONTRADICTION means two live rules say opposite things in one situation; one is wrong.")
    if any(h.get("verdict") == "correction" for h in settled):
        print("  ^ a CORRECTION means one rule fixes the other; supersede the older one.")


def _run_recall(base: str, arguments: argparse.Namespace) -> None:
    """One record in full by id; a phrase (or an id the hub refuses) falls through to ranked
    search. EMPTY and BROKEN never read the same: a failed id lookup prints as a FAILURE on
    stdout, where a reader grepping this output is looking."""
    from urllib.parse import quote
    _utf8_stdout()
    ref = arguments.ref.strip()
    found: list[dict[str, Any]] = []
    id_failed = None
    if looks_like_an_id(ref):
        parts = ref.split(":")
        # `<project>:<type>:<local>` and a bare `<type>:<local>` reach the same route.
        type_, local = (parts[1], parts[2]) if len(parts) == 3 else (parts[0], parts[1])
        try:
            found = [_c._get(base, f"{quote(type_)}/{quote(local)}.json")["data"]]
        except RuntimeError as error:
            id_failed = str(error)[:300]
    hits: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    if not found:
        res = _c._get(base, f"search.json?q={quote(ref)}&limit={arguments.limit}")
        hits, meta = res.get("data") or [], res.get("metadata") or {}
    for ent in found:
        print("=" * 72)
        print(ent.get("id", "?"), "—", ent.get("title") or ent.get("name") or "")
        for key in ("status", "body_md", "acceptance", "evidence", "summary", "decision_md", "needs",
                    "verified_as_of", "verify", "supersedes", "superseded_by"):
            if ent.get(key):
                print(f"\n{key}: {ent[key]}")
        print_overlap(ent)
        prov = ent.get("provenance") or {}
        if prov.get("agent"):
            print(f"\n(from {prov['agent']}, {prov.get('updated_at', '')})")
    for h in hits:
        print("- [%s] %s  (%s, score %s)" % (h.get("id"), h.get("title"), h.get("kind"), h.get("score")))
        if h.get("excerpt"):
            print("    " + " ".join(str(h["excerpt"]).split())[:240])
    if meta.get("partial"):
        print(meta["partial"])
    if not found and not hits:
        if id_failed:
            print(f"!! recall FAILED: {ref!r} looked like an id and the hub refused it ({id_failed}); "
                  "the text search that followed matched nothing. This is NOT 'nothing matches'.")
        else:
            print("no match for %r — %s" % (ref, meta.get("hint") or "try different words"))
    elif id_failed:
        print(f"(note: {ref!r} did not resolve as an id — these are text matches)")
    return None


# ── reads ──

def _run_related(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import quote
    if arguments.text:
        return _c._get(base, f"related.json?text={quote(arguments.text)}")
    if not arguments.ref:
        raise ValueError("related needs an id or --text")
    return _c._get(base, f"related.json?id={quote(arguments.ref)}")


def _run_capabilities(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    from urllib.parse import urlencode
    query = {k: v for k, v in (("kind", arguments.kind), ("q", arguments.q)) if v}
    return _c._get(base, "capabilities.json" + (("?" + urlencode(query)) if query else ""))


def _run_prompt_context(base: str, arguments: argparse.Namespace) -> None:
    """Print the knowledge block for THIS prompt — the verb a prompt hook calls.

    With --hook, reads the harness's hook JSON from stdin (`session_id`, `hook_event_name`,
    `prompt`): a session-start event resets the per-session receipt, and the prompt's first 400
    characters are the focus when HUB_FOCUS/--focus is unset. An unreachable board prints one
    marked line and exits 0 — a knowledge hook must never block the prompt it decorates."""
    from urllib.parse import urlencode
    from . import prompt_context as pc
    _utf8_stdout()
    sid = arguments.session or os.environ.get("HUB_SESSION_ID", "")
    event, prompt = arguments.event or "", ""
    if arguments.hook:
        try:
            hook = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            hook = {}
        if isinstance(hook, dict):
            sid = sid or str(hook.get("session_id") or "")
            event = event or str(hook.get("hook_event_name") or "")
            prompt = str(hook.get("prompt") or "")
    if event.lower().replace("-", "") in ("sessionstart", "start"):
        pc.reset_delivered(sid)
    focus = (arguments.focus or os.environ.get("HUB_FOCUS") or " ".join(prompt.split())[:400]).strip()
    # THE HAND-OFF: when this machine's own memory engine serves the board's knowledge (switch
    # on, mirror fresh, local recall healthy -- knowledge_mirror.local_owner), printing the
    # hub-ranked block too would deliver every record twice. Ask for a one-row index (the live
    # block still rides) and print one line that says which side is serving. Any doubt keeps
    # the hub block: the decision fails closed.
    from . import knowledge_mirror
    local = knowledge_mirror.local_owner()
    query: dict[str, Any] = {"agent": _agent(arguments),
                             "memory_cap": 1 if local else arguments.memory_cap,
                             "memory_full": 0 if local else arguments.memory_full}
    if focus:
        query["focus"] = focus
    try:
        payload = _c._get(base, "guidance.json?" + urlencode(query), timeout=15)
    except RuntimeError as error:
        print("<hub-knowledge>(the board could not be reached for this prompt: %s)</hub-knowledge>"
              % str(error)[:200])
        return None
    delivered = pc.load_delivered(sid) if sid else []
    out = pc.render(payload, delivered=delivered or None,
                    budget=min(pc.MEMORY_BUDGET, pc.OUTPUT_MAX - 600))
    if out["live"] and not pc.live_due(sid, out["live"]):
        out["live"] = ""               # unchanged but for its ages: said recently enough
    if local:
        print(pc.fit_output([out["live"], "<hub-knowledge>knowledge for this prompt is served by "
                             "this machine's local memory (mirror fresh, recall healthy); the "
                             "board's own search is `python -m hub_core.client search`</hub-knowledge>"]))
        return None
    parts = [out["live"], out["memory"]]
    if out["spill"]:
        path = pc.write_spill(sid, out["spill"])
        parts.append("(%d more ranked records are in %s)" % (len(out["spill"]), path or
                     "no file — the overflow could not be written; use `search`"))
    text = pc.fit_output(parts)
    if text:
        print(text)
    if sid and out["keys"]:
        pc.save_delivered(sid, delivered + out["keys"])
    return None


def _run_knowledge_feed(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """--feed: the append-only operation log a local memory engine indexes, plus its sidecar
    (hub_core.knowledge_mirror). Rate-limited by --min-interval, so a daemon can call this on
    every heartbeat and a person can call it by hand (--min-interval 0, the default)."""
    from pathlib import Path
    from . import knowledge_mirror as km

    def get(route, headers):
        try:
            return _c._request(_c._bases_of(base), "GET", route, data=None,
                               headers={**_c._common_headers(), **_c._optional_auth_headers(),
                                        **_c._telemetry_headers(), **headers})
        except _c.HubRefused as refusal:
            if refusal.status == 304:
                raise km.NotModified() from None
            raise

    feed = (Path(os.path.expanduser(arguments.feed)) if arguments.feed not in (None, "", "-")
            else km.default_feed())
    result = km.tick(get, feed, min_interval=arguments.min_interval,
                     max_pages=arguments.max_pages)
    result["local_owner"] = km.local_owner(feed)
    result["health"] = km.health_line(feed)
    if result.get("status") == "error":
        result["_exit"] = 1
    return result


def _run_knowledge_sync(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Mirror the board's knowledge into one local JSON file from /hub/knowledge/since.

    The first run bootstraps (pages until `more` is false); later runs ask only for what changed
    after the stored cursor and apply put / revoke / reset. A caught-up run costs one request
    and changes nothing. The file is rewritten atomically."""
    if getattr(arguments, "feed", None) is not None:
        return _run_knowledge_feed(base, arguments)
    if not arguments.out:
        raise ValueError("knowledge-sync needs --out <file.json> (a snapshot) or --feed [<file.jsonl>] "
                         "(the operation log a local memory engine indexes)")
    from pathlib import Path
    from urllib.parse import quote
    path = Path(os.path.expanduser(arguments.out))
    try:
        mirror = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        mirror = {}
    records = mirror.get("records") if isinstance(mirror.get("records"), dict) else {}
    cursor = str(mirror.get("cursor") or "")
    counts = {"put": 0, "revoke": 0, "reset": 0, "pages": 0}
    while True:
        page = _c._get(base, f"knowledge/since?cursor={quote(cursor)}&limit={arguments.limit}")
        counts["pages"] += 1
        for item in page.get("items") or []:
            op = item.get("op")
            if op == "reset":
                source = item.get("source")
                records = {k: v for k, v in records.items() if v.get("type") != source}
                counts["reset"] += 1
            elif op == "put" and item.get("id"):
                records[item["id"]] = {k: v for k, v in item.items() if k != "op"}
                counts["put"] += 1
            elif op == "revoke" and item.get("id"):
                records.pop(item["id"], None)
                counts["revoke"] += 1
        cursor = page.get("cursor") or cursor
        if not page.get("more"):
            break
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"cursor": cursor, "records": records}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return {"cursor": cursor, "records": len(records), "applied": counts, "file": str(path)}


def _run_adjudicate(base: str, arguments: argparse.Namespace) -> dict[str, Any]:
    """Settle the overlap suspicions lesson writes recorded — rules first, a model when reading
    helps (HUB_JUDGE_URL / HUB_JUDGE_MODEL on THIS machine) — writing back through the served
    API. Exits 2 when there is judging to do and no model: a pass that settles nothing is loud."""
    from urllib.parse import quote
    from . import adjudicate
    notes = [n for n in (_c._get(base, "note.json").get("data") or []) if isinstance(n, dict)]
    by_id = {n.get("id"): n for n in notes}

    def get_entity(eid):
        if eid in by_id:
            return by_id[eid]
        parts = str(eid or "").split(":")
        if len(parts) != 3:
            return None
        try:
            return _c._get(base, f"{quote(parts[1])}/{quote(parts[2])}.json").get("data")
        except RuntimeError:
            return None

    def lookup_related(eid):
        body = _c._get(base, f"related.json?id={quote(eid)}")
        return body.get("data") or {}, body.get("metadata") or {}

    def write(eid, fields, version):
        try:
            _c._post(base, "note", {"id": eid, "agent": _agent(arguments), "expected_version": version,
                                    **fields}, extra_headers=_c._presence_headers(arguments))
            return True, "written"
        except (RuntimeError, ValueError) as error:
            return False, str(error)[:300]

    result = adjudicate.run_pass(notes, get_entity=get_entity, lookup_related=lookup_related,
                                 write=write, limit=arguments.limit, dry_run=arguments.dry_run)
    if result.get("needs_model"):
        result["_exit"] = 2
        result["msg"] = ("%d overlap(s) need a reader and no judge model is configured here "
                         "(set HUB_JUDGE_URL and HUB_JUDGE_MODEL)" % result["needs_model"])
    return result


# ── registration ──

def register(commands) -> None:
    share = commands.add_parser("share", help="record a LESSON (a rule earned from a mistake); admitted "
                                "and tagged with what it may overlap, never refused for resemblance")
    share.add_argument("rule")
    share.add_argument("--why", help="what happened — the story that earned the rule")
    share.add_argument("--tier", choices=("normal", "foundational"))
    share.add_argument("--verify", help="for a STATE claim: the command or URL that answers it NOW")
    share.add_argument("--verified-as-of", dest="verified_as_of", help="YYYY-MM-DD it was last checked")
    share.add_argument("--supersedes", help="the record this corrects (retired while this one is live)")
    share.add_argument("--tag", action="append", default=[])
    share.add_argument("--agent")
    share.set_defaults(payload=_payload_share)

    for kind, help_text in (("finding", "record a FINDING — a fact discovered about how a system behaves"),
                            ("method", "record a METHOD — a procedure the team follows and exhibits"),
                            ("review", "record a REVIEW — a question only a person may answer")):
        rec = commands.add_parser(kind, help=help_text)
        rec.add_argument("title")
        rec.add_argument("--note", help="the evidence / how it is done / the question in full")
        rec.add_argument("--evidence", help="a sha, URL or path that dereferences")
        rec.add_argument("--category", help=("extraction|analysis|transformation|verification|governance|"
                                             "presentation" if kind == "method" else "a free sub-kind tag"))
        rec.add_argument("--verify")
        rec.add_argument("--verified-as-of", dest="verified_as_of")
        rec.add_argument("--tag", action="append", default=[])
        rec.add_argument("--relates-to", action="append", default=[], dest="relates_to")
        rec.add_argument("--expected-version", type=int, dest="expected_version",
                         help="update the existing record of this title")
        rec.add_argument("--agent")
        rec.set_defaults(payload=_record_payload(kind))

    gap = commands.add_parser("gap", help="record a GAP — an ownable deficiency with a severity")
    gap.add_argument("title")
    gap.add_argument("--severity", required=True, choices=("P0", "P1", "P2", "P3"))
    gap.add_argument("--note", help="what is missing and how it shows (stored as the gap's evidence text)")
    gap.add_argument("--evidence", help="where it was observed (file:line, a URL, a command's output)")
    gap.add_argument("--source")
    gap.add_argument("--agent")
    gap.set_defaults(payload=_payload_gap)

    if "recall" in commands.choices:
        # ONE recall verb: a TASK id answers with the task's joined row (state, holder,
        # commits, checkpoints in order); any other id or a phrase answers from knowledge.
        recall = commands.choices["recall"]
        recall.add_argument("--limit", type=int, default=8)
        core_recall = recall.get_default("runner")

        def _recall_dispatch(base, arguments):
            ref = str(getattr(arguments, "task_id", "") or "")
            if ":task:" in ref or ref.startswith("task:"):
                return core_recall(base, arguments)
            arguments.ref = ref
            return _run_recall(base, arguments)
        recall.set_defaults(runner=_recall_dispatch)
    else:
        recall = commands.add_parser("recall", help="one record in full by id (overlaps and verdicts "
                                     "included), or a ranked search for a phrase")
        recall.add_argument("ref")
        recall.add_argument("--limit", type=int, default=8)
        recall.set_defaults(runner=_run_recall)

    related = commands.add_parser("related", help="records close to an id (or --text), by vocabulary and meaning")
    related.add_argument("ref", nargs="?")
    related.add_argument("--text")
    related.set_defaults(runner=_run_related)

    caps = commands.add_parser("capabilities", help="what an agent can already do here (ledger + published catalog)")
    caps.add_argument("--kind")
    caps.add_argument("--q")
    caps.set_defaults(runner=_run_capabilities)

    if "prompt-context" in commands.choices:
        # ONE prompt-context verb: the three-channel payload (doctrine / live / nudge) by
        # default; --hook (or --knowledge) prints the knowledge block a prompt hook injects.
        pctx = commands.choices["prompt-context"]
        pctx.add_argument("--hook", action="store_true",
                          help="knowledge block: read the hook JSON (session, event, prompt) on stdin")
        pctx.add_argument("--knowledge", action="store_true",
                          help="print the knowledge block for this prompt instead of the payload")
        pctx.add_argument("--focus")
        pctx.add_argument("--memory-cap", type=int, default=150, dest="memory_cap")
        pctx.add_argument("--memory-full", type=int, default=25, dest="memory_full")
        core_pctx = pctx.get_default("runner")

        def _pctx_dispatch(base, arguments):
            if getattr(arguments, "hook", False) or getattr(arguments, "knowledge", False):
                if base is None:
                    print("<knowledge unavailable: no hub configured (HUB_API_BASE)>")
                    return None
                if arguments.event == "UserPromptSubmit" and getattr(arguments, "hook", False):
                    arguments.event = ""          # let the hook JSON name its own event
                return _run_prompt_context(base, arguments)
            return core_pctx(base, arguments)
        pctx.set_defaults(runner=_pctx_dispatch)
    else:
        pctx = commands.add_parser("prompt-context", help="print the knowledge block for this prompt "
                                   "(from a prompt hook: --hook reads the hook JSON on stdin)")
        pctx.add_argument("--hook", action="store_true")
        pctx.add_argument("--event", help="SessionStart resets what this session is known to hold")
        pctx.add_argument("--session")
        pctx.add_argument("--focus")
        pctx.add_argument("--agent")
        pctx.add_argument("--memory-cap", type=int, default=150, dest="memory_cap")
        pctx.add_argument("--memory-full", type=int, default=25, dest="memory_full")
        pctx.set_defaults(runner=_run_prompt_context)

    ksync = commands.add_parser("knowledge-sync", help="mirror the board's knowledge locally: a JSON "
                                "snapshot (--out) or an append-only op log a local memory engine indexes (--feed)")
    ksync.add_argument("--out")
    ksync.add_argument("--feed", nargs="?", const="-",
                       help="append put/revoke/reset ops to this JSONL file (default HUB_KNOWLEDGE_FEED "
                            "or <state dir>/feeds/knowledge.jsonl) and keep its <stem>.state.json sidecar")
    ksync.add_argument("--min-interval", type=float, default=0, dest="min_interval",
                       help="--feed: skip the poll when the last one was this recent (a daemon passes 300)")
    ksync.add_argument("--max-pages", type=int, default=12, dest="max_pages")
    ksync.add_argument("--limit", type=int, default=500)
    ksync.set_defaults(runner=_run_knowledge_sync)

    adj = commands.add_parser("adjudicate", help="settle lesson overlap suspicions (rules, then a judge model)")
    adj.add_argument("--limit", type=int, default=0, help="max model judgements this run")
    adj.add_argument("--dry-run", action="store_true", dest="dry_run")
    adj.add_argument("--agent")
    adj.set_defaults(runner=_run_adjudicate)
