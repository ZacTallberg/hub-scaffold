"""Safe read-modify-write for an agent harness's hook settings file (e.g. a Claude Code
``settings.json``, a Codex ``hooks.json``).

These files belong to the person and to every other tool that registers hooks there. An installer
that read one as strict UTF-8 and treated any parse failure as an EMPTY document turned a
settings file saved with a byte order mark into ``{}`` — and the write that followed kept only the
installer's own hooks and silently dropped everyone else's. Four rules, each closing one way that
happened or could:

1. Read with ``utf-8-sig`` (a BOM is legal and common where editors and shells write UTF-8).
2. FAIL CLOSED: a file that exists and does not parse as a JSON object is never written. The
   install aborts and says why; a person fixes the file. An absent or empty file is ``{}``.
3. Write atomically (temp file + ``os.replace``), after a byte-for-byte backup.
4. Prove it: re-read what was written and require every hook entry that was not ours — and every
   top-level key we did not mean to change — to still be present and unchanged. Otherwise restore
   the backup and abort loudly.

Standard library only.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Callable


class SettingsRefused(RuntimeError):
    """The write refused to touch the file, or restored it after a failed proof."""


def load(path, *, default: dict | None = None) -> dict:
    path = Path(path)
    if not path.exists():
        return dict(default or {})
    raw = path.read_bytes()
    if not raw.strip():
        return dict(default or {})
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SettingsRefused(
            "REFUSING to write %s: it exists but is not valid JSON (%s). Writing now would replace "
            "every hook and setting in it. Fix the file (or restore %s.bak), then re-run."
            % (path, exc, path.name)) from exc
    if not isinstance(data, dict):
        raise SettingsRefused("REFUSING to write %s: its top level is %s, not a JSON object."
                              % (path, type(data).__name__))
    return data


def foreign_hooks(data: dict, is_ours: Callable[[dict], bool]) -> list:
    """Every hook entry that is not ours, as sorted (event, canonical JSON) — the invariant."""
    out = []
    hooks = data.get("hooks") or {}
    if not isinstance(hooks, dict):
        return out
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and not is_ours(entry):
                out.append((str(event), json.dumps(entry, sort_keys=True)))
    return sorted(out)


def _others(data: dict, touched: tuple) -> dict:
    return {k: json.dumps(v, sort_keys=True) for k, v in data.items() if k not in touched}


def save(path, data: dict, *, before: dict, is_ours: Callable[[dict], bool] = lambda _e: False,
         touched: tuple = ("hooks",)) -> str:
    """Atomically write ``data``; prove no foreign hook entry and no untouched top-level key was
    lost or changed; restore the backup and raise on any loss. Returns the backup path ("" when
    there was no file before)."""
    path = Path(path)
    want_hooks = foreign_hooks(before, is_ours)
    want_keys = _others(before, touched)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak")
    had_file = path.exists()
    if had_file:
        shutil.copy2(path, backup)
    tmp = path.with_name("%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        after = load(path)
    except SettingsRefused:
        after = None
    missing = [e for e in want_hooks if after is None or e not in foreign_hooks(after, is_ours)]
    got_keys = _others(after or {}, touched)
    changed = sorted(k for k, v in want_keys.items() if got_keys.get(k) != v)
    if after is None or missing or changed:
        if had_file:
            shutil.copy2(backup, path)
        else:
            path.unlink(missing_ok=True)
        what = []
        if missing:
            what.append("%d hook entr%s that %s not ours (events: %s)"
                        % (len(missing), "y" if len(missing) == 1 else "ies",
                           "is" if len(missing) == 1 else "are",
                           ", ".join(sorted({ev for ev, _ in missing}))))
        if changed:
            what.append("top-level key(s) %s" % ", ".join(changed))
        raise SettingsRefused("ABORTED: writing %s would have lost %s. The original file was restored%s."
                              % (path, " and ".join(what) or "its content",
                                 (" from " + backup.name) if had_file else ""))
    return str(backup) if had_file else ""
