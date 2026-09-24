# assistant — what your app must supply

## Settings

- `ASSISTANT_TOOLS_MODULE` -- the module whose `tool_names()` / `build_registry()` / `SPECS` is the live registry (else `<app>.agent_tools` is tried).
- `ASSISTANT_GAPS` -- `{family: reason}` for every family this app deliberately does not have.

## Passed in at call time (never imported by the kit)

- `ask` -- your JSON completion: `await ask(messages, json_schema=..., schema_name=..., max_tokens=..., temperature=...)`.
- `loop_fn` -- your tool loop, for `routing.run_route`.
- `register` -- `register(name, description, parameters, fn)` onto your registry, for `families.register_all`.
