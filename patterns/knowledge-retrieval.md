# Knowledge retrieval — record it by kind, find it by meaning, deliver it before each prompt

A board accumulates what its agents learned: lessons, findings, methods, reviews, and the answers
people got when they were stuck. That knowledge is worth exactly as much as the chance an agent
meets it at the moment of need. This pattern wires four things together; each is optional
beyond the first, and each says out loud when it is not running.

| Piece | Where it lives | Runs without configuration? |
|---|---|---|
| Record verbs + BM25F search | `POST /hub/api/lesson` `finding` `method` `review` `gap`, `GET /hub/search.json` | yes |
| Dense retrieval (meaning) | `HUB_EMBED_*`, `manage.py semantic_index` | no — search says it is keyword-only |
| Per-prompt knowledge block | `GET /hub/guidance.json`, `client prompt-context --hook` | yes (standing order); ranked with an embedder |
| Published capability catalog | `HUB_CAPABILITY_*`, `GET /hub/capabilities.json` | no — the catalog is simply absent and says so |

## 1. Record by kind

Filing everything as one kind of note is how a board loses the difference between a FACT about
how a system behaves (finding), a RULE earned from a mistake (lesson), a PROCEDURE the team
follows (method), a QUESTION ONLY A PERSON MAY ANSWER (review), and an OWNABLE DEFICIENCY (gap).
Each has its own verb in `python -m hub_core.client` and its own MCP tool.

**A lesson is admitted, tagged, and adjudicated later — never refused for resemblance.** A
similarity gate at the door is the wrong shape: a correction is near-identical text by
definition, so the gate is optimally shaped to reject exactly the writes that fix a wrong rule.
The write lands with `related` (what it may duplicate or correct, with the shared terms that
drove the match) and `related_partial` (which basis did not run). An identical live rule is the
one case needing no reader: it returns `duplicate_of` and writes nothing.

`python -m hub_core.client adjudicate` settles the tags where a model is reachable: rules first
(identical text → duplicate, a non-rule match → other-kind, a retired target → target-retired),
then an OpenAI-compatible chat model (`HUB_JUDGE_URL`, `HUB_JUDGE_MODEL`, `HUB_JUDGE_TOKEN`) for
duplicate / correction / contradiction / unrelated with one sentence of reason. It never guesses
(an unparseable reply leaves the entry open) and exits 2 when there is judging to do and no
model, so a scheduled pass that settles nothing is loud. It writes back through the served API.

A verdict recorded on a lesson changes nothing by itself: a duplicate stays two records, both
served, both ranked. **Consolidation** (§1a) is the half that acts on it.

A STATE claim ("the export host serves port 8443", a measurement) decays from the day it is
written. Record it with `--verify "<the command or URL that answers it now>"` and
`--verified-as-of YYYY-MM-DD`; every surface prints both.

## 1a. Consolidate: fold what the board knows twice, and say when each rule applies

On the instance this was lifted from, about one new lesson in eight restated a record already on
the board — the same fact filed as a finding and then a lesson on one day, or re-learned days
later by an agent the first record never reached. `python -m hub_core.client consolidate` runs
where the judge (`HUB_JUDGE_*`) and the embedder (`HUB_EMBED_*`) are reachable, reads the board
through `note.json`, and writes back only through `POST /hub/api/note` (the one mutation
entrance). Engine: `hub_core/consolidate.py` (stdlib, store-free — the verb injects the writer).
Like `adjudicate`, it is a client verb only, not an MCP tool: it needs a judge and an embedder
on the calling machine and runs for minutes, which a tool call inside a console is not for.

1. **Candidates** — every pair of live lessons and findings whose cosine is an outlier for BOTH
   ends' own similarity distribution (z ≥ 3, the write-time tagger's relative criterion; an
   absolute cosine means nothing across a model swap). Two findings are never a pair. A board of
   fewer than about eleven records cannot produce z ≥ 3 and the run says so. Vectors are cached
   locally by text hash, so only edited or new records are embedded again.
2. **Verdicts** — duplicate / correction / contradiction / related / unrelated plus which record
   to keep, cached by the text hashes of both records: a run never re-reads what an earlier run
   read. An unparseable or self-contradictory reply (duplicate with keep=both) leaves the pair
   unjudged and counted — never coerced.
3. **Plan** — duplicates fold (union-find, clusters over five are reported, not folded) into ONE
   canonical lesson, the most foundational then the oldest, which gains `reinforced_by` (who else
   learned it, when, their story); a folded lesson becomes `superseded` with `superseded_by`, a
   folded finding keeps standing and is tagged `folded-into:<id>`. A correction supersedes the
   rule it corrects. A contradiction is written onto the lesson's `related` as a settled verdict,
   where `recall` prints it for a person.
