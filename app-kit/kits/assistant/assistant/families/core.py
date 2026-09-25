"""The core family: list, search, detail, recent, count and breakdown over declared entities.

Serves the canon's ``core_reads`` and ``primitives`` families for any app with an adapter. Each
tool takes ``entity`` (one of the adapter's keys), so six tools cover every noun the app
declares rather than six per noun.

The guarantees it keeps, because they are the ones a generic read breaks first:

* **Rows are evidence, never a total.** Every row-returning result carries ``shown`` and
  ``total`` for the WHOLE match; ``count`` owns totals. A model summing a sampled page is
  confidently wrong.
* **Fields by inclusion.** Only ``Entity.shows()`` fields leave -- a column added later is
  invisible until somebody decides it may be published. Filters and dimensions are offered only
  over published or declared fields, so a count cannot be used to read an unpublished column.
* **Whose rows.** The model comes from ``adapter.model_for``, so an entity's ``scope`` narrows
  every read; a scoped total says "you can see", never "this app holds".
* **Counts carry denominators**, and a breakdown names what it counted out of. A tally never
  counts a SOFT-DELETED row the app's own pages hide (``is_deleted``) unless the filter names
  that flag, and every payload says how many it left out (``excluded_deleted``).
* **A filter is a closed vocabulary**, derived from the model: booleans, declared choices (a
  LIST means "any of these"), foreign keys (by the related record's name), and ``days`` over the
  entity's clock. An unknown key or value is REFUSED with the accepted vocabulary, never ignored.
* **A relation groups by name.** A foreign-key dimension groups by what a person calls the
  related record (its declared label, else its name/title/label field), never its pk.
* **One record means one.** ``detail`` counts the matches in the database; several records
  sharing a label is a refusal that names each one's id, never the first of them.
* **An unknown entity is a sentence**, naming the ones that exist, never an exception.
"""
from __future__ import annotations

import datetime as dt
import uuid

MAX_ROWS = 25
VOCABULARY_CAP = 12
#: The soft-delete flag. A tally that counts a deleted row reports a number the app's own pages
#: never show, so the tally tools count LIVE rows unless the caller's filter names this flag.
SOFT_DELETE_FIELD = "is_deleted"
_NAME_FIELDS = ("name", "title", "label")


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


def _row(adapter, entity, obj) -> dict:
    out = {}
    for f in obj._meta.concrete_fields:
        if not entity.shows(f.name):
            continue
        if f.is_relation and f.many_to_one:
            # The related record by the name a person uses for it -- the same path a filter or
            # a breakdown uses -- never a bare pk the reader cannot look up.
            # A related record that is itself a declared entity is read through ITS scope, so
            # a row never names a record the asker could not open.
            value = getattr(obj, f.attname)
            other = _related_entity(adapter, f)
            path = _related_path(adapter, f)
            if value is not None and other is not None:
                value = (adapter.model_for(other.key).objects.filter(pk=value)
                         .values_list(other.label, flat=True).first()) or "(not visible to you)"
            elif value is not None and not path.endswith("_id"):
                value = getattr(getattr(obj, f.name), path.split("__", 1)[1], value)
            out[f.name] = _plain(value)
        else:
            out[f.name] = _plain(getattr(obj, f.name))
    out.setdefault(entity.label, str(getattr(obj, entity.label)))
    if entity.detail_url and not entity.label_is_handle:
        try:
            from django.urls import reverse
            arg = obj.pk if entity.detail_url_arg == "pk" else getattr(obj, entity.label)
            out["open"] = reverse(entity.detail_url, args=[arg])
        except Exception:                                     # noqa: BLE001
            pass                    # an unresolvable link is left out, never invented
    return out


def _whose(entity) -> str:
    """What a total counts: the whole table, or -- for a scoped entity -- what the asker sees."""
    from ..adapter import SHARED
    scope = getattr(entity, "scope", None)
    return "this app holds" if scope is None or scope is SHARED else "you can see"


def _is_text(field) -> bool:
    return field.get_internal_type() in ("CharField", "TextField", "SlugField", "EmailField")


# ------------------------------------------------------------------ filters, from the model
def _related_entity(adapter, field):
    """The declared entity a foreign key points at, or None."""
    from ..adapter import real_model
    for other in adapter.entities:
        try:
            if real_model(adapter.model_for(other.key)) is field.related_model:
                return other
        except Exception:                                     # noqa: BLE001
            continue
    return None


