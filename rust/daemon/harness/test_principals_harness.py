"""The principal-store golden harness reads its fixture DDL and operator
routes from the daemon's own sources, writes invites with its SQL fixture
and compares every step with the committed golden, without the frozen
Python package. These tests need no database or built binary."""
import importlib
import json
from pathlib import Path
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
    'const OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", "/api/daemon-notice", OTHER];',
    'const OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", "/ui/next"];',
    'const OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", "/api/\\"x"];',
    'static OPERATOR_POST_PATHS: [&str; 2] = ["/api/config", "/api/daemon-notice"];',
    "",
    DECLARATION + "\n" + DECLARATION,
])
def test_operator_routes_refuse_a_changed_declaration(declaration):
    with pytest.raises(AssertionError, match="daemon operator route"):
        principals.operator_post_paths(HTTP.replace(DECLARATION, declaration))


AUTH = principals.AUTH_SOURCE.read_text(encoding="utf-8")
POLICY = 'matches!(path, "/api/config" | "/api/daemon-notice")'
ADDED = "/api/facts/set"


def test_operator_routes_accept_a_new_route_in_both_daemon_lists_even_wrapped():
    added = ('const OPERATOR_POST_PATHS: [&str; 3] = [\n    "/api/config",\n    "/api/daemon-notice",\n'
             f'    "{ADDED}",\n];')
    policy = f'matches!(path, "/api/config" | "/api/daemon-notice" | "{ADDED}")'
    assert AUTH.count(POLICY) == 1
    assert principals.operator_post_paths(HTTP.replace(DECLARATION, added), AUTH.replace(POLICY, policy)) == \
        frozenset({"/api/config", "/api/daemon-notice", ADDED})


@pytest.mark.parametrize("policy", [
    f'matches!(path, "/api/config" | "/api/daemon-notice" | "{ADDED}")',
    'matches!(path, "/api/config")',
    'matches!(path, "/api/config" | OTHER)',
])
def test_operator_routes_refuse_a_policy_that_names_other_routes(policy):
    with pytest.raises(AssertionError, match="name different routes"):
        principals.operator_post_paths(HTTP, AUTH.replace(POLICY, policy))


def test_pairing_alphabet_is_the_daemons_and_refuses_a_changed_declaration():
    source = principals.PAIR_SOURCE.read_text(encoding="utf-8")
    assert principals.PAIRING_CODE_ALPHABET == "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    declaration = 'const PAIRING_CODE_ALPHABET: &str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";'
    assert source.count(declaration) == 1
    assert principals.pairing_code_alphabet(source.replace(declaration, declaration.replace("XYZ", "XY"))) \
        == "0123456789ABCDEFGHJKMNPQRSTVWXY"
    for changed in (declaration.replace("XYZ", "XYZZ"), declaration.replace("&str", "&[u8; 32]"), ""):
        with pytest.raises(AssertionError, match="PAIRING_CODE_ALPHABET"):
            principals.pairing_code_alphabet(source.replace(declaration, changed))


def test_golden_inspections_ask_exactly_the_current_operator_requests():
    records = json.loads(principals.GOLDEN.read_text(encoding="utf-8"))["records"]
    asked = [[{"method": p["method"], "path": p["path"]} for p in resolved["operator_policy"]]
             for record in records if isinstance(record["output"], dict)
             for resolved in record["output"].get("resolved", [])]
    assert len(asked) > 40
    assert all(requests == principals.OPERATOR_REQUESTS for requests in asked)


def test_golden_steps_are_the_scenario_steps_in_order():
    labels = []

    class Recorder(principals.Replay):
        """The harness's own invite, adversarial, secret and code helpers;
        only the step that would reach the probe and the bank is replaced."""

        def step(self, label, *_args, **_kwargs):
            labels.append(label)

    recorder = Recorder.__new__(Recorder)
    recorder.codes, recorder.raw_secrets, recorder.rng = {}, [], principals.random.Random(53)
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
    # Inside a list a value compares by type too: true is not 1.
    listed = {**RECORD, "output": {"resolved": [{"allowed": True}]}}
    assert principals.step_diffs(listed, {**listed, "output": {"resolved": [{"allowed": 1}]}})


def test_the_race_must_prove_two_queued_callers_and_one_winner():
    proof = {"case": principals.RACE_CASE, "output": dict(principals.RACE_PROOF), "state": None}
    assert principals.run_failures([RECORD, proof]) == []
    for broken in ({**proof["output"], "both_queued": False}, {**proof["output"], "winners": 2}):
        assert principals.run_failures([RECORD, {**proof, "output": broken}])
    assert principals.run_failures([RECORD])
    assert principals.run_failures([proof, proof])


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


