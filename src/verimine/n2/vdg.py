"""N2 steps 1-2: the variable dependency graph (VDG).

Nodes are ports, signals and registers, named by full hierarchical path ("top.u_ctrl.count").
An edge u -> v means v reads u. Each edge carries:
  kind   data (u is on the right-hand side) or control (u is in the assignment's control-condition
         stack: an enclosing if or case)
  delay  1 when v is assigned in a clocked block (always_ff, or always with an edge event), else 0
  via    assign (inside a module) or port (through an instance port connection)
  reset  the edge comes from a reset signal (kept in the graph; left out of PageRank)

Port connections join the per-instance graphs: parent signals feed child input ports, child
output ports drive parent signals. Instances that N1 marked BLOCKED are not modeled; their
outputs are simply undriven in the parent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from verimine.n1.assign import cond_refs, tree_refs
from verimine.n1.elaborate import InstanceInfo

CLOCKED_KINDS = {"always_ff"}


@dataclass
class Node:
    id: str
    inst: str
    name: str
    kind: str        # input | output | inout | reg | wire | memory
    width: int
    level: str       # port | internal  (v5: port-level assertions survive refactoring)
    top_port: bool = False
    clock: bool = False
    reset: bool = False


@dataclass(frozen=True, order=True)
class Edge:
    src: str
    dst: str
    kind: str        # data | control
    delay: int       # 0 | 1
    via: str         # assign | port
    reset: bool = False


@dataclass
class Vdg:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    modeled: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    # per instance: local reset name -> active level ("low" | "high")
    resets: dict[str, dict[str, str]] = field(default_factory=dict)
    clocks: dict[str, set[str]] = field(default_factory=dict)
    # node -> canonical text of the logic that drives it (assignments with their condition
    # stacks, or the parent-side expression of a port connection). COI fingerprints hash this.
    logic: dict[str, list[str]] = field(default_factory=dict)

    def preds(self) -> dict[str, list[Edge]]:
        out: dict[str, list[Edge]] = {n: [] for n in self.nodes}
        for e in self.edges:
            out[e.dst].append(e)
        return out


def is_clocked(inst: InstanceInfo, proc: int | None) -> bool:
    if proc is None:
        return False
    p = inst.procs[proc]
    if p.kind in CLOCKED_KINDS:
        return True
    return p.kind == "always" and any(ev.get("edge") for ev in p.sensitivity)


def _clock_reset_sets(instances: list[InstanceInfo], top: str, clocks: list[dict], resets: list[dict]):
    """Configured clocks/resets are top-level port names; follow plain port connections down
    the hierarchy. Async resets seen in sensitivity lists count too (negedge -> active low)."""
    by_parent: dict[str | None, list[InstanceInfo]] = {}
    for i in instances:
        by_parent.setdefault(i.parent, []).append(i)
    clk: dict[str, set[str]] = {i.path: set() for i in instances}
    rst: dict[str, dict[str, str]] = {i.path: {} for i in instances}
    clk[top] = {c["name"] for c in clocks}
    rst[top] = {r["name"]: r["active"] for r in resets}

    def down(inst: InstanceInfo) -> None:
        for child in by_parent.get(inst.path, []):
            for c in child.connections:
                e = c.get("expr")
                if c["direction"] != "in" or not e or "ref" not in e:
                    continue
                if e["ref"] in clk[inst.path]:
                    clk[child.path].add(c["port"])
                if e["ref"] in rst[inst.path]:
                    rst[child.path][c["port"]] = rst[inst.path][e["ref"]]
            down(child)

    for root in by_parent.get(None, []):
        down(root)
    for i in instances:
        for cand in i.clk_rst_candidates:
            if cand.is_async_reset:
                rst[i.path].setdefault(cand.signal, "low" if cand.edge == "negedge" else "high")
            elif cand.edge:
                clk[i.path].add(cand.signal)
        clk[i.path] -= set(rst[i.path])
    return clk, rst


def build(
    instances: list[InstanceInfo],
    top: str,
    statuses: dict[str, str],
    clocks: list[dict],
    resets: list[dict],
) -> Vdg:
    g = Vdg()
    g.clocks, g.resets = _clock_reset_sets(instances, top, clocks, resets)
    by_path = {i.path: i for i in instances}
    modeled = set()
    for i in instances:
        st = statuses.get(i.path, "OK")
        if st == "BLOCKED":
            g.skipped.append({"path": i.path, "module": i.module, "reason": "N1 状态为 BLOCKED，不进入 N2"})
        else:
            modeled.add(i.path)
    g.modeled = sorted(modeled)

    for path in g.modeled:
        inst = by_path[path]
        registers = {lhs for a in inst.assigns if is_clocked(inst, a.proc) for lhs in a.lhs}
        for p in inst.ports:
            kind = {"in": "input", "out": "output"}.get(p.direction, "inout")
            nid = f"{path}.{p.name}"
            g.nodes[nid] = Node(nid, path, p.name, kind, p.width, "port", path == top,
                                p.name in g.clocks[path], p.name in g.resets[path])
        for s in inst.signals:
            nid = f"{path}.{s.name}"
            kind = "memory" if s.array_dims else "reg" if s.name in registers else "wire"
            g.nodes[nid] = Node(nid, path, s.name, kind, s.width, "internal", False,
                                s.name in g.clocks[path], s.name in g.resets[path])

    edges: set[Edge] = set()

    def add(src: str, dst: str, kind: str, delay: int, via: str, reset: bool = False) -> None:
        if src in g.nodes and dst in g.nodes:  # self-edges kept: cnt <= cnt + 1 reads itself
            edges.add(Edge(src, dst, kind, delay, via, reset))

    for path in g.modeled:
        inst = by_path[path]
        rst = g.resets[path]
        for a in inst.assigns:
            delay = 1 if is_clocked(inst, a.proc) else 0
            canon = json.dumps({"kind": inst.procs[a.proc].kind if a.proc is not None else "assign",
                                "sens": inst.procs[a.proc].sensitivity if a.proc is not None else [],
                                "lhs": a.lhs_tree, "rhs": a.rhs_tree, "cond": a.cond_stack,
                                "blocking": a.blocking}, sort_keys=True)
            for lhs in a.lhs:
                dst = f"{path}.{lhs}"
                g.logic.setdefault(dst, []).append(canon)
                for r in a.rhs_refs:
                    add(f"{path}.{r}", dst, "data", delay, "assign")
                for r in cond_refs(a.cond_stack):
                    add(f"{path}.{r}", dst, "control", delay, "assign", r in rst)
        parent = inst.parent
        if parent is None or parent not in modeled:
            continue
        for c in inst.connections:
            e = c.get("expr")
            if not e:
                continue
            port = f"{path}.{c['port']}"
            refs = tree_refs(e)
            conn = json.dumps({"port": c["port"], "dir": c["direction"], "expr": e}, sort_keys=True)
            if c["direction"] in ("in", "inout"):
                g.logic.setdefault(port, []).append(conn)
            if c["direction"] in ("out", "inout"):
                for r in sorted(refs):
                    g.logic.setdefault(f"{parent}.{r}", []).append(conn)
            if c["direction"] in ("in", "inout"):
                for r in refs:
                    add(f"{parent}.{r}", port, "data", 0, "port", r in g.resets[parent])
            if c["direction"] in ("out", "inout"):
                for r in refs:
                    add(port, f"{parent}.{r}", "data", 0, "port")
    g.edges = sorted(edges)
    for k in g.logic:
        g.logic[k] = sorted(g.logic[k])
    return g
