"""The turn plan: the MODEL reads the person's words; nothing else does.

    from assistant import plan

    turn = await plan.plan_turn(question, schemas=registry.schema(ctx), ask=json_complete,
                                lanes=routing.offers(schemas), app="Budget planning for teams")
    ...
    verdict = await plan.check_answer(answer_text, trail, ask=json_complete, question=question)

WHY. Assistants grow the same front door: regular expressions deciding what a sentence meant --
"is this a request for the app?", "which propose_ tool?", "did the answer claim it staged
something?". Each table is right for the phrasings it was written against and wrong for the next
person's, and each fix adds a phrasing, which is why fixing one case keeps exposing the next.
"I'm missing an entry field for the second approver" reads as a build; "the Save button kind of
vanishes" gets "I have filed a request" with no tool called.

So understanding a sentence is done by the one component that understands sentences. Two
structured calls, both schema-constrained so the answer can only name things that exist:

* :func:`plan_turn` -- before the loop. What KIND of turn this is, the ONE tool that does it
  (an enum of tools this person can call), the few tools it needs, the lanes, and the record
  references the person wrote.
* :func:`check_answer` -- after the loop. Given the answer AND the tools that actually ran, does
  the answer claim a draft or a filing the trail does not show, or say a capability is missing?

FAIL OPEN, NEVER GUESS. A plan that cannot be made returns ``TurnPlan(ok=False)`` and the caller
runs the turn exactly as before: one loop, every tool shown. There is deliberately NO
pattern-matching fallback -- a second, cruder reader of the same sentence is the thing this
module removes. A failed check returns an empty verdict, which adds nothing to the answer.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: What a turn can be, defined by what the person WANTS, never by the words they used.
KINDS = {
    "question": "They want to know or understand something. Nothing should change.",
    "change": "They want records in THIS app created, changed or removed. That is done by "
              "staging a draft with one of the app's own tools; a person applies it.",
    "app_request": "They want the APPLICATION ITSELF to be different -- a new field, button, "
                   "page, behaviour, integration, or something that looks broken -- and none of "
                   "the app's own tools can deliver it. It is filed for the team.",
    "request_followup": "They are asking about, adding to, changing or withdrawing a request "
                        "for the application that was ALREADY filed.",
    "chat": "Greeting, thanks or small talk; no tool is needed.",
}
MAX_TOOLS = 8


@dataclass(frozen=True)
class TurnPlan:
    """What the model understood the turn to be. ``ok=False`` means no plan: run open."""

    ok: bool = False
    kind: str = ""
    tool: str = ""
    tools: tuple = ()
    lanes: tuple = ()
    refs: tuple = ()
    uses_document: bool = False
    named: bool = False                 # the PERSON named the tool (not an inference)
    reason: str = ""
    why_not: str = ""

    @property
    def acts(self) -> bool:
        """A turn that must end with its tool CALLED: a change, a filing, a follow-up."""
        return self.ok and self.kind in ("change", "app_request", "request_followup") \
            and bool(self.tool)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "kind": self.kind, "tool": self.tool, "tools": list(self.tools),
                "lanes": list(self.lanes), "refs": list(self.refs),
                "uses_document": self.uses_document, "named": self.named,
                "reason": self.reason, "why_not": self.why_not}


def _first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    cut = text.find(". ")
    head = text if cut < 0 else text[:cut + 1]
    return head if len(head) <= limit else head[:limit - 1] + "..."


def catalog(schemas) -> list[tuple[str, str]]:
    """``(name, what it is for)`` for every tool this person can call, in registry order."""
    out, seen = [], set()
    for spec in schemas or ():
        if not isinstance(spec, dict):
            continue
        fn = spec.get("function") if isinstance(spec.get("function"), dict) else {}
        name = str(spec.get("name") or fn.get("name") or "")
        if name and name not in seen:
            seen.add(name)
            out.append((name, _first_sentence(spec.get("description") or fn.get("description"))))
    return out


_PLANNER = """You decide what ONE turn of a conversation with an application's assistant is,
before the assistant acts. Read the person's words for what they MEAN -- never match on
particular words.

{app}THE KINDS OF TURN:
{kinds}

