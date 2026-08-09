# Pattern — the standing re-check (Law 4 of `deploy-contract.md`)

**The claim "it is deployed" decays.** A deploy verified at ship time can stop being true hours
later — a rollback nobody recorded, a cache serving a corpse, a container silently replaced — and
the deploying agent's process is gone by then. Law 4 therefore requires the blessed record to be
re-checked **out of band**: by something that is not the deploying agent, does not share its
assumptions, and only believes the live page.

This pattern used to ship here as a cron script (`standing-canary.sh`). It was converted to this
runbook deliberately: a scheduled organ is the failure mode, not the feature. A cron canary rots
exactly like a deploy script — it keeps exiting 0 after its `BLESSED_DIR` moves, its alert channel
dies, or its cooldown file wedges, and nobody notices *the watcher* died because nothing watches
it. The re-check survives; the schedule does not.

## The re-check, by hand (an agent executes this at natural moments)

For every blessed record — one line, `<sha> <url>`, written at each VERIFIED deploy:

1. Fetch the live page with a browser-shaped user agent and a bounded timeout:
   `curl -sL -A "Mozilla/5.0" --max-time 45 <url>`
2. Assert it still serves `<meta name="build" content="build-<sha>">` for the **blessed** sha.
3. On match: record `OK <project> <sha>` with a timestamp in the run's receipt.
4. On mismatch or unreachable: this is a live incident, not a log line. Read what IS served
   (`grep -oi 'name="build" content="build-[^"]*"'`), name blessed-vs-served in the alert, and
   raise it where a human will see it. One alert per incident — you are a person reading output,
   which is the whole cooldown mechanism.

**When:** after every deploy (the verify-after step), at session start when picking up a project,
and before trusting any "it is live" claim someone else recorded. The trigger is an agent at a
natural moment with the receipt to show for it — never a timer that can die silently.

**Why a human-visible receipt beats a cron log:** the canary's own health is the thing a schedule
cannot prove. An agent that ran the re-check has a receipt in the ledger; a cron job that stopped
firing has nothing, which reads identical to "all green".

If your org still chooses a scheduled form, you accept the watcher-of-the-watcher problem as your
own; the checks above are the whole procedure either way.
