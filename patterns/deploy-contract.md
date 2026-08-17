# The Deploy Contract — four laws that make "deployed" a fact, not a claim

Every failure mode this contract exists to kill is a variant of one meta-failure: **FALSE-GREEN**
— a pipeline that reports success without independent evidence. Agents (human or AI) will
truthfully report "deploy succeeded" when the build was stale, the release was silently swallowed
by the platform, the wrong artifact shipped, or someone later overwrote live with something else.
The fix is never "be more careful"; it is out-of-process enforcement. These four laws are that
enforcement, and they are platform-agnostic: they say nothing about *how* you build or ship, only
about what must be true before anyone may say "done".

The companion files implement the contract:

| File | Role |
|---|---|
| `deploy-runbook.md` | How an agent satisfies laws 1–4 by hand, reading real output at each step |
| `standing-canary.md` | Law 4: the out-of-band re-check, executed at natural moments — not a cron |
| `pre-receive-gate.sh` | Adjacent: server-side push gate (secrets can never enter the repo) — code, because it refuses |

For a mounted Hub, add one project-specific post-canary integration: append one immutable validated
`deploy` entity whose `sha` and canary-observed `served_sha` match exactly and whose
`tasks_closed[]` explicitly names the already-done work carried by that release. Also update the
running deployment's `PROJECT/state.json` with `last_deploy_sha` and `live_url`. The running Hub
reads its own normalized `HUB_BUILD_SHA`, platform-provided `SOURCE_VERSION`, or pre-build stamp; the
exact deploy closure therefore proves named tasks live without requiring `.git` in the image. The
generic shell cannot know how to reach a container/host's durable runtime mount, so the adopter must
wire this integration. Until then, Hub build and delivery coherence remain unknown.

---

## Law 1 — Build from a clean detached worktree of HEAD

**Rule.** The deploy script never builds from the working directory. It creates a detached
`git worktree` of `HEAD`, builds inside that, and removes it afterwards. If the working tree is
dirty, it may warn — but the uncommitted changes physically cannot be in the artifact.

**Rationale.** The single most common false-green is *"it works on my machine because my machine
has uncommitted fixes."* When you build from the working directory, the artifact is a snapshot of
an unrecorded state: it cannot be reproduced, bisected, rolled back to, or audited. Building from
a detached worktree of HEAD makes the invariant structural: **what ships is exactly a commit**.
A teammate (or the standing canary) can check out that SHA and get byte-identical inputs.

Corollaries:
- Refuse to run at all outside a git repo — a project deploys its HEAD, so there must be one.
- A dirty tree is a warning, not an error: the operator may legitimately be mid-work. But the
  warning must state plainly that the dirty files will NOT ship.

## Law 2 — Bake the git short SHA into the artifact pre-build; serve it front-door

**Rule.** Before the build step runs, write the short SHA of HEAD into a file inside the worktree
(e.g. `build_sha.txt`) so the build **bakes it into the artifact**. The application serves it on
its public entry page as:

```html
<meta name="build" content="build-<sha>">
```

**Rationale.** Every downstream verification law needs an unforgeable answer to "what code is this
server actually running?" Version endpoints that read git at request time lie when the deployed
checkout drifts; hand-maintained version strings lie always, eventually. A SHA stamped *before*
the build and served *from inside* the artifact is the only value that provably traveled the whole
pipeline. Serving it in a `<meta>` tag on the front door means any curl — no auth, no API, no
platform access — can read it.

Corollaries:
- The stamp is written pre-build, never post-deploy. A post-hoc stamp proves nothing.
- The placeholder page a project serves before its first real build must NOT carry a build meta —
  so an unbuilt page can never pass a canary.

## Law 3 — A deploy is DONE only when an independent front-door probe sees `build-<sha>` live

**Rule.** After shipping, the deploy script curls the real public URL ({{LIVE_URL}} — the same
door users walk through, through the same proxy/CDN/edge) and asserts the body contains
`build-<sha>` for **this** build's SHA. It retries with patience (cold starts, slow release
stages), then **fails closed**: no match within the window = the deploy FAILED, exit non-zero.
This check is not optional. If a script offers an opt-out at all, it must be an explicit flag
whose use leaves a paper trail.

