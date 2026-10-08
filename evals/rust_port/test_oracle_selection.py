"""Event rules use immutable inputs; stale historical pins cannot block parity."""
import json
import sys
from types import SimpleNamespace

import pytest

from . import oracle_selection as oracle

BASE, HEAD, MERGE, CHECKOUT = (character * 40 for character in "abcd")


@pytest.fixture
def graph(monkeypatch):
    calls = []

    def git(root, *arguments):
        calls.append(arguments)
        if arguments[:2] == ("merge-base", "--all"):
            return MERGE
        return arguments[-1].split("^")[0]

    monkeypatch.setattr(oracle, "git", git)
    return calls


def test_pull_request_uses_actual_base_and_head_not_synthetic_merge(graph):
    selected = oracle.select("root", "pull_request", {
        "pull_request": {"base": {"sha": BASE}, "head": {"sha": HEAD}}}, CHECKOUT)
    assert selected["oracle_head"] == MERGE
    assert selected["checkout_head"] == CHECKOUT
    assert graph[0] == ("merge-base", "--all", BASE, HEAD)


def test_push_uses_after_without_computing_before_merge_base(graph):
    selected = oracle.select("root", "push", {"before": BASE, "after": HEAD}, HEAD)
    assert selected["oracle_head"] == HEAD
    assert not any(arguments[0] == "merge-base" for arguments in graph)


def test_dispatch_uses_frozen_head_without_new_input(graph):
    selected = oracle.select("root", "workflow_dispatch", {"inputs": {"frozen_head": HEAD}}, HEAD,
                             github_ref="refs/heads/master")
    assert selected["oracle_head"] == HEAD
    assert selected["github_ref"] == "refs/heads/master"
    assert not any(arguments[0] == "merge-base" for arguments in graph)


@pytest.mark.parametrize("ref", [None, "", "refs/heads/topic", "refs/tags/master"])
def test_dispatch_requires_the_master_branch_ref(graph, ref):
    with pytest.raises(ValueError, match="dispatch requires refs/heads/master"):
        oracle.select("root", "workflow_dispatch", {"inputs": {"frozen_head": HEAD}}, HEAD,
                      github_ref=ref)


def test_loaded_dispatch_selection_rechecks_recorded_ref(graph, monkeypatch):
    selection = oracle.select("root", "workflow_dispatch", {"inputs": {"frozen_head": HEAD}}, HEAD,
                              github_ref="refs/heads/master")
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selection))
    assert oracle.selected_oracle("root") == selection
    selection["github_ref"] = "refs/heads/topic"
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selection))
    with pytest.raises(ValueError, match="dispatch requires refs/heads/master"):
        oracle.selected_oracle("root")


@pytest.mark.parametrize("name,event", [
    ("push", {"after": HEAD}), ("workflow_dispatch", {"inputs": {"frozen_head": HEAD}})])
def test_event_head_mismatch_is_not_replaced_with_master(graph, name, event):
    with pytest.raises(ValueError, match="checkout head differ"):
        oracle.select("root", name, event, CHECKOUT, github_ref="refs/heads/master")


@pytest.mark.parametrize("value", [None, "master", "a" * 7, "0" * 40, "A" * 40])
def test_missing_or_nonimmutable_input_has_no_fallback(value):
    with pytest.raises(ValueError, match="full nonzero commit"):
        oracle.commit(value)


def test_multiple_actual_merge_bases_are_reported_without_selecting_one(monkeypatch):
    monkeypatch.setattr(oracle, "git", lambda *args: BASE + "\n" + MERGE)
    with pytest.raises(ValueError, match="one actual merge base"):
        oracle.merge_base("root", BASE, HEAD)


def test_loaded_selection_is_recomputed_and_tampering_is_rejected(graph, monkeypatch):
    selection = oracle.select("root", "pull_request", {
        "pull_request": {"base": {"sha": BASE}, "head": {"sha": HEAD}}}, CHECKOUT)
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selection))
    assert oracle.selected_oracle("root") == selection
    selection["oracle_head"] = HEAD
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selection))
    with pytest.raises(ValueError, match="immutable event binding"):
        oracle.selected_oracle("root")


