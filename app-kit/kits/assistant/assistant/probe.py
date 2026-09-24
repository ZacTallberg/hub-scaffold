"""Drive the REAL model through the things an assistant must do, and print what it did.

A mock verifies the renderer; it cannot see TOOL-SKIP -- a model answering from its own head
instead of calling the tool that exists -- which is the measured failure mode of every mid-size
model. A suite stays green because the tool exists, not because the model calls it. This
harness has found, each time on a real model and each time with every tool check passing:

* an assistant narrating a dry run it had been REFUSED,
* a model interrogating a person for details instead of filing their request,
* a filing claimed with no id behind it.

All three were PROMPT defects, which is why this is the real operation to run whenever the
prompt or the catalog changes. It is a harness, not a suite: the cases are the task's receipt,
printed for a person to read, and they are not kept as a standing battery.

    from assistant.probe import Probe, Case

    Probe(
        ask=my_ask_function,                  # (question, **kw) -> TurnResult
        cases=[
            Case("a capability the app lacks", "we need X and this app doesn't do it",
                 expect_called="file_app_request"),
            Case("an action with no click", "run the thing",
                 expect_called="run_thing", expect_denied="confirmation"),
        ],
        stubbed=("who is asking: a fixed test actor",),
    ).run()

WHAT MAY BE STUBBED, and it is NAMED in the output: who is asking, and any write to a system
outside this app. Everything the probe is about -- the model, the tools, the gate, the belt,
the database -- is real, or the run proves nothing.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class TurnResult:
    """What one real turn did, as the person saw it."""

    text: str = ""
    calls: list = field(default_factory=list)
    denials: list = field(default_factory=list)
    corrections: list = field(default_factory=list)
    receipts: list = field(default_factory=list)
    seconds: float = 0.0
    captured: dict = field(default_factory=dict)


@dataclass
class Case:
    """One thing the assistant must actually do, asked the way a person asks.

    (Not a routing lane: :class:`assistant.canon.Lane` is a kind of turn. This is one probe
    case.)"""

    name: str
    question: str
    #: A tool that MUST be called -- the most valuable assertion here; tool-skip is invisible
    #: to everything else.
    expect_called: str = ""
    #: A substring that must appear in a denial's reason (refused for the go-ahead, not outright).
    expect_denied: str = ""
    expect_not_called: str = ""
    #: A belt correction that must fire, by kind.
    expect_correction: str = ""
    #: ``(TurnResult) -> True | str``; a string means "failed, and here is why".
    expect: Callable | None = None
    kw: dict = field(default_factory=dict)


class Probe:
    def __init__(self, ask: Callable, cases, *, stubbed=()):
        self.ask = ask
        self.cases = list(cases)
        self.stubbed = tuple(stubbed)

    def run(self, *, verbose: bool = True) -> dict:
        verdicts = []
        if not self.cases:
            # A probe of nothing reports nothing held -- never "0/0 held", which reads as clean.
            raise ValueError("a probe with no cases measures nothing")
        if verbose and self.stubbed:
            print("STUBBED (everything else is real): " + "; ".join(self.stubbed))
        for case in self.cases:
            started = time.monotonic()
            try:
                turn = self.ask(case.question, **case.kw)
            except Exception as exc:                          # noqa: BLE001
                turn = TurnResult(text=f"({type(exc).__name__}: {exc})")
            turn.seconds = round(time.monotonic() - started, 1)
            if verbose:
                print(f"\n--- {case.name} ({turn.seconds}s) ---")
                print(f"  called: {turn.calls}")
                if turn.denials:
                    print(f"  denied: {[d[0] for d in turn.denials]}")
                for line in (turn.text or "").splitlines():
                    if line.strip():
                        print("  " + line[:160])
                for note in turn.corrections:
                    print(f"  CORRECTION: {str(note)[:150]}")
            verdicts.extend(self._judge(case, turn))
        held = sum(1 for _, ok, _ in verdicts if ok)
        if verbose:
            print("\n================ verdict ================")
            for label, ok, why in verdicts:
                print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {why}" if why and not ok else ""))
            print(f"{held}/{len(verdicts)} held")
        return {"held": held, "total": len(verdicts), "stubbed": list(self.stubbed),
                "verdicts": [{"what": v[0], "ok": v[1], "why": v[2]} for v in verdicts]}

    @staticmethod
    def _judge(case, turn) -> list[tuple[str, bool, str]]:
        out = []
        if case.expect_called:
            out.append((f"{case.name}: calls {case.expect_called}",
                        case.expect_called in turn.calls,
                        f"called {turn.calls or 'nothing'} instead"))
        if case.expect_not_called:
            out.append((f"{case.name}: does not call {case.expect_not_called}",
                        case.expect_not_called not in turn.calls, "it called it"))
        if case.expect_denied:
            out.append((f"{case.name}: refused for {case.expect_denied}",
                        any(case.expect_denied.lower() in str(d[1] or "").lower()
                            for d in turn.denials),
                        f"denials were {[d[1] for d in turn.denials] or 'none'}"))
        if case.expect_correction:
            out.append((f"{case.name}: the belt fires ({case.expect_correction})",
                        any(case.expect_correction in str(c) for c in turn.corrections),
                        "no correction was emitted"))
        if case.expect:
            verdict = case.expect(turn)
            out.append((f"{case.name}: {getattr(case.expect, '__doc__', '') or 'holds'}",
                        verdict is True, verdict if isinstance(verdict, str) else ""))
        if not out:
            out.append((f"{case.name}: states an expectation", False,
                        "this case asserts nothing, so it cannot hold or fail"))
        return out


def sse_turn(raw: str) -> TurnResult:
    """Parse a streamed turn (event: token|tool|denied|correction|tool_result) into a result."""
    turn = TurnResult()
    for frame in (raw or "").split("\n\n"):
        name, data = "message", ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if not data:
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if name == "token":
            turn.text += payload.get("text", "")
        elif name == "tool":
            turn.calls.append(payload.get("tool"))
        elif name == "denied":
            turn.denials.append((payload.get("tool"), payload.get("reason")))
        elif name == "correction":
            turn.corrections.append(payload.get("note") or payload.get("kind"))
        elif name == "tool_result" and payload.get("ok"):
            turn.receipts.append({k: v for k, v in payload.items()
                                  if k in ("tool", "task", "run_id", "duplicate_of")})
    return turn
