"""The Assistant Canon, as data — so "is this assistant complete?" has an answer.

``../README.md`` (the kit's own) states how the families are used. There
are SIXTY-FOUR of them, in two halves. The first thirty-two were read out of
thirteen shipped catalogs -- somebody built it, it worked, it became a family.
The second thirty-two were DERIVED, because a canon taken only from what has
been built is a ceiling at the current best and cannot answer "what has nobody
here thought of yet?" -- see the long comment at the halfway mark.
Prose is what people read; this is what a machine can check. The audit in
:mod:`assistant.audit` walks an app's live tool registry against this table and
names the families it has nothing for — which is the whole point, because the
failure this library exists to prevent is not a bad tool, it is a MISSING one
that nobody counted.

The lesson it encodes: an assistant shipped with eleven read-only lookups and was called
useless by the person it was built for; it was rebuilt to two dozen tools across four families
and reported as done, while the sibling app it was modelled on had over a hundred tools across
twelve. Nothing was broken. It was simply not finished, and no test could have said so. A count
could.

WHAT A CANONICAL NAME IS. The ``tools`` on each family are the names shipped
apps have settled on, taken from the apps that shipped them. An app is not required to
use the name — ``list_routes`` and ``list_accounts`` are the same family member —
so the audit matches on family COVERAGE (did anything answer this question?) with
the names as the hint. An app that deliberately has no answer declares the family
in ``ASSISTANT_GAPS`` with its reason, and the audit prints the reason instead of
a gap. "We didn't get to it" is not a reason.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Family:
    """One canonical capability family."""

    key: str
    title: str
    #: The question a person brings. This, not the tool list, is what the family IS.
    question: str
    #: What the family owes whoever relies on it.
    guarantee: str
    #: Canonical names, from the apps that shipped them. Hints, not a contract.
    tools: tuple[str, ...] = ()
    #: Families that are genuinely inapplicable to some apps, and the shape of app
    #: that may skip them. Everything else must be implemented or declared.
    skippable_when: str = ""
    #: "observed" (read out of a shipped app's catalog) or "derived" (from the question grid).
    learned_from: str = ""
    #: Which SPECIALIST LANE(S) answer this family — see ``LANES``. Plural on purpose:
    #: ``decomposition`` ("where does that 47 come from?") is both an analysis and an
    #: evidence question, and forcing one lane would hide it from the other. Left empty
    #: a family is UNROUTED: ``assistant.audit`` counts it and says so, because a family
    #: no lane offers is one the orchestrator can never pick — invisible in exactly the
    #: way a missing tool is, and for the same reason it must be a count and not a test.
    lanes: tuple[str, ...] = ()


FAMILIES: tuple[Family, ...] = (
    Family(
        "core_reads", "Core record reads",
        "What is in here?",
        "Every first-class object listable, searchable and inspectable. Search "
        "returns the total for the WHOLE match beside the rows shown. Rows are "
        "evidence, NEVER a total — the totals tool owns totals, or the model sums "
        "a sampled page and is confidently wrong. "
        "WHOSE rows: a per-user noun is read through the ACTOR's scope on every read, and one "
        "with an owner column but no declared scope gets no generic tool at all (fail closed, "
        "named as skipped) rather than every user's rows; work fanned out to threads carries "
        "the actor's context, and a scoped total says 'you can see', never 'this app holds'. "
        "One record means ONE: matches are counted in the database and several sharing a "
        "label are refused with each one's id, never answered with the first.",
        ("list", "search", "detail", "recent"), learned_from="observed", lanes=("retrieve",)),
    Family(
        "primitives", "Generic count, breakdown and find",
        "How many? Break that down. I don't know which thing it is.",
        "A DB-side count for any filter, a group-by tally whose groups sum to its "
        "total, and one search box across EVERY entity type. "
        "A tally never counts a SOFT-DELETED row the app's own pages hide unless the filter "
        "names the flag, and says how many it left out. The breakdown takes the count's "
        "filter, so 'open X by Y' is one call whose groups are counts OF the filtered set; a "
        "choice filter takes a list for 'any of these'; a relation groups by the related "
        "record's name, never its pk.",
        ("count", "breakdown", "find"), learned_from="observed", lanes=("retrieve", "analyse")),
    Family(
        "self_description", "The app explaining itself — the front door",
        "What can you do? How does this work? Where do I look?",
        "Read from the LIVE registry and the app's own code, never a hand-written "
        "list. A hand-written list is wrong the day after it is written, and wrong "
        "in the direction that matters: it advertises what was removed.",
        ("app_capabilities", "how_it_works", "how_is_it_counted", "where_to_look",
         "documentation", "changelog", "links", "read_walkthrough"),
        learned_from="observed", lanes=("explain",)),
    Family(
        "preflight", "Pre-flight — what is possible, before you attempt it",
        "What am I allowed to change, and what does it require?",
        "The valid next states with the fields each REQUIRES, the real option "
        "values, the actual finding codes, the exact field names — read at call "
        "time. A proposal built from guessed names is refused at the form, and the "
        "person hears 'it tried and failed' rather than 'it asked'.",
        ("what_can_be_changed", "get_transitions", "get_create_requirements",
         "finding_catalog", "what_a_card_can_be", "versions_and_state", "data_dictionary", "list_scopes"), learned_from="observed", lanes=("explain", "act")),
    Family(
        "actions", "Doing the work",
        "Then do it.",
        "The app's OWN verb, never a reimplementation, so an assistant run and a "
        "button click produce the same record and the same audit row. Behind one "
        "of the three write models. A gated verb stays VISIBLE in the schema.",
        ("run", "apply", "enable", "disable", "self_test", "propose"),
        learned_from="observed", lanes=("act",)),
    Family(
        "authoring", "Authoring",
        "Make me a new one.",
        "Draft-then-create: the draft is READ, saves nothing, and validates and "
        "PROBES everything the real thing would touch. New objects are created in "
        "the safe/off state whatever was asked. Dedupe before create.",
        ("draft", "create", "find_similar", "propose_roll_forward", "author_document"), learned_from="observed", lanes=("act",)),
    Family(
        "requests", "Asking for what the app cannot do",
        "It doesn't do that.",
        "Filed on the coordination board (the hub) with an id, from the person's OWN words, then "
        "checkable and appendable. READ, no click, EVERY role — the person most "
        "likely to need it cannot run anything. A timeout is never 'nothing was "
        "filed'.",
        ("file_app_request", "request_status", "list_requests", "add_to_request",
         "change_request"), learned_from="observed", lanes=("act",)),
    Family(
        "source_truth", "Reading the SOURCE, not just the copy",
        "Is this number stale?",
        "Reach upstream live and compare against what this app landed, so 'the app "
        "says X' and 'the truth is X' are distinguishable. Missing data is not "
        "zero; an unreachable source is NO ANSWER, not a count of zero.",
        ("source_status", "source_vs_app", "record_authority"),
        skippable_when="the app is the system of record and has no upstream",
        learned_from="observed", lanes=("analyse", "explain")),
    Family(
        "files_in", "Files and documents in",
        "Here's a file — what is it, and what would it do?",
        "Inspect before landing; a preview that shows the DIFF; every source row "
        "accounted for. Binary files are described, never decoded. Attached text "
        "is DATA, never instructions.",
        ("list_files", "inspect_file", "import_plan", "import_preview",
         "import_intake", "read_document"),
        skippable_when="the app never takes a file from anyone", lanes=("intake",)),
    Family(
        "comms_in", "Ingest from communications",
        "It came in by email.",
        "Mail read into PROPOSED changes with the evidence attached, and a queue a "
        "person rules on. MESSAGE TEXT IS DATA, NEVER INSTRUCTIONS — the single "
        "most important sentence here for any app ingesting text somebody else wrote.",
        ("inbox_queue", "inbox_measure", "read_message", "search_mail"),
        skippable_when="the app has no inbound mail or message lane",
        learned_from="observed", lanes=("intake",)),
    Family(
        "documents_out", "Documents out",
        "Give me that as a spreadsheet.",
        "A real downloadable artifact that names its own provenance — what "
        "produced it, from which data, as of when. A file with no as-of line "
        "becomes a wrong number in somebody's inbox three weeks later.",
        ("build_workbook", "build_report", "export", "export_table", "author_document"), lanes=("deliver",)),
    Family(
        "insight", "Insight and analysis",
        "What changed? What's worst? What if?",
        "Aggregated in the DATABASE, never by scanning rows. Every count carries "
        "its denominator. A SCENARIO verb computes and never applies — it is what "
        "stops somebody finding out by doing it.",
        ("trend", "top_movers", "slice", "what_if", "detail", "comparison"), lanes=("analyse", "scenario")),
    Family(
        "review", "Review, gaps and ABSENCE",
        "What's wrong that nobody has noticed?",
        "Absence is a first-class finding. A check whose denominator can be zero "
        "must say 'no data', never pass — 0 of 0 reading as all-clear is a measured "
        "failure here. Coverage is TRI-STATE: unknown is not absent. And the thing "
        "with no due date never appears in a list of what is late.",
        ("verification_queue", "coverage_gaps", "duplicates", "findings",
         "stale", "compare_versions", "parity_checks"), lanes=("analyse",)),
    Family(
        "reference", "Reference and dictionary",
        "What does this field actually mean?",
        "Read from the CODE — the real registry, the real token set, the real "
        "choices — so the answer cannot drift from what the app does. "
        "A profile's related rows go through the RELATED entity's own scope -- a shared "
        "parent must never list another person's children.",
        ("data_dictionary", "metric_definition", "profile", "catalog"), lanes=("explain", "retrieve")),
    Family(
        "provenance", "Provenance, audit and people",
        "Who did this, and who else is in here?",
        "Field-level provenance: which source last wrote this, did a human edit "
        "it, is it LOCKED against automation, what automated writes were blocked. "
        "Two DIFFERENT permission decisions — can they sign in at all, and which "
        "scopes do they then see — are never one tool.",
        ("record_history", "history", "audit_trail", "who_typed_this", "field_provenance",
         "access_roster", "who_can_see", "who_else_is_here"),
        learned_from="observed", lanes=("retrieve",)),
    Family(
        "identity", "Cross-system identity and reconciliation",
        "Who is this person, really?",
        "Resolve across systems, walk the org chart, cross a register against the "
        "live roster to find hygiene gaps. Report the ambiguous and unmatched "
        "rather than guessing — a ticket is a fact, a name join is a LEAD.",
        ("lookup_person", "lookup_asset", "supervisory_chain", "org_tree",
         "check_roster", "join_questions"),
        skippable_when="the app holds no people and joins no external register",
        learned_from="observed", lanes=("retrieve",)),
    Family(
        "health", "Health, sources and settings",
        "Is anything broken? Is the data current?",
        "One table for every failure class. Per-source freshness. And the "
        "operational TOGGLES — check them before reporting a fault, because a "
        "disabled sync is not a broken one. SELF-DIAGNOSE before declaring an "
        "outage: silence from one vantage is not a verdict.",
        ("app_errors", "app_health", "sources_status", "app_settings",
         "connection_status", "ingest_runs"), learned_from="observed", lanes=("explain",)),
    Family(
        "self_memory", "The assistant's own memory",
        "What did I ask you last week? Where did that number come from?",
        "THREE DISTINCT histories, and conflating them loses the answer: what was "
        "ASKED, what was LOOKED UP (with arguments and refusals), and what was "
        "PROPOSED. Plus the team's shared threads. And measure your own quality — "
        "with denominators.",
        ("list_conversations", "read_conversation", "tool_call_history",
         "change_history", "recent_team_questions", "self_quality"),
        learned_from="observed", lanes=("retrieve", "explain")),
    Family(
        "surface", "The surface itself — agent-composable UI",
        "Put that on the dashboard.",
        "The dashboard is data the assistant can read, compute and rearrange, "
        "including a SPATIAL view — what sits left, right, above, below, where "
        "there is empty room, what overlaps. PREVIEW before propose: a card that "
        "counts zero should be fixed or flagged, not staged silently.",
        ("what_a_card_can_be", "whats_on_the_dashboard", "read_card",
         "preview_card", "propose_card", "propose_placement", "look_at_the_dashboard"),
        skippable_when="the app has no composable dashboard",
        learned_from="observed", lanes=("act",)),
    Family(
        "self_configuration", "App self-configuration",
        "Add a column. Turn that notification on.",
        "Columns, custom fields and their options, relabelling built-ins, "
        "notification rules, membership. STATE THE CONSEQUENCE in the proposal, "
        "not just the change — changing a column's stage moves every number on the "
        "board, and turning a rule on sends real email to real people.",
        ("propose_a_column", "propose_a_variable", "propose_an_option",
         "propose_a_notification_rule", "propose_a_membership_change"),
        skippable_when="the app has no user-configurable structure",
        learned_from="observed", lanes=("act",)),
    Family(
        "schema_evolution", "Schema and metadata evolution",
        "Add a category. Reword that guidance.",
        "What re-rates or re-counts everything — scales, bands, denominators — is "
        "NOT changeable by the same verb that rewords a label, and the refusal "
        "says why.",
        ("propose_schema", "propose_schema_update", "propose_schema_guidance",
         "get_schema", "list_schemas"),
        skippable_when="the app's schema is fixed in code",
        learned_from="observed", lanes=("act",)),
    Family(
        "approvals", "Approvals, signoff and lifecycle",
        "Whose signature am I waiting on?",
        "THE PERSON WHO APPLIES IS THE SIGNER. The assistant never signs.",
        ("signoff_status", "propose_signoff", "propose_advancing_the_workflow",
         "propose_review", "propose_roll_forward", "change_position"),
        skippable_when="the app has no approval chain or review cycle",
        learned_from="observed", lanes=("retrieve", "act")),
    Family(
        "reversal", "Reversal and recovery",
        "Put it back.",
        "Soft delete with a list of what was removed and by whom; revert ONE field "
        "from append-only history; preview what undoing would do. Append-only: a "
        "revert writes a NEW event carrying the old value forward. Where no "
        "automated revert exists, SAY SO rather than implying one.",
        ("propose_removal", "removed_items", "propose_revert", "revert_preview",
         "revert_landing"), learned_from="observed", lanes=("act",)),
    Family(
        "bulk", "Bulk and batch",
        "Do that to all of them.",
        "REQUIRES a filter and refuses to stage a change to everything in scope, "
        "with the card listing every record it would touch. Load-from-paste reads "
        "a pasted table into rows and refuses while any row has a problem.",
        ("propose_bulk_update", "bulk_preview", "preview_a_load",
         "propose_loading_items", "propose_bulk_change", "import_preview", "run_import"), learned_from="observed", lanes=("act",)),
    Family(
        "comms", "Communications and meetings",
        "What was said about this?",
        "Meetings, transcripts, search across comms, topic timelines, who "
        "committed to what.",
        ("list_meetings", "read_transcript", "search_transcripts", "my_commitments",
         "topic_timeline", "person_activity"),
        skippable_when="the app has no comms corpus",
        learned_from="observed", lanes=("retrieve",)),
    Family(
        "roadmap", "What is being built",
        "Is anyone working on what I asked for?",
        "The board read live. SHIPPED is decided by the running build, never by "
        "the board's word for it.",
        ("up_next", "planning_items", "planning_progress", "whats_live"), lanes=("retrieve", "explain")),
    Family(
        "corpus", "Document and corpus intelligence",
        "What do our documents say, and which copy is current?",
        "Every hit carries a REAL citable ref a person can open, plus an internal "
        "handle that is NEVER written into an answer. Exact lookup says plainly "
        "when the identifier does not exist, which search cannot. Completeness is "
        "reported per failure REASON.",
        ("document_search", "document_lookup", "document_versions", "revision_diff",
         "corpus_completeness", "folder_map", "file_index", "list_contexts", "workbook_find", "workbooks", "workbook_sheet", "document_compare"),
        skippable_when="the app holds no document corpus",
        learned_from="observed", lanes=("retrieve",)),
    Family(
        "analysis", "Deterministic analysis over structured data",
        "Compute that for me.",
        "Profile first, then ONE CLOSED operation at a time, computed in code. "
        "Typed joins return the unmatched KEYS. Real statistics. Exports carry a "
        "citation on EVERY ROW. THE MODEL CHOOSES THE OPERATION AND NEVER PRODUCES "
        "THE NUMBER. Identifier columns are not summed. "
        "The call's own filter is pushed down to the row source so it narrows BEFORE the "
        "source's row cap (and applied again here, so a looser source stays exact); a filter "
        "applied after the cap only ever searched the first N rows.",
        ("sheet_schema", "sheet_query", "sheet_join", "statistical_analysis",
         "render_chart", "export_table", "file_schema", "file_query", "file_join",
         "file_stats", "export_rows", "compare_files", "workbook_sheet"),
        skippable_when="the app holds no tabular data",
        learned_from="observed", lanes=("analyse",)),
    Family(
        "schedule", "Schedule and dependency",
        "What's on the critical path? What moves if this slips?",
        "The critical path taken from the schedule's OWN exported float, not "
        "re-derived. What is executable right now. What would move — reported, "
        "never rescheduled.",
        ("schedule_query", "schedule_join", "work_readiness", "stage_ageing",
         "what_would_move_if_this_slips"),
        skippable_when="the app has no schedule or dependency graph",
        learned_from="observed", lanes=("temporal", "scenario")),
    Family(
        "obligations", "Obligations, outcomes and expected records",
        "What did we commit to, and is it evidenced?",
        "THE MODEL READS AND THE TOOL VERIFIES — never the other way round. Each "
        "expectation carries the verbatim sentence that invoked it. Outcome "
        "tallies declare a CLOSED label vocabulary first.",
        ("commitment_register", "class_outcomes", "expected_records",
         "work_item_records", "closeout_register", "change_position"),
        skippable_when="the app holds no prose obligations",
        learned_from="observed", lanes=("retrieve", "explain")),
    Family(
        "capability_files", "Capability-based access to files",
        "Read me that file.",
        "LIST then READ: the name must come from the listing, a path is never "
        "accepted, and a name that was not handed out is refused. The allow-list "
        "IS the capability, so there is no path to traverse.",
        ("fileshare_list", "fileshare_read", "list_attachments", "read_attachment",
         "read_source_file", "read_file", "read_pdf_attachment", "inspect_file", "list_files"),
        skippable_when="the app exposes no file bytes to the assistant",
        learned_from="observed", lanes=("retrieve",)),
    Family(
        "merge", "Merge, publish and contested state",
        "What would this publish overwrite?",
        "Totals, rows kept, rows added, and EVERY CONTESTED ROW with both "
        "timestamps and which side wins — before anything is written. A two-writer "
        "conflict is shown before it is resolved, never after.",
        ("merge_preview", "publish_preview", "delivery_preview", "dry_run", "import_preview", "revert_preview"),
        skippable_when="the app writes to nothing anyone else writes to",
        learned_from="observed", lanes=("scenario", "act")),

    # ======================================================================
    # DERIVED, NOT OBSERVED — the second half of the canon
    # ======================================================================
    #
    # Everything above was read out of thirteen shipped catalogs: somebody built
    # it, it worked, and it became a family. That method has one failure it cannot
    # see, and it is the failure this library exists to prevent. A canon derived
    # ONLY from what has been built is a ceiling at the current best, and the
    # question it can never answer is "what has nobody here thought of yet?"
    #
    # So these are derived from the SHAPE of the question instead. An app is a set
    # of records, governed by rules, changed by people, over time, inside an
    # organisation. Every question anybody brings to one is a cell of
    #
    #     {record · rule · person · time · organisation}
    #         x
    #     {what is · what was · what will be · what if · what should ·
    #      why THIS one · where else · tell me when · help me do it elsewhere ·
    #      teach me · and the conversation itself}
    #
    # Walking that grid against the families above shows where the observed canon
    # is thin, and it is thin in the same place every time: it answers WHAT IS
    # extremely well and almost nothing else. The families below fill the rest.
    #
    # Each still carries the bar the observed ones do: the QUESTION in a person's
    # words, and a GUARANTEE that is a rule somebody can be held to rather than a
    # description. `learned_from` says "derived" where no app has shipped it yet --
    # honestly, because a citation nobody can check is worse than none.

    # ---- what if, and what follows ---------------------------------------
    Family(
        "whatif", "Simulation — what happens if",
        "What happens if I change this?",
        "Run the app's OWN rules over a hypothetical and report what would move, "
        "writing NOTHING. Two halves and apps ship only the first: the delta, and "
        "the THINGS THAT DO NOT MOVE for a reason worth knowing (a row already at "
        "the cap, a record the rule excludes). A simulation that reports only "
        "winners reads as a smaller change than it is. The answer names which rules "
        "it ran and which it could not, because a partial simulation presented "
        "whole is the confidently-wrong number an app can ship. "
        "The app's rules are evaluated against the NAMED record, never a blank one.",
        ("simulate", "what_if", "model_change", "recalculate_preview"),
        skippable_when="the app computes nothing from its records — it only stores "
                       "and shows them, so there is no rule to run over a guess",
        learned_from="derived", lanes=("scenario",)),
    Family(
        "impact", "Forward lineage — what depends on this",
        "What breaks if this goes away?",
        "Walk dependency FORWARD from one record and report everything that would "
        "be affected, each with WHY it is reached. Provenance walks backward to "
        "where a value came from; this is the other direction and no app that has "
        "only one of them can answer an offboarding, a decommission or a deletion. "
        "Cycle-guarded and depth-capped, saying where it stopped and why — a walk "
        "that silently truncates reports a smaller blast radius than the real one.",
        ("impact_of", "depends_on_this", "downstream_of", "blast_radius", "trace_figure", "change_impact"),
        skippable_when="records here reference nothing and nothing references them",
        learned_from="derived", lanes=("scenario",)),
    Family(
        "scenario", "Scenarios and drafts of the whole set",
        "Make me a copy I can play with.",
        "A named branch of the record set that a person can change freely, compare "
        "against live, and throw away. Live is never touched and the scenario is "
        "never mistakable for it — every payload says which one it read. Promoting "
        "a scenario is an ACTION with the write model's gate; nothing here merges "
        "on its own.",
        ("create_scenario", "list_scenarios", "compare_to_live", "discard_scenario", "versions_and_state", "compare_versions", "what_if"),
        skippable_when="the app holds one live set and planning against alternatives "
                       "is not something anybody does here",
        learned_from="derived", lanes=("scenario", "act")),

    # ---- why THIS number, and what it was --------------------------------
    Family(
        "decomposition", "Why THIS number is what it is",
        "Where does that 47 come from?",
        "Take ONE computed figure apart into the rows and the arithmetic that "
        "produced it, down to records a person can open. `reference` says how the "
        "metric is DEFINED; this says how THIS instance of it was reached. The "
        "parts must sum to the whole and the tool says so, or it names the residue "
        "— a decomposition that does not reconcile is the one that gets quoted in "
        "a meeting and then falls apart.",
        ("explain_number", "drill_down", "contributors_to", "reconcile_total", "figure_detail", "trace_figure", "how_it_works"),
        skippable_when="the app shows only stored values and computes no figure "
                       "anybody would question",
        learned_from="derived", lanes=("analyse", "explain")),
    Family(
        "as_of", "Point in time — what it looked like then",
        "What did this look like on the 3rd, and what changed since?",
        "Reconstruct state AS OF a moment and diff it against now. `provenance` "
        "answers who changed a field; this answers what the whole thing looked "
        "like, which is what a person asks when a number they quoted last week no "
        "longer matches. Where history is partial the tool says from WHEN it can "
        "answer and refuses earlier — a reconstruction from an incomplete log "
        "looks exactly like a complete one.",
        ("as_of", "state_on", "changed_since", "point_in_time", "history",
         "year_over_year"),
        skippable_when="the app keeps no history — it stores only current state, "
                       "and says so rather than reconstructing from guesses",
        learned_from="derived", lanes=("temporal",)),
    Family(
        "forecast", "Where this is heading",
        "At this rate, when?",
        "A projection that states its METHOD, its window and its uncertainty, in "
        "the same payload as the number. A bare forecast is indistinguishable from "
        "a measurement once it is in a person's notes, so every figure here is "
        "labelled projected and is never summed with a measured one. Too few "
        "points is a refusal naming how many it had, never a line through two "
        "dots. "
        "A flow series totals COMPLETE days only, and a weekly or monthly figure extrapolated "
        "from fewer days is labelled projected, never shown as measured.",
        ("forecast", "run_rate", "projected_completion", "trend_to"),
        skippable_when="nothing here accumulates over time, so there is no rate to "
                       "carry forward",
        learned_from="derived", lanes=("temporal",)),
    Family(
        "capacity", "Effort, cost and room",
        "How much work is this, and do we have room?",
        "Answer in the app's OWN units — hours, dollars, slots, licences — and name "
        "the unit every time. Committed, available and the difference are three "
        "numbers, never one: an app that reports only headroom hides an overcommit. "
        "Where the app holds no effort figure it says so rather than estimating "
        "from row counts, which is a made-up number with a real number's shape.",
        ("capacity", "effort_estimate", "cost_of", "utilisation", "headroom"),
        skippable_when="records here carry no cost, effort or quota",
        learned_from="derived", lanes=("analyse", "temporal")),

    # ---- what should I do ------------------------------------------------
    Family(
        "prioritisation", "What to do first",
        "Where do I start?",
        "A ranking whose REASON is in the payload beside every row — the stakes in "
        "the app's own terms, the rule that put it there, and what would move it. "
        "A bare order is an opinion the model will defend; a ranking a person can "
        "argue with is a tool. The ranking is computed by CODE from declared "
        "weights, never by the model reading the list and choosing.",
        ("what_should_i_do", "priority_queue", "triage", "next_best_action", "verification_queue", "findings", "up_next"),
        skippable_when="everything here is equally urgent by design — a log, an "
                       "archive, a reference table",
        learned_from="derived", lanes=("analyse",)),
    Family(
        "anomaly", "What looks wrong",
        "Is anything off here?",
        "Outliers, contradictions and impossible values, each with the COMPARISON "
        "that made it an outlier — the cohort, the window, the threshold. `review` "
        "finds absence; this finds the unusual PRESENT, and the two are different "
        "searches. An anomaly with no stated comparison is an accusation, and the "
        "payload says plainly that an outlier is a lead, not a finding.",
        ("anomalies", "outliers", "contradictions", "impossible_values", "findings", "import_exceptions"),
        skippable_when="the register is small enough that a person reads every row",
        learned_from="derived", lanes=("analyse",)),
    Family(
        "quality", "How good is this data",
        "Can I trust what is in here?",
        "Per field: how stale, how precise, how often it conflicts with another "
        "source, and the DENOMINATOR for each. Distinct from coverage — a field "
        "100% populated with values last touched in 2019 reads perfect to a "
        "coverage check. Tri-state throughout: good, bad and NOT MEASURED, because "
        "folding unmeasured into either side is how a quality score becomes the "
        "least trustworthy number in the app. "
        "A record with nothing publishable says '0 fields published', never 'empty' -- an "
        "absence of data and an absence of permission are different findings.",
        ("data_quality", "staleness", "field_confidence", "conflicting_values", "data_freshness", "parity_checks", "import_exceptions", "import_intake"),
        skippable_when="every field here is entered once by one system and cannot "
                       "age or conflict",
        learned_from="derived", lanes=("analyse",)),
    Family(
        "reconciliation_internal", "Two sources inside one app disagree",
        "Which of these two is right?",
        "Cross two feeds the app ITSELF holds and report agreement, disagreement "
        "and PRESENT-IN-ONLY-ONE as three classes, never two. `identity` crosses a "
        "register against a roster of people; this crosses any two record sets, and "
        "the third class is the one that matters because a row missing from one "
        "side never appears in a comparison of the rows they share.",
        ("reconcile_sources", "agreement", "only_in", "value_conflicts", "source_vs_app", "compare_files", "parity_checks", "sheet_reconcile"),
        skippable_when="the app ingests from exactly one source",
        learned_from="derived", lanes=("analyse",)),

    # ---- where else ------------------------------------------------------
    Family(
        "federation", "What the other apps know",
        "Is this tracked anywhere else?",
        "Name the OTHER apps that hold this subject and what each is for, and stop "
        "there. It NEVER answers on their behalf: a summary of another app's data, "
        "fetched or remembered, is a second copy that drifts and a number nobody "
        "can trace. A pointer with a link is the whole deliverable, and it says "
        "when its directory was last read.",
        ("also_tracked_in", "related_apps", "app_pointer", "source_search", "list_requests"),
        skippable_when="nothing this app holds is held in any other app",
        learned_from="derived", lanes=("retrieve",)),
    Family(
        "compliance_map", "Which rule this satisfies",
        "Which control does this cover?",
        "Map records to an EXTERNAL framework — a standard, a contract, a policy — "
        "citing the clause by its own identifier so a reader can open it. Coverage "
        "is reported with the framework's full clause list as the denominator, so "
        "'we cover 40 controls' can never be read as 'we cover it'. A clause with "
        "nothing mapped is stated, not omitted.",
        ("controls_for", "framework_coverage", "clause_citation", "unmapped_clauses"),
        skippable_when="no external framework governs what this app holds",
        learned_from="derived", lanes=("retrieve", "explain")),
    Family(
        "precedent", "What we did last time",
        "Has this come up before?",
        "Find the closest PRIOR cases and say what happened to each — not just that "
        "they resemble this one. Similarity is computed and its basis is named, and "
        "the outcome is read from the record rather than inferred from its state. "
        "A precedent with no outcome is a lookalike, and the payload says which it "
        "is offering.",
        ("find_similar_past", "precedent", "how_was_this_handled", "outcome_of", "list_notes", "import_history"),
        skippable_when="the app holds no closed cases to learn from",
        learned_from="derived", lanes=("retrieve",)),

    # ---- tell me when ----------------------------------------------------
    Family(
        "watch", "Standing questions",
        "Tell me when this happens.",
        "A question the person OWNS: named, listable, editable and switchable off "
        "by the person who made it, with its own firing history. Two failures it is "
        "written against — a watch that fires forever because nobody can find the "
        "off switch, and one that silently stopped and looked exactly like quiet. "
        "So every watch reports when it last EVALUATED, not only when it last "
        "fired.",
        ("watch", "list_watches", "unwatch", "watch_history"),
        skippable_when="nothing here changes between the times a person looks",
        learned_from="derived", lanes=("act", "temporal")),
    Family(
        "saved_questions", "Ask that again",
        "Run that one again next month.",
        "Name a question, re-run it, and keep the results so the ANSWER has a "
        "history rather than only the data. The saved thing is the question and its "
        "parameters, never a cached answer — a stored answer served later is the "
        "oldest way to show somebody a stale number with a fresh timestamp.",
        ("save_question", "run_saved", "list_saved", "question_history"),
        skippable_when="every question here is asked once",
        learned_from="derived", lanes=("act", "temporal")),
    Family(
        "escalation", "This needs a person",
        "Who decides this?",
        "Route a thing that cannot be settled here to the person who can settle it, "
        "WITH what they need to decide — the values, the options and the "
        "consequence of each. Naming a decider and sending them nothing is how a "
        "queue of escalations becomes a queue of questions nobody can answer. The "
        "assistant never decides on their behalf and never marks it decided.",
        ("escalate", "who_decides", "decision_package", "open_decisions", "file_app_request", "up_next", "findings"),
        skippable_when="every decision here is one the asker can make themselves",
        learned_from="derived", lanes=("retrieve", "act")),

    # ---- help me do it elsewhere -----------------------------------------
    Family(
        "export_out", "Out to the tools a person already uses",
        "Give me that in a form I can use somewhere else.",
        "A saved-view URL, a calendar feed, a query somebody can run themselves, a "
        "table shaped for a paste — the person's OWN tools, not another page here. "
        "Every export carries the filter that produced it IN the artifact, because "
        "a spreadsheet on somebody's desktop outlives every caveat said beside it. "
        "Distinct from `documents_out`, which builds a document the app owns.",
        ("share_link", "calendar_feed", "as_query", "copyable_table", "deep_link", "export", "build_workbook"),
        skippable_when="nothing here is useful outside this app",
        learned_from="derived", lanes=("deliver",)),
    Family(
        "programmatic", "Getting this without asking",
        "How do I pull this myself?",
        "The app's OWN endpoint, filter vocabulary and auth, read from the live "
        "routes rather than from a document — and the limits stated with it. An "
        "assistant that will not tell a capable person how to help themselves "
        "makes itself the bottleneck it was built to remove. A hand-written API "
        "note is wrong the day after it is written.",
        ("api_for", "query_syntax", "endpoint_catalog", "rate_limits"),
        skippable_when="the app exposes no programmatic surface at all",
        learned_from="derived", lanes=("deliver", "explain")),
    Family(
        "comms_out", "Telling somebody",
        "Let them know.",
        "DRAFT the message, resolve the recipients from the app's own roster, and "
        "show both to the person. Sending is an ACTION behind the write gate and "
        "never a side effect of asking for a draft — mail leaves the building and "
        "cannot be reverted, which puts it in the same class as a destructive "
        "write. Recipients are named individually; 'the team' is refused.",
        ("draft_message", "resolve_recipients", "notification_preview", "send"),
        skippable_when="the app sends nothing to anybody",
        learned_from="derived", lanes=("act", "deliver")),

    # ---- what I hand you -------------------------------------------------
    Family(
        "paste_in", "Text somebody pasted",
        "Here's what I copied — make it rows.",
        "Turn pasted text or a screenshot into structured rows with a per-row "
        "confidence and the ORIGINAL span beside each, landing nothing. `files_in` "
        "takes a file with a shape; a paste has none and is the commonest way real "
        "data arrives. Rows it could not parse are RETURNED as unparsed, never "
        "dropped — a silent drop is the one error nobody can see. "
        "A pasted table is read as LOGICAL records: a quoted multi-line cell is one value, "
        "and a row with an unbalanced quote is never reported as clean.",
        ("parse_paste", "table_from_text", "structure_this", "unparsed_rows"),
        skippable_when="nothing here is ever typed or pasted by a person",
        learned_from="derived", lanes=("intake",)),
    Family(
        "precheck_mine", "Check my list before I commit it",
        "Will this work if I upload it?",
        "Validate the PERSON's own data against the app's real rules and existing "
        "records, writing nothing and keeping nothing. Answers the three questions "
        "an upload cannot: what would be rejected and why, what already exists, and "
        "what would silently overwrite. The last is the one an import preview "
        "usually misses, and it is the expensive one.",
        ("validate_my_list", "would_this_import", "already_exists", "would_overwrite", "import_plan", "import_preview"),
        skippable_when="people never bring their own data to this app",
        learned_from="derived", lanes=("intake",)),
    Family(
        "media", "Pictures attached to records",
        "What's in this photo?",
        "Describe an attached image, drawing or scan and say WHICH engine read it "
        "and how confident it is. It never states a value read from an image as "
        "though it were a field: an extracted figure is quoted with the region it "
        "came from and marked as read-from-image, because a transcription error and "
        "a typed value are indistinguishable once both are plain text in an answer.",
        ("describe_image", "read_attachment", "extract_from_image", "read_document", "read_pdf_attachment"),
        skippable_when="no record here carries an image",
        learned_from="derived", lanes=("intake",)),

    # ---- the person, and the conversation --------------------------------
    Family(
        "authority", "What I am allowed to do",
        "Why can't I see this?",
        "Explain the ASKER's own permissions: what they can reach, what they "
        "cannot, and exactly what would change it. A refusal that does not say what "
        "would lift it teaches people the app is arbitrary. It reports the shape of "
        "what is hidden without its contents — 'there are 12 records you cannot "
        "see, ask X' — because a silent filter makes a partial answer look whole.",
        ("what_can_i_do", "why_refused", "who_can_grant", "who_can_see", "app_capabilities", "access_roster"),
        skippable_when="every signed-in person here sees and does the same things",
        learned_from="derived", lanes=("explain",)),
    Family(
        "working_set", "The pile I am building",
        "Keep those, add these, now do that to them.",
        "A named set built ACROSS turns, inspectable at any point, re-runnable, and "
        "the thing an action is finally applied to. Without it every multi-step "
        "request restarts from a sentence, and the model re-selects the rows each "
        "time — which is how an apply lands on a different set than the one the "
        "person reviewed. The set is IDs, held by the app; the model never carries "
        "it in prose.",
        ("start_set", "add_to_set", "remove_from_set", "show_set", "apply_to_set"),
        skippable_when="nothing here is ever done to more than one record at a time",
        learned_from="derived", lanes=("retrieve", "act")),
    Family(
        "clarify", "Asking back",
        "I don't know which one you mean.",
        "When a request is ambiguous the assistant ASKS, with the candidates and "
        "what distinguishes them, instead of choosing. The app keeps the record of "
        "what was ambiguous, because a recurring ambiguity is a naming problem in "
        "the DATA and nobody will ever find it from a chat log. Guessing quietly is "
        "the single most expensive failure an assistant has.",
        ("disambiguate", "candidates_for", "ambiguity_log"),
        learned_from="derived", lanes=("retrieve",)),
    Family(
        "teach", "Somebody new",
        "I'm new here — where do I start?",
        "A first path built from the app's REAL current state: the things waiting "
        "for this person, the one safe action to try, the vocabulary they will "
        "meet. Never a static tour, which is out of date the first time the app "
        "changes and teaches a newcomer something false on their first day.",
        ("get_started", "my_first_task", "glossary_here", "guided_next_step", "where_to_look", "data_dictionary", "documentation"),
        learned_from="derived", lanes=("explain",)),
    Family(
        "feedback", "That answer was wrong",
        "That's not right.",
        "Take the correction where the app can ACT on it: attached to the turn, the "
        "tool and the values it returned, and visible to whoever maintains the app. "
        "An assistant that accepts a correction and stores nothing has apologised, "
        "not learned — and the person who bothered to correct it will not bother "
        "twice.",
        ("report_wrong_answer", "correction_queue", "flag_result", "file_app_request"),
        learned_from="derived", lanes=("act",)),

    # ---- presentation ----------------------------------------------------
    Family(
        "units", "In my terms",
        "Show me that in my timezone.",
        "Convert time, currency and units, NAMING the zone, the rate and its date "
        "every time. A converted figure that does not carry its rate is a number "
        "that will be wrong later and cannot be checked now. Where a rate is "
        "unavailable it refuses rather than using a stale one — a quietly stale "
        "rate is a wrong number shown to a human with full confidence. "
        "A time with no zone is read in the app's own zone and reported in UTC, and a local "
        "time a daylight-saving change makes ambiguous or nonexistent is flagged, never "
        "silently resolved.",
        ("in_timezone", "in_currency", "convert_units"),
        skippable_when="one zone, one currency, one unit system, everywhere",
        learned_from="derived", lanes=("retrieve", "analyse")),
    Family(
        "geo", "Where",
        "What's at that site?",
        "Answer by place where the app holds one: what is at a location, what is "
        "near it, and how each record got its coordinates. Provenance matters more "
        "here than anywhere: a geocoded address and a surveyed point look identical "
        "in a payload and are not the same claim, so every position says which it "
        "is and how precise.",
        ("at_location", "near", "by_site", "position_provenance"),
        skippable_when="no record here has a place",
        learned_from="derived", lanes=("retrieve",)),
    Family(
        "evidence_pack", "The pack behind a claim",
        "Show me everything behind that.",
        "Assemble ONE artifact carrying a finding's values, its citations, its "
        "audit trail and the exact queries that produced it, so somebody who was "
        "not in the conversation can check it. Every other family produces answers "
        "that live in a chat; this produces the thing that survives the chat, and "
        "it is what an audit, a dispute or a handover actually needs.",
        ("evidence_pack", "citations_for", "reproduce_this", "audit_bundle", "trace_figure", "figure_detail", "transaction_detail"),
        learned_from="derived", lanes=("retrieve", "explain")),
    Family(
        "handoff", "Giving it to somebody else",
        "Hand this to someone.",
        "A package the RECEIVER can act on: the record, the history, what was "
        "already tried, what is still open, and who to ask. Named from the app's "
        "own roster, never from a name in a sentence. It states plainly that it has "
        "handed nothing over until the app's own assignment verb runs — an "
        "assistant that says 'handed to X' without a record is how work disappears.",
        ("handoff_package", "assign_to", "context_for", "whats_been_tried"),
        skippable_when="records here have no owner and are never passed on",
        learned_from="derived", lanes=("act",)),
)

#: Properties of the CATALOG, not of any one tool. The audit reports these as a
#: checklist an app answers for itself, because no registry walk can see them.
@dataclass(frozen=True)
class Lane:
    """One SPECIALIST: a kind of turn, not a corner of the app's data.

    The lanes are cut by what the turn has to DO, because that is what changes how it
    has to run. A retrieval turn wants many cheap reads and a wide net; an analysis turn
    wants few reads and room to compute; an act turn touches the write gates and must
    not be hurried. Cutting by domain instead (RAID · projects · committee) produces
    specialists that differ only in their nouns and re-learn the same reasoning badly,
    and it stops being a taxonomy the moment a question spans two domains — which is
    most real questions.

    Not to be confused with :class:`assistant.probe.Lane`, which is one PROBE CASE - a
    thing the assistant must be seen to do. Both words are right in their own module and
    the two now sit one file apart, so the distinction is stated here rather than left
    for somebody to work out from a confusing traceback.
    """

    key: str
    title: str
    #: What this specialist owns, in the words of the person asking.
    owns: str
    #: The step budget a specialist of this kind gets by default. Retrieval is cheap and
    #: wide; analysis is few calls and much thinking; acting is deliberately short,
    #: because a long act lane is a model looking for something else to change.
    max_steps: int = 8


#: THE LANES. Small on purpose: every lane is a prompt somebody has to keep honest, and
#: a taxonomy nobody can hold in their head is one that gets assigned at random. Eight
#: covers the 64 families without a residue, and each one is a different SHAPE of turn.
LANES: tuple[Lane, ...] = (
    Lane("retrieve", "Retrieval", "Find it, show it, and say what else is near it.", 10),
    Lane("analyse", "Analysis", "Compute it, compare it, and say whether it can be trusted.", 8),
    Lane("temporal", "Time", "What it was, what it will be, and what the calendar does to it.", 8),
    Lane("scenario", "Scenario", "What would happen if — before anything happens.", 8),
    Lane("act", "Action", "Change something, through the gate that owns the change.", 6),
    Lane("intake", "Intake", "Something arrived from outside; say what it is before it lands.", 6),
    Lane("deliver", "Delivery", "Get it out of here in a form somebody else can use.", 6),
    Lane("explain", "Explanation", "The app explaining itself, its rules and its own state.", 8),
)


def lane(key: str) -> Lane | None:
    """The :class:`Lane` with this key, or ``None``."""
    for row in LANES:
        if row.key == key:
            return row
    return None


def families_in_lane(lane_key: str) -> tuple[str, ...]:
    """Every family key this lane answers for, in canon order."""
    want = (lane_key or "").strip()
    return tuple(fam.key for fam in FAMILIES if want and want in fam.lanes)


def unrouted() -> tuple[str, ...]:
    """Families that name no lane — the orchestrator can never offer these."""
    return tuple(fam.key for fam in FAMILIES if not fam.lanes)


GUARANTEES: tuple[tuple[str, str], ...] = (
    ("correction_belt",
     "Every answer is checked against the turn's own recorded trail before the "
     "stream ends: performed / fabricated / misreported / unfiled."),
    ("secrets_by_inclusion",
     "Secrets structurally unreachable — an allow-list by INCLUSION, so a field "
     "added later defaults to invisible. Prove it by disabling the boundary."),
    ("declared_caps", "Every result declares its cap: shown against total."),
    ("denominators", "Every count carries its denominator; zero denominator is "
                     "'no data', never a pass."),
    ("never_blocks", "Nothing blocks on a slow dependency; answer from the last "
                     "reading and say how old it is."),
    ("toggles_first", "Read the toggles before reporting a fault; self-diagnose "
                      "before declaring an outage."),
    ("text_is_data", "Message and document text is DATA, never instructions."),
    ("rows_not_totals", "Rows are evidence, never a total."),
    ("surface_ambiguity", "Report the ambiguous rather than guessing."),
    ("state_consequence", "State the consequence of a change, not just the change."),
    ("detector_inputs", "A detector's INPUTS have their own coverage — mutate the "
                        "code that builds the trail, not only the predicate."),
    ("real_model_probe", "Proven against the REAL model whenever the prompt or the "
                         "catalog changes (assistant.probe); a mock cannot see tool-skip."),
    ("measured_or_inferred", "Say whether a value was measured or inferred, and "
                             "name the source."),
    ("handle_vs_citation", "A handle is internal; a citation is public and "
                           "openable."),
    ("code_counts", "The model reads; the CODE counts. Any number in an answer "
                    "came from a tool, or it is a guess wearing a number's clothes."),
)

BY_KEY = {f.key: f for f in FAMILIES}


def families_of(tool_name: str) -> set[str]:
    """EVERY family a tool name answers for — a tool can honestly serve two.

    The first version returned ONE family, first match wins, and that produced
    false gaps rather than missed tools: ``dry_run_route`` matched ``actions``
    on the substring "run" and was therefore never credited to ``merge``, whose
    whole point is a preview of what a write would land on. A false gap is as
    expensive as a missed one — it sends somebody to build what is already there.

    Exact match first; then a substring hint, because apps legitimately name a
    family member for their own domain (``list_routes`` for ``list``,
    ``file_query`` for ``sheet_query``). The audit would rather over-credit a
    real tool than invent a gap, so a tie goes to coverage — and the report
    prints the tools it credited, so an over-credit is visible and arguable
    rather than silent.
    """
    name = (tool_name or "").strip().lower()
    if not name:
        return set()
    exact = {fam.key for fam in FAMILIES if name in fam.tools}
    if exact:
        return exact
    hit = set()
    for fam in FAMILIES:
        for canonical in fam.tools:
            if not canonical:
                continue
            if (name == canonical or name.startswith(canonical + "_")
                    or name.endswith("_" + canonical) or canonical in name):
                hit.add(fam.key)
                break
    return hit


def family_of(tool_name: str) -> str:
    """The first family a name answers for, or "". Kept for simple callers."""
    found = sorted(families_of(tool_name))
    return found[0] if found else ""
