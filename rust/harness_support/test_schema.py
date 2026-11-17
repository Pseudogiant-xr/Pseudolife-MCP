"""Current-schema fixture construction must not load the frozen package."""
import builtins
import importlib
import sys
import json
from pathlib import Path

import pytest


def test_current_schema_helpers_without_package(monkeypatch):
    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "pseudolife_memory" or name.startswith("pseudolife_memory."):
            raise AssertionError("frozen package import: " + name)
        return original(name, *args, **kwargs)

    for name in list(sys.modules):
        if name == "pseudolife_memory" or name.startswith("pseudolife_memory."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "daemon" / "harness"))
    from rust.cli_harness.rows import _bank
    monkeypatch.setattr(_bank, "drop", lambda name: None)
    _bank.teardown(_bank.name("pl_cf_no_package"))
    for name in ("schema_cases",):
        monkeypatch.delitem(sys.modules, name, raising=False)
        importlib.import_module(name)


def test_schema_executor_is_package_independent():
    from rust.harness_support import schema
    assert callable(schema.ensure_schema)


@pytest.mark.parametrize("last,row,value", [(None, False, False), ((None,), True, False),
                                         ((False,), True, True), ((0,), True, True), (("",), True, True)])
def test_predicates_distinguish_absent_null_and_false_values(last, row, value):
    from rust.harness_support.schema import _test
    assert _test({"last": "row"}, last, {}) is row
    assert _test({"last": "value"}, last, {}) is value
    assert _test({"not": {"variable": "prior"}}, last, {"prior": row}) is (not row)


@pytest.mark.parametrize("predicate", [{"last": "other"}, {"constant": True}, {"last": "row", "not": {}}])
def test_unknown_predicates_fail(predicate):
    from rust.harness_support.schema import _test
    with pytest.raises(ValueError, match="predicate"):
        _test(predicate, None, {})


def test_unknown_operation_in_unselected_branch_fails_before_connecting(tmp_path, monkeypatch):
    from rust.harness_support import schema
    plan = {"format": 1, "schema_version": 59, "steps": [
        {"op": "if", "test": {"last": "row"}, "then": [{"op": "unimplemented"}], "else": []}]}
    (tmp_path / "schema_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (tmp_path / "schema.sql").write_text("-- schema_version 59\n", encoding="utf-8")
    monkeypatch.setattr(schema, "STORAGE", tmp_path)
    with pytest.raises(ValueError, match="unknown schema operation"):
        schema.ensure_schema(None)


def test_first_row_crosses_command_results_and_preserves_sql_null():
    from rust.harness_support.schema import _first
    from contextlib import nullcontext
    from types import SimpleNamespace

    class Cursor:
        def __init__(self):
            self.result = iter([(True, None), (True, (None,)), (True, ("later",))])
            self.description = None
            self.row = None

        def nextset(self):
            entry = next(self.result, None)
            if entry is None:
                return False
            self.description, self.row = entry
            return True

        def fetchone(self):
            return self.row

    assert _first(SimpleNamespace(execute=lambda sql: nullcontext(Cursor())), "fixture") == (None,)


def test_production_server_refused_before_any_schema_statement(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from rust.harness_support import schema
    from rust.harness_support.database_guard import ProductionDatabaseError

    statements = []

    def execute(sql):
        statements.append(sql)
        return SimpleNamespace(fetchone=lambda: ("pseudolife_memory",))

    monkeypatch.setenv("_PSEUDOLIFE_PRODUCTION_DB", "pseudolife_memory")
    with pytest.raises(ProductionDatabaseError):
        schema.ensure_schema(SimpleNamespace(transaction=nullcontext, execute=execute))
    assert statements == ["SELECT current_database()"]
