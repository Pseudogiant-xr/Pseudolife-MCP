"""A setup retry must not conceal a test or artifact-retention failure."""
import copy
import json

import pytest

import stress
import stress_history as history

SHA = "a" * 40


def job(attempt=1, conclusion="success", test="success"):
    return {"id": 100 + attempt, "run_id": 12, "run_attempt": attempt, "head_sha": SHA,
            "name": "Stress / windows-latest / 1", "status": "completed", "conclusion": conclusion,
            "steps": [{"name": "Download harness candidate", "status": "completed",
                       "conclusion": "failure" if test == "skipped" else "success"},
                      {"name": "Run the selection once", "status": "completed", "conclusion": test},
                      {"name": "Retain this iteration", "status": "completed", "conclusion": "success"}]}


def document(jobs):
    return {"run_id": 12, "head_sha": SHA, "run_attempt": max(j["run_attempt"] for j in jobs), "jobs": jobs}


def rows(attempt=2):
    return [{"iteration": 1, "attempt": attempt, "outcome": "pass"}]


def test_only_a_failed_setup_may_be_replaced_once():
    original = job(conclusion="failure", test="skipped")
    result = history.assess(document([original, job(2)]), rows())
    assert result["clean"] and result["retried"] == [1]
    assert [j["cause"] for j in result["jobs"]] == ["setup failure: Download harness candidate", "passed"]


def test_a_lone_success_on_a_later_attempt_has_no_original_setup_proof():
    assert not history.assess(document([job(2)]), rows())["clean"]


def test_a_failure_after_a_skipped_test_is_not_a_setup_only_failure():
    failed = job(conclusion="failure", test="skipped")
    failed["steps"][-1]["conclusion"] = "failure"
    result = history.assess(document([failed, job(2)]), rows())
    assert not result["clean"] and "Retain this iteration" in result["jobs"][0]["cause"]


@pytest.mark.parametrize("test", ["failure", "success", "cancelled", "timed_out", "in_progress", None])
def test_a_test_that_started_or_a_post_test_failure_cannot_be_replaced(test):
    original = job(conclusion="failure", test=test)
    assert not history.assess(document([original, job(2)]), rows())["clean"]


def test_runner_provisioning_failure_has_no_test_steps():
    original = job(conclusion="failure")
    original["steps"] = []
    result = history.assess(document([original, job(2)]), rows())
    assert result["clean"] and "no steps" in result["jobs"][0]["cause"]


def test_a_second_setup_retry_is_not_clean():
    jobs = [job(conclusion="failure", test="skipped"), job(2, "failure", "skipped"), job(3)]
    assert not history.assess(document(jobs), rows(3))["clean"]


@pytest.mark.parametrize("change", [
    lambda j: j.pop("steps"), lambda j: j.update(steps=None),
    lambda j: j["steps"].pop(1),
    lambda j: j["steps"].append(copy.deepcopy(j["steps"][1])),
    lambda j: j.update(head_sha="b" * 40), lambda j: j.update(run_id=99),
    lambda j: j.update(name="Stress / ubuntu-latest / 1"),
])
def test_missing_ambiguous_or_mismatched_evidence_fails_closed(change):
    original = job(conclusion="failure", test="skipped")
    change(original)
    assert not history.assess(document([original, job(2)]), rows())["clean"]


def test_the_artifact_must_match_the_successful_job_attempt():
    assert not history.assess(document([job()]), rows(2))["clean"]


def test_collect_reads_every_attempt_and_every_page():
    first, second = job(conclusion="failure", test="skipped"), job(2)
    calls = []

    def get(path):
        calls.append(path)
        selected = first if "/attempts/1/" in path else second
        scope = {**selected, "id": selected["id"] + 1000, "name": "Scope"}
        return {"jobs": [scope] if path.endswith("page=1") else [selected], "total_count": 2}

    result = history.collect(get, 12, 2, SHA)
    assert result == document([first, second])
    assert len(calls) == 4 and "/attempts/1/jobs?" in calls[0] and "/attempts/2/jobs?" in calls[2]


@pytest.mark.parametrize("value", [None, 0, 2, "1", True])
def test_collect_cannot_drop_a_job_with_a_missing_or_wrong_attempt(value):
    failed = job(conclusion="failure", test="failure")
    failed["run_attempt"] = value
    with pytest.raises(ValueError):
        history.collect(lambda _: {"jobs": [failed], "total_count": 1}, 12, 1, SHA)


