"""Refuse control-character bytes at the write seam, before they reach the ledger.

The failure this exists for is quiet and total. A markdown source carried the path
``C:\\srv\\audit.log``; somewhere between the editor and the publish call one layer treated
``\\a`` as an escape, and the published text read ``C:\\srv<BEL>udit.log``.
Nothing failed. The bytes were valid UTF-8, the JSON was valid, the page rendered (the BEL is
invisible), and the corrupted instruction rode every agent prompt for a week before anyone
noticed that the one path it named no longer existed.

A lost escape always produces the same evidence: a C0 control character (``\\a`` BEL, ``\\b``
BS, ``\\f`` FF, ``\\v`` VT, ``\\x1b`` ESC, NUL, ...) inside prose. Legitimate board text never
needs one; newline, carriage return and tab are the only controls prose carries. So the rule
is mechanical and cheap, and it holds for every current and future writer because it sits at
the one choke point they all pass through: refuse, name the field and the offsets, and let the
author fix the SOURCE rather than the symptom.

The operational error stream is deliberately exempt (the adapter does not call this for the
``error:report`` scope): a stack trace or a coloured tool log may legitimately carry ESC
sequences, and a failure report refused at the door is a failure nobody sees. Those rows are
redacted and bounded by ``hub_core.errorlog`` and never enter the hash-chained ledger.
"""

from __future__ import annotations

_ALLOWED = {"\n", "\r", "\t"}
_NAMES = {0x00: "NUL", 0x07: "BEL (a lost \\a)", 0x08: "BS (a lost \\b)", 0x0B: "VT (a lost \\v)",
          0x0C: "FF (a lost \\f)", 0x1B: "ESC", 0x7F: "DEL"}


def _bad(ch: str) -> bool:
    code = ord(ch)
    return (code < 0x20 and ch not in _ALLOWED) or code == 0x7F


def _scan(value, path: str, out: list) -> None:
    if len(out) >= 3:
        return
    if isinstance(value, str):
        offsets = [i for i, ch in enumerate(value) if _bad(ch)][:5]
        if offsets:
            first = ord(value[offsets[0]])
            out.append({"field": path or "(body)", "offsets": offsets,
                        "char": ("U+%04X %s" % (first, _NAMES.get(first, ""))).strip()})
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan(str(key), (path + "." if path else "") + "<key>", out)
            _scan(item, (path + "." if path else "") + str(key), out)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan(item, "%s[%d]" % (path, index), out)


def control_char_problems(payload) -> list:
    """Up to three ``{field, offsets, char}`` findings, or ``[]`` when the payload is clean."""
    out: list = []
    _scan(payload, "", out)
    return out


def message(problems: list) -> str:
    where = "; ".join("%s at offset(s) %s (%s)" % (p["field"], p["offsets"], p["char"])
                      for p in problems)
    return ("refused: the payload carries control characters -- %s. This is almost always a "
            "backslash escape eaten between the source and this call (\\a, \\b, \\f, \\v "
            "inside a Windows path or a regex). The ledger is append-only and the text would "
            "reach every reader corrupted; fix the SOURCE and resend." % where)


_ANSI = None


def neutralize(text: str) -> str:
    """Make machine output SAFE TO POST, for a writer that relays text it did not author (a
    session's last words, a tool's log). Terminal colour/cursor sequences are removed outright;
    any other C0 control is rewritten as a visible ``\\xNN`` so the evidence survives without
    tripping the guard above. Newline, carriage return and tab pass. Authored text should be
    fixed at its source instead -- this is for relayed bytes only."""
    global _ANSI
    if _ANSI is None:
        import re
        _ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
    cleaned = _ANSI.sub("", str(text or ""))
    return "".join("\\x%02x" % ord(ch) if _bad(ch) else ch for ch in cleaned)
