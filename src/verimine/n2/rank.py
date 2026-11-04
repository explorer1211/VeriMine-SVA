"""N2 step 8: PageRank on the VDG (optional feature for N4; not a default ranking axis for N9).

Rank flows from a signal to the signals it reads, so a signal scores high when many
(important) signals depend on it. Edges from reset signals are left out: resets gate almost
every register and would dominate the ranking without saying anything about behavior.
Clocks never appear as edges (they are events, not reads).

Plain power iteration in sorted node order, scores rounded, so the result is deterministic.
"""
from __future__ import annotations

DAMPING = 0.85
MAX_ITER = 100
TOL = 1e-12


def pagerank(nodes: list[str], edges) -> dict[str, float]:
    if not nodes:
        return {}
    idx = {n: i for i, n in enumerate(sorted(nodes))}
    n = len(idx)
    out: list[set[int]] = [set() for _ in range(n)]
    for e in edges:
        if e.reset or e.src == e.dst:
            continue
        # reader -> read: rank moves from e.dst to e.src
        out[idx[e.dst]].add(idx[e.src])
    links = [sorted(x) for x in out]
    rank = [1.0 / n] * n
    for _ in range(MAX_ITER):
        dangling = sum(rank[i] for i in range(n) if not links[i])
        new = [(1 - DAMPING) / n + DAMPING * dangling / n] * n
        for i in range(n):
            if links[i]:
                share = DAMPING * rank[i] / len(links[i])
                for j in links[i]:
                    new[j] += share
        delta = sum(abs(a - b) for a, b in zip(new, rank))
        rank = new
        if delta < TOL:
            break
    names = sorted(idx)
    return {names[i]: round(rank[i], 10) for i in range(n)}
