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

## Proving it

Each part has a real operation: send a message to a console and watch it land (then ack it);
close a console and send to it, and watch the mail reach the sibling console with `rerouted_from`;
run two prompts and confirm the second carries no doctrine; publish a deliberately corrupt update
and watch it refused and reported while the installed copy keeps running.
