# Unattended responders — spend a session only when something happened

`hub_core/responder.py` is an event-driven launcher: a scheduler runs `poll` every few minutes,
the poll reads the board cheaply, and only a genuinely workable item buys an agent session — one
bounded session for exactly that item, then gone. Like every pattern here it is inert until you
wire it: nothing runs merely because the module exists.

```
python -m hub_core.responder poll [--dry-run]   # read once; launch one `respond` per workable item
python -m hub_core.responder respond <item>     # one bounded session for one item
python -m hub_core.responder status             # the local run ledger
python -m hub_core.responder canary             # daily end-to-end proof of the ask loop
python -m hub_core.responder check-env [--report]   # the environment repair table
```

Configuration is environment only: `HUB_API_BASE`, `HUB_AGENT_TOKEN` (or `HUB_WRITE_TOKEN`),
`HUB_AGENT_ID` (the identity whose inbox it drains — the operator identity if it answers
questions), `HUB_RESPONDER_RUNTIME` (the session command, a JSON argv list or a shell-style
string containing `{prompt_file}` or `{prompt}`), `HUB_RESPONDER_WORKSPACE`,
`HUB_RESPONDER_HOME` (default `~/.hub-responder`, resolved from the interpreter, never a shell
`$HOME`), `HUB_MACHINE`, and the clocks `HUB_RESPONDER_SHORT_S` / `HUB_RESPONDER_TASK_S`. A
scheduled run inherits no shell, so `<home>/responder.env` supplies `HUB_*` lines; a token
pasted there is refused — `HUB_AGENT_TOKEN_FILE` names a file only this user can read, which
keeps the credential out of the task definition, the registry, and every process argument.
Kill switch: create `<home>/DISABLED`.

## What wakes a session — and what never does

| Wakes a responder | Never a wake reason (visible, counted, left for a person) |
|---|---|
| a question in the operator's inbox | a question raised by another unattended session (`via=responder`) |
| a FRESH unclaimed operational error (last seen within a day) | an older unclaimed error — still listed, never pushed |
| a `todo` task marked for the lane (`create --unattended`) | the ready queue — most of it belongs to whoever filed it |
| | a task that is rotting in progress |
| | the responder's own lane faults (environment, canary, launch) |

**A rotting task pages; it never wakes.** A stalled in-progress task is a human decision —
close it, resume it, or hand it back — and waking a full session per stalled task was measured
at millions of tokens burned on a single abandoned task. Surface it aggressively (the board's
adherence scoring already does); act on it conservatively. The same holds for a timer: waking
on a schedule to "look at the queue" spends a session on a queue the session is correctly
forbidden to touch. Only an event that has actually happened is worth a session.

**The loop break is mechanical.** Every session runs with `HUB_UNATTENDED=1`, and
`hub_core.client ask` stamps `via=responder` onto any question raised under it — so a chain of
automated escalations stops after one hop without depending on a model remembering a marker.

## Safe by construction

The launcher never acts; every judgement lives in the bounded session and its charter. The
launcher only:

- **re-reads the item live** before spending anything, again with the lane held, and while it
  waits for a lane — and stands down the moment somebody else took or cleared it. An unreadable
  board is "cannot tell", never a reason to act;
- **caps attempts** per item on this machine (two), refunding an attempt whose launcher died;
- **runs one session per lane**: a short lane (questions, errors — 25 minutes) and a long lane
  (tasks — 90 minutes), so a one-minute answer never waits behind a build;
- **states the clock**: the session's charter names its kill minute and a ship-by minute eight
  minutes earlier. Anything not recorded on the board by the kill does not survive it;
- **kills the whole tree at the clock and proves the pid is gone** (a returned parent is not a
  dead tree; "I killed it" that nobody verified is the claim that was wrong);
- **verifies the outcome from the board**, never from the exit code or the session's summary:
  a task is cleared when it is `done`, an error when its ack note says `resolved:`, a question
  when it left the inbox. An unreadable board degrades to not-cleared, never a false success.

## The last mile: finishing the ship cycle, never losing work

A diagnosis nobody had time to apply is a bug report, not a fix. The charter makes the whole job
explicit: stage only your own files by path, commit, push through the project's normal path,
and **never answer "shipped" on a commit whose deploy has not verified**. When the deploy is not
close, do not sit on the lane — a wait is not free while another item is queued behind it:
`step` the task with `sha=<sha> pipeline=<id>` and what is left, leave it in progress, exit.

Short on time or unable to finish, the session puts its work on a `responder-wip/<slug>` branch,
pushes it, records it, and escalates. Whatever happens, the launcher posts a terminal update on
the board for EVERY outcome (tagged `automated`, so it is telemetry rather than an item addressed
to a person) — including the early exits that never start a session (`stood-down`, `lane-full`,
`unverified`, `exhausted`). It names the uncommitted files THIS session left: the launcher snapshots the
workspace's dirty files (status plus content digest) before launch and reports only what
changed, so an earlier run's leftovers are never blamed on a later one. It never commits them:
only the session knew which files were its own. The session's last words are relayed with
terminal colour codes stripped and any other control byte written as a visible `\xNN`, because
the write seam refuses raw control characters and the one note that makes an abandonment visible
must never be the note that is refused. A duplicate line is far cheaper than a silent
abandonment.

