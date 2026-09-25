# Pattern — consoles that find each other and split the work

Several consoles (people's sessions, unattended runs) working one estate will, sooner or later,
fix the same thing twice: two consoles each diagnose the same failure, each edit the same file, and
the second push either conflicts or silently overwrites the first. Neither could see the other.

The hub already holds everything needed to notice: each console's project, the files it edited
recently, its focus, and the task it CLAIMED (`hub_core.presence` + leases). It compares them ONCE,
server-side (`hub_core.overlap`), and addresses the result to the consoles it concerns. Nobody pays
for a roster of everyone else's work on every prompt.

## What a console reports

Any write may carry these headers (the client sends them from the environment):

| Header | Environment | What it feeds |
|---|---|---|
| `X-Hub-Session` | `HUB_SESSION_ID` | which console this is — leases bind to it |
| `X-Hub-Project` | `HUB_PROJECT` | same-project and same-subsystem pairing |
| `X-Hub-Files` | `HUB_FILES` (comma list) | same-file pairing (only edits inside the activity window count) |
| `X-Hub-Focus` | `HUB_FOCUS` | subject pairing, and what peers read about you |
| `X-Hub-Session-Kind`, `X-Hub-Run`, `X-Hub-Subject` | `HUB_SESSION_KIND`, `HUB_RUN_ID`, `HUB_SUBJECT` | marks an unattended run and what it was started for |

A supervisor that watches a session can report a digest with the heartbeat
(`python -m hub_core.client presence --phase editing --doing "…" --narration "…" --last-result "exit 0"`),
so the board can say "idle 12 min, last did X" instead of stamping every open window "active".

## The signals, strongest first

| Kind | Fires when | Suggested split |
|---|---|---|
| `file` | two consoles edited the same file in the activity window | agree ONE editor; the other takes a separate component, the diagnosis or the review; share the patch before integration |
| `task` | both hold the same task (across repositories — the task is the unit), or one holds a task whose words match what the other is doing untracked | split into complementary deliverables and name an integrator; the task keeps its owner and contributors report back to it |
| `project` | same project AND same subsystem (first directory below the project) or same subject — co-location alone is silent | compare the interfaces being changed, divide components or implementation and review |
| `topic` | different projects naming the same configured system (`HUB_OVERLAP_SYSTEMS`, a comma list) or the same distinctive file | exchange the finding or reusable code; team up only where it advances both tasks. Capped per console |

Each addressed signal carries evidence, not an assignment: the peer's focus, their task, their
latest checkpoint — labelled a **peer report**, a claim to read rather than proof — the command that
reaches them, and the split for its kind.

## Where it arrives

- `GET /hub/consoles.json?session=<id>` (`client consoles --session`, MCP `board_consoles`) — the
  signals for one console, phrased from its side; without `session`, every live console and every pair.
- The console's inbox (kind `overlap`, `client inbox --text` → CROSSOVERS), and therefore the
  long-poll a supervisor already blocks on. `POST /hub/api/overlap-seen` records delivery in a
  sidecar (never the ledger); each signal is announced once per side and again only when it changes
  or six hours pass.
- The board's Consoles card lists the pairs.

## Rules the detector holds (each was paid for once)

1. **A worktree or scratch path is not a project, and its folder name is not a subject.** A console
   reviewing code in a throwaway worktree named after a feature was paired "both on <feature>" with
   the console actually building it. But a worktree's FILES are the repository's files: a file is
   compared as (project, path inside the repository), the worktree's project read from the console
   standing in it, so `hub/x.py` edited in the main checkout and in a worktree of the same
   repository is ONE file and pairs as `file` (before this, worktree paths were dropped and two
   consoles editing one file from two checkouts never collided).
2. **Words the hub writes into a focus are a status, not a subject.** Two consoles that had each just
   "finished <id>" of unrelated tasks were paired "both on finished".
3. **One person's own windows in one repository is how they work.** Only the same FILE or TASK pairs
   them.
4. **An unattended run cannot coordinate.** It is never addressed; its pairs reach the attended side
   only on the strongest kinds.
5. **A task binds to the console that claimed it**, never to one inferred from a directory. A legacy
   lease with no session is attributed only when its agent has exactly one live console.

## The protocol when a signal reaches you

1. **Coordinate, then keep moving.** Send your current evidence and a proposed split; agree who edits
   the shared files and who integrates. Continue on your agreed part while the peer works theirs.
2. **Keep one accountable owner.** Helping is not a reason to reclaim the owner's lease or file a
   duplicate task. Record the split and your results as checkpoints on the existing task.
3. **Reuse the peer's work.** Read their task and checkpoints before re-deriving a diagnosis; a second
   investigation should answer a different question or check a risky claim.
4. **One editor per file at a time; one integrator per change.** Avoid competing pushes of the same
   work.

The hub supplies the evidence and the channel. The agreement is the consoles' own.
