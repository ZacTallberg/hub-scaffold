"""Process-level memo discovery: what the shared battery runner must forget between files.

fastcheck execs every battery file in ONE interpreter to pay Django's boot once. That is sound
only while no process-level memo carries one test's board into the next — and two empty temp
boards share (seq=0, hash=""), so a cache keyed on the ledger head will happily serve one test's
answer to another. The reset was a hand-maintained list of four names against 29 module-level
dicts, which is the shape of every guard this project has had to replace: a denylist of the cases
someone thought of. Three new memos landed in a single session without touching it.

So discovery is STRUCTURAL. A module-level dict is a cache iff its name ends in one of
CACHE_SUFFIXES, and every other module-level dict must be DECLARED in CONSTANT_TABLES. Anything
in neither set is `undeclared()`, which the self-test fails on — so a new memo is covered the
moment it is named by the convention, and a memo named outside it cannot slip through silently.
"""
import importlib
import pkgutil

CACHE_SUFFIXES = ("_CACHE", "_MEMO", "_CKPT", "_CHECKPOINT")
PACKAGES = ("hub", "hub_core")

# Module-level dicts that are NOT caches. A constant lookup table cleared between files would not
# leak state — it would delete the vocabulary the code runs on. `_LEDGER_RLOCKS`/`_LEDGER_DEPTH`
# are live re-entrancy bookkeeping for the append lock: clearing them mid-flight would forget that
# this thread holds it. Every entry here is a deliberate statement that the dict carries no
# per-board answer.
CONSTANT_TABLES = frozenset({
    "hub.agent_card._IDENT",
    "hub.agent_card._SKILL_BLURBS",
    "hub.mcp_server._IDENT",
    "hub.hub_api._COLLECTION",
    "hub_core.cost._ATTR_FOR",
    "hub_core.project._EDGES",
    "hub_core.projections._STATUS_GLYPH",
    "hub_core.viz._STATUS_TOKEN",
    "hub_core.store._LEDGER_DEPTH",
    "hub_core.store._LEDGER_RLOCKS",
    # The reset's own memory of what pristine looks like. Resetting it mid-run would discard the
    # baseline every later file is restored to — it is machinery, not a per-board answer. (The
    # enforcement below caught this one on its own author, which is the point of it.)
    "hub_core.caches._BASELINE",
    # The delivery projection's absence VOCABULARY: the sentence each unmeasured field carries when
    # it has no evidence. Clearing it between files would leave records absent-with-no-reason, which
    # is the one thing that projection may never produce.
    "hub.delivery._ABSENT_DEFAULT",
    "hub.delivery._STATE_ORDER",
    # The oracle's per-language VOCABULARY: which prefixes open a comment, and which idioms count
    # as a deterministic signal. Clearing these would not drop a cached answer, it would delete the
    # languages the oracle knows how to read.
    "hub_core.audit._ORACLE_COMMENT",
    "hub_core.audit._ORACLE_SIGNALS",
    # Live re-entrancy bookkeeping for the append lock and the process lock: which fd holds the
    # byte-range lock that IS the critical section, and how deep THIS thread is inside it. Clearing
    # any of these mid-flight would forget that this thread holds the lock — the failure is a
    # double-entered critical section, not a stale read.
    "hub_core.store._LEDGER_FDS",
    "hub_core.process_lock._THREAD_LOCKS",
    "hub_core.process_lock._DEPTH",
    # Process instrumentation: how many audit spawns were batched vs fell back. It counts what this
    # process DID, never what a board contains, so it carries no per-board answer to leak.
    "hub_core.audit._BATCH_STATS",
})


def _modules():
    """Every importable module in the hub packages. An import that fails is skipped rather than
    raised: this runs inside a test runner, and a module that cannot import has no live memo."""
    mods = []
    for name in PACKAGES:
        try:
            pkg = importlib.import_module(name)
        except Exception:
            continue
        mods.append(pkg)
        for info in pkgutil.iter_modules(getattr(pkg, "__path__", []), pkg.__name__ + "."):
            try:
                mods.append(importlib.import_module(info.name))
            except Exception:
                continue
    return mods


def _module_dicts():
    """[(qualified name, module, attr, dict)] for every UPPERCASE module-level dict."""
    out = []
    for mod in _modules():
        for attr, value in list(vars(mod).items()):
            if not attr.startswith("_") or not isinstance(value, dict):
                continue
            if attr != attr.upper():
                continue
            out.append((f"{mod.__name__}.{attr}", mod, attr, value))
    return sorted(out, key=lambda row: row[0])


def is_cache(attr):
    return attr.endswith(CACHE_SUFFIXES)


def discover():
    """The qualified names of every discoverable cache, sorted."""
    return [q for q, _m, attr, _d in _module_dicts() if is_cache(attr)]


def undeclared():
    """Module-level dicts that are neither named as a cache nor declared a constant table.

    A non-empty result is a real defect, not a style complaint: the dict is either a memo the
    battery never forgets (cross-test contamination) or a table nobody has vouched for."""
    return [q for q, _m, attr, _d in _module_dicts()
            if not is_cache(attr) and q not in CONSTANT_TABLES]


# The pristine contents of each cache, captured before any test has run. Reset RESTORES this
# rather than clearing, because a cache's SHAPE can be part of its contract: hub_core.identity's
# reads `_CACHE["key"]` directly, so an emptied dict is a KeyError at import time, not a cold
# cache. (The hand-written reset this replaces knew that for one memo — it assigned the slots back
# instead of clearing — and that knowledge was exactly the kind a list loses.)
_BASELINE = {}


def snapshot_baseline():
    """Record every cache's current contents as the state reset_all() returns to. Call once,
    before the first test file, while the process is still pristine."""
    _BASELINE.clear()
    for qualified, _mod, attr, value in _module_dicts():
        if is_cache(attr):
            _BASELINE[qualified] = dict(value)
    return sorted(_BASELINE)


def reset_all():
    """Return every discoverable cache to its pristine contents. Returns the names reset, so a
    caller can assert it did something rather than trusting a silent no-op.

    With no baseline captured yet, this captures one first: a library caller gets sane behaviour,
    and the battery runner takes the snapshot explicitly at the point it knows is clean."""
    if not _BASELINE:
        snapshot_baseline()
    reset = []
    for qualified, _mod, attr, value in _module_dicts():
        if not is_cache(attr):
            continue
        value.clear()
        value.update(_BASELINE.get(qualified, {}))
        reset.append(qualified)
    return reset
