"""The belt against an answer that claims work the turn did not do.

Two failures seen within a day of two action lanes going live, and they are the same failure:

* asked for a dry run without a click, a model called the verb, was refused for the go-ahead,
  and answered that the job "would send 1 file". There is no file count in a refusal.
* a model said it had filed a request with the team having called no filing tool at all. The
  person walked away believing the team knew.

Neither is fixable by prompting: a model that has read a rich result narrates the result it
EXPECTED. A streamed turn cannot be un-streamed, so it is corrected in place -- the claim read
from the answer text, the truth read from the turn's own recorded trail -- and the correction is
the last thing the person reads.

**Everything here is pure.** Claims come from a string, truth from a list of dicts. That is what
lets a detector of model output have a case it MUST fire on.

**AND THE INPUTS NEED THEIR OWN COVERAGE.** The surviving mistakes are in the code that BUILDS
the trail, not in the predicate: stop recording denials and the belt goes blind on exactly the
bug it exists for. When you adopt this, exercise the recorder too.

    from assistant import belt
    fix = belt.correction(answer_text, steps)
    if fix:
        kind, note = fix       # append `note` to the answer and emit it before `done`

THE TRAIL SHAPE, built from the same events the person saw:

    {"kind": "result"|"denied", "tool": str, "ok": bool,
     "action": bool,          # does this tool CHANGE something
     "receipt": {...},        # ids the tool RETURNED: run_id, task, id...
     "duplicate_of": str,     # set when a filing folded into an existing one
     "reason": str}           # the gate's own words, on a denial

``TASK_ID`` is the shape of the id your filing tool returns; set ``belt.TASK_ID`` to your own
pattern (one capture group) if it is not ``<letters>-<digits>`` or ``#<digits>``.
"""
from __future__ import annotations

import re

# Past tense and present perfect ONLY. Deliberately NOT here: "I can run", "would deliver",
# "press confirm and I will". Offering to act and describing what a verb does are the assistant
# working correctly, and a belt that fires on those teaches people to skip the last paragraph
# of every answer -- the one place a real correction is printed.
_PERFORMED = re.compile(
    r"\b(?:i|i've|i have|we|we've)\s+(?:just\s+|now\s+)?"
    r"(?:ran|run|executed|performed|triggered|started|enabled|disabled|applied|"
    r"updated|created|deleted|removed|added|saved|imported|posted|sent|"
    r"self[- ]tested|dry[- ]ran|kicked off)\b"
    r"|\b(?:the\s+)?(?:route|job|run|import|change|update|task)\s+"
    r"(?:has\s+been|was|is|been)\s+"
    r"(?:run|completed|finished|executed|enabled|disabled|started|triggered|"
    r"applied|saved|created|deleted)\b"
    r"|\b(?:dry[- ]run|self[- ]test|run|import|update)\s+(?:completed|finished|succeeded)\b"
    r"|\bhas\s+been\s+(?:enabled|disabled|run|executed|applied|saved|created)\b"
    r"|\bis\s+now\s+(?:enabled|disabled|running|applied|saved|on|off)\b",
    re.I)

#: "I filed that for you." THE SUBJECT IS LOAD-BEARING: this is a claim about what THIS
#: assistant did in THIS turn, so it takes a first-person claim, a claim about the person's own
#: request, or a named id -- nothing else. Measured over real prose nobody wrote for a test, the
#: subject-less version matched only sentences describing somebody ELSE's filing ("since you
#: filed this", "the two problems you raised"), which an assistant reading a ticket corpus
#: quotes constantly -- and each one appended "nothing was filed" to a correct answer.
_FILED = re.compile(
    r"\b(?:i|i've|i have|we|we've)\s+(?:just\s+|now\s+|already\s+)?"
    r"(?:filed|logged|submitted|queued|raised)\b"
    r"|\byour\s+(?:request|ticket|issue|ask|change|enhancement|bug)"
    r"[^.?!\n]{0,60}\b(?:is|has\s+been|was|been)\s+(?:now\s+)?"
    r"(?:filed|logged|submitted|queued|raised|on the board|with the team)\b"
    r"|\b(?:filed|logged|submitted)\s+(?:as|under|with)\s+(?:task\s*)?#?[A-Za-z]*-?\d{2,}\b",
    re.I)

#: A measured outcome -- something only a real run reports. A refusal carries none of these,
#: so any of them after a refused action is invention.
_QUANTIFIED = re.compile(
    r"\b\d+\s+(?:of\s+\d+\s+)?(?:files?|rows?|records?|items?|lines?)\b"
    r"|\b(?:run|job|import)\s+#?\d+\b"
    r"|\b(?:delivered|uploaded|transferred|skipped|sent|created|updated|deleted)\s+\d+\b"
    r"|\b\d+\s+(?:were|was|would\s+be)\s+(?:delivered|uploaded|sent|skipped|created|updated)\b"
    r"|\bwould\s+(?:send|deliver|upload|create|update)\s+\d+(?:\s+\w+)?",
    re.I)

