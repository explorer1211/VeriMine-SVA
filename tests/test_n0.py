import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import codes
from verimine.config import json_schema
from verimine.issues import StageError
from verimine.n0.load import EXIT_NOT_CONFIRMED


# ---- step 0: entry check -------------------------------------------------------------------

def test_draft_rejected(project):
    project.cfg["status"] = "draft"
    with pytest.raises(StageError) as e:
        project.run()
    assert codes(e) == ["N0_NOT_CONFIRMED"]
    assert e.value.exit_code == EXIT_NOT_CONFIRMED


def test_unresolved_rejected_even_if_confirmed(project):
    project.cfg["unresolved"] = ["宏 WIDTH 需要取值"]
    with pytest.raises(StageError) as e:
        project.run()
    assert codes(e) == ["N0_UNRESOLVED"]


def test_entry_check_runs_before_schema(project):
    # A half-filled draft must be told "confirm first", not flooded with schema errors.
    project.cfg = {"status": "draft", "design": {"bogus": 1}}
    with pytest.raises(StageError) as e:
        project.run()
    assert codes(e) == ["N0_NOT_CONFIRMED"]


# ---- steps 1-3: merge, validation, normalization -------------------------------------------

def test_yaml_and_filelist_merge(project):
    project.write("rtl/pkg.sv", "package p; endpackage\n")
    project.write("rtl/inc/d.vh", "`define W 4\n")
    project.write("files.f", "+incdir+rtl/inc\n+define+W=8+SYNTH\nrtl/pkg.sv\n")
    project.design.update(filelist=["files.f"], defines={"W": 16})
    res = project.run()
    d = res.normalized["design"]
    assert d["files"] == ["rtl/pkg.sv", "rtl/top.sv"]  # .f entries first, then YAML
    assert d["include_dirs"] == ["rtl/inc"]
    assert d["defines"] == {"SYNTH": None, "W": "16"}  # YAML overrides .f, values as strings
    assert [w.code for w in res.warnings] == ["N0_DEFINE_OVERRIDE"]


def test_all_errors_reported_together(project):
    project.write("../outside.sv", "module o; endmodule\n")
    project.design.update(
        top=None,
        files=["rtl/top.sv", "rtl/missing.sv", "../outside.sv", "rtl/./top.sv"],
    )
    project.cfg["clocks"] = [{"name": "clk"}, {"name": "clk"}]
    project.cfg["resets"] = [{"name": "rst_n", "active": "low", "type": "async", "clock": "clk2"}]
    with pytest.raises(StageError) as e:
        project.run()
    assert codes(e) == sorted([
        "N0_TOP_MISSING", "N0_FILE_MISSING", "N0_PATH_ESCAPES_ROOT", "N0_DUP_FILE",
        "N0_DUP_SIGNAL", "N0_RESET_CLOCK_UNKNOWN",
    ])


def test_symlink_escaping_root_rejected(project, tmp_path):
    (tmp_path / "secret.sv").write_text("module s; endmodule\n")
    os.symlink(tmp_path / "secret.sv", project.root / "rtl" / "link.sv")
    project.design["files"].append("rtl/link.sv")
    with pytest.raises(StageError) as e:
        project.run()
    assert codes(e) == ["N0_PATH_ESCAPES_ROOT"]


@pytest.mark.parametrize("patch, field", [
    ({"clocks": [{"name": "clk", "edge": "rising"}]}, "clocks.0.edge"),
    ({"resets": [{"name": "rst_n", "active": "lo", "type": "async"}]}, "resets.0.active"),
    ({"resets": [{"name": "rst_n", "active": "low"}]}, "resets.0.type"),
    ({"clocks": [{"name": "1clk"}]}, "clocks.0.name"),
    ({"llm": {"mode": "maybe"}}, "llm.mode"),
    ({"extra_key": 1}, "extra_key"),
])
def test_schema_errors(project, patch, field):
    project.cfg.update(patch)
    with pytest.raises(StageError) as e:
        project.run()
    assert codes(e) == ["N0_SCHEMA"]
    assert field in e.value.issues[0].message


def test_llm_mode_off_is_string_not_false(project):
    # YAML 1.1 would read `off` as False; this would then fail the Literal check.
    p = project.yaml()
    p.write_text(p.read_text().replace("status: confirmed", "status: confirmed\nllm: {mode: off}"))
    from verimine.n0 import run_n0
    res = run_n0(p, tools={"x": "1"}, env={}, write=False)
    assert res.normalized["llm"]["mode"] == "off"


