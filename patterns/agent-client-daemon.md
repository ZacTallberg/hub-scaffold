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
- Keep a short ledger of the consoles that ENDED on this machine (id, name, subject; a couple of
  weeks) that outlives the per-console record. Without it the daemon cannot prove a retired
  console was this box's, and mail for it goes nowhere; with it, that mail lands as box mail in the
  newest ATTENDED console. A finished or unattended console is never a live delivery target: on one
  measured inbox 8 of 9 addressed items were going to such a console's dead pipe.
- **Mail for an ended console goes to the console doing the same work**, not the one typed in
  last: a live attended console on the SAME subject (task, problem) first, then the same
  repository or app, then the most recently active. Freshness alone handed an answer about task X
  to a console in another project, which read it as noise and acknowledged it.
- **Delivered once means one receipt key per message, stable across re-picks.** Key a delivery
  receipt by the message and the console it was WRITTEN for (`rerouted_from` when the Hub
  readdressed it), never by the console the Hub picked this poll: the pick of a live home for an
  ended console's mail can change from poll to poll, and one answer reached one console five
  times. Mail too long to show whole gets its own one-time "shown, clipped" receipt -- shown once
  with the line that recalls it in full, never re-posted, still unacknowledged so `inbox` keeps
  it; with no receipt at all it was re-posted every cooldown, forever.
- **One owner of the desktop.** When another notifier owns the person's desktop (it rewrites a
  small ownership marker every few seconds), the daemon's own toast stands down while that marker
  is fresh, and resumes when it goes stale -- the same question once, never twice, and a stopped
  notifier never leaves a silent gap.
- **A condition an agent can clear is the responder's, not the person's.** An attention item the
  Hub marks `actor: agent` goes to the unattended lane; listing it under "addressed to you" in the
  person's prompt asks them to do the responder's work.