def _related_path(adapter, field) -> str:
    """How a person names the record a foreign key points at: the declared entity's label, else
    the related model's own name/title/label, else -- said plainly -- its id."""
    other = _related_entity(adapter, field)
    if other is not None:
        return f"{field.name}__{other.label}"
    related = field.related_model
    own = {f.name for f in related._meta.concrete_fields}
    for name in _NAME_FIELDS:
        if name in own:
            return f"{field.name}__{name}"
    return f"{field.name}_id"


def _offered(entity, name: str) -> bool:
    """A field a filter or a dimension may touch: published, or declared as a dimension. A count
    over an unpublished column would read it one bucket at a time."""
    if name in entity.sensitive:
        return False
    return entity.shows(name) or name in entity.dimensions or name == SOFT_DELETE_FIELD


def filters_for(adapter, entity, model) -> dict:
    """``{key: (kind, path, vocabulary)}`` -- every exact filter this model supports.

    Deliberately narrow: booleans, declared choices and foreign keys are closed vocabularies a
    refusal can name back; free text goes through ``search``.
    """
    out = {}
    for field in model._meta.concrete_fields:
        if getattr(field, "primary_key", False) or not _offered(entity, field.name):
            continue
        if field.is_relation and field.many_to_one:
            out[field.name] = ("fk", _related_path(adapter, field), ())
        elif getattr(field, "choices", None):
            out[field.name] = ("choice", field.name, tuple(str(v) for v, _ in field.choices))
        elif field.get_internal_type() == "BooleanField":
            out[field.name] = ("bool", field.name, ("true", "false"))
    if entity.date_field:
        out["days"] = ("days", f"{entity.date_field}__gte", ())
    return out


def _describe(filters: dict) -> str:
    parts = []
    for key, (kind, path, vocab) in filters.items():
        if kind == "choice":
            parts.append(f"{key} ({'|'.join(vocab)}, or a list of them)")
        elif kind == "bool":
            parts.append(f"{key} (true|false)")
        elif kind == "fk":
            parts.append(f"{key} (by {path.split('__')[-1]})")
        else:
            parts.append(f"{key} (the last N days)")
    return ", ".join(parts) or "none on this kind"


