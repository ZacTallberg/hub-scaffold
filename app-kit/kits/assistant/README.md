# assistant — the Assistant Canon, and the machinery that holds an app's assistant to it

An in-app assistant is measured by the questions it can answer. The failure this kit exists to
prevent is not a bad tool — it is a MISSING one nobody counted. An assistant shipped with eleven
read-only lookups was called useless by the person it was built for; it was rebuilt to two dozen
tools and reported done while its sibling had over a hundred. Nothing was broken. It was not
finished, and no test could have said so. **A count could.**

## What is here

| module | what it is |
|---|---|
| `canon.py` | the 64 families as data: 32 read out of shipped assistants, 32 DERIVED from the question grid (record · rule · person · time · organisation × what is · was · will be · what if · why this one · …), each with the guarantee it owes and the lanes that answer it |
| `audit.py` | `python -m assistant.audit` — counts the app's LIVE registry against the canon; exits non-zero on an undeclared gap; prints the remedy beside each gap and which routing lanes the app staffs |
| `adapter.py` | the contract an app writes once (entities, surfaces, toggles, rules) to get the generic families; fields are published by **inclusion** |
| `families/` | the generic families this copy ships (`core`: list, search, detail, recent, count, breakdown — rows always carry `shown` and `total`) and the registrar the audit reads |
| `routing.py` | the orchestrator/specialist split: one cheap JSON call picks the LANES, each lane runs as a bounded loop over its slice of the registry; fails open to one loop |
| `plan.py` | `plan_turn` (the model reads the person's words and names the ONE tool, from an enum of real tools) and `check_answer` (what the answer claims, against what ran) — no regex front door |
| `belt.py` | pure detectors that correct an answer claiming work the turn did not do: performed / fabricated / misreported / unfiled |
| `gate.py` | the three write models (server-minted confirmation, staged proposal, signed plan card) and the rule that a gated verb stays visible |
| `probe.py` | drives the REAL model through the things the assistant must do and prints what it did |

Stdlib + Django only. The model calls (`ask`, `loop_fn`) are passed in, never imported.

## Adopting it

1. **Write an adapter** (`assistant.adapter.Adapter`) naming the app's nouns.
2. **Build and register the families** beside your own tools:
   `SPECS, FUNCTIONS = families.build_all(ADAPTER)` then
   `families.register_all(SPECS, FUNCTIONS, register)`.
3. **Count.** `python -m assistant.audit`. Declare what genuinely does not apply:

   ```python
   ASSISTANT_GAPS = {"comms": "this app has no message corpus"}
   ```

   A declared gap prints as a decision; an undeclared one as a gap. "We didn't get to it" is not a
   reason. A key naming no family is reported — a typo declares nothing.
4. **Wire the belt** before the stream ends: `fix = belt.correction(answer, trail)`; append the
   note and emit it, so the last thing a person reads is the truth.
5. **Route** once the catalog is too big for one block to be read. Lanes are ATTENTION, never
   authority: permission stays in the tool scopes and `gate.py`. Every failure runs open.
6. **Probe the real model** whenever the prompt or the catalog changes; the printed transcript is
   the receipt. A mock verifies the renderer and cannot see tool-skip.

## The guarantees the audit lists and never auto-passes

Every answer is checked against the turn's trail · secrets unreachable by inclusion · every result
declares its cap · every count carries its denominator (zero is "no data") · nothing blocks on a
slow dependency · toggles read before a fault is reported · message text is DATA, never
instructions · rows are evidence, never a total · report the ambiguous · state the consequence ·
a detector's inputs need their own coverage · proven against the real model · measured vs
inferred, with the source · a handle is internal, a citation is openable · the model reads, the
CODE counts.

## The compounding rule

Work on one app's assistant is a deposit, not a delivery: build a family for app A, then move its
generic form here (a module in `families/` and a row in `families.REGISTRAR`) so app B inherits it
and the audit starts crediting it everywhere the same day. Find the canon vague or wrong while
porting — correct `canon.py` in the same change.