- Every frame the Hub delivers carries a **reply line** saying how to answer it: the Hub is not a
  peer console, so "reply to the sender" through a console-to-console channel reaches nobody. Name
  the verb (`message --to <agent> [--session]`, claim/resolve for a problem) instead. Advertise a
  console-to-console channel only for an attended live console on the same machine.

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
- **Then send what CHANGED, line by line.** A block hashed as a whole re-sends every line when one
  count moves: on one fleet 53% of a day's per-prompt output was lines the same session had already
  received, and an addressed list re-sent all its items whenever its count flapped. Keep a digest
  per age-masked line the session was sent (a few hundred, newest kept -- err toward repeating,
  never toward withholding); after the session start send only the unsent lines under the current
  count ("3; 2 already shown in this session"), and nothing when the change only removed lines. A
  session start, a resume or a compaction sends everything and resets what the session holds.
  (`client prompt-context`'s live channel is the reference.)
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

Two more rules keep the live channel cheap:

- **Standing lists are change-gated per console.** A list that is usually the same between prompts
  (open questions, addressed items) is re-shown only when its content hash changes, or as a
  reminder every few hours — never on every prompt. Crossovers render once per prompt, from the
  Hub's own verdict; a console never runs a weaker local detector alongside it, and the Hub says
  explicitly when it checked and found nothing (a count of 0, never an absent field), so "checked,
  nothing" is not mistaken for "not checked".
- **Expensive per-prompt work is warmed off the prompt path.** If the Hub ranks context against a
  console's focus (an embedding, a search), the daemon asks for it when the focus CHANGES, on its
  own tick with a bounded timeout, and the prompt hook sends a focus that is already warm. A prompt
  that pays for an embedding inline is a prompt that waits on the network.
- **Rank by the console's WORKING CONTEXT, re-rank only on a material change.** Most prompts are
  not the person (on one fleet 82% were relayed peer messages and notifications), so ranking by
  the prompt's text ranks by an envelope or by somebody else's words. Build the query from the
  held task and its latest checkpoint, the files edited in the last few minutes and the gist of the
  person's last real prompt; re-rank on a session start, a person's prompt, a new held task or a
  new subsystem among the edited files -- or a peer message whose text is FAR from that context
  (`guidance.json?peer=` answers the cosine; one fleet measured 0.33 on its own frames). Anything
  else reuses the last query and renders no new rows.
- **Alerts name the document that holds their protocol.** When the situational doctrine lives in
  on-demand documents (skills) rather than the always-loaded core, the moment an alert arrives is
  the moment its protocol applies: a crossover, an unclaimed-problems line or a blocked-app block
  ends with the name of the document to load. Chosen structurally (the hook knows which block it
  emitted), never by matching prose. And say at SESSION START, in one line and only where those
  documents are installed, that they are doctrine and are loaded before the first action on
  matching work: measured on real headless sessions, told nothing 0 of 7 loaded the matching
  document, the same sentence in the core instructions 0 of 3, and as a session-start hook line the
  session loaded it as its first tool call.

### The prompt path never waits on the network

The daemon keeps a payload file current; the prompt hook reads it. Every rule below was a prompt
that took seconds:

- **A payload on disk is served, even stale** (marked stale), never traded for a network fallback:
  a laptop that just woke with every route down spent 25 s of one prompt timing out on four
  addresses while a payload sat on disk. The network fallback runs only when there is no payload at
  all; a dead or sleeping daemon is restarted on the same run, detached.
- **A payload write a reader blocked is not stamped current.** On Windows a reader holding the file
  makes the replace fail; a writer that stamps "current" anyway serves the old bytes as new. Retry
  the replace; a coalesced report retries too.
- **The hook process imports nothing it does not use.** A prompt served from the local payload
  never imports the socket and HTTP stack (~110 ms on one machine) -- lazy loading belongs to the
  single-threaded hook process only; the long-running multi-threaded daemon imports the ordinary
  way (lazy imports under threads were a suspect in a daemon hang).
- **Time each phase into a log** (`gate-timing.jsonl`-style: read, render, network, supervisor), so
  a slow prompt names where it went instead of "it was slow".
- **A prompt never pays for the supervisor.** It does not query the OS scheduler when the
  supervisor already watches the daemon; it starts the daemon at most once a minute, and only when
  the daemon's loop stopped ticking; an unconfirmed supervisor is repaired by a DETACHED install,
  never inline (the first prompt after an overnight sleep spent 8 s in a scheduler query and a
  process start inside the hook).

The same daemon is the natural carrier for the OTHER direction: a local session observer that
distils "worth remembering" candidates appends them to an outbox file, and the daemon's heartbeat
ships it (`python -m hub_core.client ship-outbox --outbox <file>`) at most every few minutes and
only when the file grew. The offset moves only after the hub accepted the batch, so a dropped
connection resends; the hub's candidate queue is review-first and idempotent by id
(`patterns/knowledge-retrieval.md`), so a resend never duplicates or reopens anything.

## 3. A second agent runtime is an adapter, not a fork

Supporting another runtime means an adapter that: reconciles the daemon's hook entries in that
runtime's hook configuration (installed ≠ observed: report both — a hook written to disk that has
never fired is not working); maintains a managed doctrine block in that runtime's instructions file
(one delimited block, rewritten atomically, never clobbering the user's text around it); mirrors
the shared skills; and exposes the runtime on every console row (`X-Hub-Runtime`), so the roster
and the board can say which runtime a console is. Enrollment and daily repair must accept a machine
that runs only the second runtime. Isolate the receiver's long-poll from maintenance work and from
queue waits, so a slow repair never delays delivery.

**Keep the interpreter a hook already names.** A runtime that trusts a hook by its EXACT definition
(the command line, interpreter path included) silently revokes that trust when the definition
changes. The daemon, its updater and enrolment can each run under a different interpreter, so
writing "whichever python is running me" flipped the definition between them and dropped the
person's trust every few hours. Reconcile keeps the interpreter the existing entry names while it
still exists; a reconcile run under another interpreter leaves the file byte-identical.

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
- **A restart is not an outage.** A long-poll cut by a hub deploy (a refused or reset connection,
  a proxy 502/503/504) is ridden: wait for the hub on a cheap read every ~2 s (honour a
  `Retry-After` while it warms), then return with a random spread of a second or two -- without
  doubling the back-off, without a failure streak and without an outage report. On one fleet 2-4
  failed polls per restart pushed the wait to minutes, so mail sat undelivered long after the hub
  was back. Past a few minutes it IS an outage and the back-off above applies. (`hub_core.client`'s
  `_request` rides restarts the same way for every verb.)
- **An unrendered source copy is replaced, not refused.** When the served files are rendered from a
  source (facet fences stripped per tier, say), a file on disk that still carries the source's
  markers is somebody's workaround for a broken publish -- the source copied over the install --
  and they expect the next update to replace it. Read as a hand edit it was refused on every later
  publish and stranded the machine. Archive it (named, dated, never deleted), install, and post an
  info event; a genuine hand edit (no source markers) is still refused.
- **The self-test reads no real machine state.** A staged self-test that read the machine's live
  sidecar (a delivery-owner bit) passed on one machine and failed on another, and a failed staged
  self-test refuses the update -- so every machine where that bit was set stopped taking updates.
  Point every path the self-test reads (state, sidecars, settings) into its own scratch directory.
- **Run the served artifact's self-test in CI, per tier, before it publishes.** Render the daemon
  exactly as each tier is served it, stage it, run its own `--selftest` and compile every served
  file. Without that, the fleet is the first place the self-test runs: a daemon every machine
  refused (one subprocess call without the hidden-window policy) was published three times in
  three weeks, and each time every machine sat on the old version until a person noticed. The same
  stage runs the tier lens over every served file, so a restricted tier is never served a name its
  lens hides -- a bundle that fails closed on such a name withholds the whole daemon from that tier.

## 4a. Companion tools at a pin the Hub publishes

When every machine must run a companion tool (a local memory engine, a linter, a helper CLI), let
the Hub publish ONE pin — `{repo, sha}` — and let the daemon converge each machine to it:

- An unreadable or malformed pin does nothing: it must never trigger an install of the wrong code.
- Assess first: absent -> clone at the pin; behind -> fast-forward to it; AHEAD, locally edited or
  diverged -> never moved (report it). The clone never waits on a prompt (ssh in batch mode with a
  connect timeout, credential helpers off, a hard timeout), names WHY it failed from git's own words
  (auth / prompt / network -- a 900 s hang was reported as "register your key", the one cause a
  hang rules out), and falls back once to a bounded, non-interactive HTTPS clone riding credentials
  the OS already caches.
- **A tree held behind the pin says HELD, never "current".** A dirty or diverged checkout is
  correctly left alone, and its pass records `held` with the head, the reason and the pin, reports
  a warning and is re-checked hourly; summarising it as "current at <head>" hid it. Report the
  ANCESTRY of any tree off the pin (`ahead | behind | dirty | diverged | pin-unknown`, from
  `merge-base --is-ancestor`) so the distribution view grades only a descendant as current
  (`<name>.rel=` on `HUB_DISTRIBUTION_PINS`).
- **Fetch the pin from the published repository, not whatever `origin` is.** A developer's checkout
  can name its remotes differently (origin = an upstream mirror); fetching only `origin` never got
  the pin and held the tree forever. When `origin` does not bring the pin, fetch the published URL
  directly -- refs land in `FETCH_HEAD`, the checkout's remotes untouched.
- **A partial clone is archived and cloned again.** A clone killed mid-way leaves a `.git` with no
  readable commit (or no project file); read as a checkout it was kept forever and every pass died
  in the installer. Trust a checkout only when HEAD is a readable commit AND the project file is
  present; otherwise rename it aside and clone fresh -- unless it holds the tool's DATA, which is
  never moved.
- **A data-only directory is adopted in place.** A directory holding the tool's store but no
  `.git` was refused forever ("a person should look"). Initialise, fetch and check out the pin where
  it stands: the data is ignored by the repository and never moved; leftover files the pin would
  overwrite are moved to a `displaced-<stamp>` directory first and put back if the checkout still
  fails; a failed adoption archives its new `.git` and leaves the directory as it was.
- A broken environment (a virtualenv that will not run) is ARCHIVED and rebuilt, never deleted;
  the tool's own data directories are never touched.
- Run the tool's own self-test at the pin; ONLY on a pass activate it (wire its hooks). Back the
  runtime's settings file up first and restore every foreign hook the tool's installer drops.
- **Installed is not working.** End every pass with one real operation of the tool (one recall
  through the installed environment for a memory engine) and report ok only when it answered
  fully; otherwise `degraded`, reported and retried. An EMPTY store is reported, never filled from
  the installer (a rehearsal's full index ran a disk out).
- **After moving the code, restart what still runs the old code.** A tool with a warm server keeps
  the code it imported; stop THIS install's server processes (matched by the command line naming
  this install's environment, never by name alone) so the supervisor relaunches them on the new
  code. Without it a pin bump left every machine on a degraded fallback until a reboot.
- **Wire a second runtime too, when it is present.** If the machine runs a second agent runtime,
  the same pass wires the tool into it (its MCP entry and hooks), with the same backup and
  foreign-hook rules; a failure there is a warning, never a failed primary pass. A fix line that
  promises "the update wires it" must be true of the update.
- **A rehearsal never reports.** Only a pass on the real install directory, the real settings file
  and the real daemon home may report to the board; a scratch rehearsal (whose temp directory a
  policy may forbid executables in) was reported as the machine's install failure.
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
- **A scheduler query that times out is a host stall, not "not registered".** On a machine whose
  host stalls for minutes (every daemon thread blocked in a filesystem call), `schtasks /Query`
  times out in the same window; counted as a registration failure it reported a task that was
  registered and firing every minute. A timeout counts on its own "scheduler unresponsive" streak
  and reports only after many consecutive attempts, as a warning naming the stall; a real bad exit
  still counts as a registration failure.
- **Watch the daemon for hangs, and look twice before acting.** A watchdog thread compares the
  loop's own last-tick stamp (and each worker thread's last attempt) with the clock; past a few
  minutes it dumps every thread's stack to a file (`faulthandler`) and posts one warning (at most
  every half hour), so the next stall names its cause. A supervisor that finds the loop silent for
  many minutes reaps the daemon (its pid verified by command line) so the replacement can take the
  singleton lock. BOTH need two stale looks a minute apart: one stale look is a machine that just
  woke from sleep (a "45,585 s hang" was an overnight sleep with every thread in `connect`), and a
  reaper acting on it kills a healthy daemon.

