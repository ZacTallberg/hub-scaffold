# worklogs/ — execution logs with numbers

> canonical · owner: the executing seat · update: append entries as work happens (not retrospectively) · append-only

One file per workstream: `<slug>.md`. The worklog is the "what actually happened, with measured
before/after" companion to any plan — plans claim, worklogs prove.

Entry discipline:
- Open with a **baseline snapshot** (the numbers before you touch anything).
- One dated entry per meaningful action/run: what ran, the metrics delta, what was discovered,
  what was deferred (deferred items also land as hub tasks or `registers/` rows — a worklog is not a tracker).
- Numbers come from runs (`../runs/`), never from memory.
