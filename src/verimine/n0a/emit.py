"""N0a steps 11-13: YAML draft, inference report, optional .f export.

Output is deterministic: same input, byte-identical files. Every list is sorted or in compile
order, every path is relative, and nothing carries a timestamp or machine name.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from verimine.config import Config

REPORT_VERSION = 1


def _flow_map(d: dict) -> CommentedMap:
    m = CommentedMap(d)
    m.fa.set_flow_style()
    return m


def flow_seq(items: list[dict]) -> CommentedSeq:
    return CommentedSeq(_flow_map(x) for x in items)


def _yaml() -> YAML:
    y = YAML()  # round-trip: keeps key order and, when filling an existing YAML, its comments
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 120
    return y


def draft_document(cfg: dict[str, Any]) -> CommentedMap:
    """Plain config dict -> ordered document in the layout of the spec's example."""
    d = cfg["design"]
    design = CommentedMap()
    for key in ("name", "root", "top", "filelist", "files", "include_dirs", "defines",
                "parameters", "language", "blackbox"):
        if key in d:
            design[key] = d[key]
    doc = CommentedMap()
    doc["schema_version"] = cfg.get("schema_version", 1)
    doc["status"] = cfg["status"]
    doc["unresolved"] = CommentedSeq(cfg["unresolved"])
    doc["design"] = design
    doc["clocks"] = flow_seq(cfg["clocks"])
    doc["resets"] = flow_seq(cfg["resets"])
    doc["waveforms"] = CommentedSeq(cfg.get("waveforms", []))
    doc["llm"] = _flow_map(cfg.get("llm", {"mode": "off"}))
    doc["run"] = _flow_map(cfg.get("run", {"seed": 1, "profile": "default"}))
    doc.yaml_set_start_comment("由 verimine infer 生成的草稿。处理完 unresolved 后运行 verimine confirm。")
    return doc


def validate(doc: dict) -> None:
    """The draft must already satisfy the shared config model; only status/unresolved differ."""
    Config.model_validate(json.loads(json.dumps(doc)))


def dump_yaml(doc: CommentedMap) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def load_yaml_rt(path: Path) -> CommentedMap:
    return _yaml().load(path.read_text(encoding="utf-8"))


def dump_report(report: dict) -> str:
    report = {"report_version": REPORT_VERSION, **report}
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def export_filelist(yaml_name: str, design: dict) -> str:
    """Step 13. Derived from the YAML; the YAML stays the source of truth."""
    lines = [f"// 由 {yaml_name} 生成，以 YAML 为准。不要手工修改此文件。"]
    lines += [f"+incdir+{d}" for d in design.get("include_dirs", [])]
    for k, v in design.get("defines", {}).items():
        lines.append(f"+define+{k}" if v is None else f"+define+{k}={v}")
    lines += list(design.get("files", []))
    return "\n".join(lines) + "\n"
