"""The re-check of every knowledge record that carries a ``verify`` line.

A STATE claim (who has what, what is up, which port serves which model) decays from the day it
is written; ``verify`` names the command or URL that answers it now. Nothing ran those checks, so
a record could fail its own check for months and still be injected into every prompt with the
authority of standing law. This pass runs them -- on a schedule the adopter wires
(``patterns/knowledge-retrieval.md``), from a vantage that can reach what the checks name.

WHAT IT WILL RUN, and why that is safe by construction. It NEVER executes a stored command. It
recognises three shapes and extracts ONE URL from them -- a bare http(s) URL, ``GET <url>``, and a
curl invocation whose flags are all read-only (-s -S -k -L -I -f, timeouts) -- and fetches that
URL itself with one bounded GET: no credentials, no body, a timeout, a size cap, and redirects
followed only to http(s). Every other shape (ssh, SQL, a script, a curl that posts, writes a file
or sends a header) is SKIPPED and says why, because nothing can prove it read-only. Text after the
URL (a jq filter, "(.version >= 3)") is the check's own condition; it is reported, never evaluated.

WHAT A RESULT MEANS.
  answered  the endpoint answered 2xx/3xx
  failed    it could not answer: connection refused, timeout, DNS, TLS, 404, 5xx
  skipped   not runnable from here (not provably read-only, or it needs a signed-in session)
A failure marks the record NEEDS REVIEW -- ``recheck.status=failed`` plus a ``needs-review`` tag,
so the board, the per-prompt label and every mirror say it -- and NEVER retires it: a heuristic
may not retire knowledge (fail closed). A record is written only when its outcome changes, or
once a week to refresh the date, so a quiet pass costs no ledger writes.

Framework-free: ``run_pass`` takes the records and a ``write`` callable. The client verb
(``python -m hub_core.client recheck-knowledge``) feeds it from the served read API and writes
back through the served write API -- never the ledger directly.

Stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import re
import shlex
import ssl
import urllib.error
import urllib.request

TIMEOUT_S = 10.0
MAX_BYTES = 64 * 1024
REFRESH_DAYS = 7
REVIEW_TAG = "needs-review"

_URL_RE = re.compile(r"https?://[^\s'\"<>|;`]+", re.I)
# curl flags that only shape a GET's output or tolerance. Anything else (-X, -d, -F, -T, -o, -u,
# -H, --data*, --upload*, --output ...) makes the check not provably read-only.
_CURL_BOOL = {"-s", "-S", "-k", "-L", "-I", "-f", "-v", "--silent", "--show-error", "--insecure",
              "--location", "--head", "--fail", "--compressed", "--globoff", "-g"}
_CURL_VALUED = {"--max-time", "-m", "--connect-timeout", "--retry", "--retry-delay"}
_CURL_SHORT = set("sSkLIfvg")
_SIGN_IN = re.compile(r"/(login|signin|sign-in|accounts|oauth|saml|auth)\b", re.I)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify(verify: str) -> dict:
    """{kind: "url", url, insecure, condition} or {kind: "unsupported", reason}."""
    text = " ".join(str(verify or "").split())
    if not text:
        return {"kind": "unsupported", "reason": "empty"}
    first = text.split(" ", 1)[0]
    if re.match(r"^https?://", first, re.I):
        url = _URL_RE.match(text).group(0)
        return {"kind": "url", "url": url, "insecure": False, "condition": text[len(url):].strip()[:160]}
    if first.upper() == "GET":
        m = _URL_RE.search(text)
        if m and text[:m.start()].strip().upper() == "GET":
            return {"kind": "url", "url": m.group(0), "insecure": False,
                    "condition": text[m.end():].strip()[:160]}
        return {"kind": "unsupported", "reason": "GET without a URL"}
    if first.lower() in ("curl", "curl.exe"):
        m = _URL_RE.search(text)
        if not m:
            return {"kind": "unsupported", "reason": "curl without a URL"}
        try:
            tokens = shlex.split(text[:m.start()], posix=True)[1:]
        except ValueError:
            return {"kind": "unsupported", "reason": "curl flags do not parse"}
        insecure, skip_next = False, False
        for tok in tokens:
            if skip_next:
                skip_next = False
                continue
            if tok in _CURL_VALUED:
                skip_next = True
                continue
            if tok in _CURL_BOOL or (tok.startswith("-") and not tok.startswith("--")
                                     and len(tok) > 1 and set(tok[1:]) <= _CURL_SHORT):
                insecure = insecure or tok == "--insecure" or (not tok.startswith("--") and "k" in tok[1:])
                continue
            return {"kind": "unsupported", "reason": "curl %s is not provably read-only" % tok[:40]}
        return {"kind": "url", "url": m.group(0), "insecure": insecure,
                "condition": text[m.end():].strip()[:160]}
    return {"kind": "unsupported", "reason": "a %s check is not provably read-only" % first[:24]}


class _HttpOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to http(s): a check may never be steered to another scheme."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not re.match(r"^https?://", str(newurl or ""), re.I):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-http(s) URL refused",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def run_check(verify: str, *, timeout: float = TIMEOUT_S) -> dict:
    """Run one check. Returns the ``recheck`` fields (without dates). Never raises."""
    c = classify(verify)
    if c["kind"] != "url":
        return {"status": "skipped", "kind": "unsupported", "detail": c["reason"][:400]}
    handlers = [_HttpOnlyRedirect()]
    if c["url"].lower().startswith("https://"):
        ctx = ssl.create_default_context()
        if c["insecure"]:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(c["url"], method="GET", headers={"User-Agent": "hub-recheck/1"})
    cond = (" · condition not machine-checked: %s" % c["condition"]) if c.get("condition") else ""
    try:
        with opener.open(req, timeout=timeout) as r:
            r.read(MAX_BYTES)
            final = r.geturl() or ""
            code = int(getattr(r, "status", 200) or 200)
        if final != c["url"] and _SIGN_IN.search(final):
            return {"status": "skipped", "kind": "url", "http_status": code,
                    "detail": ("needs a signed-in session (redirected to %s)" % final)[:400]}
        return {"status": "answered", "kind": "url", "http_status": code,
                "detail": ("GET %s -> %s%s" % (c["url"], code, cond))[:400]}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"status": "skipped", "kind": "url", "http_status": e.code,
                    "detail": "needs a signed-in session (HTTP %s)" % e.code}
        return {"status": "failed", "kind": "url", "http_status": e.code,
                "detail": ("GET %s -> HTTP %s %s%s" % (c["url"], e.code, e.reason, cond))[:400]}
    except Exception as e:                                   # noqa: BLE001 - every cause is a result
        return {"status": "failed", "kind": "url",
                "detail": ("GET %s -> %s: %s%s" % (c["url"], type(e).__name__,
                                                    str(getattr(e, "reason", e))[:160], cond))[:400]}


def _due(old: dict, new: dict, now: _dt.datetime) -> bool:
    """Write only when the outcome changed, or to refresh a week-old date."""
    if not old:
        return True
    if (old.get("status"), old.get("http_status"), old.get("kind")) != \
            (new.get("status"), new.get("http_status"), new.get("kind")):
        return True
    try:
        last = _dt.datetime.fromisoformat(str(old.get("checked_at")).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=_dt.timezone.utc)
    return (now - last).days >= REFRESH_DAYS


def run_pass(records, *, write, check=run_check, limit: int = 500, dry_run: bool = False,
             out=print) -> dict:
    """One pass over live knowledge records that carry ``verify``.

    ``write(eid, fields, version) -> (ok, detail)`` merges ONLY the fields this pass owns
    (``recheck``, ``tags``) onto the record. Returns the tallies; the caller maps
    ``write_refused`` to a non-zero exit (a re-check that cannot record what it found is
    indistinguishable from one that never ran). A failing CHECK is the finding, not a failure."""
    now = _dt.datetime.now(_dt.timezone.utc)
    t = {"with_verify": 0, "answered": 0, "failed": 0, "skipped": 0, "written": 0,
         "unchanged": 0, "write_refused": 0, "needs_review": 0}
    todo = sorted((e for e in records if str(e.get("verify") or "").strip()),
                  key=lambda e: str(e.get("id")))
    t["with_verify"] = len(todo)
    for ent in todo[:limit]:
        eid = str(ent.get("id"))
        result = check(str(ent.get("verify")))
        t[result["status"]] += 1
        old = ent.get("recheck") if isinstance(ent.get("recheck"), dict) else {}
        stamp = now_iso()
        rec = {k: v for k, v in result.items() if v is not None}
        rec["checked_at"] = stamp
        rec["since"] = (old.get("since") if old.get("status") == rec["status"] and old.get("since")
                        else stamp)
        out("RECHECK %-8s %s  %s" % (rec["status"], eid, rec.get("detail", "")[:160]))
        prior_tags = list(ent.get("tags") or [])
        tags = [x for x in prior_tags if x != REVIEW_TAG]
        if rec["status"] == "failed":
            tags.append(REVIEW_TAG)
            t["needs_review"] += 1
        if not _due(old, rec, now) and tags == prior_tags:
            t["unchanged"] += 1
            continue
        if dry_run:
            continue
        fields = {"recheck": rec}
        if tags != prior_tags:
            fields["tags"] = tags
        ok, detail = write(eid, fields, ent.get("version"))
        if ok:
            t["written"] += 1
        else:
            t["write_refused"] += 1
            out("WRITE REFUSED %s %s" % (eid, str(detail)[:300]))
    return t