HOW TO CHOOSE THE TOOL:
- "tool" is the ONE tool that does what they asked: for a change, the tool that stages that
  draft; for an app request, the tool that files a request; for a follow-up, the tool that reads
  or changes the existing request. For a question, the tool that best answers it, or "" if none
  does. For chat, "".
- If the person names a tool, that is the tool, and "named" is true. Otherwise "named" is false.
- If the app's OWN tools can deliver what they want, it is a "change", not an "app_request".
- "tools": up to {max_tools} tools the turn will need, the chosen tool first.
- "lanes": the kinds of work involved, from: {lanes}.
- "refs": identifiers of individual records the person wrote, exactly as written. Names of
  pages, people or lists are NOT refs. [] if none.
- "uses_document": true only if the request is about a document attached to this conversation.
- "reason": one short sentence saying what they want.

{where}{documents}THE TOOLS (name - what it is for):
{catalog}"""


def _schema(names: list[str], lanes: list[str]) -> dict:
    choices = sorted(set(names))
    return {"type": "object", "additionalProperties": False,
            "required": ["kind", "tool", "named", "tools", "lanes", "refs", "uses_document",
                         "reason"],
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS)},
                "tool": {"type": "string", "enum": [""] + choices},
                "tools": {"type": "array", "maxItems": MAX_TOOLS,
                          "items": {"type": "string", "enum": choices}},
                "lanes": {"type": "array", "maxItems": 3,
                          "items": {"type": "string", "enum": sorted(set(lanes)) or [""]}},
                "refs": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
                "uses_document": {"type": "boolean"},
                "named": {"type": "boolean"},
                "reason": {"type": "string"}}}


def _object(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


async def plan_turn(question: str, *, schemas, ask, history: str = "", where: str = "",
                    documents=(), lanes=(), app: str = "", max_tokens: int = 500) -> TurnPlan:
    """Plan one turn with ONE structured call. Never raises; ``ok=False`` means run open.

    ``lanes`` may be ``routing.offers(schemas)`` or plain keys; ``documents`` are titles of
    files attached to the conversation; ``app`` is one line naming the application, so "the
    app" in the person's words has a referent.
    """
    items = catalog(schemas)
    if not items or not str(question or "").strip():
        return TurnPlan(why_not="no tools" if not items else "nothing to plan")
    names = [n for n, _ in items]
    lane_keys = [str(getattr(getattr(o, "lane", None), "key", o)) for o in (lanes or ())]
    lane_keys = [k for k in lane_keys if k]
    system = _PLANNER.format(
        app=(f"THE APPLICATION: {app}\n\n" if app else ""),
        kinds="\n".join(f"- {k}: {v}" for k, v in KINDS.items()), max_tools=MAX_TOOLS,
        lanes=", ".join(lane_keys) or "(none)",
        where=(f"WHERE THE PERSON IS IN THE APP: {where}\n\n" if where else ""),
        documents=(("ATTACHED TO THIS CONVERSATION: " + "; ".join(map(str, documents)) + "\n\n")
                   if documents else ""),
        catalog="\n".join(f"{n} - {w}" for n, w in items))
    user = question if not history else (f"Earlier in this conversation:\n{history}\n\n"
                                         f"The turn to plan: {question}")
    try:
        raw = await ask([{"role": "system", "content": system},
                         {"role": "user", "content": user}],
                        json_schema=_schema(names, lane_keys), schema_name="turn_plan",
                        max_tokens=max_tokens, temperature=0.0)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("turn plan failed (%s) - running open", exc)
        return TurnPlan(why_not=f"plan call failed: {type(exc).__name__}: {exc}"[:300])
    raw = _object(raw)
    if raw is None:
        return TurnPlan(why_not="plan was not an object")
    kind = str(raw.get("kind") or "")
    if kind not in KINDS:
        return TurnPlan(why_not=f"plan named no kind ({kind!r})")
    known = set(names)
    tool = str(raw.get("tool") or "")
    tool = tool if tool in known else ""
    tools = []
    for n in [tool] + [str(t) for t in (raw.get("tools") or ()) if isinstance(t, str)]:
        if n and n in known and n not in tools:
            tools.append(n)
    chosen = [str(k) for k in (raw.get("lanes") or ()) if str(k) in set(lane_keys)]
    refs = [str(r).strip() for r in (raw.get("refs") or ()) if str(r).strip()]
    return TurnPlan(ok=True, kind=kind, tool=tool, tools=tuple(tools[:MAX_TOOLS]),
                    lanes=tuple(dict.fromkeys(chosen))[:3], refs=tuple(dict.fromkeys(refs)),
                    uses_document=bool(raw.get("uses_document")),
                    named=bool(raw.get("named")) and bool(tool),
                    reason=str(raw.get("reason") or "")[:300])


@dataclass(frozen=True)
class AnswerCheck:
    """What the answer CLAIMS, read by the model. ``ok=False``: no verdict, add nothing."""

    ok: bool = False
    claims_staged: bool = False
    claims_filed: bool = False
    says_cannot: bool = False
    cannot_what: str = ""
    only_suggests: bool = False
    why_not: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "claims_staged": self.claims_staged,
                "claims_filed": self.claims_filed, "says_cannot": self.says_cannot,
                "cannot_what": self.cannot_what, "only_suggests": self.only_suggests,
                "why_not": self.why_not}


_CHECKER = """You read an assistant's answer and say what it CLAIMS, so the application can
check each claim against what actually happened. Judge the meaning, not particular words.
Report only claims about what THIS assistant did in THIS turn -- a quoted or described action by
somebody else is not a claim.

