"""Register checks (only when the config names a register map, `registers:`).

Input: CSV with columns name, address (or offset), width, access, reset[, signal], or an IP-XACT
XML (1685-2009 spirit or 2014/2022 ipxact; only name, addressOffset, size, access, reset value
are read). A register maps to the RTL signal named in the `signal` column, otherwise to a
signal of the same name in the scope instance (also tried: name_q, name_reg, name_r).

  reg.address   static: two registers at the same or overlapping addresses
  reg.decode    static (needs registers.addr): every map address is compared with the address
                signal somewhere in the scope, and the RTL decodes no address the map lacks
  reg.reset     static: the reset value in the RTL equals the map;
                assert: one cycle after reset is active the register holds the map value
  reg.access    static: a read-only register has a write path from the bus (assigned under a
                condition that reads the write enable); a writable register has no assignment
                besides its reset
"""
from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from verimine.n1.assign import tree_refs
from verimine.n2 import exprs as X
from verimine.n2b.props import ERROR, WARNING, EQ, ref, const


@dataclass
class Register:
    name: str
    address: int
    width: int
    access: str          # RW | RO | WO | W1C | ... (upper case)
    reset: int | None
    signal: str = ""


def _int(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.strip().replace("_", "")
    if not s:
        return None
    m = re.fullmatch(r"(?:\d+)?'([bodhBODH])([0-9a-fA-F]+)", s)
    if m:
        return int(m.group(2), {"b": 2, "o": 8, "d": 10, "h": 16}[m.group(1).lower()])
    try:
        return int(s, 0)
    except ValueError:
        return None


def _access(s: str) -> str:
    s = (s or "RW").strip().upper().replace("-", "")
    return {"READWRITE": "RW", "READONLY": "RO", "WRITEONLY": "WO", "READWRITEONCE": "RW",
            "WRITEONCE": "WO"}.get(s, s)


def parse_csv(path: Path) -> list[Register]:
    out = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            addr = _int(row.get("address") or row.get("offset") or row.get("addr"))
            if not row.get("name") or addr is None:
                continue
            out.append(Register(row["name"], addr, _int(row.get("width") or row.get("size")) or 32,
                                _access(row.get("access", "")), _int(row.get("reset")), row.get("signal", "")))
    return out


def parse_ipxact(path: Path) -> list[Register]:
    out = []
    root = ET.parse(path).getroot()
    local = lambda e: e.tag.rsplit("}", 1)[-1]  # noqa: E731

    def child(e, name):
        return next((c for c in e if local(c) == name), None)

    for e in root.iter():
        if local(e) != "register":
            continue
        name, off = child(e, "name"), child(e, "addressOffset")
        if name is None or off is None:
            continue
        size, acc = child(e, "size"), child(e, "access")
        reset = None
        for r in e.iter():
            if local(r) == "reset":
                v = child(r, "value")
                reset = _int(v.text if v is not None else r.text)
                break
        out.append(Register(name.text.strip(), _int(off.text) or 0, _int(size.text if size is not None else "") or 32,
                            _access(acc.text if acc is not None else ""), reset))
    return out


def load(path: Path, fmt: str | None) -> list[Register]:
    fmt = fmt or ("ipxact" if path.suffix.lower() in (".xml", ".ipxact") else "csv")
    return parse_ipxact(path) if fmt == "ipxact" else parse_csv(path)


def register_checks(cx, cfg: dict, root: Path) -> None:
    regs = load(root / cfg["file"], cfg.get("format"))
    scope = cfg.get("scope") or cx.n2.n1.elaboration.top_path
    inst = cx.by_path.get(scope)
    if inst is None or scope not in cx.views:
        cx.find("reg.address", None, f"寄存器检查：作用域实例 {scope} 不存在或未建模", WARNING, module="")
        return
    view = cx.views[scope]
    names = set(view.widths)

    # reg.address: duplicates and overlaps
    spans = sorted(((r.address, r.address + max(1, (r.width + 7) // 8) - 1, r) for r in regs),
                   key=lambda s: (s[0], s[1], s[2].name))
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            a0, a1, ra = spans[i]
            b0, b1, rb = spans[j]
            if b0 > a1:
                break
            cx.find("reg.address", inst, f"寄存器 {ra.name}（0x{ra.address:x}）与 {rb.name}（0x{rb.address:x}）地址"
                    f"{'相同' if a0 == b0 else '重叠'}", ERROR, [])

    # reg.decode
    addr = cfg.get("addr")
    if addr:
        decoded = view.compared_consts(addr)
        for r in regs:
            if r.address not in decoded:
                cx.find("reg.decode", inst, f"寄存器 {r.name} 的地址 0x{r.address:x} 在 RTL 中没有被 {addr} 译码",
                        ERROR, [addr])
        for a in sorted(decoded - {r.address for r in regs}):
            cx.find("reg.decode", inst, f"RTL 译码了地址 0x{a:x}（{addr}），寄存器表中没有这个地址", WARNING, [addr])

    rst = cx.g.resets.get(scope, {})
    write = cfg.get("write")
    for r in regs:
        sig = r.signal or next((c for c in (r.name, r.name.lower(), f"{r.name.lower()}_q", f"{r.name.lower()}_reg",
                                            f"{r.name.lower()}_r") if c in names), "")
        if not sig or sig not in names:
            cx.find("reg.access", inst, f"寄存器 {r.name} 在 {scope} 中找不到对应信号（可在 CSV 的 signal 列指定）",
                    WARNING, [])
            continue
        writes = view.writes.get(sig, [])
        resets = [a for a in writes if view.in_reset(a)]
        others = [a for a in writes if not view.in_reset(a)]

        # reg.reset
        if r.reset is not None:
            rtl = sorted({X.const_of(a.rhs_tree) for a in resets if X.const_of(a.rhs_tree) is not None})
            if rtl and r.reset not in rtl:
                cx.find("reg.reset", inst, f"寄存器 {r.name}（{sig}）的复位值：表中 0x{r.reset:x}，RTL 中 "
                        + "、".join(f"0x{v:x}" for v in rtl), ERROR, [sig], resets[0].location)
            if not resets:
                cx.find("reg.reset", inst, f"寄存器 {r.name}（{sig}）在 RTL 中没有复位赋值，表中复位值 0x{r.reset:x}",
                        WARNING, [sig])
            clock = cx.proc_clock(inst, writes[0].proc) if writes else cx.inst_clock(inst)
            if rst and clock:
                active = None
                for n, lv in sorted(rst.items()):
                    t = {"op": "!", "args": [ref(n)]} if lv == "low" else ref(n)
                    active = t if active is None else {"op": "||", "args": [active, t]}
                cx.prop(inst, "assert", "reg.reset", EQ(ref(sig), const(r.reset)), guard=active, next=True,
                        clock=clock, resets={}, message=f"复位后 {sig} 为表中复位值 0x{r.reset:x}",
                        on_trigger={"message": f"寄存器 {r.name}（{sig}）复位后不等于表中复位值 0x{r.reset:x}",
                                    "severity": ERROR, "signals": [sig]})

        # reg.access
        if r.access == "RO" and write:
            for a in others:
                conds = set().union(*(tree_refs(e) for e in X.cond_exprs(a.cond_stack))) if a.cond_stack else set()
                if write in conds or write in a.rhs_refs:
                    cx.find("reg.access", inst, f"只读寄存器 {r.name}（{sig}）有总线写入路径（受 {write} 控制）",
                            ERROR, [sig], a.location)
                    break
        if r.access in ("RW", "WO", "W1C", "W1S") and not others:
            cx.find("reg.access", inst, f"可写寄存器 {r.name}（{sig}，{r.access}）除复位外没有任何赋值", ERROR, [sig])
