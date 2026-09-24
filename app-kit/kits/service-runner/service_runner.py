"""Run, supervise and verify an app's declared services from one strict manifest.

    python service_runner.py --validate-manifest            # SERVICE_MANIFEST_OK services=N
    python service_runner.py --console <name>               # foreground, supervised
    python service_runner.py --verify <name> [--base-url U] # health + gate posture, from the manifest
    python service_runner.py --svc-run <name>               # under a Windows service host (optional)

The manifest (``deploy/services.json`` beside this file, or ``--manifest``) is strict: unknown
or missing fields are refused, secrets may not ride in ``env``, and ports must fall inside the
bands the manifest declares. Declaring the bands is deliberate: a validator that hard-codes ONE
allocation band refuses every app that legitimately kept a port from before the band existed,
and each such app then patches its own copy of the runner by hand.

A DELIBERATE STOP IS NOT A CRASH. The supervisor watches the server thread and raises when it
dies — but a stop closes the server, which is exactly what makes the thread die. If the stop
flag is raised only AFTER closing, the watchdog's tick can land in between and report "server
stopped unexpectedly" for a restart somebody asked for; every deploy then files a phantom error.
So every stop path raises ``stopping`` FIRST, and the supervisor treats a dead worker as a
fault only when no stop was requested.

The verifier reads the app's gate posture from the manifest (``"gate": "required"`` or
``"public"``) instead of assuming the posture of whichever app the runner was copied from: a
gated app must answer an anonymous request for its root with 302/401, never 200.
"""
from __future__ import annotations

import argparse
import importlib
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = PROJECT_DIR / "deploy" / "services.json"
MANIFEST_FIELDS = {"schema_version", "services"}
MANIFEST_OPTIONAL = {"port_bands"}
DEFAULT_PORT_BANDS = ((1024, 65535),)
SERVICE_FIELDS_COMMON = {"name", "runtime", "port", "entry", "display_name", "health_path",
                         "gate", "env"}
SERVICE_FIELDS = {"wsgi": SERVICE_FIELDS_COMMON | {"threads", "channel_timeout"},
                  "asgi": SERVICE_FIELDS_COMMON | {"timeout_keep_alive"}}
BOUNDS = {"wsgi": (("threads", 4, 128), ("channel_timeout", 30, 3600)),
          "asgi": (("timeout_keep_alive", 30, 3600),)}


class ManifestError(Exception):
    pass


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ManifestError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def read_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_strict_object)
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    validate_manifest(payload)
    return payload


def _bands(payload: dict) -> tuple[tuple[int, int], ...]:
    raw = payload.get("port_bands")
    if raw is None:
        return DEFAULT_PORT_BANDS
    if not isinstance(raw, list) or not raw:
        raise ManifestError("port_bands must be a non-empty list of [low, high] pairs")
    bands = []
    for band in raw:
        if (not isinstance(band, list) or len(band) != 2 or not all(type(b) is int for b in band)
                or not 1 <= band[0] <= band[1] <= 65535):
            raise ManifestError(f"port_bands entry {band!r} must be [low, high] within 1..65535")
        bands.append((band[0], band[1]))
    return tuple(bands)


