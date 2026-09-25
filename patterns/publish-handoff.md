# Pattern — the publish hand-off: a worker that cannot push still ships

An unattended run (or any worker) that makes a change, proves it, and then cannot push has
answered the question without delivering it. The usual causes are deliberate, not bugs: a
per-machine key the forge was never told about, a credential manager that cannot prompt with
nobody at the keyboard, a network path that reaches the hub but not the forge. Left alone, the
run writes a patch file on its own disk -- work nobody else can reach -- and a person redoes it by
hand.

The hand-off moves the PUSH to a machine that can push, using rights that machine already has.
It adds no credential, no forge admin token, and nothing to anyone's forge account. It ships in
the hub (`hub_core/handoff.py`, routes in `adapters/django/hub/handoff_api.py`, verbs in
`hub_core/client.py`, MCP tools `submit_handoff` / `handoff_queue` / `claim_handoff` /
`fetch_handoff_bundle` / `report_handoff`), and does nothing until the operator sets
`HUB_HANDOFF_GIT_HOSTS`.

```
python -m hub_core.client handoff proj:task:0042 [--repo DIR] [--branch main]   # the author
python -m hub_core.client publish-handoff --machine build-01                    # a publisher
python -m hub_core.client handoffs [--status all]                               # the queue
```

## The lane

1. **The author** bundles `origin/<branch>..HEAD` and uploads it with the task id. It fetches
   first; when the fetch is refused too (the same broken auth usually refuses both) it says what
   the fetch was told -- the named refusal line, never a bare "fetch failed" -- and uses the
   last-fetched branch head as the base, because the publisher rebases onto the real head
   anyway. An empty range or a HEAD that is not a fast-forward of the base is refused before
   anything is uploaded. A credential in the origin URL is stripped before it leaves the machine.
2. **A publisher** (any machine with push rights and the `handoff:publish` scope) takes a short,
   FENCED lease on the oldest open hand-off: a random token (the hub stores only its hash) and a
   fence that increases with every claim. Only the newest claim may download the bundle or report.
3. It clones the branch into a scratch directory, fetches the bundle, checks the bundle carries
   the recorded head, rebases onto the current branch head and pushes -- **never forced**. A push
   that raced another push is fetched, rebased and tried once more.
4. It reports one of three outcomes:
   * `published` with the full pushed sha. The hub asks its commit resolver whether a server has
     that sha; a provable "no" is refused (no false green), an unanswerable one is recorded
     `verified: null`.
   * `failed` with a reason (a rebase conflict names the conflicting paths). Nothing was pushed.
   * `released` -- "this machine cannot push either" (its clone or push was refused). The
     hand-off goes back to the queue WITHOUT spending one of its claims, so a publisher that can
     push takes it.
5. **The board is told.** A terminal outcome writes a checkpoint onto the task: for a publish, a
   `pushed` row carrying the sha as a field -- exactly what the verified-deploy close reads, so
   the task closes itself when that commit goes live. The author's MACHINE gets a message (a run
   that handed off has usually ended; a message pinned to a dead console never lands).

## Rules the lane holds

* **The host allowlist is the security boundary.** A publisher pushes with its own rights, so a
  hand-off that could name any host would turn every publisher into a push relay for whoever
  uploads. `HUB_HANDOFF_GIT_HOSTS` empty means the lane refuses everything (fail closed). The
  project path in the request must match the path the origin URL names.
* **Sidecar storage, never the ledger.** `<hub dir>/handoffs/events.jsonl` is append-only (the
  state is its fold) and each bundle is a file beside it; the bundle lands on disk before the
  event that names it. A bundle is source code and up to 20 MB -- the hash-chained ledger is the
  wrong home for either.
* **A lapse is bounded.** A hand-off whose lease lapses three times with no result is closed
  `failed` with that reason, rather than offered forever.
* **Idempotent upload.** The client keys an upload on (task, project, base, head); a resend
  after a lost response replays the first record, and the same head already waiting answers
  that record instead of queueing the commits twice. A hand-off that FAILED may be sent again.
* **git never waits on a person.** Both verbs run git with `GIT_TERMINAL_PROMPT=0`,
  `GCM_INTERACTIVE=never` and ssh in batch mode, and decode git's output with replacement so a
  server banner in another encoding cannot swallow the line that names the refusal.

## What the adopter wires

Who publishes (a build machine, the operator's workstation, a scheduled `publish-handoff`
loop), which credential carries `handoff:publish`, and the forge-side commit resolver
(`HUB_COMMIT_RESOLVER`) that lets the hub verify a pushed sha. None of it runs until you set the
hosts and start a publisher.
