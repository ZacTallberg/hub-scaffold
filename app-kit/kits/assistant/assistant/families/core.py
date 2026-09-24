"""The core family: list, search, detail, recent, count and breakdown over declared entities.

Serves the canon's ``core_reads`` and ``primitives`` families for any app with an adapter. Each
tool takes ``entity`` (one of the adapter's keys), so six tools cover every noun the app
declares rather than six per noun.

The guarantees it keeps, because they are the ones a generic read breaks first:

* **Rows are evidence, never a total.** Every row-returning result carries ``shown`` and
  ``total`` for the WHOLE match; ``count`` owns totals. A model summing a sampled page is
  confidently wrong.
* **Fields by inclusion.** Only ``Entity.shows()`` fields leave -- a column added later is
  invisible until somebody decides it may be published.
* **Counts carry denominators**, and a breakdown names what it counted out of.
* **An unknown entity is a sentence**, naming the ones that exist, never an exception.
"""
from __future__ import annotations

MAX_ROWS = 25


def _entity_param(adapter) -> dict:
    return {"type": "string", "enum": list(adapter.entity_keys),
            "description": "Which kind of record: " + ", ".join(
                f"{e.key} ({e.means or e.model})" for e in adapter.entities)}


def _plain(value):
    """A JSON-safe value that loses nothing: a Decimal becomes its exact string, never a float."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row(entity, obj) -> dict:
    out = {}
    for f in obj._meta.concrete_fields:
        if entity.shows(f.name):
            out[f.name] = _plain(getattr(obj, f.attname if f.is_relation else f.name))
    out.setdefault(entity.label, str(getattr(obj, entity.label)))
    if entity.detail_url and not entity.label_is_handle:
        try:
            from django.urls import reverse
            arg = obj.pk if entity.detail_url_arg == "pk" else getattr(obj, entity.label)
            out["open"] = reverse(entity.detail_url, args=[arg])
        except Exception:                                     # noqa: BLE001
            pass                    # an unresolvable link is left out, never invented
    return out


def build(adapter):
    from django.db.models import Count, Q

    def resolve(args):
        key = str((args or {}).get("entity") or "")
        entity = adapter.entity(key)
        if entity is None:
            return None, None, {"error": f"no such kind of record {key!r}",
                                "kinds": list(adapter.entity_keys)}
        return entity, adapter.model_for(key), None

    def order(entity, qs):
        return qs.order_by(f"-{entity.date_field}") if entity.date_field else qs.order_by("-pk")

    def page(entity, qs, total, limit):
        rows = [_row(entity, o) for o in qs[:limit]]
        return {"entity": entity.key, "rows": rows, "shown": len(rows), "total": total,
                "note": ("rows are a sample; use count for totals" if total > len(rows)
                         else "every match is shown")}

    def limit_of(args):
        try:
            return max(1, min(MAX_ROWS, int((args or {}).get("limit") or 10)))
        except (TypeError, ValueError):
            return 10

    def list_(args):
        entity, model, err = resolve(args)
        if err:
            return err
        qs = order(entity, model.objects.all())
        return page(entity, qs, qs.count(), limit_of(args))

    def search(args):
        entity, model, err = resolve(args)
        if err:
            return err
        text = str((args or {}).get("text") or "").strip()
        if not text:
            return {"error": "search needs text"}
        fields = entity.search or (entity.label,)
        cond = Q()
        for f in fields:
            cond |= Q(**{f"{f}__icontains": text})
        qs = order(entity, model.objects.filter(cond))
        result = page(entity, qs, qs.count(), limit_of(args))
        result["searched"] = list(fields)
        return result

    def detail(args):
        entity, model, err = resolve(args)
        if err:
            return err
        ref = str((args or {}).get("label") or "").strip()
        obj = model.objects.filter(**{entity.label: ref}).first()
        if obj is None:
            return {"error": f"no {entity.key} with {entity.label} {ref!r}"}
        return {"entity": entity.key, "record": _row(entity, obj)}

    def recent(args):
        entity, model, err = resolve(args)
        if err:
            return err
        if not entity.date_field:
            return {"error": f"{entity.key} declares no date_field, so 'recent' has no honest "
                             "meaning here; use list"}
        qs = order(entity, model.objects.all())
        return page(entity, qs, qs.count(), limit_of(args))

    def count(args):
        entity, model, err = resolve(args)
        if err:
            return err
        total = model.objects.count()
        return {"entity": entity.key, "count": total, "of": f"all {entity.key}",
                "measured": True}

    def breakdown(args):
        entity, model, err = resolve(args)
        if err:
            return err
        dims = entity.dimensions or tuple(
            f.name for f in model._meta.concrete_fields if getattr(f, "choices", None))
        by = str((args or {}).get("by") or (dims[0] if dims else ""))
        if by not in dims:
            return {"error": f"cannot break {entity.key} down by {by!r}",
                    "dimensions": list(dims)}
        rows = list(model.objects.values(by).annotate(n=Count("pk")).order_by("-n", by))
        total = sum(r["n"] for r in rows)
        return {"entity": entity.key, "by": by, "total": total,
                "groups": [{"value": _plain(r[by]), "count": r["n"]} for r in rows],
                "note": f"{len(rows)} group(s) out of {total} {entity.key}"}

    ent = _entity_param(adapter)
    lim = {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}

    def spec(desc, props, required):
        return (desc, {"type": "object", "properties": props, "required": required,
                       "additionalProperties": False})

    specs = {
        "list": spec("List records of one kind, newest first. Returns shown and total; rows "
                     "are a sample, never a total.", {"entity": ent, "limit": lim}, ["entity"]),
        "search": spec("Find records of one kind whose searchable fields contain the text.",
                       {"entity": ent, "text": {"type": "string"}, "limit": lim},
                       ["entity", "text"]),
        "detail": spec("One record by the identifier a person uses for it.",
                       {"entity": ent, "label": {"type": "string"}}, ["entity", "label"]),
        "recent": spec("The most recently changed records of one kind.",
                       {"entity": ent, "limit": lim}, ["entity"]),
        "count": spec("How many records of one kind exist. The only tool that gives totals.",
                      {"entity": ent}, ["entity"]),
        "breakdown": spec("Count records of one kind grouped by one dimension.",
                          {"entity": ent, "by": {"type": "string"}}, ["entity"]),
    }
    functions = {"list": list_, "search": search, "detail": detail, "recent": recent,
                 "count": count, "breakdown": breakdown}
    return specs, functions
