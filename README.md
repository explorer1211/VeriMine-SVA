# VeriMine-SVA
SVA-Agnet automates legacy RTL verification for chip firms, IP vendors, and labs. It mines RTL behavior, uses LLMs to generate SVA, and refines assertions through formal verification, non-vacuity checks, coverage feedback, and counterexample-guided repair, reducing verification costs and turnaround time.

## Status

| Stage | What | Docs |
|---|---|---|
| N0 | Project preprocessing: confirmed YAML (+ .f) → normalized config, source/build/env/run hashes, content-addressed snapshot, design/revision/run records | [docs/N0.md](docs/N0.md) |

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/verimine n0 examples/fifo/verimine.yaml
.venv/bin/pytest
```
