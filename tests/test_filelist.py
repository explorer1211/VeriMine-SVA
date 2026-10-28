from pathlib import Path

from verimine.issues import IssueLog
from verimine.n0.filelist import parse_filelist


def _parse(root: Path, text: str, env=None, name="a.f"):
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text)
    log = IssueLog()
    return parse_filelist(root / name, root, log, env=env or {}), log


def test_basic_options(tmp_path):
    fl, log = _parse(tmp_path, """
        // line comment
        +incdir+inc+inc2   # trailing comment
        +define+A=1+B
        /* block
           comment */
        -v lib/cells.v
        -y lib/dir +libext+.v+.sv
        "dir with space/x.sv"
        top.sv
    """)
    assert not log.errors and not log.warnings
    assert [p.name for p, _ in fl.files] == ["x.sv", "top.sv"]
    assert [p.name for p, _ in fl.include_dirs] == ["inc", "inc2"]
    assert [(n, v) for n, v, _ in fl.defines] == [("A", "1"), ("B", None)]
    assert [p.name for p, _ in fl.library_files] == ["cells.v"]
    assert [p.name for p, _ in fl.library_dirs] == ["dir"]
    assert fl.libext == [".v", ".sv"]
    # origins point back to file:line for error messages
    assert fl.files[1][1].endswith("a.f:10")


def test_nested_f_relative_to_root_and_F_relative_to_self(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "lo.f").write_text("x.sv\n")
    (tmp_path / "sub" / "self.f").write_text("y.sv\n")
    fl, log = _parse(tmp_path, "-f sub/lo.f\n-F sub/self.f\n")
    assert not log.errors
    assert [p for p, _ in fl.files] == [tmp_path / "x.sv", tmp_path / "sub" / "y.sv"]


def test_env_expansion_all_forms(tmp_path):
    fl, log = _parse(tmp_path, "$A/x.sv\n${A}/y.sv\n$(A)/z.sv\n", env={"A": "rtl"})
    assert not log.errors
    assert [p.relative_to(tmp_path).as_posix() for p, _ in fl.files] == ["rtl/x.sv", "rtl/y.sv", "rtl/z.sv"]


def test_undefined_env_is_error(tmp_path):
    _, log = _parse(tmp_path, "${NOPE}/x.sv\n")
    assert [e.code for e in log.errors] == ["N0_FILELIST_ENV"]


def test_cycle_detected(tmp_path):
    (tmp_path / "b.f").write_text("-f a.f\n")
    _, log = _parse(tmp_path, "-f b.f\n")
    assert [e.code for e in log.errors] == ["N0_FILELIST_CYCLE"]


def test_missing_nested_f(tmp_path):
    _, log = _parse(tmp_path, "-f nope.f\n")
    assert [e.code for e in log.errors] == ["N0_FILE_MISSING"]


def test_unknown_option_warns(tmp_path):
    fl, log = _parse(tmp_path, "-sv\n+notimingchecks\nx.sv\n")
    assert not log.errors
    assert len(log.warnings) == 2
    assert len(fl.files) == 1


def test_option_missing_argument(tmp_path):
    _, log = _parse(tmp_path, "-v\n")
    assert [e.code for e in log.errors] == ["N0_FILELIST_SYNTAX"]
