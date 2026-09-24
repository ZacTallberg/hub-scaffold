# Pattern — doctrine publishes itself, from the tip of main, and says so when it did not

The standing doctrine agents receive usually has two homes: a file in a repository (what people
edit and review) and a published copy the agents actually READ (a hub entity, an installed
instruction file, a skill store). If nothing connects them, they stay in sync only while somebody
remembers to publish by hand — and the repository looks authoritative while the running system
disagrees. This pattern is the pipeline that connects them. It is written for GitLab CI because the
traps below were measured there; every one has an equivalent on other CI systems.

This scaffold ships no pipeline (a pipeline encodes one environment's runners and paths). Adopt the
laws; write the mechanism for your CI.

## The laws

1. **Every push to the protected default branch publishes the WHOLE tree.** Do not gate the publish
   job on `changes:`. A push pipeline evaluates `changes` against its own diff, and CI auto-cancels a
   pending pipeline when the next push lands — so a launcher change followed quickly by a doc-only
   push survives only as the doc-only pipeline, which publishes nothing the first push carried,
   with every pipeline green. Every publish step must therefore be **idempotent** (compare with the
   published body; write nothing and print `UNCHANGED` when equal), so an unrelated push costs a
   no-op and version numbers count real changes rather than pipelines.
2. **Publish only from the tip.** A pipeline that outlived a newer push must not republish older
   doctrine over newer. First step: fetch the branch; if its tip is not this pipeline's commit,
   print `PUBLISH_SUPERSEDED this=<sha> tip=<sha>` and exit 0 — the newest pipeline publishes
   everything. If the tip cannot be read, publish this commit and say so.
3. **Gate on the protected flag, not the branch name.** Use `CI_COMMIT_REF_PROTECTED == "true"`
   (or your CI's equivalent). On an unprotected branch every deploy-shaped job is ineligible and the
   pipeline still goes green.
4. **Assert what reached the published store, from a fresh process.** After publishing, re-read
   the published copy through a NEW process (a dry-run of the same publisher that must print
   `UNCHANGED`) and fail the job if it does not match. A probe over an authenticated HTTP surface
   that cannot authenticate turns a successful publish red; read the store the way the publisher
   does, under the same trust boundary.
5. **A publish that never ran is loud, under its own name.** When an earlier stage fails, CI marks
   the publish job SKIPPED and the pipeline reads merely "red" — while the push's doctrine,
   launcher or skills sit off every machine. Add a job in the publish stage with `when: on_failure`
   that stands down when superseded (law 2) and otherwise prints `PUBLISH_DID_NOT_RUN sha=<sha>`
   and FAILS, so whatever turns CI failures into board rows says "the agents did not get this
   push", distinct from "a step is red". Publishing anyway is not the answer.
6. **Wait for a cross-project target to be whole.** If the publish step imports another project's
   deployed checkout (to write into its store), that checkout may be mid-rewrite by its own deploy.
   CI resource groups are scoped to ONE project, so a group of the same name in two projects never
   serializes them. Before publishing, poll until: no git operation holds the checkout's
   `.git/index.lock`, the application's own startup check passes, and `HEAD` is the same sha before
   and after that check. Anything short of ready after the last attempt fails the job with the last
   check's output — never publish into a half-written tree, never report a skipped publish as
   success.
7. **Size a job's wall from measurement.** A job timeout covers source checkout and interpreter
   start as well as the work; on a busy runner those can eat most of a small budget, and steps that
   spawn many processes (git) run many times slower there than on a workstation. A job that dies at
   its wall with every step it reached passing has a wrong wall, not a broken step — raise it to the
   measured worst case plus room, and cut process spawns rather than coverage.
8. **No new credential for the publisher.** If publishing over HTTP would need a token that can
   also command every agent, run the publish on the host against the deployed store instead,
   under the same trust boundary as that host's own deploy.

## Shape (GitLab CI, abbreviated)

```yaml
publish_doctrine:
  stage: publish
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_COMMIT_REF_PROTECTED == "true"'
    - when: never
  script:
    - ./ci/stand-down-unless-tip.sh          # law 2: PUBLISH_SUPERSEDED / PUBLISH_FROM_TIP
    - ./ci/wait-target-whole.sh              # law 6: HUB_CHECKOUT_READY head=<sha>
    - ./publish doctrine/ --idempotent       # law 1: PUBLISHED v<N> | UNCHANGED
    - ./publish doctrine/ --dry-run | grep '^UNCHANGED '   # law 4, fresh process

publish_doctrine_did_not_run:
  stage: publish
  rules:
    - if: '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_COMMIT_REF_PROTECTED == "true"'
      when: on_failure
    - when: never
  script:
    - ./ci/stand-down-unless-tip.sh
    - echo "PUBLISH_DID_NOT_RUN sha=$CI_COMMIT_SHA"; exit 1     # law 5
```

This scaffold's proof policy applies to the pipeline too: it publishes and re-reads what it
published; it does not grow a standing test battery in front of the publish (`docs/TESTING.md`).
If an adopter keeps any earlier stage, law 5 is what keeps its failure from silently withholding
the publish.
