"""OTel GenAI-semconv telemetry for worker runs + the receipt gate (ADR-0009 interop edge).

One worker CLI invocation = one trace, in the GenAI semantic-conventions shape:
  - root span  `invoke_agent <agent>`  (gen_ai.operation.name=invoke_agent, gen_ai.agent.name,
    gen_ai.conversation.id=<task id>), carrying gen_ai.usage.input_tokens/output_tokens when the
    harness exports GEN_AI_USAGE_INPUT_TOKENS / GEN_AI_USAGE_OUTPUT_TOKENS (omitted, never
    fabricated, when unknown);
  - child spans `execute_tool <tool>`  (gen_ai.operation.name=execute_tool, gen_ai.tool.name) for
    every hub API call and the out-of-band verification run — the receipt gate's submission is
    the `/hub/api/complete` tool span, error.type carrying the refusal code on a non-200.

Transport: OTLP/JSON lines appended to <hub_dir>/telemetry/otlp.jsonl — each line one OTLP
export request ({"resourceSpans": ...} or {"resourceMetrics": ...}, spec field names), so any
OTLP-speaking backend can ingest the file verbatim. Metrics: gen_ai.client.token.usage (sum,
gen_ai.token.type=input|output) and gen_ai.client.operation.duration (seconds).

The EMIT side needs opentelemetry-sdk and degrades to a no-op without it; the READ side
(`read_aggregate`, the cockpit's cost/latency source) is pure JSON over the OTLP lines — the
cockpit numbers are derived from the OTLP data, never from a bespoke side-channel field.
"""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from hub_core import identity

try:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    _SDK = True
except ImportError:  # emission is optional; reading never needs the SDK
    _SDK = False

SCOPE = f"{identity.load()['key']}.hub.worker"
_SERVICE = f"{identity.load()['key']}-hub-worker"
USAGE_ENV = ("GEN_AI_USAGE_INPUT_TOKENS", "GEN_AI_USAGE_OUTPUT_TOKENS")


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


class RunTelemetry:
    """One worker run's trace + metrics; capture in memory, encode OTLP/JSON on flush()."""

    def __init__(self, agent, hub_dir):
        self.agent = agent
        self.hub_dir = Path(hub_dir)
        self.enabled = _SDK
        self._duration_s = None
        if not _SDK:
            return
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider(resource=Resource.create({"service.name": _SERVICE}))
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer(SCOPE)

    @contextmanager
    def root(self, operation, task_id=None):
        if not self.enabled:
            yield None
            return
        t0 = time.monotonic()
        attrs = {"gen_ai.operation.name": "invoke_agent",
                 "gen_ai.agent.name": self.agent,
                 "hub.action": operation}
        if task_id:
            attrs["gen_ai.conversation.id"] = task_id
        # Model + cache tokens ride the same omitted-never-fabricated rule as input/output usage:
        # the cost fold (hub_core.cost) prices a run only from what the harness actually exported.
        model = os.environ.get("GEN_AI_REQUEST_MODEL", "")
        if model:
            attrs["gen_ai.request.model"] = model
        for env, key in zip(
                USAGE_ENV + ("GEN_AI_USAGE_CACHE_READ_TOKENS", "GEN_AI_USAGE_CACHE_CREATION_TOKENS"),
                ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
                 "gen_ai.usage.cache_read_input_tokens", "gen_ai.usage.cache_creation_input_tokens")):
            raw = os.environ.get(env, "")
            if raw.isdigit():
                attrs[key] = int(raw)
        with self.tracer.start_as_current_span(f"invoke_agent {self.agent}", attributes=attrs) as span:
            try:
                yield span
            finally:
                self._duration_s = time.monotonic() - t0

    @contextmanager
    def tool(self, name, **attrs):
        if not self.enabled:
            yield None
            return
        base = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name}
        base.update(attrs)
        with self.tracer.start_as_current_span(f"execute_tool {name}", attributes=base) as span:
            yield span

    def spans(self):
        return self.exporter.get_finished_spans() if self.enabled else ()

    def flush(self):
        """Append this run's OTLP/JSON trace + metrics lines; a no-op run writes nothing."""
        if not self.enabled or not self.spans():
            return None
        out = self.hub_dir / "telemetry" / "otlp.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(self._trace_line(), separators=(",", ":")) + "\n")
            metrics = self._metrics_line()
            if metrics:
                f.write(json.dumps(metrics, separators=(",", ":")) + "\n")
        return out

    def _trace_line(self):
        spans = []
        for s in self.spans():
            ctx, parent = s.get_span_context(), s.parent
            spans.append({
                "traceId": format(ctx.trace_id, "032x"),
                "spanId": format(ctx.span_id, "016x"),
                "parentSpanId": format(parent.span_id, "016x") if parent else "",
                "name": s.name,
                "kind": 1,  # SPAN_KIND_INTERNAL
                "startTimeUnixNano": str(s.start_time),
                "endTimeUnixNano": str(s.end_time),
                "attributes": [_attr(k, v) for k, v in dict(s.attributes or {}).items()],
                "status": {"code": 1 if (s.status is None or s.status.is_ok) else 2},
            })
        return {"resourceSpans": [{
            "resource": {"attributes": [_attr("service.name", _SERVICE)]},
            "scopeSpans": [{"scope": {"name": SCOPE}, "spans": spans}],
        }]}

    def _metrics_line(self):
        points = []
        for env, ttype in zip(USAGE_ENV, ("input", "output")):
            raw = os.environ.get(env, "")
            if raw.isdigit():
                points.append({"asInt": raw, "attributes": [_attr("gen_ai.token.type", ttype)],
                               "timeUnixNano": str(time.time_ns())})
        metrics = []
        if points:
            metrics.append({"name": "gen_ai.client.token.usage", "unit": "{token}",
                            "sum": {"aggregationTemporality": 1, "isMonotonic": True,
                                    "dataPoints": points}})
        if self._duration_s is not None:
            metrics.append({"name": "gen_ai.client.operation.duration", "unit": "s",
                            "gauge": {"dataPoints": [{
                                "asDouble": round(self._duration_s, 6),
                                "attributes": [_attr("gen_ai.operation.name", "invoke_agent"),
                                               _attr("gen_ai.agent.name", self.agent)],
                                "timeUnixNano": str(time.time_ns())}]}})
        if not metrics:
            return None
        return {"resourceMetrics": [{
            "resource": {"attributes": [_attr("service.name", _SERVICE)]},
            "scopeMetrics": [{"scope": {"name": SCOPE}, "metrics": metrics}],
        }]}


