"""Docs-only scope decisions for CI and Rust shim."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import yaml

from ops import ci_batch_passed, ci_push_passed
from rust.ci import docs_scope
from rust.ci.ci_budget import Evaluator, condition_text


ROOT = Path(__file__).resolve().parents[2]
MASTER = "refs/heads/master"
CI_JOBS = ("guards", "test", "test-lite-windows", "test-lite-macos",
           "test-windows-nightly", "lock-resolve", "frontend")
RUST_JOBS = ("rust", "candidate", "candidate-harness", "fixtures", "daemon-image")
RUST_CHECKS = ("Rust / ubuntu-latest", "Rust / windows-latest")


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["ci.yml", "rust.yml"])
def test_scope_uses_the_shared_documentation_classifier(name):
    assert any("python3 -m rust.ci.docs_scope" in step.get("run", "")
               for step in workflow(name)["jobs"]["scope"]["steps"])


@pytest.mark.parametrize("reader", [
    "tests/test_release_ux.py", "tests/test_llms_txt.py", "tests/test_atlas_currency.py",
    "tests/test_eval_evidence.py", "tests/test_i18n_readme.py", "tests/test_claude_md_citations.py",
    "tests/test_extractor_model_lists.py", "rust/ci/test_docs_scope.py",
    "tests/test_batch_timeline.py::test_committed_artifact_backs_the_findings",
    "rust/ci/test_parity_scope.py::test_the_plan_pages_counted_as_docs_are_read_by_no_code",
    "rust/test_producer_census.py::test_current_tree_census_has_real_producer_floors",
    "rust/test_producer_census.py::test_snapshot_is_independent_of_source_commit_history",
])
def test_fast_ci_runs_doc_readers_and_the_scope_tests(reader):
    steps = workflow("ci.yml")["jobs"]["guards"]["steps"]
    tree_guards = next(step for step in steps if step.get("name") == "Tree-wide guards")
    assert "if" not in tree_guards
    assert reader in shlex.split(tree_guards["run"])


class ScopeOutputs(Evaluator):
    def __init__(self, event, outputs):
        super().__init__(event, ["scope"], set())
        self.outputs = outputs

    def lookup(self, path):
        if path[:3] == ["needs", "scope", "outputs"]:
            return self.outputs.get(path[3], "")
        return super().lookup(path)

    def call(self, name):
        if name == "fromJSON":
            self.take()
            data = self.disjunction()
            assert self.peek(")")
            self.take()
            return json.loads(data)
        return super().call(name)


def selected_lanes(name, jobs, event, outputs):
    conditions = workflow(name)["jobs"]
    evaluate = ScopeOutputs(event, outputs)
    return {job for job in jobs if evaluate.run(condition_text(conditions[job]["if"]))}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("the workflow scope jobs run on Ubuntu with Bash")
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setattr(docs_scope, "ROOT", root)
    docs_scope.git("init", "-q", "-b", "master")
    docs_scope.git("config", "user.name", "CI scope test")
    docs_scope.git("config", "user.email", "scope@example.com")

    def commit(*paths):
        for relative in paths:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("change\n", encoding="utf-8")
        docs_scope.git("add", "-A")
        docs_scope.git("commit", "-q", "-m", "fixture")
        return docs_scope.git("rev-parse", "HEAD").strip()

    base = commit("initial.txt")
    return root, base, commit


def ci_outputs(root, tmp_path, env, before, weekly=""):
    job = workflow("ci.yml")["jobs"]["scope"]
    output = tmp_path / "ci-output"
    step_outputs = {}
    for step in job["steps"]:
        if step.get("id") == "scope":
            subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=root,
                           env={**os.environ, **env, "BEFORE": before, "WEEKLY_FROZEN": weekly,
                                "GITHUB_OUTPUT": str(output)}, check=True, capture_output=True, text=True)
            step_outputs["scope"] = dict(line.split("=", 1) for line in output.read_text().splitlines())
        if step.get("id") == "docs" and env["EVENT_NAME"] == "push":
            step_outputs["docs"] = {"decision": docs_scope.push_scope(env)}

    class JobOutputs(ScopeOutputs):
        def lookup(self, path):
            if path[0] == "steps":
                return step_outputs.get(path[1], {}).get(path[3], "")
            return super().lookup(path)

    evaluator = JobOutputs(env["EVENT_NAME"], {})
    resolved = {name: evaluator.run(condition_text(job["outputs"][name]))
                for name in ("python", "rust_only", "include_frozen")}
    return {name: value for name, value in resolved.items() if value}, step_outputs.get("docs", {}).get("decision")


@pytest.mark.parametrize("case, expected", [
    ("merge", "docs"),
    ("direct", "docs"),
    ("multiple-shallow", "docs"),
    ("rust-multiple-shallow", "other"),
    ("rust-only-push", "other"),
    ("rust-plan-api-failure", "unknown"),
    ("rust-plan-docs", "docs"),
    ("python", "other"),
    ("workflow", "other"),
    ("census", "other"),
    ("earlier-code", "other"),
    ("batch-passed", "other"),
    ("batch-different-tree", "other"),
    ("merge-api-empty", "unknown"),
    ("multiple-associated-prs", "unknown"),
    ("wrong-pr-base", "unknown"),
    ("wrong-pr-merge", "unknown"),
    ("unmerged-pr", "unknown"),
    ("api-failure", "unknown"),
    ("bad-api-shape", "unknown"),
    ("missing-before", "unknown"),
    ("equal-endpoints", "unknown"),
    ("wrong-github-sha", "unknown"),
    ("force-zero-commits", "unknown"),
    ("missing-shallow-base", "unknown"),
    ("symlink-added", "other"),
    ("symlink-deleted", "other"),
    ("gitlink-added", "other"),
])
def test_master_push_lanes(repo, tmp_path, monkeypatch, case, expected):
    root, before, commit = repo
    paths = ["docs/example.md"]
    if case == "rust-only-push":
        paths = ["rust/first.rs"]
    if case.startswith("rust-plan-"):
        # A real plan path receives the legacy Rust-only verdict.
        paths = [min(docs_scope.RUST_PLAN_DOCS)]
    mixed = {"python": "helper.py", "workflow": ".github/workflows/rust.yml",
             "census": "rust/producer-census.json"}
    if case in mixed:
        paths.append(mixed[case])
    if case in ("multiple-shallow", "earlier-code"):
        commit("docs/overview.md" if case == "multiple-shallow" else "helper.py")
    if case == "rust-multiple-shallow":
        commit("rust/first.rs")
        paths = ["rust/second.rs"]
    merge = case in ("merge", "batch-passed", "batch-different-tree", "merge-api-empty",
                     "multiple-associated-prs", "wrong-pr-base", "wrong-pr-merge", "unmerged-pr")
    if merge:
        docs_scope.git("switch", "-q", "-c", "docs-topic")
        commit(*paths)
        docs_scope.git("switch", "-q", "master")
        # This code was on master before the push. Comparing the merge to
        # the docs branch parent would incorrectly include it.
        before = commit("prior.py")
        docs_scope.git("merge", "-q", "--no-ff", "-m", "merge docs", "docs-topic")
        after = docs_scope.git("rev-parse", "HEAD").strip()
        assert docs_scope.git("rev-parse", "HEAD^1").strip() == before
    elif case.startswith("symlink-"):
        doc = root / paths[0]
        doc.parent.mkdir()
        doc.symlink_to("../initial.txt")
        docs_scope.git("add", "--", paths[0])
        docs_scope.git("commit", "-qm", "add docs symlink")
        if case == "symlink-deleted":
            before = docs_scope.git("rev-parse", "HEAD").strip()
            docs_scope.git("rm", "--", paths[0])
            docs_scope.git("commit", "-qm", "remove docs symlink")
        after = docs_scope.git("rev-parse", "HEAD").strip()
    elif case == "gitlink-added":
        docs_scope.git("update-index", "--add", "--cacheinfo", f"160000,{before},{paths[0]}")
        docs_scope.git("commit", "-qm", "add docs gitlink")
        after = docs_scope.git("rev-parse", "HEAD").strip()
    else:
        after = commit(*paths)

    if case in ("multiple-shallow", "rust-multiple-shallow"):
        shallow = tmp_path / "shallow"
        docs_scope.git("clone", "-q", "--depth=2", root.as_uri(), str(shallow))
        monkeypatch.setattr(docs_scope, "ROOT", shallow)
        root = shallow
        with pytest.raises(subprocess.CalledProcessError):
            docs_scope.git("cat-file", "-e", f"{before}^{{commit}}")

    event = tmp_path / "event.json"
    payload = {"ref": MASTER, "before": before, "after": after, "forced": False}
    if case == "missing-before":
        del payload["before"]
    if case == "equal-endpoints":
        payload["before"] = after
    if case == "force-zero-commits":
        payload.update(forced=True, before="0" * 40, commits=[])
    if case == "missing-shallow-base":
        payload["before"] = "2" * 40
    event.write_text(json.dumps(payload), encoding="utf-8")
    env = {"EVENT_NAME": "push", "HEAD_REF": "docs/topic",
           "GITHUB_EVENT_NAME": "push", "GITHUB_REF": MASTER,
           "GITHUB_SHA": before if case == "wrong-github-sha" else after,
           "GITHUB_EVENT_PATH": str(event), "GITHUB_TOKEN": "fixture",
           "GITHUB_API_URL": "https://api.example.com", "GITHUB_REPOSITORY": "example/repo"}

    batch_head = "a" * 40
    workflow_path = ".github/workflows/ci.yml"
    batch_ref = "integrate/fixture"
    pulls = [{"number": 1, "base": {"ref": "master"}, "merged_at": "2026-11-18T00:00:00Z",
              "merge_commit_sha": after, "head": {"ref": batch_ref if case.startswith("batch-") else "docs-topic",
                                                "sha": batch_head, "repo": {"full_name": "example/repo"}}}] if merge else []
    if case == "multiple-associated-prs":
        pulls.append({**pulls[0], "number": 2})
    if case == "wrong-pr-base":
        pulls[0]["base"]["ref"] = "topic"
    if case == "wrong-pr-merge":
        pulls[0]["merge_commit_sha"] = before
    if case == "unmerged-pr":
        pulls[0]["merged_at"] = None

    def get(path):
        if path.endswith("/pulls"):
            if case in ("api-failure", "rust-plan-api-failure"):
                raise ci_batch_passed.ApiError("HTTP 503")
            if case == "merge-api-empty":
                return []
            return {"unexpected": []} if case == "bad-api-shape" else pulls
        if path.endswith(f"/git/commits/{batch_head}"):
            return {"tree": {"sha": "different" if case == "batch-different-tree" else "same"}}
        if path.endswith(f"/git/commits/{after}"):
            return {"tree": {"sha": "same"}}
        if "/actions/workflows/ci.yml/runs?" in path:
            return {"workflow_runs": [{"path": workflow_path, "event": "pull_request", "head_sha": batch_head,
                                       "head_branch": batch_ref, "head_repository": {"full_name": "example/repo"},
                                       "status": "completed", "id": 1, "run_attempt": 1}]}
        assert "/actions/runs/1/attempts/1/jobs?" in path
        return {"total_count": 1, "jobs": [{"name": "test", "head_sha": batch_head,
                                           "status": "completed", "conclusion": "success"}]}

    reports = []
    monkeypatch.setattr(docs_scope, "github_get", lambda token, url: get)
    monkeypatch.setattr(docs_scope, "report_census", reports.append)
    ci, push = ci_outputs(root, tmp_path, env, payload.get("before", ""))
    assert push == expected
    passed, _ = ci_batch_passed.decide(get, repo="example/repo", event="push",
                                       ref=MASTER, sha=after, workflow=workflow_path)
    ci["batch_passed"] = json.dumps(passed)
    full, rust = docs_scope.rust_scope(env)
    rust_outputs = {"full": str(full).lower(), "rust": str(rust).lower()}

    fast = {"lock-resolve", "frontend"}
    python_checks = {"test", "test-lite-windows", "test-lite-macos"}
    rust_only = case in ("rust-only-push", "rust-plan-api-failure")
    ci_expected = fast | ({"guards"} if expected == "docs" or rust_only else python_checks)
    if case == "batch-passed":
        ci_expected.remove("test")
    assert selected_lanes("ci.yml", CI_JOBS, "push", ci) == ci_expected
    assert ci.get("rust_only") == ("true" if rust_only else None)
    assert selected_lanes("rust.yml", RUST_JOBS, "push", rust_outputs) == (
        set() if expected == "docs" else set(RUST_JOBS))
    assert reports == ([env] if expected == "docs" else [])
    if case in ("missing-shallow-base", "rust-multiple-shallow"):
        assert ci["include_frozen"] == "true"


def test_rust_only_batch_pull_request_keeps_its_reader_guards(repo, tmp_path):
    root, before, commit = repo
    commit("rust/first.rs")
    env = {"EVENT_NAME": "pull_request", "HEAD_REF": "integrate/fixture"}
    ci, _ = ci_outputs(root, tmp_path, env, before)
    assert ci == {"python": "false", "rust_only": "true"}
    assert selected_lanes("ci.yml", CI_JOBS, "pull_request", ci) == {"guards", "lock-resolve", "frontend"}


@pytest.mark.parametrize("weekly", ["", "true"])
def test_scheduled_scope_keeps_the_full_lanes(repo, tmp_path, weekly):
    root, before, _ = repo
    env = {"EVENT_NAME": "schedule"}
    ci, _ = ci_outputs(root, tmp_path, env, before, weekly=weekly)
    assert ci == ({"python": "true", "include_frozen": "true"} if weekly else {"python": "true"})
    assert selected_lanes("ci.yml", CI_JOBS, "schedule", ci) == set(CI_JOBS) - {"guards"}
    assert docs_scope.rust_scope(env) == (True, True)
    assert selected_lanes("rust.yml", RUST_JOBS, "schedule", {"full": "true", "rust": "true"}) == set(RUST_JOBS)


@pytest.mark.parametrize("generated_census", [False, True])
def test_ordinary_docs_pull_request_keeps_the_fast_lanes(repo, tmp_path, generated_census):
    root, before, commit = repo
    paths = ["docs/example.md"]
    if generated_census:
        paths.append(docs_scope.CENSUS)
    commit(*paths)
    env = {"EVENT_NAME": "pull_request", "HEAD_REF": "docs/example"}
    ci, _ = ci_outputs(root, tmp_path, env, before)
    assert selected_lanes("ci.yml", CI_JOBS, "pull_request", ci) == {"guards", "lock-resolve", "frontend"}
    full, rust = docs_scope.rust_scope(env)
    assert (full, rust) == (False, False)
    assert not selected_lanes("rust.yml", RUST_JOBS, "pull_request", {"full": "false", "rust": "false"})


@pytest.mark.parametrize("checks, reuse", [
    ([], False),
    ([RUST_CHECKS[0]], False),
    (list(RUST_CHECKS), True),
])
def test_rust_nightly_only_reuses_a_push_that_ran_rust(checks, reuse):
    steps = workflow("rust.yml")["jobs"]["scope"]["steps"]
    command = shlex.split(next(step for step in steps if step.get("id") == "push")["run"])
    required = tuple(command[index + 1] for index, arg in enumerate(command) if arg == "--require-job-passed")
    assert required == RUST_CHECKS
    sha = "a" * 40
    workflow_path = ".github/workflows/rust.yml"
    run = {"id": 1, "head_sha": sha, "head_branch": "master", "path": workflow_path, "event": "push",
           "head_repository": {"full_name": "example/repo"}, "status": "completed", "conclusion": "success"}

    def get(path):
        if "/jobs?" in path:
            return {"jobs": [{"name": name, "conclusion": "success"} for name in checks]}
        return {"workflow_runs": [run]}

    passed, _ = ci_push_passed.decide(get, repo="example/repo", event="schedule", ref=MASTER,
                                     sha=sha, workflow=workflow_path, require_passed=required)
    assert passed is reuse


def test_census_drift_is_visible_in_the_push_summary(tmp_path, monkeypatch):
    message = "producer census is stale (report only); regenerate before using it in a port slice\n"
    summary = tmp_path / "summary"
    env = {"GITHUB_STEP_SUMMARY": str(summary)}

    def census(args, **kwargs):
        assert args[-1] == "--ci-check"
        return message

    monkeypatch.setattr(subprocess, "check_output", census)
    docs_scope.report_census(env)
    assert summary.read_text(encoding="utf-8") == f"### Producer census\n\n{message}\n"


def test_a_census_reporting_error_keeps_the_rust_lanes(monkeypatch):
    def broken_report(_):
        raise subprocess.CalledProcessError(1, "producer_census.py")

    monkeypatch.setattr(docs_scope, "push_scope", lambda _: "docs")
    monkeypatch.setattr(docs_scope, "report_census", broken_report)
    assert docs_scope.rust_scope({"EVENT_NAME": "push"}) == (True, True)
