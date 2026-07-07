# INFRA INVENTORY — deploy & ops runbook

> template → canonical once filled · owner: whoever touches infra · update: same session as any infra change; re-verify the "verified" stamp when read cold

**Verified against real config files on: <date> by <who>** — a runbook that hasn't been re-verified
against source is a rumor. Secrets stay in your organization's credential store BY KEY NAME
(e.g. a git-ignored `secrets.local` file plus a deploy-access atlas doc); this file holds structure, never values.

## Process & boot
How the app runs (server, workers, migrate-on-boot), and the exact boot order.

## Deploy paths
- **Code:** command, owner (campaigns: seat per `pm/PROTOCOL.md` §7), gates it must pass, expected
  duration + the patience notes (what a "hung" deploy actually is).
- **Data:** command, owner, pre-ship gates, the stop/swap/start window behavior.
- **Sequencing law:** code-first when a change spans both (new code tolerates old data; old code
  on new data produces user-visible lies).

## Environment variables
| Var | Purpose | Notes (key name in creds, never the value) |
|---|---|---|

## Storage & mounts
Persistent paths, ownership/uids, backup story (and its verification date).

## Front door & TLS
DNS → edge → tunnel/origin chain; canary URLs; cache rules; the UA/CF gotchas that false-green canaries.

## Recovery
The failure modes this infra has actually had (link `../registers/INCIDENTS.md`) and the proven
recovery sequence for each.
