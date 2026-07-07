# research/ — deep research & review corpus

> canonical · owner: any seat producing research · update: one file per campaign/memo; chronicle updated same session

## The contract
1. **Research precedes build** (`DOCTRINE.md` §4.1). No architectural task starts until its
   dependent research is captured here — findings living only in chat are lost work; mine prior
   chat/session history for findings and file them here before they evaporate.
2. **One file per effort**, named `YYYY-MM-DD-<slug>.md`. Genres this folder holds:
   - **Deep-research dossiers** — multi-agent research passes; keystone findings flagged.
   - **MoE panel reviews** — adversarial multi-expert reviews; keep verdicts AND explicit
     rejections/refutations (respect them later — they are anti-rework armor).
   - **Improvement-surface memos** — periodic step-back "how do we improve the whole system"
     essays with prioritized keystones.
   - **Domain audits** — focused investigations that end in a design recipe.
3. **`RESEARCH-HISTORY.md` is the chronicle** — the front door to WHY the architecture is what it
   is. Every research file gets an entry (date, question, keystones, decisions it fed → ADR ids)
   in the same session it lands.
4. **Findings that gate work become hub `gap` entities**; decisions become ADRs; the research file
   is their evidence link — not a task tracker itself.
