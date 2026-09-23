"""How written text is shortened for DISPLAY, and the rule that it is never shortened for STORAGE.

What a person or an agent writes to the board -- a question, a checkpoint note, a claim note, a
decision -- reaches the ledger whole. A cap on the write path is a silent cut: the writer sees a
success, the reader sees half a sentence, and nothing anywhere says anything was dropped.

Surfaces that must fit a line (a fleet card's "last did", an attention row, a search hit) show
a PREVIEW instead: cut at a word boundary and ending in an ellipsis, so the cut is visible and
the whole text is one click away. Standard library only.
"""
from __future__ import annotations

ELLIPSIS = "…"


def preview(value, limit: int) -> str:
    """``value`` as one display line of at most ``limit`` characters.

    Whitespace runs collapse to one space. Text that already fits is returned unchanged. Longer
    text is cut at the last word boundary inside the limit (unless that would discard more than
    a third of it, in which case the cut is mid-word) and ends in an ellipsis -- never a silent
    truncation that reads like the whole thing.
    """
    text = " ".join(str(value or "").split())
    if limit <= 0 or len(text) <= limit:
        return text
    room = max(1, limit - 1)
    cut = text[:room]
    space = cut.rfind(" ")
    if space >= room * 2 // 3:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + ELLIPSIS
