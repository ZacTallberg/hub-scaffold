"""What an app declares, so the kit's families can serve it without knowing its schema.

    # budget/assistant_adapter.py
    from assistant.adapter import Adapter, Entity, Surface

    ADAPTER = Adapter(
        app="budget-app",
        entities=[
            Entity("lines", model="budget.BudgetLine", label="code",
                   search=("code", "title"), dimensions=("team", "status"),
                   public_fields=("code", "title", "team", "status", "amount", "updated_at"),
                   date_field="updated_at", detail_url="budget:line"),
        ],
        surfaces=[Surface("budget:home", "Every budget line and where it stands",
                          ("the list", "home", "where do I start"))],
    )

An app writes ONE adapter and gets the generic families; it writes its own tools only for what
is genuinely its own. Every field below exists because some family cannot be written without
it, and a family that needs something new adds a field HERE rather than reaching into the app.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field


# -- WHOSE rows ---------------------------------------------------------------------------------
# Every generic family reads ``model.objects``. Without a per-actor scope, a per-user app's
# conversations, uploads, saved prompts and feedback were listed, counted, searched and opened
# for ANY signed-in person -- measured on a shipped app, across ~40 generic tools, the day the
# scope below was added. A scope is declared ONCE, on the Entity, and every family that resolves
# the model through the adapter inherits it.
class _ScopedManager:
    """``model.objects`` for a scoped entity: every read starts from the SCOPED queryset. A scope
    that raises, or returns None, reads as NO rows -- a scope failure must never widen to the
    whole table."""

    def __init__(self, model, scope):
        self._model, self._scope = model, scope

    def get_queryset(self):
        base = self._model._default_manager.all()
        try:
            scoped_qs = self._scope(base)
        except Exception:                                     # noqa: BLE001
            return base.none()
        return base.none() if scoped_qs is None else scoped_qs

    def all(self):
        return self.get_queryset()

    def __getattr__(self, name):
        return getattr(self.get_queryset(), name)


class ScopedModel:
    """A model seen through its entity's ``scope``: ``.objects`` / ``._default_manager`` are the
    scoped rows; everything else (``_meta``, ``DoesNotExist``, field access) is the real model's."""

    def __init__(self, model, scope):
        self._model, self._scope = model, scope

    @property
    def objects(self):
        return _ScopedManager(self._model, self._scope)

    @property
    def _default_manager(self):
        return _ScopedManager(self._model, self._scope)

    @property
    def unscoped(self):
        return self._model

    def __getattr__(self, name):
        return getattr(self._model, name)

    def __eq__(self, other):
        return real_model(other) is self._model

    def __hash__(self):
        return hash(self._model)

    def __repr__(self):
        return f"<scoped {self._model._meta.label}>"


def real_model(model):
    """The Django model behind a possibly-scoped one (for identity comparisons)."""
    return model.unscoped if isinstance(model, ScopedModel) else model


def SHARED(qs):
    """``Entity(scope=SHARED)``: this noun is deliberately app-wide -- every signed-in person may
    see every row (a shared queue, a catalogue). Saying so is REQUIRED for an entity that has an
    owner column; see :func:`unscoped_owner`."""
    return qs


def scoped(model, scope):
    return model if (model is None or scope is None or scope is SHARED) else ScopedModel(model, scope)


#: Column names that mean "this row belongs to someone". An entity carrying one and declaring no
#: ``scope`` would let every generic family show other people's rows.
OWNER_FIELDS = ("owner", "user", "username", "principal", "created_by", "requested_by", "actor",
                "author", "user_label")


def unscoped_owner(entity, model) -> str:
    """The owner column of a per-user entity that declares no scope, or ""."""
    if getattr(entity, "scope", None) is not None:
        return ""
    names = {f.name for f in model._meta.get_fields()}
    return next((f for f in OWNER_FIELDS if f in names), "")


@dataclass(frozen=True)
class Entity:
    """One first-class noun this app holds."""

    #: What people call it, plural and lower case: "lines", "requests", "accounts".
    key: str
    #: "app_label.ModelName", resolved lazily so an adapter can be imported early.
    model: str
    #: The field that IDENTIFIES one to a person -- a code, a slug, a name.
    label: str
    search: tuple[str, ...] = ()
    #: Fields offered as a breakdown dimension. Empty: the model's choice fields.
    dimensions: tuple[str, ...] = ()
    detail_url: str = ""
    list_url: str = ""
    means: str = ""
    # -- the credential boundary --------------------------------------------------------------
    # A generic family that emits a record's concrete fields would publish a secret column the
    # day somebody declares that entity. A name screen ("anything called *secret*") is
    # EXCLUSION-shaped and wrong the moment a field is called ``sealed_blob``. So declare what
    # may be shown: INCLUSION, and anything not named is dropped -- a field added later
    # defaults to invisible.
    public_fields: tuple[str, ...] = ()
    #: Never published whatever else says -- the two lists are edited by different people at
    #: different times.
    sensitive: tuple[str, ...] = ()
    public_keys: tuple[str, ...] = ()
    # -- what a family would otherwise have to GUESS -------------------------------------------
    date_field: str = ""
    columns: tuple[str, ...] = ()
    #: True when ``label`` is an internal id, not something a person can look up: a handle is
    #: internal, a citation is public and openable.
    label_is_handle: bool = False
    detail_url_arg: str = "label"
    ranges: tuple[str, ...] = ()
    #: WHOSE rows: ``(queryset) -> queryset`` narrowing to what the current actor may see,
    #: evaluated on every read. REQUIRED for a per-user noun; it must fail CLOSED
    #: (``qs.none()``) when there is no actor. ``SHARED`` declares a deliberately app-wide noun.
    scope: Callable | None = None

    def shows(self, field_name: str) -> bool:
        """Whether a tool may publish this flat field. Inclusion, then the veto. With no
        ``public_fields`` declared nothing but the label is shown -- the safe default."""
        if field_name in self.sensitive:
            return False
        if not self.public_fields:
            return field_name == self.label
        return field_name in self.public_fields

    def keys_for(self, adapter_keys: tuple[str, ...]) -> tuple[str, ...]:
        return self.public_keys or adapter_keys


