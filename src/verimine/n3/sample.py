"""N3 steps 3-5: map waveform names to the structural index, sample once per clock cycle, trim
the reset period, X states and the initialization phase, and cut the trace into test cases.

Sampling uses sampled-value semantics (as SVA does): the row for a clock edge holds every
signal's value just before that edge. A formal trace (smtbmc) shows step k at the edge that
starts it, so its last step has no later edge; for those `final_sample` adds one more row with
the values at the end of the trace.

A test case is a reset-delimited stretch of one waveform: a regression that resets between
tests yields one case per test, a single simulation run or one formal trace yields one case.
Cases are never cut by time (step 5 splits whole cases).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from verimine.hashing import tagged_hash
from verimine.n3.wave import Wave


@dataclass
class Design:
    """What sampling needs to know about the design (built from N1/N2 in pipeline.py)."""
    top: str                                  # N1 top path, e.g. "fifo_top"
    top_ports: list[str]
    children: list[str]                       # instance names directly under the top
    nodes: dict[str, tuple[str, str, int]]    # node id -> (inst path, name, width)
    clock: str                                # top-level clock port
    edge: str                                 # posedge | negedge
    resets: dict[str, str]                    # node id -> active level ("low" | "high")
    inputs: list[str]                         # node ids of top inputs that are data (not clk/rst)
    init_regs: list[str]                      # registers with a reset value: X after reset = init phase


@dataclass
class Case:
    id: str
    source: str               # file or run name
    kind: str                 # waveform | testbench | random | idle | toggle | formal
    legality: str             # user | constrained | unknown
    cycles: int
    signals: dict[str, list[int | None]]
    hash: str = ""
    set: str = ""             # mine | val
    detail: dict = field(default_factory=dict)

    def meta(self) -> dict:
        return {"id": self.id, "source": self.source, "kind": self.kind, "legality": self.legality,
                "cycles": self.cycles, "hash": self.hash, "set": self.set, "detail": self.detail}


def find_scope(w: Wave, d: Design) -> tuple[str | None, int]:
    """The waveform scope that holds the design top: the one whose variables cover the most top
    ports and child instances."""
    scopes: dict[str, set[str]] = {}
    for n in w.vars:
        parts = n.split(".")
        for i in range(1, len(parts)):
            scopes.setdefault(".".join(parts[:i]), set()).add(parts[i])
    best, score = None, 0
    for s, kids in sorted(scopes.items(), key=lambda kv: (kv[0].count("."), kv[0])):
        sc = sum(1 for p in d.top_ports if p in kids) + sum(1 for c in d.children if c in kids)
        if sc > score:
            best, score = s, sc
    return best, score


def wave_names(scope: str, d: Design) -> dict[str, str]:
    """node id -> waveform variable name."""
    out = {}
    for node, (inst, name, _w) in d.nodes.items():
        rel = inst[len(d.top):].lstrip(".")
        out[node] = ".".join(x for x in (scope, rel, name) if x)
    return out


def sample(w: Wave, scope: str, d: Design, final_sample: bool = False) -> tuple[list[int], dict[str, list]]:
    names = wave_names(scope, d)
    clk = w.changes(f"{scope}.{d.clock}")
    want = 1 if d.edge == "posedge" else 0
    edges, prev = [], None
    for t, v in clk:
        if v == want and prev != want:
            edges.append(t)
        prev = v
    present = {n: v for n, v in names.items() if v in w.vars}
    series = {n: w.changes(v) for n, v in present.items()}
    end = max([w.end_time] + [s[-1][0] for s in series.values() if s])
    times = list(edges)
    if final_sample and (not edges or end >= edges[-1]):
        times.append(end + 1)  # "just before" a virtual edge after the end: the final values
    out: dict[str, list] = {}
    for node, ch in series.items():
        vals, i, cur = [], 0, None
        for t in times:
            while i < len(ch) and ch[i][0] < t:
                cur = ch[i][1]
                i += 1
            vals.append(cur)
        out[node] = vals
    return times, out


def cut(times: list[int], sig: dict[str, list], d: Design, min_cycles: int = 2) -> tuple[list[tuple[int, int]], dict]:
    """Reset-delimited segments [start, end) of kept rows, plus counts of what was dropped."""
    n = len(times)
    stats = {"rows": n, "reset": 0, "x": 0, "init": 0, "short": 0, "before_reset": 0}
    resets = {r: lv for r, lv in d.resets.items() if r in sig}
    inputs = [x for x in d.inputs if x in sig]
    init = [x for x in d.init_regs if x in sig]
    ever_reset = any(sig[r][i] == (0 if lv == "low" else 1) for r, lv in resets.items() for i in range(n))

    def in_reset(i):
        for r, lv in resets.items():
            v = sig[r][i]
            if v is None or v == (0 if lv == "low" else 1):
                return True
        return False

    segs, start, seen_reset, leading = [], None, not ever_reset, True

    def close(i):
        nonlocal start
        if start is not None:
            if i - start >= min_cycles:
                segs.append((start, i))
            else:
                stats["short"] += i - start
        start = None

    for i in range(n):
        if in_reset(i):  # reset active, or the reset itself unknown
            stats["reset"] += 1
            seen_reset = True
            close(i)
            leading = True
            continue
        if not seen_reset:
            stats["before_reset"] += 1
            continue
        if any(sig[x][i] is None for x in inputs):
            stats["x"] += 1
            close(i)
            continue
        if leading and any(sig[x][i] is None for x in init):
            stats["init"] += 1
            continue
        leading = False
        if start is None:
            start = i
    close(n)
    return segs, stats


def make_cases(times, sig, d: Design, *, source: str, kind: str, legality: str, prefix: str,
               min_cycles: int = 2, detail: dict | None = None) -> tuple[list[Case], dict]:
    segs, stats = cut(times, sig, d, min_cycles)
    cases = []
    for k, (a, b) in enumerate(segs):
        s = {node: v[a:b] for node, v in sorted(sig.items())}
        c = Case(f"{prefix}#{k}" if len(segs) > 1 else prefix, source, kind, legality, b - a, s,
                 detail=dict(detail or {}) | {"rows": [a, b]})
        c.hash = tagged_hash("n3_case", s)
        cases.append(c)
    return cases, stats