- claims_staged: it says it created, staged, drafted or prepared a record or draft for review.
- claims_filed: it says it filed, logged, raised or submitted a request or ticket for the team.
- says_cannot: it says the assistant is unable to do something or lacks a tool. cannot_what:
  what, in a few words ("" if says_cannot is false).
- only_suggests: the person asked for something to be CHANGED and the answer only describes or
  suggests the change, without saying it staged or filed anything.

What actually ran this turn (tool - outcome):
{trail}"""

_CHECK_SCHEMA = {"type": "object", "additionalProperties": False,
                 "required": ["claims_staged", "claims_filed", "says_cannot", "cannot_what",
                              "only_suggests"],
                 "properties": {"claims_staged": {"type": "boolean"},
                                "claims_filed": {"type": "boolean"},
                                "says_cannot": {"type": "boolean"},
                                "cannot_what": {"type": "string"},
                                "only_suggests": {"type": "boolean"}}}


def _trail(steps) -> str:
    lines = []
    for step in steps or ():
        name = isinstance(step, dict) and (step.get("name") or step.get("tool"))
        if not name:
            continue
        state = "ok" if step.get("ok") else ("failed" if step.get("ok") is False else "no result")
        lines.append(f"- {name}: {state}")
    return "\n".join(lines) or "- (no tool ran)"


async def check_answer(answer: str, steps, *, ask, question: str = "",
                       max_tokens: int = 200) -> AnswerCheck:
    """Read what the answer claims. Never raises; ``ok=False`` means no verdict.

    What to DO with a verdict is the app's: ``claims_filed`` with no filing in the trail is the
    belt's "unfiled" correction; ``says_cannot`` is a missing-tool signal worth filing, not an
    answer to leave standing.
    """
    text = str(answer or "").strip()
    if not text:
        return AnswerCheck(why_not="empty answer")
    user = (f"The person asked: {question}\n\n" if question else "") + f"The answer:\n{text[-6000:]}"
    try:
        raw = await ask([{"role": "system", "content": _CHECKER.format(trail=_trail(steps))},
                         {"role": "user", "content": user}],
                        json_schema=_CHECK_SCHEMA, schema_name="answer_check",
                        max_tokens=max_tokens, temperature=0.0)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("answer check failed (%s) - adding nothing", exc)
        return AnswerCheck(why_not=f"check call failed: {type(exc).__name__}"[:200])
    raw = _object(raw)
    if raw is None:
        return AnswerCheck(why_not="check was not an object")
    return AnswerCheck(ok=True, claims_staged=bool(raw.get("claims_staged")),
                       claims_filed=bool(raw.get("claims_filed")),
                       says_cannot=bool(raw.get("says_cannot")),
                       cannot_what=str(raw.get("cannot_what") or "")[:200],
                       only_suggests=bool(raw.get("only_suggests")))
