"""Forward ERROR+ to the hub's operational error stream (the hub's patterns/error-visibility.md).

Fail-soft by construction: forwarding must never become the reason a request fails, and a hub
outage must never take the app down. A silent no-op until HUB_API_BASE is set; the token is a
scoped ``error:report`` credential issued for this app, never the hub's root token.
"""
import json
import logging
import os
import traceback
import urllib.request


class HubForwarder(logging.Handler):
    def emit(self, record):
        base = os.environ.get("HUB_API_BASE", "").rstrip("/")
        if not base:
            return
        try:
            body = {
                "app": os.environ.get("APP_SLUG", "budget-app"),
                "kind": "job" if getattr(record, "background", False) else "server",
                "message": record.getMessage()[:800],
                "severity": "critical" if record.levelno >= logging.CRITICAL else "error",
                "details": ("".join(traceback.format_exception(*record.exc_info))[:2000]
                            if record.exc_info else ""),
                "component": record.name,
            }
            req = urllib.request.Request(
                base + "/api/app-error", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "X-Agent-Token": os.environ.get("HUB_AGENT_TOKEN", "")})
            urllib.request.urlopen(req, timeout=5)
        except Exception:                                        # noqa: BLE001
            self.handleError(record)
