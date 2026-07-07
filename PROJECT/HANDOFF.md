# HANDOFF — living continuity file

> canonical · owner: the principal agent (solo) or LEADER (campaign) · update: at every significant state change and ALWAYS before ending a session

This is the single resume entry point. A cold agent reads this first (`README.md` §0) and must be
able to continue seamlessly from it alone plus the tails of any active channels. Keep it current —
a stale handoff is a defect. Rewrite in place (it is a snapshot, not a ledger); history lives in
the hub and the channels.

## 0. The arrangement
Operating mode (SOLO / campaign per `pm/PROTOCOL.md` §0 with seat roster), who deploys what, and
where the channels are. If a campaign is active: "read `pm/PROTOCOL.md`, then your seat's CHARTER,
DIRECTIVES tail, and STATE."

## 1. Standing doctrine deltas
Nothing here duplicates `DOCTRINE.md` — list only recent laws not yet internalized by all seats,
with their ADR numbers.

## 2. What's live right now (all verified, with evidence)
- Deployed code SHA + how it was verified.
- Deployed data state + gate status (`runs/status.json`).
- Domains/surfaces and their states.

## 3. In-flight
What is being worked RIGHT NOW, by whom, and what to verify when each lands.

## 4. Backlog (priority order)
The next actionable items with enough context to start each. Deep specs stay in their own docs — link.

## 5. Environment quirks & access
Pointers only (atlas + creds by key name), plus the quirks that burn cold agents.

## 6. Hard-won gotchas
The "do not relearn these" list. Promote recurring ones into `DOCTRINE.md` §6 or the global gotcha docs.

## 7. Session narrative (compressed)
A few sentences of how the project got here — enough to reconstruct intent, not a diary.
