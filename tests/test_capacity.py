"""Tests for the capacity test: the design generator, one small tier end to end, the capability
statement and the write-out."""
from __future__ import annotations

from verimine import capacity


def test_generate_has_a_top_and_the_counter_invariant():
    text = capacity.generate(500)
    assert text.count("module blk_") == 500 // capacity.BLOCK_LINES
    assert "module cap_top" in text and "assert(!$past(rst_n)" in text
    assert len(text.splitlines()) > 400


def test_one_tier():
    res = capacity.run([1], timeout=60, workspace=None, budget=120)
    row = res["tiers"][0]
    assert row["tier"] == 1 and row["lines"] > 300 and row["bytes"] > 0
    assert row["slang"]["ok"] and row["yosys"]["ok"]
    assert row["l0"]["max_depth"] is not None
    assert res["capability"]["text"]
    md = capacity.markdown(res)
    assert "能力声明" in md and "档" in md


def test_statement_without_any_pass():
    res = {"tiers": [{"lines": 500, "yosys": {"ok": False}, "slang": {"ok": True}, "l1": {"ok": False}}]}
    assert capacity.statement(res)["lines"] is None
