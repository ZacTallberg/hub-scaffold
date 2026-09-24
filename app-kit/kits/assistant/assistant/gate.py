"""The three write models, as helpers, so an app picks one instead of inventing a fourth.

An assistant that changes things must choose one of these. All three are legitimate. What is
never legitimate is the model being in the write path alone.

**A. SERVER-MINTED CONFIRMATION** -- the tool is a WRITE scope and the tool substrate refuses
it before the arguments bind unless it holds a token this server minted, from a role IT read
and a click IT received. Best when the action is the app's own existing button and the person
is watching.

**B. STAGED PROPOSAL** -- ``propose_*`` writes a DRAFT and returns a review card showing before
and after; a person applies it through the same code path the edit form uses. Best when the
change is complex, reviewable or batched; it gives you a proposals queue for free.

**C. SIGNED PLAN CARD** -- ``propose_*`` mints a signed card; a person clicking Confirm posts the
signature to a DIFFERENT endpoint, actor-matched and recorded. The model is never in the write
path at all. Best when the target is a system of record you do not own.

TWO RULES HOLD WHICHEVER YOU PICK:

1. **A gated verb stays VISIBLE in the schema.** Hide it until the go-ahead exists and the model
   cannot call it, so no refusal fires, so the button that grants the go-ahead never appears --
   and the assistant tells the person it cannot do the thing it was built to do. The fix is
   :func:`blocked_only_by_confirmation`: show it, and let the refusal be how the app asks.
2. **The irreversible verbs are DESTRUCTIVE**, which the substrate should make unreachable from a
   loop by construction rather than merely gated -- and they are declared anyway, so the catalog
   is honest about where the line is and the refusal can name the surface that owns them.

Stdlib only.
"""
from __future__ import annotations

import hmac
import time
from hashlib import sha256

#: Roles that may grant a go-ahead, weakest first. An app overrides this with its own ladder
#: (the gate kit's is visitor < member < contributor < admin < superadmin). What matters is
#: that the ladder is the SERVER's, read from a roster, and never anything the model or the
#: browser can assert.
DEFAULT_LADDER = ("visitor", "member", "contributor", "admin", "superadmin")


def roles_for(role: str, ladder: tuple[str, ...] = DEFAULT_LADDER) -> frozenset:
    """Everything at or below a role, so ``required_roles={"admin"}`` matches a superadmin
    too -- otherwise every gate needs a list instead of a floor. An unknown role gets the
    weakest rung, never a stronger one."""
    if role not in ladder:
        return frozenset({ladder[0]})
    return frozenset(ladder[:ladder.index(role) + 1])


def mint(actor: str, role: str, *, confirmed: bool, turn_id: str,
         allow=("admin", "superadmin")) -> str | None:
    """The token a WRITE needs, or None.

    None for everything else, and that is what makes the refusal honest: a reader who presses
    the button still gets nothing, because THE BUTTON IS NOT THE PERMISSION -- the role is. The
    model has no path to this function and cannot talk its way past a call that never happened.
    """
    if not confirmed or role not in allow:
        return None
    return f"turn:{turn_id}:{actor or 'unknown'}"


def sign(payload: str, secret: str) -> str:
    """A plan card's signature (write model C). The card travels through the browser, so it must
    be unforgeable there: the execute endpoint recomputes this and refuses a card whose
    contents moved."""
    return hmac.new(secret.encode(), payload.encode(), sha256).hexdigest()


def verify(payload: str, signature: str, secret: str, *, max_age_s: int = 900,
           issued_at: float | None = None) -> tuple[bool, str]:
    """Whether a plan card is still good, and why not when it is not."""
    if not hmac.compare_digest(sign(payload, secret), signature or ""):
        return False, ("this plan was not issued by this server, or its contents changed "
                       "after it was")
    if issued_at is not None and time.time() - issued_at > max_age_s:
        return False, (f"this plan is older than {max_age_s // 60} minutes. The record it was "
                       "computed against may have moved, so it is refused rather than applied "
                       "to a different world.")
    return True, ""


def blocked_only_by_confirmation(tool, ctx) -> bool:
    """True when the ONLY thing between this tool and running is a person's click.

    Such a tool must still be SHOWN to the model (rule 1). A tool is duck-typed: ``scope``
    with ``needs_confirmation``, ``required_roles``; ``ctx`` carries ``confirmation``,
    ``tainted`` and ``roles``.
    """
    scope = getattr(tool, "scope", None)
    if not getattr(scope, "needs_confirmation", False):
        return False
    if getattr(ctx, "confirmation", None) or getattr(ctx, "tainted", False):
        return False
    required = getattr(tool, "required_roles", None) or frozenset()
    return not (required - (getattr(ctx, "roles", None) or frozenset()))


CONFIRMATION_HINT = (" Needs a person's go-ahead: call it anyway -- the refusal is how the app "
                     "asks them, and a confirm button appears.")


def describe_for_model(tool, ctx) -> str:
    """The description the model should see, with the cost stated when it applies. A tool
    whose description hides that it costs a click is called casually and then apologised for;
    one that says so up front gets called deliberately."""
    text = getattr(tool, "description", "") or ""
    if blocked_only_by_confirmation(tool, ctx) and "go-ahead" not in text:
        return text + CONFIRMATION_HINT
    return text


def staged(kind: str, before: dict, after: dict, *, why: str = "", touches: int = 1) -> dict:
    """A review card for write model B: what would change, both sides, and the cost.

    ``touches`` is not decoration. A bulk proposal must say how many records it would reach,
    and the apply must refuse if that count MOVED between the look and the click -- otherwise
    somebody approves "12 rows" and 4,000 change. :func:`scope_moved` is that refusal.
    """
    return {"kind": kind, "before": before, "after": after,
            "why": why or "(no reason given)", "touches": touches, "applied": False,
            "note": ("Nothing has changed. A person applies this, and the apply runs through "
                     "the same code path the form uses.")}


def scope_moved(card: dict, touches_now: int) -> str:
    """The refusal text when a staged card's reach changed since it was shown, else ""."""
    shown = int(card.get("touches") or 0)
    if shown == int(touches_now):
        return ""
    return (f"Refused: this change was reviewed as reaching {shown} record(s) and would now "
            f"reach {touches_now}. Review it again before applying.")
