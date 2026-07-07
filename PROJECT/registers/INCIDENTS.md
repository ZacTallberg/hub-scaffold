# INCIDENTS — defect/incident instance ledger

> canonical · owner: whoever detects (leader confirms class) · update: at detection, again at resolution · append-only

Every concrete defect instance gets a row at detection time — including process failures (a false
green, a deploy collision, a fabricated artifact) and near-misses. The class column MUST resolve to
a `FAILURE-MODES.md` row (create it first). An incident is closed only when its class detector
exists and has fired in test.

| ID | Date | Class (FM-) | What happened (one line) | Detected by | Resolution | Detector born / probe banked |
|---|---|---|---|---|---|---|
