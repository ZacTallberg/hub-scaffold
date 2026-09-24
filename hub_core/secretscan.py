"""Refuse secret-shaped payloads at the write seam, before they reach the ledger.

The ledger is append-only under a hash chain with triggers that abort UPDATE and DELETE —
by design, because that is what makes it evidence. The same property means a secret pasted
into it can never be removed without destroying the tamper-evidence. And the highest-volume
risky behaviour on any agent board is pasting failing command output into a question or a
note, which routinely contains connection strings. So refuse at the door: milliseconds, no
human in the loop, and the failure message tells the caller exactly what to fix.

Three traps this scanner already paid for, kept as regressions-in-comment:
* "_" is a word character, so a \\b boundary never fires before CLIENT_SECRET= or
  DB_PASSWORD= — the exact .env paste this exists to catch walked through the first
  version. The keyword boundary is therefore "start or non-alphanumeric".
* \\s crosses newlines, so "Set the password:" at end-of-line matched the NEXT line's
  first word and refused innocent prose. An assignment lives on one line: [ \\t]*.
* json.dumps turns a real newline into the characters \\ and n, which are non-space — so a
  markdown line break after "password:" swallowed the next line's first word. The escapes
  are split back out before matching.

A placeholder is what a member types AFTER being told to redact — refusing it again makes
the error message a dead end, so recognizable placeholders clear the assignment check.
"""

from __future__ import annotations

import json
import re

_REDACTED = re.compile(r"(?i)^(x{3,}|\*{3,}|\.{3,}|<[^>]*>|\[[^\]]*\]|\{[^}]*\}|"
                       r"redacted\S*|removed|placeholder|your[_-]?\w+|none|null|empty|"
                       r"changeme|example\S*|\$\{?\w+\}?|%\w+%)$")

_KEYWORD = r"(?:^|[^A-Za-z0-9])(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|" \
           r"client[_-]?secret|private[_-]?key|conn(?:ection)?[_-]?string)"

_SECRET_SHAPES = (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY", "a PEM private key"),
    (r"(?i)glpat-[A-Za-z0-9_.-]{10,}", "a repository-platform personal access token"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "a repository-platform token"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "a chat-platform token"),
    (r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}", "a bearer token"),
    (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "a JWT"),
    (r"AKIA[0-9A-Z]{16}", "a cloud access key id"),
)

# A PLACEHOLDER MAY CONTAIN SPACES. The plain value alternative stops at the first space, so
# `API_TOKEN=<supplied by the deploy>` was captured as `<supplied`, which the bracket placeholder
# cannot match — and an operator's documentation write was refused as a credential. A bracketed
# value is therefore taken WHOLE, and only when the bracket ENDS the token, so
# `password=<x>realsecret` is still read as a credential.
_BRACKETED = r"(?:<[^>\n]{0,160}>|\[[^\]\n]{0,160}\]|\{[^}\n]{0,160}\})(?=[\s\"',;]|$)"
_ASSIGNMENT = re.compile(_KEYWORD + r"[ \t]*[=:][ \t]*(" + _BRACKETED + r"|[^\s\"',;]{6,})",
                         re.IGNORECASE)


def secret_problem(payload) -> str:
    """Return a human message naming the credential shape found, or '' if clean."""
    try:
        blob = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    blob = blob.replace("\\n", "\n").replace("\\r", "\n").replace("\\t", "\t")
    for pattern, label in _SECRET_SHAPES:
        if re.search(pattern, blob):
            return label
    for m in _ASSIGNMENT.finditer(blob):
        if not _REDACTED.match(m.group(1)):
            return "a credential assignment"
    return ""