4. **`applies_when`** — for every live lesson, the literal strings whose appearance means the rule
   applies NOW: `errors` (text as the tool prints it), `commands`, `paths`, `systems`. This is what
   a tool-time hook matches; similarity over a failed command's output rarely returns the record
   that explains it. Command triggers must name the **defective form**, never the bare tool:
   `<...>` placeholders for the varying parts (`git show <rev>:.<dotfile>`, `sudo -S <cmd> <<`); a
   flag that is wrong on only one command is written WITH that command
   (`APP_AUTH_REQUIRED=false <...> manage.py test`, never the bare flag, which is harmless
   elsewhere); a misuse typable in two shells gets one trigger per form (POSIX and
   `$env:APP_AUTH_REQUIRED=<...> manage.py test`). The parser keeps a command only if it carries a
   placeholder, a shell operator, `$`, an assignment, or three words — measured on real tool
   calls, two bare-tool triggers matched hundreds of calls each and cut a hook's precision from
   about 64% to about 20%. Generic strings and incident debris (shas, timestamps) are dropped.
   The prompt version (`TRIGGER_VERSION`) is part of the cache key, so a prompt change re-derives
   every lesson. A fold hands its members' triggers to the canonical rule. The hook that matches
   these at tool time is adopter wiring; the field is the contract.
5. **Re-learn count** — per ISO week, lessons filed and how many restated an older record
   (`relearned`: another author or more than a day later; `double_filed`: same author within a
   day). The scaffold has no served trend store: the run prints it (`RELEARN`) and returns it.

**The review-first apply contract.**

- **A dry run is the default** and prints every verdict with both texts, every action, and every
  derived trigger. It writes nothing on the board.
- **An apply writes only what an earlier dry run judged.** `--apply` judges nothing new and derives
  no new triggers; it replays the cached plan, so an apply must use the dry run's `--state-dir`.
  Otherwise pairs judged in the same run as the apply would be written unread.
- **Hold-backs.** `--except <id>,<id>` drops every action touching a held record (and its
  `applies_when`), each printed `HELD BY REVIEWER`. `--apply-limit` caps writes per run.
