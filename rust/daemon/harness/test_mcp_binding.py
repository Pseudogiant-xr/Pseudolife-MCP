"""The binding table and sweep: currency, coverage and comparison strictness.

Run with the pinned pydantic (2.14.0 / core 2.50.0) the generator asserts.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import mcp_binding_sweep as sweep  # noqa: E402
import mcp_binding_table as table  # noqa: E402

SRC = HERE.parent / "src" / "mcp"


def test_committed_table_matches_the_registered_tools():
    assert table.TARGET.read_text(encoding="utf-8") == table.render(), (
        "run python rust/daemon/harness/mcp_binding_table.py")


def test_every_required_baseline_binds_in_python():
    # mcp_compare's stub calls and the sweep's cases start from these.
    from pseudolife_memory import mcp_server
    tools = mcp_server.mcp._tool_manager._tools
    tables = json.loads(table.TARGET.read_text(encoding="utf-8"))["tools"]
    for name, entry in tables.items():
        args = {p["name"]: sweep.baseline(p["schema"])
                for p in entry["params"] if "default" not in p}
        assert "bound" in sweep.python_bind(tools[name].fn_metadata, json.dumps(args)), name


def test_corpus_varies_every_parameter_of_every_tool():
    tables = json.loads(table.TARGET.read_text(encoding="utf-8"))["tools"]
    for name, entry in tables.items():
        texts = [json.loads(t, parse_int=str) for t in sweep.cases_for(name, entry)]
        for param in entry["params"]:
            assert sum(param["name"] in t for t in texts) > 50, (name, param["name"])


def test_comparison_is_type_strict():
    assert not sweep.same(1, True)
    assert not sweep.same(1, 1.0)
    assert sweep.same(float("nan"), float("nan"))
    assert not sweep.same({"a": 1, "b": 2}, {"b": 2, "a": 1})
    assert sweep.same([1.5, "x"], [1.5, "x"])


def test_every_binder_mutant_is_swept():
    names = set()
    for path in SRC.glob("*.rs"):
        names |= set(re.findall(r'mutants::active\("(mcp-bind-[a-z0-9-]+)"\)',
                                path.read_text(encoding="utf-8")))
    # The smart-union strict pass is covered by a binding.rs unit test.
    swept = {m for m in sweep.MUTANTS if m.startswith("mcp-bind-")}
    # The year-one timestamp rule is Linux-only behaviour (Windows raises
    # OSError for naive instants before 1970); an as_of.rs unit test covers it.
    assert names - swept == {"mcp-bind-union-lax-first", "mcp-bind-asof-year-one"}
    assert swept <= names
