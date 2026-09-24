"""The example's assistant registry: the kit's core family over the declared entities.

``python -m assistant.audit`` (with DJANGO_SETTINGS_MODULE=config.settings) counts THIS registry
against the canon; the families this app does not have are declared in settings.ASSISTANT_GAPS,
each with its reason.
"""
from assistant import families
from assistant.adapter import Adapter, Entity, Surface

ADAPTER = Adapter(
    app="budget-app",
    entities=[
        Entity("lines", model="budget.BudgetLine", label="code",
               search=("code", "title", "team"), dimensions=("team", "status"),
               date_field="updated_at", detail_url="budget:line",
               public_fields=("code", "title", "team", "status", "planned", "actual",
                              "updated_at"),
               means="a planned amount for one team, with the actual once it is measured"),
    ],
    surfaces=[Surface("budget:home", "Every open budget line and where it stands",
                      ("the list", "home", "over budget"))],
    rules={"archive": {"question": "what does archive do?",
                       "rule": "Archiving hides an over-budget line from the open list; it is "
                               "reversible and recorded in the line's history."}},
)

REGISTRY: dict = {}


def build_registry() -> dict:
    """``{name: (description, parameters, fn)}`` -- a plain dict registry for the example."""
    if not REGISTRY:
        specs, functions = families.build_all(ADAPTER)

        def register(name, description, parameters, fn):
            REGISTRY[name] = (description, parameters, fn)

        families.register_all(specs, functions, register)
    return REGISTRY


def tool_names() -> list[str]:
    return sorted(build_registry())


def invoke(name: str, args: dict):
    """Run one tool. Reads only: every tool here comes from the core family."""
    return build_registry()[name][2](args)