def test_library_dir_files_hashed(project):
    project.write("lib/cell_a.v", "module cell_a; endmodule\n")
    project.write("lib/cell_b.sv", "module cell_b; endmodule\n")
    project.write("lib/notes.txt", "ignored\n")
    project.design["library_dirs"] = ["lib"]
    res = project.run()
    lib = [s["path"] for s in res.sources if s["role"] == "library"]
    assert lib == ["lib/cell_a.v", "lib/cell_b.sv"]


# ---- step 4: includes ----------------------------------------------------------------------

def test_nested_includes_collected(project):
    project.write("inc/a.vh", '`include "b.vh"\n`define A 1\n')
    project.write("inc/b.vh", "`define B 1\n")
    project.write("rtl/top.sv", '`include "a.vh"\nmodule top(input clk, input rst_n); endmodule\n')
    project.design["include_dirs"] = ["inc"]
    res = project.run()
    assert [(s["path"], s["role"]) for s in res.sources] == [
        ("inc/a.vh", "include"), ("inc/b.vh", "include"), ("rtl/top.sv", "source"),
    ]


def test_include_follows_defines(project):
    project.write("inc/on.vh", "")
    project.write("inc/off.vh", "")
    project.write("rtl/top.sv", '`ifdef FAST\n`include "on.vh"\n`else\n`include "off.vh"\n`endif\n'
                                "module top; endmodule\n")
    project.design.update(include_dirs=["inc"], defines={"FAST": None})
    inc = [s["path"] for s in project.run().sources if s["role"] == "include"]
    assert inc == ["inc/on.vh"]


def test_missing_include_is_error(project):
    project.write("rtl/top.sv", '`include "nope.vh"\nmodule top; endmodule\n')
    with pytest.raises(StageError) as e:
        project.run()
    assert codes(e) == ["N0_INCLUDE_MISSING"]
    assert "top.sv:1" in e.value.issues[0].where


def test_include_outside_root_is_error(project, tmp_path):
    (tmp_path / "ext").mkdir()
    (tmp_path / "ext" / "x.vh").write_text("")
    project.write("rtl/top.sv", f'`include "{tmp_path / "ext" / "x.vh"}"\nmodule top; endmodule\n')
    with pytest.raises(StageError) as e:
        project.run()
    assert codes(e) == ["N0_INCLUDE_ESCAPES_ROOT"]


# ---- step 5: hashes ------------------------------------------------------------------------

def _hashes(project, **kw):
    return project.run(write=False, **kw).hashes


def test_header_change_changes_source_hash(project):
    project.write("inc/a.vh", "`define A 1\n")
    project.write("rtl/top.sv", '`include "a.vh"\nmodule top; endmodule\n')
    project.design["include_dirs"] = ["inc"]
    h1 = _hashes(project)
    project.write("inc/a.vh", "`define A 2\n")
    h2 = _hashes(project)
    assert {k for k in h1 if h1[k] != h2[k]} == {"source_hash", "build_hash", "run_hash"}


def test_crlf_does_not_change_hash(project):
    h1 = _hashes(project)
    project.write("rtl/top.sv", b"module top(input clk, input rst_n); endmodule\r\n")
    assert _hashes(project) == h1


def test_status_and_unresolved_not_hashed(project):
    h1 = _hashes(project)
    project.cfg["unresolved"] = []
    project.cfg["status"] = "confirmed"
    project.design["name"] = "renamed"
    assert _hashes(project) == h1


def test_moving_project_keeps_hashes(project, tmp_path):
    h1 = _hashes(project)
    project.root.rename(tmp_path / "moved")
    project.root = tmp_path / "moved"
    assert _hashes(project) == h1


@pytest.mark.parametrize("mutate, changed", [
    (lambda c: c["design"].update(top="other"), {"build_hash", "run_hash"}),
    (lambda c: c["design"].update(defines={"X": 1}), {"build_hash", "run_hash"}),
    (lambda c: c["design"].update(parameters={"W": 4}), {"build_hash", "run_hash"}),
    (lambda c: c["design"].update(language="sv2012"), {"build_hash", "run_hash"}),
    (lambda c: c["design"].update(blackbox=["sram"]), {"build_hash", "run_hash"}),
    (lambda c: c.update(assumes=[{"name": "a", "expr": "1"}]), {"build_hash", "run_hash"}),
    (lambda c: c["clocks"][0].update(edge="negedge"), {"env_hash", "run_hash"}),
    (lambda c: c["resets"][0].update(type="sync"), {"env_hash", "run_hash"}),
    (lambda c: c.update(run={"seed": 2}), {"run_hash"}),
    (lambda c: c.update(run={"params": {"k": 3}}), {"run_hash"}),
    (lambda c: c.update(llm={"mode": "cloud", "provider": "p", "model": "m"}), {"run_hash"}),
    (lambda c: c.update(llm={"mode": "off", "model": "stale"}), set()),
    (lambda c: c.update(waveforms=[]), set()),
])
def test_hash_layers(project, mutate, changed):
    h1 = _hashes(project)
    mutate(project.cfg)
    h2 = _hashes(project)
    assert {k for k in h1 if h1[k] != h2[k]} == changed


