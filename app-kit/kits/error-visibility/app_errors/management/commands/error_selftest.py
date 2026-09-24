"""`manage.py error_selftest` -- is this app DARK, or is nothing failing?

Those look identical from outside. The only way to tell them apart is to MAKE something
fail and look for the row. This fires each PRODUCER for real -- a logging call, a dying
thread, the request-exception signal, the agent entry point -- rather than calling record()
directly (that would prove the recorder and skip the wiring, which is what breaks).

    python manage.py error_selftest              # every producer, locally
    python manage.py error_selftest --forward    # ...and prove the hub accepts a row

Exits non-zero when any channel is dark. SAFE BY CONSTRUCTION, because a diagnostic that
leaves a mess is one people stop running, and one that poisons the shared queue is worse:

* forwarding is held DOWN for the run, and a pre-flight PROVES the switch is honoured
  (a real forward is attempted with the sender constructor swapped for a recorder); if a
  sender would still start, the probe refuses to fabricate anything;
* senders are COUNTED for the whole run, so a leak is reported (and contained) rather
  than silently committed -- "PROVEN" covers the leak check too;
* every probe row carries a unique token and is RESOLVED afterwards, never deleted;
* --forward sends ONE row at severity `info` (never queued by the hub) and READS the answer.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import types
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from app_errors import capture

PROBES = ("django", "data", "other", "background", "server", "agent")


_DUMMY_CONFIG = ("https://selftest.invalid/hub", "selftest", "selftest")


def _swap_sender(recorded: list):
    """Replace capture's thread constructor with one that records and never starts, and give
    the forwarder a (never-contacted) configuration, so an unconfigured app cannot pass the
    leak checks vacuously by returning before it would ever have started a sender."""
    saved = capture.threading
    saved_config = capture._forward_config
    capture._forward_config = lambda: _DUMMY_CONFIG

    class _Recorder:
        def __init__(self, *a, **kw):
            recorded.append(kw.get("name") or "sender")

        def start(self):
            pass

    capture.threading = types.SimpleNamespace(
        Thread=_Recorder, excepthook=saved.excepthook, local=saved.local,
        Lock=saved.Lock, main_thread=saved.main_thread)
    return saved, saved_config


def _restore_sender(saved) -> None:
    capture.threading, capture._forward_config = saved


def suppression_is_proven() -> tuple[bool, str]:
    """(ok, why not) -- with suppression ON, a real forward must not reach its sender."""
    started: list = []
    saved = _swap_sender(started)
    try:
        capture._hub_forward("server", "suppression preflight (nothing is sent)",
                             "", "", "/", automatic=False)
    except Exception as exc:                                  # noqa: BLE001
        return False, "the forwarder raised during the preflight: %s" % str(exc)[:120]
    finally:
        _restore_sender(saved)
    if started:
        return False, "suppression is set but NOT honoured: a sender still started"
    return True, ""


def forward_probe(token: str) -> tuple[bool, str]:
    """One verified delivery through the SAME config resolution and sender as the forwarder."""
    base, hub_token, slug = capture._forward_config()
    missing = [n for n, v in (("HUB_API_BASE", base), ("HUB_AGENT_TOKEN", hub_token),
                              ("APP_SLUG", slug)) if not v]
    if missing:
        return False, "not configured: %s missing -- the forwarder is a silent no-op here" % (
            ", ".join(missing))
    payload = capture.build_payload(
        capture.ARM_KIND, "error selftest reached the hub (%s)" % token, "",
        "error_selftest", "", severity="info", code="app_error_probe", slug=slug)
    try:
        ok, detail = capture.post_to_hub(payload, timeout=15)
        return ok, "the hub accepted it (%s)" % detail if ok else "the hub answered %s" % detail
    except Exception as exc:                                  # noqa: BLE001
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]   # an HTTPError's answer
        except Exception:                                     # noqa: BLE001
            pass
        return False, "the hub did not accept it: %s %s" % (str(exc)[:140], body)


class Command(BaseCommand):
    help = "Fire every error producer for real and report which channels are dark."

    def add_arguments(self, parser):
        parser.add_argument("--forward", action="store_true",
                            help="also prove the hub accepts a row (one info row, never queued)")

    def handle(self, *a, **o):
        out = self.stdout.write
        token = "EV-SELFTEST-" + uuid.uuid4().hex[:12]
        out("error selftest  token=%s" % token)
        out("firing each PRODUCER for real; a row is the only evidence that counts\n")
        capture._suppress_forward["on"] = True
        try:
            ok, why = suppression_is_proven()
            if not ok:
                out("REFUSED TO RUN: %s. Nothing was fabricated and nothing was sent." % why)
                raise CommandError("error selftest refused: suppression is not proven")
            ok = self._fire_and_read(token)
        finally:
            capture._suppress_forward["on"] = False
        if o.get("forward"):
            delivered, detail = forward_probe(token)
            out("  %-4s forwarder    %s" % ("PASS" if delivered else "FAIL", detail))
            ok = ok and delivered
        else:
            out("  ---- forwarder    not tested (pass --forward to prove the chain end to end)")
        out("\nRESULT: %s" % ("every producer writes a row, and nothing leaked" if ok
                              else "NOT PROVEN -- see FAIL above"))
        if not ok:
            raise CommandError("error selftest FAILED -- a channel is dark")

    def _fire_and_read(self, token: str) -> bool:
        out = self.stdout.write
        escaped: list = []
        saved = _swap_sender(escaped)       # contain AND count anything that tries to send
        try:
            logging.getLogger("django.selftest").error("%s django-channel probe", token)
            try:
                json.loads("{nope")
            except json.JSONDecodeError:
                logging.getLogger("app.ingest").exception("%s data-channel probe", token)
            logging.getLogger("app.selftest").error("%s other-channel probe", token)

            def _die():
                raise RuntimeError("%s background-channel probe" % token)

            # A REAL thread (this module's threading), so threading.excepthook fires.
            worker = threading.Thread(target=_die, name="error-selftest")
            worker.start()
            worker.join(10)
            try:
                raise RuntimeError("%s server-channel probe" % token)
            except RuntimeError:
                from django.core.signals import got_request_exception
                got_request_exception.send(sender=None, request=None)
            capture.record_agent("%s agent-channel probe" % token, area="selftest")
            time.sleep(0.5)
        finally:
            _restore_sender(saved)

        Model = capture._model()
        rows = list(Model.objects.filter(message__contains=token))
        seen = {r.kind for r in rows}
        evaluated = 0
        ok = True
        for kind in PROBES:
            evaluated += 1
            hit = kind in seen
            ok = ok and hit
            out("  %-4s %-12s %s" % ("PASS" if hit else "FAIL", kind,
                                     "a row was written" if hit else "NO ROW -- this channel is dark"))
        for kind in sorted(seen - set(PROBES)):
            out("  ??   %-12s a row appeared under an unexpected kind" % kind)
        # Zero evaluated is never an all-clear.
        if not evaluated:
            ok = False
        now = timezone.now()
        for r in rows:
            r.resolved_at = now
            r.resolved_by = "selftest"
            r.save(update_fields=["resolved_at", "resolved_by"])
        out("\n%d of %d producers wrote a row; %d probe row(s) resolved"
            % (len(seen & set(PROBES)), evaluated, len(rows)))
        if escaped:
            ok = False
            out("  FAIL leak         %d sender(s) tried to start while suppressed (%s); "
                "contained, so the hub is clean -- but suppression is not honoured"
                % (len(escaped), ", ".join(sorted(set(escaped)))))
        else:
            out("  PASS leak         0 senders started while the probe ran")
        return ok
