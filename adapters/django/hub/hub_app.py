"""Project hub integration: the Django adapter over the portable, stack-neutral hub_core.

Single source of truth = the event log in PROJECT/.hub. The Django views, the typed write API,
and `manage.py hubaudit` all go through here. Django-TOLERANT at import time: every Django
settings read is lazy + guarded, so the pure helpers stay unit-testable without django.setup().

Settings keys (all optional; the {{...}} literals are the documented defaults that init.sh
substitutes at scaffold time):
    HUB_PROJECT_KEY   entity-id prefix (lowercase slug), e.g. "acme".  Default "{{PROJECT_KEY}}".
    HUB_BRAND         human brand for titles, e.g. "Acme".             Default "{{BRAND}}".
    HUB_PROJECT_DIR   canonical project-plane directory.               Default BASE_DIR/PROJECT.
    HUB_WORK_ROOT     repository/evidence root for Git and local paths. Default PROJECT.parent.
    HUB_BUILD_STAMP   BASE_DIR-relative path of the build-sha stamp the deploy pipeline bakes
                      into the artifact.                               Default "build_sha.txt".
    HUB_BUILD_SHA     explicit immutable revision injected by the artifact platform; overrides stamp.
    HUB_DONE_STRICTNESS completion proof dial: tracked or strict.       Default "tracked".
    HUB_SETTINGS_FILE settings.py path the AST security audit scans.   Default: the module file
                      of DJANGO_SETTINGS_MODULE.
    HUB_WRITE_TOKEN   shared-root compatibility bearer token; grants terminal board authority,
                      not shell execution. Fail-closed when empty.
    HUB_SHARED_TOKEN_COMPAT accept the legacy shared-root credential. Default True for migration.
    HUB_WORKER_LAUNCH_ENABLED   expose the optional grant-backed local launcher. Default False.
    HUB_WORKER_PROTOCOL         custom URL scheme registered on the workstation. Default hub-worker.
    HUB_WORKER_LAUNCH_ISSUER_URL explicit HTTPS consume endpoint (recommended in production).
    HUB_WORKER_GRANT_TTL_S      short grant lifetime, clamped by hub_core. Default 120 seconds.
The project plane defaults to Django ``BASE_DIR/PROJECT``; ``HUB_PROJECT_DIR`` relocates that whole
canonical plane, ``HUB_WORK_ROOT`` can identify a nested adopter's repository/evidence root, and
``HUB_DIR`` can separately relocate runtime ledger state. Production must configure it explicitly
to a durable mounted path; the runtime reports and audits implicit or unwritable storage.
"""
import ast
import functools
import json
import os
import subprocess
import time
from pathlib import Path

import hub_core
from hub_core import audit as _audit
from hub_core import identity as _identity
from hub_core import project as _project


def _dj_setting(name, default=None):
    """A Django settings value, or `default` when Django is absent/unconfigured (CLI/unit use)."""
    try:
        from django.conf import settings
        return getattr(settings, name, default)
    except Exception:
        return default


BASE_DIR = Path(_dj_setting("BASE_DIR") or os.environ.get("HUB_BASE_DIR") or Path.cwd())
_PROJECT_SETTING = _dj_setting("HUB_PROJECT_DIR")
_PROJECT_OVERRIDE = _PROJECT_SETTING or os.environ.get("HUB_PROJECT_DIR")
PROJECT = Path(_PROJECT_OVERRIDE) if _PROJECT_OVERRIDE else BASE_DIR / "PROJECT"
if not PROJECT.is_absolute():
    PROJECT = BASE_DIR / PROJECT
# hub_core.identity is deliberately Django-free and reads this same canonical override from the
# environment. Mirror a Django setting into the process before loading identity so the adapter,
# agent card, MCP metadata, schemas, and ledger cannot split across two Project Planes.
if _PROJECT_SETTING:
    os.environ["HUB_PROJECT_DIR"] = str(PROJECT)
else:
    os.environ.setdefault("HUB_PROJECT_DIR", str(PROJECT))
_WORK_ROOT_OVERRIDE = _dj_setting("HUB_WORK_ROOT") or os.environ.get("HUB_WORK_ROOT")
WORK_ROOT = Path(_WORK_ROOT_OVERRIDE) if _WORK_ROOT_OVERRIDE else PROJECT.parent
if not WORK_ROOT.is_absolute():
    WORK_ROOT = BASE_DIR / WORK_ROOT
_HUB_DIR_OVERRIDE = _dj_setting("HUB_DIR") or os.environ.get("HUB_DIR")
HUB_DIR_CONFIGURED = bool(_HUB_DIR_OVERRIDE)
HUB_DIR = Path(_HUB_DIR_OVERRIDE or (PROJECT / ".hub"))
if not HUB_DIR.is_absolute():
    HUB_DIR = BASE_DIR / HUB_DIR
SCHEMA_DIR = PROJECT / "schema"
_IDENTITY = _identity.load()
PROJECT_KEY = _dj_setting("HUB_PROJECT_KEY", _IDENTITY["key"])
BRAND = _dj_setting("HUB_BRAND", _IDENTITY["brand"])


def _publish_realtime(kind, **identity):
    """Publish only mutation identity after durability; canonical content stays on the read API.

    Kept lazy so pure/CLI imports of ``hub_app`` do not require Django's streaming surface.  A
    broker outage is deliberately non-transactional: the ledger/lease is already the truth, and a
    reconnect cursor repairs notification loss.
    """
    try:
        from . import realtime
        realtime.publish(HUB_DIR, {"kind": kind, **identity}, channel=PROJECT_KEY)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Hub realtime wake-up failed after durable mutation")


def publish_event(event):
    """Broadcast the safe event envelope used by SSE, never its entity payload."""
    _publish_realtime(
        "ledger.appended",
        event={
            "seq": event.get("seq"),
            "ts": event.get("ts"),
            "event": event.get("type"),
            "aggregate": event.get("aggregate"),
            "version": event.get("result_version"),
            "agent": event.get("agent_id"),
        },
    )


def realtime_info():
    from . import realtime
    info = realtime.info(HUB_DIR, channel=PROJECT_KEY)
    info["storage"] = storage_info()
    return info


def storage_info():
    """Public, topology-free truth about the active ledger's runtime boundary."""
    target = HUB_DIR
    while not target.exists() and target.parent != target:
        target = target.parent
    writable = bool(target.exists() and target.is_dir() and os.access(target, os.W_OK))
    if writable and HUB_DIR.exists():
        for name in ("events.jsonl", "events.db", "agent-credentials.json"):
            candidate = HUB_DIR / name
            if candidate.exists() and not os.access(candidate, os.W_OK):
                writable = False
                break
    return {
        "configuration": "explicit" if HUB_DIR_CONFIGURED else "implicit",
        "writable": writable,
        # Explicit configuration is an operator declaration, not a fabricated claim that Python
        # can prove a volume will survive its container or host.
        "durability": "operator-declared" if HUB_DIR_CONFIGURED else "unconfirmed",
    }


def _schedule_lease_truth(lease):
    """Wake readers exactly when a lease becomes stalled or expires; no clock polling."""
    try:
        from . import realtime
        task = lease.get("task")
        agent = lease.get("agent")
        expires = float(lease.get("expires") or 0)
        heartbeat = float(lease.get("last_heartbeat") or lease.get("claimed") or 0)
        now = time.time()
        stall_at = heartbeat + 900
        if heartbeat and now < stall_at < expires:
            realtime.schedule(HUB_DIR, "lease-stall:" + task, stall_at,
                              {"kind": "lease.stalled", "task": task, "agent": agent},
                              channel=PROJECT_KEY)
        else:
            realtime.cancel_scheduled(HUB_DIR, "lease-stall:" + task)
        if expires > now:
            realtime.schedule(HUB_DIR, "lease-expiry:" + task, expires,
                              {"kind": "lease.expired", "task": task, "agent": agent},
                              channel=PROJECT_KEY)
        else:
            realtime.cancel_scheduled(HUB_DIR, "lease-expiry:" + task)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Hub lease truth timer could not be scheduled")


