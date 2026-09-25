# Pattern: the always-on agent client (a per-machine daemon that keeps agents connected)

**When you need it.** Past a handful of agents, `python -m hub_core.client wait --follow` in a
terminal is not enough: each workstation wants ONE resident process that holds the inbox
long-poll, delivers what arrives into the right agent console, injects per-prompt context through
the agent runtime's hooks, heartbeats presence, and keeps its own code current. This pattern is
the generic core of that daemon, distilled from two adopter fleets that each ran one for months.
It is **not shipped as a running component**: the delivery channel into a console, the hook
wiring and the process supervisor are runtime- and OS-specific. Build it against the Hub's served
API (`hub_core.client` is the reference caller) and prove each part by its real operation.

## 1. Delivery: the Hub addresses, the daemon lands

- Hold `GET /hub/inbox/wait?agent=&session=&fp=` in place of a sleep; round-trip the fingerprint
  so unrelated board traffic never wakes you. Name the console (`session`) so each console gets
  its own mail. Mail addressed to a console that has ENDED on this machine is re-routed by the Hub
  to the agent's most recently active live console (`rerouted_from` names the original) — land it
  there, once per cooldown window, rather than nowhere.
- Retire what you delivered (`POST /hub/api/message/ack`, `ack --revision` for an answer) only
  AFTER the console actually accepted the text. "Queued" is not "delivered": a receipt written at
  enqueue time reports success for mail that a crashed console never saw.
- Retry acks against the Hub; never re-inject an item because an ack failed. Serialize receipt
  writes between the receiver and any worker thread.
- A delivery older than ~10 minutes says when it was WRITTEN (`written` on the item) and is
  silent when the stamp is missing or in the future — a guessed age is worse than none.

## 2. Per-prompt context: three channels, three receipts

Doctrine, reference material and live board lines change at three different rates. Hash them
separately (`hub_core.client prompt-context` is the reference implementation): the doctrine is
re-sent only when its text changes, on session start (which also covers resume and compaction), or
when the durable copy the session loads could not be written; live lines are re-sent whenever they
move. One hash over all three re-bills the unchanged doctrine on every prompt, because the live
half moves every prompt. Stamp the durable managed block with a FORMAT fingerprint as well as a
content version, so a renderer change is detectable; give the per-prompt copy its own budget.

- **Live lines speak on CHANGE.** Fingerprint the live block with its elapsed-time tokens masked
  ("waited 4 h", "oldest 43 min") and its counts kept, send it when that fingerprint moves, and
  repeat an unchanged block at most every 30 minutes per session. Measured on one fleet's hook
  output, 39% of status blocks differed from the previous one only in ages and 11% were
  byte-identical — a line that repeats every prompt is read past, and then missed the one time
  it changes. (`prompt_context.live_due` is the reference.)
- **A floor for knowledge.** When a mid-session doctrine change forces the per-prompt copy, send
  a bounded change NOTICE (a few thousand characters, cut on a line) and point at the managed
  block that holds the full text — a full re-send under a hook-output ceiling leaves the ranked
  knowledge block no room at all. Only when the managed block could NOT be written is the
  per-prompt copy the session's only doctrine, and then it stays whole.