def validate_manifest(payload: object) -> None:
    if not isinstance(payload, dict) or not MANIFEST_FIELDS <= set(payload) \
            or set(payload) - MANIFEST_FIELDS - MANIFEST_OPTIONAL:
        raise ManifestError(f"manifest must contain {sorted(MANIFEST_FIELDS)} "
                            f"(optionally {sorted(MANIFEST_OPTIONAL)}) and nothing else")
    if payload["schema_version"] != 1:
        raise ManifestError("schema_version must equal 1")
    bands = _bands(payload)
    services = payload["services"]
    if not isinstance(services, list) or not services:
        raise ManifestError("services must be a non-empty list")
    names, ports = set(), set()
    for index, service in enumerate(services):
        label = f"services[{index}]"
        if not isinstance(service, dict) or service.get("runtime") not in SERVICE_FIELDS:
            raise ManifestError(f"{label} must be an object with runtime wsgi or asgi")
        runtime = service["runtime"]
        if set(service) != SERVICE_FIELDS[runtime]:
            raise ManifestError(f"{label} ({runtime}) must contain exactly "
                                f"{sorted(SERVICE_FIELDS[runtime])}")
        name = service["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,62}", name):
            raise ManifestError(f"{label}.name must match [A-Za-z][A-Za-z0-9_-]+")
        if name in names:
            raise ManifestError(f"duplicate service name: {name}")
        names.add(name)
        port = service["port"]
        if type(port) is not int or not any(lo <= port <= hi for lo, hi in bands):
            spans = ", ".join(f"{lo}-{hi}" for lo, hi in bands)
            raise ManifestError(f"{label}.port must be an integer inside a declared band ({spans})")
        if port in ports:
            raise ManifestError(f"duplicate service port: {port}")
        ports.add(port)
        if not isinstance(service["entry"], str) or not re.fullmatch(
                r"[a-zA-Z_][a-zA-Z0-9_.]*:[a-zA-Z_][a-zA-Z0-9_]*", service["entry"]):
            raise ManifestError(f"{label}.entry must be a module:attribute reference")
        if not isinstance(service["display_name"], str) or not service["display_name"].strip():
            raise ManifestError(f"{label}.display_name must be a non-empty string")
        if service["gate"] not in ("required", "public"):
            raise ManifestError(f"{label}.gate must be 'required' or 'public'")
        for field, low, high in BOUNDS[runtime]:
            if type(service[field]) is not int or not low <= service[field] <= high:
                raise ManifestError(f"{label}.{field} must be an integer from {low} through {high}")
        if not isinstance(service["health_path"], str) or not re.fullmatch(
                r"/[A-Za-z0-9_./-]+/", service["health_path"]):
            raise ManifestError(f"{label}.health_path must be an absolute trailing-slash path")
        env = service["env"]
        if not isinstance(env, dict):
            raise ManifestError(f"{label}.env must be an object")
        for key, value in env.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not isinstance(value, str):
                raise ManifestError(f"{label}.env entries must map uppercase keys to strings")
            if re.search(r"PASSWORD|SECRET|TOKEN|PRIVATE|CREDENTIAL", key):
                raise ManifestError(f"{label}.env cannot carry secret-like key {key}")
        proxy = env.get("APP_TRUSTED_PROXY")
        if proxy:
            try:
                ipaddress.ip_address(proxy)
            except ValueError as exc:
                raise ManifestError(f"{label}.env.APP_TRUSTED_PROXY must be one IP address") from exc


def load_service(name: str, manifest: Path = DEFAULT_MANIFEST) -> dict:
    for service in read_manifest(manifest)["services"]:
        if service["name"] == name:
            return service
    raise ManifestError(f"service {name!r} is not declared in {manifest}")


def _apply_env(service: dict) -> None:
    for key, value in service["env"].items():
        os.environ[key] = value


def _bind_host(service: dict) -> str:
    return service["env"].get("APP_BIND_HOST", "127.0.0.1")


def build_server(service: dict):
    """(server, run, stop) for the declared runtime. Imported lazily: the host runtime is
    only needed to RUN, never to validate or verify."""
    _apply_env(service)
    proxy = service["env"].get("APP_TRUSTED_PROXY")
    if service["runtime"] == "asgi":
        import uvicorn

        config = uvicorn.Config(
            service["entry"], host=_bind_host(service), port=service["port"],
            log_level="info", access_log=False, lifespan="off",
            timeout_keep_alive=service["timeout_keep_alive"],
            proxy_headers=bool(proxy), forwarded_allow_ips=proxy or None)

        class _Server(uvicorn.Server):
            def install_signal_handlers(self):     # the runner owns signals
                pass

        server = _Server(config)

        def stop():
            server.should_exit = True
        return server, server.run, stop

    from waitress.server import create_server

    module_name, attr = service["entry"].split(":", 1)
    app = getattr(importlib.import_module(module_name), attr)
    kwargs = {}
    if proxy:
        kwargs = {"trusted_proxy": proxy, "trusted_proxy_count": 1,
                  "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-host",
                                            "x-forwarded-proto"},
                  "clear_untrusted_proxy_headers": True}
    server = create_server(app, host=_bind_host(service), port=service["port"],
                           threads=service["threads"],
                           channel_timeout=service["channel_timeout"], **kwargs)
    return server, server.run, server.close


def supervise_server(wait_for_stop, worker_alive, stop_requested, runtime: str) -> None:
    """Block until a stop is requested, or the server thread dies unbidden.

    ``worker_alive`` and ``stop_requested`` are read fresh every tick, in that order, because
    during a stop the worker dies BEFORE the stop event is signalled. Raises only when the
    server is gone and nobody asked for it to go.
    """
    while True:
        if wait_for_stop():
            return
        if not worker_alive():
            if stop_requested():
                return
            raise RuntimeError(f"{runtime.upper()} server stopped unexpectedly")


