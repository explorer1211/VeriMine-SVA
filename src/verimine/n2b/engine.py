"""N2b step 2: run the properties through Yosys + SymbiYosys.

This is the minimal engine N2b needs until N6/N7 exist (spec: 引擎抽象层, "Yosys + SymbiYosys
（现在）"). It reads the verification version and decides every property:

  assert   FAILED      BMC found a counterexample (trace kept) -> candidate RTL defect, for N8
           PROVEN      k-induction proved it for all depths
           BOUNDED     no counterexample within `depth` cycles, induction inconclusive
  cover    REACHED     a trace reaches it (step reported)
           UNREACHABLE its negation was proved by k-induction: it can never happen
           UNREACHED   not reached within `depth` cycles
  both     ENV_ERROR   Yosys could not read the design with the assertions
           SKIPPED     the verification version could not be built (reason in `skip`)
           NOT_RUN     sby / yosys / a solver is missing, or formal checking is turned off

Tri-state buses are converted with `tribuf -formal`; its own conflict assertions are deleted
(x.bus_conflict checks the same thing, and they would fail every proof of an unrelated property).

How the assertions get into the design: Yosys has no `bind`, so each module that has
properties gets a same-line `include "vm_n2b_<module>.svh"` right before its `endmodule` in a
copy of the source (no line moves). The include holds immediate assertions in `always` blocks,
labeled, so sby reports them per instance ("top.u_ctrl.vm_3"). One copy of the text serves
every instance of the module, so a property is verified only if every instance of that module
has it (instances with different parameters may not). The top module's include holds the
environment: reset active in the first cycle, and the user's assumes (simple `a |-> b` /
`a |=> b` / expression forms).

Inputs are otherwise unconstrained, so a FAILED assertion can be a missing environment
constraint as well as an RTL bug: N8 decides which.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from verimine.n1.baseline import _HEADER_IMPORT_RE
from verimine.n2b.props import Prop
from verimine.n2b.render import Unrenderable, negated_cover, pv_name, uses_past, verification
from verimine.n2b.scan import ScanResult

SOLVERS = ["bitwuzla", "boolector", "yices-smt2", "z3"]
_FAILED = re.compile(r"^\s*failed assertion (\S+) at .*? step (\d+)")
_REACHED = re.compile(r"^\s*reached cover statement (\S+) at .*? step (\d+)")
_UNREACHED = re.compile(r"^\s+(\S+) at \S+\s*$")
_TRACE = re.compile(r"^\s*(?:counterexample|cover) trace(?: \[\w+\])?: (\S+)")


@dataclass
class EngineResult:
    available: bool
    solver: str = ""
    depth: int = 0
    workdir: str = ""
    runs: list[dict] = field(default_factory=list)      # {"task", "mode", "status", "props"}
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def find_tools() -> tuple[bool, str]:
    if not (shutil.which("sby") and shutil.which("yosys") and shutil.which("yosys-smtbmc")):
        return False, ""
    for s in SOLVERS:
        if shutil.which(s):
            return True, {"yices-smt2": "yices"}.get(s, s)
    return False, ""


def _assume_lines(assumes: list[dict], clock: dict | None, warn: list[str]) -> list[str]:
    out = []
    for a in assumes:
        text = a["expr"].strip()
        if re.search(r"##|\[\*|\[->|\[=|\bs_eventually\b|\beventually\b|\buntil\b", text):
            warn.append(f"假设 {a['name']} 含序列运算，验证版暂不支持，未使用")
            continue
        if ("|->" in text or "|=>" in text) and not clock:
            warn.append(f"假设 {a['name']} 需要时钟，未使用")
            continue
        if "|=>" in text:
            ante, cons = text.split("|=>", 1)
            body = f"!$past({ante.strip()}) || ({cons.strip()})"
            gate = f"!$initstate && {pv_name(clock)}"
        elif "|->" in text:
            ante, cons = text.split("|->", 1)
            body, gate = f"!({ante.strip()}) || ({cons.strip()})", "!$initstate"
        else:
            body, gate = text, "!$initstate"
        ev = f"{clock['edge']} {clock['name']}" if clock else "*"
        out.append(f"  always @({ev}) if ({gate}) {a['name']}: assume({body});")
    return out


class _Design:
    def __init__(self, files, include_dirs, defines, top, root: Path, scan: ScanResult, workdir: Path):
        self.files, self.include_dirs, self.defines, self.top = files, include_dirs, defines, top
        self.root, self.scan, self.workdir = root, scan, workdir

    def write_sources(self, modules: set[str]) -> list[Path]:
        """Copies of the sources with the include hook before `endmodule` of `modules`."""
        src = self.workdir / "src"
        src.mkdir(parents=True, exist_ok=True)
        out = []
        root = self.root.resolve()
        for i, f in enumerate(self.files):
            real = Path(f).resolve()
            rel = real.relative_to(root).as_posix() if real.is_relative_to(root) else str(f)
            data = real.read_bytes()  # offsets from slang are byte offsets
            hooks = sorted((m.end_offset, m.name) for m in self.scan.modules.values()
                           if m.file == rel and m.name in modules)
            for off, name in reversed(hooks):
                hook = f'`include "vm_n2b_{name}.svh" '.encode()
                data = data[:off] + hook + data[off:]
            text = data.decode("utf-8", errors="replace")
            text = _HEADER_IMPORT_RE.sub(
                lambda m: f"{' '.join(m.group(5).split())} {m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}", text)
            dst = src / f"{i:04d}_{Path(f).name}"
            dst.write_text(text, encoding="utf-8")
            out.append(dst)
        return out


def _run_sby(workdir: Path, task: str, mode: str, solver: str, depth: int, timeout: int,
             design: _Design, sources: list[Path], incs: dict[str, str]) -> dict:
    inc = workdir / task / "inc"
    if (workdir / task).exists():
        shutil.rmtree(workdir / task)
    inc.mkdir(parents=True)
    for name, text in incs.items():
        (inc / f"vm_n2b_{name}.svh").write_text(text, encoding="utf-8")
    dirs = [inc] + sorted({Path(f).resolve().parent for f in design.files}) + [Path(d) for d in design.include_dirs]
    defs = ["-DVERIMINE_N2B"] + [f"-D{k}={v}" if v is not None else f"-D{k}" for k, v in sorted(design.defines.items())]
    keep = " --keep-going" if mode == "bmc" else ""
    sby = workdir / task / f"{task}.sby"
    sby.write_text(f"""[options]
