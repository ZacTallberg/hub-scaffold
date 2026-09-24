"""The orchestrator/specialist split, as a canon-level concern.

    from assistant import routing

    async for event in routing.run_route(
            messages, registry=registry, ctx=ctx, loop_fn=agent_loop, ask=json_complete,
            complete_fn=complete, stream_fn=stream):
        ...

WHY. The canon names several hundred canonical tools across 64 families, and an app that adopts
even half of it hands one model a menu no model reads. Ranking the WHOLE surface against one
sentence has no notion of what kind of turn this is: "what would happen if I moved this?" and
"show me that file" compete for the same slots, and the scenario tools lose to retrieval tools
that look superficially relevant to every question.

A LANE is that missing notion: a KIND OF TURN (retrieve, analyse, temporal, scenario, act,
intake, deliver, explain), declared in :mod:`assistant.canon` and carried on every family. The
route is chosen ONCE per turn by a cheap JSON call that touches no tools, and each chosen lane
runs as a bounded loop over its own slice of the registry.

FOUR RULES:

1. LANES ARE ATTENTION, NEVER AUTHORITY. A lane changes which tools are SHOWN. Permission stays
   in the tool substrate's scopes and :mod:`assistant.gate`, enforced at invoke time whatever
   was shown. A routing layer that also decided authority would be a second security model, and
   the second one is always the one nobody audits.
2. FAIL OPEN, ALL THE WAY BACK. Fewer than two staffed lanes, an orchestrator that errors or
   answers in the wrong shape, a pinned ``required_tool``, an empty route: each runs the single
   unscoped loop the app ran before routing existed. Routing can make a turn cheaper, never
   impossible.
3. EVERY SKIP IS SAID. A declined lane is emitted as a ``LaneEvent``; a lane that RAN AND FAILED
   is written into the answering lane's context in those words.
4. ONE SYSTEM VOICE PER LOOP. Lane guidance rides ``system_extra``, never a second system
   message: two system prompts can make a model stop calling tools entirely, silently.

WHO ANSWERS: the LAST lane, streaming, with the other lanes' findings in its context; if the
route contains ``act``, ``act`` is last, because the lane that performs a change is the only one
that can honestly report what changed. There is no separate synthesis call -- that would be the
ideal place to claim a write that never happened.

Nothing here enumerates tools: a tool maps to families (:func:`canon.families_of`) and a family
declares its lanes, so the day a family gains a lane every app that covers it is staffed.
Stdlib only; the model calls (``loop_fn``, ``ask``) are passed in.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from . import canon

log = logging.getLogger(__name__)

#: How many specialists one turn may spend. A budget, not a taxonomy limit: each lane is a whole
#: bounded loop.
MAX_LANES = 3
#: Ceiling on how many tools a specialist is SHOWN. Past this the block stops being read.
LANE_TOOL_CAP = 24
_ANSWER_LAST = ("act",)
#: The lane appended, after its own tools, to whichever lane ANSWERS. One lane guessing slightly
#: wrong must not lose a tool outright, and reading is the universal fallback.
FALLBACK_LANE = "retrieve"
#: Tools every lane SHOWS whatever the router picked. A request to change the APPLICATION ("the
#: approvers need a field") is phrased like any other turn, so no lane owns it -- and a
#: specialist that cannot see the request tools answers "I have no tool to file feature
#: requests". Small on purpose: every name here costs every routed turn its schema.
ALWAYS_SHOWN = ("file_app_request", "request_status", "list_requests",
                "add_to_request", "change_request")


@dataclass(frozen=True)
class LaneEvent:
    """A frame about the ROUTE. ``kind``: route | lane_start | lane_done | lane_failed |
    skipped. A consumer that does not know this type ignores it, so a view written before
    routing existed keeps working and simply shows no lane chrome."""

    kind: str
    lane: str = ""
    title: str = ""
    ask: str = ""
    why: str = ""
    tools: tuple = ()
    steps: tuple = ()
    mode: str = ""
    finding: str = ""


@dataclass(frozen=True)
class LaneOffer:
    """What THIS app can put behind one lane right now, resolved from the live schema."""

    lane: canon.Lane
    tools: tuple


@dataclass(frozen=True)
class RouteStep:
    lane: str
    ask: str = ""
    why: str = ""


@dataclass(frozen=True)
class Route:
    """``mode`` is ``routed`` when specialists will run, ``open`` for one unscoped loop (not a
    failure; often correct). ``reason`` always says which, in words a person can read."""

    steps: tuple = ()
    skipped: tuple = ()
    mode: str = "open"
    reason: str = ""

    @property
    def lanes(self) -> tuple:
        return tuple(s.lane for s in self.steps)


def _names(schemas) -> list:
    return [str((s.get("name") if isinstance(s, dict) else s) or "") for s in schemas or ()
            if (s.get("name") if isinstance(s, dict) else s)]


def _split(lane_key: str, schemas: list) -> tuple:
    """``(placed, unplaced)``: names the canon put in this lane, and names it could not place
    anywhere (the app's own domain)."""
    placed, unplaced = [], []
    for name in _names(schemas):
        families = canon.families_of(name)
        if not families:
            unplaced.append(name)
        elif any(lane_key in (canon.BY_KEY[f].lanes if f in canon.BY_KEY else ())
                 for f in families):
            placed.append(name)
    return tuple(placed), tuple(unplaced)


def lane_tools(lane_key: str, schemas: list) -> tuple:
    """Every tool this lane may be shown: its own first, then the APP-SPECIFIC ones, which ride
    every staffed lane rather than being dropped -- dropping them is how a routing layer
    silently removes capability. A lane with no placed tools is not staffed and returns ()."""
    placed, unplaced = _split(lane_key, schemas)
    return tuple(placed) + tuple(unplaced) if placed else ()


def offers(schemas: list) -> tuple:
    """Every lane this app can actually staff, in canon order."""
    found = []
    for lane in canon.LANES:
        tools = lane_tools(lane.key, schemas)
        if tools:
            found.append(LaneOffer(lane=lane, tools=tools))
    return tuple(found)


_ORCHESTRATOR = """You are routing ONE question to the specialists that can answer it.

You do not answer the question and you do not call tools. You choose which kinds of turn this
question needs, and for each one you write the sub-question that specialist should work on, in
the person's own terms.

The specialists available for this app:

{brief}

Rules:
- Choose the FEWEST lanes that can answer it. Most questions need exactly one.
- At most {max_lanes}.
- Choose a lane only if the question genuinely needs that KIND of work.
- If the question asks to CHANGE something, "act" must be one of them.
- For every lane you did NOT choose, say in one short clause why not.
- "ask" is what that specialist must find out or do, phrased so it stands alone."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "array", "items": {
            "type": "object",
            "properties": {"lane": {"type": "string"}, "ask": {"type": "string"},
                           "why": {"type": "string"}},
            "required": ["lane", "ask"]}},
        "skipped": {"type": "array", "items": {
            "type": "object",
            "properties": {"lane": {"type": "string"}, "why": {"type": "string"}},
            "required": ["lane", "why"]}},
    },
    "required": ["route"],
}


def brief(available: tuple) -> str:
    """The lane menu: what each lane OWNS, with sample tool names as evidence it is real here."""
    lines = []
    for offer in available:
        sample = ", ".join(offer.tools[:6])
        more = f", +{len(offer.tools) - 6} more" if len(offer.tools) > 6 else ""
        lines.append(f"- {offer.lane.key}: {offer.lane.owns}\n"
                     f"  {len(offer.tools)} tools here, e.g. {sample}{more}")
    return "\n".join(lines)


def _answer(raw) -> dict:
    """The orchestrator's reply as an object, whatever came back. A JSON lane that degrades to
    unconstrained ``json_object`` mode answers with the bare ``route`` ARRAY often enough to
    matter, and ``{}.get`` on a list raising out of this module would kill the turn routing is
    supposed to be incapable of killing. A bare list IS the route; anything else is ``{}``."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (list, tuple)):
        return {"route": raw}
    return {}


def _rows(value) -> tuple:
    """Route rows as dicts, tolerant of an unwrapped single row or a bare lane name."""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    rows = []
    for row in value:
        if isinstance(row, dict):
            rows.append(row)
        elif isinstance(row, str) and row.strip():
            rows.append({"lane": row})
    return tuple(rows)


async def plan_route(question: str, *, available: tuple, ask, history: str = "",
                     max_lanes: int = MAX_LANES) -> Route:
    """Choose the lanes for one question: one cheap JSON call, no tools. EVERY failure returns
    an OPEN route rather than raising. ``ask(messages, **kw)`` is the app's JSON completion."""
    if len(available) < 2:
        why = "no lane has tools" if not available else f"only one lane ({available[0].lane.key})"
        return Route(mode="open", reason=f"nothing to route: {why}")

    keys = {o.lane.key for o in available}
    schema = json.loads(json.dumps(_SCHEMA))
    schema["properties"]["route"]["items"]["properties"]["lane"]["enum"] = sorted(keys)
    schema["properties"]["route"]["maxItems"] = max(1, int(max_lanes))
    system = _ORCHESTRATOR.format(brief=brief(available), max_lanes=max_lanes)
    user = question if not history else (f"Earlier in this conversation:\n{history}\n\n"
                                         f"The question: {question}")
    try:
        raw = await ask([{"role": "system", "content": system},
                         {"role": "user", "content": user}],
                        json_schema=schema, schema_name="assistant_route",
                        max_tokens=600, temperature=0.0)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("assistant route: orchestrator failed (%s) - running open", exc)
        return Route(mode="open", reason=f"orchestrator failed: {exc}")

    reply = _answer(raw)
    steps, seen = [], set()
    for row in _rows(reply.get("route")):
        key = str(row.get("lane") or "").strip()
        if key not in keys or key in seen:
            continue
        seen.add(key)
        steps.append(RouteStep(lane=key, ask=str(row.get("ask") or "").strip(),
                               why=str(row.get("why") or "").strip()))
        if len(steps) >= max_lanes:
            break
    if not steps:
        return Route(mode="open", reason="orchestrator named no usable lane")
    said = {str(r.get("lane") or ""): str(r.get("why") or "") for r in _rows(reply.get("skipped"))}
    skipped = [RouteStep(lane=o.lane.key, why=said.get(o.lane.key, "not chosen"))
               for o in available if o.lane.key not in seen]
    steps.sort(key=lambda s: s.lane in _ANSWER_LAST)
    return Route(steps=tuple(steps), skipped=tuple(skipped), mode="routed",
                 reason=f"{len(steps)} of {len(available)} lanes")


def scope_for(lane_key: str, schemas: list, *, inner=None, core=(), cap: int = LANE_TOOL_CAP,
              fallback: str = ""):
    """``(scope_fn, core, k)`` for one lane, in the loop's own vocabulary.

    ORDER IS THE WHOLE POINT, because the cap cuts from the end: the lane's own tools, then the
    fallback lane's (why the answering lane gets one at all), then the UNPLACED names -- they
    ride every lane so routing cannot make a tool disappear, but they are what the canon knows
    least about and can be numerous enough to eat the budget. ``inner`` (the app's scorer)
    re-ranks WITHIN the lane; its answer is filtered to the lane and anything it omits is
    appended. ALWAYS_SHOWN tools present in the schema take their slots out of the cap.
    """
    placed, unplaced = _split(lane_key, schemas)
    if not placed:
        placed, unplaced = (), ()
    tail = _split(fallback, schemas)[0] if (placed and fallback and fallback != lane_key) else ()
    names = tuple(dict.fromkeys((*placed, *tail, *unplaced)))
    allowed = set(names)

    async def _scope(query, sch, *, observation=None, history=None):
        ranked = list(names)
        if inner is not None:
            try:
                got = await inner(query, sch, observation=observation, history=history)
            except Exception:                                   # noqa: BLE001
                log.warning("assistant route: inner scorer failed in lane %s", lane_key,
                            exc_info=True)
                got = None
            if got:
                first = [n for n in got if n in allowed]
                ranked = first + [n for n in names if n not in set(first)]
        return ranked

    present = set(_names(schemas))
    always = tuple(n for n in ALWAYS_SHOWN if n in present)
    core = tuple(dict.fromkeys(tuple(core) + always))
    k = max(1, min(len(names), int(cap)) - len(always))
    return _scope, core, k


def lane_guidance(lane: canon.Lane, ask: str = "") -> str:
    """The specialist's brief, for ``system_extra``. Never a second system message."""
    text = (f"THIS TURN IS A {lane.title.upper()} TURN. {lane.owns}\n"
            "Work only on that. Another specialist is handling anything else this question "
            "needs, and the tools you are shown are the ones for this kind of work.")
    if ask:
        text += f"\n\nWhat you are being asked for: {ask}"
    return text


def findings_block(found: list) -> str:
    """The other specialists' results for the answering lane. A lane that FAILED is written in
    as a failure: leaving it out produces an answer that reads as complete while part of its
    evidence is missing."""
    if not found:
        return ""
    lines = ["WHAT THE OTHER SPECIALISTS ON THIS TURN FOUND",
             "These results are from this turn and are already obtained. Use them in your "
             "answer; do not run their tools again."]
    for _key, title, text, ok in found:
        if ok:
            lines.append(f"\n[{title}]\n{text}")
        else:
            lines.append(f"\n[{title}] COULD NOT ANSWER: {text}\nSay so in your answer. Do not "
                         "present the rest as if this part had been checked.")
    return "\n".join(lines)


def with_required(route: Route, require_lanes, available, *, max_lanes: int = MAX_LANES) -> Route:
    """``route`` with every REQUIRED lane in it. An OPEN route stays open (the loop gets the
    requirement through its kwargs as before). A required lane the orchestrator left out is
    appended; over ``max_lanes`` the orchestrator's LAST non-required picks are dropped and
    reported as skipped. A requirement is the app's decision and must not depend on model luck."""
    offered = {o.lane.key for o in (available or ())}
    wanted = [k for k in dict.fromkeys(str(x) for x in (require_lanes or ())) if k in offered]
    if route.mode != "routed" or not wanted:
        return route
    steps = list(route.steps)
    present = {s.lane for s in steps}
    for key in wanted:
        if key not in present:
            steps.append(RouteStep(lane=key, why="this turn is required to use a tool in this lane"))
            present.add(key)
    dropped = []
    limit = max(1, int(max_lanes), len(wanted))
    while len(steps) > limit:
        for index in range(len(steps) - 1, -1, -1):
            if steps[index].lane not in wanted:
                dropped.append(steps.pop(index))
                break
        else:
            break
    steps.sort(key=lambda s: s.lane in _ANSWER_LAST)
    skipped = [s for s in route.skipped if s.lane not in {x.lane for x in steps}]
    skipped += [RouteStep(lane=d.lane, why="dropped to make room for a lane this turn requires")
                for d in dropped]
    return Route(steps=tuple(steps), skipped=tuple(skipped), mode="routed",
                 reason=route.reason + f"; {len(wanted)} required")


async def run_route(messages: list, *, registry, ctx, loop_fn, ask, scope_fn=None,
                    scope_core=(), scope_k=None, system_extra: str = "",
                    max_lanes: int = MAX_LANES, lane_cap: int = LANE_TOOL_CAP, turn_sink=None,
                    required_tool: str = "", require_lanes=(), lane_kwargs=None,
                    route: Route | None = None, **loop_kwargs):
    """Route one turn and stream it. Yields the loop's own events plus ``LaneEvent``.

    ``registry.schema(ctx)`` is the authorized schema; ``loop_fn(messages, registry=, ctx=,
    scope_fn=, scope_core=, scope_k=, max_steps=, system_extra=, turn_sink=, **kw)`` is the
    app's tool loop. ``route`` may be a plan the caller already made (:mod:`assistant.plan`),
    so the orchestrator is not asked twice. ``lane_kwargs(lane, answering, tools) -> dict``
    varies the loop per lane -- how a required tool reaches only the lane that owns it.
    Usage is per loop: a routed turn emits one usage frame per lane and a consumer must SUM.
    """
    schemas = registry.schema(ctx)
    available = offers(schemas)
    question = _last_user_text(messages)

    if required_tool:
        route = Route(mode="open", reason=f"required_tool={required_tool}")
    elif route is not None:
        staffed = {o.lane.key for o in available}
        steps = tuple(s for s in route.steps if s.lane in staffed)
        if route.mode == "routed" and steps:
            steps = tuple(sorted(steps, key=lambda s: s.lane in _ANSWER_LAST))
            route = Route(steps=steps, skipped=tuple(
                RouteStep(lane=o.lane.key, why="not in the plan") for o in available
                if o.lane.key not in {s.lane for s in steps}),
                mode="routed", reason=route.reason or "planned")
        else:
            route = Route(mode="open", reason=route.reason or "the plan named no staffed lane")
        route = with_required(route, require_lanes, available, max_lanes=max_lanes)
    else:
        route = await plan_route(question, available=available, ask=ask,
                                 history=_history_text(messages), max_lanes=max_lanes)
        route = with_required(route, require_lanes, available, max_lanes=max_lanes)

    yield LaneEvent(kind="route", mode=route.mode, why=route.reason,
                    steps=tuple((s.lane, s.ask) for s in route.steps))

    if route.mode != "routed":
        async for event in loop_fn(messages, registry=registry, ctx=ctx, scope_fn=scope_fn,
                                   scope_core=tuple(scope_core), scope_k=scope_k,
                                   system_extra=system_extra,
                                   turn_sink=_tagged_sink(turn_sink, "open"),
                                   required_tool=required_tool, **loop_kwargs):
            yield event
        return

    for step in route.skipped:
        lane = canon.lane(step.lane)
        yield LaneEvent(kind="skipped", lane=step.lane, title=lane.title if lane else step.lane,
                        why=step.why)

    found: list = []
    last = len(route.steps) - 1
    for index, step in enumerate(route.steps):
        lane = canon.lane(step.lane)
        if lane is None:
            continue
        answering = index == last
        scope, core, k = scope_for(step.lane, schemas, inner=scope_fn, core=scope_core,
                                   cap=lane_cap, fallback=FALLBACK_LANE if answering else "")
        extra = "\n\n".join(x for x in (system_extra, lane_guidance(lane, step.ask)) if x)
        if answering and found:
            extra += "\n\n" + findings_block(found)
        yield LaneEvent(kind="lane_start", lane=lane.key, title=lane.title, ask=step.ask,
                        why=step.why, tools=lane_tools(lane.key, schemas),
                        mode="answering" if answering else "specialist")

        convo = messages if answering else _asked(messages, step.ask)
        per_lane = dict(loop_kwargs)
        if lane_kwargs is not None:
            try:
                per_lane.update(lane_kwargs(lane.key, answering, lane_tools(lane.key, schemas))
                                or {})
            except Exception:                                   # noqa: BLE001
                log.warning("assistant route: lane_kwargs failed for %s", lane.key,
                            exc_info=True)
        lane_steps = per_lane.pop("max_steps", None) or lane.max_steps
        text, failure = "", ""
        try:
            async for event in loop_fn(convo, registry=registry, ctx=ctx, scope_fn=scope,
                                       scope_core=core,
                                       scope_k=min(k, scope_k) if scope_k else k,
                                       max_steps=lane_steps, system_extra=extra,
                                       turn_sink=_tagged_sink(turn_sink, lane.key), **per_lane):
                name = type(event).__name__
                if answering:
                    yield event
                    continue
                # A specialist does not talk to the person: its prose is evidence for the lane
                # that answers. Its TOOL activity is forwarded -- the turn showing its work.
                if name == "DoneEvent":
                    text = str(getattr(event, "text", "") or "")
                elif name == "ErrorEvent":
                    failure = str(getattr(event, "message", "") or "") or "the lane errored"
                    yield event
                elif name not in ("TokenEvent", "MetaEvent"):
                    yield event
        except Exception as exc:                                # noqa: BLE001
            if answering:
                raise
            failure = str(exc) or exc.__class__.__name__
            log.warning("assistant route: lane %s failed", lane.key, exc_info=True)
        if answering:
            return
        ok = bool(text) and not failure
        found.append((lane.key, lane.title, text if ok else (failure or "it returned nothing"), ok))
        yield LaneEvent(kind="lane_done" if ok else "lane_failed", lane=lane.key,
                        title=lane.title, finding=text, why=failure)


def _tagged_sink(turn_sink, lane_key: str):
    """Put the lane on every turn record, so tool-skip stays countable PER LANE."""
    if turn_sink is None:
        return None

    def _sink(record):
        turn_sink({**record, "lane": lane_key})
    return _sink


def _asked(messages: list, ask: str) -> list:
    """The conversation with the specialist's own sub-question as the last user turn."""
    if not ask:
        return list(messages)
    head = [m for m in (messages or ()) if m.get("role") != "system"]
    asked = {"role": "user", "content": ask}
    return head[:-1] + [asked] if head else [asked]


def _last_user_text(messages: list) -> str:
    for message in reversed(messages or ()):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(str(p.get("text") or "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text").strip()
    return ""


def _history_text(messages: list, *, turns: int = 2) -> str:
    rows = [m for m in (messages or ()) if m.get("role") in ("user", "assistant")]
    out = []
    for message in rows[-(turns * 2 + 1):-1]:
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            out.append(f"{message['role']}: {content.strip()[:400]}")
    return "\n".join(out)


def status() -> dict:
    """What the routing layer is, for a status endpoint. Derived, never declared."""
    return {"lanes": [{"key": l.key, "title": l.title, "max_steps": l.max_steps,
                       "families": len(canon.families_in_lane(l.key))} for l in canon.LANES],
            "families": len(canon.FAMILIES), "unrouted_families": list(canon.unrouted()),
            "max_lanes": MAX_LANES, "lane_tool_cap": LANE_TOOL_CAP}
