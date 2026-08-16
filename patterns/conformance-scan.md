# On-demand portfolio conformance inventory — pattern

Despite the legacy filename, this is not a standing scanner, test suite, or release gate. It is an
on-demand discovery pattern for a deliberately scoped portfolio task: observe which projects and
Hubs exist, record the current mechanical facts, and create repair or build tasks for real gaps.

Do not install it as cron, CI, a scheduled workflow, or a permanent checker. If a temporary helper
is useful for one inventory pass, create it in disposable task scratch, retain the resulting report
and task receipts, and delete the helper before commit.

## Why it exists

- Give the operator a current project/Hub inventory without hand-maintained fiction.
- Turn observed missing or broken surfaces into actionable Hub tasks.
- Route repeated operational failures to a dedicated repair agent when one exists, preserving the
  delivery queue's throughput.

This pattern does not judge copy, wording, visual style, animation quality, UX taste, or routine
implementation details. Those are product work, not validation targets.

## One-shot shape

1. Read the portfolio's real registry, repository list, or public site links. Do not create a
   second permanent registry.
2. Observe only facts needed by the current inventory request—for example project identity,
   repository presence, Hub URL, and whether the actual front door responds.
3. Produce one timestamped report with the source and observed outcome for each row. Use
   `OBSERVED`, `MISSING`, `UNREACHABLE`, or `NOT_APPLICABLE`; avoid a synthetic quality score.
4. Create a fresh Hub task for every actionable missing or broken item. An observed failure is the
   notice; the inventory itself does not become a standing alarm.
5. Stop when the report and tasks exist. Delete any temporary query/helper before commit.

## Suggested observations

Choose only observations relevant to the task; this is a menu, not a mandatory battery.

| Fact | Observe from | Resulting action |
|---|---|---|
| Project exists | canonical registry, repo list, or public site links | create an investigation task only when sources disagree |
| Repository exists | actual repository provider or local checkout | create a repository/recovery task when missing |
| Hub exists | project metadata or deployed Hub URL | create a Hub construction task when absent |
| Front door works | one real navigation/request | create a repair task when the real operation fails |
| Build identity is visible | deployed artifact metadata, only when deployment coherence is in scope | create a deployment-coherence task when absent |
| Working tree has unfinished work | repository state, only when handoff/release scope requires it | record it in the owning task; do not grade it |

Do not add a `done_verified` observation or rerun completed task proof. Completed task receipts
compose into the portfolio report. If several completed parts create a genuinely new critical
security, destructive-data, migration, protocol, or concurrency seam, that seam alone may receive
one transient probe under `PROJECT/verify/README.md`.

## Failure and repair flow

```text
real observation fails → fresh Hub repair task → repair lane (if available)
                     → retry the failed real operation → durable receipt → done
```

The retry is the default proof. Do not preserve a test, fixture, scanner, or workflow after it
succeeds. Copy/style changes receive no automated validation at any point.
