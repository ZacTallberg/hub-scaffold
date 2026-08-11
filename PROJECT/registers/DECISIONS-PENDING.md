# DECISIONS PENDING — the operator queue

> canonical · owner: leader curates, operator resolves · update: on raise and on resolution · append-only rows

The routing target for the ONLY things agents may wait on (DOCTRINE §1.2): irreversible acts,
privileged/undefined-secret operations, and true operator-only choices. Raising a `DP-` entry never
stalls other work — route around it. Every entry carries a recommendation and an explicit
default-if-unanswered so the queue can drain without a meeting. Resolution becomes an ADR.

| ID | Raised | Question | Options + recommendation | Default if unanswered (and when it triggers) | Resolved → |
|---|---|---|---|---|---|
