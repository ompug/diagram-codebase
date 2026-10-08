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


def connected_components(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Weakly connected components, largest first, each sorted (deterministic)."""
    nodes = sorted(set(nodes))
    nbrs: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        nbrs[a].add(b)
        nbrs[b].add(a)
    seen: set[str] = set()
    out: list[list[str]] = []
    for root in nodes:
        if root in seen:
            continue
        comp, todo = [], [root]
        seen.add(root)
        while todo:
            cur = todo.pop()
            comp.append(cur)
            for m in sorted(nbrs[cur]):
                if m not in seen:
                    seen.add(m)
                    todo.append(m)
        out.append(sorted(comp))
    return sorted(out, key=lambda c: (-len(c), c[0]))


def pack(groups: list[list[str]], max_size: int) -> list[list[str]]:
    """Greedy first-fit packing of groups into bins of at most `max_size` items.

    Groups larger than `max_size` are cut into consecutive chunks first."""
    pieces: list[list[str]] = []
    for g in groups:
        for i in range(0, len(g), max_size):
            pieces.append(list(g[i : i + max_size]))
    bins: list[list[str]] = []
    for piece in sorted(pieces, key=lambda p: (-len(p), p[0] if p else "")):
        for b in bins:
            if len(b) + len(piece) <= max_size:
                b.extend(piece)
                break
        else:
            bins.append(piece)
    return bins


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)