def test_compile_order_is_hashed(project):
    project.write("rtl/a.sv", "module a; endmodule\n")
    project.design["files"] = ["rtl/a.sv", "rtl/top.sv"]
    h1 = _hashes(project)
    project.design["files"] = ["rtl/top.sv", "rtl/a.sv"]
    h2 = _hashes(project)
    assert h1["source_hash"] == h2["source_hash"] and h1["build_hash"] != h2["build_hash"]


def test_tool_version_and_docker_digest(project):
    h1 = _hashes(project, tools={"pyslang": "1"})
    h2 = _hashes(project, tools={"pyslang": "2"})
    assert h1["env_hash"] != h2["env_hash"] and h1["build_hash"] == h2["build_hash"]
    project.cfg["env"] = {"docker_digest": "sha256:" + "a" * 64}
    assert _hashes(project, tools={"pyslang": "1"}) == _hashes(project, tools={"pyslang": "2"})


def test_known_hash_value(project):
    # Pins the hash encoding. If this changes on purpose, every stored revision id changes too.
    content = b"module top(input clk, input rst_n); endmodule\n"
    payload = '[["rtl/top.sv","%s"]]' % hashlib.sha256(content).hexdigest()
    expected = hashlib.sha256(b"verimine/source/v1\0" + payload.encode()).hexdigest()
    assert _hashes(project)["source_hash"] == expected


# ---- steps 6-8: tools, snapshot, database --------------------------------------------------

def test_snapshot_and_db(project):
    res = project.run()
    ws = project.root / ".verimine"
    for s in res.sources:
        obj = ws / "store" / "objects" / s["sha256"][:2] / s["sha256"][2:]
        assert obj.read_bytes() == (project.root / s["path"]).read_bytes()
    with sqlite3.connect(ws / "verimine.db") as con:
        assert con.execute("SELECT id, top FROM design").fetchall() == [("top", "top")]
        assert con.execute("SELECT id, source_hash FROM revision").fetchall() == \
            [(res.hashes["build_hash"], res.hashes["source_hash"])]
        assert con.execute("SELECT id, revision_id, env_hash, stage FROM run").fetchall() == \
            [(res.hashes["run_hash"], res.hashes["build_hash"], res.hashes["env_hash"], "N0")]
        assert con.execute("SELECT path, role FROM revision_file").fetchall() == [("rtl/top.sv", "source")]
    assert res.db["new_revision"] and res.db["new_run"]
    out = json.loads((ws / "runs" / res.hashes["run_hash"] / "n0.json").read_text())
    assert out["hashes"] == res.hashes


def test_rerun_is_idempotent_and_new_seed_adds_run_only(project):
    project.run()
    again = project.run()
    assert not again.db["new_revision"] and not again.db["new_run"]
    project.cfg["run"] = {"seed": 7}
    third = project.run()
    assert not third.db["new_revision"] and third.db["new_run"]
    with sqlite3.connect(project.root / ".verimine" / "verimine.db") as con:
        assert con.execute("SELECT count(*) FROM run").fetchone() == (2,)
        assert con.execute("SELECT count(*) FROM revision").fetchone() == (1,)


def test_real_tools_recorded(project):
    res = project.run(tools=None, write=False)
    assert {"verimine", "python", "pyslang"} <= set(res.tools)


def test_output_deterministic(project):
    a = project.run(write=False).to_dict()
    b = project.run(write=False).to_dict()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---- schema and CLI ------------------------------------------------------------------------

def test_committed_schema_is_current():
    committed = Path(__file__).parents[1] / "schemas" / "verimine-config.schema.json"
    assert committed.read_text(encoding="utf-8") == json_schema(), "run: verimine schema --out " + str(committed)


def test_cli_exit_codes(project):
    cfg = project.yaml()
    run = lambda: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "verimine.cli", "n0", str(cfg), "--dry-run"], capture_output=True, text=True)
    assert run().returncode == 0
    project.cfg["status"] = "draft"
    project.yaml()
    r = run()
    assert r.returncode == EXIT_NOT_CONFIRMED and "N0_NOT_CONFIRMED" in r.stderr