@dataclass(frozen=True)
class Surface:
    """One page, described by what a person would ASK for rather than its name."""

    url_name: str
    purpose: str
    cues: tuple[str, ...] = ()
    per_entity: bool = False


@dataclass(frozen=True)
class Adapter:
    """Everything the generic families need to know about one app."""

    app: str
    entities: Sequence[Entity] = ()
    surfaces: Sequence[Surface] = ()
    #: Operational switches, ``{name: value}``: a disabled thing must never be reported as a
    #: broken one, so include every switch that can make the app look broken while working.
    toggles: Callable[[], dict] | None = None
    #: Bounded self-checks, ``{name: callable -> {"ok": bool, "detail": str}}``.
    reachability: dict[str, Callable[[], dict]] = field(default_factory=dict)
    audit_model: str = ""
    #: ``(queryset) -> queryset`` narrowing the audit log to what the actor may see: the audit
    #: log of a per-user app holds everyone's activity.
    audit_scope: Callable | None = None
    audit_fields: dict = field(default_factory=lambda: {
        "at": "at", "actor": "actor", "action": "action", "detail": "detail"})
    #: The app's own rules, ``{topic: {"question": ..., "rule": ...}}`` -- cited, never recited.
    rules: dict = field(default_factory=dict)
    #: How this app counts, ``{metric: {"counts": ..., "does_not_count": ...}}``.
    metrics: dict = field(default_factory=dict)
    public_keys: tuple[str, ...] = ()

    def model_for(self, key: str):
        """The entity's model, SCOPED when the entity declares a ``scope``."""
        from django.apps import apps
        entity = self.entity(key)
        return scoped(apps.get_model(entity.model), entity.scope) if entity else None

    def audit_model_class(self):
        """The audit model, scoped by ``audit_scope``; None when not declared."""
        if not self.audit_model:
            return None
        from django.apps import apps
        return scoped(apps.get_model(self.audit_model), self.audit_scope)

    def entity(self, key: str) -> Entity | None:
        return next((e for e in self.entities if e.key == key), None)

    @property
    def entity_keys(self) -> tuple[str, ...]:
        return tuple(e.key for e in self.entities)


def check(adapter: Adapter) -> list[str]:
    """Every problem with an adapter, in the words its author needs -- at BUILD, so a bad
    adapter fails with a sentence rather than at the first question with an AttributeError."""
    from django.apps import apps

    problems = []
    if not adapter.app:
        problems.append("the adapter needs an `app` slug")
    seen = set()
    for entity in adapter.entities:
        if entity.key in seen:
            problems.append(f"two entities are both called {entity.key!r}")
        seen.add(entity.key)
        try:
            model = apps.get_model(entity.model)
        except Exception as exc:                              # noqa: BLE001
            problems.append(f"{entity.key}: {entity.model!r} is not a model ({exc})")
            continue
        names = {f.name for f in model._meta.get_fields()}
        if entity.label not in names:
            problems.append(f"{entity.key}: label {entity.label!r} is not a field on "
                            f"{entity.model} -- it identifies one to a person, so it must exist")
        if entity.detail_url_arg not in ("label", "pk"):
            problems.append(f"{entity.key}: detail_url_arg must be 'label' or 'pk'")
        owner = unscoped_owner(entity, model)
        if owner:
            problems.append(
                f"UNSCOPED: {entity.key} has an owner column ({owner!r}) and declares no `scope`, "
                "so every generic family would show every user's rows. Declare "
                "scope=<(queryset) -> the actor's rows>, or scope=SHARED if it really is app-wide.")
        for extra in (*entity.search, *entity.dimensions, *entity.public_fields,
                      *entity.sensitive, *entity.columns, *entity.ranges,
                      *((entity.date_field,) if entity.date_field else ())):
            if extra.split("__")[0] not in names:
                problems.append(f"{entity.key}: {extra!r} is not a field on {entity.model}")
    return problems
