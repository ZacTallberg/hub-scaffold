# Agent memory — the hand-off between the board and a machine's own memory engine

The board is the durable record; it is not a memory engine. A workstation may run one — hybrid
search over its own session transcripts, curated notes and a local copy of the board's knowledge,
injected before each prompt by harness hooks. **claude-memory** (mirrored as its own repository)
is the reference implementation of that engine. This scaffold does not ship it. It ships the SEAM
an engine plugs into, the verbs that make the seam observable, and the rules both sides must
compute identically — each earned by a failure on the system this was lifted from.

| Piece | Where it lives | Runs without configuration? |
|---|---|---|
| The knowledge feed an engine mirrors | `GET /hub/knowledge/since` (`patterns/knowledge-retrieval.md`) | yes |
| Feed text redacted at the one choke point every mirror reads | `hub_core/knowledge.py` `put_op` + `hub_core/secretscan.py` | yes |
| The consumer contract (live fold, one decider, index text, rule form) | `hub_core/memory_feed.py`, `client memory-feed status\|records` | yes (reads files only) |
| Tool-time lesson triggers | `hub_core/lesson_triggers.py`, `client lesson-trigger --hook`, `client triggers`, `GET /hub/triggers.json`, MCP `match_lesson_triggers` | yes; fires only on records carrying `applies_when` |
| Safe hook installation | `hub_core/settings_io.py`, `client install-hooks` | yes |
| Where a prompt's time went | `client prompt-context --hook` receipt (`last`) | yes |
| One index pass at a time | `manage.py semantic_index` | yes |
| Observer distillation | the engine (off by default) | no — see §6 |

## 1. The file contract

A producer on the workstation (`client knowledge-sync`, or a daemon that pages the feed on its
heartbeat) writes the board's knowledge where the engine reads it:

    ~/.agent-memory/feeds/<feed>.jsonl         (HUB_MEMORY_FEEDS_DIR; one feed per file)
    ~/.agent-memory/feeds/<feed>.state.json    (the sidecar: freshness and the ONE decision)

Each JSONL line is one op, exactly as the feed serves it: `put` (the record as it is now),
`revoke` (drop it), `reset` (drop every record of `source` before its re-send). The file is the
contract: nothing on the board imports the engine, and nothing in the engine knows the board.
The producer appends a page and makes it durable BEFORE it moves its cursor (a crash re-fetches a
page rather than losing one), stores the cursor opaquely, pages a bootstrap until `more` is false,
and compacts the file to its live set past a size ceiling (written aside and swapped in, so a
reader sees a new file and replays it).

`client knowledge-sync --out <file>` writes the same records as one JSON document; every reader in
this scaffold accepts either shape.

## 2. What a consumer must do with it

`hub_core/memory_feed.py` holds the part both sides compute; an engine that re-derives it drifts.

- **Newest op per id wins. A revoke DELETES.** An index that keeps serving a retired rule scores
  below having no memory at all. A reset drops one record type before the puts that restore it.
- **Read only new complete lines.** Keep a byte offset and the file's identity per feed; a torn
  final line is a write in flight and waits for the next pass; a shrunk or replaced file (the
  producer compacted it) is replayed from the start. Puts are idempotent by the board's
  `text_sha`, so a replay costs reads, not writes.
- **Write the source row and its chunk as one unit.** An engine that recorded the record's hash
  before its chunk write, and then failed the chunk write, treated every record as "unchanged"
  forever — measured on a first real feed: every put failed that way and the replay skipped all
  of them. Drop the half-written source on failure; retry next pass.
- **Redact at index time.** Agents paste credentials into records — a token in a clone URL, a
  service password in a diagnosis. A mirror is a second copy on disk that a backup's secret scan
  will refuse. The feed already serves text redacted (`put_op`), and the consumer redacts again
  (`index_text`) because it may read feeds from other publishers. The typed marker stays; the
  board's `text_sha` still keys the record, so redaction never causes a re-put.
