"""N7 proof jobs: one engine run described as plain JSON.

A job carries everything an engine run needs (design files, environment, the properties with
their chosen rendering) and comes back as plain JSON, so the same job can run in a thread of
this process, in a subprocess, or on a Slurm node, and the layer logic in prove.py does not
care which. `python -m verimine.n7.jobs spec.json out.json` is the entry point for the last two.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from verimine.n2b import engine
from verimine.n2b.props import Prop
from verimine.n2b.scan import scan as n2b_scan
from verimine.n6 import variants


@dataclass
class _Inst:
    """What engine.run reads of an instance: its module and path."""
    module: str
    path: str


def make(units, *, files, include_dirs, defines, top, root, instances, top_resets, assumes, top_clock,
         depth: int, timeout: int, prove: bool, prove_max: int, workdir: Path, name: str) -> dict:
    """units: objects with .name, .kind, .prop (N2b Prop) and .variant."""
    return {
        "name": name, "workdir": str(workdir), "files": [str(f) for f in files],
        "include_dirs": [str(d) for d in include_dirs], "defines": dict(defines), "top": top, "root": str(root),
        "instances": [[i.path, i.module] for i in instances],
        "top_resets": list(top_resets), "assumes": list(assumes), "top_clock": top_clock,
        "depth": depth, "timeout": timeout, "prove": prove, "prove_max": prove_max,
        "props": [{"id": u.name, "kind": u.kind, "inst": u.prop.inst, "module": u.prop.module,
                   "expr": u.prop.expr, "guard": u.prop.guard, "next": u.prop.next,
                   "check": u.prop.check, "message": u.prop.message, "clock": u.prop.clock,
                   "resets": dict(u.prop.resets), "variant": int(u.variant)} for u in units],
    }


def run_spec(spec: dict) -> dict:
    root = Path(spec["root"])
    files = [Path(f) for f in spec["files"]]
    incdirs = [Path(d) for d in spec["include_dirs"]]
    defines = spec["defines"]
    scan = n2b_scan(files, spec["top"], incdirs, defines, root)
    props = []
    for d in spec["props"]:
        p = Prop(d["kind"], d["check"], d["inst"], d["module"], d["expr"], guard=d["guard"], next=d["next"],
                 clock=d["clock"], resets=dict(d["resets"]), message=d["message"])
        p.id = d["id"]
        # the text under test is the one N6 compiled; engine.run substitutes its own label
        p.verification = variants.verification(p, "LABEL", d["variant"])
        props.append(p)
    res = engine.run(props, instances=[_Inst(m, p) for p, m in spec["instances"]], files=files,
                     include_dirs=incdirs, defines=defines, top=spec["top"], root=root, scan=scan,
                     top_resets=spec["top_resets"], assumes=spec["assumes"], top_clock=spec["top_clock"],
                     workdir=Path(spec["workdir"]), depth=int(spec["depth"]), timeout=int(spec["timeout"]),
                     prove=bool(spec["prove"]), prove_max=int(spec["prove_max"]),
                     render=lambda p, lab: p.verification)
    return {"name": spec["name"], "available": res.available, "solver": res.solver, "depth": res.depth,
            "runs": res.runs, "errors": res.errors, "warnings": res.warnings,
            "props": {p.id: {"status": p.status, "step": p.step, "trace": p.trace, "skip": p.skip}
                      for p in props}}


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 2:
        print("usage: python -m verimine.n7.jobs spec.json out.json", file=sys.stderr)
        return 2
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    out = run_spec(spec)
    Path(argv[1]).write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
