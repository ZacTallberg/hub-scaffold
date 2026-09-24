# Many agents, one board — coordination, records, and publishing doctrine

Several agent consoles working one project will collide unless the board tells them about each
other, tells them in the right place, and gives them a vocabulary for what they learned. This is
the doctrine; adopt it by pasting the relevant parts into your governance files and by serving
your doctrine through `/hub/doctrine.json` (below).

## Crossover: compute it once, tell only the pair

Every console already reports where it is (presence: machine, session, working directory, focus)
and what it holds (task leases, error claims). The COMPARISON between consoles belongs on the
hub, computed once, and the answer belongs only in the two consoles it concerns. Shipping every
agent a roster of everybody else's work on every prompt is expensive and gets ignored.

Kinds, strongest first:

| Kind | Fires when |
|---|---|
| **file** | two consoles edited the same file recently (edits made through a shell, a heredoc or a subagent count; the path carries its project, so `app/views.py` in two repositories is not one file) |
| **problem** | both hold or name the same board problem, or both are triaging the error queue |
| **task** | both hold the same task, or one holds a task about the project the other is working in untracked |
| **project** | same project AND the same subsystem or subject — co-location alone produces nothing |
| **alignment** | different projects, the same external system or terms — worth a message, not a collision |

What deliberately does NOT fire, each measured as noise on the origin system:

- one person's own windows pairing on project or alignment — five consoles in one repository
  is how a person works; only the same FILE, problem or task between them is a collision;
- an unattended one-off session being told anything (nobody is reading it), or pairing with a
  person on anything weaker than a file, problem or task;
- a console standing above every project being "aligned" with anyone;
- "X is working in a repository Y refers to" — referring to a repository is reading it.

Each signal is announced once per side while its condition holds; it repeats only when it
CHANGES (a new file, a new subject) or after a long quiet interval.

### When a crossover reaches you

Act on it before you continue, and coordinate the work rather than standing down:

1. **Send your evidence and a proposed split** — through the board's ask/directive loop
   (`python -m hub_core.client ask …` / `directive …`) or your runtime's own messaging.
2. **Divide useful work.** On a shared problem: exchange the reproduction, split diagnosis,
   implementation and focused verification. On a shared task: split complementary
   deliverables. On related components: agree the interface and implement the halves.
3. **One editor per file, one accountable owner, one integrator.** A shared file needs one
   editor at a time; the other console works a separate component, investigates an open
   question, or reviews the patch. Helping is not a reason to take over the owner's lease or to
   file a duplicate task; contributors send results to the owner, and one integrator assembles
   and ships them — never competing pushes of the same work.
4. **Reuse verification.** Run the smallest check the changed behaviour justifies and share the
   result with the commit it applies to. Repeating a peer's passing check without a new change
   is not a contribution.
5. **If a console is on YOUR problem, read its trail first** — its task checkpoints, its
   findings, the board search. A second investigation should answer a different question.

Say what you are on in your own words (`presence --focus "<text>"`): an honest focus is what
lets the hub match you to the person who would want to know.

## Record the right KIND of thing

When the only record verb is "lesson", everything becomes a lesson and every other tab goes
stale. The client and the MCP `record` tool speak five kinds; use the one that fits, the moment
it is true — a finding you meant to file is indistinguishable from one you never had.

| Kind | Verb | What it is |
|---|---|---|
| **finding** | `finding "<what>" --evidence "…"` | a fact you DISCOVERED about how a system actually behaves; it needs evidence, not a rule |
| **lesson** | `note` in the `gotcha` category | a RULE earned from a mistake, stated so the next person avoids it; if it does not generalise past the incident, it is a finding |
| **method** | `method "<procedure>" --how "…"` | a procedure the project both follows and exhibits — reusable practice, not a one-off |
| **gap** | `gap "<what is missing>" --severity P0..P3 --evidence "…"` | a named deficiency somebody could own and close; a gap nobody can act on is a rollup |
| **review** | `review "<question>" --context "…"` | a human gate: a question only a person may answer, DELIVERED to the operator's inbox before the thing ships rather than discovered after |

Architecture decisions and feature specs stay with whoever holds that authority on your board
(`adr` and `feat` need their own write scopes and full content, which no one-line verb can
supply honestly).

## Publishing doctrine: control bytes, quoted counts, and audiences

- **Refuse control characters at the write seam.** A backslash escape eaten between an author's
  source and a publish call (`\a` inside a Windows path) puts a raw BEL byte into the text: valid
  UTF-8, valid JSON, invisible on the page — and the one path the instruction named no longer
  exists. Every board write now answers `422 control_chars` naming the field and offsets
  (`hub_core/textguard.py`); fix the SOURCE and resend. The operational error stream is exempt:
  a stack trace may legitimately carry ESC colour codes, and a failure refused at the door is a
  failure nobody sees.
- **Quote what the tool printed, with the date.** A count in doctrine ("nine dimensions, 52
  rules") drifts the day the tool changes. Date-stamp every such number and tell readers to
  quote the tool's own output from today, never a number from memory or from the doctrine.
- **One source, fenced by audience.** Doctrine written for the most privileged reader invites a
  limited contributor's agent to hallucinate capabilities it does not have; forking the
  document per audience guarantees drift. Fence the audience-scoped parts in place:

  ```markdown
  <!-- facet: ops -->
  Production credentials are issued from the vault; pull what the deploy needs.
  <!-- /facet -->
  <!-- facet: !ops -->
  The deploy supplies the credentials your service needs. Missing one? Ask.
  <!-- /facet -->
  ```

  `GET /hub/doctrine.json?doc=<name>` (client `doctrine`, MCP `read_doctrine`) serves the
  document rendered for the presenting credential: a credential with scope `facet:ops` (or
  `facet:*`, or `*`) sees the first block, anyone else the second, an anonymous reader no facet
  at all. Hidden blocks are omitted without a trace, an unterminated fence hides to the end of
  the document, and the marker lines vanish for everyone. Markers match in any case, and a line
  that looks like a fence attempt but is not exactly a marker (trailing text, a typo'd form)
  hides everything after it from every reader below `*` — a fence mistake costs the narrow
  reader text, never leaks it. To mention the syntax in served prose, escape it
  (`&lt;!-- facet: x --&gt;`). Visibility is decided by the
  credential, never by a request parameter. Name the served documents with `HUB_DOCTRINE_FILES`
  (default: `PROJECT/DOCTRINE.md`, `CHARTER-CORE.md`, `AGENTS.md`).
