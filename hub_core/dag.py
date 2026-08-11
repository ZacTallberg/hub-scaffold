"""The dependency DAG as a shape you can measure and export.

The board knows what blocks what, but only ever answered "is this one ready?". Two numbers fall
straight out of the same graph and answer the questions a fleet actually has:

  CRITICAL PATH LENGTH — the longest chain of unfinished dependencies. No amount of concurrency
  finishes the board faster than this, because each link waits on the one before it. It is the
  floor on makespan, in tasks.

  MAX FRONTIER WIDTH — the widest layer of that same chain decomposition: how many tasks are
  workable at once at the fleet's busiest moment. Workers beyond this number have nothing to pull,
  which is what makes "launch 8 workers" a guess without it.

Together they give an honest ETA: with at least `max_frontier_width` workers the board cannot
finish in fewer than `critical_path_length` sequential steps, and that IS the min makespan.

Stack-neutral and pure: a fold over the same entities every other consumer reads, no I/O, no
project binding. GraphML because it is the interchange format every graph tool already reads
(yEd, Gephi, networkx) — an export nothing can open is a file, not an export.
"""
from xml.sax.saxutils import escape, quoteattr

TERMINAL = ("done", "dropped")


def _open_tasks(state):
    """{id: task} for tasks still to be done — the DAG's nodes. A terminal task constrains
    nothing downstream, so it is not part of what is left to schedule."""
    return {t["id"]: t for t in state.get("by_type", {}).get("task", [])
            if (t.get("status") or "").lower() not in TERMINAL}


def _edges(state, nodes):
    """(dep, task) pairs where BOTH ends are open tasks. A dep on a done task is satisfied and a
    dep on an entity that does not exist is the dangling-dep rail's business, not the DAG's —
    including either would invent structure the schedule does not actually have."""
    out = []
    for tid, task in nodes.items():
        for dep in (task.get("deps") or []):
            if dep in nodes:
                out.append((dep, tid))
    return out


def _depth(nodes, edges):
    """{id: longest chain of open deps ending at this task}, 1-based. Cycles cannot extend a
    path: a node still waiting on unresolved predecessors when the queue empties keeps the depth
    it reached, so a cyclic board degrades to a number instead of an infinite loop (dep_cycle is
    the guard that reports the cycle itself)."""
    successors, indegree = {n: [] for n in nodes}, {n: 0 for n in nodes}
    for dep, tid in edges:
        successors[dep].append(tid)
        indegree[tid] += 1
    depth = {n: 1 for n in nodes}
    queue = sorted(n for n in nodes if indegree[n] == 0)
    seen = 0
    while queue:
        node = queue.pop(0)
        seen += 1
        for nxt in successors[node]:
            depth[nxt] = max(depth[nxt], depth[node] + 1)
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
        queue.sort()
    return depth, (seen == len(nodes))


def critical_path(state, depth=None, nodes=None, edges=None):
    """The actual longest chain of open dependencies, oldest-first — not just its length.

    A number tells the operator the board needs eleven sequential steps; the CHAIN tells them
    which eleven, so the one queue worth unblocking first is visible instead of inferred. Walks
    back from the deepest node through whichever predecessor sits exactly one level below it,
    which is what made that node's depth.

    Ties break on id so the same board always renders the same chain — a path that reshuffles
    between two identical snapshots reads as progress that did not happen."""
    if nodes is None:
        nodes = _open_tasks(state)
    if not nodes:
        return []
    if edges is None:
        edges = _edges(state, nodes)
    if depth is None:
        depth, _ = _depth(nodes, edges)
    preds = {n: [] for n in nodes}
    for dep, tid in edges:
        preds[tid].append(dep)
    # Deepest node wins; ties break on id for a stable render.
    cur = min((n for n in nodes if depth[n] == max(depth.values())))
    chain = [cur]
    while True:
        step = sorted(p for p in preds[cur] if depth[p] == depth[cur] - 1)
        if not step:
            break
        cur = step[0]
        chain.append(cur)
    chain.reverse()
    return [{"id": t, "title": nodes[t].get("title"), "status": nodes[t].get("status"),
             "priority": nodes[t].get("priority"), "depth": depth[t]} for t in chain]


