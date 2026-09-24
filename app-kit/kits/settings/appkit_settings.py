"""The settings kit: one call that gives a Django project the app-kit posture.

    # <project>/config/settings.py
    from pathlib import Path
    import appkit_settings

    BASE_DIR = Path(__file__).resolve().parent.parent
    appkit_settings.configure(
        globals(), BASE_DIR,
        app_module="budget",                 # your Django app
        root_urlconf="config.urls",
        gate_table_prefix="budget_app",       # a COMMITTED literal, never an env read
    )

What it decides for you, each for a reason that cost somebody a day:

* **Fail closed.** DEBUG defaults off; SECRET_KEY is required outside DEBUG; the identity
  gate defaults ON; ALLOWED_HOSTS never defaults to '*'.
* **One test posture.** A test run is detected BEFORE configuration is read, and gets a fixed
  posture — no ambient .env, no external database, gate off, no URL prefix — so a laptop whose
  .env holds the production posture cannot turn a suite red that CI runs green. ``TESTING`` is
  set as an alias of ``RUNNING_TESTS``: code elsewhere asks for ``TESTING``, and a getattr that
  finds nothing defaults False and silently disarms every "never touch the live system from a
  test" guard at once.
* **Every production problem at once.** Outside DEBUG the environment is validated by
  :mod:`envcheck`, which names every missing and invalid key in one error.
* **Gate tables are structure.** ``gate_table_prefix`` is a parameter you commit, because a
  missing environment key must never be able to rename a live app's tables.
* **The model lane is configuration, never a default host.** ``APP_LLM_BASE_URL`` has no
  default: an unset lane is OFF and says so, rather than shipping pointed at a machine nothing
  guarantees is there. An app that requires a model sets ``APP_LLM_REQUIRED=true`` and pins the
  endpoint from its secret store; the envcheck then refuses to boot without it.

Stdlib + Django only. No dotenv dependency: the host ``.env`` is read by :mod:`envcheck`'s
reader, and values already in the process environment win.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:                                  # importable both as a kit module and from its folder
    from . import envcheck            # type: ignore[no-redef]
except ImportError:                   # pragma: no cover - flat sys.path import
    import envcheck                   # type: ignore[no-redef]

TRUE = {"1", "true", "yes", "on"}
FALSE = {"0", "false", "no", "off"}


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(f"{name} must be true or false, got {raw!r}")


def env_list(name: str, default: str = "") -> list[str]:
    """A LIST, never the raw string: iterating a string iterates its characters, which is how
    an owner list of one name once became a set of eleven letters that matched nobody."""
    return [v.strip() for v in os.getenv(name, default).replace(";", ",").split(",") if v.strip()]


def env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 1 << 30) -> int:
    from django.core.exceptions import ImproperlyConfigured
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ImproperlyConfigured(f"{name} must be between {minimum} and {maximum}")
    return value


def running_tests(argv: list[str] | None = None) -> bool:
    argv = sys.argv if argv is None else argv
    head = os.path.basename(argv[0] if argv else "")
    return ("test" in argv[1:2] or head.startswith(("pytest", "py.test"))
            or "unittest" in " ".join(argv[:2]))


def read_build_id(base_dir: Path) -> str:
    """The commit this process runs, read once from the checkout's own HEAD (or APP_BUILD_ID).
    Pages carry it and the live stream announces it, so an open tab can tell it is stale."""
    if os.getenv("APP_BUILD_ID"):
        return os.environ["APP_BUILD_ID"][:12]
    git = base_dir / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:12]
        ref = head[5:]
        if (git / ref).exists():
            return (git / ref).read_text(encoding="utf-8").strip()[:12]
        packed = git / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split()[0][:12]
    except OSError:
        pass
    return "dev"


_TEST_POSTURE_POPPED = ("APP_DATABASE_URL_ENABLED", "FORCE_SCRIPT_NAME",
                        "DJANGO_CSRF_TRUSTED_ORIGINS", "DJANGO_ALLOWED_HOSTS",
                        "APP_GATE_ALLOWLIST", "APP_LLM_REQUIRED", "APP_COMMS_ARMED")


def configure(ns: dict, base_dir: Path, *, app_module: str, root_urlconf: str,
              gate_table_prefix: str, extra_apps: tuple[str, ...] = (),
              public_paths: tuple[str, ...] = (), public_prefixes: tuple[str, ...] = (),
              shell_context: str | None = "app_shell.context_processors.shell",
              env_file: str = ".env") -> None:
    """Fill a settings module's namespace (pass ``globals()``)."""
    base_dir = Path(base_dir)
    tests = running_tests()
    ns["RUNNING_TESTS"] = tests
    ns["TESTING"] = tests
    if tests:
        os.environ["DJANGO_DEBUG"] = "true"
        os.environ["APP_GATE_REQUIRED"] = "false"
        for name in _TEST_POSTURE_POPPED:
            os.environ.pop(name, None)
    else:
        env_path = base_dir / env_file
        if env_path.is_file():
            for key, value in envcheck.read_env_file(env_path).items():
                os.environ.setdefault(key, value)     # the process environment wins

    debug = env_bool("DJANGO_DEBUG", False)
    ns["DEBUG"] = debug
    if not debug and not tests:
        envcheck.assert_production_env(os.environ)

    from django.core.exceptions import ImproperlyConfigured
    secret = os.getenv("DJANGO_SECRET_KEY", "")
    if not secret:
        if not debug:
            raise ImproperlyConfigured("DJANGO_SECRET_KEY is required when DJANGO_DEBUG is false")
        import secrets
        secret = "dev-ephemeral-" + secrets.token_urlsafe(32)
    ns["SECRET_KEY"] = secret

    slug = os.getenv("APP_SLUG", base_dir.name).strip()
    ns["APP_SLUG"] = slug
    ns["APP_NAME"] = os.getenv("APP_NAME", slug.replace("-", " ").title())
    ns["BUILD_ID"] = read_build_id(base_dir)
    ns["ALLOWED_HOSTS"] = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
    ns["CSRF_TRUSTED_ORIGINS"] = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
    prefix = os.getenv("FORCE_SCRIPT_NAME") or None
    ns["FORCE_SCRIPT_NAME"] = prefix
    ns["USE_X_FORWARDED_HOST"] = env_bool("APP_BEHIND_PROXY", False)

    # --- the identity gate (kits/gate) -------------------------------------------------
    ns["APP_GATE_REQUIRED"] = env_bool("APP_GATE_REQUIRED", True)
    ns["APP_GATE_UNGATED_DEV"] = env_bool("APP_GATE_UNGATED_DEV", False)
    ns["APP_GATE_TABLE_PREFIX"] = gate_table_prefix
    ns["APP_GATE_SUPERADMINS"] = env_list("APP_GATE_SUPERADMINS")
    ns["APP_GATE_ALLOWLIST"] = env_list("APP_GATE_ALLOWLIST")
    ns["APP_GATE_AUTHENTICATOR"] = os.getenv("APP_GATE_AUTHENTICATOR",
                                             "app_gate.backends:django_password")
    ns["APP_GATE_PUBLIC_PATHS"] = tuple(public_paths)
    ns["APP_GATE_PUBLIC_PREFIXES"] = tuple(public_prefixes)

    # --- the model lane ------------------------------------------------------------------
    ns["APP_LLM_BASE_URL"] = os.getenv("APP_LLM_BASE_URL", "").strip()
    ns["APP_LLM_MODEL"] = os.getenv("APP_LLM_MODEL", "").strip()
    ns["APP_LLM_API_KEY"] = os.getenv("APP_LLM_API_KEY", "").strip()
    ns["APP_LLM_TIMEOUT_S"] = env_int("APP_LLM_TIMEOUT_S", 60, minimum=1, maximum=600)
    ns["APP_LLM_REQUIRED"] = env_bool("APP_LLM_REQUIRED", False)
    ns["APP_LLM_LANE"] = ("model" if ns["APP_LLM_BASE_URL"] and ns["APP_LLM_MODEL"]
                          else "off: APP_LLM_BASE_URL/APP_LLM_MODEL unset — rules only")

    ns["INSTALLED_APPS"] = [
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.messages",
        "django.contrib.staticfiles",
        "app_gate",
        "app_shell",
        "app_health",
        *extra_apps,
        app_module,
    ]
    ns["MIDDLEWARE"] = [
        "django.middleware.security.SecurityMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "app_gate.middleware.GateMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
    ]
    processors = [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "app_gate.context_processors.actor",
    ]
    if shell_context:
        processors.append(shell_context)
    ns["ROOT_URLCONF"] = root_urlconf
    ns["TEMPLATES"] = [{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [base_dir / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": processors},
    }]

    db_path = Path(os.getenv("DJANGO_DATABASE_PATH", "db.sqlite3"))
    if not db_path.is_absolute():
        db_path = base_dir / db_path
    ns["DATABASES"] = {"default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": db_path,
        "OPTIONS": {
            "timeout": 20,
            # Two simultaneous writers answered "database is locked" under the default
            # DEFERRED transaction: it starts as a reader and fails the instant it upgrades.
            # IMMEDIATE takes the write lock up front so the timeout applies and the second
            # writer WAITS; WAL keeps readers off the writer's back.
            "transaction_mode": "IMMEDIATE",
            "init_command": "PRAGMA journal_mode=WAL;PRAGMA synchronous=NORMAL",
        },
    }}

    ns["STATIC_URL"] = f"{prefix.rstrip('/')}/static/" if prefix else "static/"
    ns["STATIC_ROOT"] = base_dir / "staticfiles"
    ns["APP_SERVE_STATIC"] = env_bool("APP_SERVE_STATIC", True)
    ns["LANGUAGE_CODE"] = "en-us"
    ns["TIME_ZONE"] = os.getenv("DJANGO_TIME_ZONE", "UTC")
    ns["USE_I18N"] = True
    ns["USE_TZ"] = True
    ns["DEFAULT_AUTO_FIELD"] = "django.db.models.BigAutoField"
    ns["LOGIN_URL"] = "app_gate:login"

    if not debug:
        ns["SESSION_COOKIE_SECURE"] = True
        ns["CSRF_COOKIE_SECURE"] = True
        ns["SECURE_CONTENT_TYPE_NOSNIFF"] = True
        ns["SECURE_PROXY_SSL_HEADER"] = ("HTTP_X_FORWARDED_PROTO", "https")


def static_urlpatterns() -> list:
    """``/static/`` for the app server itself -- add ``*appkit_settings.static_urlpatterns()`` to
    the root URLconf.

    Django's runserver serves static files; a production WSGI server (waitress, gunicorn) serves
    NONE, so an app that looked right locally ships with no stylesheet and no script -- every
    declared shell capability silently dead. In DEBUG this serves through the finders (no
    collectstatic needed); otherwise from ``STATIC_ROOT`` after ``manage.py collectstatic``, and
    ``APP_SERVE_STATIC=false`` hands the job to a proxy that serves the directory itself. The
    gate exempts ``/static/``, so a sign-in page gets its stylesheet.
    """
    from django.conf import settings
    from django.urls import re_path

    if settings.DEBUG:
        from django.contrib.staticfiles.views import serve as finder_serve
        return [re_path(r"^static/(?P<path>.*)$", finder_serve, {"insecure": True})]
    if not getattr(settings, "APP_SERVE_STATIC", True):
        return []
    from django.views.static import serve
    return [re_path(r"^static/(?P<path>.*)$", serve, {"document_root": settings.STATIC_ROOT})]
