"""Graph helpers used by the planner: SCCs, DAG checks, clustering."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable


def strongly_connected(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Tarjan's algorithm (iterative). Returns SCCs with more than one node or a self-loop."""
    adj: dict[str, list[str]] = defaultdict(list)
    self_loops = set()
    for a, b in edges:
        adj[a].append(b)
        if a == b:
            self_loops.add(a)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(adj[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(adj[w])))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1 or v in self_loops:
                    out.append(sorted(comp))
    return out


def is_dag(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> bool:
    return not strongly_connected(list(nodes), list(edges))


def label_propagation(
    nodes: list[str], edges: list[tuple[str, str]], max_size: int, rounds: int = 20
) -> list[list[str]]:
    """Deterministic community detection, then greedy split of oversized communities."""
    nbrs: dict[str, list[str]] = defaultdict(list)
    for a, b in edges:
        if a != b:
            nbrs[a].append(b)
            nbrs[b].append(a)
    label = {n: n for n in nodes}
    order = sorted(nodes)
    for _ in range(rounds):
        changed = False
        for n in order:
            if not nbrs[n]:
                continue
            counts: dict[str, int] = defaultdict(int)
            for m in nbrs[n]:
                counts[label[m]] += 1
            best = max(sorted(counts), key=lambda k: counts[k])
            if counts[best] > counts.get(label[n], 0) and best != label[n]:
                label[n] = best
                changed = True
        if not changed:
            break
    groups: dict[str, list[str]] = defaultdict(list)
    for n in order:
        groups[label[n]].append(n)
    out: list[list[str]] = []
    for members in sorted(groups.values(), key=lambda g: (-len(g), g[0])):
        for i in range(0, len(members), max_size):
            out.append(members[i : i + max_size])
    # Merge tiny leftovers into the smallest group that still has room.
    merged: list[list[str]] = []
    for g in out:
        if len(g) <= 2 and merged:
            target = min(merged, key=len)
            if len(target) + len(g) <= max_size:
                target.extend(g)
                continue
        merged.append(g)
    return merged
