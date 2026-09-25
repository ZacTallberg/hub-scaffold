"""manage.py semantic_index — embed what the board knows, beside the ledger (never in it).

Search, related-record lookup and the per-prompt memory index read vectors from a sqlite
sidecar in HUB_DIR. Nothing on a write path embeds (a write never waits on the embedder), so
this command is how vectors come to exist: it derives the work (indexable records minus stored
vectors, keyed by a hash of each record's text), embeds it in batches, retires the vectors of
records that stopped being knowledge, and optionally rebuilds the CSLS hub term.

    manage.py semantic_index                 # embed everything pending, then the hub term
    manage.py semantic_index --limit 256     # at most 256 records this run
    manage.py semantic_index --probe         # ask the embedder what it returns; embed nothing
    manage.py semantic_index --coverage      # print coverage/partiality only

Run it on a schedule (or after bulk imports). With no embedder configured (HUB_EMBED_URL) it
exits non-zero and says so: an index job that quietly does nothing is how a board ends up
answering keyword-only for a month while every surface reports success.
"""
import json
import math
import os
import time

from django.core.management.base import BaseCommand

from hub import hub_app, knowledge_api
from hub_core import semantic


class Command(BaseCommand):
    help = "Embed indexable board records into the semantic sidecar and rebuild the hub term."

    def add_arguments(self, p):
        p.add_argument("--limit", type=int, default=0, help="max records to embed this run (0 = all pending)")
        p.add_argument("--no-hub-terms", action="store_true", help="skip the CSLS hub-term rebuild")
        p.add_argument("--probe", action="store_true",
                       help="embed one query and one document, print dimension and norm, write nothing")
        p.add_argument("--coverage", action="store_true", help="print coverage only")

    def _say(self, obj):
        self.stdout.write(json.dumps(obj, indent=2, sort_keys=True, default=str))

    def handle(self, *a, limit=0, no_hub_terms=False, probe=False, coverage=False, **o):
        if probe:
            return self._probe()
        if coverage:
            return self._run(limit, no_hub_terms, coverage)
        # ONE INDEX PASS PER BOARD AT A TIME. A scheduled run that overlaps a manual one (or the
        # next tick of a slow one) doubles the embedder's load, which slows both, which makes
        # the next overlap likelier. A second pass returns at once instead: the running pass
        # already covers what this one would have read. The lock names its holder's pid, so a
        # crashed indexer's lock is reclaimed by the next run rather than wedging indexing.
        from hub_core.process_lock import LockBusy, ProcessFileLock
        # A one-second budget, not zero: reclaiming a DEAD holder's lock file takes one more
        # create attempt after the unlink, and a zero budget gives up before making it.
        lock = ProcessFileLock(hub_app.HUB_DIR, name="semantic_index.lock", timeout=1.0)
        try:
            lock.__enter__()
        except LockBusy as busy:
            # Only the acquisition is judged here: a runtime lock timing out INSIDE the pass is a
            # real failure and must not be reported as "another pass is running".
            self._say({"skipped": "another semantic_index pass holds the lock", "detail": busy.public})
            return None
        try:
            return self._run(limit, no_hub_terms, coverage)
        finally:
            lock.__exit__(None, None, None)

    def _run(self, limit, no_hub_terms, coverage):
        state = hub_app.current_state()
        # [] when no catalog is configured (nothing is missing), None when configured and
        # unreadable — so coverage never reports a denominator it could not count.
        knowledge_api.catalog(wait=True)
        cat_items, cat_error = knowledge_api._catalog_for_index()
        records = semantic.indexable(state, cat_items or [])
        conn = semantic.connect(hub_app.HUB_DIR)
        try:
            if coverage:
                cov = semantic.coverage(state, conn, records, cat_items, cat_error)
                self._say({"coverage": cov, "partiality": semantic.partiality(dict(cov, semantic=True))})
                return
            if not semantic.configured():
                self.stderr.write("semantic_index: no embedder configured (set HUB_EMBED_URL); "
                                  "search stays keyword-only and says so")
                raise SystemExit(2)
            started, embedded, rounds, last = time.time(), 0, 0, {}
            while True:
                room = (limit - embedded) if limit else 0
                if limit and room <= 0:
                    break
                last = semantic.backfill(records, conn, limit=room)
                rounds += 1
                embedded += last.get("embedded", 0)
                if not last.get("ok") or not last.get("embedded") or not last.get("pending"):
                    break
            result = {"embedded": embedded, "rounds": rounds, "pending": last.get("pending"),
                      "retired": last.get("retired"), "ok": last.get("ok"),
                      "seconds": round(time.time() - started, 1)}
            if last.get("reason"):
                result["reason"] = last["reason"]
            if not no_hub_terms and last.get("ok"):
                result["hub_terms"] = semantic.compute_hub_terms(conn)
            result["coverage"] = semantic.coverage(state, conn, records, cat_items, cat_error)
            self._say(result)
            if not last.get("ok"):
                raise SystemExit(1)
        finally:
            conn.close()

    def _probe(self):
        """What the configured model ACTUALLY returns. Every property the ranking relies on
        (dimension, normalization, that a query and a document land in one space) is an
        assumption until a real call answers it."""
        import urllib.request
        if not semantic.configured():
            self.stderr.write("semantic_index --probe: no embedder configured (set HUB_EMBED_URL)")
            raise SystemExit(2)
        body = {"input": ["how do I retry a failed deploy"]}
        model = os.environ.get("HUB_EMBED_MODEL", "").strip()
        token = os.environ.get("HUB_EMBED_TOKEN", "").strip()
        if model:
            body["model"] = model
        raw_norm = None
        try:
            req = urllib.request.Request(semantic.embed_url() + "/embeddings", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json",
                                                  **({"Authorization": "Bearer " + token} if token else {})},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=semantic.QUERY_TIMEOUT_S) as resp:
                vec = json.loads(resp.read().decode("utf-8"))["data"][0]["embedding"]
            raw_norm = math.sqrt(sum(float(x) * float(x) for x in vec))
        except Exception as exc:                               # noqa: BLE001
            self.stderr.write("probe: raw call failed: %s: %s" % (type(exc).__name__, exc))
            raise SystemExit(1)
        t0 = time.perf_counter()
        q = semantic.embed(["how do I retry a failed deploy"], is_query=True)[0]
        d = semantic.embed(["Retry a failed deploy by re-running the pipeline from its last green stage."],
                           is_query=False)[0]
        u = semantic.embed(["The cafeteria menu changes on Mondays."], is_query=False)[0]
        self._say({"url": semantic.embed_url(), "model": model or "(server default)",
                   "dimension": len(vec), "raw_l2_norm": round(raw_norm, 6),
                   "normalized_by_model": abs(raw_norm - 1.0) < 1e-3,
                   "cos(query, related doc)": round(semantic.cosine(q, d), 4),
                   "cos(query, unrelated doc)": round(semantic.cosine(q, u), 4),
                   "ms_for_three_embeds": round((time.perf_counter() - t0) * 1000, 1)})
