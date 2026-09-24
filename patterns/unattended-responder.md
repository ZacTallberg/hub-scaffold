# Pattern — the unattended responder: one bounded session per board item, then gone

`patterns/worker-longevity.md` is about a seat that LIVES: a looping worker whose green condition
is completions. This pattern is its opposite and its complement: a session that is born because
exactly one board item needs work, does that item, and is killed. A scheduler runs a cheap scan
every few minutes; only when something is genuinely workable does it spend a session.

It ships as `python -m hub_core.unattended` (standard library only, speaks to the hub through its
served HTTP seam) with its charter in `hub_core/unattended/RESPONDER.md`. None of it runs until you
schedule it.

```
python -m hub_core.unattended scan              # what could be taken here, and what may not, with why
python -m hub_core.unattended scan --launch     # spawn one `respond` per workable item (--max N)
python -m hub_core.unattended respond <item-id> # one bounded session for exactly this item
python -m hub_core.unattended sweep             # hand back leases left by a launcher that died
python -m hub_core.unattended status | lanes    # pauses, attempt records, recent runs, lane slots
```

Environment: `HUB_API_BASE`, a hub credential (`HUB_AGENT_TOKEN`, or the shared
`HUB_WRITE_TOKEN`), `HUB_AGENT_ID` (set it to the operator identity, `HUB_OPERATOR_AGENT`, if this
launcher should answer questions), `HUB_MACHINE`, `HUB_WORKER_RUNTIME` (`auto`|`claude`|`codex`),
`HUB_UNATTENDED_LONG_SLOTS`, `HUB_UNATTENDED_TASK_BOUND_S` / `_SHORT_BOUND_S`,
`HUB_REPO_URL_TEMPLATE`, `HUB_UNATTENDED_HOME` (local state; defaults to `~/.hub-unattended`
resolved by the interpreter, never a shell's `$HOME`).

## What is workable — and the one predicate that decides it

A **task** is an unattended responder's only when it says so: its `routing.required_capabilities`
contains `unattended`. An unmarked task belongs to whoever made it. A **question** is workable when
this launcher's agent is the operator identity and the escalation rule below allows it.

`scan` and `respond` use the same predicates. A scan that counts items the responder will then
refuse launches sessions that are killed at the ceiling with nothing to show.

## Outcome from the board, never from the exit code

An exit code cannot distinguish "it worked" from "it never ran". After every run the launcher
reads the item back:

| board state after the run | outcome |
|---|---|
| task `done` | cleared |
| task still `in_progress` | left-active → handed back at teardown |
| task `todo` | not-cleared |
| question no longer open | cleared |
| hub unreadable | not-cleared (fail closed: never a false success) |

"No longer offered to me" is not cleared: a task a run left in progress is not offered either.

## Resume, never abandon

A task left `in_progress` whose lease was released or expired is `readiness.stale_reclaim` on the
board. `GET /hub/task/<n>.json` now carries `holder` (agent, seconds to expiry, last heartbeat —
never the token), `readiness`, and `handed_back`, so a launcher tells "somebody is on it" from "a
run that ended left it" without attempting a claim. A stale task is re-offered and its prompt says
**RESUMING**: the next run's first job is what the last one recorded (the pushed sha, the pipeline
it was waiting on), not starting over.

## Hand-back with proof, and ONE self-counting row

The launcher claims the task itself and gives the session its fencing token (`HUB_LEASE_TOKEN`)
plus a lease journal (`HUB_RUN_LEASES`); `python -m hub_core.client claim|start` appends every
granted lease to that journal. At teardown every journalled lease whose task is still in progress
goes through `POST /hub/api/hand-back` (client `hand-back`, MCP `hand_back_task`):

- the hub proves the lease (token + subject) under the lease lock, so a launcher can only ever
  return a lease THIS run held — never a person's, never another run's;
- the task returns to `todo` and its plan gains ONE row `{kind: handed_back, lifecycle: true,
  times: N}` that is re-appended at the end and counts itself. Three hand-backs are a pattern about
  the task; three copies of one sentence are how a pattern goes unread;
- `lifecycle` rows are shown on the task and never counted as a done step — counted, a task read
  MORE finished each time a run died on it. The board's progress, the agent view, the adherence
  score and the client's `step` all skip them.