def recorded_golden(tmp_path, monkeypatch, records):
    golden = tmp_path / "principals.json"
    golden.write_text(json.dumps({"normalizers": "label", "records": records}, indent=1) + "\n", encoding="utf-8")
    monkeypatch.setattr(principals, "GOLDEN", golden)
    monkeypatch.setattr(principals.candidate_record, "provenance", lambda _binary: {"commit": "x"})
    return golden


def test_record_candidate_dry_run_exits_1_and_write_keeps_unchanged_records(tmp_path, monkeypatch):
    # Key order differs from the candidate's: an unchanged record stays as committed.
    first = {"state": {"rows": {}}, "output": {"state": "new"}, "case": "invite-a"}
    committed = [first, {**RECORD, "case": "b"}]
    golden = recorded_golden(tmp_path, monkeypatch, committed)
    before = golden.read_bytes()
    unchanged = [RECORD, {**RECORD, "case": "b"}]
    assert principals.record_candidate(unchanged, True, "bin") == (0, {
        "unchanged": 2, "changed": [], "new": [], "removed": [], "refused": [], "written": False})
    assert golden.read_bytes() == before
    changed = [RECORD, {**RECORD, "case": "b", "output": {"state": "pending"}}, {**RECORD, "case": "c"}]
    code, report = principals.record_candidate(changed, False, "bin")
    assert (code, report["changed"], report["new"], report["written"]) == (1, ["1:b"], ["2:c"], False)
    assert golden.read_bytes() == before
    code, report = principals.record_candidate(changed, True, "bin")
    assert (code, report["written"]) == (0, True)
    written = json.loads(golden.read_text(encoding="utf-8"))
    assert list(written["records"][0]) == ["state", "output", "case"]
    assert written["records"] == [first, *changed[1:]] and written["normalizers"] == "label"
    assert written[principals.candidate_record.PROVENANCE_KEY] == {"principals": {"commit": "x"}}
    code, report = principals.record_candidate(changed[:1], True, "bin")
    assert (code, report["removed"]) == (0, ["1:b", "2:c"])
    assert len(json.loads(golden.read_text(encoding="utf-8"))["records"]) == 1


def test_record_candidate_writes_the_committed_golden_back_unchanged_but_for_the_change(tmp_path, monkeypatch):
    text = principals.GOLDEN.read_text(encoding="utf-8")
    records = json.loads(text)["records"]
    golden = recorded_golden(tmp_path, monkeypatch, [])
    golden.write_text(text, encoding="utf-8", newline="\n")
    candidate = json.loads(json.dumps(records))
    candidate[1]["output"] = {"ok": "changed"}
    assert principals.record_candidate(candidate, True, "bin")[0] == 0
    before, after = text.splitlines(), golden.read_text(encoding="utf-8").splitlines()
    import difflib
    changed = [line for line in difflib.unified_diff(before, after, lineterm="", n=0)
               if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    assert 0 < len(changed) < 12, changed


@pytest.mark.parametrize("refusal", ["home", "failure", "nan"])
def test_record_candidate_refuses_a_failed_check_host_path_or_unwritable_record(refusal, tmp_path, monkeypatch):
    golden = recorded_golden(tmp_path, monkeypatch, [RECORD])
    before = golden.read_bytes()
    records, failures = [RECORD], ()
    if refusal == "home":
        records = [{**RECORD, "output": {"home": str(Path.home())}}]
    elif refusal == "failure":
        failures = ["the race did not prove itself"]
    else:
        records = [{**RECORD, "output": {"value": float("nan")}}]
    try:
        code, report = principals.record_candidate(records, True, "bin", failures)
    except ValueError:  # NaN refused by the JSON round trip itself
        code, report = 1, {"refused": ["unwritable"]}
    assert code == 1 and report["refused"]
    assert golden.read_bytes() == before


def test_committed_golden_holds_its_records_and_at_most_provenance():
    keys = set(json.loads(principals.GOLDEN.read_text(encoding="utf-8")))
    assert {"normalizers", "records"} <= keys <= {"normalizers", "records",
                                                    principals.candidate_record.PROVENANCE_KEY}


@pytest.mark.parametrize("argv,error", [
    (["mutants", "--rust-bin", "x", "--out", "o", "--record-candidate"], "records the golden run"),
    (["golden", "--rust-bin", "x", "--out", "o", "--write"], "--write needs --record-candidate"),
    (["live", "--rust-bin", "x", "--out", "o"], "invalid choice"),
    (["oracle", "--out", "o"], "invalid choice"),
    (["golden", "--out", "o"], "--rust-bin"),
    (["golden", "--rust-bin", "x", "--out", str(principals.GOLDEN)], "--out names the committed golden"),
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
