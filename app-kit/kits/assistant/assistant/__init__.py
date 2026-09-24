"""The Assistant Canon, and the machinery that holds an app's assistant to it.

    from assistant import canon, audit, routing, plan, belt, gate, probe
    from assistant.adapter import Adapter, Entity, Surface
    from assistant import families

The kit README (one directory up) is the adoption path. ``canon.py`` states the 64 families
as data; ``audit.py`` counts an app's LIVE registry against them.

WHY THIS FILE EXISTS AT ALL, since it declares nothing. Without it ``assistant`` would be a
NAMESPACE package, and a regular package of the same name anywhere on ``sys.path`` wins
outright -- path order does not save you. An app that vendors an older copy of this kit is a
regular package called ``assistant`` on somebody's path, and a namespace copy would be
silently replaced by it: an audit then reports the newest families as missing, and is
believed until somebody prints which file it imported.

Nothing is re-exported here on purpose: an app may take a subset of the kit (the canon and
the audit, no routing), and eager imports would make a partial copy fail at import rather
than at the one call it cannot answer. Import what you need.
"""
__all__ = ()