def test_stale_pin_comparison_is_informational_even_when_different_or_unavailable(monkeypatch):
    for code in (0, 1, 128):
        monkeypatch.setattr(oracle.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=code))
        report = oracle.stale_pin_report("root", BASE, ("pseudolife_memory",))
        assert report["informational_only"] is True
        assert report["source_matches_historical_pin"] == (code == 0)
        assert report["comparison_available"] == (code in (0, 1))


def test_new_capture_cannot_silently_use_an_old_pin(monkeypatch):
    monkeypatch.delenv(oracle.SELECTION_ENV, raising=False)
    with pytest.raises(RuntimeError, match="select the event oracle"):
        oracle.selected_oracle("root")


@pytest.mark.parametrize("name,event,checkout,expected", [
    ("pull_request", {"pull_request": {"base": {"sha": BASE}, "head": {"sha": HEAD}}}, CHECKOUT, MERGE),
    ("push", {"after": HEAD}, HEAD, HEAD),
    ("workflow_dispatch", {"inputs": {"frozen_head": HEAD}}, HEAD, HEAD),
])
def test_selector_writes_the_same_sha_to_receipt_and_github_environment(
        graph, monkeypatch, tmp_path, name, event, checkout, expected):
    original_git = oracle.git
    monkeypatch.setattr(oracle, "git", lambda root, *args:
                        checkout if args == ("rev-parse", "HEAD") else original_git(root, *args))
    payload, receipt, environment = (tmp_path / name for name in ("event.json", "selection.json", "env"))
    payload.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/master")
    monkeypatch.setattr(sys, "argv", ["selection", "--root", str(tmp_path), "--event-name", name,
                                     "--event-path", str(payload), "--checkout-head", checkout,
                                     "--out", str(receipt), "--github-env", str(environment)])
    oracle.main()
    observed = json.loads(receipt.read_text(encoding="utf-8"))
    assert observed["oracle_head"] == expected
    assert observed["github_ref"] == "refs/heads/master"
    assert environment.read_text(encoding="utf-8") == oracle.SELECTION_ENV + "=" + receipt.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        oracle.main()


@pytest.mark.parametrize("failure", [OSError("unavailable"), oracle.subprocess.TimeoutExpired("git", 10)])
def test_unavailable_historical_report_cannot_block_current_parity(monkeypatch, failure):
    def unavailable(*args, **kwargs):
        raise failure
    monkeypatch.setattr(oracle.subprocess, "run", unavailable)
    assert oracle.stale_pin_report("root", BASE, ("pseudolife_memory",)) == {
        "historical_pin": BASE, "informational_only": True,
        "comparison_available": False, "source_matches_historical_pin": False}


@pytest.mark.parametrize("dirty", [False, True])
def test_source_guard_uses_selected_schema_and_rejects_actual_source_drift(monkeypatch, tmp_path, dirty):
    from . import stdio_capture
    schema = tmp_path / "pseudolife_memory/storage/schema.py"
    schema.parent.mkdir(parents=True)
    schema.write_text("SCHEMA_META_VERSION = 59\n", encoding="utf-8")
    selection = {"oracle_head": HEAD}
    monkeypatch.setattr(stdio_capture, "selected_oracle", lambda: selection)
    calls = []
    def compare(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=int(dirty))
    monkeypatch.setattr(stdio_capture.subprocess, "run", compare)
    monkeypatch.setattr(stdio_capture.subprocess, "check_output", lambda *args, **kwargs: b"")
    monkeypatch.setattr(stdio_capture, "source_metadata", lambda root, **kwargs: kwargs)
    if dirty:
        with pytest.raises(RuntimeError, match="selected production source required"):
            stdio_capture.require_phase1_source(tmp_path)
    else:
        checked = stdio_capture.require_phase1_source(tmp_path)
        assert checked["oracle_head"] == HEAD
        assert checked["oracle_schema"] == 59
        assert checked["oracle_selection"] == selection
        assert checked["stale_pin_report"]["informational_only"]
    assert calls[0][3] == HEAD
