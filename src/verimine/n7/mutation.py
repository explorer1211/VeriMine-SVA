"""N7 step 10: mutation kill, as the supplement the report has to give.

Every PROVEN assertion claims the design does something. A mutant (one operator in one
assignment changed in the RTL), run on the same vector as the differential test, is killed if
some PROVEN assertion fails on it. The kill ratio is an approximate measure of how much of the
logic the proven set actually constrains; it is not a proof core and is labelled as such.

Mutations are textual, on the line of an N1 assignment, one operator at a time. A mutant that
does not compile is dropped as invalid (not counted as killed). Capped by `n7_mutants`.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from verimine.n2b.engine import find_tools
from verimine.n2b.render import checker_module, refs
from verimine.n3.stim import available
from verimine.n5 import diff as diff_mod
from verimine.n5.render import CYCLE
from verimine.n6 import variants as V


def _p(sig: str) -> str:
    return "p_" + re.sub(r"_+", "_", re.sub(r"\W", "_", sig)).strip("_")

# tokens that may be mutated (multi-char first) and their replacement
_OPS = {"==": "!=", "!=": "==", "&&": "||", "||": "&&", "+": "-", "-": "+", "&": "|", "|": "&", "^": "~^"}
_SKIP = {"===", "!==", "==?", "!=?", "<=", ">=", "<<", ">>", "++", "--", "->", "=>"}
_TOKEN = re.compile(r"===|!==|==\?|!=\?|==|!=|<=|>=|<<|>>|&&|\|\||\+\+|--|->|[+\-^&|]")


def _mutable(line: str) -> list[tuple[int, str, str]]:
    """(position, token, replacement) of the operators in a line that can be mutated."""
    out = []
    quotes = line.count('"')
    for m in _TOKEN.finditer(line):
        tok = m.group(0)
        if tok in _SKIP or tok not in _OPS:
            continue
        before = line[: m.start()]
        if quotes and before.count('"') % 2:
            continue
        out.append((m.start(), tok, _OPS[tok]))
    return out


def sites(scan, instances, modeled: set[str], root: Path) -> list[dict]:
    """Candidate lines: in a modeled module, the assignment lines that write one of its
    signals. N1's assignment locations are empty on this pyslang version, so the line is found
    in the module span N2b's scanner recorded (file, first/last line)."""
    import re as _re

    seen, out = set(), []
    cache: dict[Path, list[str]] = {}
    for inst in instances:
        if inst.path not in modeled:
            continue
        span = scan.modules.get(inst.module)
        if span is None:
            continue
        path = (root / span.file) if not Path(span.file).is_absolute() else Path(span.file)
        if path not in cache:
            try:
                cache[path] = path.read_text(encoding="utf-8", errors="replace").split("\n")
            except OSError:
                cache[path] = []
        lines = cache[path]
        signals = sorted({x for a in inst.assigns for x in a.lhs})
        for ln in range(span.start_line, min(span.end_line, len(lines)) + 1):
            if (path, ln) in seen:
                continue
            text = lines[ln - 1]
            if not _re.search(r"(?<![<>=!])=(?!=)|<=", text):
                continue
            hit = next((x for x in signals if _re.search(rf"(?<![\w$]){_re.escape(x)}(?![\w$])", text)), None)
            if hit:
                seen.add((path, ln))
                out.append({"file": str(path), "line": ln, "inst": inst.path, "signal": hit})
    return sorted(out, key=lambda s: (s["file"], s["line"]))


def _mutants(sites_list: list[dict], root: Path, cap: int) -> list[dict]:
    out = []
    files: dict[str, list[str]] = {}
    for s in sites_list:
        p = Path(s["file"])
        p = p if p.is_absolute() else root / p
        try:
            if p not in files:
                files[p] = p.read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            continue
        lines = files[p]
        if not (1 <= s["line"] <= len(lines)):
            continue
        line = lines[s["line"] - 1]
        for k, (pos, tok, rep) in enumerate(_mutable(line)[:1]):
            out.append({**s, "path": p, "pos": pos, "token": tok, "to": rep, "mutated": line[:pos] + rep + line[pos + len(tok):]})
            if len(out) >= cap:
                return out
    return out