A launcher killed so hard that it died too is swept by the next launcher (`sweep`, and at the start
of every `respond`): its journal names the leases, its pid is gone, and the same proof applies.

## Attempts are charged only for real tries

Per item per machine: two attempts, then it is left for a person. Across machines: a task handed
back three times (the row's own `times`, read off the board by every machine) is a person's. A
local `done` record the board contradicts (a run suspended with the machine and journalled on
wake) is dropped and refunded; a `running` record older than twice the longest bound is refunded.

## Lanes: single-flight held by a heartbeat, short and long, machine-sized slots

- A lane lock is taken with `O_EXCL` and **touched every 60 s** while the session lives. It is
  taken over only when BOTH nobody has touched it for 15 minutes AND its recorded pid is gone.
  An age-based lock lets a second launcher in during the first hour-long run.
- Questions run in the **short** lane, tasks in the **long** lane: a one-minute answer never waits
  behind a ninety-minute build.
- The long lane has `min(cores/2, GB/5)` slots, floor 2, ceiling 6; `HUB_UNATTENDED_LONG_SLOTS`
  overrides (1..12). A launcher waiting for a slot re-reads its task every two minutes and stands
  down if it was taken or closed meanwhile.

## The lane's own failures are not the item's

`hub_core/unattended/faults.py` classifies every finished run before anything is charged:

| verdict | how it is recognised | what happens |
|---|---|---|
| usage limit | non-zero exit whose output says the account is out of usage | launches pause until the parsed reset (capped at 6 h); nothing charged |
| harness dead | non-zero exit having spent **no** tokens and **no** turns (not logged in, bad key) | critical `agent-error` on the board, 30-min pause, nothing charged |
| API error | the run did work and the model API ended it (`terminal_reason: api_error`, a 5xx) | warning on the board, 5-min pause, nothing charged |
| ran | anything else | outcome read from the board |

On a lane fault the launcher RELEASES the lease instead of handing the task back: a hand-back row
counts toward the cross-machine cap, and a lane fault is not a try. The task stays in progress with
nobody holding it — `stale_reclaim`, re-offered as a resume.

The runtime's result may be one JSON object or a LIST of them, possibly after a line of chatter.
Tokens come from `usage`, or per model from `modelUsage` when the API ended the run; `num_turns > 0`
means it ran whatever the token parse says. Every lane fault is posted to `/hub/api/agent-error`,
which folds per (agent, source, code, message): a lane down for an hour is ONE row with a count.

## When a pass is killed at its ceiling: read its transcript

`claude -p --output-format json` prints only at exit, so a killed pass has, by construction, no
stdout — "hung before its first call" and "worked productively for the whole bound" look identical.
The runtime writes its transcript as it runs, so `forensics.py` reads it:

- the directory is `~/.claude/projects/<slug>` where the slug is the working directory with EVERY
  non-alphanumeric character replaced by one dash (collapsing runs yields a directory that does not
  exist);
- attribution is by START time: the earliest session whose first TIMESTAMPED record lands inside
  the launch window (a transcript's head is often an untimestamped summary line);
- the kill report carries turns, output tokens, the last action and began/last-wrote offsets; a
  pass still writing within two minutes of the kill is "ran out of clock while working" — a
  warning, not a page; with no transcript the report names WHICH of four causes it saw;
- a session whose last turn ended (`stop_reason: end_turn`) followed by the runtime's shutdown
  record, that has not exited, gets 60 s and is then ended and recorded as FINISHED;
- the wait is in 60-s heartbeats, and a heartbeat that returns far later than it asked means the
  machine slept: that overrun is summed and reported, not guessed. A wake lock is held for the
  length of every run;
- the pass is TOLD its clock in its prompt (the same number the reaper enforces) and told never to
  start a command it cannot finish.

## A task runs in its own worktree

`worktree.py`: the repository is cloned once per machine into `<workspace>/_wt/_repos/<repo>` and
each task gets `_wt/responder-<repo>-<n>` on branch `responder/<n>`. A person's checkout is only
ever READ, for its origin URL. The repository comes from the task title's leading `<slug>:`,
matched to a checkout's origin name with `-` and `_` as one separator, or — only when exactly one
checkout carries it as a whole word — a partial name; then `HUB_REPO_URL_TEMPLATE`
(`https://git.example.com/team/{slug}.git`), reached with `ls-remote` before use. The tree is
removed only on proof it holds nothing (task done, clean, every commit on the remote); otherwise it
is kept and named. A second run on the same task keeps the tree and never resets unpushed commits.
Untracked per-app notes (`CLAUDE.md` in the person's checkout) are carried by an `@<path>` import
stub hidden by the clone's `info/exclude` (git reads exclude from the COMMON git dir). Any fallback
to the launcher's own directory is reported to the board. On Windows git runs with
`http.sslBackend=schannel` so a fresh clone verifies against the OS certificate store.

## Bounded escalation: hop 1 is retaken once, hop 2 is a person's

A question raised by an unattended run carries `hop` as a FIELD set by the process, not the
model: the launcher exports `HUB_RESPONDER_HOP` (one deeper than its item), and the client's `ask`
(and MCP `ask_operator`) stamps it; the hub validates `0..9`. The board shows an "escalation · hop
N" chip on the thread.

- hop 0 — a person's or an attended session's question: workable at once;
- hop 1 — an unattended run stopped and asked: workable ONCE more, by whichever machine wins the
  claim, after a 30-minute cooldown (whatever it waited on has usually finished by then);
- hop ≥ 2 — a person's.

Refusing every escalation bounds chains too, but turns them all into a person-only queue of
mechanical follow-ups. The launcher fails closed if its client cannot stamp the next hop, and
it asks the client, not its source: it parses a real `ask` through the client's own parser with a
sentinel `HUB_RESPONDER_HOP`, runs the payload builder, and requires the sentinel in
`payload["hop"]`. A text search for the variable name passes with the stamping deleted, because
a comment or a help string still names it.

## The charter (what the session is told)

`RESPONDER.md` is the whole standing prompt: scope is exactly one item; hard limits (nothing
destructive, no messages to real people, ship only through the normal path, never an interactive
prompt); **the credentials the operator provisioned are the worker's to use**; **a business fact
is research, not a blocker** — only a genuine choice is the operator's, and the ask says where it
looked; **before asking, check whether anything is left you are not ALLOWED to do** — landing your
own pushed fix is not a blocker; non-core work ships straight through, core work (new data access,
new tools, a changed figure, permissions) goes to a reviewable branch plus an ask; a push is not a
ship until live; never leave uncommitted work.

## Two runtimes

`HUB_WORKER_RUNTIME=auto` uses Claude Code where installed, else Codex; both share the lanes,
clock, outcome checks and fault classification.

- **Claude Code**: `claude -p <prompt> --permission-mode auto --allowedTools … --output-format
  json`. Never a permission bypass; in print mode a tool it cannot auto-allow is denied.
- **Codex**: `codex exec --json …` (flags configurable via `HUB_WORKER_CODEX_ARGS`, because they
  vary by version — check `codex exec --help`). Its session id comes from the `thread.started`
  JSONL event. Codex owns its hook trust, auth, model and MCP configuration: install hooks into
  `~/.codex/hooks.json` alongside any existing ones, review them in `/hooks`, and never bypass or
  edit its trust store — an installed hook is not proof it ran, a real receipt is. Standing
  doctrine for Codex goes in the active global `AGENTS.md` (or `AGENTS.override.md`); shared
  skills install under `~/.agents/skills/<prefix>-*`. Codex sessions address each other through
  the hub (`ask`/`inbox`), not through another runtime's session names.
- The parent's runtime identity (`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID`)
  is stripped from the child's environment; git never prompts (`GIT_TERMINAL_PROMPT=0`); stdin is
  the null device; the whole process TREE is reaped and proven dead after every run.

## Scheduling it

Any scheduler that runs a command every few minutes will do (cron, a systemd timer, Windows Task
Scheduler). Run `scan --launch` from a directory that holds the checkouts the tasks name. For a long
unattended window on a laptop, `adapters/windows/keep_awake.py` holds a wake lock for the whole
window; each run already holds one for its own length.