A **review gate** (`client review`, MCP `record kind=review`) is a question only a person may
answer. It is tagged `review`; `find_work` counts it under `review_gates_left_for_a_person` and
never launches for it, `resolve_item` refuses it, and the charter forbids answering one.

## Errors are problems: claim, fix, resolve

An error item is one fingerprint, however many rows it left. The session claims it BEFORE
digging — `ack-error <fp> --note "claimed: <what you are checking>"` — so every other console
sees it is in flight, fixes the cause, ships, then records
`ack-error <fp> --note "resolved: <root cause> evidence <sha|url>"`. Only a fresh problem is
ever pushed into a session; delivery to people follows the same restraint — a critical problem
reaches its owner at once, an error after it has sat unclaimed for a short while (ten minutes on
the origin system), and anything still unclaimed after half an hour reaches the operator — each
once, never as a stream, and never more than one frame per console per ten minutes. A board is
supposed to be empty or entirely in flight.

## Unattended means non-interactive, and invisible

- **No credential dialog.** Sessions run with `GIT_TERMINAL_PROMPT=0` and
  `GCM_INTERACTIVE=never`: a prompt nobody is watching is not a pause, it is a hang to the
  timeout. Fail fast with a logged error instead.
- **No blockable stdin.** Sessions get `stdin=DEVNULL`. A runtime that reads piped input from a
  fresh console blocks forever before its first model call — on the origin system that was
  almost half of all scheduled passes dying with zero output.
- **No popping windows on Windows.** The session gets its OWN console, hidden
  (`CREATE_NEW_CONSOLE` + `SW_HIDE`), not `CREATE_NO_WINDOW`: a console-less parent makes every
  console grandchild its tools spawn allocate a fresh VISIBLE window on the owner's desktop.
  Leaf helpers (git, tasklist, taskkill) use `CREATE_NO_WINDOW`. The scheduled poll itself runs
  under `pythonw.exe` — `adapters/windows/register-responder.ps1` arranges this — because Task
  Scheduler shows a console program's window for as long as it runs.

## Every run is a record

`<home>/runs.json` holds one row per session from the moment its launcher starts WAITING for a
lane (`queued`, with the launcher's pid and no `started`) through `started` to a verified
`outcome`, with duration, attempt, reap verdict, uncommitted files and the session's last words.
`status` reads each row honestly: `waiting-for-lane`, `running`, `ended`, or `died-unrecorded`
(a dead pid with no outcome — said so, never shown as running). The run id, kind, item and title
ride the session's environment (`HUB_RUN_ID`, `HUB_RUN_KIND`, `HUB_RUN_ITEM`, `HUB_RUN_TITLE`),
and each run is its own presence seat on the board whose focus says what it is doing — headless
sessions never open a console the board could otherwise see.

## The canary tells the truth or says nothing

The canary is a question FOR MACHINES, so it is filed `synthetic`: it stays on the record
(`questions.json`, where the canary is judged) but is delivered only to a reader that asks for
it with `inbox.json?include=synthetic` — the responder. A person's inbox, the `inbox/wait`
notifier and its fingerprint never carry it, so a daily self-test never spends a person's
attention or a notifier's cooldown slot ahead of a real question. It is deliberately NOT one of
the automation tags, which every reader drops: a canary its own responder cannot see can only
time out, or read as "retired" and pass.

With `HUB_RESPONDER_CANARY=1`, `poll` files a synthetic question through the real ask path once
a day and later judges whether the loop answered it. Three rules keep that verdict honest:

1. **Unreachable is not a verdict.** If the board cannot be read, nothing is concluded — no
   green, no alarm. A network blip must never page anyone as "the loop is broken".
2. **One empty read is not proof.** A canary absent from a reachable read is either retired
   (answered) or a transient empty read; only several consecutive absent reads count as retired.
3. **Alarm only when you know.** The alarm fires only on a reachable read that SHOWS the canary
   still unanswered past its deadline — once per canary.

## Enrollment is repair, and leaving is one command

`check-env` prints the table a machine needs to run responders — state directory, credential
(as the hub actually received it), identity, session runtime, git, `pythonw.exe`, kill switch —
one line each, `OK` / `FIXED` / `NEEDS A PERSON`, never silent. What it can fix it fixes;
`--report` turns every NEEDS A PERSON line into an operational error on the board owned by this
machine. `poll` re-runs it once a day, so an environment that rotted since enrollment says so on
the board instead of in a log nobody opens. Arming is idempotent — re-running
`register-responder.ps1` is the repair — and `register-responder.ps1 -Remove` is the one-step
leave: it removes exactly what arming added and leaves the ledger and log for you to read.

On other platforms, schedule `python -m hub_core.responder --home <dir> poll` with cron or a
systemd timer; `start_new_session` gives each session its own process group, which the clock
kills whole.
