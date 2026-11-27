"""N7 step 6: run proof jobs in threads, in subprocesses, or on Slurm.

The job format (jobs.py) is plain JSON, so all three backends run the same code:

  thread     one process, jobs in a thread pool (the default; small designs)
  process    one Python subprocess per job (clear isolation, same machine)
  slurm      one Slurm job per job, `sbatch --wait`; falls back to `process` (with a warning)
             where sbatch is not installed. Not tested on a cluster (see docs/N7.md).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from verimine.n7.jobs import run_spec

BACKENDS = ("thread", "process", "slurm")


def available() -> list[str]:
    return [b for b in BACKENDS if b != "slurm" or shutil.which("sbatch")]


def _subprocess(spec: dict) -> dict:
    work = Path(spec["workdir"])
    work.mkdir(parents=True, exist_ok=True)
    sp, out = work / "spec.json", work / "out.json"
    sp.write_text(json.dumps(spec, ensure_ascii=False, default=str), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "verimine.n7.jobs", str(sp), str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        return {"name": spec["name"], "available": False, "props": {}, "runs": [],
                "errors": [f"子进程失败：{(r.stderr or r.stdout).strip()[-300:]}"], "warnings": []}
    return json.loads(out.read_text(encoding="utf-8"))


def _slurm(spec: dict) -> dict:
    work = Path(spec["workdir"])
    sp, out, log = work / "spec.json", work / "out.json", work / "slurm.log"
    sp.write_text(json.dumps(spec, ensure_ascii=False, default=str), encoding="utf-8")
    script = work / "job.sh"
    script.write_text("#!/bin/sh\n"
                      f"#SBATCH --job-name=verimine-n7-{spec['name']}\n#SBATCH --ntasks=1\n"
                      f"#SBATCH --output={log}\n"
                      f"cd {Path.cwd()}\n"
                      f"{sys.executable} -m verimine.n7.jobs {sp} {out}\n", encoding="utf-8")
    r = subprocess.run(["sbatch", "--wait", str(script)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        return {"name": spec["name"], "available": False, "props": {}, "runs": [],
                "errors": [f"Slurm 提交失败：{(r.stderr or r.stdout).strip()[-300:]}"], "warnings": []}
    return json.loads(out.read_text(encoding="utf-8"))


def run(specs: list[dict], backend: str = "thread", workers: int | None = None,
        warnings: list[str] | None = None) -> list[dict]:
    warnings = warnings if warnings is not None else []
    if backend not in BACKENDS:
        warnings.append(f"未知的并行方式 {backend}，改用 thread")
        backend = "thread"
    if backend == "slurm" and not shutil.which("sbatch"):
        warnings.append("没有 sbatch：Slurm 并行改用子进程（本机）")
        backend = "process"
    if not specs:
        return []
    if backend == "thread":
        n = max(1, min(workers or 1, len(specs), os.cpu_count() or 1))
        if n == 1:
            return [run_spec(s) for s in specs]
        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(run_spec, specs))
    fn = _slurm if backend == "slurm" else _subprocess
    n = max(1, min(workers or len(specs), len(specs)))
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(fn, specs))
