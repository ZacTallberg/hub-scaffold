"""Per-agent-run token/cost accounting over OTLP GenAI lines.

The cockpit's cost numbers derive from the SAME OTLP telemetry the workers emit
(hub_core.telemetry) — never a bespoke side-channel field. This module folds the
`invoke_agent` root spans into (run_id, model) usage rows, prices them with a fixed
Anthropic price map, attributes cost to tasks via gen_ai.conversation.id, and projects
cost-to-drain from the measured average.

Pricing (USD per 1M tokens, Anthropic first-party rates, cached 2026-08-02 from the
claude-api reference): cache reads bill at 0.1x input, cache writes (5m TTL) at 1.25x input.
A run whose model attribute is absent or unknown is counted but never priced — a fabricated
price is worse than a visible gap (the cost-unmeasured amber owns the gap).
"""
import json
from pathlib import Path

_MTOK = 1_000_000


def _rates(inp, out):
    return {"input": inp, "output": out, "cache_read": round(inp * 0.1, 4),
            "cache_write": round(inp * 1.25, 4)}


PRICE_MAP = {
    "claude-fable-5": _rates(10.00, 50.00),
    "claude-mythos-5": _rates(10.00, 50.00),
    "claude-opus-5": _rates(5.00, 25.00),
    "claude-opus-4-8": _rates(5.00, 25.00),
    "claude-opus-4-7": _rates(5.00, 25.00),
    "claude-opus-4-6": _rates(5.00, 25.00),
    "claude-sonnet-5": _rates(3.00, 15.00),
    "claude-sonnet-4-6": _rates(3.00, 15.00),
    "claude-haiku-4-5": _rates(1.00, 5.00),
}

# Per-run USD ceilings (the token-budget circuit breaker): a single run whose cumulative cost
# crosses its model's ceiling is a runaway loop, not a big task — the breaker kills it before it
# drains the month. RUN_BUDGET_CEILING_USD overrides every model (ops tuning / tests); an
# unknown or unpriced model has no ceiling — an unmeasured run cannot breach (the
# cost-unmeasured amber owns that gap).
CEILING_USD = {
    "claude-fable-5": 100.0,
    "claude-mythos-5": 100.0,
    "claude-opus-5": 50.0,
    "claude-opus-4-8": 50.0,
    "claude-opus-4-7": 50.0,
    "claude-opus-4-6": 50.0,
    "claude-sonnet-5": 30.0,
    "claude-sonnet-4-6": 30.0,
    "claude-haiku-4-5": 10.0,
}


def ceiling_for(model):
    import os
    raw = os.environ.get("RUN_BUDGET_CEILING_USD", "")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return CEILING_USD.get(model or "")


_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_ATTR_FOR = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cache_read_tokens": "gen_ai.usage.cache_read_input_tokens",
    "cache_write_tokens": "gen_ai.usage.cache_creation_input_tokens",
}
_READ_TAIL_LINES = 5000  # mirror telemetry.read_aggregate's bound on a long-lived file


def price_usd(model, usage):
    """Dollar cost for one usage row, or None when the model has no published rate."""
    rates = PRICE_MAP.get(model or "")
    if not rates:
        return None
    return round(
        usage.get("input_tokens", 0) * rates["input"] / _MTOK
        + usage.get("output_tokens", 0) * rates["output"] / _MTOK
        + usage.get("cache_read_tokens", 0) * rates["cache_read"] / _MTOK
        + usage.get("cache_write_tokens", 0) * rates["cache_write"] / _MTOK, 6)


def _attrmap(attr_list):
    return {a.get("key"): (a.get("value") or {}) for a in (attr_list or [])}


def _intval(v):
    try:
        return int(v.get("intValue"))
    except (AttributeError, TypeError, ValueError):
        return 0


