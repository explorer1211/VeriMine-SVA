# VeriMine-SVA
SVA-Agnet automates legacy RTL verification for chip firms, IP vendors, and labs. It mines RTL behavior, uses LLMs to generate SVA, and refines assertions through formal verification, non-vacuity checks, coverage feedback, and counterexample-guided repair, reducing verification costs and turnaround time.

## Status

| Stage | What | Docs |
|---|---|---|
| N0a | Config completion: RTL / .f / partial YAML → YAML draft + inference report (top, files and order, include dirs, macros, black boxes, clocks/resets); `confirm` turns it into a confirmed config | [docs/N0a.md](docs/N0a.md) |
| N0 | Project preprocessing: confirmed YAML (+ .f) → normalized config, source/build/env/run hashes, content-addressed snapshot, design/revision/run records | [docs/N0.md](docs/N0.md) |
| N1 | Structural index: slang elaboration, instance tree, assignments with control-condition stacks, module fingerprints, slang/Verilator/Yosys baseline, module status, clock domains | [docs/N1.md](docs/N1.md) |
| N2 | Behavioral model: variable dependency graph, state machines and transitions, counters / shift registers / memories, interface events, signal roles with confidence, PageRank, COI fingerprints | [docs/N2.md](docs/N2.md) |
| N2b | Structural checks: templates per problem class (dead code, FSM, arrays/arithmetic, X/drivers, control, timing, counters/FIFOs, registers) → assertions and covers, checked with Yosys + SymbiYosys; failed assertions are candidate RTL defects; standalone report and a bind file of standard SVA | [docs/N2b.md](docs/N2b.md) |
| N3 | Trace preparation: user waveforms (VCD/FST via wellen) → user testbench → generated stimulus (random under confirmed assumes, formal exploration of gaps, conservative without assumes); sampled per cycle, trimmed, cut into test cases, split into S_mine / S_val; branch / state / transition / toggle coverage and gap list | [docs/N3.md](docs/N3.md) |
| N3a | Assumption mining: candidates from N2 interface events (protocol hold, flow control) and from the user's traces (mutex, one-hot, reset values, minimum hold, ranges); trace check, over-strength check with N2b covers, assume-guarantee for submodules (DISCHARGED / OPEN), confirmation into `assumes`, env_missing attribution of N2b counterexamples | [docs/N3a.md](docs/N3a.md) |

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'   # dev includes pywellen
.venv/bin/verimine infer path/to/rtl          # N0a: draft verimine.yaml + n0a_report.json
.venv/bin/verimine confirm path/to/rtl/verimine.yaml
.venv/bin/verimine n0 examples/fifo/verimine.yaml
.venv/bin/verimine n1 examples/fifo/verimine.yaml
.venv/bin/verimine n2 examples/fifo/verimine.yaml
.venv/bin/verimine n2b examples/fifo/verimine.yaml   # needs yosys + sby + an SMT solver for the formal part
.venv/bin/verimine n3 examples/fifo/verimine.yaml    # needs Icarus Verilog (or Verilator)
.venv/bin/verimine n3a examples/fifo/verimine.yaml   # add --accept-high / --accept IDs to write assumes
.venv/bin/pytest
```