## 5a. Dispatching unattended work from the daemon

When the daemon also launches unattended responders (`patterns/unattended-responder.md`):

- **Dispatch on its own thread, woken by the Hub's task wake**, never inside the heartbeat's
  maintenance ticks: a wake that reached the machine within a second spawned 15 s later, behind
  maintenance.
- **No pass waits on an expensive read.** Eligibility that needs one task read per recent run (to
  find resumable work) took 17-35 s per pass, and every new request queued behind it. Serve the last
  answer (a few minutes old at most) and refresh it on a background thread. Log every pass as one
  line (trigger, seconds since the wake, per-phase cost, what spawned), and a failed read as FAILED
  with its error class, not as a slow phase -- a question that spawned 20 s late was invisible until
  the log named the phase.
- **Keep the newest ids, never the alphabetically last.** A spawned-items set trimmed with
  `sorted(ids)[-N:]` dropped the ids that sort first the moment it was written; one kind of item
  (whose prefix sorts first) was fired again every cycle, with two or three launchers on one item.
- **The spawn order keeps the eligibility order.** A person waiting on a request goes first; a
  later step that re-sorted by kind (questions before tasks) undid it, and a request missed the
  cycle that spawned three others.
- **A resident agent that spends a person's account is opt-in.** A standby process kept warm on
  somebody's machine spends that person's subscription; start it only after its person armed it
  (a flag file written by an explicit verb, removed by its inverse), report "not opted in" as a
  state of its own, and never grade it stale.

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
