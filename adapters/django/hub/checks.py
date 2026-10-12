"""The fold and the schema are one contract, checked where CI already looks (``manage.py check``).

The fold (hub_core.project) stamps provenance onto every entity, and the schemas are closed
(``additionalProperties: false``). The write door validates what an event WRITES, so a new fold
stamp no longer refuses writes -- but every reader, export and backup reconstruction still serves
the FOLD'S OUTPUT, and a stamp the schema does not declare makes that output invalid everywhere at
once. On the instance this was lifted from, the same class shipped three times (a fold stamp added
without its declaration, then the declaration lost again in a rebase), each first seen as a stream
of refused writes in production. This check folds a real create + update through the real fold and
validates the result with the real validator, so the next undeclared stamp fails ``manage.py
check`` -- the pipeline -- instead.
"""
from django.core.checks import Error, Tags, register


@register(Tags.models)
def fold_output_matches_schema(_app_configs=None, **_kwargs):
    from hub_core import ids
    from hub_core import project as _project
    from hub_core.validate import validate

    from . import hub_app

    eid = ids.make_id(hub_app.PROJECT_KEY, "note", "fold-schema-contract")
    payload = {"type": "note", "title": "fold/schema contract", "category": "context",
               "body_md": "a note created and then updated, folded by the real fold."}
    # Every field the store puts on an event, so every branch of the provenance stamp runs.
    events = [{"aggregate": eid, "type": "note.created", "payload": payload, "result_version": v,
               "seq": v, "ts": "2026-01-01T00:00:0%dZ" % v, "agent_id": "agent-%d" % v,
               "git_sha": "%07dabcdef" % v} for v in (1, 2)]
    ent = _project.fold(events).get(eid) or {}
    problems = validate(ent, "note", hub_app.registry())
    if not problems:
        return []
    return [Error(
        "the fold's output for an updated note fails PROJECT/schema: " + "; ".join(problems[:5]),
        hint="Declare every key hub_core/project.py stamps (provenance especially) in "
             "PROJECT/schema/common.schema.json: every reader, export and backup "
             "reconstruction serves the fold's output.",
        id="hub.E001")]