class _NoopTelemetry:
    """The shape of RunTelemetry with nothing behind it (SDK absent or telemetry off)."""
    enabled = False

    @contextmanager
    def root(self, operation, task_id=None):
        yield None

    @contextmanager
    def tool(self, name, **attrs):
        yield None

    def flush(self):
        return None


NOOP = _NoopTelemetry()

_READ_TAIL_LINES = 5000   # snapshot cost: bound the per-request read of a long-lived file


def _attrmap(attr_list):
    return {a.get("key"): (a.get("value") or {}) for a in (attr_list or [])}


def read_aggregate(hub_dir):
    """The cockpit's cost/latency source: aggregate the OTLP lines (pure JSON, no SDK).
    {runs, input_tokens, output_tokens, p50_run_ms, source:'otlp'} — zeros when no file."""
    agg = {"runs": 0, "input_tokens": 0, "output_tokens": 0, "p50_run_ms": None, "source": "otlp"}
    path = Path(hub_dir) / "telemetry" / "otlp.jsonl"
    if not path.is_file():
        return agg
    durations = []
    lines = path.read_text(encoding="utf-8").splitlines()[-_READ_TAIL_LINES:]
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        for rs in obj.get("resourceSpans", []):
            for ss in rs.get("scopeSpans", []):
                for sp in ss.get("spans", []):
                    a = _attrmap(sp.get("attributes"))
                    if a.get("gen_ai.operation.name", {}).get("stringValue") == "invoke_agent":
                        agg["runs"] += 1
                        try:
                            durations.append((int(sp["endTimeUnixNano"]) - int(sp["startTimeUnixNano"])) / 1e6)
                        except (KeyError, ValueError):
                            pass
        for rm in obj.get("resourceMetrics", []):
            for sm in rm.get("scopeMetrics", []):
                for m in sm.get("metrics", []):
                    if m.get("name") != "gen_ai.client.token.usage":
                        continue
                    for dp in (m.get("sum") or {}).get("dataPoints", []):
                        ttype = _attrmap(dp.get("attributes")).get("gen_ai.token.type", {}).get("stringValue")
                        try:
                            n = int(dp.get("asInt", 0))
                        except (TypeError, ValueError):
                            continue
                        if ttype == "input":
                            agg["input_tokens"] += n
                        elif ttype == "output":
                            agg["output_tokens"] += n
    if durations:
        durations.sort()
        agg["p50_run_ms"] = round(durations[len(durations) // 2], 1)
    return agg
