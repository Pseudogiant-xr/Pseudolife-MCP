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
    monkeypatch.setattr(oracle, "release_cut", lambda *args: None)
    monkeypatch.setattr(oracle, "require_pinned_head", lambda *args: None)

    def git(root, *arguments):
        calls.append(arguments)
        if arguments == ("rev-parse", "refs/remotes/origin/master^{commit}"):
            return BASE
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


def test_merge_group_uses_its_base_and_checked_out_head(graph, monkeypatch):
    # A queued group is checked out at its own head; its base is the oracle side.
    selected = oracle.select("root", "merge_group", {
        "merge_group": {"base_sha": BASE, "head_sha": HEAD}}, HEAD,
        github_ref="refs/heads/gh-readonly-queue/master/pr-830-" + BASE)
    assert selected["oracle_head"] == MERGE
    assert (selected["base_head"], selected["candidate_head"]) == (BASE, HEAD)
    assert graph[0] == ("merge-base", "--all", BASE, HEAD)
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selected))
    assert oracle.selected_oracle("root") == selected
    selected["oracle_head"] = HEAD
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selected))
    with pytest.raises(ValueError, match="immutable event binding"):
        oracle.selected_oracle("root")


def test_merge_group_head_must_be_the_checkout(graph):
    with pytest.raises(ValueError, match="checkout head differ"):
        oracle.select("root", "merge_group", {
            "merge_group": {"base_sha": BASE, "head_sha": HEAD}}, CHECKOUT)


def test_push_uses_after_without_computing_before_merge_base(graph):
    selected = oracle.select("root", "push", {"before": BASE, "after": HEAD}, HEAD)
    assert selected["oracle_head"] == HEAD
    assert not any(arguments[0] == "merge-base" for arguments in graph)


def test_schedule_uses_immutable_master_checkout_and_rechecks_receipt(graph, monkeypatch):
    selection = oracle.select('root', 'schedule', {'schedule': '23 3 * * *'}, HEAD,
                              github_ref='refs/heads/master')
    assert selection['event'] == 'schedule' and selection['oracle_head'] == HEAD
    assert selection['candidate_head'] == HEAD and selection['base_head'] is None
    assert not any(arguments[0] == 'merge-base' for arguments in graph)
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selection))
    assert oracle.selected_oracle('root') == selection
    selection['oracle_head'] = BASE
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selection))
    with pytest.raises(ValueError, match='immutable event binding'):
        oracle.selected_oracle('root')


@pytest.mark.parametrize('ref', [None, '', 'refs/heads/topic', 'refs/tags/master'])
def test_schedule_rejects_non_master_ref(graph, ref):
    with pytest.raises(ValueError, match='schedule requires refs/heads/master'):
        oracle.select('root', 'schedule', {}, HEAD, github_ref=ref)


def test_dispatch_uses_frozen_head_without_new_input(graph):
    selected = oracle.select("root", "workflow_dispatch", {"inputs": {"frozen_head": HEAD}}, HEAD,
                             github_ref="refs/heads/master")
    assert selected["oracle_head"] == HEAD
    assert selected["github_ref"] == "refs/heads/master"
    assert not any(arguments[0] == "merge-base" for arguments in graph)


@pytest.mark.parametrize("ref", [None, "", "refs/heads/topic", "refs/tags/master",
                                 "refs/heads/Master", "refs/heads/MASTER"])
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


def test_branch_stress_freezes_master_and_rechecks_without_following_the_ref(graph, monkeypatch):
    selected = oracle.select("root", "workflow_dispatch", {"inputs": {"mode": "stress"}}, HEAD,
                             github_ref="refs/heads/topic")
    assert selected["oracle_head"] == MERGE
    assert selected["candidate_head"] == selected["checkout_head"] == HEAD
    assert selected["base_head"] == BASE
    assert selected["dispatch_mode"] == "stress"
    assert ("merge-base", "--all", BASE, HEAD) in graph
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selected))
    original_git = oracle.git

    def moved_master(root, *args):
        assert args != ("rev-parse", "refs/remotes/origin/master^{commit}"), "receipt must retain its frozen base"
        return original_git(root, *args)

    monkeypatch.setattr(oracle, "git", moved_master)
    assert oracle.selected_oracle("root") == selected
    selected["oracle_head"] = HEAD
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selected))
    with pytest.raises(ValueError, match="immutable event binding"):
        oracle.selected_oracle("root")


@pytest.mark.parametrize("ref", ["refs/heads/topic", "refs/heads/Master", "refs/heads/MASTER"])
def test_branch_ci_uses_the_run_wide_base_and_reviewed_head(graph, monkeypatch, ref):
    selected = oracle.select("root", "workflow_dispatch", {
        "inputs": {"mode": "ci", "frozen_head": HEAD}}, HEAD, github_ref=ref, dispatch_base=BASE)
    assert selected["base_head"] == BASE
    assert selected["oracle_head"] == MERGE
    assert selected["candidate_head"] == HEAD and selected["dispatch_mode"] == "ci"
    monkeypatch.setenv(oracle.SELECTION_ENV, json.dumps(selected))
    original_git = oracle.git
    def moved_master(root, *args):
        assert "refs/remotes/origin/master^{commit}" not in args
        return original_git(root, *args)
    monkeypatch.setattr(oracle, "git", moved_master)
    assert oracle.selected_oracle("root") == selected