- **Nothing is deleted; everything is revertible.** Before each write the record's full prior
  state is appended to `<state-dir>/runs/<run>.jsonl`. `--revert <run>` restores every record the
  run touched; `--revert <run> --only <id>` restores one without undoing the approved rest. A hub
  write MERGES, so revert states every key the run wrote explicitly (prior value or the empty
  form; a lesson's status defaults to `standing`) — re-sending a payload without an added key
  leaves the key standing. Each revert carries its own idempotency key, so a corrective second
  revert writes instead of replaying the first.
- **Parse or refuse.** `--from-trigger FILE` reads the last non-comment line: `apply [limit=N]
  [except <ids>]`, `revert <run> [only <ids>]`, anything else a dry run. Every token must be
  understood; an unreadable limit or a hold-back with no ids exits 4 before any write, and the
  run prints `PARSED mode=… apply_limit=… exclude=… only=…` with the file and git HEAD of the
  code that parsed it. On the source instance an ignored hold-back was written through and the
  held record superseded; the trace could not say which code ran.
- **A schedule never writes.** `--scheduled` forces a dry run and refuses `--apply`, `--revert`
  and `--from-trigger`: re-reading an `apply` line nightly would re-apply yesterday's plan and,
  because an apply judges nothing new, the loop would stall on old verdicts. Only a deliberate
  invocation (a person, or a pushed trigger line) writes.
- **The code that runs must contain the commit that asked.** A trigger line pushed with a commit
  is executed by whatever checkout the runner has; pass `--require-commit <sha>` (e.g. the
  pipeline's commit) and the verb refuses with exit 5 unless `git merge-base --is-ancestor <sha>
  HEAD` holds for the code running, printing `CODE_UNDER_TEST` when it does.

Exit status: 0 ran (a dry run proposing nothing is a result) · 2 judging, deriving or embedding
to do and no model · 3 the hub refused a write · 4 trigger line or scheduled-write refused ·
5 code under test does not contain the required commit.

## 2. Find by meaning

`search.json` is BM25F over every live record that is knowledge rather than traffic:
inter-agent messages and open/restated questions are excluded (the top hit for a question was
otherwise the question itself), and a retired or superseded record is gone from every surface
at once. It needs nothing.

Configure an OpenAI-compatible embeddings endpoint to add meaning:

    HUB_EMBED_URL=http://127.0.0.1:8080/v1     # POST <base>/embeddings
    HUB_EMBED_MODEL=<model name>               # optional
    HUB_EMBED_TOKEN=<bearer>                   # optional
    HUB_EMBED_QUERY_PREFIX="query: "           # asymmetric models: set BOTH prefixes the
    HUB_EMBED_DOC_PREFIX="passage: "           # model was trained with, or recall drops silently

Then run `python manage.py semantic_index` on a schedule (and after bulk imports). No write path
ever embeds; the work is derived (indexable records minus stored vectors, keyed by a text hash),
so an edited record re-embeds itself and a record written while the embedder was down simply
waits. Vectors live in a sqlite sidecar in `HUB_DIR`, never in the ledger. `--probe` asks the
model what it actually returns (dimension, norm, a related vs. unrelated cosine) before you trust
it; `--coverage` prints what is embedded.

Scores are fused convexly (`alpha=0.3` lexical) with a CSLS hub penalty computed by the index
job. A record written since the last index run has no vector yet; it is scored by its wording
ALONE, at full weight, never as "dense = 0" — a missing vector is unknown, not dissimilar, and
treating it as zero capped every new record at 0.3 so the lesson written a minute ago ranked
below records that shared none of its words. Between index runs a new record is therefore
findable by its words at its true rank, and by meaning once the job has embedded it. Those constants were measured on one board; **measure yours** with
`python manage.py retrieval_eval`, which scores every configuration over the board's own
answered asks (a question somebody asked, linked by the board to the reply that resolved it —
data nobody wrote for an evaluation), fits alpha on one half and reports on the other across
five splits, and ranks ties pessimistically. Move `CONVEX_ALPHA` only to a value that run
reported. It needs at least ten answered asks.

When anything could not be seen, `metadata.partial` carries a `<memory-partial>` block and every
surface shows it; when the answer is whole it is silent. A search that matched nothing says it
is a fact about the words used — ask again in different words.

## 3. Deliver before each prompt

Wire the client into your agent harness's prompt hook (any harness that runs a command on
session start and on each prompt, passing JSON with `session_id`, `hook_event_name` and
`prompt` on stdin):

    HUB_API_BASE=https://board.example/hub HUB_AGENT_ID=alice \
      python -m hub_core.client prompt-context --hook

It asks `guidance.json` for the index ranked by the console's focus (by meaning with an embedder; by the focus's WORDS without one, and the header says which) (`--focus`, `HUB_FOCUS`, or
the first 400 characters of the prompt), prints the rows the session does not already hold
(a per-session receipt in `HUB_CLIENT_STATE_DIR`, reset on session start), names the relevant
rows it already holds in one line, and keeps the whole output under 9,500 characters — rows that
did not fit are written to a pack file whose path is printed, and only rows that rendered become
receipt keys. An unreachable board prints one marked line and exits 0.

**Ranked by words is cut, never padded.** Without a usable embedder (none configured, or down)
the index is ranked by BM25F over each row's rule and story, and every matching row carries
`lexical_score`: its score as a share of THIS query's own ceiling (the score a record matching
every query term fully would reach — terms no record contains count, because they are the part
of the question the corpus does not answer). Identifiers in the focus (board ids, shas, run
counts) are stripped first: they are never shared vocabulary. Scores relative to the best hit
came back flat and cut nothing; against the ceiling they separate. The block then shows only the
rows that clear the cut (the top row at 0.25 or more, a later one at 0.24 and 60% of the top, at
most four) under a header that says the ranking was by words — and when nothing clears it, it
says so in one line instead of delivering standing-order titles that read as relevance.

**A down embedder is asked once a minute, not once a prompt.** After a transport failure the
focus and overlap embeds are skipped for 60 s (`semantic.EMBED_RETRY_S`); a focus whose vector
is already cached still ranks by meaning. An adjudication pass likewise takes the first
"unreachable" as the answer for the rest of the pass and reports `embedder` in its summary,
instead of paying the timeout once per lesson.

**One decider for who delivers.** If a machine hands per-prompt knowledge to a local mirror
(its own recall ranks the mirrored corpus), exactly ONE process decides — switch on, mirror
caught up, local recall healthy — and writes the bit with its decision time into a sidecar both
sides read back. Two deciders with different freshness rules duplicate some prompts and starve
others; a decision older than a few heartbeats (the decider died) means the hub block.

A machine that wants the whole corpus locally (to rank offline, or to feed another tool) mirrors
it with `python -m hub_core.client knowledge-sync --out <file>`, which pages
`/hub/knowledge/since` once and afterwards asks only for what changed.

## 4. Publish a capability catalog from another repository

Teams often keep "what an agent can already do here" — tools, recipes, contracts — in a separate
repository where it is reviewed like code. Point the hub at a local checkout:

    HUB_CAPABILITY_REPO=/srv/catalog         # a git checkout
    HUB_CAPABILITY_REF=origin/main           # default HEAD
    HUB_CAPABILITY_EXPORT=capabilities.export.json
    HUB_CAPABILITY_MANIFEST=manifest.json    # optional; read at the SAME commit
    HUB_CAPABILITY_FETCH=1                   # fetch before resolving (a mirror)

The export is `{"items": [{"name", "kind", "what", "when", "get", "kit"?}]}`. The hub resolves
the ref once and reads every file at that sha; refuses a publication with a missing field, a
duplicate name, an item the board's own `cap` schema would not accept, or a `kit` the manifest
at that commit does not carry; refreshes in the background at most once a minute; and keeps
serving the last complete publication (cached on disk) with `publication.state = cached` and the
refusal reason. Catalog items join search, the index, and the mirror feed.

## Receipts belong to the adopter

Whether meaning-based retrieval helps on YOUR board is an empirical question: run
`retrieval_eval` against it and read the table, including when it says the fused row is not
better than the lexical one.
