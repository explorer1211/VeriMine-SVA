"""N0: engineering preprocessing. Confirmed YAML in; normalized config, four hashes, snapshot,
and design / revision / run records out."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verimine.hashing import file_hash, tagged_hash
from verimine.n0.includes import collect_includes
from verimine.n0.load import load
from verimine.n0.store import record, snapshot
from verimine.n0.tools import collect_tools


@dataclass
class N0Result:
    root: Path
    workspace: Path
    normalized: dict[str, Any]
    sources: list[dict[str, str]]
    hashes: dict[str, str]
    tools: dict[str, str]
    warnings: list
    db: dict | None

    def to_dict(self) -> dict:
        return {
            "stage": "N0",
            "root": str(self.root),
            "hashes": self.hashes,
            "normalized": self.normalized,
            "sources": self.sources,
            "tools": self.tools,
            "warnings": [w.to_dict() for w in self.warnings],
            "db": self.db,
        }


def compute_hashes(normalized: dict, sources: list[dict], tools: dict[str, str]) -> dict[str, str]:
    """Step 5. Each layer hashes the one below plus its own inputs. Paths are root-relative,
    so moving the project directory changes nothing."""
    d = normalized["design"]
    source_hash = tagged_hash("source", sorted([s["path"], s["sha256"]] for s in sources))
    build_hash = tagged_hash("build", {
        "source_hash": source_hash,
        "top": d["top"],
        "files": d["files"],
        "library_files": d["library_files"],
        "library_dirs": d["library_dirs"],
        "libext": d["libext"],
        "include_dirs": d["include_dirs"],
        "defines": d["defines"],
        "parameters": d["parameters"],
        "language": d["language"],
        "blackbox": d["blackbox"],
        "assumes": normalized["assumes"],
    })
    digest = normalized["env"]["docker_digest"]
    env_hash = tagged_hash("env", {
        "clocks": normalized["clocks"],
        "resets": normalized["resets"],
        # A pinned image fixes every tool version at once; otherwise use what was detected.
        "tools": {"docker_digest": digest} if digest else tools,
    })
    run_hash = tagged_hash("run", {
        "build_hash": build_hash,
        "env_hash": env_hash,
        **run_inputs(normalized),
    })
    return {"source_hash": source_hash, "build_hash": build_hash, "env_hash": env_hash, "run_hash": run_hash}


def run_inputs(normalized: dict) -> dict:
    llm = normalized["llm"]
    # With the LLM off, a leftover provider/model name must not create a "different" run.
    if llm["mode"] == "off":
        llm = {"mode": "off"}
    return {"llm": llm, "run": normalized["run"]}


def run_n0(
    config_path: Path,
    workspace: Path | None = None,
    env: dict[str, str] | None = None,
    tools: dict[str, str] | None = None,
    write: bool = True,
) -> N0Result:
    loaded = load(Path(config_path), env=env)  # steps 0-3
    root, normalized, log = loaded.root, loaded.normalized, loaded.log
    d = normalized["design"]

    units = d["files"] + d["library_files"] + loaded.library_dir_files
    includes = collect_includes(root, units, d["include_dirs"], d["defines"], log)  # step 4
    log.raise_if_errors()

    roles: dict[str, str] = {}
    for paths, role in ((d["files"], "source"), (d["library_files"] + loaded.library_dir_files, "library"),
                        (includes, "include")):
        for p in paths:
            roles.setdefault(p, role)
    sources, blobs = [], {}
    for p in sorted(roles):
        h, data = file_hash(root / p)
        sources.append({"path": p, "sha256": h, "role": roles[p]})
        blobs[h] = data

    tools = collect_tools() if tools is None else dict(sorted(tools.items()))  # step 6
    hashes = compute_hashes(normalized, sources, tools)

    workspace = Path(workspace) if workspace else root / ".verimine"
    db = None
    if write:
        snapshot(workspace / "store", blobs)  # step 7
        db = record(workspace / "verimine.db", normalized, sources, hashes, tools,
                    run_inputs(normalized))  # step 8
    result = N0Result(root, workspace, normalized, sources, hashes, tools, sorted(log.warnings), db)
    if write:
        out = workspace / "runs" / hashes["run_hash"] / "n0.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result
