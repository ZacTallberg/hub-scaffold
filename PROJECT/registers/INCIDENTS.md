# INCIDENTS — defect/incident instance ledger

> canonical · owner: whoever detects (leader confirms route) · update: at detection, again at resolution · append-only

Every observed real failure gets a row at detection time — including process failures (a false
green, a deploy collision, a fabricated artifact) and near-misses — plus a fresh repair task. Use
a `FAILURE-MODES.md` class when it improves routing; a novel incident may be repaired first and
classified during the same task. Close the incident when the failed real operation succeeds. Do
not create a permanent test, fixture, or workflow as a closure condition. If a critical boundary
needed a one-shot temporary probe, retain only its receipt and delete the probe before commit.

| ID | Date | Class (FM-, if useful) | What happened (one line) | Detected by | Repair task / route | Successful retry or critical receipt |
|---|---|---|---|---|---|---|
