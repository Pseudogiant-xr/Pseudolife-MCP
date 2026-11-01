"""A published body comparison must distinguish missing evidence from parity."""
import json
from pathlib import Path

import pytest

import body_evidence as evidence


def test_write_counts_every_difference_and_preserves_scenario_names():
    report = {"complete": True, "planned_scenarios": ["write", "restart"], "scenarios": [
        {"scenario": "write", "steps": [{"step": 0, "diffs": []},
                                             {"step": 1, "diffs": ["body", "state"]}]},
        {"scenario": "restart", "steps": [{"step": 0, "diffs": []}]},
    ]}
    result = evidence.summarize("w2e", report, 1)
    assert result["status"] == "fail"
    assert result["counts"] == {"scenarios": 2, "cases": 3, "different_cases": 1,
                                 "differences": 2}
    assert result["scenarios"][0]["differences"] == [
        {"case": "1", "detail": "body"}, {"case": "1", "detail": "state"}]


@pytest.mark.parametrize("family,key", [("w2d_search", "db_diffs"), ("board", "chain"),
                                        ("board", "state_diffs")])
def test_state_and_chain_fail_even_without_body_differences(family, key):
    rows = "cases" if family == "w2d_search" else "steps"
    report = {"complete": True, "planned_scenarios": ["test"],
              "results": [{"scenario": "test", rows: [{"diffs": []}], key: ["lost row"]}]}
    assert evidence.summarize(family, report, 0)["status"] == "fail"


@pytest.mark.parametrize("report", [{}, {"scenarios": []},
    {"scenarios": [{"scenario": "empty", "steps": []}]},
    {"scenarios": [{"scenario": "skip", "steps": [{"diffs": []}], "skipped": "missing model"}]}])
def test_missing_empty_or_skipped_evidence_never_passes(report):
    assert evidence.summarize("w2e", report, 0)["status"] == "fail"


def test_nonzero_exit_never_becomes_a_pass():
    report = {"complete": True, "planned_scenarios": ["ok"],
              "scenarios": [{"scenario": "ok", "steps": [{"diffs": []}]}]}
    assert evidence.summarize("w2e", report, 0)["status"] == "pass"
    assert evidence.summarize("w2e", report, 2)["status"] == "fail"


def test_board_mixed_chain_is_counted_without_inventing_http_cases():
    report = {"complete": True, "planned_scenarios": ["mixed-chain (python first)"],
              "results": [{"scenario": "mixed-chain (python first)", "steps": [],
                            "state_diffs": [], "chain": []}]}
    result = evidence.summarize("board", report, 0)
    assert result["status"] == "pass"
    assert result["counts"]["cases"] == 0


def test_runner_attempts_all_three_and_keeps_failure_report(tmp_path, monkeypatch):
    binary = tmp_path / "daemon"
    binary.write_bytes(b"candidate")
    manifest = {"rust_head": "a" * 40, "python_head": "a" * 40,
                "event": "workflow_dispatch", "ref": "refs/heads/test"}
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        output = Path(command[command.index("--out") + 1])
        if len(calls) != 2:
            key = "scenarios" if len(calls) == 1 else "results"
            output.write_text(json.dumps({"complete": True, "planned_scenarios": ["one"],
                                          key: [{"scenario": "one", "steps": [{"diffs": []}]}]}))
        return type("Result", (), {"returncode": 0 if len(calls) != 2 else 2})()

    monkeypatch.setattr(evidence.subprocess, "run", execute)
    result = evidence.run(binary, tmp_path / "out", manifest)
    assert len(calls) == 3
    assert all("live" in c and "--only" not in c for c in calls)
    assert result["status"] == "fail"
    assert [s["status"] for s in result["suites"]] == ["pass", "fail", "pass"]
    saved = json.loads((tmp_path / "out/evidence.json").read_text())
    assert saved["rust_head"] == saved["python_head"] == "a" * 40
    assert saved["binary_sha256"]


def test_tokenless_woke_is_compared_as_a_served_body(monkeypatch):
    import importlib

    monkeypatch.syspath_prepend(str(evidence.HARNESS))
    harness = importlib.import_module("run")
    case = next(c for c in harness.Tokenless().cases() if c["name"] == "tokenless hook woke authorized")
    assert case["declared"] is None
    response = {"status": 200, "headers": {"content-type": "text/plain; charset=utf-8"}, "bytes": ""}
    assert harness.compare_case(case, response, response)["diffs"] == []
    assert harness.compare_case(case, response, dict(response, status=501))["diffs"]


def test_partial_report_names_the_scenarios_that_did_not_run():
    report = {"complete": False, "planned_scenarios": ["first", "second"],
              "scenarios": [{"scenario": "first", "steps": [{"diffs": []}]}]}
    result = evidence.summarize("w2e", report, 1)
    assert result["not_run"] == ["second"]
    assert result["counts"]["scenarios"] == 1
    assert result["status"] == "fail"
    report["complete"] = True
    assert evidence.summarize("w2e", report, 0)["status"] == "fail"