def run(units, *, spec_common, n3, d, by_path, params: dict, workdir: Path, warnings: list[str]) -> dict:
    """units: N7Prop with .name, .kind, .prop, .variant, .status (PROVEN only are used)."""
    proven = [u for u in units if u.kind == "assert" and getattr(u, "status", "") == "PROVEN"]
    cap = int(params.get("n7_mutants", 0))
    base = {"total": 0, "killed": 0, "invalid": 0, "survived": [], "ratio": None,
            "basis": "mutation_kill（Verilator 仿真，同一向量）",
            "note": "证明核覆盖的近似：变异体被已证明断言杀死=该逻辑被约束；近似值，不是证明核"}
    if cap <= 0:
        return base | {"basis": "已关闭（n7_mutants: 0）"}
    if not proven:
        return base | {"basis": "没有已证明的断言"}
    if not (available("verilator") and find_tools()[0]):
        return base | {"basis": "需要 Verilator 和形式引擎（同一向量由 N5 的差分测试构造）"}
    from verimine.n5.pipeline import DEFAULTS as N5D
    vp = N5D | {"n5_diff_cases": int(params.get("n7_vec_cases", 2)),
                "n5_diff_cycles": int(params.get("n7_vec_cycles", 30))}
    vec = diff_mod.build_vector(n3.cases, d, vp)
    if vec is None:
        return base | {"basis": "没有可用作向量的轨迹"}
    # checkers for the PROVEN assertions, in simulation form
    by_inst: dict[str, list] = {}
    for u in proven:
        p = u.prop
        text = V.delivery_text(p, max(1, u.variant))   # ports renamed: never a label/port clash
        if not text.endswith(");"):
            continue
        import copy
        q = copy.copy(p)
        q.delivery = text[:-1] + f' else $display("VM_FAIL {u.name} %0d", {CYCLE});'
        by_inst.setdefault(p.inst, []).append(q)
    mods, checkers = [], []
    for k, (inst, ps) in enumerate(sorted(by_inst.items())):
        info = by_path[inst]
        text, _b = checker_module(f"vm_n7_mut_{k}", inst, ps, {x.name: (x.width or 1, x.signed)
                                                               for x in [*info.ports, *info.signals]}, port=_p)
        mods.append(text)
        checkers.append((f"vm_n7_mut_{k}", inst, sorted(set().union(*(refs(p) for p in ps)))))
    if not checkers:
        return base | {"basis": "已证明的断言无法用于仿真检查"}
    tb = diff_mod.bench_text(by_path[spec_common["top"]], spec_common.get("parameters", {}), vec, checkers,
                             port=_p)
    root = Path(spec_common["root"])
    sites_list = sites(spec_common["scan"], spec_common["n1"].elaboration.instances,
                       set(spec_common["modeled"]), root)
    muts = _mutants(sites_list, root, cap)
    base["total"] = len(muts)
    for i, m in enumerate(muts):
        work = workdir / f"mut_{i}"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        files = []
        for f in spec_common["files"]:
            f = Path(f)
            if f.resolve() == Path(m["path"]).resolve():
                dst = work / f.name
                lines = Path(m["path"]).read_text(encoding="utf-8", errors="replace").split("\n")
                lines[m["line"] - 1] = m["mutated"]
                dst.write_text("\n".join(lines), encoding="utf-8")
                files.append(dst)
            else:
                files.append(f)
        sim = diff_mod.run_sim(files, spec_common["include_dirs"], spec_common["defines"], "\n".join(mods), tb,
                               work / "sim", int(params.get("n7_mut_timeout", 120)))
        if "error" in sim:
            base["invalid"] += 1
            continue
        hit = [n for n in sim["hits"] if any(u.name == n for u in proven)]
        if hit:
            base["killed"] += 1
        else:
            base["survived"].append({"file": str(m["path"]), "line": m["line"], "inst": m["inst"],
                                     "signal": m["signal"], "mutation": f"{m['token']} → {m['to']}"})
    good = base["total"] - base["invalid"]
    base["ratio"] = round(base["killed"] / good, 4) if good else None
    if base["invalid"]:
        warnings.append(f"变异：{base['invalid']} 个变异体编译不过，未计入")
    return base
