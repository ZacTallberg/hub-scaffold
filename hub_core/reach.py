"""What can this person reach? The apps one person may open, for the banner's app drawer.

Two inputs, joined here so every reader answers the question the same way:

* the APP DIRECTORY -- what each app is called and where it lives. An adopter declares it
  (``HUB_APPS = [{"slug": "budget-app", "name": "Budget", "url": "https://..."}]``); a hub
  that also records per-app deploys can generate it from them.
* the GRANTS -- which slugs this person holds, and in what role. Access is the adopter's
  system of record (a roster table, a directory group, an identity provider), so it arrives
  through a seam: ``{slug: role}`` for one person, from a callable the adopter names.

It invents no access and softens none, and it cannot check what it is handed, so the SEAM
carries two duties:

* return ACTIVE grants only. A revoked, expired or inactive grant is not access; filter it at
  the source, because this join lists whatever it is given. Being signed in to one app is not
  access to the next.
* normalise the person's name. The hub asks with ONE spelling (the username the calling app
  or HUB_PERSON resolved, lower-cased); an access system of record often keeps grants under
  several (a short name, a user-principal name, an email address). The seam must match every
  spelling it holds for that person, or grants filed under the other one silently vanish from
  the drawer.

A grant for an app the directory has no URL for is NOT listed -- a drawer row is a door, and a
door that opens nothing is exactly what the drawer exists to prevent -- but it is COUNTED
(``granted_without_url``), so the omission is visible instead of silent.

Standard library only.
"""
from __future__ import annotations

import re

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def directory(declared) -> dict:
    """``{slug: {"name", "url"}}`` from an adopter's declared list; malformed rows dropped."""
    out: dict = {}
    for row in declared or ():
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip().lower()
        if not SLUG.fullmatch(slug):
            continue
        url = str(row.get("url") or "").strip()
        if url and not re.match(r"^(https?://|/)", url):
            url = ""                                   # only a real door is a door
        out[slug] = {"name": str(row.get("name") or slug)[:80], "url": url}
    return out


def apps_for(grants, declared) -> dict:
    """``{"apps": [{slug, name, url, role}], "granted_without_url": n}``, sorted by name --
    a list somebody reads down, where alphabetical is findable and "newest grant" is not."""
    known = directory(declared)
    apps, no_url = [], 0
    for slug, role in sorted((grants or {}).items()):
        slug = str(slug or "").strip().lower()
        if not SLUG.fullmatch(slug):
            continue
        row = known.get(slug)
        if not row or not row["url"]:
            no_url += 1
            continue
        apps.append({"slug": slug, "name": row["name"], "url": row["url"],
                     "role": str(role or "")[:40]})
    apps.sort(key=lambda r: r["name"].lower())
    return {"apps": apps, "granted_without_url": no_url}
