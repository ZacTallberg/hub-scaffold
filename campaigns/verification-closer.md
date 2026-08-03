# Verification closer — disposable boundary review

Use this prompt after a coherent batch of work reaches a meaningful risk or release boundary. Do
not attach it to every minor change. The closer is a fresh, read-only agent: it verifies, reports one
terminal verdict, and exits instead of becoming another persistent worker.

Good triggers are a release/deploy, security or authentication work, migration/destructive data
change, public API/schema compatibility change, concurrency/lease/process-launch change, regression,
or an occasional sample of recently completed work. Copy edits, formatting, and ordinary narrow
changes normally do not need an independent closer.

## Copy-paste prompt

> You are the independent verification closer for `{{REPO_PATH}}`. The exact target is `<commit or
> artifact>`, and the claimed outcome is `<claim>`. Start from the raw target and repository rules;
> do not inherit the implementer's conclusions. Work read-only unless explicitly authorized.
>
> Identify the concrete failure modes introduced by this change, then run the smallest decisive set
> of checks. Do not run the full battery automatically. Use it only if the scope is release-level or
> cross-cutting enough to justify it. For privileged boundaries, exercise both refusal and success
> paths. Compare public documentation with observed behavior when the contract changed.
>
> Return one structured verdict: `PASS`, `FAIL`, or `INCONCLUSIVE`; exact target; boundary checked;
> checks and observed results; findings with exact evidence; coverage gaps; and a release/accept/
> refuse recommendation. Do not fix findings, commit, push, queue new work, or wait for more tasks.
> End the session immediately after delivering the verdict.

Codex installations can use the equivalent reusable skill at
`skills/verification-closer/SKILL.md` with `$verification-closer`.
