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
python -m hub_core.unattended publish           # push at most one waiting hand-off, then exit
python -m hub_core.unattended sweep             # hand back leases left by a launcher that died
python -m hub_core.unattended status | lanes    # pauses, the auth disarm, attempts, runs, lane slots
```

A scheduled run inherits no shell: `--home DIR` (before the verb) loads `DIR/unattended.env`
(`HUB_*` lines only; a pasted token is refused, `HUB_AGENT_TOKEN_FILE` names a file instead).
`<home>/DISABLED` is the kill switch: nothing launches and nothing publishes while it exists.

Environment: `HUB_API_BASE`, a hub credential (`HUB_AGENT_TOKEN`, or the shared
`HUB_WRITE_TOKEN`), `HUB_AGENT_ID` (set it to the operator identity, `HUB_OPERATOR_AGENT`, if this
launcher should answer questions), `HUB_MACHINE`, `HUB_WORKER_RUNTIME` (`auto`|`claude`|`codex`),
`HUB_UNATTENDED_LONG_SLOTS`, `HUB_UNATTENDED_TASK_BOUND_S` / `_SHORT_BOUND_S`,
`HUB_REPO_URL_TEMPLATE`, `HUB_UNATTENDED_HOURLY_CEILING`, `HUB_PUBLISH_HOSTS`,
`HUB_PUBLISH_URL_TEMPLATE`, `HUB_PUBLISH_BRANCH`, `HUB_UNATTENDED_HOME` (local state; defaults to
`~/.hub-unattended` resolved by the interpreter, never a shell's `$HOME`).

## What is workable — and the one predicate that decides it

A **task** is an unattended responder's only when it says so: its `routing.required_capabilities`
contains `unattended`. An unmarked task belongs to whoever made it. A **question** is workable when
this launcher's agent is the operator identity and the escalation rule below allows it.

A **needs-attention condition** (`GET /hub/attention.json`) is workable when the hub marks it
`actor: agent` and it is not informational; one the hub says only its own machine can clear
(`on_its_machine`) is offered only there. A list without the `actor` field says nothing about who
can clear a condition, so nothing is offered from it — a guess would put a session on something
only a person can do. The id is `attention:<the hub item's id>`.

A task whose lease holder the hub reports **abandoned** (its console is not among the live ones) is
offered as a resume, whoever held it: a machine that went silent a minute after claiming otherwise
strands the task for the whole lease. The hub's claim gate still decides — it takes over only a
holder it can prove gone — so a refusal is a cheap stand-down, never a session.

`scan` and `respond` use the same predicates. A scan that counts items the responder will then
refuse launches sessions that are killed at the ceiling with nothing to show.

## A refusal is a response; one run per item, one machine per item

- **Deferrals.** A stand-down behind another machine's claim (re-checked in 15 minutes, not the
  claim's TTL: a finished run releases its claim at once), an escalation inside its cooldown, an
  item at its attempt cap (6 hours) and one behind the hourly ceiling (10 minutes) each write a
  `deferred_until` record, and `scan` does not offer that item before it. Without one a capped
  item was re-offered and refused every cycle, and an item that stood down behind a machine that
  then went silent was never offered again.