def maintain_action():
    """The navbar's "Update Core Systems" link — the adopter's MANUAL repair pass for a machine
    whose own update loop has not converged (a runbook page, a deep link that opens an agent
    session, a pipeline trigger). HUB_MAINTAIN_URL enables it; HUB_MAINTAIN_LABEL renames it.
    A script-bearing scheme is refused: the value lands in an href on every board."""
    url = str(_dj_setting("HUB_MAINTAIN_URL") or os.environ.get("HUB_MAINTAIN_URL") or "").strip()
    scheme = url.split(":", 1)[0].lower() if ":" in url.split("/", 1)[0] else ""
    if not url or scheme in ("javascript", "data", "vbscript"):
        return None
    label = str(_dj_setting("HUB_MAINTAIN_LABEL") or os.environ.get("HUB_MAINTAIN_LABEL")
                or "Update Core Systems").strip()[:40]
    return {"url": url[:500], "label": label}


def worker_launch_enabled() -> bool:
    """Whether this deployment intentionally exposes its optional local-worker launch bridge."""
    value = _dj_setting("HUB_WORKER_LAUNCH_ENABLED", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def worker_protocol() -> str:
    """Return a syntactically safe custom URL scheme (the Windows adapter must use the same one)."""
    import re

    value = str(_dj_setting("HUB_WORKER_PROTOCOL", _IDENTITY["worker_scheme"])
                or _IDENTITY["worker_scheme"]).lower()
    return value if re.fullmatch(r"hub-[a-z0-9][a-z0-9+.-]{0,26}", value) else "hub-worker"


@functools.lru_cache(maxsize=1)
def registry():
    return hub_core.Registry.from_dir(SCHEMA_DIR)


def ledger_wait_s() -> float:
    """How long a request waits for the ledger lock before answering 503 busy.

    HUB_LEDGER_WAIT_S (setting or environment), default 30 s: long enough to outlast a full index
    rebuild, short enough to stay under common proxy read timeouts. Offline tools that construct
    their own EventStore keep the store's far-off ceiling."""
    raw = _dj_setting("HUB_LEDGER_WAIT_S") or os.environ.get("HUB_LEDGER_WAIT_S") or 30
    try:
        return max(1.0, min(float(raw), 600.0))
    except (TypeError, ValueError):
        return 30.0


def store():
    """A fresh EventStore handle per call (cheap; avoids cross-thread sqlite handles). Bounded by
    ledger_wait_s(): a lock held past it raises hub_core.store.StoreBusy, which the write seam
    and LedgerBusyMiddleware answer 503 + Retry-After — never a 500."""
    return hub_core.EventStore(HUB_DIR, lock_timeout=ledger_wait_s())


def current_state(st=None):
    """Fold the board, closing only a store opened by this helper."""
    if st is not None:
        return _project.state(st.events())
    owned = store()
    try:
        return _project.state(owned.events())
    finally:
        owned.close()


_GIT_HEAD = {"key": None, "sha": None, "at": 0.0}
_GIT_HEAD_TTL_S = 60.0       # only when there is no checkout whose refs can be watched


def _git_head_key():
    """What HEAD resolves through, as a cheap stat-only fingerprint — or None without a checkout.

    Walks up from WORK_ROOT to the ``.git`` git itself would find (a linked worktree's ``.git``
    is a file naming its private dir; ``commondir`` names the shared one), then stamps HEAD's
    text plus the mtime of every place the named ref can live (loose ref in either dir,
    packed-refs). A commit, checkout, reset or pull rewrites one of those, so the memo can never
    serve a HEAD that has moved."""
    import os as _os
    try:
        here = Path(WORK_ROOT).resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        dot = d / ".git"
        if dot.is_dir():
            gitdir = dot
            break
        if dot.is_file():
            try:
                text = dot.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if not text.startswith("gitdir:"):
                return None
            gitdir = Path(text[7:].strip())
            if not gitdir.is_absolute():
                gitdir = (d / gitdir).resolve()
            break
    else:
        return None
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    common = gitdir
    try:
        rel = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        common = (gitdir / rel).resolve() if not _os.path.isabs(rel) else Path(rel)
    except OSError:
        pass
    stamps = []
    if head.startswith("ref:"):
        ref = head[4:].strip()
        for base in (gitdir, common):
            for path in (base / ref, base / "packed-refs"):
                try:
                    stamps.append(path.stat().st_mtime_ns)
                except OSError:
                    stamps.append(0)
    return (str(gitdir), head, tuple(stamps))


def _git_head():
    """Return the running code identity in every deployment shape.

    A source checkout can ask Git directly. A production image normally contains no ``.git``;
    there the pre-build stamp is the artifact's own identity and is the value that must ride on
    Hub mutations and discovery metadata.

    MEMOIZED on what HEAD resolves through (``_git_head_key``). Every snapshot keys on the head
    and every ledger write stamps it, and a ``git rev-parse`` subprocess per call was measured as
    the dominant cost of a snapshot build (two spawns, ~0.5 s of a ~0.55 s build on Windows).
    The refs are stat()ed instead, so a commit or checkout is still seen on the next read. With
    no watchable checkout the answer is remembered for a minute.
    """
    key = _git_head_key()
    now = time.time()
    memo = _GIT_HEAD
    if memo["sha"] is not None:
        if key is not None and memo["key"] == key:
            return memo["sha"]
        if key is None and memo["key"] is None and now - float(memo["at"]) < _GIT_HEAD_TTL_S:
            return memo["sha"]
    try:
        r = subprocess.run(["git", "-C", str(WORK_ROOT), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=4)
        head = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        head = ""
    sha = head or _running_sha()
    if sha:
        memo.update(key=key, sha=sha, at=now)
    return sha


def entity_from_store(st, eid: str) -> dict:
    """ONE entity, folded from its own aggregate's events only.

    A write needs the entity it is about to update (to merge, validate and check its version)
    and nothing else; folding the whole ledger for it made every append cost a full replay, and
    every append moves the head, so a burst of writes paid one full fold each. The fold of an
    aggregate's own events is exactly that entity's row in the full fold (payloads merge per
    aggregate, never across aggregates)."""
    events = st.events(aggregate=eid)
    return dict((_project.fold(events) or {}).get(eid, {})) if events else {}


def entity(eid: str) -> dict:
    """``entity_from_store`` over a store this helper opens and always closes."""
    owned = store()
    try:
        return entity_from_store(owned, eid)
    finally:
        owned.close()


def git_is_ancestor(sha, deployed):
    """The VCS ancestry seam deploy-driven task closure asks: is commit ``sha`` contained in
    build ``deployed``? True/False when the repository at WORK_ROOT can answer, None when it
    cannot (no git, a shallow or foreign clone, an unknown object) — which the caller reports as
    UNCHECKED, never as "no". An adopter whose production image carries no repository points
    HUB_VCS_ANCESTRY at "none" (always None) or wires its own forge API in place of this."""
    if str(_dj_setting("HUB_VCS_ANCESTRY", os.environ.get("HUB_VCS_ANCESTRY", "git"))).lower() == "none":
        return None
    try:
        r = subprocess.run(["git", "-C", str(WORK_ROOT), "merge-base", "--is-ancestor",
                            str(sha), str(deployed)], capture_output=True, timeout=10)
    except Exception:                                        # noqa: BLE001
        return None
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None


def _build_stamp_path() -> Path:
    return BASE_DIR / _dj_setting("HUB_BUILD_STAMP", "build_sha.txt")


def _normalize_build_sha(value):
    """Canonical raw Git identity, or ``None`` for a mutable/non-revision value."""
    import re

    value = str(value or "").strip().lower()
    if value.startswith("build-"):
        value = value[6:]
    return value if re.fullmatch(r"[0-9a-f]{7,64}", value) else None


def _running_sha():
    """The immutable identity carried by the RUNNING artifact, even when ``.git`` is absent.

    An explicit Hub override wins. The pre-build stamp is next because adopters commonly bake a
    short SHA while buildpacks expose the same revision as a full ``SOURCE_VERSION``; choosing the
    stamp keeps it directly comparable to the deploy proof. ``SOURCE_VERSION`` remains the
    zero-file fallback for platforms that inject the revision themselves.
    """
    for value in (_dj_setting("HUB_BUILD_SHA"), os.environ.get("HUB_BUILD_SHA")):
        revision = _normalize_build_sha(value)
        if revision:
            return revision
    try:
        stamped = _normalize_build_sha(_build_stamp_path().read_text(encoding="utf-8"))
    except OSError:
        stamped = None
    return stamped or _normalize_build_sha(os.environ.get("SOURCE_VERSION"))


def _state_json() -> dict:
    p = PROJECT / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def build_meta(served=None, state=None) -> dict:
    """The build/coherence block for /hub.json. ``coherent`` is always computed.

    Runtime ``PROJECT/state.json`` remains a useful deploy-side shortcut, but it is baked before a
    release while an immutable deploy entity is written after the public canary. Prefer a valid
    closure matching the running artifact, then an already-matching state shortcut, then the newest
    valid closure. A stale mutable file must never mask stronger post-canary evidence.
    """
    sj = _state_json()
    head = _git_head()  # Git checkout or, in a production artifact, the baked build stamp.
    state_sha = _normalize_build_sha(sj.get("last_deploy_sha"))
    served_sha = _normalize_build_sha(served) if served is not None else None
    sha = state_sha
    release = None
    candidates = []
    if state is not None:
        for deploy in state.get("by_type", {}).get("deploy", []):
            shipped = _normalize_build_sha(deploy.get("sha"))
            observed = _normalize_build_sha(deploy.get("served_sha"))
            if (shipped and observed == shipped and
                    isinstance(deploy.get("tasks_closed"), list)):
                candidates.append(deploy)
    matching = [row for row in candidates if _normalize_build_sha(row.get("sha")) == head]
    if matching:
        release = max(matching, key=lambda row: str(row.get("at") or ""))
        sha = _normalize_build_sha(release.get("sha"))
    elif state_sha and state_sha == head:
        sha = state_sha
    elif candidates:
        release = max(candidates, key=lambda row: str(row.get("at") or ""))
        sha = _normalize_build_sha(release.get("sha"))
    coherent = bool(head and sha and head == sha and (served is None or served_sha == head))
    release_source = "deploy entity" if release else ("state.json" if state_sha else None)
    return {
        "repo": sj.get("repo_build"),
        "deploy": (release or {}).get("build") or sj.get("last_deploy_build"),
        "tag": sj.get("last_deploy_tag"), "sha": sha, "served_sha": served_sha, "head": head,
        "coherent": coherent, "live_url": sj.get("live_url") or _IDENTITY.get("app_host"),
        "release_source": release_source,
    }


# ---- behavioral audit adapters (the CHARTER security gate, AST not regex) ----

def _sv(vid, invariant, observed, expected="prod-safe default", remediation="require the env var; no unsafe default"):
    return {"id": vid, "kind": "ast", "severity": "high", "status": "open", "invariant": invariant,
            "observed": observed, "expected": expected, "evidence_uri": "", "remediation": remediation,
            "autofix_allowed": False}


def _call_default(value):
    """The literal 2nd arg of an env(...)/env_bool(...) call (the default), else None."""
    if isinstance(value, ast.Call) and len(value.args) >= 2:
        try:
            return ast.literal_eval(value.args[1])
        except Exception:
            return None
    return None


def _settings_file():
    """The settings.py the AST audit scans: HUB_SETTINGS_FILE, else the DJANGO_SETTINGS_MODULE
    file, else the structurally resolved site package (the one top-level package holding both
    settings.py and wsgi.py). The structural resort keeps a seam the environment never reached
    honest instead of raising a violation against a repo whose layout is perfectly unambiguous;
    a genuinely ambiguous tree still fails closed."""
    p = _dj_setting("HUB_SETTINGS_FILE")
    if p:
        return Path(p)
    mod = os.environ.get("DJANGO_SETTINGS_MODULE")
    if mod:
        try:
            import importlib
            f = importlib.import_module(mod).__file__
            return Path(f) if f else None
        except Exception:
            return None
    try:
        from hub_core import site_package as _site_package
        return _site_package.settings_file(WORK_ROOT)
    except Exception:
        return None


def settings_ast_adapter(state):
    """AST-scan the project settings.py for prod-unsafe defaults (DEBUG/SECRET_KEY/ALLOWED_HOSTS).
    Fail-closed: an unlocatable/unparseable settings file is a violation, never a silent skip."""
    sp = _settings_file()
    if sp is None:
        return [_sv("settings:locate", "the Django settings file is locatable",
                    "neither HUB_SETTINGS_FILE nor DJANGO_SETTINGS_MODULE resolves to a file",
                    "locatable", "set HUB_SETTINGS_FILE in settings")]
    viols = []
    try:
        tree = ast.parse(sp.read_text(encoding="utf-8"))
    except Exception as e:
        return [_sv("settings:parse", "settings.py parses", str(e), "parseable", "fix the syntax error")]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not node.targets:
            continue
        name = getattr(node.targets[0], "id", None)
        if name == "DEBUG" and _call_default(node.value) is True:
            viols.append(_sv("settings:debug", "DEBUG default is False", "DEBUG defaults to True"))
        elif name == "SECRET_KEY":
            d = _call_default(node.value)
            if isinstance(d, str) and d:
                viols.append(_sv("settings:secret_key", "SECRET_KEY has NO literal fallback",
                                 f"literal default {d[:18]!r}...", remediation="SECRET_KEY=os.environ['SECRET_KEY'] (no default)"))
        elif name == "ALLOWED_HOSTS":
            try:
                src = ast.unparse(node.value)
            except Exception:
                src = ""
            if '"*"' in src or "'*'" in src:
                viols.append(_sv("settings:allowed_hosts", "ALLOWED_HOSTS default is not '*'", "defaults to '*'"))
    return viols


def _is_hub_view(callback):
    """True when a URL callback is one of this adapter package's own views."""
    module = str(getattr(callback, "__module__", "") or "")
    return module.split(".")[0] == __name__.split(".")[0] and module != __name__


def route_guard_adapter(state):
    """Auth-boundary primitive: assert every mutating route has an explicit gate.

    General writes carry ``@writer`` with a named operation scope. The one deliberately narrow
    browser capability may instead carry ``@csrf_protect`` plus ``_hub_origin_gated``; it can only
    mint a short-lived launch grant and never receives general write authority. The CI ingest,
    whose sender is a CI system rather than an agent, carries ``_hub_secret_gated``: it refuses
    everything without a configured webhook secret and can only add or retire CI rows.
    """
    try:
        from django.urls import get_resolver
        resolver = get_resolver()
    except Exception:
        return []
    viols = []

    def walk(patterns, prefix=""):
        for p in patterns:
            pat = prefix + str(getattr(p, "pattern", ""))
            sub = getattr(p, "url_patterns", None)
            if sub is not None:
                walk(sub, pat)
            elif "hub/api/" not in pat and (pat.startswith("hub/")
                                             or _is_hub_view(getattr(p, "callback", None))):
                cb = getattr(p, "callback", None)
                # Every non-mutation Hub route is a READ of the projected board and must carry
                # the read gate, so a public board is a declared setting, never a route that
                # was simply added without one. Discovery documents (the agent card) declare
                # themselves public explicitly.
                if _is_hub_view(cb) and not (getattr(cb, "_hub_read_gated", False)
                                             or getattr(cb, "_hub_public_discovery", False)):
                    viols.append(_sv("routes:read-ungated", "every Hub read route carries the read gate",
                                     "%s -> %s is not wrapped by read_auth.reader" %
                                     (pat, getattr(cb, "__name__", "?")),
                                     "read_auth.reader(view)",
                                     remediation="wrap the read view with read_auth.reader in urls.py"))
                # Every read route DECLARES who may see it (open | veiled | member). An
                # undeclared route serves no narrowed reader, and a new one must not slip in
                # without somebody deciding — the veil is only as strong as its coverage.
                if pat.startswith("hub/") and getattr(cb, "_hub_visibility", None) not in (
                        "open", "veiled", "member"):
                    viols.append(_sv("routes:undeclared-visibility",
                                     "every /hub read route declares its visibility",
                                     "%s -> %s declares none" % (pat, getattr(cb, "__name__", "?")),
                                     "open | veiled | member",
                                     remediation="add the route to urls.VISIBILITY"))
            elif "hub/api/" in pat:
                cb = getattr(p, "callback", None)
                guarded = (getattr(cb, "_hub_token_gated", False) or getattr(cb, "_hub_origin_gated", False)
                           or getattr(cb, "_hub_secret_gated", False))
                if not guarded:
                    viols.append(_sv("routes:unguarded", "every /hub/api/ route has an explicit gate",
                                     "%s -> %s is not token-, origin- or secret-gated" %
                                     (pat, getattr(cb, "__name__", "?")),
                                     "@writer or narrow @csrf_protect capability",
                                     remediation="wrap general writes with @writer"))
                elif (getattr(cb, "_hub_token_gated", False) and
                      not getattr(cb, "_hub_required_scope", None)):
                    viols.append(_sv("routes:unscoped", "every token-gated /hub/api/ route names a scope",
                                     "%s -> %s has no operation scope" %
                                     (pat, getattr(cb, "__name__", "?")),
                                     "@writer(scope='operation:name')",
                                     remediation="assign the least-privilege operation scope"))
    try:
        walk(resolver.url_patterns)
    except Exception as e:
        return [_sv("routes:introspect", "URLConf is walkable", str(e), "walkable")]
    return viols


def read_posture_adapter(state):
    """A public board in production is a decision the audit keeps visible, not a default.

    Reads are authenticated unless ``HUB_READ_AUTH = "public"``. Under DEBUG a public board is
    the local preview and says nothing; outside DEBUG it is one high finding naming the setting,
    because the board projects every question, error and assignment it holds."""
    from . import read_auth
    if read_auth.mode() != read_auth.PUBLIC or _dj_setting("DEBUG", False):
        return []
    return [_sv("routes:public-read", "Hub reads require an authenticated principal in production",
                "HUB_READ_AUTH='public' with DEBUG off: every board read is anonymous",
                "HUB_READ_AUTH unset (required)",
                remediation="remove HUB_READ_AUTH='public', or keep it only for a board whose "
                            "entire content is deliberately public")]


def identity_settings_adapter(state):
    """One project must present one entity namespace at every discovery and mutation edge."""
    configured = str(PROJECT_KEY or "").strip().lower()
    portable = str(_IDENTITY.get("key") or "").strip().lower()
    if configured == portable:
        return []
    return [_sv(
        "identity:project-key-mismatch",
        "Django HUB_PROJECT_KEY matches PROJECT/project.json key",
        "HUB_PROJECT_KEY=%r but portable identity key=%r" % (configured, portable),
        "one identical project key",
        "align HUB_PROJECT_KEY with PROJECT/project.json before accepting another mutation",
    )]


def storage_runtime_adapter(state):
    """A production board cannot be healthy on implicit or unwritable runtime storage."""
    if _dj_setting("DEBUG", False):
        return []
    info = storage_info()
    violations = []
    if not HUB_DIR_CONFIGURED:
        violations.append(_sv(
            "storage:implicit-hub-dir",
            "production HUB_DIR is explicitly configured to the durable runtime mount",
            "HUB_DIR fell back inside the application tree",
            "an explicit durable mounted HUB_DIR",
            "set HUB_DIR to the deployment's persistent volume before accepting live work",
        ))
    if not info["writable"]:
        violations.append(_sv(
            "storage:hub-dir-unwritable",
            "the Hub service account can write its configured runtime ledger",
            "the configured HUB_DIR or an existing ledger file is not writable",
            "writable runtime storage",
            "repair the mounted directory ownership/permissions before accepting live work",
        ))
    return violations


# (base, head) -> paths changed between two commits, memoized: both ends are immutable, so the
# answer cannot change, and a warm audit must not pay a subprocess. None means the range could not
# be read (a shallow clone, an unfetched sha) — UNKNOWN, never "empty".
_RANGE_TOUCH_CACHE = {}


def _range_touched_paths(base, head):
    key = (base, head)
    if key in _RANGE_TOUCH_CACHE:
        return _RANGE_TOUCH_CACHE[key]
    try:
        r = subprocess.run(["git", "-C", str(WORK_ROOT), "diff", "--name-only", f"{base}..{head}"],
                           capture_output=True, text=True, timeout=15)
        val = None if r.returncode != 0 else tuple(sorted(
            p.strip().replace("\\", "/") for p in (r.stdout or "").splitlines() if p.strip()))
    except Exception:
        val = None
    _RANGE_TOUCH_CACHE[key] = val
    return val


def _compatibility_baselines():
    """Read immutable adopter cutoffs; absent/invalid records enable no exception."""
    try:
        manifest = json.loads((WORK_ROOT / "hub-scaffold-adoption.json").read_text(encoding="utf-8"))
        compatibility = manifest.get("compatibility") or {}
        receipt = compatibility.get("legacy_done_receipts")
        entity_schema = compatibility.get("legacy_entity_schema")
        return (
            receipt if isinstance(receipt, dict) else None,
            entity_schema if isinstance(entity_schema, dict) else None,
        )
    except (OSError, ValueError, TypeError):
        return None, None


def _run_audit_with_store(s, served=None) -> dict:
    state = current_state(s)
    bm = build_meta(served, state=state)
    coh = {"head": bm["head"], "sha": bm["sha"], "served": bm["served_sha"]}
    # DEPLOY BOOKKEEPING IS NOT DRIFT. A deploy records its own sha in the state file AFTER the
    # canary passes, so HEAD sits one commit past the shipped sha from then until the next deploy.
    # coherence:repo demanded exact equality, which made it permanently high — reachable-green only
    # in the instant between shipping and recording, and a red nobody can clear is how a board
    # teaches its readers to ignore reds. The audit quiets it ONLY when the whole delta is that
    # bookkeeping file; supply the delta so it can tell. A None answer is UNKNOWN and still fires.
    if bm["head"] and bm["sha"] and bm["head"] != bm["sha"]:
        coh["delta_paths"] = _range_touched_paths(bm["sha"], bm["head"])
    # Unknowable coherence must SAY SO — a None head/sha silently skipping the checks is the
    # vacuous-green failure mode (audit green while the running identity is unmeasured).
    if not bm["head"]:
        coh["unknown"] = ("running build identity unknown (no .git and no %s)"
                          % _dj_setting("HUB_BUILD_STAMP", "build_sha.txt"))
        if _dj_setting("DEBUG", False):
            # A dev checkout without a build stamp is like pre-first-deploy: visible amber,
            # but it must not block local work. In prod it stays a blocking violation.
            coh["unknown_severity"] = "warn"
    elif not bm["sha"]:
        # Pre-first-deploy is a legitimate state: visible, but it must not block the very deploy
        # that creates the record.
        coh["unknown"] = ("no deploy record yet (neither PROJECT/state.json last_deploy_sha nor "
                          "a coherent immutable deploy entity is present)")
        coh["unknown_severity"] = "warn"
    receipt_baseline, entity_schema_baseline = _compatibility_baselines()
    return _audit.audit(state, registry(), store=s, coherence=coh,
                        legacy_receipt_baseline=receipt_baseline,
                        legacy_entity_schema_baseline=entity_schema_baseline,
                        adapters=[settings_ast_adapter, identity_settings_adapter,
                                  storage_runtime_adapter,
                                  route_guard_adapter, read_posture_adapter])


def run_audit(st=None, served=None) -> dict:
    """Audit the board while preserving ownership of a caller-provided store."""
    if st is not None:
        return _run_audit_with_store(st, served=served)
    owned = store()
    try:
        return _run_audit_with_store(owned, served=served)
    finally:
        owned.close()


# ---- agent claims: a lease + fencing token so exactly one agent owns a task ----
import os as _os
import time as _time
import uuid as _uuid
from hub_core import atomic
from hub_core.process_lock import ProcessFileLock

CLAIMS = HUB_DIR / "claims"


def _claim_path(task_id):
    return CLAIMS / (task_id.replace(":", "_") + ".json")


def _read_lease(task_id):
    p = _claim_path(task_id)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_lease(task_id, lease):
    CLAIMS.mkdir(parents=True, exist_ok=True)
    # Locked, retrying replace: every board render reads these files with no lock, so on
    # Windows a render landing mid-rename used to turn a claim into a 500 (hub_core.atomic).
    atomic.write_json(_claim_path(task_id), lease)


def commit_resolver():
    """Where commit questions are put: this Hub's own repository (WORK_ROOT), each configured
    project's checkout (``HUB_PROJECT_REPOS = {"budget-app": "/srv/checkouts/budget-app"}``),
    and an optional remote resolver (``HUB_COMMIT_RESOLVER = "package.module:function"``, called
    as ``function(project, sha) -> True | False | None``) for a project with no local checkout.

    A resolver that cannot be imported is "could not ask", never a crash and never a "no"."""
    from hub_core import commits
    repos = _dj_setting("HUB_PROJECT_REPOS", None) or {}
    if not isinstance(repos, dict):
        repos = {}
    resolved = {}
    for key, path in repos.items():
        p = Path(str(path))
        resolved[str(key)] = p if p.is_absolute() else BASE_DIR / p
    remote = None
    dotted = str(_dj_setting("HUB_COMMIT_RESOLVER", "") or "").strip()
    if dotted:
        module_name, _, attr = dotted.partition(":")
        try:
            import importlib
            remote = getattr(importlib.import_module(module_name), attr)
        except Exception:                                    # noqa: BLE001

            def remote(project, sha):                        # noqa: ARG001 - an honest "unknown"
                return None
    return commits.Resolver(WORK_ROOT, resolved, remote)


def gone_grace_s():
    """How long a provably GONE console keeps what it held (``HUB_GONE_GRACE_S``, default
    hub_core.liveness.GONE_GRACE_S). One number for every claim kind, so the board, the sweep,
    the task claim and the item claim can never disagree about when a claim frees."""
    from hub_core import liveness
    try:
        return max(60, int(_dj_setting("HUB_GONE_GRACE_S", liveness.GONE_GRACE_S)))
    except (TypeError, ValueError):
        return liveness.GONE_GRACE_S


def lease_verdict(lease, roster_=None, now=None) -> dict:
    """``{state, gone_s, frees_in_s, released}`` for one lease's holder (hub_core.liveness):
    released is True only when its console is provably GONE past ``gone_grace_s()``. Fails
    closed to UNPROVABLE -- which releases nothing -- when presence cannot be read."""
    from hub_core import liveness
    lease = lease or {}
    try:
        roster_ = roster_ if roster_ is not None else roster()
    except Exception:                                        # noqa: BLE001 - unprovable, not gone
        roster_ = None
    return liveness.verdict(roster_, lease.get("session"), lease.get("machine") or "",
                            floor=float(lease.get("last_heartbeat") or lease.get("claimed") or 0),
                            now=now, grace_s=gone_grace_s())


def roster():
    """Who is live on this board right now, with its own completeness (hub_core.liveness)."""
    from hub_core import liveness
    return liveness.resolve(HUB_DIR)


def claim(task_id, agent, ttl_s=900, *, auth_subject=None, credential_id=None,
          actor_kind=None, session="", machine=""):
    """Grant or renew the fenced lease on one task.

    The result carries ``created``: whether THIS call brought the lease into existence. A caller
    whose follow-up step fails may tear down only a lease it created — a renewal's token belongs
    to work already in flight (often the same worker's earlier request whose response timed
    out), and releasing it strands a worker that did claim.

    ``session`` names the CONSOLE that claimed, not just the agent. With several live consoles
    per agent the board otherwise cannot tell which window holds the work and has to infer it
    from a directory — and anyone who reads a repository then looks like its owner.
    """
    session = str(session or "").strip()[:64]
    machine = str(machine or "").strip().lower()[:120]
    with ProcessFileLock(CLAIMS, name=".claims.lock", timeout=30):
        now = _time.time()
        cur = _read_lease(task_id)
        took_over = None
        if (cur and cur.get("expires", 0) > now and (cur.get("session") or cur.get("machine"))
                and (str(cur.get("session") or "") != session or cur.get("agent") != agent)):
            # A lease whose console is provably GONE past its grace -- or whose heartbeating
            # machine has gone silent (liveness.MACHINE_SILENT_S) -- is released here exactly as
            # the sweep would release it, so a claim never waits on a sweep that has not run yet.
            v = lease_verdict(cur, now=now)
            if v["released"]:
                took_over = {"session": cur.get("session"), "machine": cur.get("machine"),
                             "agent": cur.get("agent"), "claimed": cur.get("claimed"),
                             "gone_s": v.get("gone_s"), "ended": bool(v.get("ended")),
                             "machine_silent": bool(v.get("machine_silent")),
                             "at": now}
                cur = None
        if cur and cur.get("expires", 0) > now:
            if (cur.get("agent") != agent or
                    (cur.get("auth_subject") and cur.get("auth_subject") != auth_subject) or
                    (cur.get("credential_id") and cur.get("credential_id") != credential_id)):
                refusal = {"ok": False, "reason": "held", "held_by": cur.get("agent"),
                           "expires": cur.get("expires")}
                lost = taken_over_notice(cur, agent, session)
                if lost:
                    # The console whose lease was taken over is back (its step/finish re-claims
                    # first): say WHY it lost the task, so it stops instead of racing the holder.
                    refusal.update(lost)
                if cur.get("session"):
                    # Say WHEN it frees: a gone holder's lease is released by the sweep once its
                    # grace runs out, well before the clock; a live one only by the clock.
                    from hub_core import liveness
                    v = liveness.holder(roster(), cur, now, grace_s=gone_grace_s())
                    refusal.update({"held_by_session": cur.get("session"),
                                    "holder_state": v["holder_state"],
                                    "holder_gone_s": v["holder_gone_s"],
                                    "frees_in_s": (v["holder_frees_in_s"]
                                                   if v["holder_state"] == liveness.GONE
                                                   else max(0, int(cur.get("expires", now) - now)))})
                return refusal
            # ONE AGENT'S CONSOLES ARE NOT ONE WORKER. The checks above are per agent/credential,
            # so a second console of the same agent used to renew this lease in place, receive
            # its fencing token, and close work the first console was still doing. A renewal
            # from a DIFFERENT console is refused while the recorded one is provably LIVE. When
            # liveness cannot be established (presence unreadable, the holder's machine quiet)
            # the renewal still goes through -- refusing there would strand work whose holder
            # really is gone -- but it never rewrites the recorded holder, because every
            # surface that says who is on this task keys on it.
            holder = str(cur.get("session") or "")
            if session and holder and session != holder:
                from hub_core import liveness
                roster_ = roster()
                state, _seen = roster_.state(holder, cur.get("machine") or "")
                ended = roster_.is_ended(holder)
                # A FRESH lease is held even before its console reaches the roster: a new
                # console's first presence row can land minutes after its first claim, and a
                # second console of the same agent "renewing" in that gap used to receive the
                # token and close work that had just begun.
                touched = max(float(cur.get("claimed") or 0), float(cur.get("last_heartbeat") or 0))
                fresh = (now - touched) < liveness.FRESH_LEASE_S
                if not ended and (state == liveness.LIVE or fresh):
                    refusal = {"ok": False, "reason": "held_by_console",
                               "held_by": cur.get("agent"), "held_by_session": holder,
                               "held_by_machine": cur.get("machine") or None,
                               "expires": cur.get("expires")}
                    refusal.update(taken_over_notice(cur, agent, session) or {})
                    return refusal
                # The recorded console ENDED, is provably GONE, or went stale unseen: this console
                # takes the task over AS ITSELF. The record names the console actually on it,
                # the takeover is on record, and the fencing token ROTATES, so the old console
                # can never complete over this one (it used to keep the old holder's name and
                # hand this caller the same token).
                cur["took_over_from"] = {"session": holder, "agent": cur.get("agent"),
                                         "machine": cur.get("machine") or None,
                                         "claimed": cur.get("claimed"), "state": state,
                                         "ended": ended, "at": now}
                cur["token"] = _uuid.uuid4().hex
                cur["session"] = session
                if machine:
                    cur["machine"] = machine
                holder = session
                took_over = cur["took_over_from"]
            if session and not holder:
                cur["session"] = session
            if machine and not cur.get("machine"):
                cur["machine"] = machine
            # Retrying the same claim must not silently invalidate the fencing token already held
            # by this worker. Renew the lease in place and return that same token.
            if not cur.get("auth_subject"):
                cur["auth_subject"] = auth_subject
                cur["credential_id"] = credential_id
                cur["actor_kind"] = actor_kind
            cur["last_heartbeat"] = now
            cur["expires"] = now + ttl_s
            if session:
                cur["session"] = session
            _write_lease(task_id, cur)
            _publish_realtime("lease.heartbeat", task=task_id, agent=agent,
                              expires=cur["expires"])
            _schedule_lease_truth(cur)
            return {"ok": True, "created": False, "took_over": bool(took_over),
                    "heartbeat_after_s": max(1, ttl_s // 3), **cur}
        # The CLAIMING CONSOLE rides the lease: with several consoles of one agent live, the
        # session is the only fact that says which of them took responsibility — a claim must
        # never be inferred from the directory a console happens to stand in.
        lease = {"task": task_id, "agent": agent, "token": _uuid.uuid4().hex,
                 "auth_subject": auth_subject, "credential_id": credential_id,
                 "actor_kind": actor_kind,
                 "session": str(session or "")[:64], "machine": str(machine or "").lower()[:120],
                 "claimed": now, "last_heartbeat": now, "expires": now + ttl_s}
        # The console and machine that took it: the one record that survives the holder's
        # disappearance, so liveness can later say LIVE / GONE / UNPROVABLE about it.
        if session:
            lease["session"] = session
        if machine:
            lease["machine"] = machine
        if took_over:
            lease["took_over_from"] = took_over
        _write_lease(task_id, lease)
        _publish_realtime("lease.claimed", task=task_id, agent=agent,
                          expires=lease["expires"])
        _schedule_lease_truth(lease)
        return {"ok": True, "created": True, "took_over": bool(took_over),
                "heartbeat_after_s": max(1, ttl_s // 3), **lease}


def taken_over_notice(lease, agent, session="") -> dict:
    """The refusal fields a console gets when the lease it held was TAKEN OVER while it was away:
    who holds it now, when and why, and what to do -- so a console that went quiet (a dropped
    network, a sleep) and came back stops instead of pushing over the new holder. Empty when this
    caller is not the one the lease was taken from."""
    prev = (lease or {}).get("took_over_from")
    if not isinstance(prev, dict):
        return {}
    same_console = bool(session) and str(prev.get("session") or "") == str(session)
    same_agent = str(prev.get("agent") or "") == str(agent or "") and not session
    if not (same_console or same_agent):
        return {}
    why = ("the console holding it had ended" if prev.get("ended") else
           "its machine had been silent %s s" % (prev.get("gone_s") or "?")
           if prev.get("machine_silent") else
           "its console was %s" % (prev.get("state") or "gone"))
    return {"reason": "taken_over", "held_by": lease.get("agent"),
            "held_by_session": lease.get("session") or None,
            "held_by_machine": lease.get("machine") or None,
            "taken_at": prev.get("at"),
            "msg": ("%s took this task over because %s; stop work on it and hand your changes to "
                    "%s (a message with your branch or sha) rather than pushing"
                    % (lease.get("agent"), why, lease.get("agent")))}


def leases(*, now=None, include_expired=False):
    """Read lease sidecars safely, newest claims first.

    A vanished/torn file is an absent lease, never a request-wide failure.  Heartbeat and claim
    timestamps remain separate so presence cannot masquerade as task progress.
    """
    now = _time.time() if now is None else float(now)
    rows = []
    try:
        paths = list(CLAIMS.glob("*.json"))
    except OSError:
        return rows
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if include_expired or row.get("expires", 0) > now:
            rows.append(row)
    rows.sort(key=lambda row: (row.get("claimed", 0), row.get("task", "")), reverse=True)
    return rows


def wip_status(active=None):
    """The configured, enforced WIP contract.

    Adaptive control is intentionally not claimed here: a controller is only real once its loss
    signals are wired.  The same setting feeds the claim gate and every read projection.
    """
    try:
        ceiling = int(_dj_setting("HUB_WIP_LIMIT", 8))
    except (TypeError, ValueError):
        ceiling = 8
    ceiling = max(1, min(256, ceiling))
    active = len(leases()) if active is None else int(active)
    return {"ceiling": ceiling, "active": active, "saturated": active >= ceiling,
            "source": "configured"}


def lease_valid(task_id, token):
    cur = _read_lease(task_id)
    return bool(cur and cur.get("token") == token and cur.get("expires", 0) > _time.time())


def lease_authorized(task_id, token, auth_subject, credential_id=None):
    """Require both the fencing token and the subject that acquired it.

    A legacy lease has no subject. Only the conspicuous shared-root migration actor may consume
    such a lease; the next fresh claim is always fully bound.
    """
    cur = _read_lease(task_id)
    if not cur or cur.get("token") != token or cur.get("expires", 0) <= _time.time():
        return False
    bound = cur.get("auth_subject")
    if bound and bound != auth_subject:
        return False
    if not bound and auth_subject != "shared-root":
        return False
    bound_credential = cur.get("credential_id")
    return not bound_credential or bound_credential == credential_id


def heartbeat(task_id, token, ttl_s=900, *, auth_subject=None, credential_id=None,
              actor_kind=None):
    with ProcessFileLock(CLAIMS, name=".claims.lock", timeout=30):
        cur = _read_lease(task_id)
        if (not cur or cur.get("token") != token or cur.get("expires", 0) <= _time.time() or
                (cur.get("auth_subject") and cur.get("auth_subject") != auth_subject) or
                (cur.get("credential_id") and cur.get("credential_id") != credential_id) or
                (not cur.get("auth_subject") and auth_subject != "shared-root")):
            return {"ok": False, "reason": "no/stale lease"}
        now = _time.time()
        if not cur.get("auth_subject"):
            cur["auth_subject"] = auth_subject
            cur["credential_id"] = credential_id
            cur["actor_kind"] = actor_kind
        cur["last_heartbeat"] = now
        cur["expires"] = now + ttl_s
        _write_lease(task_id, cur)
        _publish_realtime("lease.heartbeat", task=task_id, agent=cur.get("agent"),
                          expires=cur["expires"])
        _schedule_lease_truth(cur)
        return {"ok": True, "expires": cur["expires"], "last_heartbeat": now,
                "heartbeat_after_s": max(1, ttl_s // 3)}


def void_lease(task_id, token, why="") -> bool:
    """Expire exactly the lease named by its fencing token, in place, recording why. Used when
    its holder's console is provably gone (hub_core.lease_sweep): the record stays readable, the
    token stops fencing, and a successor's lease is never touched."""
    with ProcessFileLock(CLAIMS, name=".claims.lock", timeout=30):
        cur = _read_lease(task_id)
        if not cur or not token or cur.get("token") != token:
            return False
        now = _time.time()
        cur["expires"] = min(float(cur.get("expires") or now), now)
        cur["voided_at"] = now
        cur["voided_why"] = str(why or "")[:300]
        _write_lease(task_id, cur)
    _publish_realtime("lease.voided", task=task_id, agent=cur.get("agent"))
    return True


def release_lease(task_id, token) -> bool:
    """Remove exactly the lease named by its fencing token; never release a successor's claim."""
    with ProcessFileLock(CLAIMS, name=".claims.lock", timeout=30):
        cur = _read_lease(task_id)
        if not cur or cur.get("token") != token:
            return False
        try:
            _claim_path(task_id).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Completion is already durable at this point. If Windows temporarily holds the
            # sidecar open, expire it in place so it cannot fence a successor or surface as live.
            cur["expires"] = 0
            try:
                _write_lease(task_id, cur)
            except OSError:
                return False
        _publish_realtime("lease.released", task=task_id, agent=cur.get("agent"), expires=0)
        try:
            from . import realtime
            realtime.cancel_scheduled(HUB_DIR, "lease-stall:" + task_id)
            realtime.cancel_scheduled(HUB_DIR, "lease-expiry:" + task_id)
        except Exception:
            pass
        return True


# ---- observed presence: who is on this board, per (agent, machine, console) ----
# hub_core.presence over HUB_DIR. Observed state like claims/, never ledger truth: rows are
# written from the authenticated write seam (an unauthenticated caller can never forge a seat)
# and from the presence heartbeat, and they self-retire when a seat stops reporting.
from hub_core import presence as _presence

_PRESENCE_PUBLISH = {"stamp": None, "at": 0.0}


#: Optional session headers a client may send on any write, mapped to presence session fields.
_SESSION_HEADERS = {"X-Hub-Project": "project", "X-Hub-Files": "files",
                    "X-Hub-Session-Kind": "kind", "X-Hub-Run": "run", "X-Hub-Subject": "subject",
                    "X-Hub-Runtime": "runtime"}


def _presence_files(header, project="", repo=""):
    """X-Hub-Files as `<project>/<path>` tokens (hub_core.presence.parse_files). A bare file
    name is qualified with the console's own project (X-Hub-Project, else the repository's
    last path segment) when one is known, so a console that reports plain names keeps its list
    while two consoles in different repos are never paired on one bare name."""
    project = str(project or "").strip().strip("/")
    if not project and repo:
        project = str(repo).strip().rstrip("/").replace(":", "/").rsplit("/", 1)[-1]
        if project.endswith(".git"):
            project = project[:-4]
    tokens = []
    for part in str(header or "").replace(";", ",").split(","):
        part = part.strip().replace(chr(92), "/").strip("/")
        if not part:
            continue
        if "/" not in part and project:
            part = project + "/" + part
        tokens.append(part)
    return _presence.parse_files(",".join(tokens))


def observe_presence(agent, headers, *, heartbeat=False, extra=None):
    """Refresh the caller's presence row from optional X-Hub-* headers (and, from the presence
    ping, a body digest of what the session is doing), then wake connected cockpits — throttled,
    because presence rides every write and the wake-up plane must not carry one signal per
    request. X-Hub-Client-Version is the reporting client's own version, kept per machine so a
    seat on an older client than this hub serves is visible. Fail-soft end to end: presence must
    never break a write."""
    try:
        files_header = headers.get("X-Hub-Files")
        files = None if files_header is None else _presence_files(
            files_header, headers.get("X-Hub-Project") or "", headers.get("X-Hub-Repo") or "")
        fields = {name: headers.get(header) for header, name in _SESSION_HEADERS.items()
                  if headers.get(header) and name != "files"}
        fields.update(extra or {})
        unattended = headers.get("X-Hub-Unattended")
        _presence.observe(
            HUB_DIR, agent,
            machine=headers.get("X-Hub-Machine") or "",
            session=headers.get("X-Hub-Session") or "",
            cwd=headers.get("X-Hub-Cwd") or "",
            focus=headers.get("X-Hub-Focus") or "",
            name=headers.get("X-Hub-Console") or headers.get("X-Hub-Console-Name") or "",
            repo=headers.get("X-Hub-Repo") or "",
            app=headers.get("X-Hub-App") or "",
            state=headers.get("X-Hub-State") or "",
            runtime=headers.get("X-Hub-Runtime") or "",
            # A console's recently edited files. Absent header = no claim (keep the last one);
            # an empty header = "nothing edited recently" (clear it).
            files=files,
            retract_focus=headers.get("X-Hub-Focus-Retract") or "",
            heartbeat=heartbeat, extra=fields,
            # Kit telemetry: the client's own version+sha and the sha of every artifact the
            # seat runs. It is what separates a computer from a bare caller (is_kit_machine)
            # and what the distribution view grades. X-Hub-Client-Version is the digest the
            # stale-seat detector compares against the client this hub serves.
            client=headers.get("X-Hub-Client") or "",
            client_digest=headers.get("X-Hub-Client-Version") or "",
            artifacts=_presence.parse_artifacts(headers.get("X-Hub-Artifacts") or ""),
            # Crossover facts a client may send directly: the project the console stands in
            # and whether it is an unattended process nobody is reading.
            project=headers.get("X-Hub-Project") or "",
            unattended=(None if unattended in (None, "") else
                        str(unattended).strip().lower() in ("1", "true", "yes")),
            # The computer's own facts, graded by the distribution view: which interpreter the
            # client runs on, and whether the machine proved it can push. Absent = unreported.
            machine_facts={"python": headers.get("X-Hub-Python") or "",
                           "push": headers.get("X-Hub-Push") or ""})
        stamp = _presence.stamp(HUB_DIR)
        now = _time.time()
        if stamp != _PRESENCE_PUBLISH["stamp"] and now - _PRESENCE_PUBLISH["at"] >= 2.0:
            _PRESENCE_PUBLISH["stamp"] = stamp
            _PRESENCE_PUBLISH["at"] = now
            _publish_realtime("presence.observed")
    except Exception:                                        # noqa: BLE001
        pass


def read_presence():
    return _presence.read(HUB_DIR)


def read_presence_rows():
    """The RAW per-(agent, machine) rows, offline machines included (the live view ages them
    out, and an offline laptop is still its person's machine)."""
    return _presence.rows(HUB_DIR)


def is_kit_machine(row):
    return _presence.is_kit_machine(row)


# ---- distribution: is every seat running what this hub publishes? ----
from hub_core import distribution as _distribution


def distribution_files():
    """What this hub publishes, as {artifact: file}. Always the client it serves (the seats'
    `python -m hub_core.client`) and the charter core when the project ships one; plus any
    adopter file named in HUB_DISTRIBUTED_ARTIFACTS ({name: path}, relative to WORK_ROOT) —
    a seat reports the same name through HUB_ARTIFACTS."""
    import hub_core.client as _client_module
    files = {"client": Path(_client_module.__file__)}
    charter = WORK_ROOT / "CHARTER-CORE.md"
    if charter.exists():
        files["charter"] = charter
    extra = _dj_setting("HUB_DISTRIBUTED_ARTIFACTS") or {}
    if isinstance(extra, dict):
        for name, rel in extra.items():
            path = Path(rel)
            files[str(name).lower()] = path if path.is_absolute() else WORK_ROOT / path
    return files


def distribution_report(now=None):
    """(report, published) — the grade of every seat, fail-soft: a broken read is an empty
    report that SAYS it could not grade, never a 500 on the board."""
    try:
        pub = _distribution.published(distribution_files())
        report = _distribution.assess(read_presence_rows(), pub, now=now,
                                      is_kit=_presence.is_kit_machine,
                                      is_service=_presence.is_service_identity,
                                      retired=_presence.retired_rows(HUB_DIR),
                                      required_python=_distribution.parse_required_python(
                                          _dj_setting("HUB_REQUIRED_PYTHON") or ""))
        return report, pub
    except Exception as exc:                                 # noqa: BLE001
        return {"machines": [], "converged": False, "graded": 0,
                "verdict": "distribution could not be computed: %s" % type(exc).__name__}, {}


def _distribution_first_stale(report, now=None):
    """The first-observed-stale clock for graded facts that have no published file, kept in
    ``HUB_DIR/distribution-first-stale.json`` across passes. An unreadable store starts every
    clock now (an item raised late, never early). Never raises."""
    path = HUB_DIR / "distribution-first-stale.json"
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prior = {}
    cur = _distribution.observe_stale(report, prior, now=now)
    if cur != prior:
        try:
            from hub_core import atomic as _atomic
            _atomic.write_json(path, cur)
        except Exception:                                    # noqa: BLE001
            pass
    return cur


def distribution_inbox_items(now=None):
    report, pub = distribution_report(now)
    try:
        return _distribution.inbox_items(report, pub, now=now,
                                         first_stale=_distribution_first_stale(report, now))
    except Exception:                                        # noqa: BLE001
        return []


def live_sessions():
    return _presence.live_sessions(HUB_DIR)


def presence_stamp():
    return _presence.stamp(HUB_DIR)


# ---- addressed delivery: the adopter's human-gate seam and the receipt record ----
# An ask only a person can satisfy (an approval on a host, a signature) is delivered as a GATE:
# it reaches the operator and is never widened to every console. HUB_HUMAN_GATE_PATTERN is a
# regex over the ask's title + body; an ask can also carry the `human-only` tag itself.
# HUB_GATE_RESOLVER is an optional dotted path to `resolver(text) -> str`: a non-empty answer
# names the evidence that the approval has ALREADY landed, which turns the gate back into an
# ordinary question anybody can close. It runs inside every inbox fold, so it must read a
# cache only — never a subprocess, never a network call.
from hub_core import inbox as _inbox
from hub_core import receipts as _receipts


def human_gate():
    pattern = _dj_setting("HUB_HUMAN_GATE_PATTERN") or os.environ.get("HUB_HUMAN_GATE_PATTERN") or ""
    return _inbox.gate_pattern_classifier(pattern)


@functools.lru_cache(maxsize=4)
def _resolver(path):
    import importlib
    module, _, name = path.rpartition(".")
    return getattr(importlib.import_module(module), name)


def gate_satisfied():
    path = _dj_setting("HUB_GATE_RESOLVER") or os.environ.get("HUB_GATE_RESOLVER") or ""
    if not path:
        return None
    try:
        return _resolver(path)
    except Exception:                                        # noqa: BLE001 - a bad path reads as unset
        return None


def question_items(state):
    """The WHOLE open-question queue, longest wait first, gates classified."""
    return _inbox.question_items(state, human_gate=human_gate(), gate_satisfied=gate_satisfied())


def receipt(kind, ref, stage, **kwargs):
    """One notification-lifecycle receipt. Fail-soft: a receipt that cannot be written must
    never be the reason a delivery does not happen."""
    try:
        return _receipts.record(HUB_DIR, kind, ref, stage, **kwargs)
    except Exception:                                        # noqa: BLE001
        return {}


# ---- the operational error stream: record-and-wake wrappers over hub_core.errorlog ----
from hub_core import errorlog as _errorlog


def record_error(source, message, **kwargs):
    """Record one operational error and wake connected cockpits. A throttled repeat (the row
    reports suppressed_since) changed nothing on disk, so it publishes nothing."""
    row = _errorlog.record(HUB_DIR, source, message, **kwargs)
    if not row.get("suppressed_since"):
        _publish_realtime("errors.recorded", fingerprint=row.get("fingerprint"))
    return row


def record_ledger_busy(path, method, waited_s, details=""):
    """One WARNING row for a busy-ledger refusal, the same from every path that answers it.

    Both refusal paths call this: LedgerBusyMiddleware (a StoreBusy that escaped a view) and the
    write seam (which catches StoreBusy itself to answer the structured 503). Contention is
    back-pressure, so it is trended as a warning rather than triaged as a defect — but it must
    be RECORDED, or write contention stays invisible on the board. Fail-soft: the 503 is served
    whether or not the row lands."""
    try:
        return record_error(
            "hub.ledger",
            "ledger lock unavailable; answered 503 (retryable, nothing was written)",
            severity="warning", code="ledger_busy", details=str(details or "")[:500],
            # The wait rides in `reason`: the error log keeps only an allowlisted set of
            # context keys, so a bespoke key would be dropped at the door.
            context={"component": "store", "path": str(path or "")[:240],
                     "method": str(method or ""),
                     "reason": "waited %.1f s for the ledger lock" % float(waited_s or 0)})
    except Exception:                                        # noqa: BLE001
        return None


def errors_changed():
    """Wake cockpits after an ack/reopen/clear — queue state changed with no new row."""
    _publish_realtime("errors.changed")


# ---- declared services, problems, app health: the adopter's own map ----
def apps_config():
    """The services this project runs, as the adopter declares them: settings.HUB_APPS (a
    dict) or the HUB_APPS_JSON environment variable. Per service, all optional:
    {"url", "health_url", "project" (its CI project when it differs), "hosted_in",
     "owners": [agent, ...], "status": "planned"}. A service that has never been declared but
    has forwarded an error still appears — reporting is evidence enough to exist."""
    raw = _dj_setting("HUB_APPS", None)
    if raw is None:
        text = os.environ.get("HUB_APPS_JSON") or ""
        try:
            raw = json.loads(text) if text.strip() else {}
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): (v if isinstance(v, dict) else {}) for k, v in raw.items()
            if str(k).strip()}


def native_slug():
    """The hub's own row in the health table: it records its own errors directly."""
    return str(_dj_setting("HUB_APP_SLUG", "") or os.environ.get("HUB_APP_SLUG") or "hub").strip().lower()


def native_deploy(state):
    """The newest deploy record on this board — the hub's own release."""
    best = None
    for ent in (state.get("entities") or {}).values():
        if not isinstance(ent, dict) or ent.get("type") != "deploy":
            continue
        at = str(ent.get("at") or (ent.get("provenance") or {}).get("updated_at") or "")
        if best is None or at > best["at"]:
            best = {"at": at, "sha": str(ent.get("sha") or "")}
    return best