- **Index the rule once.** Most titles are the rule cut short; indexing both counts the same words
  twice and spends recall budget on a repeat (`index_text`).
- **Tell a text change from a label change.** `keys()` returns the text hash and a metadata hash
  (date, check, status). A record whose text is unchanged but whose check moved updates its label
  in place — re-embedding it wastes work, and skipping it means a new check (or a failed
  re-check) never reaches recall.
- **Filter kinds BEFORE the top-k.** Knowledge records are a few percent of an engine's rows; a
  feed-only search that filtered one global top-k returned 1 of 8 (lexical) and 0 of 8 (vectors).
  Widen the window until k eligible rows exist.
- **Keep the fused order.** A general-purpose cross-encoder (trained on web passages) ranked these
  short records WORSE than the fused lexical+vector order it was reranking — @1 14.1% vs 24.4% on
  78 real answered questions. Measure a reranker on your own records before adding one.

## 3. One decider for per-prompt delivery

Two channels — the board's per-prompt block (`client prompt-context --hook`) and local recall —
can each deliver the same knowledge. Both sending doubles the tokens; neither sending starves the
prompt. When each side ran its own freshness test, 36 of 218 real prompts got the knowledge twice
and 24 got it from neither.

So ONE decider (the producer, on its heartbeat) writes the decision into the sidecar and both
sides obey that bit:

    {"feed", "ok_at", "more", "error", "local_owns", "owns", "decided_at", "written_at"}

- `decide()`: the operator's switch is on AND the mirror is current AND local recall is working.
  Fails closed — any doubt keeps the board's block.
- **Current = the mirror's age, not the last poll's success.** `mirror_fresh()`: caught up
  (`more` false) at `ok_at` within 6 h. Knowledge is not live state. Requiring every poll to
  succeed handed delivery back to the board on each slow poll — 41 of 73 prompts, exactly while
  the board was timing out, which is when local delivery matters most. `more` is never set false
  by a FAILED poll: an interrupted first bootstrap must not read as a complete mirror.
- **Working = the last full-pipeline recall within 15 min** (`recall_healthy()`). A status record
  without a mode (a session-start map, a skipped short prompt) is not a failed recall; only a
  recall that ran and was not full is.
- `owner()`: obey `owns` while `decided_at` is within 15 min (three missed heartbeats). A stale
  decision means the decider stopped: both sides fall back to the board's block. Disagreement must
  cost duplication, never a gap. A sidecar with no `owns` (an older producer) falls back to the
  switch plus the mirror's age, so the halves can be upgraded in either order.
- Local delivery gets its OWN slots and a reserved share of the recall budget, and each delivered
  record is keyed `<id>#<text_sha>` in the same per-session receipt as everything else.

`python -m hub_core.client memory-feed status` prints every feed, its live and revoked counts,
its sidecar, the mirror's age, and who delivers before each prompt right now.

## 4. How owned knowledge is shown

`rule_form()`: the statement alone, cut at 300 characters, the id kept so the reader can pull the
story with `client recall <id>`. The owned block is a quarter of the recall budget and a full
record (title, rule, story) renders at a median ~740 characters, so three fit whatever the slot
count said. Measured on 396 real answered questions: six rule-only records answered 57.3% where
three full records answered 48.7%; the verdict sentence survived the cut in 388 of 388. Hand the
renderer more candidates (8) than slots, or the cheaper form can never deliver what its budget
now fits. `client memory-feed records --render rule|full` shows either form over a real mirror.

## 5. The recall contract

What any engine's per-prompt recall owes the reader — the same rules the board's own block keeps:

- **A state claim dates itself and carries its check.** "Port 8443 serves X" decays from the day it
  is written. Print `as of <date> (<age>)` on every note, label state-like notes `STATE` with their
  `verify` command (or `UNVERIFIED` when they have none) and rank them below durable rules. As-of
  is the LATER of the note's own modified stamp and the file's mtime; a dated EVENT is history, not
  state. The board records `verify` / `verified_as_of` on every knowledge verb.
