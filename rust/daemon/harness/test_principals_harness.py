"""The principal-store golden harness reads its fixture DDL and operator
routes from the daemon's own sources, writes invites with its SQL fixture
and compares every step with the committed golden, without the frozen
Python package. These tests need no database or built binary."""
import importlib
import json
import sys
from unittest.mock import MagicMock

import pytest

from rust.harness_support.import_blocker import cold_harness_imports

import principals


HTTP = principals.HTTP_SOURCE.read_text(encoding="utf-8")
DECLARATION = 'const OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", "/api/daemon-notice"];'


def test_harness_imports_without_the_product_package():
    original = sys.modules.get("principals")
    with cold_harness_imports():
        importlib.import_module("principals")
        with cold_harness_imports():
            assert "principals" not in sys.modules
            loaded = importlib.import_module("principals")
            assert loaded.OPERATOR_POST_PATHS == frozenset({"/api/config", "/api/daemon-notice"})
    assert sys.modules.get("principals") is original


def test_fixture_ddl_is_the_daemons_own_statements():
    assert principals.META_TABLE_SQL == (
        "CREATE TABLE IF NOT EXISTS meta (\n  key TEXT PRIMARY KEY,\n  value JSONB NOT NULL\n);")
    ddl = principals.PRINCIPALS_TABLE_SQL
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS principals (\n    principal        TEXT PRIMARY KEY,")
    assert "    owned            BOOLEAN NOT NULL DEFAULT TRUE\n);\nDO $$" in ddl
    assert ddl.endswith("END $$;")
    assert ddl in principals.SCHEMA_SOURCE.read_text(encoding="utf-8")
    assert "maintainer_passkeys" not in ddl


@pytest.mark.parametrize("source,error", [
    ("-- nothing\n", "no single"),
    ("CREATE TABLE IF NOT EXISTS meta (a);\nCREATE TABLE IF NOT EXISTS meta (b);\n", "no single"),
    ("CREATE TABLE IF NOT EXISTS meta (a)\n", "does not end"),
])
def test_schema_statement_refuses_a_missing_repeated_or_open_statement(source, error):
    with pytest.raises(AssertionError, match=error):
        principals.schema_statement("CREATE TABLE IF NOT EXISTS meta (", ");", source)


def test_schema_statement_stops_at_the_first_end():
    source = "x;\nCREATE TABLE IF NOT EXISTS meta (a);\nCREATE TABLE b ();\n"
    assert principals.schema_statement("CREATE TABLE IF NOT EXISTS meta (", ");", source) == \
        "CREATE TABLE IF NOT EXISTS meta (a);"


def test_operator_routes_are_the_daemons_declaration():
    assert HTTP.count(DECLARATION) == 1
    assert principals.operator_post_paths() == frozenset({"/api/config", "/api/daemon-notice"})
    assert principals.OPERATOR_REQUESTS == [
        {"method": method, "path": path}
        for path in ("/api/config", "/api/daemon-notice", "/api/stats") for method in ("GET", "POST")]


@pytest.mark.parametrize("declaration", [
    'const OPERATOR_POST_PATHS: [&str; 3] = ["/api/config", "/api/daemon-notice"];',
    'const OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", "/api/config"];',
    'const OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", OTHER];',
    'const OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", "/ui/next"];',
    'const OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", "/api/\\"x"];',
    'static OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", "/api/daemon-notice"];',
    "",
    DECLARATION + "\n" + DECLARATION,
])
def test_operator_routes_refuse_a_changed_declaration(declaration):
    with pytest.raises(AssertionError, match="daemon operator route"):
        principals.operator_post_paths(HTTP.replace(DECLARATION, declaration))


def test_operator_routes_accept_a_new_route_the_golden_would_then_report():
    added = 'const OPERATOR_POST_PATHS: [&str; 3] = ["/api/config", "/api/daemon-notice", "/api/x"];'
    assert principals.operator_post_paths(HTTP.replace(DECLARATION, added)) == \
        frozenset({"/api/config", "/api/daemon-notice", "/api/x"})


def test_golden_inspections_ask_exactly_the_current_operator_requests():
    records = json.loads(principals.GOLDEN.read_text(encoding="utf-8"))["records"]
    asked = [[{"method": p["method"], "path": p["path"]} for p in resolved["operator_policy"]]
             for record in records if isinstance(record["output"], dict)
             for resolved in record["output"].get("resolved", [])]
    assert len(asked) > 40
    assert all(requests == principals.OPERATOR_REQUESTS for requests in asked)


def test_golden_steps_are_the_scenario_steps_in_order():
    labels = []

    class Recorder:
        def step(self, label, *_args, **_kwargs):
            labels.append(label)

        def invite(self, name, tier=None, board=True, replace=False, ttl=900, adversarial=False,
                   owned=principals.KEEP):
            self.codes[name] = "hash-" + name
            labels.append(("adversarial-invite-" if adversarial else "invite-") + name +
                          ("-replace" if replace else ""))

        def adversarial(self, label, *_args, **_kwargs):
            labels.append("adversarial-" + label)

        def secret(self, label):
            return "secret-" + label

    recorder = Recorder()
    recorder.codes, recorder.raw_secrets = {}, []
    principals.scenarios(recorder)
    records = json.loads(principals.GOLDEN.read_text(encoding="utf-8"))["records"]
    assert labels == [record["case"] for record in records]


RECORD = {"case": "invite-a", "output": {"state": "new"}, "state": {"rows": {}}}


def test_step_comparison_reports_label_output_state_and_length():
    assert principals.step_diffs(RECORD, json.loads(json.dumps(RECORD))) == []
    assert principals.step_diffs(RECORD, {**RECORD, "case": "invite-b"}) == \
        ["/case: golden and candidate steps differ"]
    assert principals.step_diffs(RECORD, {**RECORD, "output": {"state": "pending"}})
    assert principals.step_diffs(RECORD, {**RECORD, "state": {"rows": {"x": 1}}})[0].startswith("/database")
    assert principals.step_diffs(RECORD, {**RECORD, "state": None})
    assert principals.step_diffs(None, RECORD) == ["/: only in the candidate"]


def test_a_golden_record_past_the_last_step_is_a_difference():
    replay = principals.Replay.__new__(principals.Replay)
    replay.records, replay.differences = [RECORD], []
    replay.golden = [RECORD, {**RECORD, "case": "extra"}]
    replay.finish()
    assert replay.differences == [{"case": "extra", "diffs": ["/: only in the golden"]}]


def connection(existing, returned=(1000.0, None, True, True)):
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [existing, returned]
    return conn


def test_invite_fixture_inserts_a_new_name_with_ownership_only_when_set():
    conn = connection(None)
    assert principals.create_invite(conn, "a", "h", tier=None, board=True, ttl_seconds=900, replace=False) == \
        {"state": "new", "tier": None, "board": True, "owned": True}
    insert, params = conn.execute.call_args_list[1].args
    assert insert.startswith("INSERT INTO public.principals AS p (principal, tier, board, code_hash,")
    assert params == {"name": "a", "code": "h", "ttl": 900.0, "tier": None, "board": True, "owned": None}
    conn = connection(None, (1000.0, None, True, False))
    assert principals.create_invite(conn, "g", "h", tier=None, board=True, ttl_seconds=900, replace=False,
                                    owned=False)["owned"] is False
    assert "(principal, tier, board, owned, code_hash," in conn.execute.call_args_list[1].args[0]


def test_invite_fixture_reissues_a_pending_or_replaced_name():
    for existing, replace, state in (((False, False), False, "pending"), ((True, False), True, "paired")):
        conn = connection(existing, (1000.0, "minimal", False, True))
        assert principals.create_invite(conn, "a", "h", tier="minimal", board=False, ttl_seconds=900,
                                        replace=replace) == \
            {"state": state, "tier": "minimal", "board": False, "owned": True}
        update = conn.execute.call_args_list[1].args[0]
        assert "paired_code_hash = NULL, tier = %(tier)s, board = %(board)s WHERE" in update
        assert "owned =" not in update and "revoked_at" not in update


@pytest.mark.parametrize("existing,replace", [((False, True), False), ((True, True), True), ((True, False), False)])
def test_invite_fixture_refuses_states_the_scenario_never_reaches(existing, replace):
    conn = connection(existing)
    with pytest.raises(AssertionError, match="does not model"):
        principals.create_invite(conn, "a", "h", tier=None, board=True, ttl_seconds=900, replace=replace)
    assert conn.execute.call_count == 1


def test_record_candidate_classes_steps_and_writes_only_with_write(tmp_path, monkeypatch):
    golden = tmp_path / "principals.json"
    monkeypatch.setattr(principals, "GOLDEN", golden)
    monkeypatch.setattr(principals.candidate_record, "provenance", lambda _binary: {"commit": "x"})
    committed = [RECORD, {**RECORD, "case": "b"}, {**RECORD, "case": "gone"}]
    result = {"records": [RECORD, {**RECORD, "case": "b", "output": {"state": "pending"}}]}
    report = principals.record_candidate(result, committed, False, None)
    assert report == {"unchanged": 1, "changed": ["1:b"], "new": [], "removed": ["2:gone"], "written": False}
    assert not golden.exists()
    report = principals.record_candidate({"records": [*result["records"], {**RECORD, "case": "c"}]},
                                         committed[:2], True, None)
    assert (report["changed"], report["new"], report["written"]) == (["1:b"], ["2:c"], True)
    written = json.loads(golden.read_text(encoding="utf-8"))
    assert written["records"][2]["case"] == "c"
    assert written["normalizers"] == principals.NORMALIZERS
    assert written[principals.candidate_record.PROVENANCE_KEY] == {"commit": "x"}
    golden.unlink()
    assert principals.record_candidate({"records": [RECORD]}, [RECORD], True, None)["written"] is False
    assert not golden.exists()


def test_committed_golden_keeps_its_normalizer_label():
    assert json.loads(principals.GOLDEN.read_text(encoding="utf-8"))["normalizers"] == principals.NORMALIZERS


@pytest.mark.parametrize("argv,error", [
    (["mutants", "--rust-bin", "x", "--out", "o", "--record-candidate"], "records the golden run"),
    (["golden", "--rust-bin", "x", "--out", "o", "--write"], "--write needs --record-candidate"),
    (["live", "--rust-bin", "x", "--out", "o"], "invalid choice"),
    (["oracle", "--out", "o"], "invalid choice"),
    (["golden", "--out", "o"], "--rust-bin"),
])
def test_retired_and_inconsistent_modes_are_refused(argv, error, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["principals.py", *argv])
    with pytest.raises(SystemExit) as exit_info:
        principals.main()
    assert exit_info.value.code == 2
    assert error in capsys.readouterr().err


def test_record_candidate_refuses_a_selected_mutant(monkeypatch, capsys):
    monkeypatch.setenv("PSEUDOLIFE_DAEMON_MUTANT", "principal-shadow")
    monkeypatch.setattr(sys, "argv", ["principals.py", "golden", "--rust-bin", "x", "--out", "o",
                                      "--record-candidate"])
    with pytest.raises(SystemExit):
        principals.main()
    assert "PSEUDOLIFE_DAEMON_MUTANT=principal-shadow is set" in capsys.readouterr().err