_RUN_ID = re.compile(r"\b(?:run|job|import)\s+#?(\d+)\b", re.I)
#: The id a filing returns. Override for your tracker's shape; keep ONE capture group.
TASK_ID = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,7}|#\d{2,7})\b")


def claims_performed(answer: str) -> str | None:
    match = _PERFORMED.search(answer or "")
    return match.group(0) if match else None


def claims_filed(answer: str) -> str | None:
    match = _FILED.search(answer or "")
    return match.group(0) if match else None


def claims_quantified(answer: str) -> str | None:
    match = _QUANTIFIED.search(answer or "")
    return match.group(0) if match else None


def ids_named(answer: str) -> dict:
    """Every id the answer points at, by kind. A right-sounding wrong id sends a person to
    somebody else's record, which is worse than no id at all."""
    return {"runs": {int(n) for n in _RUN_ID.findall(answer or "")},
            "tasks": set(TASK_ID.findall(answer or ""))}


def receipts(steps) -> list[dict]:
    """Action results that actually succeeded. A successful READ is NOT a receipt -- that is the
    likeliest false negative: the model looks the record up and narrates the action from it."""
    return [s for s in (steps or []) if isinstance(s, dict) and s.get("kind") == "result"
            and s.get("ok") and s.get("action")]


def filings(steps) -> list[dict]:
    """Results that came back with a task id -- the only proof of a filing."""
    out = []
    for step in steps or []:
        if not (isinstance(step, dict) and step.get("kind") == "result" and step.get("ok")):
            continue
        task = (step.get("receipt") or {}).get("task") or step.get("task")
        if task:
            out.append({**step, "task": task})
    return out


def refusals(steps) -> list[dict]:
    """Actions the gate denied, or that ran and failed."""
    return [s for s in (steps or []) if isinstance(s, dict) and s.get("action")
            and (s.get("kind") == "denied" or (s.get("kind") == "result" and not s.get("ok")))]


def receipt_answer(filed) -> str:
    """The acknowledgement for a turn's filings, rendered from the RECEIPT, never the model's
    prose. A repeat folded into an open request filed nothing NEW, and "filed as <old id>" there
    is a filing claim with no filing behind it."""
    new, folded = [], []
    for step in filed:
        task = str(step.get("task") or "")
        if task:
            (folded if step.get("duplicate_of") else new).append(task)
    new = list(dict.fromkeys(new))
    folded = [t for t in dict.fromkeys(folded) if t not in new]
    parts = []
    if new:
        parts.append("Filed as " + ", ".join(new) + ".")
    if folded:
        parts.append("Nothing new was filed: this is the same ask as " + ", ".join(folded)
                     + ", which is already open, so the new words were added to it.")
    return " ".join(parts) if parts else "The request was filed."


def correction(answer: str, steps) -> tuple[str, str] | None:
    """``(kind, note)`` for an answer the trail does not support, else None.

    The filing claim is checked first because it is the one a person is least able to verify:
    a wrong run id sends them to the wrong row, a filing that never happened sends them nowhere,
    for as long as they are willing to wait.
    """
    filed = filings(steps)
    if claims_filed(answer):
        if not filed:
            return ("unfiled", "Correction: nothing was filed in this turn -- no request came "
                               "back with an id, so there is nothing to follow. Ask me to file "
                               "it and I will.")
        real = {str(s.get("task")) for s in filed}
        wrong = ids_named(answer)["tasks"] - real
        if wrong:
            return ("misfiled", "Correction: " + receipt_answer(filed) + " Not "
                    + ", ".join(sorted(wrong)) + ".")

    got, refused = receipts(steps), refusals(steps)
    if not got:
        performed = claims_performed(answer)
        if performed:
            return ("performed", "Correction: nothing was run in this turn. No action returned "
                                 "a receipt, so “" + performed.strip() + "” did not "
                                 "happen. " + _what_now(refused))
        if refused:
            quantified = claims_quantified(answer)
            if quantified:
                return ("fabricated", "Correction: “" + quantified.strip() + "” is "
                                      "not a measured result -- the action did not run, so "
                                      "nothing counted. " + _what_now(refused))
        return None

    named = ids_named(answer)["runs"]
    real_runs = {(s.get("receipt") or {}).get("run_id") or s.get("run_id") for s in got}
    real_runs = {r for r in real_runs if isinstance(r, int)}
    wrong = {n for n in named if n not in real_runs}
    if wrong and real_runs:
        return ("misreported", "Correction: this turn produced "
                + ", ".join(f"run {r}" for r in sorted(real_runs))
                + ", not " + ", ".join(f"run {w}" for w in sorted(wrong)) + ".")
    return None


def _what_now(refused) -> str:
    if not refused:
        return "Ask again and the action will be offered with a confirm button."
    reason = str(refused[0].get("reason") or "").strip()
    if "confirmation" in reason.lower() or "go-ahead" in reason.lower():
        return ("It is waiting on your go-ahead: the action is offered with a confirm button, "
                "and it runs when you press it.")
    return reason or "The action did not complete."