@pytest.mark.parametrize("frozen", [None, "", BASE, "a" * 12, "0" * 40])
def test_branch_ci_requires_the_exact_full_reviewed_head(graph, frozen):
    with pytest.raises(ValueError):
        oracle.select("root", "workflow_dispatch", {"inputs": {"mode": "ci", "frozen_head": frozen}},
                      HEAD, github_ref="refs/heads/topic", dispatch_base=BASE)


def test_branch_ci_cannot_resolve_a_moving_master_itself(graph):
    with pytest.raises(ValueError, match="frozen.*base"):
        oracle.select("root", "workflow_dispatch", {"inputs": {"mode": "ci", "frozen_head": HEAD}},
                      HEAD, github_ref="refs/heads/topic")


def test_freeze_ci_base_reads_master_only_once_and_requires_the_reviewed_head(graph, monkeypatch):
    calls = []
    def git(root, *args):
        calls.append(args)
        return BASE if args == ("rev-parse", "FETCH_HEAD^{commit}") else ""
    monkeypatch.setattr(oracle, "git", git)
    event = {"inputs": {"mode": "ci", "frozen_head": HEAD}}
    assert oracle.freeze_ci_base("root", "workflow_dispatch", event, HEAD, "refs/heads/topic") == BASE
    assert calls == [("fetch", "--no-tags", "--depth=1", "origin", "refs/heads/master"),
                     ("rev-parse", "FETCH_HEAD^{commit}")]
    calls.clear()
    assert oracle.freeze_ci_base("root", "workflow_dispatch", event, HEAD, "refs/heads/master") is None
    assert calls == []


@pytest.mark.parametrize("mode,frozen,ref", [
    ("ci", None, "refs/heads/topic"), ("ci", "b" * 12, "refs/heads/topic"),
    ("ci", BASE, "refs/heads/topic"), ("ci", HEAD, "refs/tags/topic"),
    ("ci", HEAD, None), ("ci", HEAD, "refs/heads/"),
    ("stress", HEAD, "refs/heads/topic"),
])
def test_ci_freezing_rejects_invalid_inputs_before_fetch(graph, mode, frozen, ref):
    with pytest.raises(ValueError):
        oracle.freeze_ci_base("root", "workflow_dispatch", {"inputs": {"mode": mode, "frozen_head": frozen}},
                              HEAD, ref)
    assert graph == []




@pytest.mark.parametrize("mode", ["wait-mail", "stressful", "", None])
def test_only_explicit_ci_or_stress_can_dispatch_from_a_branch(graph, mode):
    with pytest.raises(ValueError, match="dispatch requires refs/heads/master"):
        oracle.select("root", "workflow_dispatch", {"inputs": {"mode": mode, "frozen_head": HEAD}},
                      HEAD, github_ref="refs/heads/topic")


@pytest.mark.parametrize("ref", [None, "", "refs/tags/topic", "refs/pull/1/merge", "refs/heads/"])
def test_branch_stress_requires_a_branch_ref(graph, ref):
    with pytest.raises(ValueError, match="stress dispatch requires a branch ref"):
        oracle.select("root", "workflow_dispatch", {"inputs": {"mode": "stress"}}, HEAD,
                      github_ref=ref)


def test_branch_stress_cannot_replace_the_dispatched_checkout_with_frozen_head(graph):
    with pytest.raises(ValueError, match="dispatch frozen head and checkout head differ"):
        oracle.select("root", "workflow_dispatch", {
            "inputs": {"mode": "stress", "frozen_head": BASE}}, HEAD, github_ref="refs/heads/topic")


def test_stress_does_not_change_the_existing_master_frozen_capture(graph):
    selected = oracle.select("root", "workflow_dispatch", {
        "inputs": {"mode": "stress", "frozen_head": HEAD}}, HEAD, github_ref="refs/heads/master")
    assert selected["oracle_head"] == HEAD
    assert selected["base_head"] is None
    assert not any(args[0] == "merge-base" for args in graph)


@pytest.mark.parametrize("ref", ["refs/heads/topic", "refs/heads/Master", "refs/heads/MASTER"])
def test_stress_cli_uses_the_run_wide_base_after_master_moves(graph, monkeypatch, tmp_path, ref):
    original_git = oracle.git
    monkeypatch.setattr(oracle, "git", lambda root, *args:
                        HEAD if args in (("rev-parse", "HEAD"),
                                         ("rev-parse", "refs/remotes/origin/master^{commit}"),
                                         ("rev-parse", "origin/master^{commit}")) else original_git(root, *args))
    payload, receipt = tmp_path / "event.json", tmp_path / "selection.json"
    payload.write_text(json.dumps({"inputs": {"mode": "stress"}}), encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_PORT_DISPATCH_BASE", BASE)
    monkeypatch.setattr(sys, "argv", ["selection", "--root", str(tmp_path),
        "--event-name", "workflow_dispatch", "--event-path", str(payload),
        "--checkout-head", HEAD, "--github-ref", ref, "--out", str(receipt)])
    oracle.main()
    selected = json.loads(receipt.read_text(encoding="utf-8"))
    assert selected["base_head"] == BASE
    assert ("merge-base", "--all", BASE, HEAD) in graph


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
    ("merge_group", {"merge_group": {"base_sha": BASE, "head_sha": HEAD}}, HEAD, MERGE),
    ("push", {"after": HEAD}, HEAD, HEAD),
    ("schedule", {"schedule": "23 3 * * *"}, HEAD, HEAD),
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