class _Stopper:
    def __init__(self, stop_server):
        self.stopping = False
        self.event = threading.Event()
        self._stop_server = stop_server

    def request(self, *_):
        self.stopping = True               # FIRST: the supervisor must already know
        try:
            self._stop_server()
        finally:
            self.event.set()


def run_console(service: dict) -> None:
    server, run, stop = build_server(service)
    stopper = _Stopper(stop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stopper.request)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stopper.request)
    worker = threading.Thread(target=run, name=f"{service['name']}-server", daemon=True)
    worker.start()
    print(f"SERVICE_RUNNING {service['name']} on {_bind_host(service)}:{service['port']}", flush=True)
    supervise_server(lambda: stopper.event.wait(1.0), worker.is_alive,
                     lambda: stopper.stopping, service["runtime"])
    worker.join(timeout=10)
    print(f"SERVICE_STOPPED {service['name']} (requested)", flush=True)


def run_windows_service(service: dict) -> None:
    """Optional adapter for a Windows service host (needs pywin32). Same supervision rule."""
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class ManagedService(win32serviceutil.ServiceFramework):
        _svc_name_ = service["name"]
        _svc_display_name_ = service["display_name"]

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.stopping = False
            self._stop = None
            socket.setdefaulttimeout(60)

        def SvcStop(self):
            self.stopping = True             # raised FIRST, before the server is closed
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self._stop is not None:
                self._stop()
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            logging.basicConfig(filename=str(PROJECT_DIR / f"service_{service['name']}.log"),
                                level=logging.INFO,
                                format="%(asctime)s %(levelname)s %(name)s %(message)s")
            try:
                _server, run, self._stop = build_server(service)
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                supervise_server(
                    lambda: win32event.WaitForSingleObject(self.stop_event, 1000)
                    == win32event.WAIT_OBJECT_0,
                    worker.is_alive, lambda: self.stopping, service["runtime"])
            except Exception:
                logging.exception("service failed")
                raise

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(ManagedService)
    servicemanager.StartServiceCtrlDispatcher()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def verify(service: dict, base_url: str | None = None, timeout: float = 10.0) -> list[str]:
    """Probe the RUNNING service: health answers 200, and the root's anonymous answer matches
    the declared gate posture. Returns problems (empty = verified)."""
    base = (base_url or f"http://127.0.0.1:{service['port']}").rstrip("/")
    opener = urllib.request.build_opener(_NoRedirect)
    problems = []

    def status(path: str) -> int:
        req = urllib.request.Request(base + path, headers={"Accept": "text/html"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except (urllib.error.URLError, OSError) as exc:
            problems.append(f"{path}: unreachable ({exc})")
            return 0

    health = status(service["health_path"])
    if health and health != 200:
        problems.append(f"{service['health_path']} answered {health}, expected 200")
    root = status("/")
    if root:
        if service["gate"] == "required" and root not in (301, 302, 303, 307, 308, 401, 403):
            problems.append(f"AUTH POSTURE FAILED: anonymous / answered {root}; a gated app must "
                            "redirect to sign-in or refuse")
        if service["gate"] == "public" and root >= 400:
            problems.append(f"/ answered {root} to an anonymous request on a public app")
    print(f"verify {service['name']} at {base}: health={health or 'unreachable'} "
          f"anonymous-root={root or 'unreachable'} gate={service['gate']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--base-url")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--validate-manifest", action="store_true")
    group.add_argument("--console", metavar="NAME")
    group.add_argument("--verify", metavar="NAME")
    group.add_argument("--svc-run", metavar="NAME")
    args = parser.parse_args(argv)
    try:
        if args.validate_manifest:
            payload = read_manifest(args.manifest)
            print(f"SERVICE_MANIFEST_OK services={len(payload['services'])}")
        elif args.console:
            run_console(load_service(args.console, args.manifest))
        elif args.verify:
            problems = verify(load_service(args.verify, args.manifest), args.base_url)
            for p in problems:
                print(f"  - {p}")
            print("SERVICE_VERIFIED" if not problems else f"SERVICE_NOT_VERIFIED {len(problems)} problem(s)")
            return 0 if not problems else 1
        else:
            run_windows_service(load_service(args.svc_run, args.manifest))
    except Exception as exc:
        print(f"SERVICE_BLOCKED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
