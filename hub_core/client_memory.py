"""Workstation verbs for `python -m hub_core.client` that connect the board's knowledge to a
machine's own memory and to the moment a rule applies::

    python -m hub_core.client lesson-trigger --hook          # PreToolUse / PostToolUseFailure hook
    python -m hub_core.client triggers                       # what the local index would fire on
    python -m hub_core.client triggers --refresh             # rate triggers against this machine's calls
    python -m hub_core.client triggers --stats               # fired / cited / precision
    python -m hub_core.client triggers --replay              # every trigger over the real call sample
    python -m hub_core.client trigger-match --error "..."    # ask the BOARD which lesson applies
    python -m hub_core.client memory-feed status             # the hand-off, as a consumer reads it
    python -m hub_core.client memory-feed records --render rule
    python -m hub_core.client install-hooks [--uninstall] [--dry-run]

Everything except ``trigger-match`` is local: it reads files on this machine and needs no hub.
``patterns/agent-memory.md`` is the write-up. Registered by `client._parser`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ── lesson triggers ──

def _run_lesson_trigger(_bases, arguments) -> None:
    """The hook. Reads the harness event JSON on stdin, prints the hook JSON (or nothing), and
    exits 0 on ANY fault: a per-tool-call hook must never block the tool it decorates."""
    from . import lesson_triggers as lt
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig") if arguments.hook else "{}"
        event = json.loads(raw or "{}")
        if not isinstance(event, dict):
            return None
        out = lt.run_hook(event, Path(os.path.expanduser(arguments.mirror)) if arguments.mirror else None)
        if out:
            _utf8_stdout()
            sys.stdout.write(out)
            sys.stdout.flush()
    except Exception:                                   # noqa: BLE001 - never block a tool call
        pass
    return None


def _run_triggers(_bases, arguments):
    from . import lesson_triggers as lt
    _utf8_stdout()
    mirror = Path(os.path.expanduser(arguments.mirror)) if arguments.mirror else None
    index = lt.load_index(mirror)
    if arguments.refresh:
        return lt.refresh(force_sample=arguments.resample, index=index)
    if arguments.stats:
        a = lt.aggregate()
        prec = (lambda f, c: ("%.0f%%" % (100.0 * c / f)) if f else "-")
        print("LEDGER %s: %d fired, %d cited, precision %s"
              % (lt.ledger_path(), a["fired"], a["cited"], prec(a["fired"], a["cited"])))
        print("\nper trigger (fired / cited / precision):")
        for t, (f, c) in sorted(a["by_trigger"].items(), key=lambda kv: -kv[1][0]):
            print("  %5d %5d %5s  %r" % (f, c, prec(f, c), t))
        print("\nper record:")
        for rid, (f, c) in sorted(a["by_record"].items(), key=lambda kv: -kv[1][0]):
            print("  %5d %5d %5s  %s" % (f, c, prec(f, c), rid))
        _index, report = lt.guarded(index, lt._read_json(lt.guard_path()))
        print("\nGUARD (%s): a trigger matching more than %s of the last %s calls of its kind is "
              "ignored %s" % (report["state"], report.get("rate"), report.get("m"), report.get("counts") or ""))
        for t, r in sorted(report["dropped"].items(), key=lambda kv: -kv[1]["rate"]):
            print("  DROPPED %s %r  %d/%d = %.3f%%" % (r["kind"], t, r["hits"], r["calls"], 100 * r["rate"]))
        if report["held"]:
            print("  held until rated: %d trigger(s) — run `triggers --refresh`" % len(report["held"]))
        return None
    if arguments.replay:
        # Every trigger over this machine's REAL recent calls, so a candidate trigger is vetted
        # against what agents actually run before anyone relies on it.
        pairs = lt.index_pairs(index)
        if arguments.seed:
            seed = json.loads(Path(arguments.seed).read_text(encoding="utf-8-sig"))
            extra = trigger_seed(seed)
            index = lt.build_index({**{r["id"]: r for r in index.get("records") or []}, **extra})
            pairs = lt.index_pairs(index)
        sample = lt._read_json(lt.sample_path())
        if not sample or arguments.resample or lt.sample_stale():
            sample = lt.build_sample()
            lt._atomic_json(lt.sample_path(), sample)
        rated = lt.rate_triggers(sorted(pairs), sample, examples=2)
        print("REPLAY over %d real tool calls %s (%d transcripts), guard line %.1f%%"
              % (len(sample.get("calls") or []), sample.get("counts"), sample.get("files") or 0,
                 100 * lt.GUARD_RATE))
        for key, row in sorted(rated.items(), key=lambda kv: -kv[1][0]):
            kind, trig = key.split("\x1f", 1)
            hits, n, seen = row[0], row[1], row[2]
            flag = "DROP" if lt.verdict(hits, n) else ("unrated" if n < lt.MIN_KIND_CALLS else "keep")
            print("  %-7s %-8s %5d/%-6d %6.3f%%  %r" % (flag, kind, hits, n, (100.0 * hits / n) if n else 0, trig))
            for s in seen:
                print("            e.g. %s" % s)
        if index.get("rejected"):
            print("\nrejected at index build (never matched):")
            for t, why in sorted(index["rejected"].items()):
                print("  %r — %s" % (t, why))
        return None
    guarded_index, report = lt.guarded(index, lt._read_json(lt.guard_path()))
    return {"mirror": str(mirror or lt.mirror_path()).replace("\\", "/"),
            "records_with_triggers": len(index.get("records") or []),
            "records_live_after_guard": len(guarded_index.get("records") or []),
            "rejected_at_build": index.get("rejected") or {},
            "guard": {k: report.get(k) for k in ("state", "rate", "m", "counts", "span")},
            "guard_dropped": report.get("dropped"), "held_until_rated": report.get("held"),
            "ledger": lt.aggregate().get("fired"),
            "hint": ("no record in the mirror carries applies_when — refresh the mirror "
                     "(client knowledge-sync) after triggers are derived on the board"
                     if not index.get("records") else "")}


def trigger_seed(seed) -> dict:
    """``--seed``: candidate triggers to vet before they are published — a list of
    ``{"id", "title"?, "applies_when": {...}}``."""
    from . import lesson_triggers as lt
    rows = seed if isinstance(seed, list) else (seed.get("records") if isinstance(seed, dict) else [])
    live = {str(r.get("id")): r for r in rows or [] if isinstance(r, dict) and r.get("id")}
    return lt.trigger_records(live)


def _run_trigger_match(bases, arguments):
    from urllib.parse import urlencode
    from . import client as _c
    query = {k: v for k, v in (("error", arguments.error), ("command", arguments.match_command),
                                 ("path", arguments.path), ("context", arguments.context)) if v}
    if not query.get("error") and not query.get("command") and not query.get("path"):
        raise ValueError("trigger-match needs --error, --command or --path")
    return _c._get(bases, "triggers.json?" + urlencode(query))


# ── the memory hand-off ──

def _run_memory_feed(_bases, arguments):
    from . import memory_feed as mf
    _utf8_stdout()
    directory = Path(os.path.expanduser(arguments.dir)) if arguments.dir else None
    mirror = Path(os.path.expanduser(arguments.mirror)) if arguments.mirror else None
    if arguments.action == "status":
        return mf.status(directory, mirror)
    # records: what a memory engine would index and render, redacted
    if mirror is None:
        d = directory or mf.feeds_dir()
        name = arguments.feed or "fleet"
        mirror = d / (name + ".jsonl")
        if not mirror.exists():
            from . import lesson_triggers as lt
            mirror = lt.mirror_path()
    live = mf.live_records(mirror)
    words = [w.lower() for w in (arguments.q or "").split() if w]
    shown = 0
    print("# %d live records in %s (rendered %s, redacted)" % (len(live), str(mirror).replace("\\", "/"),
                                                                   arguments.render))
    for item in reversed(list(live.values())):
        text = mf.index_text(item).lower()
        if words and not all(w in text for w in words):
            continue
        print(mf.rule_form(item, arguments.chars) if arguments.render == "rule" else mf.full_form(item))
        shown += 1
        if shown >= arguments.limit:
            break
    if not shown:
        print("(no record matched)" if live else "(the mirror is empty or absent — run `client knowledge-sync`)")
    return None


# ── hook installation ──

MARKERS = ("prompt-context --hook", "lesson-trigger --hook")
TOOL_MATCHER = "Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit"


def _is_ours(entry: dict) -> bool:
    for h in entry.get("hooks") or []:
        cmd = str((h or {}).get("command") or "") if isinstance(h, dict) else ""
        if "hub_core" in cmd and any(m in cmd for m in MARKERS):
            return True
    return False


def hook_command(verb: str, python: str, root: str) -> str:
    """A command that finds this checkout's hub_core without depending on the hook's cwd."""
    root = root.replace("\\", "/")
    return ('"%s" -c "import sys; sys.path.insert(0, r\'%s\'); from hub_core.client import main; '
            'sys.exit(main())" %s' % (python.replace("\\", "/"), root, verb))


def trigger_hook_command(python: str, root: str) -> str:
    """The per-tool-call hook: loads hub_core.lesson_triggers under a bare package stub, so the
    package's __init__ (the schema validator, ~0.4 s measured) is never imported on a tool call.
    The trailing marker is how install-hooks recognises its own entry."""
    root = root.replace("\\", "/")
    return ('"%s" -c "import sys, types; p = types.ModuleType(\'hub_core\'); '
            'p.__path__ = [r\'%s/hub_core\']; sys.modules[\'hub_core\'] = p; '
            'from hub_core.lesson_triggers import hook_main; sys.exit(hook_main())" '
            'lesson-trigger --hook' % (python.replace("\\", "/"), root))


def desired_hooks(python: str, root: str) -> dict:
    ctx = {"type": "command", "command": hook_command("prompt-context --hook", python, root), "timeout": 20}
    trig = {"type": "command", "command": trigger_hook_command(python, root), "timeout": 5}
    return {"SessionStart": [{"hooks": [ctx]}],
            "UserPromptSubmit": [{"hooks": [dict(ctx)]}],
            "PreToolUse": [{"matcher": TOOL_MATCHER, "hooks": [trig]}],
            "PostToolUseFailure": [{"hooks": [dict(trig)]}]}


def _run_install_hooks(_bases, arguments):
    from . import settings_io
    settings = Path(os.path.expanduser(arguments.settings or os.path.join("~", ".claude", "settings.json")))
    python = arguments.python or sys.executable
    root = str(Path(__file__).resolve().parent.parent)
    try:
        before = settings_io.load(settings)
    except settings_io.SettingsRefused as refusal:
        return {"_exit": 3, "refused": str(refusal)}
    data = json.loads(json.dumps(before))
    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        return {"_exit": 3, "refused": "REFUSING to write %s: its `hooks` is %s, not an object."
                % (settings, type(hooks).__name__)}
    removed = 0
    for event in list(hooks):
        entries = hooks[event] if isinstance(hooks[event], list) else []
        keep = [e for e in entries if not (isinstance(e, dict) and _is_ours(e))]
        removed += len(entries) - len(keep)
        if keep:
            hooks[event] = keep
        else:
            hooks.pop(event, None)
    added = 0
    if not arguments.uninstall:
        for event, entries in desired_hooks(python, root).items():
            hooks.setdefault(event, []).extend(entries)
            added += len(entries)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    summary = {"settings": str(settings).replace("\\", "/"), "removed_ours": removed, "added": added,
               "foreign_hook_entries_kept": len(settings_io.foreign_hooks(before, _is_ours)),
               "events": sorted(desired_hooks(python, root)) if not arguments.uninstall else []}
    if arguments.dry_run:
        summary["dry_run"] = True
        summary["would_write"] = data.get("hooks") or {}
        return summary
    if data == before:
        summary["unchanged"] = True
        return summary
    try:
        summary["backup"] = settings_io.save(settings, data, before=before, is_ours=_is_ours)
    except settings_io.SettingsRefused as refusal:
        return {"_exit": 3, "refused": str(refusal)}
    return summary


# ── registration ──

def register(commands) -> None:
    lt = commands.add_parser("lesson-trigger", help="tool-time hook: surface the lesson a command, path "
                             "or error names (reads the harness event on stdin)")
    lt.add_argument("--hook", action="store_true", help="read the PreToolUse/PostToolUseFailure JSON on stdin")
    lt.add_argument("--mirror", help="the knowledge mirror (default HUB_KNOWLEDGE_MIRROR or "
                    "~/.hub-client/knowledge.json); a JSONL feed works too")
    lt.set_defaults(runner=_run_lesson_trigger, local=True)

    tr = commands.add_parser("triggers", help="the local trigger index: what it would fire on, the "
                             "corpus guard, and fired/cited precision")
    tr.add_argument("--mirror")
    tr.add_argument("--refresh", action="store_true", help="rate triggers against this machine's tool calls")
    tr.add_argument("--resample", action="store_true", help="rebuild the tool-call sample first")
    tr.add_argument("--stats", action="store_true", help="fired / cited / precision per trigger and record")
    tr.add_argument("--replay", action="store_true", help="every trigger over the real call sample, with examples")
    tr.add_argument("--seed", help="with --replay: a JSON list of candidate {id, applies_when} to vet")
    tr.set_defaults(runner=_run_triggers, local=True)

    tm = commands.add_parser("trigger-match", help="ask the board which live lesson an error, a command or "
                             "a path names (the same matcher the hook uses, over the board's records)")
    tm.add_argument("--error")
    tm.add_argument("--command", dest="match_command")
    tm.add_argument("--path")
    tm.add_argument("--context", help="extra text the `systems` tie-break may look at")
    tm.set_defaults(runner=_run_trigger_match)

    mf = commands.add_parser("memory-feed", help="the knowledge hand-off to this machine's memory "
                             "engine, as a consumer reads it")
    mf.add_argument("action", choices=("status", "records"))
    mf.add_argument("--dir", help="the feeds directory (default HUB_MEMORY_FEEDS_DIR or ~/.agent-memory/feeds)")
    mf.add_argument("--feed", help="records: which feed in --dir (default fleet)")
    mf.add_argument("--mirror", help="a knowledge-sync JSON mirror or a JSONL feed file")
    mf.add_argument("--render", choices=("rule", "full"), default="rule")
    mf.add_argument("--chars", type=int, default=300)
    mf.add_argument("--limit", type=int, default=20)
    mf.add_argument("--q", help="records: only those containing every word")
    mf.set_defaults(runner=_run_memory_feed, local=True)

    ih = commands.add_parser("install-hooks", help="wire prompt-context and lesson-trigger into a harness "
                             "settings file without touching anyone else's hooks")
    ih.add_argument("--settings", help="default ~/.claude/settings.json")
    ih.add_argument("--python", help="the interpreter the hooks run (default this one)")
    ih.add_argument("--uninstall", action="store_true", help="remove only this client's hooks")
    ih.add_argument("--dry-run", action="store_true", dest="dry_run")
    ih.set_defaults(runner=_run_install_hooks, local=True)
