"""Lane approvals must not rewrite a shared generated baseline."""
import copy
import json

import pytest

import ci_budget


def workflow():
    return {"jobs": {"test": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo original"}]}}}


def test_accept_writes_a_fragment_without_changing_numeric_or_generated_data(tmp_path, monkeypatch):
    before = workflow()
    after = copy.deepcopy(before)
    after["jobs"]["test"]["steps"].append({"run": "echo diagnostic"})
    budget = tmp_path / "ci_budget.json"
    budget.write_text(json.dumps({"ci.yml": {"jobs": {"test": {"minutes": 1.0}},
                                           "limits": {"jobs": 1, "runner_minutes": 2}}}))
    baseline = tmp_path / "ci_budget_fingerprints.json"
    baseline.write_text(json.dumps({"schema": 1, "workflows": {"ci.yml": ci_budget.fingerprints(before)}}))
    budget_bytes, baseline_bytes = budget.read_bytes(), baseline.read_bytes()
    monkeypatch.setattr(ci_budget, "BUDGET", budget)
    monkeypatch.setattr(ci_budget, "load", lambda name, resolved=True: copy.deepcopy(after))
    assert ci_budget.main(["accept", "--workflow", "ci.yml", "--job", "test", "--reason", "prints one line"]) == 0
    assert budget.read_bytes() == budget_bytes
    assert baseline.read_bytes() == baseline_bytes
    paths = list((tmp_path / "ci_budget_accept").glob("*.json"))
    assert len(paths) == 1
    record = json.loads(paths[0].read_text())
    assert record["before"] == ci_budget.fingerprints(before)["test"]
    assert record["fingerprint"] == ci_budget.fingerprints(after)["test"]
    assert record["reason"] == "prints one line"


def test_raw_parity_definitions_bind_the_helper_as_data():
    raw = workflow()
    raw["jobs"]["test"]["strategy"] = {"matrix": {"suite": "${{ fromJSON(needs.scope.outputs.parity).suite }}"}}
    first = ci_budget.definition_fingerprints("rust.yml", raw, "raise RuntimeError('never execute')")
    second = ci_budget.definition_fingerprints("rust.yml", raw, "raise RuntimeError('different data')")
    assert first["test"] != second["test"]


@pytest.mark.parametrize("value", ["needs.scope.outputs.parity", "${{ format('needs.scope.outputs.parity') }}",
                                  "${{ fromJSON(other.needs.scope.outputs.parity).suite }}",
                                  "${{ format('}}') }}-${{ format('needs.scope.outputs.parity') }}"])
def test_literal_or_unrelated_matrix_text_is_not_a_scope_dependency(value):
    raw = workflow()
    raw["jobs"]["test"]["strategy"] = {"matrix": {"suite": value}}
    assert ci_budget.definition_fingerprints("rust.yml", raw, "first") == ci_budget.definition_fingerprints("rust.yml", raw, "second")


@pytest.mark.parametrize("value", ["${{ fromJSON(needs['scope'].outputs['parity']).suite }}",
                                  "${{ fromJSON(needs[inputs.job].outputs.parity).suite }}"])
def test_actual_or_unresolved_scope_accesses_are_bound(value):
    raw = workflow()
    raw["jobs"]["test"]["strategy"] = {"matrix": {"suite": value}}
    assert ci_budget.definition_fingerprints("rust.yml", raw, "first") != ci_budget.definition_fingerprints("rust.yml", raw, "second")


def test_budget_data_merge_unions_metadata_but_refuses_conflicting_costs():
    base = {"ci.yml": {"jobs": {"test": {"minutes": 1}}, "fingerprints": {"test": "base"}, "accepted": []}}
    left, right = copy.deepcopy(base), copy.deepcopy(base)
    left["ci.yml"]["fingerprints"]["test"] = "left"
    right["ci.yml"]["fingerprints"]["test"] = "right"
    left["ci.yml"]["accepted"].append({"reason": "left"})
    right["ci.yml"]["accepted"].append({"reason": "right"})
    assert ci_budget.merge_budget_data(base, left, right) == {"ci.yml": {"jobs": {"test": {"minutes": 1}}}}
    left["ci.yml"]["jobs"]["test"]["minutes"] = 2
    right["ci.yml"]["jobs"]["test"]["minutes"] = 3
    with pytest.raises(ValueError, match="semantic"):
        ci_budget.merge_budget_data(base, left, right)


def test_removed_job_has_an_explicit_reasoned_tombstone(tmp_path, monkeypatch):
    budget = tmp_path / "ci_budget.json"
    budget.write_text(json.dumps({"ci.yml": {"jobs": {}}}))
    original = ci_budget.fingerprints(workflow())
    (tmp_path / "ci_budget_fingerprints.json").write_text(json.dumps({"schema": 1, "workflows": {"ci.yml": original}}))
    monkeypatch.setattr(ci_budget, "BUDGET", budget)
    monkeypatch.setattr(ci_budget, "load", lambda name, resolved=True: {"jobs": {}})
    assert ci_budget.main(["accept", "--workflow", "ci.yml", "--job", "test", "--reason", "cases moved to another lane"]) == 0
    records, errors = ci_budget.read_acceptances()
    assert not errors and records[0]["fingerprint"] is None and records[0]["before"] == original["test"]


def test_a_missing_scope_helper_is_not_mistaken_for_job_retirement(tmp_path, monkeypatch):
    budget = tmp_path / "ci_budget.json"
    budget.write_text(json.dumps({"rust.yml": {"jobs": {}}}))
    (tmp_path / "ci_budget_fingerprints.json").write_text(json.dumps({"schema": 1, "workflows": {"rust.yml": {"test": "a" * 16}}}))
    monkeypatch.setattr(ci_budget, "BUDGET", budget)
    monkeypatch.setattr(ci_budget, "ROOT", tmp_path)
    monkeypatch.setattr(ci_budget, "load", lambda name, resolved=True: workflow())
    with pytest.raises(SystemExit):
        ci_budget.main(["accept", "--workflow", "rust.yml", "--job", "test", "--reason", "same cost"])
    assert not ci_budget.acceptance_directory().exists()
