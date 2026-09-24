"""Validate a production environment and name EVERY problem in one pass.

A validator that throws on the first missing key makes each additional key cost a whole deploy
round trip: the operator fixes one, pushes, waits, and learns the next. Measured on the system
this kit was lifted from: an app's first deploy died on one missing key while five more were
missing and two more had invalid values — five pushes of discovery hiding behind one message.

So this collects presence, value shape, and the keys a POSTURE pulls in (gate on -> the
authenticator keys; external database on -> its connection keys), and raises once with the
whole list. Value checks run only on keys that are present, so a missing key is reported as
missing rather than tripping a shape check on an empty string.

Usage — from settings, before anything else reads the environment::

    from envcheck import assert_production_env
    if not DEBUG and not RUNNING_TESTS:
        assert_production_env(os.environ)

Or standalone against a file, which is how you check an ``.env`` BEFORE you deploy it::

    python envcheck.py path/to/.env          # exit 0 clean, 1 with every problem listed

Stdlib only. The spec below is the kit's own contract (the settings kit reads exactly these
keys); an app extends it by passing ``extra_required`` / ``extra_rules``.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

#: Always required in production.
REQUIRED = (
    "APP_NAME",
    "APP_SLUG",
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
    "DJANGO_DEBUG",
    "APP_GATE_REQUIRED",
)

#: Keys a posture pulls in: (posture key, value that arms it, keys it then requires).
#: An UNSET gate key is read as armed on purpose — production has no other legal posture, so
#: the keys it needs are reported now rather than one deploy later.
POSTURES = (
    ("APP_GATE_REQUIRED", {"true", ""}, ("APP_GATE_SUPERADMINS",)),
    ("APP_DATABASE_URL_ENABLED", {"true"}, ("APP_DATABASE_URL",)),
    ("APP_LLM_REQUIRED", {"true"}, ("APP_LLM_BASE_URL", "APP_LLM_MODEL")),
)

_PLACEHOLDER = re.compile(r"(?i)replace|change.?me|example|local-development|secret.?key|dev-")


def _val(env: Mapping[str, str], key: str) -> str:
    raw = env.get(key)
    return "" if raw is None else str(raw).strip()


def _rule_debug(env):
    v = _val(env, "DJANGO_DEBUG")
    if v and v.lower() != "false":
        yield "DJANGO_DEBUG must be false in production"


def _rule_secret(env):
    v = _val(env, "DJANGO_SECRET_KEY")
    if v and (len(v) < 50 or _PLACEHOLDER.search(v)):
        yield "DJANGO_SECRET_KEY must be a non-placeholder value of at least 50 characters"


def _rule_hosts(env):
    v = _val(env, "DJANGO_ALLOWED_HOSTS")
    if not v:
        return
    hosts = {h.strip().lower() for h in v.split(",") if h.strip()}
    if "*" in hosts:
        yield "DJANGO_ALLOWED_HOSTS must name hosts explicitly, never '*'"
    if not {"127.0.0.1", "localhost"} <= hosts:
        yield ("DJANGO_ALLOWED_HOSTS must include 127.0.0.1 and localhost so the host can "
               "probe its own service")


def _rule_origins(env):
    v = _val(env, "DJANGO_CSRF_TRUSTED_ORIGINS")
    for origin in (o.strip() for o in v.split(",") if o.strip()):
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?", origin):
            yield "DJANGO_CSRF_TRUSTED_ORIGINS must contain only explicit https:// origins"
            return


def _rule_booleans(env):
    for key in ("APP_GATE_REQUIRED", "APP_DATABASE_URL_ENABLED", "APP_LLM_REQUIRED"):
        v = _val(env, key)
        if v and v.lower() not in {"true", "false"}:
            yield f"{key} must be explicitly true or false"


def _rule_gate_on(env):
    v = _val(env, "APP_GATE_REQUIRED").lower()
    if v and v != "true":
        yield "production requires APP_GATE_REQUIRED=true (an app is never anonymous)"


def _rule_script_name(env):
    slug, prefix = _val(env, "APP_SLUG"), _val(env, "FORCE_SCRIPT_NAME")
    if prefix and not re.fullmatch(r"/[A-Za-z0-9/_-]*[A-Za-z0-9_-]", prefix):
        yield "FORCE_SCRIPT_NAME must be an absolute path without a trailing slash"
    if slug and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        yield "APP_SLUG must be lowercase letters, digits and hyphens"


def _rule_llm_url(env):
    v = _val(env, "APP_LLM_BASE_URL")
    if v and not re.match(r"https?://", v):
        yield "APP_LLM_BASE_URL must be an http(s) URL"


RULES: tuple[Callable[[Mapping[str, str]], Iterable[str]], ...] = (
    _rule_debug, _rule_secret, _rule_hosts, _rule_origins, _rule_booleans,
    _rule_gate_on, _rule_script_name, _rule_llm_url,
)


def problems(env: Mapping[str, str], *, extra_required: Iterable[str] = (),
             extra_rules: Iterable[Callable] = ()) -> list[str]:
    """Every problem with ``env`` as a production environment, in a stable order."""
    found: list[str] = []
    for key in (*REQUIRED, *extra_required):
        if not _val(env, key):
            found.append(f"missing required value {key}")
    for posture, armed_by, needs in POSTURES:
        if _val(env, posture).lower() in armed_by:
            for key in needs:
                if not _val(env, key):
                    found.append(f"{posture}={_val(env, posture) or '(unset, read as true)'} "
                                 f"requires {key}")
    for rule in (*RULES, *extra_rules):
        found.extend(rule(env))
    return list(dict.fromkeys(found))


class EnvironmentProblems(RuntimeError):
    def __init__(self, found: list[str]):
        self.problems = found
        lines = [f"production environment has {len(found)} problem(s) — fix them ALL in one edit:"]
        lines += [f"  - {p}" for p in found]
        super().__init__("\n".join(lines))


def assert_production_env(env: Mapping[str, str], **kwargs) -> None:
    found = problems(env, **kwargs)
    if found:
        raise EnvironmentProblems(found)


def read_env_file(path: Path) -> dict[str, str]:
    """A minimal KEY=VALUE reader (comments, blank lines, optional quotes). Read as utf-8-sig
    because an editor that writes a BOM otherwise hides the first key."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if len(argv) != 1:
        print("usage: python envcheck.py <path/to/.env>", file=sys.stderr)
        return 2
    path = Path(argv[0])
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    found = problems(read_env_file(path))
    if found:
        print(EnvironmentProblems(found))
        return 1
    print(f"ENV_OK {path} satisfies the production contract ({len(REQUIRED)} required keys "
          f"plus posture-implied keys)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