def fold_costs(hub_dir):
    """Fold the OTLP lines into priced usage keyed on (run_id, model).

    run_id is the trace id — one worker CLI invocation is one trace. Returns
    {"by_run": {(run_id, model): {usage..., "cost_usd", "tasks": set}},
     "by_task": {task_id: {usage..., "cost_usd"|None, "priced": bool}},
     "total_cost_usd", "runs", "unpriced_runs"}."""
    by_run, by_task = {}, {}
    path = Path(hub_dir) / "telemetry" / "otlp.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()[-_READ_TAIL_LINES:] if path.is_file() else []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        for rs in obj.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    a = _attrmap(sp.get("attributes"))
                    if a.get("gen_ai.operation.name", {}).get("stringValue") != "invoke_agent":
                        continue
                    model = a.get("gen_ai.request.model", {}).get("stringValue") or None
                    task = a.get("gen_ai.conversation.id", {}).get("stringValue") or None
                    usage = {k: _intval(a.get(_ATTR_FOR[k])) for k in _USAGE_KEYS}
                    key = (sp.get("traceId", ""), model)
                    row = by_run.setdefault(key, {k: 0 for k in _USAGE_KEYS} | {"tasks": set()})
                    for k in _USAGE_KEYS:
                        row[k] += usage[k]
                    if task:
                        row["tasks"].add(task)
                        trow = by_task.setdefault(task, {k: 0 for k in _USAGE_KEYS} | {"models": set()})
                        for k in _USAGE_KEYS:
                            trow[k] += usage[k]
                        if model:
                            trow["models"].add(model)
    total, unpriced = 0.0, 0
    for (rid, model), row in by_run.items():
        row["cost_usd"] = price_usd(model, row)
        if row["cost_usd"] is None:
            unpriced += 1
        else:
            total += row["cost_usd"]
    for task, trow in by_task.items():
        models = trow.pop("models")
        if len(models) == 1:
            trow["cost_usd"] = price_usd(next(iter(models)), trow)
        else:
            # several models (or none) touched the task: price per-run precision is gone —
            # sum the runs that named this task instead of pricing the blended row.
            trow["cost_usd"] = None
            per = [r["cost_usd"] for r in by_run.values() if task in r["tasks"]]
            if per and all(c is not None for c in per):
                trow["cost_usd"] = round(sum(c for c in per), 6)
        trow["priced"] = trow["cost_usd"] is not None
        trow["measured"] = any(trow[k] for k in _USAGE_KEYS)
    return {"by_run": by_run, "by_task": by_task, "total_cost_usd": round(total, 6),
            "runs": len(by_run), "unpriced_runs": unpriced}


def measured_tasks(hub_dir):
    """Task ids with at least one token recorded against them — the guard's quiet set."""
    return {t for t, row in fold_costs(hub_dir)["by_task"].items() if row["measured"]}


# Prompt-cache health. The hit rate is cache_read / (cache_read + cache_write): of the tokens that
# went through the cacheable prefix, the share served FROM cache rather than re-written. A moved or
# broken prefix shows up here as writes replacing reads, which is the degradation worth paging on.
CACHE_HITRATE_FLOOR = 0.60
CACHE_WINDOW_RUNS = 20


def cache_hitrate(hub_dir, window=CACHE_WINDOW_RUNS):
    """Rolling cache hit rate over the most recent `window` runs, or None when nothing cacheable
    was recorded.

    None, never 0.0, on an empty window — and the distinction is the whole point. A rate of
    "0% hits out of 0 tokens" would recreate exactly that: a permanent alarm about a lane nobody
    feeds. An unmeasured lane is not a degraded one.
    """
    rows = list(fold_costs(hub_dir)["by_run"].values())[-window:]
    reads = sum(r.get("cache_read_tokens", 0) for r in rows)
    writes = sum(r.get("cache_write_tokens", 0) for r in rows)
    if reads + writes == 0:
        return None
    return {"hit_rate": round(reads / (reads + writes), 4), "cache_read_tokens": reads,
            "cache_write_tokens": writes, "runs": len(rows), "window": window}


def cost_block(hub_dir, state):
    """The cockpit block: cost-per-task and projected cost-to-drain, from OTLP + the board.

    Projection = average cost of MEASURED done tasks x tasks not yet terminal. None when
    nothing is measured yet — an unmeasured fleet projects nothing rather than a guess."""
    fold = fold_costs(hub_dir)
    tasks = state.get("by_type", {}).get("task", [])
    done_ids = {t["id"] for t in tasks if t.get("status") == "done"}
    remaining = sum(1 for t in tasks
                    if (t.get("status") or "todo") not in ("done", "dropped", "shadow"))
    measured_done = [row["cost_usd"] for tid, row in fold["by_task"].items()
                     if tid in done_ids and row["priced"] and row["measured"]]
    avg = round(sum(measured_done) / len(measured_done), 6) if measured_done else None
    projected = round(avg * remaining, 2) if avg is not None else None
    return {
        "total_cost_usd": fold["total_cost_usd"],
        "runs": fold["runs"],
        "unpriced_runs": fold["unpriced_runs"],
        "measured_done_tasks": len(measured_done),
        "avg_cost_per_done_task_usd": avg,
        "remaining_tasks": remaining,
        "projected_cost_to_drain_usd": projected,
        "source": "otlp",
    }