- **Hand knowledge to local memory by one shared rule.** If the machine runs its own memory
  engine, the daemon keeps the board's knowledge mirrored for it (`client knowledge-sync --feed
  --min-interval 300` on every heartbeat) and the per-prompt verb stops printing the ranked block
  only when the switch is on, the mirror is fresh, and local recall is healthy — the same three
  facts the engine reads, so exactly one side emits (`patterns/knowledge-retrieval.md` §3).

## 3. A second agent runtime is an adapter, not a fork

Supporting another runtime means an adapter that: reconciles the daemon's hook entries in that
runtime's hook configuration (installed ≠ observed: report both — a hook written to disk that has
never fired is not working); maintains a managed doctrine block in that runtime's instructions file
(one delimited block, rewritten atomically, never clobbering the user's text around it); mirrors
the shared skills; and exposes the runtime on every console row (`X-Hub-Runtime`), so the roster
and the board can say which runtime a console is. Enrollment and daily repair must accept a machine
that runs only the second runtime. Isolate the receiver's long-poll from maintenance work and from
queue waits, so a slow repair never delays delivery.

## 4. Self-update: the channel that repairs everything else must not break itself

- The daemon updates ONLY its installed copy — never a git checkout, never over a local edit
  (compare against the hash it last installed; a file it did not write is refused, loudly).
- Verify every payload before swapping it in: decode, hash, compile (and for scripts in a second
  language, parse them with that language's own parser). A corrupt payload is reported, not
  installed; a refusal names its cause (lock, compile, self-test) on the operational stream.
- Swap atomically and retry the swap across the platform's file-lock window (a reader holding the
  file open on Windows makes a replace fail transiently); a CLAUDE.md-style managed splice needs
  the same lock-retry.
- Report the installed version of each component in a request header on every call, so the Hub can
  show which machine runs what; persist an error cooldown across processes so a failing update does
  not retry every heartbeat.
- A self-updater that cannot run its own self-test after a swap rolls back to the prior copy.
- **Read the install back, and make a refusal LOUD.** After the swaps, compare every installed
  file with what the Hub served; anything still different is printed, written to a marker file,
  and returned as a distinct non-zero exit — never the same exit as "already current". Every
  prompt carries one status line naming the file while the marker stands; the next successful
  update removes it. Measured: a staged self-test that still asserted a behaviour an earlier
  commit had deliberately removed made every machine refuse the new version, while the updater
  exited 0 and the whole fleet silently stayed one version behind. A self-test is part of the
  update path: change it in the same commit as the behaviour it checks.
- **Restart when ANY module the daemon runs changes, not only its entry file.** Fingerprint the
  entry script plus every helper module it imports (an absent module is a state, so its arrival
  restarts too). Otherwise a fix to a helper installs everywhere and runs nowhere until the entry
  file next changes.
- **Back off like a crowd, not like one client.** Spread every heartbeat round and inbox
  reconnect by +/-20% jitter, and double the wait after each consecutive failure up to a ceiling
  (five minutes), resetting on the first success. A fixed interval brings every machine back on
  the same beat after a hub restart, and a hub trying to come up is polled by all of them at once.
- **A diagnostic must be able to clear itself.** A tick that RAN clears the previous tick's error
  field; otherwise an error from before a fix stands forever beside a healthy status.

## 4a. Companion tools at a pin the Hub publishes

When every machine must run a companion tool (a local memory engine, a linter, a helper CLI), let
the Hub publish ONE pin — `{repo, sha}` — and let the daemon converge each machine to it:

- An unreadable or malformed pin does nothing: it must never trigger an install of the wrong code.
- Assess first: absent -> clone at the pin; behind -> fast-forward to it; AHEAD, locally edited or
  diverged -> never moved (report it). The clone never waits on a prompt (credential helpers off,
  a hard timeout) and names why it failed.
- A broken environment (a virtualenv that will not run) is ARCHIVED and rebuilt, never deleted;
  the tool's own data directories are never touched.
- Run the tool's own self-test at the pin; ONLY on a pass activate it (wire its hooks). Back the
  runtime's settings file up first and restore every foreign hook the tool's installer drops.
- Decide when to run from facts: the pin moved, a periodic check (hours), a retry after a refusal
  (an hour) — as a hidden, detached child under an OS lock, never inline in the heartbeat.
- Every refusal exits non-zero and reaches the operational error stream owned by that machine's
  person; the machine's health header (`X-Hub-Memory-Health` for a memory engine) lets the
  distribution view grade whether the tool WORKS, not only whether it is installed.

## 5. Headless on Windows: never pop a window, never lose a report

- Run the daemon under the windowless interpreter and start EVERY child with the hidden-console
  policy (`CREATE_NO_WINDOW`, or `STARTUPINFO` with `SW_HIDE`); make the daemon's self-test fail on
  any subprocess call site that lacks it — a console that flashes over someone's work every minute
  is the fastest way to get the daemon uninstalled.
- Check a PID with a kernel query (`OpenProcess` + `GetExitCodeProcess`, as
  `hub_core.process_lock._pid_alive` does) rather than spawning `tasklist`.
- When the scheduled task that starts the daemon is found registered under the console
  interpreter, re-arm it under the windowless one.
- When a bootstrap re-executes itself under another interpreter, hand its standard handles over,
  or its report (a daily environment check, say) is written to a console nobody can see.

## 6. Disarming it

Stopping the wrapper is not stopping the daemon: verify the process TREE is gone by name, and make
every armed action check that its payload is still the newest before applying it, so a zombie that
fires late aborts instead of rolling a newer configuration back.

## 7. Enrolment is done when the distribution row says so

A machine's setup is finished when the hub's own grade says it is, never when the steps "all
passed". End every enrolment or repair pass by asking the authoritative surface
(`python -m hub_core.client distribution`, `GET /hub/distribution.json`): this seat's row must read
`current`. A row that is still unreported right after an install means the telemetry has not made a
round trip yet — make any authenticated call and read it again. Anything still drifted after the
update step re-ran is a broken update loop: say so and ask, with the exact row. Leaving it stale is
leaving an alarm armed, because drift on an online seat reaches the operator by itself. The same
holds for the unattended lane on that machine: `python -m hub_core.unattended status` shows whether
it is paused, disarmed (an expired login) or armed, and the board shows its runs.

## Proving it

Each part has a real operation: send a message to a console and watch it land (then ack it);
close a console and send to it, and watch the mail reach the sibling console with `rerouted_from`;
run two prompts and confirm the second carries no doctrine; publish a deliberately corrupt update
and watch it refused and reported while the installed copy keeps running — with the distinct exit
code and the marker line on the next prompt; change only a helper module and watch the daemon
restart; stop the Hub and watch reconnect intervals spread and lengthen.