- **Identity is the SUBJECT, not the directory.** A memory is about a repository: the normalized
  origin remote (`git@h:g/r.git`, `ssh://git@h/g/r.git` and `https://u:tok@h/g/r` are one subject),
  followed through a worktree's `commondir`, so a worktree and its main checkout share memory. The
  same definition the board uses for "same project" in crossover detection — two definitions of
  that is how split memories come back. No remote = no canonical subject; say so, never guess.
- **Provenance.** A note records who wrote it and whether the session was attended or unattended
  (and on which task), from the runtime's own marker, never from a guess.
- **Say what could not be seen — once.** When recall ran keyword-only, or the subject was
  unresolved, emit a partiality notice. Key it in the session receipt by a hash of its exact text:
  79% of 802 real notices repeated the previous one byte for byte.
- **One index pass per store at a time.** An OS byte lock beside the store, released by the OS
  when its holder dies. A hook that falls back to spawning an index pass when the server misses
  its deadline does so exactly when the server is busy indexing, and each busy moment spawned
  another indexer. This scaffold's `manage.py semantic_index` holds the same rule: a second pass
  prints `{"skipped": ...}` and exits 0.
- **Record where the time went.** Time each phase of a recall (queued, embed, search, rerank,
  format) into its delivery receipt; a request that times out logs its phases so far, and the
  worker that kept running logs its full phases when it finishes. "Exceeded 12 s" alone named
  nothing; the phases named two shared model sessions (prompt embeds queued behind index batches).
  `client prompt-context --hook` keeps `last: {fetch_ms, render_ms, pack_ms, total_ms, outcome,
  delivered, held, chars, rank}` in its receipt, and an unreachable board line says how long it
  was waited on.
- **Deliver only what the session lacks, and only what landed.** A per-session receipt keyed
  `<source>#<text hash>`, reset on session start and compaction; receipt keys are written only for
  blocks inside the text actually emitted; output stays under the hook's inline ceiling.

## 6. Observer distillation — OFF by default

An engine may distil finished sessions into dated observations: a deterministic salience
pre-filter first (most turns never reach a model), then one call to an OpenAI-compatible endpoint
that returns observations with a durability judgement, stored beside the transcripts with a
full-text index. Fail-soft: the index pass never fails because the observer did. The API key is
read from an environment variable, never from config. **Ship it disabled**: it sends session text
to a model, and a public default must not do that until an operator points it at an endpoint they
trust.