@pytest.mark.parametrize("total", [-1, 0, True, "1"])
def test_collect_rejects_invalid_or_inconsistent_totals(total):
    with pytest.raises(ValueError):
        history.collect(lambda _: {"jobs": [job()], "total_count": total}, 12, 1, SHA)


def test_collect_rejects_a_count_that_changes_between_pages():
    def get(path):
        return {"jobs": [job()] if path.endswith("page=1") else [],
                "total_count": 2 if path.endswith("page=1") else 1}
    with pytest.raises(ValueError, match="count changed"):
        history.collect(get, 12, 1, SHA)


def test_collect_keeps_known_carried_jobs_without_counting_them_twice():
    original, replacement = job(conclusion="failure", test="skipped"), job(2)
    def get(path):
        jobs = [original] if "/attempts/1/" in path else [original, replacement]
        return {"jobs": jobs, "total_count": len(jobs)}
    assert history.collect(get, 12, 2, SHA) == document([original, replacement])


def test_post_test_artifact_failure_is_retained():
    original = job(conclusion="failure")
    original["steps"][-1]["conclusion"] = "failure"
    result = history.assess(document([original, job(2)]), rows())
    assert not result["clean"] and "Retain this iteration" in result["jobs"][0]["cause"]


def test_an_optional_setup_error_does_not_discard_a_clean_test_execution():
    # Oracle binding is intentionally best-effort for selections that do
    # not consume it; a passed selection in a successful job still counts.
    original = job()
    original["steps"][0].update(name="Bind optional oracle", conclusion="failure")
    assert history.assess(document([original]), rows(1))["clean"]


def test_unavailable_history_cannot_certify_a_clean_run(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    path = tmp_path / "history.json"
    assert history.main(["--out", str(path)]) == 0
    assert not history.assess(json.loads(path.read_text()), rows())["clean"]


def test_workflow_always_checks_history_with_read_only_permissions():
    from pathlib import Path
    import yaml
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/rust.yml").read_text())
    report = workflow["jobs"]["stress-report"]
    assert report["permissions"] == {"contents": "read", "actions": "read"}
    steps = report["steps"]
    collect = next(s for s in steps if s.get("name") == "Read every stress job attempt")
    join = next(s for s in steps if s.get("name") == "Join the iterations")
    assert steps.index(collect) < steps.index(join)
    assert not collect.get("if") and not join.get("if")
    assert collect["run"] == "python3 rust/ci/stress_history.py --out stress-job-history.json"
    assert "--job-history stress-job-history.json" in join["run"]
    upload = next(s for s in steps if s.get("name") == "Retain the stress report")
    assert upload["with"]["name"] == "stress-report-${{ github.run_attempt }}"
    test = [s for s in workflow["jobs"]["stress"]["steps"] if s.get("name") == history.TEST_STEP]
    assert len(test) == 1 and not test[0].get("continue-on-error")


def test_report_accepts_mixed_attempts_only_with_verified_setup_history(tmp_path):
    root = tmp_path / "rows"
    root.mkdir()
    for i, attempt in [(1, 1), (2, 2)]:
        (root / f"stress-row-{i}.json").write_text(json.dumps({"iteration": i, "attempt": attempt,
            "outcome": "pass", "runner": {}, "seconds": 1, "exit_code": 0}))
    one, failed, retry = job(), job(conclusion="failure", test="skipped"), job(2)
    failed["id"], retry["id"] = 201, 202
    for item in (failed, retry):
        item["name"] = "Stress / windows-latest / 2"
    path = tmp_path / "history.json"
    path.write_text(json.dumps(document([one, failed, retry])))
    output = tmp_path / "report.json"
    args = ["report", "--selection", "row:hook", "--iterations", "2", "--os", "windows-latest",
            "--rows", str(root), "--out", str(output)]
    assert stress.main(args) == 1
    assert stress.main([*args, "--job-history", str(path)]) == 0
    assert json.loads(output.read_text())["job_history"]["retried"] == [2]
    failed["steps"][1]["conclusion"] = "failure"
    path.write_text(json.dumps(document([one, failed, retry])))
    assert stress.main([*args, "--job-history", str(path)]) == 1