**Rationale.** Ship steps lie constantly and creatively: the platform accepts the artifact but the
release hook errors after the connection drops; a health check goes green against the *old*
container; a CDN serves a stale cached bundle over a fresh backend; the image loads but the swap
never happens. Each of these passes every in-process check. The only observation that subsumes all
of them is: *the front door, fetched from outside, serves the SHA I just built.* Anchoring the
probe on `build-<sha>` (not on some text that both old and new versions render) is what makes a
stale deploy structurally unable to pass.

Corollaries:
- If the ship step's exit status is ambiguous (dropped SSH, platform timeout), do NOT trust it and
  do NOT fail yet — extend the canary window and let the probe decide. The probe is the truth.
- "Done" in any tracker/record must name the SHA the probe saw. A deploy-dependent task without a
  live SHA in its record is not done.

## Law 4 — A standing out-of-band re-checker compares live vs a blessed record and alerts on drift

**Rule.** When Law 3 passes, the deploy writes a **blessed record** — one file per project
containing `<sha> <url>` — into a records directory on a machine that is *not* part of any
deploying agent's process. The record is re-checked out of band (`standing-canary.md`): an agent
re-fetches every blessed URL at natural moments — after each deploy, at session start, before
trusting any "it is live" claim — compares the served `build-<sha>` meta against the record, and
raises a mismatch as a live incident with a receipt. A schedule is deliberately not the mechanism:
a cron canary's own death reads identical to "all green", which is the exact failure this law exists
to catch.

**Rationale.** Laws 1–3 prove the deploy was true *at the moment it finished*. Nothing about that
moment protects the next hour: someone deploys over you without the wrapper, the platform restarts
into an older image, DNS or the proxy gets repointed, a host is restored from a stale snapshot.
The blessing pattern converts "verified once" into "continuously asserted": the blessed record is
the org's written-down intent, and the canary is an independent process that believes only the
live page. Because the deploy script registers the blessing automatically on every verified
deploy, coverage grows with zero extra discipline.

Corollaries:
- The canary must run out-of-band — different process, ideally different machine, from anything
  that deploys. An agent checking its own work is Law 3; this law is about *someone else* checking.
- Alert content should include both the blessed SHA and what was actually served (or
  "unreachable"), so the responder can distinguish drift from outage at a glance.

---

## The shape of a compliant deploy script

```
preconditions   git repo?  warn-if-dirty  SHA := short HEAD          (Law 1)
worktree        git worktree add --detach <tmp> HEAD                 (Law 1)
stamp           write SHA into the worktree pre-build                (Law 2)
critical seam   only when declared: run its transient one-shot probe and retain the receipt
build           $BUILD_CMD inside the worktree
serialize       acquire a real per-target release lease; one front door has one release owner
ship            $SHIP_CMD  (the only org-specific part; unique release-attempt log/identity)
canary          poll $LIVE_URL for "build-$SHA"; fail closed         (Law 3)
bless           write "<sha> <url>" to the blessed-records dir       (Law 4; failure blocks)
record          POST immutable {sha, served_sha:sha, tasks_closed:[done ids], at}; write runtime state
```

Anti-patterns this contract explicitly bans:

- **Self-attested green** — any step that reports success based on its own exit code where an
  external observation is possible.
- **Committed-but-not-deployed counted as done** — code in git is not code in production; only a
  probe-witnessed SHA closes a task.
- **Canary on stable text** — asserting the live page contains the product name proves nothing;
  the old build contains it too. Assert the SHA.
- **Optional-by-default verification** — verification you can forget is verification that will be
  forgotten. Fail closed; make opt-out loud and explicit.
- **Concurrent same-target release tails** — two builds may run in parallel, but two swaps and
  canaries for one front door can make an older artifact overwrite a newer one after both ship
  commands succeed. Hold one real per-target lease from swap through canary and deploy record.
- **Shared per-app or per-tag release logs** — every release attempt gets its own log/verdict
  identity. Reusing one pathname lets another attempt satisfy, truncate, or overwrite its observer,
  even when both attempts carry the same artifact tag.
