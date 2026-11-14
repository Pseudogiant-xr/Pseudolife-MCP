"""The batch may derive CI identities only after member reasons are verified."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ops import integrate as batch
from rust.ci import ci_budget


def setup_reader(monkeypatch, tmp_path, *, legacy=False):
    before = {"jobs": {"test": {"runs-on": "ubuntu-latest", "steps": [{"run": "echo before"}]}}}
    current = copy.deepcopy(before)
    current["jobs"]["test"]["steps"][0]["run"] = "echo after"
    old = ci_budget.definition_fingerprints("ci.yml", before)
    new = ci_budget.definition_fingerprints("ci.yml", current)
    budget = {"ci.yml": {"jobs": {"test": {"minutes": 1.0}}, "measured_on": [1, 2]}}
    baseline = json.dumps({"schema": 1, "workflows": {"ci.yml": old}})
    data = {
        ("base", batch.CI_FINGERPRINTS): baseline,
        ("common", batch.CI_FINGERPRINTS): baseline,
        ("member", batch.CI_FINGERPRINTS): None if legacy else baseline,
        ("common", ".github/workflows/ci.yml"): yaml.safe_dump(before),
        ("member", ".github/workflows/ci.yml"): yaml.safe_dump(current),
        ("common", batch.CI_BUDGET): json.dumps(budget),
        ("member", batch.CI_BUDGET): json.dumps(budget),
    }
    if legacy:
        previous, present = copy.deepcopy(budget), copy.deepcopy(budget)
        previous["ci.yml"]["fingerprints"] = old
        present["ci.yml"]["fingerprints"] = new
        present["ci.yml"]["accepted"] = [{"job": "test", "fingerprint": new["test"], "reason": "prints one line"}]
        data[("common", batch.CI_FINGERPRINTS)] = None
        data[("common", batch.CI_BUDGET)] = json.dumps(previous)
        data[("member", batch.CI_BUDGET)] = json.dumps(present)
    records = []
    monkeypatch.setattr(batch, "_budget_tools", lambda _: (ci_budget, None, None))
    monkeypatch.setattr(batch, "_budget_text", lambda rev, path: data.get((rev, path)))
    monkeypatch.setattr(batch, "_out", lambda *args: "common" if args[0] == "merge-base" else str(tmp_path.resolve()))
    monkeypatch.setattr(batch, "_budget_records", lambda rev, module: records)
    return data, records, old, new


def test_generated_identity_cannot_replace_a_member_cost_reason(monkeypatch, tmp_path):
    _, records, old, new = setup_reader(monkeypatch, tmp_path)
    member = SimpleNamespace(sha="member")
    with pytest.raises(ValueError, match="without a reviewed cost reason"):
        batch._budget_member_evidence("base", member)
    records.append({"schema": 1, "workflow": "ci.yml", "job": "test", "before": old["test"],
                    "fingerprint": new["test"], "reason": "prints one line"})
    assert batch._budget_member_evidence("base", member) == []


def test_a_member_cannot_regenerate_its_own_baseline(monkeypatch, tmp_path):
    data, _, _, new = setup_reader(monkeypatch, tmp_path)
    data[("member", batch.CI_FINGERPRINTS)] = json.dumps({"schema": 1, "workflows": {"ci.yml": new}})
    with pytest.raises(ValueError, match="PR changed generated fingerprints"):
        batch._budget_member_evidence("base", SimpleNamespace(sha="member"))


def test_legacy_member_reason_is_preserved_as_audit_data(monkeypatch, tmp_path):
    _, _, _, new = setup_reader(monkeypatch, tmp_path, legacy=True)
    imported = batch._budget_member_evidence("base", SimpleNamespace(sha="member"))
    assert imported == [{"workflow": "ci.yml", "job": "test", "fingerprint": new["test"], "reason": "prints one line"}]


def test_unverified_legacy_fingerprint_is_not_imported(monkeypatch, tmp_path):
    data, _, _, _ = setup_reader(monkeypatch, tmp_path, legacy=True)
    value = json.loads(data[("member", batch.CI_BUDGET)])
    value["ci.yml"]["fingerprints"]["test"] = "a" * 16
    data[("member", batch.CI_BUDGET)] = json.dumps(value)
    with pytest.raises(ValueError, match="unverified legacy cost data"):
        batch._budget_member_evidence("base", SimpleNamespace(sha="member"))


def test_member_scope_source_is_hashed_without_being_executed(monkeypatch, tmp_path):
    data, records, _, _ = setup_reader(monkeypatch, tmp_path)
    marker = tmp_path / "member-executed"
    before = {"jobs": {"test": {"runs-on": "ubuntu-latest", "strategy": {"matrix": {
        "suite": "${{ fromJSON(needs.scope.outputs.parity).suite }}"}}}}}
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    old = ci_budget.definition_fingerprints("rust.yml", before, "original source")
    new = ci_budget.definition_fingerprints("rust.yml", before, source)
    baseline = json.dumps({"schema": 1, "workflows": {"rust.yml": old}})
    for rev in ("base", "common", "member"):
        data[(rev, batch.CI_FINGERPRINTS)] = baseline
    for rev in ("common", "member"):
        data[(rev, batch.CI_BUDGET)] = json.dumps({"rust.yml": {"jobs": {}}})
        data[(rev, ".github/workflows/rust.yml")] = yaml.safe_dump(before)
    data[("common", batch.CI_SCOPE_CODE)] = "original source"
    data[("member", batch.CI_SCOPE_CODE)] = source
    records.append({"workflow": "rust.yml", "job": "test", "before": old["test"],
                    "fingerprint": new["test"], "reason": "metadata reader fixture"})
    assert batch._budget_member_evidence("base", SimpleNamespace(sha="member")) == []
    assert not marker.exists()


def test_redirected_generated_paths_are_refused_without_writes(monkeypatch, tmp_path):
    root = tmp_path.resolve()
    target, outside = root / batch.CI_FINGERPRINTS, root.parent / "outside.json"
    resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: outside if path == target else resolve(path, *args, **kwargs))
    with pytest.raises(batch.Refusal, match="redirected"):
        batch._metadata_target(root, batch.CI_FINGERPRINTS)
    assert not outside.exists()


def test_regular_generated_paths_remain_valid(tmp_path):
    root = tmp_path.resolve()
    assert batch._metadata_target(root, batch.CI_FINGERPRINTS) == root / batch.CI_FINGERPRINTS


def test_unresolvable_generated_path_is_refused(monkeypatch, tmp_path):
    root = tmp_path.resolve()
    monkeypatch.setattr(Path, "resolve", lambda path: (_ for _ in ()).throw(RuntimeError("symlink loop")))
    with pytest.raises(batch.Refusal, match="cannot be resolved"):
        batch._metadata_target(root, batch.CI_FINGERPRINTS)


def test_regeneration_preserves_numeric_data_and_legacy_reasons(monkeypatch, tmp_path):
    data, _, _, new = setup_reader(monkeypatch, tmp_path)
    data[("HEAD", batch.CI_BUDGET)] = data[("member", batch.CI_BUDGET)]
    data[("HEAD", ".github/workflows/ci.yml")] = data[("member", ".github/workflows/ci.yml")]
    target = tmp_path / batch.CI_BUDGET
    target.parent.mkdir(parents=True)
    target.write_text(data[("HEAD", batch.CI_BUDGET)])
    original = target.read_bytes()
    reason = {"workflow": "ci.yml", "job": "test", "fingerprint": new["test"], "reason": "prints one line"}
    monkeypatch.setattr(batch, "_budget_member_evidence", lambda base, member: [reason])
    staged = []
    monkeypatch.setattr(batch, "git", lambda *args, **kwargs: staged.append(args))
    plan = SimpleNamespace(base_sha="base", merged=[SimpleNamespace(sha="member")])
    paths = batch._regenerate_budget(plan, tmp_path.resolve())
    assert paths == {batch.CI_FINGERPRINTS, f"{batch.CI_ACCEPT}/imported-member.json"}
    assert target.read_bytes() == original
    baseline = json.loads((tmp_path / batch.CI_FINGERPRINTS).read_text())
    assert baseline["workflows"]["ci.yml"] == new
    archive = json.loads((tmp_path / batch.CI_ACCEPT / "imported-member.json").read_text())
    assert archive["legacy"] is True and archive["accepted"] == [reason]
    assert all(command[0] == "add" for command in staged)


def test_budget_loader_reads_only_master_code(monkeypatch):
    source = Path(ci_budget.__file__).read_text(encoding="utf-8")
    requested = []
    def read(rev, path):
        requested.append((rev, path))
        return source if rev == "base" and path == batch.CI_BUDGET_CODE else None
    monkeypatch.setattr(batch, "_budget_text", read)
    module, resolver, scope = batch._budget_tools.__wrapped__("base")
    assert module.definition_fingerprints("ci.yml", {"jobs": {}}) == {}
    assert resolver is None and scope is None
    assert all(rev == "base" for rev, _ in requested)


def test_cleanly_merged_legacy_fields_are_normalized_without_changing_prices(monkeypatch, tmp_path):
    data, _, _, _ = setup_reader(monkeypatch, tmp_path)
    legacy = json.loads(data[("member", batch.CI_BUDGET)])
    legacy["ci.yml"].update({"fingerprints": {"test": "a" * 16}, "accepted": []})
    data[("HEAD", batch.CI_BUDGET)] = json.dumps(legacy)
    data[("HEAD", ".github/workflows/ci.yml")] = data[("member", ".github/workflows/ci.yml")]
    monkeypatch.setattr(batch, "_budget_member_evidence", lambda base, member: [])
    monkeypatch.setattr(batch, "git", lambda *args, **kwargs: None)
    plan = SimpleNamespace(base_sha="base", merged=[])
    paths = batch._regenerate_budget(plan, tmp_path.resolve())
    assert batch.CI_BUDGET in paths
    result = json.loads((tmp_path / batch.CI_BUDGET).read_text())
    assert result == ci_budget.budget_data(legacy)


def test_fragment_cannot_approve_a_different_baseline(monkeypatch, tmp_path):
    _, records, _, new = setup_reader(monkeypatch, tmp_path)
    records.append({"workflow": "ci.yml", "job": "test", "before": "f" * 16,
                    "fingerprint": new["test"], "reason": "wrong starting definition"})
    with pytest.raises(ValueError, match="without a reviewed cost reason"):
        batch._budget_member_evidence("base", SimpleNamespace(sha="member"))


@pytest.mark.parametrize("fragment", [False, True])
def test_new_format_member_cannot_reintroduce_legacy_approvals(monkeypatch, tmp_path, fragment):
    data, records, _, new = setup_reader(monkeypatch, tmp_path)
    value = json.loads(data[("member", batch.CI_BUDGET)])
    claimed = "a" * 16 if fragment else new["test"]
    value["ci.yml"]["fingerprints"] = {"test": claimed}
    if fragment:
        records.append({"workflow": "ci.yml", "job": "test", "before": claimed,
                        "fingerprint": new["test"], "reason": "forged baseline"})
    else:
        value["ci.yml"]["accepted"] = [{"job": "test", "fingerprint": claimed, "reason": "old approval route"}]
    data[("member", batch.CI_BUDGET)] = json.dumps(value)
    with pytest.raises(ValueError, match="reintroduced legacy"):
        batch._budget_member_evidence("base", SimpleNamespace(sha="member"))


def test_legacy_fragment_cannot_choose_its_own_before_fingerprint(monkeypatch, tmp_path):
    data, records, _, new = setup_reader(monkeypatch, tmp_path, legacy=True)
    value = json.loads(data[("member", batch.CI_BUDGET)])
    value["ci.yml"]["fingerprints"]["test"] = "a" * 16
    value["ci.yml"]["accepted"] = []
    data[("member", batch.CI_BUDGET)] = json.dumps(value)
    records.append({"workflow": "ci.yml", "job": "test", "before": "a" * 16,
                    "fingerprint": new["test"], "reason": "forged prior definition"})
    with pytest.raises(ValueError, match="unverified legacy cost data"):
        batch._budget_member_evidence("base", SimpleNamespace(sha="member"))


def test_every_job_without_a_reason_is_named_at_once(monkeypatch, tmp_path):
    # The batch tool named only the first such job, so a pull request
    # missing two records was refused twice, one build apart.
    before = {"jobs": {name: {"runs-on": "ubuntu-latest", "steps": [{"run": "echo before"}]}
                       for name in ("lint", "test")}}
    current = copy.deepcopy(before)
    for name in ("lint", "test"):
        current["jobs"][name]["steps"][0]["run"] = "echo after"
    old = ci_budget.definition_fingerprints("ci.yml", before)
    budget = {"ci.yml": {"jobs": {"lint": {"minutes": 1.0}, "test": {"minutes": 1.0}}, "measured_on": [1, 2]}}
    baseline = json.dumps({"schema": 1, "workflows": {"ci.yml": old}})
    data = {("base", batch.CI_FINGERPRINTS): baseline, ("common", batch.CI_FINGERPRINTS): baseline,
            ("member", batch.CI_FINGERPRINTS): baseline,
            ("common", ".github/workflows/ci.yml"): yaml.safe_dump(before),
            ("member", ".github/workflows/ci.yml"): yaml.safe_dump(current),
            ("common", batch.CI_BUDGET): json.dumps(budget), ("member", batch.CI_BUDGET): json.dumps(budget)}
    monkeypatch.setattr(batch, "_budget_tools", lambda _: (ci_budget, None, None))
    monkeypatch.setattr(batch, "_budget_text", lambda rev, path: data.get((rev, path)))
    monkeypatch.setattr(batch, "_out", lambda *args: "common" if args[0] == "merge-base" else str(tmp_path.resolve()))
    monkeypatch.setattr(batch, "_budget_records", lambda rev, module: [])
    with pytest.raises(ValueError, match=r"ci\.yml/lint, ci\.yml/test changed without a reviewed cost reason; "
                                         r"run ci_budget\.py accept for each"):
        batch._budget_member_evidence("base", SimpleNamespace(sha="member"))