Observations worth sharing leave through an OUTBOX (`~/.agent-memory/outbox/promotions.jsonl`),
redacted, each once (a cursor in the engine's store). A producer ships new complete lines to the
board at most once per interval, moving its offset only after the board accepted the batch, into
a REVIEW-FIRST queue: nothing a machine distilled is served as knowledge until a person adopts it.

## 7. Installing hooks without destroying anyone else's

A harness settings file belongs to the person and to every tool registered in it. An installer
that read it as strict UTF-8 treated a file saved with a byte-order mark as `{}` — and its write
kept only its own hooks. `hub_core/settings_io.py` holds four rules: read `utf-8-sig`; refuse to
write a file that exists and does not parse as an object; back up and replace atomically; re-read
and require every foreign hook entry and every untouched key to be unchanged, else restore the
backup and fail loudly.

    python -m hub_core.client install-hooks [--settings PATH] [--dry-run] [--uninstall]

wires `prompt-context --hook` (SessionStart, UserPromptSubmit) and `lesson-trigger --hook`
(PreToolUse on shell and edit tools, PostToolUseFailure), idempotently, with an interpreter path
and a `sys.path` entry so the hook does not depend on its working directory. `--uninstall`
removes only these entries. The environment (`HUB_API_BASE`, `HUB_AGENT_ID`) is the operator's:
the installer never writes it. `patterns/presence-gate.py` follows the same four rules for the one
env key it may set.

## 8. Tool-time lesson triggers

The per-prompt block delivers knowledge ranked for what a console is doing. A rule about a
specific command or error is worth most at the moment that command is typed or that error is
printed. A knowledge record may carry `applies_when`
(`{"errors", "commands", "paths", "systems"}`, each ≤5 literal triggers), derived from its rule
and story on the board; it rides the feed to every mirror. The hook matches literally —
nearest-neighbour search over error text was relevant about 4 times in 30, and a reminder that is
usually wrong teaches the reader to skip it:

- `<...>` is ONE non-space value. A wildcard allowed to span spaces let `git show <rev>:<path>`
  match whole unrelated command lines on real transcripts.
- errors match the failing tool's OUTPUT; commands match the command about to run; paths match an
  edited path as a glob anchored at a segment boundary; systems never fire alone.
- A trigger carried by more than 6 live records, or with under 4 literal characters, is ignored.
- At most 2 records per event, once per session per record, under the line "cite the id if it
  changes what you do"; nothing at all when nothing matches.

**The corpus guard** (`client triggers --refresh`, spawned detached by the hook when due): a
trigger is ignored when it matches more than 1% of the calls of its kind (commands vs shell
commands, paths vs edited paths, errors vs failed results) among this machine's last 20,000 tool
calls, read from its own transcripts and redacted. No hand-set list. The two triggers that fired
mostly on the wrong case sat at ~2.5% of commands; the error triggers that were right 8 times in
10 sat under 0.5% of failures; the groups sit ~5x apart at every window from 5k to 100k calls,
so the line is their geometric middle. A kind with under 500 sampled calls is not rated. Until a
trigger is rated the hook holds it back: failure mode silence, never noise.

**The precision signal**: each injection is noted with the transcript offset it landed at; later
hook runs scan only new transcript bytes for an assistant turn naming the id and append a `cite`
line to an append-only ledger. `client triggers --stats` prints fired / cited / precision per
trigger and per record; `client triggers --replay [--seed candidates.json]` runs every trigger
over the real call sample with examples, so a candidate trigger is vetted against what agents
actually run before anyone relies on it.

The board answers the same question without a workstation: `GET /hub/triggers.json`,
`client trigger-match --error|--command|--path`, MCP `match_lesson_triggers`.

## 9. Lessons for whoever builds the engine

Not shipped here; recorded because each cost a day on the reference engine.

- **One store, one encoder.** Two implementations of "the same" embedding model (a local export
  and a served reference) agreed to 0.9999 inside the model's attention window and fell to 0.92
  past it. Never mix their document vectors in one store; gate a remote encoder on a recorded
  parity proof over real texts, and cap local query embeds inside the window.
- **A batch job that cannot finish says so.** A re-embed that stopped at a quarter of its rows on
  proxy 502s printed its normal summary and exited 0. Retry come-back-later statuses within a
  budget; exit non-zero naming the counts when coverage is incomplete; swap a new store in only at
  100% coverage, keeping the old config as the rollback.
- **Prompt-path models get their own sessions.** One embedder served prompts and indexing, so a
  prompt's embed queued behind document batches (11 ms idle, 7 s under load).
- **Readers never wait on a full reload.** Catch a vector snapshot up from a write log; build the
  new arrays outside the lock and swap them in whole; fall back to a full reload only when the log
  cannot account for the change — and prove a fast direct read's layout (declared dimension, exact
  blob length, validity bits) before trusting it, returning to the slow read on any mismatch.
- **A shared log file on Windows**: a rotating handler that keeps the file open loses every record
  after the first failed rename. Open per record, retry the rollover later, stamp the pid.
- **Use the model's own pooling.** A sentence-embedding export that carries its projection heads
  must be read at its sentence-embedding output; mean-pooling the first output skipped the heads
  and produced vectors nearly orthogonal to the real ones.