def metrics(state):
    """{critical_path_length, max_frontier_width, nodes, edges, layers, layer_ids, path,
    acyclic, min_makespan}.

    min_makespan is critical_path_length: the schedule cannot beat its longest chain however many
    workers pull, and it REACHES it once the fleet is at least max_frontier_width wide.

    `layer_ids` and `path` carry the DAG's SHAPE, not only its size, so a client can draw the
    schedule it is describing. Without them the cockpit could print "critical path 11" and had
    no way to show which eleven — a metric the operator has to take on faith."""
    nodes = _open_tasks(state)
    edges = _edges(state, nodes)
    if not nodes:
        return {"critical_path_length": 0, "max_frontier_width": 0, "nodes": 0, "edges": 0,
                "layers": [], "layer_ids": [], "path": [], "acyclic": True, "min_makespan": 0}
    depth, acyclic = _depth(nodes, edges)
    layers = {}
    for tid, d in depth.items():
        layers.setdefault(d, []).append(tid)
    order = sorted(layers)
    width = [len(layers[d]) for d in order]
    return {
        "critical_path_length": max(depth.values()),
        "max_frontier_width": max(width),
        "nodes": len(nodes), "edges": len(edges),
        "layers": width,
        # Bounded per layer: the cockpit draws a frontier, and a 900-task layer would ship the
        # whole board through the live payload on every snapshot. The width above stays exact,
        # so a truncated layer still renders its true count.
        "layer_ids": [sorted(layers[d])[:24] for d in order],
        "path": critical_path(state, depth=depth, nodes=nodes, edges=edges),
        "acyclic": acyclic,
        "min_makespan": max(depth.values()),
    }


def eta_tasks(state, workers):
    """The min-makespan ETA in sequential task-steps for a fleet of `workers`. At or above the
    max frontier width it is exactly the critical path; below it, the work still has to fit
    through the workers available, so the wider layers stretch."""
    m = metrics(state)
    if not m["nodes"]:
        return 0
    workers = max(1, int(workers or 1))
    if workers >= m["max_frontier_width"]:
        return m["critical_path_length"]
    return sum(-(-w // workers) for w in m["layers"])      # ceil-divide each layer


def graphml(state):
    """The open dependency DAG as GraphML — the format graph tools already read. Keys are
    declared before the graph (as the schema requires) so a standard parser accepts it."""
    nodes = _open_tasks(state)
    edges = _edges(state, nodes)
    depth, _acyclic = _depth(nodes, edges) if nodes else ({}, True)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
           '<key id="title" for="node" attr.name="title" attr.type="string"/>',
           '<key id="status" for="node" attr.name="status" attr.type="string"/>',
           '<key id="priority" for="node" attr.name="priority" attr.type="string"/>',
           '<key id="depth" for="node" attr.name="depth" attr.type="int"/>',
           '<graph id="deps" edgedefault="directed">']
    for tid in sorted(nodes):
        task = nodes[tid]
        out.append("<node id=%s>" % quoteattr(tid))
        out.append('<data key="title">%s</data>' % escape(str(task.get("title") or "")))
        out.append('<data key="status">%s</data>' % escape(str(task.get("status") or "")))
        out.append('<data key="priority">%s</data>' % escape(str(task.get("priority") or "")))
        out.append('<data key="depth">%d</data>' % depth.get(tid, 1))
        out.append("</node>")
    for i, (dep, tid) in enumerate(sorted(edges)):
        out.append("<edge id=%s source=%s target=%s/>"
                   % (quoteattr("e%d" % i), quoteattr(dep), quoteattr(tid)))
    out.append("</graph>")
    out.append("</graphml>")
    return "\n".join(out) + "\n"
