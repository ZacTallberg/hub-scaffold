"""Project hub integration: the Django adapter over the portable, stack-neutral hub_core.

Single source of truth = the event log in PROJECT/.hub. The Django views, the typed write API,
and `manage.py hubaudit` all go through here. Django-TOLERANT at import time: every Django
settings read is lazy + guarded, so the pure helpers stay unit-testable without django.setup().

Settings keys (all optional; the {{...}} literals are the documented defaults that init.sh
substitutes at scaffold time):
    HUB_PROJECT_KEY   entity-id prefix (lowercase slug), e.g. "acme".  Default "{{PROJECT_KEY}}".
    HUB_BRAND         human brand for titles, e.g. "Acme".             Default "{{BRAND}}".
    HUB_PROJECT_DIR   canonical project-plane directory.               Default BASE_DIR/PROJECT.
    HUB_BUILD_STAMP   BASE_DIR-relative path of the build-sha stamp the deploy pipeline bakes
                      into the artifact.                               Default "build_sha.txt".
    HUB_BUILD_SHA     explicit immutable revision injected by the artifact platform; overrides stamp.
    HUB_DONE_STRICTNESS completion proof dial: tracked or strict.       Default "tracked".
    HUB_SETTINGS_FILE settings.py path the AST security audit scans.   Default: the module file
                      of DJANGO_SETTINGS_MODULE.
    HUB_WRITE_TOKEN   general write bearer token; grants terminal board authority, not shell
                      execution. Fail-closed when empty.
    HUB_WORKER_LAUNCH_ENABLED   expose the optional grant-backed local launcher. Default False.
    HUB_WORKER_PROTOCOL         custom URL scheme registered on the workstation. Default hub-worker.
    HUB_WORKER_LAUNCH_ISSUER_URL explicit HTTPS consume endpoint (recommended in production).
    HUB_WORKER_GRANT_TTL_S      short grant lifetime, clamped by hub_core. Default 120 seconds.
The project plane defaults to Django ``BASE_DIR/PROJECT``; ``HUB_PROJECT_DIR`` relocates that whole
canonical plane and ``HUB_DIR`` can separately relocate runtime ledger state.
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
HUB_DIR = Path(os.environ.get("HUB_DIR") or (PROJECT / ".hub"))
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
    return realtime.info(HUB_DIR, channel=PROJECT_KEY)


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


def store():
    """A fresh EventStore handle per call (cheap; avoids cross-thread sqlite handles)."""
    return hub_core.EventStore(HUB_DIR)


def current_state(st=None):
    """Fold the board, closing only a store opened by this helper."""
    if st is not None:
        return _project.state(st.events())
    owned = store()
    try:
        return _project.state(owned.events())
    finally:
        owned.close()


def _git_head():
    """Return the running code identity in every deployment shape.

    A source checkout can ask Git directly. A production image normally contains no ``.git``;
    there the pre-build stamp is the artifact's own identity and is the value that must ride on
    Hub mutations and discovery metadata.
    """
    try:
        r = subprocess.run(["git", "-C", str(BASE_DIR), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=4)
        head = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        head = ""
    return head or _running_sha()


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

    Runtime ``PROJECT/state.json`` remains a useful deploy-side shortcut. If it is absent, the
    latest immutable post-canary deploy entity is already durable canonical evidence and supplies
    the same release SHA; production must not report "no deploy" while its ledger names one.
    """
    sj = _state_json()
    head = _git_head()  # Git checkout or, in a production artifact, the baked build stamp.
    sha = _normalize_build_sha(sj.get("last_deploy_sha"))
    release = None
    if not sha and state is not None:
        candidates = []
        for deploy in state.get("by_type", {}).get("deploy", []):
            shipped = _normalize_build_sha(deploy.get("sha"))
            observed = _normalize_build_sha(deploy.get("served_sha"))
            if (shipped and observed == shipped and
                    isinstance(deploy.get("tasks_closed"), list)):
                candidates.append(deploy)
        if candidates:
            release = max(candidates, key=lambda row: str(row.get("at") or ""))
            sha = _normalize_build_sha(release.get("sha"))
    served_sha = _normalize_build_sha(served) if served is not None else None
    coherent = bool(head and sha and head == sha and (served is None or served_sha == head))
    return {
        "repo": sj.get("repo_build"),
        "deploy": sj.get("last_deploy_build") or (release or {}).get("build"),
        "tag": sj.get("last_deploy_tag"), "sha": sha, "served_sha": served_sha, "head": head,
        "coherent": coherent, "live_url": sj.get("live_url") or _IDENTITY.get("app_host"),
        "release_source": "state.json" if _normalize_build_sha(sj.get("last_deploy_sha"))
                          else ("deploy entity" if release else None),
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
    """The settings.py the AST audit scans: HUB_SETTINGS_FILE, else the DJANGO_SETTINGS_MODULE file."""
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


def route_guard_adapter(state):
    """Auth-boundary primitive: assert every mutating route has an explicit gate.

    General writes carry ``@writer`` (the private header token).  The one deliberately narrow
    browser capability may instead carry ``@csrf_protect`` plus ``_hub_origin_gated``; it can only
    mint a short-lived launch grant and never receives general write authority.
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
            elif "hub/api/" in pat:
                cb = getattr(p, "callback", None)
                guarded = getattr(cb, "_hub_token_gated", False) or getattr(cb, "_hub_origin_gated", False)
                if not guarded:
                    viols.append(_sv("routes:unguarded", "every /hub/api/ route has an explicit gate",
                                     "%s -> %s is not token- or origin-gated" %
                                     (pat, getattr(cb, "__name__", "?")),
                                     "@writer or narrow @csrf_protect capability",
                                     remediation="wrap general writes with @writer"))
    try:
        walk(resolver.url_patterns)
    except Exception as e:
        return [_sv("routes:introspect", "URLConf is walkable", str(e), "walkable")]
    return viols


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


# (base, head) -> paths changed between two commits, memoized: both ends are immutable, so the
# answer cannot change, and a warm audit must not pay a subprocess. None means the range could not
# be read (a shallow clone, an unfetched sha) — UNKNOWN, never "empty".
_RANGE_TOUCH_CACHE = {}


def _range_touched_paths(base, head):
    key = (base, head)
    if key in _RANGE_TOUCH_CACHE:
        return _RANGE_TOUCH_CACHE[key]
    try:
        r = subprocess.run(["git", "-C", str(BASE_DIR), "diff", "--name-only", f"{base}..{head}"],
                           capture_output=True, text=True, timeout=15)
        val = None if r.returncode != 0 else tuple(sorted(
            p.strip().replace("\\", "/") for p in (r.stdout or "").splitlines() if p.strip()))
    except Exception:
        val = None
    _RANGE_TOUCH_CACHE[key] = val
    return val


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
    return _audit.audit(state, registry(), store=s, coherence=coh,
                        adapters=[settings_ast_adapter, identity_settings_adapter,
                                  route_guard_adapter])


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
    p = _claim_path(task_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(lease), encoding="utf-8")
    _os.replace(tmp, p)


def claim(task_id, agent, ttl_s=900):
    with ProcessFileLock(CLAIMS, name=".claims.lock", timeout=30):
        now = _time.time()
        cur = _read_lease(task_id)
        if cur and cur.get("expires", 0) > now:
            if cur.get("agent") != agent:
                return {"ok": False, "reason": "held", "held_by": cur.get("agent"), "expires": cur.get("expires")}
            # Retrying the same claim must not silently invalidate the fencing token already held
            # by this worker. Renew the lease in place and return that same token.
            cur["last_heartbeat"] = now
            cur["expires"] = now + ttl_s
            _write_lease(task_id, cur)
            _publish_realtime("lease.heartbeat", task=task_id, agent=agent,
                              expires=cur["expires"])
            _schedule_lease_truth(cur)
            return {"ok": True, "heartbeat_after_s": max(1, ttl_s // 3), **cur}
        lease = {"task": task_id, "agent": agent, "token": _uuid.uuid4().hex,
                 "claimed": now, "last_heartbeat": now, "expires": now + ttl_s}
        _write_lease(task_id, lease)
        _publish_realtime("lease.claimed", task=task_id, agent=agent,
                          expires=lease["expires"])
        _schedule_lease_truth(lease)
        return {"ok": True, "heartbeat_after_s": max(1, ttl_s // 3), **lease}


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


def heartbeat(task_id, token, ttl_s=900):
    with ProcessFileLock(CLAIMS, name=".claims.lock", timeout=30):
        cur = _read_lease(task_id)
        if not cur or cur.get("token") != token or cur.get("expires", 0) <= _time.time():
            return {"ok": False, "reason": "no/stale lease"}
        now = _time.time()
        cur["last_heartbeat"] = now
        cur["expires"] = now + ttl_s
        _write_lease(task_id, cur)
        _publish_realtime("lease.heartbeat", task=task_id, agent=cur.get("agent"),
                          expires=cur["expires"])
        _schedule_lease_truth(cur)
        return {"ok": True, "expires": cur["expires"], "last_heartbeat": now,
                "heartbeat_after_s": max(1, ttl_s // 3)}


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
