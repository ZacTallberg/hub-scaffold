"""What each PERSON has built on this board — derived from the ledger, never curated.

Framework-free and pure over (events, presence rows). A surface that needs somebody to tag
"built for X" goes stale, and a stale record of someone's work under-credits them silently; so
everything here is a by-product of doing the work: a task appears because its completion event
names the agent, a gap/feat/ADR/decision/note because its creating event does, a release because
the deploy record was written by them.

Identity folding, each rule paid for on the origin system:

* A MACHINE IS NOT A PERSON. Events written under a machine name belong to the agent that
  reports from that machine; unfolded, a laptop renders as an extra member of the team.
* FOLD THROUGH THE RAW PRESENCE ROWS, not the live view: the live view ages an offline laptop
  out, and an offline laptop is still its person's machine.
* A MACHINE NAME NEVER LABELS A PERSON. The label is chosen by an explicit rule (not a machine,
  then alphabetical), never by whichever identity happened to be iterated first.
* Automation identities (service accounts) are never people.
"""

from __future__ import annotations

_AUTHORED = {"gap", "feat", "adr", "decision", "note", "capability"}
_SKIP_NOTE_TAGS = {"question", "probe", "selfcheck", "automated", "healthcheck", "heartbeat", "canary"}


def machines_by_agent(rows, is_service=lambda a: False, is_kit=None) -> dict:
    """{machine: agent} from raw presence rows.

    Only computers count (a row without kit telemetry is a caller, not a machine, and must not
    join anybody's identity set). A machine that writes under its OWN credential also appears as
    an agent in presence; it is still a machine as long as it never reported from a computer
    other than itself. A name that did report from a distinct computer is a person, and a person
    is never folded into somebody else."""
    rows = [r for r in rows or [] if isinstance(r, dict)]
    is_kit = is_kit or (lambda r: bool(r.get("machine")))
    persons = set()
    for r in rows:
        agent = str(r.get("agent") or "").strip().lower()
        name = str(r.get("machine") or "").strip().lower()
        if agent and name and name != agent and is_kit(r):
            persons.add(agent)
    out, seen_at = {}, {}
    for r in rows:
        agent = str(r.get("agent") or "").strip().lower()
        name = str(r.get("machine") or "").strip().lower()
        if (agent and name and name != agent and is_kit(r) and name not in persons
                and not is_service(agent)):
            # A machine reported under several agents belongs to the one reporting from it most
            # recently — never to whichever row a directory listing happened to yield first.
            try:
                when = float(r.get("last_seen") or 0)
            except (TypeError, ValueError):
                when = 0.0
            if name not in out or when > seen_at[name]:
                out[name], seen_at[name] = agent, when
    return out


def _type_of(aggregate: str) -> str:
    parts = str(aggregate or "").split(":")
    return parts[1] if len(parts) >= 3 else ""


def by_person(events, state, rows, *, is_service=lambda a: False, is_kit=None,
              person: str | None = None) -> list:
    machines = machines_by_agent(rows, is_service, is_kit)
    entities = (state or {}).get("entities") or {}

    def canon(agent) -> str:
        agent = str(agent or "").strip().lower()
        return machines.get(agent, agent)

    per: dict = {}
    first_seen: set = set()
    for ev in events or []:
        agg = ev.get("aggregate") or ""
        etype = ev.get("type") or ""
        who_raw = str(ev.get("agent_id") or "").strip().lower()
        if not who_raw or is_service(who_raw):
            first_seen.add(agg)
            continue
        who = canon(who_raw)
        if is_service(who):
            first_seen.add(agg)
            continue
        kind = _type_of(agg)
        rec = per.setdefault(who, {"identities": set(), "tasks": [], "deploys": [], "authored": {}})
        created = agg not in first_seen
        first_seen.add(agg)
        payload = ev.get("payload") or {}
        if kind == "task" and etype == "task.transitioned" and payload.get("status") == "done":
            ent = entities.get(agg) or {}
            rec["identities"].add(who_raw)
            rec["tasks"].append({"id": agg, "title": str(ent.get("title") or agg)[:120],
                                 "at": ev.get("ts")})
        elif kind == "deploy" and created:
            ent = entities.get(agg) or {}
            rec["identities"].add(who_raw)
            rec["deploys"].append({"id": agg, "sha": str(ent.get("sha") or "")[:12], "at": ev.get("ts")})
        elif kind in _AUTHORED and created:
            ent = entities.get(agg) or {}
            tags = {str(t).lower() for t in (ent.get("tags") or [])}
            if kind == "note" and tags & _SKIP_NOTE_TAGS:
                continue
            rec["identities"].add(who_raw)
            rec["authored"].setdefault(kind, []).append(
                {"id": agg, "title": str(ent.get("title") or ent.get("name") or agg)[:120],
                 "at": ev.get("ts")})

    out = []
    for who, rec in per.items():
        ids = set(rec["identities"]) | {who} | {m for m, a in machines.items() if a == who}
        # Not a machine, then alphabetical: the label never depends on iteration order.
        label = sorted(ids, key=lambda x: (x in machines, x))[0]
        if person and person.strip().lower() not in ids:
            continue
        authored = {k: sorted(v, key=lambda r: str(r.get("at") or ""), reverse=True)
                    for k, v in rec["authored"].items()}
        totals = {"tasks": len(rec["tasks"]), "deploys": len(rec["deploys"]),
                  "authored": sum(len(v) for v in authored.values())}
        if not any(totals.values()):
            continue                     # nobody with nothing built is a row; it is noise
        out.append({"person": label, "identities": sorted(ids),
                    "tasks": sorted(rec["tasks"], key=lambda r: str(r.get("at") or ""), reverse=True)[:25],
                    "deploys": sorted(rec["deploys"], key=lambda r: str(r.get("at") or ""), reverse=True)[:10],
                    "authored": {k: v[:10] for k, v in authored.items()},
                    "authored_counts": {k: len(v) for k, v in authored.items()},
                    "totals": totals})
    out.sort(key=lambda r: (r["totals"]["tasks"] + r["totals"]["deploys"], r["totals"]["authored"]),
             reverse=True)
    return out