def _as_bool(value):
    """True/False, or None -- which is a REFUSAL, not a default: a boolean that silently
    defaults inverts the question somebody asked."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "1", "on"):
        return True
    if text in ("false", "no", "0", "off"):
        return False
    return None


def clean_filter(filters: dict, model, entity, raw) -> tuple[dict, str]:
    """``(filter, error)``. Fails CLOSED: a key this model does not have, or a value outside its
    vocabulary, means the question asked is not the question answered -- and the answer is a
    number somebody acts on. Every refusal names what IS accepted."""
    if raw in (None, "", {}):
        return {}, ""
    if not isinstance(raw, dict):
        return {}, f"filter must be an object (got {type(raw).__name__}); keys: {_describe(filters)}"
    unknown = sorted(k for k in raw if k not in filters)
    if unknown:
        return {}, (f"{', '.join(unknown)} is not a filter on {entity.key}. It takes: "
                    f"{_describe(filters)}.")
    out = {}
    for key, value in raw.items():
        kind, path, vocab = filters[key]
        if value in (None, ""):
            continue                           # an explicitly empty value is "no filter"
        if kind == "bool":
            parsed = _as_bool(value)
            if parsed is None:
                return {}, f"{key} must be true or false (got {value!r})."
            out[key] = parsed
        elif kind == "choice":
            # A LIST means "any of these": the only way to say "open" on a model whose open is
            # several statuses and whose vocabulary has no word for the set.
            texts = [str(v) for v in (value if isinstance(value, (list, tuple)) else [value])]
            if not texts or any(t not in vocab for t in texts):
                return {}, (f"{key} must be one of {', '.join(vocab)}, or a list of them "
                            f"(got {value!r}).")
            out[key] = texts[0] if len(texts) == 1 else sorted(set(texts))
        elif kind == "fk":
            text = str(value).strip()
            # A value that names nothing would answer 0, which reads as "there is none" when the
            # truth is "you asked about something that does not exist".
            if not model.objects.filter(**{path: text}).exists():
                known = [v for v in model.objects.order_by().values_list(path, flat=True)
                         .distinct()[:VOCABULARY_CAP] if v not in (None, "")]
                return {}, (f"no {key} {text!r} is referenced by any {entity.key} you can see; "
                            f"known: {known}")
            out[key] = text
        else:                                                 # days
            try:
                out[key] = max(1, min(int(value), 3650))
            except (TypeError, ValueError):
                return {}, f"days must be a whole number of days (got {value!r})."
    return out, ""


def apply_filters(qs, filters: dict, clean: dict):
    from django.utils import timezone
    for key, value in clean.items():
        kind, path, _vocab = filters[key]
        if kind == "days":
            qs = qs.filter(**{path: timezone.now() - dt.timedelta(days=value)})
        elif isinstance(value, list):
            qs = qs.filter(**{path + "__in": value})
        else:
            qs = qs.filter(**{path: value})
    return qs


def live_rows(model, clean: dict) -> tuple:
    """``(queryset, excluded)``: the model's rows minus soft-deleted ones, unless ``clean`` names
    the flag. ``excluded`` is how many were left out (0 when the model has no flag or the caller
    asked about it), so a payload states its denominator instead of leaving a reader to guess."""
    qs = model.objects.all()
    if SOFT_DELETE_FIELD not in {f.name for f in model._meta.concrete_fields} \
            or SOFT_DELETE_FIELD in clean:
        return qs, 0
    live = qs.filter(**{SOFT_DELETE_FIELD: False})
    return live, qs.count() - live.count()


def _denominator(entity, deleted: int, clean: dict, verb: str = "counted") -> str:
    text = f"all {entity.key} {_whose(entity)}"
    narrowed = {k: v for k, v in clean.items() if k != "days"}
    if narrowed:
        text += " matching " + ", ".join(f"{k}={v}" for k, v in narrowed.items())
    if clean.get("days"):
        text += f" in the last {clean['days']} day(s)"
    if deleted:
        text += f" (live only; {deleted} deleted not {verb})"
    return text


# ------------------------------------------------------------------ one record, by its handle
def _looks_like_pk(model, needle: str) -> bool:
    kind = model._meta.pk.get_internal_type()
    if kind == "UUIDField":
        try:
            uuid.UUID(needle)
            return True
        except (ValueError, AttributeError, TypeError):
            return False
    return needle.isdigit() and kind in ("AutoField", "BigAutoField", "SmallAutoField",
                                         "IntegerField", "BigIntegerField")


def find_one(model, label: str, needle, *, loose=(), sample: int = VOCABULARY_CAP,
             noun: str = "records"):
    """``(obj, refusal, matched_by)`` for the ONE record a person named.

    Order: the label exactly (case-insensitive for text), then the primary key (so an id from any
    listing opens its record), then the label and the declared search fields loosely. At every
    step the matches are COUNTED in the database and more than one is a refusal naming each
    candidate WITH ITS ID -- never ``.first()``, never "matches 5" read off a five-row slice.
    ``obj`` None with an empty refusal means nothing matched; the caller words the absence.
    """
    from django.core.exceptions import ValidationError

    def safe(thunk):
        try:
            return thunk()
        except (ValueError, TypeError, ValidationError):
            return None           # a name against a UUID column cannot even be put to it

    needle = str(needle or "").strip()
    if not needle:
        return None, "", None
    try:
        textual = _is_text(model._meta.get_field(label))
    except Exception:                                         # noqa: BLE001
        textual = False
    base = model.objects

    def named(objs):
        return "; ".join(f"{getattr(o, label, '')} (id {o.pk})" for o in objs)

    exact = safe(lambda: base.filter(**{(f"{label}__iexact" if textual else label): needle}))
    n = safe(lambda: exact.count()) if exact is not None else None
    if n == 1:
        return exact.first(), "", f"its {label}" + (" (ignoring case)" if textual else "")
    if n and n > 1:
        return None, (f"{needle!r} is the {label} of {n} {noun}: {named(exact[:sample])}"
                      + (f" (and {n - sample} more)" if n > sample else "")
                      + f". They share that {label}; pass the id of the one you mean."), None
    if _looks_like_pk(model, needle):
        obj = safe(lambda: base.filter(pk=needle).first())
        if obj is not None:
            return obj, "", "its id"
    for name in (label,) + tuple(f for f in loose if f != label):
        qs = safe(lambda name=name: base.filter(**{f"{name}__icontains": needle}))
        n = safe(lambda: qs.count()) if qs is not None else None
        if not n:
            continue
        if n == 1:
            return qs.first(), "", f"part of its {name}"
        return None, (f"{needle!r} matches {n} {noun} by {name}: {named(qs[:sample])}"
                      + (f" (and {n - sample} more)" if n > sample else "")
                      + ". Name one exactly, or pass its id."), None
    return None, "", None


# ------------------------------------------------------------------ dimensions
def dimensions_for(adapter, entity, model) -> dict:
    """``{name: ORM path}``. Declared dimensions win; a relation among them groups by NAME
    (a declared ``register`` grouped by ``register_id`` let a live model answer "I cannot name
    the registers, the results gave only ids"). Undeclared: the model's published choice fields."""
    by_name = {f.name: f for f in model._meta.concrete_fields}
    names = entity.dimensions or tuple(
        f.name for f in model._meta.concrete_fields
        if getattr(f, "choices", None) and _offered(entity, f.name))
    out = {}
    for name in names:
        field = by_name.get(name)
        if field is not None and field.is_relation and field.many_to_one:
            out[name] = _related_path(adapter, field)
        else:
            out[name] = name
    return out


def build(adapter):
    from django.db.models import Count, Q

    if not adapter.entities:
        return {}, {}               # every entity was left out (see build_all.skipped)

    def resolve(args):
        key = str((args or {}).get("entity") or "")
        entity = adapter.entity(key)
        if entity is None:
            return None, None, {"error": f"no such kind of record {key!r}",
                                "kinds": list(adapter.entity_keys)}
        return entity, adapter.model_for(key), None

    def order(entity, qs):
        return qs.order_by(f"-{entity.date_field}") if entity.date_field else qs.order_by("-pk")

    def page(entity, qs, total, limit, deleted=0):
        rows = [_row(adapter, entity, o) for o in qs[:limit]]
        out = {"entity": entity.key, "rows": rows, "shown": len(rows), "total": total,
               "note": ("rows are a sample; use count for totals" if total > len(rows)
                        else "every match is shown")}
        if deleted:
            out["excluded_deleted"] = deleted
        return out

    def limit_of(args):
        try:
            return max(1, min(MAX_ROWS, int((args or {}).get("limit") or 10)))
        except (TypeError, ValueError):
            return 10

    def filtered(entity, model, args):
        """``(queryset, clean, deleted, error)`` for the call's own ``filter``."""
        filters = filters_for(adapter, entity, model)
        clean, error = clean_filter(filters, model, entity, (args or {}).get("filter"))
        if error:
            return None, None, 0, {"error": error}
        base, deleted = live_rows(model, clean)
        return apply_filters(base, filters, clean), clean, deleted, None

    def list_(args):
        entity, model, err = resolve(args)
        if err:
            return err
        qs, deleted = live_rows(model, {})
        qs = order(entity, qs)
        return page(entity, qs, qs.count(), limit_of(args), deleted)

    def search(args):
        entity, model, err = resolve(args)
        if err:
            return err
        text = str((args or {}).get("text") or "").strip()
        if not text:
            return {"error": "search needs text"}
        qs, clean, deleted, err = filtered(entity, model, args)
        if err:
            return err
        fields = entity.search or (entity.label,)
        cond = Q()
        for f in fields:
            cond |= Q(**{f"{f}__icontains": text})
        qs = order(entity, qs.filter(cond))
        result = page(entity, qs, qs.count(), limit_of(args), deleted)
        result["searched"] = list(fields)
        if clean:
            result["filter"] = clean
        return result

    def detail(args):
        entity, model, err = resolve(args)
        if err:
            return err
        args = args or {}
        ref = str(args.get("label") or args.get("id") or "").strip()
        if not ref:
            return {"error": f"detail needs the {entity.label} (or the id) of one {entity.key}"}
        obj, refusal, matched_by = find_one(model, entity.label, ref, loose=tuple(entity.search),
                                            noun=entity.key)
        if refusal:
            return {"error": refusal}
        if obj is None:
            seen = "" if _whose(entity) == "this app holds" else " among the ones you can see"
            return {"error": f"no {entity.key} with {entity.label} or id {ref!r}{seen}"}
        return {"entity": entity.key, "record": _row(adapter, entity, obj), "id": str(obj.pk),
                "matched_by": matched_by}

    def recent(args):
        entity, model, err = resolve(args)
        if err:
            return err
        if not entity.date_field:
            return {"error": f"{entity.key} declares no date_field, so 'recent' has no honest "
                             "meaning here; use list"}
        qs, clean, deleted, err = filtered(entity, model, args)
        if err:
            return err
        qs = order(entity, qs)
        result = page(entity, qs, qs.count(), limit_of(args), deleted)
        if clean:
            result["filter"] = clean
        return result

    def count(args):
        entity, model, err = resolve(args)
        if err:
            return err
        qs, clean, deleted, err = filtered(entity, model, args)
        if err:
            return err
        matched = qs.count()
        of = live_rows(model, clean)[0].count()
        out = {"entity": entity.key, "filter": clean, "count": matched, "of": of,
               "of_what": _denominator(entity, deleted, {}), "measured": True,
               "counted_by": "the database"}
        if of:
            out["share"] = round(matched / of, 4)
        else:
            out["note"] = f"no {entity.key} at all -- 0 here is 'no data', not 'none match'"
        if deleted:
            out["excluded_deleted"] = deleted
        return out

    def breakdown(args):
        entity, model, err = resolve(args)
        if err:
            return err
        dims = dimensions_for(adapter, entity, model)
        by = str((args or {}).get("by") or next(iter(dims), ""))
        if by not in dims:
            return {"error": f"cannot break {entity.key} down by {by!r}",
                    "dimensions": list(dims)}
        # `filter` is count's own vocabulary, so "open lines by team" is ONE call and every
        # group is a count OF the filtered set -- never a tally of everything beside a count of
        # the open ones, which a model then puts in one sentence.
        qs, clean, deleted, err = filtered(entity, model, args)
        if err:
            return err
        path = dims[by]
        rows = list(qs.order_by().values(path).annotate(n=Count("pk")).order_by("-n", path))
        total = sum(r["n"] for r in rows)
        out = {"entity": entity.key, "by": by, "filter": clean, "total": total,
               "groups": [{"value": _plain(r[path]), "count": r["n"],
                           "share": round(r["n"] / total, 4) if total else None} for r in rows],
               "of_what": _denominator(entity, deleted, clean),
               "note": f"{len(rows)} group(s); the groups sum to total ({total})",
               "counted_by": "the database"}
        if deleted:
            out["excluded_deleted"] = deleted
        return out

    ent = _entity_param(adapter)
    lim = {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}
    vocab = "; ".join(
        f"{e.key}: {_describe(filters_for(adapter, e, adapter.model_for(e.key)))}"
        for e in adapter.entities)
    flt = {"type": "object",
           "description": ("Exact filters from the model's own fields -- " + vocab + ". A choice "
                           "takes a LIST for 'any of these'. Soft-deleted rows are left out "
                           "unless the filter names is_deleted. An unknown key or value is "
                           "refused with the vocabulary, never ignored.")}

    def spec(desc, props, required):
        return (desc, {"type": "object", "properties": props, "required": required,
                       "additionalProperties": False})

    specs = {
        "list": spec("List records of one kind, newest first. Returns shown and total; rows "
                     "are a sample, never a total.", {"entity": ent, "limit": lim}, ["entity"]),
        "search": spec("Find records of one kind whose searchable fields contain the text, "
                       "optionally narrowed by `filter`.",
                       {"entity": ent, "text": {"type": "string"}, "filter": flt, "limit": lim},
                       ["entity", "text"]),
        "detail": spec("One record by the identifier a person uses for it, or its id. Several "
                       "sharing that identifier is refused with each one's id, never answered "
                       "with the first.",
                       {"entity": ent, "label": {"type": "string"},
                        "id": {"type": "string",
                               "description": "The record's id, when several share a label."}},
                       ["entity"]),
        "recent": spec("The most recently changed records of one kind, optionally filtered.",
                       {"entity": ent, "filter": flt, "limit": lim}, ["entity"]),
        "count": spec("How many records of one kind match `filter` (all, without one), counted "
                      "by the database, with the denominator and the share. The only tool that "
                      "gives totals.", {"entity": ent, "filter": flt}, ["entity"]),
        "breakdown": spec("Count records of one kind grouped by one dimension. `filter` takes "
                          "count's keys and narrows what is grouped, so 'open X by Y' is one "
                          "call; a relation groups by the related record's name.",
                          {"entity": ent, "by": {"type": "string"}, "filter": flt}, ["entity"]),
    }
    functions = {"list": list_, "search": search, "detail": detail, "recent": recent,
                 "count": count, "breakdown": breakdown}
    return specs, functions
