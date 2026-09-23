"""What a failed CI job's own log SAYS, so the board never explains a failure away.

A pipeline's metadata — its trigger, its ref, its job name — explains why a job RAN. It never
explains why it FAILED, and when one job name fails for two different reasons, a metadata
explanation confidently covers the wrong one. The motivating case: a deploy verifier that runs
on pipelines with no deploy stage fails two ways that look identical from the job status —

- it stopped at the "is the host on this commit?" assertion before any phase ran (benign for
  that run: the host is simply on an earlier commit), or
- the deployed-commit check PASSED and then the app's own phases printed real failures.

A row that says "api-triggered" over the second kind teaches every reader that a whole app's
red is benign. So the trigger is a HINT and the log is the EVIDENCE: the tail of the failing
job's log is classified here, and the row says what was observed. When the log is empty or
unreadable the row says that too, and the hint stays a hint.

Provider-agnostic and standard-library only: the caller supplies the log text (a CI job's own
``after_script`` posting its tail, or a forwarder that fetched it). Markers are plain
regular expressions an adopter may extend to match the vocabulary their deploy scripts print.

Verdicts:

``rollback``      the deploy restored the previous release — the most serious thing a log says
``real``          the job ran and its own checks failed; ``detail`` names the first one
``not_deployed``  it stopped at the deployed-commit assertion; no phase of the job ran
``unclear``       it failed for a reason this cannot name (still a failure, never "fine");
                  ``detail`` is the first failure-shaped line, minus the runner's own epitaph
``unreadable``    there was no log text to read
"""

from __future__ import annotations

import re

#: The bytes of log tail worth reading. A failing step is near the end; a whole log is not
#: needed and a bounded read keeps a pathological log from costing the request.
TAIL_CHARS = 24000

ROLLBACK = re.compile(
    r"deploy_restored_previous|rolled back to the previous (?:commit|release)|"
    r"restored the previous (?:commit|release)|\bROLLBACK_(?:DONE|COMPLETE|APPLIED)\b", re.I)
# The deploy-verifier assertion that fires BEFORE any real phase: the host is not on this
# pipeline's commit, which is the normal state on a run that carries no deploy stage.
NOT_DEPLOYED = re.compile(
    r"deployed[ -]commit mismatch|DEPLOYED_COMMIT_MISMATCH|"
    r"is not (?:contained )?in what the host (?:serves|runs)|host is not on this commit", re.I)
# A real failure inside the job's own work: a phase table row, or a SCREAMING_FAILED marker.
PHASE_FAIL = re.compile(r"^\s*\[FAIL\]\s*(\S.*)$", re.M)
HARD_FAIL = re.compile(r"\b([A-Z][A-Z_]{4,})_FAILED\b")
# The first failure-shaped line of a log nothing above could classify.
FIRST_SIGNAL = re.compile(
    r"^.*?\b(?:FAIL(?:ED|URE)?|ERROR|Exception|Traceback|MISMATCH|AssertionError|"
    r"refused|denied|timed out)\b.*$", re.M | re.I)
# The runner's own epitaph says only that something failed, which the row already says.
EPITAPH = re.compile(r"^\s*(?:ERROR:\s*)?Job failed|cleaning up project directory", re.I)
_ANSI = re.compile("\x1b" + r"\[[0-9;]*m")
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z?\s*\S{0,4}\s*")

#: The severity each verdict earns on the operational stream. A rollback is critical whatever
#: the pipeline's own status said; a not_deployed stop says nothing about the app.
SEVERITY = {"rollback": "critical", "real": "error", "unclear": "error",
            "unreadable": "error", "not_deployed": "warning"}


def first_signal(text: str) -> str:
    """The first log line that looks like the failure itself, never the runner's epitaph."""
    for line in FIRST_SIGNAL.findall(str(text or "")):
        line = _STAMP.sub("", _ANSI.sub("", line).strip()).strip()
        if not line or EPITAPH.search(line):
            continue
        return line[:160]
    return ""


def classify_text(trace: str) -> dict:
    """``{"verdict", "detail"}`` for a job log (only its tail is read)."""
    text = str(trace or "")[-TAIL_CHARS:]
    if not text.strip():
        return {"verdict": "unreadable", "detail": ""}
    if ROLLBACK.search(text):
        return {"verdict": "rollback", "detail": "the deploy restored the previous release"}
    fails = PHASE_FAIL.findall(text)
    hard = HARD_FAIL.search(text)
    if fails or hard:
        first = fails[0].strip() if fails else hard.group(0)
        return {"verdict": "real", "detail": _ANSI.sub("", first)[:160]}
    if NOT_DEPLOYED.search(text):
        return {"verdict": "not_deployed",
                "detail": "stopped at the deployed-commit assertion before any phase ran"}
    return {"verdict": "unclear", "detail": first_signal(text)}


def note(verdict: dict, source: str = "", deployless: bool = False) -> str:
    """The phrase that rides the row. It states what was OBSERVED; it never says a failure is
    fine, and when the log could not be read the trigger stays a hint, named as one."""
    v = str((verdict or {}).get("verdict") or "unreadable")
    detail = str((verdict or {}).get("detail") or "")
    if v == "rollback":
        return "the deploy ROLLED BACK: the host was restored to the previous release"
    if v == "real":
        return ("the job ran and failed its own checks: %s" % detail) if detail \
            else "the job ran and failed its own checks"
    if v == "not_deployed":
        if source and deployless:
            return ("%s-triggered: it stopped at the deployed-commit assertion before any phase "
                    "ran, so this run says nothing about the app" % source)
        return "it stopped at the deployed-commit assertion before any phase ran"
    if v == "unclear":
        return ("first failure in the log: %s" % detail) if detail \
            else "failed for a reason the log does not name"
    if source and deployless:
        return ("%s-triggered: this run has no deploy stage, so a deploy/verify job here may be "
                "reporting on the previously deployed commit — read the log before trusting "
                "that" % source)
    return "no log was supplied, so the cause is unnamed — read the job log"


def classify(trace: str, source: str = "", deployless: bool = False) -> dict:
    """Classify a failed job's log: verdict, detail, the row's note and its severity."""
    out = classify_text(trace)
    out["note"] = note(out, source=source, deployless=deployless)
    out["severity"] = SEVERITY.get(out["verdict"], "error")
    return out
