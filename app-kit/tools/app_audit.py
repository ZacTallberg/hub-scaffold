#!/usr/bin/env python3
"""app_audit.py -- measure an app against THE BAR (app-kit/BAR.md), not against a bug list.

    python app-kit/tools/app_audit.py <app-dir> [-v] [--json] [--min 90]
    python app-kit/tools/app_audit.py --rules <app-dir> [<app-dir> ...]   # audit the BAR itself
    python app-kit/tools/app_audit.py --count                             # what the bar IS today

A defect auditor asks "did this app repeat a known mistake?" and can pass an app that is correct
and lifeless. This asks the other question: is this the finished version of the app, or merely
a working one? Each rule is an affirmative property -- a heading that states a finding, a
denominator beside every count, a reviewed apply, a poll that stops when nobody is looking, an
empty state that names the next action -- and each names the kit or pattern that closes it.

HOW TO READ ITS OUTPUT
* Quote the numbers it prints TODAY. The bar's size (dimensions, rules, MUST rules) is derived
  from the rule table below and printed by ``--count``; a document that types the number goes
  stale the first time a rule is added.
* A rule can return "does not apply" (an app with no assistant is not failing the assistant
  rules). A classifier that cannot tell is a LOUD failure, never a quiet exemption, because a
  wrong "not applicable" is invisible in the score: the app just gets a smaller denominator.
* A run that measured NOTHING (no files read, or no rule applied) exits 2. 0/0 is not a pass.
* Every verdict names the tree it read and whether that checkout is behind its upstream; a
  verdict from a stale tree reads exactly like a defect.

It reads source text. It is a map of where to look, not proof the app works: the proof is the
real operation (docs/TESTING.md). Exit: 0 at or above --min with no MUST failing; 1 below the
bar or a MUST failed; 2 bad usage or nothing measured. Stdlib only; never imports the app.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gitstate import stale_note  # noqa: E402

KIT_ROOT = Path(__file__).resolve().parent.parent
DECLARATION = ".appkit.json"
SKIP = {".git", "__pycache__", "node_modules", "migrations", ".venv", "venv", "staticfiles",
        "dist", "vendor"}
#: Vendored kit code that is not the app's own. It stays in ``py`` (a rule may read the
#: substrate legitimately) and is kept OUT of ``app_py``: a classifier that read the assistant
#: kit's own docstrings as the app's tool registry would hold an app with no assistant to three
#: assistant MUST rules it structurally cannot clear.
SUBSTRATE = {"assistant"}
SUFFIXES = (".py", ".html", ".js", ".css", ".md", ".ps1", ".sh", ".yml", ".yaml", ".json")
#: The .json files a rule reads. Every other .json (lockfiles, fixtures, data) is skipped: the
#: suffix is admitted only so the deploy manifest is not invisible to the deploy rule.
MANIFESTS = {"services.json"}


def _declared_dirs(root: Path, key: str):
    """[(note, dir|None)] from <app>/.appkit.json under ``key`` ("renders_from" or "kits_from").

    An app may declare where the rest of its interface lives (a shared frontend package, the
    kits it runs from). A declaration is honoured only when it resolves to a real directory
    inside the repo (the app's parent), and every outcome is returned as a note so the report
    prints it: a declaration that quietly did nothing is a silent pass.
    """
    path = root / DECLARATION
    if not path.is_file():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return [(f"{DECLARATION} unreadable ({str(exc)[:80]}) -- nothing declared was scored", None)]
    raw = doc.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        return [(f"{DECLARATION}: {key} must be a list of paths -- ignored", None)]
    repo, out = root.resolve().parent, []
    for entry in raw[:16]:
        target = (root / entry).resolve()
        if not target.is_relative_to(repo):
            out.append((f"{DECLARATION}: {entry} is outside the repo -- refused", None))
        elif not target.is_dir():
            out.append((f"{DECLARATION}: {entry} does not exist -- refused, so the app is "
                        "scored on what it actually has", None))
        else:
            out.append((f"{DECLARATION}: reading {entry} as part of this app", target))
    return out


@dataclass
class Corpus:
    root: Path
    py: str = ""
    app_py: str = ""
    html: str = ""
    ui_html: str = ""
    js: str = ""
    css: str = ""
    ops: str = ""
    files: dict = field(default_factory=dict)
    declared: list = field(default_factory=list)

    @staticmethod
    def _gitignored(root: Path, paths) -> set:
        """What git itself says is ignored -- a generated report is noise the repo already
        declared. On any failure nothing extra is skipped."""
        try:
            rel = [str(p.relative_to(root)).replace("\\", "/") for p in paths]
            if not rel:
                return set()
            r = subprocess.run(["git", "-C", str(root), "check-ignore", "--stdin"],
                               input="\n".join(rel), capture_output=True, text=True, timeout=30)
            return {line.strip().replace("\\", "/") for line in r.stdout.splitlines() if line.strip()}
        except (OSError, subprocess.SubprocessError):
            return set()

    def _add(self, rel: str, path: Path, text: str) -> None:
        if path.suffix == ".json":
            if path.name in MANIFESTS:
                self.files[rel] = text
                self.ops += "\n" + text
            return
        self.files[rel] = text
        suffix = path.suffix
        if suffix == ".py" and not path.name.startswith("test"):
            self.py += "\n" + text
            if not SUBSTRATE & set(path.parts):
                self.app_py += "\n" + text
        elif suffix == ".html":
            self.html += "\n" + text
            if "/templates/" in "/" + rel or "/static/" in "/" + rel:
                self.ui_html += "\n" + text
        elif suffix == ".js":
            self.js += "\n" + text
        elif suffix == ".css":
            self.css += "\n" + text
        elif suffix in (".ps1", ".sh", ".yml", ".yaml"):
            self.ops += "\n" + text

    @classmethod
    def load(cls, root: Path) -> "Corpus":
        c = cls(root=root)
        candidates = [p for p in root.rglob("*") if p.is_file() and p.suffix in SUFFIXES
                      and not SKIP & set(p.relative_to(root).parts)]
        ignored = cls._gitignored(root, candidates)
        for p in candidates:
            rel = str(p.relative_to(root)).replace("\\", "/")
            if rel in ignored:
                continue
            try:
                c._add(rel, p, p.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
        for key in ("renders_from", "kits_from"):
            for note, decl in _declared_dirs(root, key):
                c.declared.append(note)
                if decl is None:
                    continue
                for q in sorted(decl.rglob("*")):
                    if not q.is_file() or q.suffix not in SUFFIXES or SKIP & set(q.parts):
                        continue
                    try:
                        text = q.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    c._add("../" + str(q.relative_to(root.resolve().parent)).replace("\\", "/"),
                           q, text)
        return c

    def _body(self, where: str) -> str:
        # ui_html falls back to html, so a scoped rule can never silently measure NOTHING.
        return {"py": self.py, "app_py": self.app_py or self.py, "html": self.html,
                "ui_html": self.ui_html or self.html, "js": self.js, "css": self.css,
                "ops": self.ops, "all": self.py + self.html + self.js + self.css}[where]

    def has(self, *patterns: str, where: str = "all") -> bool:
        # Keep a pattern to ONE concept: with re.S, `a.*b` asks "do both words appear anywhere
        # in the whole app" and answers yes for almost everything.
        body = self._body(where)
        return any(re.search(p, body, re.I | re.S) for p in patterns)

    def count(self, pattern: str, where: str = "all") -> int:
        # No re.M: `^` anchors to the start of the corpus. Pass (?m) in the pattern if needed.
        return len(re.findall(pattern, self._body(where), re.I))


@dataclass
class Rule:
    id: str
    dim: str
    weight: int
    must: bool
    bar: str
    fix: str
    probe: object


RULES: list[Rule] = []
DIMENSIONS = ["honest numbers", "decisions", "liveness", "evidence", "degraded states",
              "craft", "proof", "agentic", "composable surface"]


def rule(id, dim, weight, must, bar, fix):
    def deco(fn):
        RULES.append(Rule(id, dim, weight, must, bar, fix, fn))
        return fn
    return deco


# ---------------------------------------------------------------- applicability (three-state)
def agentic_kind(c: Corpus) -> tuple[str, str]:
    """Does this app run a model against its own data through a TOOL REGISTRY? The registry is
    the load-bearing part: it carries scope, audit and blast radius. Read from ``app_py``."""
    registry = c.has(r"ToolRegistry\(", r"def (tool_names|build_registry)\(", where="app_py")
    registers = c.has(r"\.register\(", r"FunctionTool\(", r"families\.build_all\(",
                      r"^SPECS\s*=", where="app_py")
    llm = c.has(r"chat/completions", r"APP_LLM_", r"json_complete", where="app_py")
    if registry and registers:
        return "yes", "tool registry with registered tools"
    if not (registry or registers):
        return "no", "no tool registry -- the agentic rules do not apply"
    return "unclear", ("half an assistant rail: " + ("a registry" if registry else "tool "
                       "registrations") + " without the other" + (" (and a model lane)" if llm
                       else "") + " -- a scope nothing enforces is a claim, not a boundary")


def dashboard_kind(c: Corpus) -> tuple[str, str]:
    """Does a PERSON arrange this app's dashboard? The marker is a card catalog or a persisted
    layout -- never "can you drag it", which matches every app with a resize handler."""
    stores = c.has(r"dashboard_layout|card_layout|layout_json", where="py")
    catalog = c.has(r"class DashboardCard|DASHBOARD_CARDS|CARD_CATALOG", where="py")
    if stores or catalog:
        return "yes", "cards a person arranges"
    return "no", "no composable dashboard -- these rules do not apply"


def _gate(kind_fn, c):
    kind, why = kind_fn(c)
    if kind == "no":
        return (None, why)
    if kind == "unclear":
        return (False, why)
    return None


# ---------------------------------------------------------------- 1. honest numbers
@rule("title-is-a-finding", "honest numbers", 6, True,
      "A page heading states what was FOUND, not what the page is called ('4 lines over "
      "budget', not 'Budget').",
      "Compute the <h1> from the data in the view, with a conditional for the empty case "
      "(patterns: BAR.md#title-is-a-finding).")
def _title(c):
    # The cap is generous on purpose: an honest computed heading carries a conditional
    # ({% if n %}{{ n }} ...{% else %}Nothing ...{% endif %}), which makes it LONGER than a label.
    hits = re.findall(r"<h1[^>]*>(.{0,2000}?)</h1>", c.html, re.I | re.S)
    computed = [h for h in hits if "{{" in h or "{%" in h]
    if not hits:
        return False, "no <h1> found in any template -- unmeasured, not clean"
    return bool(computed), f"{len(computed)}/{len(hits)} headings carry a computed value"


@rule("denominator", "honest numbers", 5, False,
      "Every count says what it is out of ('56 of 7,693 lines', '(300 measured)').",
      "Put the denominator in the same phrase as the number.")
def _denom(c):
    n = c.count(r"\b(of\s*\{\{|\{\{[^}]+\}\}\s*of\b|measured|out of\b)", where="ui_html")
    return n >= 2, f"{n} denominator phrase(s) in templates"


@rule("provenance", "honest numbers", 4, False,
      "A page with business numbers says where they came from and what window they cover.",
      "One provenance sentence under the heading: source, window, as-of.")
def _prov(c):
    return c.has(r"computed (live |from|over)", r"read (live )?from", r"as of\b",
                 r"source of record", where="ui_html"), "provenance prose"


@rule("absent-is-not-zero", "honest numbers", 4, False,
      "A missing value renders as a dash or 'not measured', never as 0 or a blank cell.",
      "Branch on None and render the absence explicitly.")
def _absent(c):
    return c.has(r"—|&mdash;|&#8212;|not measured|no data|default_if_none",
                 where="ui_html"), "absent marker"


@rule("measured-vs-modeled", "honest numbers", 4, False,
      "A modeled or estimated figure is labeled as such and never summed with a measured one.",
      "Label the lane beside the number and refuse the sum in code.")
def _modeled(c):
    return c.has(r"modell?ed|estimated|projection", where="ui_html"), "modeled label"


# ---------------------------------------------------------------- 2. decisions
@rule("gated-apply", "decisions", 6, False,
      "A destructive or bulk action is REVIEWED then applied (dry run / approve / apply "
      "approved), never fire-and-hope.",
      "Stage the change, show before/after, apply through the form's own code path "
      "(kits/assistant gate.staged for assistant writes).")
def _gated(c):
    return c.has(r"apply approved|dry.?run|review queue|pending approval|staged\("), "review-then-apply"


@rule("scope-guard", "decisions", 5, False,
      "A bulk apply refuses when the count moved between look and click, and records it.",
      "Send the expected count with the POST and compare server-side (gate.scope_moved).")
def _scope(c):
    return c.has(r"expected_?count", r"scope_moved\(", r"count (moved|changed)"), "scope guard"


@rule("revertable", "decisions", 4, False,
      "A decision can be reverted, and the UI says which ones can and cannot.",
      "Carry revertable on the audit row and render Revert.")
def _revert(c):
    return c.has(r"\brevert|\bundo\b|reopen|deactivate"), "revert path"


@rule("confirm-explains", "decisions", 3, False,
      "A confirm dialog states the CONSEQUENCE at the point of decision; never window.confirm().",
      "kits/shell: data-confirm=\"<what this will do>\" renders the shell's dialog.")
def _confirm(c):
    native = c.count(r"\bconfirm\(\s*['\"]", where="js") + c.count(r"onclick=\"[^\"]*confirm\(",
                                                                     where="ui_html")
    worded = c.has(r"data-confirm=\"[^\"]{12,}\"", r"cannot be undone|irreversible",
                   where="ui_html")
    if native:
        return False, f"{native} native confirm() call(s): the browser can suppress them"
    return worded, "consequence stated in the confirm"


# ---------------------------------------------------------------- 3. liveness
@rule("live-surface", "liveness", 5, True,
      "A surface that can change updates itself in place -- no manual refresh.",
      "kits/shell 'live' capability (SSE) or an htmx poll with a fingerprint.")
def _live(c):
    return c.has(r"hx-trigger=\"[^\"]*every\s+\d+", r"text/event-stream", r"EventSource\(",
                 r"\"live\"\s*:\s*\{"), "self-updating surface"


@rule("poll-pauses", "liveness", 5, True,
      "Polling STOPS when the tab is hidden and re-syncs on return.",
      "One visibilitychange guard over every poller (kits/shell does it for htmx and SSE).")
def _pause(c):
    if not _live(c)[0]:
        return None, "nothing polls"
    guarded = c.has(r"visibilitychange", r"document\.hidden", where="js")
    return guarded, "visibilitychange guard" if guarded else "polls forever in a hidden tab"


@rule("honest-poll", "liveness", 4, False,
      "A poll that finds nothing new answers 204/304 or compares a fingerprint instead of "
      "re-rendering identical markup.",
      "Fingerprint the STATE and answer 204 when it is unchanged.")
def _honest_poll(c):
    if not _live(c)[0]:
        return None, "nothing polls"
    return c.has(r"status=204", r"HttpResponseNotModified", r"fingerprint|etag",
                 where="py"), "204/fingerprint"


@rule("progress-names-domain", "liveness", 4, False,
      "A running process reports progress in ITS OWN terms ('142 of 300 lines -- 7 "
      "conflicts'), not a bare percentage.",
      "Emit counted classes with each progress frame.")
def _progress(c):
    if not c.has(r"progress", where="all"):
        return None, "no long-running process shown"
    return c.has(r"\{\{\s*[\w.]+\s*\}\}\s*of\s*\{\{", r"' of ' \+|\" of \" \+|`\$\{[^}]+\} of \$\{"), \
        "domain-named progress"


@rule("stream-teardown", "liveness", 4, False,
      "A stream can be cancelled by the client and the server tears the upstream down; a "
      "stalled wire is detected, not hung.",
      "GeneratorExit/CancelledError server-side, AbortController + a stall timer client-side.")
def _teardown(c):
    if not c.has(r"text/event-stream", where="py"):
        return None, "no stream"
    return c.has(r"GeneratorExit|CancelledError", where="py") and c.has(
        r"AbortController|\.close\(\)", where="js"), "cancel/teardown"


# ---------------------------------------------------------------- 4. evidence
@rule("audit-trail", "evidence", 5, True,
      "Every state-changing action writes an append-only row naming actor, target and time.",
      "An append-only model whose save() refuses updates (kits/gate GateEvent is the shape).")
def _audit(c):
    return c.has(r"append-only", r"class \w*(Audit|Event|Ledger)\w*\(models\.Model\)",
                 where="app_py"), "append-only audit"


@rule("evidence-chips", "evidence", 4, False,
      "A finding shows WHY it was raised -- the values that produced it -- beside it.",
      "Render the inputs next to the verdict, not only in a modal.")
def _evidence(c):
    return c.has(r"\bbecause\b", r"\bevidence\b", r"why this", where="ui_html"), "evidence in the row"


@rule("method-page", "evidence", 3, False,
      "An app showing business numbers ships a page stating how each number is counted.",
      "A /method/ page: rules, exclusions, source.")
def _method(c):
    return c.has(r"/method/|how (it|this) is counted|counting rules"), "method page"


@rule("no-fabrication", "evidence", 5, True,
      "Nothing renders invented data as real: no random 'live' numbers, no seeded rows in "
      "production.",
      "A real app starts empty and fills from its real source; demo data is labeled demo.")
def _fab(c):
    if not c.has(r"random\.(randint|uniform|choice|random)\s*\(", where="app_py"):
        return True, "no random data in the app's code"
    labeled = c.has(r"\bdemo\b|synthetic|sample data", where="ui_html")
    return labeled, "random values present" + (" and labeled demo" if labeled else " and NOT labeled")


# ---------------------------------------------------------------- 5. degraded states
@rule("empty-state", "degraded states", 5, True,
      "An empty result is a positive statement with the next action, not a blank table.",
      "{% empty %} with a sentence and the next action.")
def _empty(c):
    return c.has(r"\{%\s*empty\s*%\}", r"empty-state", r"nothing (to|here|yet)",
                 where="ui_html"), "empty state"


@rule("source-down", "degraded states", 4, False,
      "An unreachable source serves last-good data and SAYS so, or names the config it needs.",
      "Catch the outage, render the stale banner or 'not configured: SET_THIS'.")
def _degraded(c):
    if not c.has(r"httpx|requests\.|urlopen|pymssql|psycopg", where="app_py"):
        return None, "no external source"
    return c.has(r"unreachable|last good|stale|not configured|degraded|fell back"), "degraded state"


@rule("timeout-everywhere", "degraded states", 4, True,
      "Every outbound call carries an explicit timeout.",
      "timeout= on every call; a page that dials out without one hangs the whole app.")
def _timeout(c):
    calls = c.count(r"(httpx|requests)\.(get|post|put|patch|delete|stream|request)\s*\(",
                    where="app_py") + c.count(r"urlopen\s*\(", where="app_py")
    if not calls:
        return None, "no outbound calls"
    timeouts = c.count(r"timeout\s*=", where="app_py")
    return timeouts >= calls, f"{timeouts} timeout(s) for {calls} outbound call(s)"


@rule("ai-degrades", "degraded states", 3, False,
      "An AI surface labels its lane (model vs rules) and degrades to a deterministic answer.",
      "kits/settings APP_LLM_LANE: 'off: ... rules only' is rendered, never an error page.")
def _ai(c):
    if not c.has(r"chat/completions|APP_LLM_BASE_URL|json_complete", where="app_py"):
        return None, "no AI surface"
    return c.has(r"APP_LLM_LANE|rules only|fallback", where="all"), "labeled degrade"


# ---------------------------------------------------------------- 6. craft
@rule("design-system", "craft", 4, True,
      "Colour, space and type come from tokens, not hex values scattered through templates.",
      "kits/shell shell.css tokens (var(--...)); restyle by redefining tokens.")
def _tokens(c):
    hexes = c.count(r"#[0-9a-f]{6}\b", where="ui_html")
    tokens = c.count(r"var\(--")
    return tokens > hexes, f"{tokens} token refs vs {hexes} raw hexes in templates"


@rule("dark-mode", "craft", 3, True,
      "Both themes are first-class and chosen before first paint (no flash).",
      "kits/shell base.html sets data-theme in <head>; tokens redefine per theme.")
def _dark(c):
    return c.has(r"data-theme", r"prefers-color-scheme"), "theme support"


@rule("keyboard", "craft", 3, False,
      "Primary surfaces are reachable by keyboard: focus-visible styling, aria, shortcuts.",
      "kits/shell: palette, keyboard scrolling of the one scroller, :focus-visible.")
def _kbd(c):
    return c.has(r"focus-visible", where="css") and c.has(r"aria-", where="ui_html"), "keyboard/aria"


@rule("shell-capabilities", "craft", 3, False,
      "The shell's capabilities are DECLARED, so the app ships the ones it uses and none of "
      "the ones it does not.",
      "settings.APP_SHELL = {...}; kits/shell renders only what is declared.")
def _shell_caps(c):
    if not c.has(r"data-app-shell", where="ui_html"):
        return None, "not on the kit's app shell"
    declared = c.has(r"(?m)^APP_SHELL\s*=\s*\{", where="app_py")
    return declared, "capabilities declared" if declared else "on the shell, declares nothing"


@rule("motion-consent", "craft", 3, False,
      "Motion is switched off under prefers-reduced-motion.",
      "One @media (prefers-reduced-motion: reduce) block (kits/shell shell.css has it).")
def _motion(c):
    if not c.has(r"transition|animation|@keyframes", where="css"):
        return None, "no motion"
    return c.has(r"prefers-reduced-motion", where="css"), "reduced-motion guard"


@rule("status-not-colour", "craft", 3, False,
      "A status carries a word or glyph as well as its colour.",
      "kits/shell .live-word beside .live-pip; badges carry text.")
def _status_not_colour(c):
    if not c.has(r"live-pip|status-dot|\bbadge\b|\bpill\b", where="ui_html"):
        return None, "no status indicator"
    return c.has(r"live-word|sr-only|aria-label", where="ui_html"), "status carries a word"


@rule("reachable", "craft", 4, False,
      "If the shell pins <body>, some region declares itself the scroller -- otherwise no page "
      "taller than the window can be read to the end.",
      "kits/shell: <main class=\"app-scroll\"> is the one scroller, keyboard-scrollable.")
def _reachable(c):
    pinned = c.has(r"body\s*\{[^}]*overflow:\s*hidden", where="css") or c.has(
        r"<body[^>]*overflow(-hidden|:\s*hidden)", where="ui_html")
    if not pinned:
        return None, "document scrolls itself (<body> is not pinned)"
    if c.has(r"<main[^>]*\b(app-scroll|overflow-y-auto|overflow-auto)\b", where="ui_html"):
        return True, "the shell's <main> declares the scroller"
    return False, "<body> is pinned and <main> declares no scroller"


@rule("responsive", "craft", 2, False,
      "Layout adapts -- breakpoints, not a fixed desktop width.",
      "A media query or container query in the stylesheet.")
def _resp(c):
    return c.has(r"@media[^{]*(max-width|min-width)", r"@container", where="css"), "breakpoints"


# ---------------------------------------------------------------- 7. proof
@rule("error-visibility", "proof", 6, True,
      "Failures are VISIBLE from outside the host: server 500s and background-job deaths "
      "land somewhere queryable, readable without a shell on the host.",
      "The hub's patterns/error-visibility.md: a LOGGING handler forwarding ERROR+ to "
      "POST /hub/api/app-error, wired before the first feature.")
def _errvis(c):
    browser = c.has(r"unhandledrejection", r"addEventListener\(\s*['\"]error", where="js")
    server = c.has(r"got_request_exception", r"class\s+\w+\(\s*logging\.Handler\s*\)",
                   r"['\"]class['\"]\s*:\s*['\"](?!logging\.)[\w.]+Handler['\"]", where="py")
    # Readable from outside: forwarded to the hub's error stream, or an in-app errors page. The
    # browser half is reported but not required -- the hub pattern keeps other apps' stale-tab
    # noise off the shared queue on purpose.
    readable = c.has(r"/api/app-error|HUB_API_BASE|/errors/|manage\.py errors", where="all")
    return server and readable, (f"server={'y' if server else 'n'} "
                                 f"readable-from-outside={'y' if readable else 'n'} "
                                 f"browser={'y' if browser else 'n'}")


_GATE_CLASS = re.compile(r"(?m)^class\s+\w*(?:Access|Auth|Gate)\w*Middleware\b")
_GATE_EXEMPT = re.compile(r"\b(PUBLIC\w*|EXEMPT\w*|OPEN_\w+)\b|\b_?is_(?:exempt|open|public)\(")
_GATE_DENY = re.compile(r"redirect\(|status=40[13]|HttpResponseForbidden|build_login_url\(")
#: The DEFAULT in the resolver call, not any assignment of the name -- a test posture that sets
#: the flag false inside "if running tests" is not an app that ships open.
_SHIPS_OPEN = re.compile(r"(?:env_bool|os\.environ\.get|os\.getenv|getattr)\s*\([^)]*?"
                         r"(GATE_REQUIRED|AUTH_REQUIRED|REQUIRE_LOGIN)[^)]*?,\s*"
                         r"(?:False|['\"](?:0|false|no)['\"])\s*\)", re.I)


@rule("auth-posture", "proof", 6, True,
      "No page is reachable without signing in: a request-path gate denies by DEFAULT and its "
      "arming flag does not default off.",
      "kits/gate GateMiddleware: deny-by-default, public paths named one at a time, "
      "APP_GATE_REQUIRED defaults on.")
def _auth(c):
    bodies = []
    for m in _GATE_CLASS.finditer(c.py):
        nxt = re.search(r"(?m)^(?:class|def)\s", c.py[m.end():])
        bodies.append(c.py[m.start():m.end() + (nxt.start() if nxt else len(c.py))])
    gate = any(_GATE_EXEMPT.search(b) and _GATE_DENY.search(b) for b in bodies)
    ships_open = bool(_SHIPS_OPEN.search(c.py))
    wired = c.has(r"GateMiddleware|AuthMiddleware|AccessMiddleware", where="py")
    if not (gate or wired):
        return False, "no request-path gate found"
    return gate and not ships_open, (f"gate={'y' if gate else 'n'} "
                                     f"arming-flag-defaults-off={'YES' if ships_open else 'n'}")


@rule("auth-deploy-verified", "proof", 3, False,
      "The deploy itself asserts the app's root refuses an anonymous request.",
      "kits/service-runner --verify <name>: 'AUTH POSTURE FAILED' when a gated root answers 200.")
def _auth_deploy(c):
    manifests = [n for n in c.files if n.rsplit("/", 1)[-1] == "services.json"]
    if not manifests and not c.has(r"services\.json|deploy", where="ops"):
        return None, "no deploy lane in this tree"
    gates, unreadable = [], []
    for name in manifests:
        try:
            services = json.loads(c.files[name]).get("services") or []
        except (ValueError, AttributeError):
            unreadable.append(name)
            continue
        gates += [s.get("gate") if isinstance(s, dict) else None for s in services]
    undeclared = sum(1 for g in gates if g not in ("required", "public"))
    runner = any(n.rsplit("/", 1)[-1] == "service_runner.py" for n in c.files)
    scripted = c.has(r"--verify|AUTH POSTURE", where="ops")
    ok = not unreadable and not undeclared and (runner or scripted) and (gates or scripted)
    return ok, (f"{len(gates)} service(s), {len(gates) - undeclared} with a declared gate"
                + (f"; unreadable manifest {', '.join(unreadable)}" if unreadable else "")
                + ("; verifier present" if runner or scripted else "; no verifier in the tree"))


@rule("health-endpoints", "proof", 3, True,
      "The app exposes /health/live/ and a /health/ready/ that needs the app's own tables.",
      "kits/health: live touches nothing; ready fails on an unmigrated database.")
def _health(c):
    both = c.has(r"health/?.{0,40}live", where="py") and c.has(r"ready", where="py")
    # The query actually executed, not the words: a docstring explaining why SELECT 1 is not
    # readiness must not convict the probe that avoids it.
    shallow = c.has(r"execute\(\s*[\"']SELECT 1\b", where="app_py")
    return both and not shallow, ("live+ready" if both else "missing a probe") + (
        "; readiness is SELECT 1, which passes with no tables" if shallow else "")


# ---------------------------------------------------------------- 8. agentic
def _agent_text(c: Corpus) -> str:
    """The app's OWN assistant code: files named for an agent or assistant, outside the vendored
    kit. Reading the whole app for "writes" would convict a read-only catalog for the app's
    ordinary views, and credit a guard that lives nowhere near the tools."""
    return "\n".join(t for n, t in c.files.items() if n.endswith(".py")
                     and re.search(r"agent|assistant", n.rsplit("/", 1)[-1])
                     and "/assistant/" not in "/" + n)


def _drives_a_model(c: Corpus) -> bool:
    return c.has(r"run_route\(|agent_loop|stream_fn|chat/completions|plan_turn\(", where="app_py")


@rule("agent-scope-declared", "agentic", 5, True,
      "Every tool is REGISTERED with a declared scope, and the registry enforces it -- not the "
      "prompt, not the tool function.",
      "Register reads as reads; register writes with their scope and roles (kits/assistant).")
def _agent_scope(c):
    skip = _gate(agentic_kind, c)
    if skip:
        return skip
    return c.has(r"scope\s*=|Scope\.|register_all\(", where="app_py"), "scopes declared"


@rule("agent-write-needs-a-person", "agentic", 5, True,
      "A tool that changes something needs a confirmation the MODEL CANNOT MINT.",
      "kits/assistant gate: server-minted token, staged proposal, or signed plan card.")
def _agent_write(c):
    skip = _gate(agentic_kind, c)
    if skip:
        return skip
    text = _agent_text(c)
    writes = re.search(r"Scope\.WRITE|Scope\.DESTRUCTIVE|propose_\w+|\.save\(|\.update\(|"
                       r"\.delete\(|\.create\(", text)
    if not writes:
        return True, "read-only catalog (no write in the app's assistant code)"
    gated = re.search(r"gate\.(mint|sign|verify|staged)|needs_confirmation", text)
    return bool(gated), ("writes are gated by a person" if gated else
                         f"the assistant writes ({writes.group(0)}) with no gate a model cannot mint")


@rule("agent-payload-allowlist", "agentic", 4, False,
      "What a tool returns is CHOSEN field by field; a column added later stays in.",
      "Entity.public_fields (inclusion) + sensitive (veto) in the adapter.")
def _agent_allow(c):
    skip = _gate(agentic_kind, c)
    if skip:
        return skip
    return c.has(r"public_fields\s*=", r"\.values\([^)]", where="app_py"), "field allow-list"


@rule("agent-caps-declared", "agentic", 3, False,
      "A bounded result says what it is out of (shown vs total).",
      "The core family returns shown and total on every row-returning tool.")
def _agent_caps(c):
    skip = _gate(agentic_kind, c)
    if skip:
        return skip
    return c.has(r"[\"']total[\"']\s*:", r"families\.build_all\(", where="app_py"), "caps declared"


@rule("agent-tool-timeout", "agentic", 3, False,
      "A tool call is bounded by a timeout, so a hang is a failure, never a spinner forever.",
      "wait_for/timeout around every tool invocation in the loop.")
def _agent_timeout(c):
    skip = _gate(agentic_kind, c)
    if skip:
        return skip
    if not _drives_a_model(c):
        return None, "no model loop in this app -- the registry is counted, never driven"
    return c.has(r"wait_for\(|TOOL_TIMEOUT|timeout\s*=", where="app_py"), "tool timeout"


@rule("agent-shows-its-work", "agentic", 3, False,
      "The answer arrives WITH what it looked up.",
      "Emit tool frames to the page; render them beside the answer.")
def _agent_work(c):
    skip = _gate(agentic_kind, c)
    if skip:
        return skip
    if not _drives_a_model(c):
        return None, "no model loop in this app -- nothing answers yet"
    return c.has(r"tool_result|event: tool|\"tool\"", where="all"), "tool activity shown"


def _canon_keys(text: str) -> list:
    """FAMILIES keys read as DATA -- importing the app's copy would execute the tree being
    judged, and a bar with a blast radius is not a bar."""
    return re.findall(r'Family\(\s*(?:key\s*=\s*)?["\']([a-z_]+)["\']', text)


def _app_canon(c: Corpus):
    return next((t for n, t in c.files.items() if n.endswith("assistant/canon.py")), None)


@rule("agent-canon-accounted", "agentic", 6, True,
      "Every canon family is ACCOUNTED FOR: served by tools, or declared as a gap with the "
      "reason. No test can fail for a tool nobody wrote; a count can.",
      "Use kits/assistant, declare ASSISTANT_GAPS, and run `python -m assistant.audit`.")
def _canon_accounted(c):
    skip = _gate(agentic_kind, c)
    if skip:
        return skip
    canon = _app_canon(c)
    if canon is None:
        return False, "no canon: a missing capability reads the same as a declined one"
    if not c.has(r"(?m)^ASSISTANT_GAPS\s*=", where="py"):
        return False, f"carries the canon ({len(_canon_keys(canon))} families) but declares no ASSISTANT_GAPS"
    return True, f"{len(_canon_keys(canon))} canon families, gaps declared"


@rule("agent-canon-current", "agentic", 4, False,
      "The canon the app counts against is CURRENT -- an old copy reports full coverage of a "
      "smaller canon, the one failure a count cannot catch by itself.",
      "Re-vendor with `tools/kits.py add assistant <app>`; `kits.py status <app>` names drift.")
def _canon_current(c):
    skip = _gate(agentic_kind, c)
    if skip:
        return skip
    canon = _app_canon(c)
    lib = KIT_ROOT / "kits" / "assistant" / "assistant" / "canon.py"
    if canon is None:
        return False, "no canon in this app"
    if not lib.is_file():
        return False, "the kit's own canon is not readable, so currency is unmeasured"
    mine, current = _canon_keys(canon), _canon_keys(lib.read_text(encoding="utf-8"))
    missing = [k for k in current if k not in mine]
    if missing:
        return False, (f"{len(mine)} families here against the kit's {len(current)}; missing "
                       + ", ".join(missing[:6]) + stale_note(KIT_ROOT))
    return True, f"canon current at {len(mine)} families" + stale_note(KIT_ROOT)


# ---------------------------------------------------------------- 9. composable surface
@rule("layout-is-placed", "composable surface", 5, True,
      "A card has a definite column and row, not a position in a flow.",
      "Persist col/row/span per card on a fixed grid (e.g. 30 columns: halves, thirds, fifths).")
def _placed(c):
    skip = _gate(dashboard_kind, c)
    if skip:
        return skip
    return c.has(r"[\"'](col|row)[\"']\s*:", r"\b(col|row)\w*\s*=\s*models\.", where="py") \
        and c.has(r"\bspan\b", where="py"), "explicit column and row"


@rule("layout-bounded-server-side", "composable surface", 5, True,
      "A posted layout is BOUNDED in Python: each value clamped or the blob size-limited.",
      "Clamp col/row/span and cap the card count before saving.")
def _bounded(c):
    skip = _gate(dashboard_kind, c)
    if skip:
        return skip
    return c.has(r"\bmin\(|\bmax\(|clamp|MAX_CARDS|len\([^)]*\)\s*>", where="app_py"), "server bounds"


@rule("card-and-drill-agree", "composable surface", 6, False,
      "A card's number and the rows its drill opens come from ONE filter grammar.",
      "One queryset builder per card, used by both the count and the drill.")
def _drill(c):
    skip = _gate(dashboard_kind, c)
    if skip:
        return skip
    return c.has(r"def \w*(filter|queryset)_for\w*\(", where="app_py"), "shared filter"


@rule("layout-survives-a-grid-change", "composable surface", 4, False,
      "A layout saved under an older grid still opens.",
      "Store the grid version with the layout and migrate on read.")
def _grid(c):
    skip = _gate(dashboard_kind, c)
    if skip:
        return skip
    return c.has(r"grid_version|GRID_VERSION|layout_version", where="app_py"), "grid versioned"


@rule("card-catalog-is-one-place", "composable surface", 3, False,
      "One catalog knows every card, so grid, tray and chart script cannot drift.",
      "A single CARD_CATALOG consumed by every surface.")
def _catalog(c):
    skip = _gate(dashboard_kind, c)
    if skip:
        return skip
    return c.count(r"(?m)^(CARD_CATALOG|DASHBOARD_CARDS)\s*=", where="app_py") == 1, "one catalog"


# ---------------------------------------------------------------- scoring and reports
def score(app: Path) -> dict:
    c = Corpus.load(app)
    results, tot, got = [], dict.fromkeys(DIMENSIONS, 0), dict.fromkeys(DIMENSIONS, 0)
    for r in RULES:
        ok, evidence = r.probe(c)
        applies = ok is not None
        results.append({"id": r.id, "dim": r.dim, "weight": r.weight, "must": r.must,
                        "applies": applies, "pass": bool(ok), "evidence": evidence,
                        "bar": r.bar, "fix": r.fix})
        if applies:
            tot[r.dim] += r.weight
            got[r.dim] += r.weight if ok else 0
    total = sum(tot.values())
    return {
        "app": app.resolve().name, "path": str(app), "files_read": len(c.files),
        "score": round(100 * sum(got.values()) / total) if total else 0,
        "measured": bool(c.files) and bool(total),
        "dimensions": {d: {"got": got[d], "of": tot[d], "pct": round(100 * got[d] / tot[d])}
                       for d in DIMENSIONS if tot[d]},
        "not_applicable": [r["id"] for r in results if not r["applies"]],
        "must_failures": [r["id"] for r in results if r["must"] and r["applies"] and not r["pass"]],
        "rules": results, "declared": c.declared, "stale": stale_note(app).strip(),
    }


BADGES = [(90, "FINISHED"), (75, "STRONG"), (60, "WORKING"), (0, "THIN")]


def badge(n: int) -> str:
    return next(b for t, b in BADGES if n >= t)


def render(rep: dict, verbose: bool) -> str:
    out = [f"\n{rep['app']}  --  {rep['score']}/100  [{badge(rep['score'])}]   "
           f"({rep['files_read']} files read from {rep['path']})"]
    if rep["stale"]:
        out.append(f"   {rep['stale']}")
    out += [f"   {note}" for note in rep["declared"]]
    out.append("-" * 72)
    for d, v in rep["dimensions"].items():
        bar = "#" * round(v["pct"] / 10) + "." * (10 - round(v["pct"] / 10))
        out.append(f"  {d:<19} {bar} {v['pct']:>3}%   ({v['got']}/{v['of']})")
    unmeasured = [d for d in DIMENSIONS if d not in rep["dimensions"]]
    if unmeasured:
        out.append(f"  not measured        {', '.join(unmeasured)} -- no rule there applies; "
                   f"scored over {len(rep['dimensions'])} of {len(DIMENSIONS)} dimensions")
    fails = sorted((r for r in rep["rules"] if r["applies"] and not r["pass"]),
                   key=lambda x: (-x["must"], -x["weight"]))
    if fails:
        out.append("\n  NOT YET AT THE BAR:")
        for r in fails:
            out.append(f"   [{'MUST' if r['must'] else '+' + str(r['weight']):>4}] {r['id']} -- {r['bar']}")
            out.append(f"          -> {r['fix']}")
            if verbose:
                out.append(f"          measured: {r['evidence']}")
    if verbose:
        out.append("\n  AT THE BAR:")
        out += [f"   [ ok ] {r['id']:<30} {r['evidence']}" for r in rep["rules"]
                if r["applies"] and r["pass"]]
        skipped = [r for r in rep["rules"] if not r["applies"]]
        if skipped:
            out.append("\n  DOES NOT APPLY (counted on neither side of the score):")
            out += [f"   [ n/a ] {r['id']:<29} {r['evidence']}" for r in skipped]
    if rep["must_failures"]:
        out.append(f"\n  BLOCKING: {len(rep['must_failures'])} MUST rule(s) failed -- "
                   + ", ".join(rep["must_failures"]))
    return "\n".join(out)


def rule_health(apps: list[Path]) -> list[dict]:
    """For each rule, across the apps given: how many it APPLIES to and how many pass. A rule
    that passes everyone is a regression guard, not a ranking; one that describes almost nobody
    is an aspiration or an invention; one that never applies is unmeasured."""
    reports = [score(a) for a in apps]
    rows = []
    for r in RULES:
        applies = passes = 0
        failing = []
        for rep in reports:
            row = next(x for x in rep["rules"] if x["id"] == r.id)
            if not row["applies"]:
                continue
            applies += 1
            passes += row["pass"]
            if not row["pass"]:
                failing.append(rep["app"])
        rate = passes / applies if applies else None
        verdict = ("NEVER APPLIES" if rate is None else "passes everyone" if rate >= 1.0
                   else "describes almost nobody" if rate <= 0.15 else "discriminates")
        rows.append({"id": r.id, "dim": r.dim, "must": r.must, "applies": applies,
                     "passes": passes, "rate": rate, "verdict": verdict, "failing": failing})
    return rows


def counts() -> dict:
    return {"dimensions": len({r.dim for r in RULES}), "rules": len(RULES),
            "must": sum(r.must for r in RULES)}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Measure an app against the app-kit bar.")
    ap.add_argument("apps", nargs="*", help="app directory (or several, with --rules)")
    ap.add_argument("--rules", action="store_true", help="audit the BAR across the given apps")
    ap.add_argument("--count", action="store_true", help="print the bar's own size")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min", type=int, default=90)
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args(argv)

    if a.count:
        n = counts()
        print(json.dumps(n) if a.json else
              f"THE BAR: {n['dimensions']} dimensions, {n['rules']} rules, {n['must']} MUST")
        return 0
    if not a.apps:
        ap.error("give an app directory")
    paths = [Path(p) for p in a.apps]
    missing = [str(p) for p in paths if not p.is_dir()]
    if missing:
        print("no such directory: " + ", ".join(missing), file=sys.stderr)
        return 2
    if a.rules:
        rows = rule_health(paths)
        if a.json:
            print(json.dumps(rows, indent=1))
            return 0
        print(f"\nTHE BAR MEASURED ACROSS {len(paths)} APP(S)\n" + "-" * 78)
        for r in sorted(rows, key=lambda x: (x["rate"] is not None and x["rate"], x["id"])):
            rate = "n/a" if r["rate"] is None else f"{round(100 * r['rate']):>3}%"
            print(f"{'!' if r['must'] else ' '}{r['id']:33}{r['dim']:21}{r['applies']:>4}{rate:>6}  {r['verdict']}")
        disc = sum(r["verdict"] == "discriminates" for r in rows)
        print("-" * 78 + f"\n{disc} of {len(rows)} rules separate these apps.")
        return 0
    if len(paths) != 1:
        ap.error("score one app at a time (use --rules for several)")
    rep = score(paths[0])
    if not rep["measured"]:
        print(f"MEASURED NOTHING at {paths[0]}: {rep['files_read']} files read and no rule "
              "applied. That is not a pass -- point this at the app's directory.")
        return 2
    print(json.dumps(rep, indent=1) if a.json else render(rep, a.verbose))
    blocked = bool(rep["must_failures"]) or rep["score"] < a.min
    if not a.json:
        n = counts()
        print(f"\n{'BELOW THE BAR' if blocked else 'AT THE BAR'} (score {rep['score']}, floor "
              f"{a.min}; bar = {n['rules']} rules, {n['must']} MUST, {n['dimensions']} dimensions)\n")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
