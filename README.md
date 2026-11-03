# VeriMine-SVA
SVA-Agnet automates legacy RTL verification for chip firms, IP vendors, and labs. It mines RTL behavior, uses LLMs to generate SVA, and refines assertions through formal verification, non-vacuity checks, coverage feedback, and counterexample-guided repair, reducing verification costs and turnaround time.

## Status

| Stage | What | Docs |
|---|---|---|
| N0a | Config completion: RTL / .f / partial YAML → YAML draft + inference report (top, files and order, include dirs, macros, black boxes, clocks/resets); `confirm` turns it into a confirmed config | [docs/N0a.md](docs/N0a.md) |
| N0 | Project preprocessing: confirmed YAML (+ .f) → normalized config, source/build/env/run hashes, content-addressed snapshot, design/revision/run records | [docs/N0.md](docs/N0.md) |
| N1 | Structural index: slang elaboration, instance tree, module fingerprints, slang/Verilator/Yosys baseline, module status, clock domains | [docs/N1.md](docs/N1.md) |

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/verimine infer path/to/rtl          # N0a: draft verimine.yaml + n0a_report.json
.venv/bin/verimine confirm path/to/rtl/verimine.yaml
.venv/bin/verimine n0 examples/fifo/verimine.yaml
.venv/bin/pytest
```