- **One run per item per machine.** `respond` takes a per-item lock (a file holding the
  launcher's pid, taken over only from a dead pid) before anything else; a second launcher for the
  same item stands down with nothing charged. The lanes have several slots, and a
  needs-attention condition has no lease to stop two runs working it side by side.
- **One machine per item.** A task has its fenced lease. A question or a condition is claimed
  through `POST /hub/api/item-claim` BEFORE the run is queued (so a machine that loses the race
  writes no ledger row), refreshed while it waits and while it runs, and released the moment the
  run ends. An unreachable claim route proceeds, loudly. A stand-down that spent nothing leaves no
  row in the bounded run ledger, where a respawn storm of them used to push out the ended runs a
  lease proof needs.
- **An hourly launch ceiling** (`HUB_UNATTENDED_HOURLY_CEILING`, default three per long-lane slot,
  never below eight), read from launches actually recorded. Needs-attention is the lowest lane: at
  most a third of the ceiling, so a backlog of conditions never starves tasks and questions.
- **Ledger writes are retried against a clock** (five seconds), never silently dropped: on Windows
  a replace fails while a reader, an indexer or a scanner holds the destination, and a swallowed
  failure made a queued or running run vanish.

## Outcome from the board, never from the exit code

An exit code cannot distinguish "it worked" from "it never ran". After every run the launcher
reads the item back:

| board state after the run | outcome |
|---|---|
| task `done` | cleared |
| task still `in_progress` | left-active → handed back at teardown |
| somebody else finished or took the task while it ran | superseded (stopped; never cleared, never charged) |
| a condition the attention LIST no longer shows | cleared |
| a condition the list still shows after six minutes of re-reads | not-cleared |
| the attention list unbuilt or unreadable | unverified (attempt refunded) |
| task `todo` | not-cleared |
| question no longer open | cleared |
| hub unreadable | not-cleared (fail closed: never a false success) |

"No longer offered to me" is not cleared: a task a run left in progress is not offered either.
For a needs-attention condition the LIST is the authority, never the inbox: the inbox carries
attention items only once the list is built, so for a while after every hub restart it carries
none, and a condition read through it would be recorded cleared while it still stood. A local
`done` for a condition the list still shows is dropped as stale.

**Superseded.** While a task run works, every heartbeat renews its lease with the fencing token.
The proof of ownership is that token, never a session-id prefix (short prefixes collide): only the
lease holder can finish a task, so a heartbeat that keeps succeeding up to a `done` means this run
finished it. A refused heartbeat while the task is in progress under another holder, on two checks
three minutes apart (never on one blip), stops the run — the tree is killed and proven reaped —
and a task that then reads `done` was finished elsewhere. Nothing of the run's is left on it, so
it is neither handed back nor charged.

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
back three times is a person's — counting only CHARGED runs. A run that recorded no new checkpoint
or push since it started (it re-read a pipeline still running, or waited on a push it could not
make) did not lose to the task; charging it retired tasks at the cap with their fix live. Such a
run is handed back with `idle: true`: the row counts it in `times` and carries `[handed back N
times, M idle]` in its note (the plan schema is closed), the charged count is `times` minus the
idle runs, and every run together backstops at twice the cap, so an idle loop still ends. A local
`done` record the board contradicts (a run suspended with the machine and journalled on wake) is
dropped and refunded; a `running` record older than twice the longest bound is refunded; a lane
fault, a superseded run and an unverifiable one are refunded.

**A task at either cap is a person's, and the board says so.** The launcher writes the task's
`needs_person` field once — `{reason, sha, at, by}` — with the failing evidence in the reason.
Both caps used to end in one log line on one machine while the task read "todo, unattended"
everywhere else, so no person ever learned it was theirs.

**Every hand-back and every `needs_person` carries the failing evidence**, from the hub's own joins
on the task: the last pushed sha and its pipeline, whether a verified deploy found it live, and the
error-stream failure recorded against that commit (`ci_problem`) when there is one. A hand-back
that said only "ended with the task still active" sent whoever it escalated to off to find what the
board already knew. A hand-back the hub REFUSES (anything but a 409 saying the lease is somebody
else's now) is itself reported, because it leaves the task active with nobody on it.

When the launcher resolves a task's repository it records it on the task as `project` (once; a
project somebody set is never replaced), so the next run, the deploy record and the commit resolver
stop guessing from the title.

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

**An expired agent login DISARMS the lane; it does not pause it.** A run that exits non-zero
saying the login expired (`OAuth token has expired`, `not logged in`, `invalid api key`) writes
`<home>/auth-expired.json`, reports ONE critical row naming the single human action, and nothing
launches until the login is proven renewed: a credential file newer than the disarm (a login
rewrites it) re-arms the lane by itself, checked on EVERY scheduled tick, queue or no queue. A lane
that only paused burned the next item on the same dead login every half hour. A weekly usage limit
names a DATE ("resets Sep 28, 3pm (Region/City)"); that form is read too, still capped at six
hours, so a machine does not probe a dead account every half hour for days.

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
**No credential ever travels in a URL.** A person's checkout can carry one in its origin
(`https://user:<token>@git.example.com/...`); copied into every clone, it spreads into their
configs, the prompt's worktree brief and the board's fault rows. Every URL the worktree module
reads or builds passes through `no_userinfo` (an ssh URL keeps its user and loses only a password;
every other scheme loses the whole userinfo), every reason string that could echo one through
`redact`, and a launcher clone made before the rule is healed in place (`remote set-url` on the
launcher's own scratch clone, never a person's checkout). Git gets credentials from its helper.

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

## Lanes for needs-attention: one at a time

Several conditions often share ONE cause (three apps missing the same route read as three items).
On the long lane's parallel slots each got its own responder, all three fixed the same thing side by
side and one pushed an identical file. Needs-attention therefore runs in its own ONE-slot lane on the
task clock: the second run starts after the first fix is on the board and finds its item cleared.
The session is told what kind of condition it holds and that the list dropping it is the only proof.

## The publish hand-off: a machine that cannot push still ships

A task is done only when its commit is on the protected branch. A run whose machine cannot push (a
key nobody registered, a credential manager that cannot prompt unattended) used to stop at a patch
file on its own disk. The charter now tells it to hand its commits to the hub
(`python -m hub_core.client handoff <task>`, a bundle of `origin/<branch>..HEAD`), and the
publisher (`hub_core/unattended/publisher.py`) — the one thing the launcher does itself rather than
through a model session, so it is mechanical and narrow — ships it from a machine that can:

1. It acts only when this machine has PROVED it can push to the host: a `push --dry-run` of a
   throwaway commit to a ref that never exists. A probe never sends a password (a directory-backed
   git host counts an expired stored password toward lockout): ssh keys only (`BatchMode`, no
   password or keyboard-interactive auth, empty credential helper and askpass), and an HTTPS
   transport is "not proven" — it fails closed. The verdict is cached six hours either way.
2. The URL is built from the record's project PATH and a transport this machine already uses,
   only for a host in `HUB_PUBLISH_HOSTS` — never taken from the hand-off, so a record can never
   steer a publisher at another host. No allowed host: the publisher is off.
3. It leases ONE hand-off (`POST /hub/api/handoff/claim`: a random token and an increasing fence),
   downloads its bundle under the lease (`/api/handoff/bundle`) and checks the sha256, and in a
   scratch clone under `<home>/publish` verifies the bundle, fetches its head, rebases onto the
   current branch and pushes WITHOUT force, re-fetching and rebasing when the branch moved (three
   rounds). It reads the branch back before reporting the pushed sha (`/api/handoff/result`); the
   hub writes the `pushed` checkpoint that lets the deploy record close the task.
4. A conflict is reported `failed` with the conflicting paths and nothing is touched; an auth
   refusal releases the lease to another publisher. One publish per machine; from `scan --launch`
   it runs as its own process under a ten-minute ceiling, before the queue, its tree reaped and
   proven gone, and its scratch directory removed by literal path.

The hand-off routes, the queue (`GET /hub/handoffs.json`) and the `handoff` client verb are the
hub side of this lane; the publisher speaks only through them.

## The charter (what the session is told)

`RESPONDER.md` is the whole standing prompt: scope is exactly one item; hard limits (nothing
destructive, no messages to real people, ship only through the normal path, never an interactive
prompt); **the credentials the operator provisioned are the worker's to use**; **a business fact
is research, not a blocker** — only a genuine choice is the operator's, and the ask says where it
looked; **before asking, check whether anything is left you are not ALLOWED to do** — landing your
own pushed fix is not a blocker; non-core work ships straight through, core work (new data access,
new tools, a changed figure, permissions) goes to a reviewable branch plus an ask to the OPERATOR —
a yes about business impact, never about who built the application (applications are shared;
"somebody else's app" is never a reason to wait); an ask whose only remedy is a person's action is
left OPEN, because answering it retires the one signal that reaches that person; a push refused
for auth is handed off, never left as a patch; a push is not a ship until live; never leave
uncommitted work. It also tells the session what the launcher does around it (handed back with its
plan, idle runs counted apart, `needs_person` at the cap, stopped if superseded, disarmed on an
expired login), so it does not work against it.

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
Scheduler: `adapters/windows/register-responder.ps1 -Launcher unattended -HomeDir <dir> -Workspace
<dir>`, which gives the tick a twenty-minute limit because it may run one publish pass first). Run
`scan --launch` from a directory that holds the checkouts the tasks name. For a long
unattended window on a laptop, `adapters/windows/keep_awake.py` holds a wake lock for the whole
window; each run already holds one for its own length.
