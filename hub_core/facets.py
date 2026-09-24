"""Facet fences: one doctrine document, served differently to readers with different authority.

A board's standing doctrine is written for its most privileged reader. Handing the same bytes to
a limited-permission contributor's agent is the wrong default twice over: a paragraph about a
capability the reader cannot use invites the agent to hallucinate that it can, and forking the
document per audience guarantees the copies drift apart. The fix is to keep ONE source and fence
the audience-scoped parts in place::

    <!-- facet: ops -->
    Production credentials are issued from the vault; pull what the deploy needs.
    <!-- /facet -->
    <!-- facet: !ops -->
    The deploy supplies the credentials your service needs. Missing one? Ask.
    <!-- /facet -->

A fence names one or more facets. ``a, b`` is shown only when EVERY named facet is visible;
``!a`` is shown only when ``a`` is NOT visible -- the alternate text the narrower reader gets
instead. Source files carry the same fences as comments on their own lines (``# <facet: ops>``
... ``# </facet>``) so what remains still parses. The marker lines themselves are removed for
every reader, so a fully-privileged reader sees exactly the prose that was written.

Three rules hold the boundary honest:

* OMISSION, NEVER MARKING -- a hidden block leaves no placeholder, count or heading behind; the
  narrower reader cannot learn that something was withheld.
* FAIL CLOSED BY SHAPE -- an unterminated fence hides everything to the end of the document; a
  malformed spec hides its block; and any line that LOOKS like a fence attempt (``<!-- facet``,
  ``<!-- /facets``, ``# <facet``, in any case, anywhere on the line) but is not exactly a
  well-formed marker hides everything from that line to the end of the document, for every
  reader below ``*``. A fence mistake costs the narrow reader text, never leaks it. (A document
  that wants to MENTION the syntax in prose writes it escaped, e.g. ``&lt;!-- facet: x --&gt;``.)
* THE BOUNDARY IS AUTHORITY, NOT A REQUEST PARAMETER -- which facets are visible is decided by
  the caller's credential (the adapter maps scope ``facet:<name>``, ``facet:*`` or ``*``), never
  by a query string the caller controls. An anonymous reader sees no facet at all.

Standard library only, pure over text.
"""

from __future__ import annotations

import re

_I = re.IGNORECASE
_MD_OPEN = re.compile(r"^[ \t]*<!--\s*facet\s*:\s*([^>]*?)\s*-->[ \t]*$", _I)
_MD_CLOSE = re.compile(r"^[ \t]*<!--\s*/facet\s*-->[ \t]*$", _I)
_SRC_OPEN = re.compile(r"^[ \t]*#\s*<facet\s*:\s*([^>]*?)\s*>[ \t]*$", _I)
_SRC_CLOSE = re.compile(r"^[ \t]*#\s*</facet\s*>[ \t]*$", _I)
# Anything that smells like a fence. A line matching this but none of the strict markers above
# is a fence MISTAKE, and a mistake must hide, never show.
_ATTEMPT = re.compile(r"<!--\s*/?\s*facets?\b|#\s*</?\s*facets?\b", _I)
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _spec_visible(spec: str, visible) -> bool:
    """Evaluate one fence spec against the reader's visible set. A malformed spec is hidden."""
    parts = [p.strip().lower() for p in str(spec or "").split(",") if p.strip()]
    if not parts:
        return False
    for part in parts:
        negated = part.startswith("!")
        name = part.lstrip("!").strip()
        if not _NAME.match(name):
            return False
        shown = "*" in visible or name in visible
        if negated == shown:
            return False
    return True


def names(text: str) -> list:
    """Every facet name the document fences (for the author's own tooling; never serve this
    to a narrower reader -- it would name what they cannot see)."""
    found = set()
    for line in str(text or "").splitlines():
        match = _MD_OPEN.match(line) or _SRC_OPEN.match(line)
        if match:
            for part in match.group(1).split(","):
                name = part.strip().lstrip("!").strip().lower()
                if _NAME.match(name):
                    found.add(name)
    return sorted(found)


def render(text: str, visible) -> str:
    """Return ``text`` as the reader holding ``visible`` facets sees it.

    ``visible`` is an iterable of facet names; ``"*"`` means every facet. Fences may nest: a
    block is shown only when it and every enclosing fence are shown. Marker lines are dropped
    for everyone. An open fence with no close hides through the end of the document."""
    visible = {str(v).strip().lower() for v in (visible or ()) if str(v).strip()}
    out, stack = [], []
    for line in str(text or "").splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        strict = (_MD_OPEN.match(bare) or _SRC_OPEN.match(bare)
                  or _MD_CLOSE.match(bare) or _SRC_CLOSE.match(bare))
        if not strict and _ATTEMPT.search(bare):
            if "*" in visible:
                continue      # the fully-privileged reader loses only the broken marker line
            break             # everyone else: a malformed fence hides the rest of the document
        opened = _MD_OPEN.match(bare) or _SRC_OPEN.match(bare)
        if opened:
            stack.append(_spec_visible(opened.group(1), visible))
            continue
        if _MD_CLOSE.match(bare) or _SRC_CLOSE.match(bare):
            if stack:
                stack.pop()
            continue          # a stray close is a marker line too: drop it, show nothing new
        if all(stack):
            out.append(line)
    return "".join(out)


def visible_from_scopes(scopes) -> set:
    """Map a credential's scopes onto the facets it may read."""
    visible = set()
    for scope in scopes or ():
        scope = str(scope or "").strip().lower()
        if scope in ("*", "facet:*"):
            visible.add("*")
        elif scope.startswith("facet:"):
            name = scope.split(":", 1)[1]
            if _NAME.match(name):
                visible.add(name)
    return visible