def test_public_report_retains_differences_without_paths_or_logins(tmp_path):
    detail = f'{evidence.ROOT}: {{"token": "fixture-secret"}}, postgresql://user:secret@localhost/db'
    clean = evidence.public_value({"differences": [detail]}, tmp_path / "out")
    text = json.dumps(clean)
    assert str(evidence.ROOT) not in text
    assert "fixture-secret" not in text and "user:secret" not in text
    assert "<checkout>" in text and "<database-url>" in text


@pytest.mark.parametrize("event,ref,eligible", [("schedule", "refs/heads/master", True),
    ("workflow_dispatch", "refs/heads/master", False), ("workflow_dispatch", "refs/heads/trial", False)])
def test_plan_binds_both_heads_and_distinguishes_a_trial(tmp_path, monkeypatch, event, ref, eligible):
    outputs = iter(["a" * 40, ""])
    monkeypatch.setattr(evidence.subprocess, "check_output", lambda *a, **kw: next(outputs))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", ref)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    planned = evidence.plan(tmp_path)
    assert planned["rust_head"] == planned["python_head"] == "a" * 40
    assert planned["scheduled_master"] is eligible
    assert planned["status"] == "incomplete"


@pytest.mark.parametrize("head,dirty,ref", [("b" * 40, "", "refs/heads/master"),
    ("a" * 40, "M code.py", "refs/heads/master"), ("a" * 40, "", "refs/heads/trial")])
def test_plan_refuses_a_misbound_or_dirty_scheduled_checkout(tmp_path, monkeypatch, head, dirty, ref):
    outputs = iter([head, dirty])
    monkeypatch.setattr(evidence.subprocess, "check_output", lambda *a, **kw: next(outputs))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_REF", ref)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    with pytest.raises(ValueError):
        evidence.plan(tmp_path)


@pytest.mark.parametrize("family", ["w2e", "w2d_search", "board.run"])
def test_harness_checkpoint_survives_a_later_scenario_error(tmp_path, monkeypatch, family):
    import importlib
    import sys
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(evidence.HARNESS))
    module = importlib.import_module(family)
    registry = module.S if family == "board.run" else module
    monkeypatch.setattr(registry, "SCENARIOS", {"first": lambda: "first", "second": lambda: "second"})
    monkeypatch.setattr(module.daemons, "scratch_root", lambda: tmp_path)
    if family == "w2e":
        monkeypatch.setattr(module, "run_root", lambda: tmp_path)
    if family == "w2d_search":
        monkeypatch.setattr(module, "seed_template", lambda *args: None)
        monkeypatch.setattr(module, "pg", SimpleNamespace(name=lambda name: name))
    count = 0

    def scenario(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("second scenario failed")
        return {"scenario": "first", "steps": [{"step": 0, "op": {}, "diffs": []}],
                "cases": [{"diffs": []}], "stats": {}}

    monkeypatch.setattr(module, "run_scenario", scenario)
    output = tmp_path / "partial.json"
    monkeypatch.setattr(sys, "argv", [family, "live", "--rust-bin", str(tmp_path / "daemon"),
                                      "--out", str(output), "--only", "first", "second"])
    with pytest.raises(RuntimeError, match="second scenario failed"):
        module.main()
    saved = json.loads(output.read_text())
    assert saved["complete"] is False
    assert saved["planned_scenarios"] == ["first", "second"]
    assert [r["scenario"] for r in saved.get("scenarios", saved.get("results"))] == ["first"]


def test_native_model_paths_reject_ambiguous_or_incomplete_provisioning(tmp_path):
    from body_oracle import native_paths

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "libonnxruntime.so.1.27.0").touch()
    models = tmp_path / "models"
    first = models / "qwen3-msmarco-one"
    first.mkdir(parents=True)
    with pytest.raises(ValueError, match="graph or tokenizer"):
        native_paths(models, runtime)
    for name in ("embedding", "reranker"):
        (first / name / "onnx").mkdir(parents=True)
        (first / name / "onnx/model.onnx").touch()
        (first / name / "tokenizer.json").touch()
    # The reranker loader consumes a flat graph, unlike the embedding loader.
    with pytest.raises(ValueError, match="graph or tokenizer"):
        native_paths(models, runtime)
    (first / "reranker/onnx/model.onnx").unlink()
    (first / "reranker/model.onnx").touch()
    assert set(native_paths(models, runtime)) == {
        "PSEUDOLIFE_DAEMON_ONNX_DIR", "PSEUDOLIFE_DAEMON_RERANK_DIR", "ORT_DYLIB_PATH"}
    (models / "qwen3-msmarco-two").mkdir()
    with pytest.raises(ValueError, match="exactly one"):
        native_paths(models, runtime)