mode {mode}
depth {depth}
timeout {timeout}

[engines]
smtbmc{keep} {solver}

[script]
verilog_defaults -add {' '.join(defs)} {' '.join(f'-I{d}' for d in dirs)}
read_verilog -formal -sv {' '.join(str(s) for s in sources)}
hierarchy -top {design.top}
proc
tribuf -formal
delete */*tribuf_conflict*
prep -top {design.top}
""", encoding="utf-8")
    out_dir = workdir / task / "run"
    try:
        r = subprocess.run(["sby", "-f", "-d", str(out_dir), str(sby)], capture_output=True, text=True,
                           timeout=timeout + 60)
        log = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return {"task": task, "mode": mode, "status": "TIMEOUT", "log": "", "failed": {}, "reached": {},
                "unreached": set(), "errors": []}
    status = "ERROR"
    sf = out_dir / "status"
    if sf.exists() and sf.read_text().split():
        status = sf.read_text().split()[0]
    # The console summary is cut after a few traces; the file named after the status has it all.
    summary = out_dir / status
    text = summary.read_text(errors="replace") if summary.is_file() else log
    failed, reached, unreached, errors = {}, {}, set(), []
    trace, in_unreached = "", False
    for line in text.splitlines():
        if m := _TRACE.search(line):
            if m.group(1).endswith(".vcd"):
                t = Path(m.group(1))
                trace = str(t if t.is_absolute() else out_dir / t)
            continue
        if m := _FAILED.search(line):
            failed.setdefault(m.group(1), (int(m.group(2)), trace))
            in_unreached = False
        elif m := _REACHED.search(line):
            reached.setdefault(m.group(1), (int(m.group(2)), trace))
            in_unreached = False
        elif "unreached cover statements:" in line:
            in_unreached = True
        elif in_unreached and (m := _UNREACHED.search(line)):
            unreached.add(m.group(1))
        else:
            in_unreached = False
    errors = [line.split("ERROR:", 1)[1].strip() for line in log.splitlines() if "ERROR:" in line]
    return {"task": task, "mode": mode, "status": status, "log": log[-4000:], "failed": failed,
            "reached": reached, "unreached": unreached, "errors": errors}


def run(props: list[Prop], *, instances, files, include_dirs, defines, top: str, root: Path,
        scan: ScanResult, top_resets: list[dict], assumes: list[dict], top_clock: dict | None,
        workdir: Path, depth: int = 20, timeout: int = 120, prove: bool = True,
        prove_max: int = 64, enabled: bool = True) -> EngineResult:
    ok, solver = find_tools()
    res = EngineResult(available=ok and enabled, solver=solver, depth=depth, workdir=str(workdir))
    if not res.available:
        for p in props:
            p.status = "NOT_RUN"
        if enabled:
            res.warnings.append("没有找到 sby / yosys / yosys-smtbmc 或 SMT 求解器，属性未验证")
        return res

    # Verification text per property, then per module: a property goes in only if every
    # instance of the module has the identical text.
    insts_of: dict[str, set[str]] = {}
    for i in instances:
        insts_of.setdefault(i.module, set()).add(i.path)
    texts: dict[tuple[str, str], set[str]] = {}
    for p in props:
        try:
            p.verification = verification(p, "LABEL")
        except Unrenderable as e:
            p.skip, p.status = str(e), "SKIPPED"
            continue
        if p.module not in scan.modules:
            p.skip, p.status = f"找不到模块 {p.module} 的源码位置（宏或 include 中定义）", "SKIPPED"
            continue
        texts.setdefault((p.module, p.verification), set()).add(p.inst)
    label_of: dict[tuple[str, str], str] = {}
    for n, ((mod, text), insts) in enumerate(sorted(texts.items())):
        if insts == insts_of.get(mod, set()):
            label_of[(mod, text)] = f"vm_{n}"
    by_label: dict[str, Prop] = {}       # "inst.label" -> prop
    for p in props:
        if p.status:
            continue
        lab = label_of.get((p.module, p.verification))
        if lab is None:
            p.skip, p.status = "同一模块的其他实例没有这条属性（参数不同）；验证版按模块插入，暂不验证", "SKIPPED"
            continue
        by_label[f"{p.inst}.{lab}"] = p
        p.verification = p.verification.replace("LABEL:", f"{lab}:", 1)
    live = [p for p in props if not p.status]
    modules = {p.module for p in live} | {top}
    design = _Design(files, include_dirs, defines, top, root, scan, workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    sources = design.write_sources(modules)

    env_lines = []
    if top_resets:
        act = " && ".join(f"!{r['name']}" if r["active"] == "low" else r["name"] for r in top_resets)
        env_lines.append(f"  always @* if ($initstate) assume({act});")
    else:
        res.warnings.append("配置中没有复位：寄存器初值任意，失败的断言可能来自不可达的初始状态")
    env_lines += _assume_lines(assumes, top_clock, res.warnings)

    def incs(selected: dict[str, list[str]]) -> dict[str, str]:
        out = {}
        for mod in modules:
            lines = selected.get(mod, [])
            if mod == top:
                lines = env_lines + lines
            clocks = {}
            for p in live:
                if p.module == mod and p.clock and (p.next or uses_past(p.expr) or uses_past(p.guard)):
                    clocks[p.clock["name"]] = p.clock
            if mod == top and top_clock and any("$past" in x for x in env_lines):
                clocks[top_clock["name"]] = top_clock
            pv = [f"  reg {pv_name(c)} = 1'b0;\n  always @({c['edge']} {c['name']}) {pv_name(c)} <= 1'b1;"
                  for c in clocks.values()]
            out[mod] = "`ifdef VERIMINE_N2B\n" + "\n".join(pv + lines) + "\n`endif\n"
        return out

    def lines_for(ps: list[Prop], negate: bool = False) -> dict[str, list[str]]:
        out: dict[str, set[str]] = {}
        for p in ps:
            lab = p.verification.split(":", 1)[0].rsplit(" ", 1)[-1]
            text = negated_cover(p, "n" + lab) if negate else p.verification
            out.setdefault(p.module, set()).add("  " + text)
        return {k: sorted(v) for k, v in out.items()}

    asserts = [p for p in live if p.kind == "assert"]
    covers = [p for p in live if p.kind == "cover"]

    def go(task, mode, sel):
        return _run_sby(workdir, task, mode, solver, depth, timeout, design, sources, incs(sel))

    workers = max(1, min(8, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {}
        if asserts:
            jobs["bmc"] = pool.submit(go, "bmc", "bmc", lines_for(asserts))
        if covers:
            jobs["cover"] = pool.submit(go, "cover", "cover", lines_for(covers))
        results = {k: f.result() for k, f in jobs.items()}

    def lab_of(p):
        return f"{p.inst}.{p.verification.split(':', 1)[0].rsplit(' ', 1)[-1]}"

    for name, r in results.items():
        res.runs.append({k: r[k] for k in ("task", "mode", "status")} | {"props": len(asserts if name == "bmc" else covers)})
        if r["status"] in ("ERROR",) and not r["failed"] and not r["reached"]:
            res.errors.append(f"{name}: " + ("；".join(r["errors"][:3]) or "sby 失败"))
            for p in asserts if name == "bmc" else covers:
                p.status = "ENV_ERROR"
            continue
        for p in asserts if name == "bmc" else covers:
            key = lab_of(p)
            if name == "bmc":
                if key in r["failed"]:
                    p.status, (p.step, p.trace) = "FAILED", r["failed"][key]
                else:
                    p.status = "BOUNDED" if r["status"] in ("PASS", "FAIL") else "UNDECIDED"
            else:
                if key in r["reached"]:
                    p.status, (p.step, p.trace) = "REACHED", r["reached"][key]
                elif key in r["unreached"]:
                    p.status = "UNREACHED"
                else:
                    p.status = "UNDECIDED"

    if prove:
        todo = [p for p in asserts if p.status == "BOUNDED"] + \
               [p for p in covers if p.status == "UNREACHED" and not p.next and not uses_past(p.expr)]
        todo = [p for p in todo if p.kind == "assert" or _negatable(p)]
        if todo:
            sel: dict[str, set[str]] = {}
            for p in todo:  # props sharing a label (same text) must go in once
                for k, v in lines_for([p], negate=p.kind == "cover").items():
                    sel.setdefault(k, set()).update(v)
            sel = {k: sorted(v) for k, v in sel.items()}
            r = go("prove", "prove", sel)
            res.runs.append({"task": "prove", "mode": "prove", "status": r["status"], "props": len(todo)})
            if r["status"] == "PASS":
                for p in todo:
                    p.status = "PROVEN" if p.kind == "assert" else "UNREACHABLE"
            elif len(todo) <= prove_max:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = [(p, pool.submit(go, f"prove_{i}", "prove", lines_for([p], negate=p.kind == "cover")))
                            for i, p in enumerate(todo)]
                    for p, f in futs:
                        if f.result()["status"] == "PASS":
                            p.status = "PROVEN" if p.kind == "assert" else "UNREACHABLE"
                res.runs.append({"task": "prove_each", "mode": "prove", "status": "DONE", "props": len(todo)})
            else:
                res.warnings.append(f"合并证明未通过，待证属性 {len(todo)} 条超过 prove_max={prove_max}，未逐条证明")
    return res


def _negatable(p: Prop) -> bool:
    try:
        negated_cover(p, "x")
        return True
    except Unrenderable:
        return False
