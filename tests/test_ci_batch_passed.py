"""ops/ci_batch_passed.py: the jobs a master push may skip because its batch passed them.

A batch merges with a merge commit whose tree is its head's
(``ops/integrate.py merge`` refuses otherwise), so a push of that merge
re-runs code that just passed. The script names the jobs that passed on the
batch pull request's latest completed run of the same workflow, and only
when every check holds; anything else names none, so everything runs. The
GitHub API is replaced by a fake built from the shapes GitHub returned for
batch #912 (head 9dab013d, merged as 2e86d4f3, 2026-10-23); no test touches
the network.
"""
from __future__ import annotations

import copy
import io
import json
import urllib.error
from pathlib import Path

import pytest
import yaml

from ops import ci_batch_passed as batch

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REPO = "Pseudogiant-xr/Pseudolife-MCP"
MERGE = "2e86d4f35118cd37cd61d29a568a1875daf687a7"
HEAD = "9dab013d9440f6fb0af8e2c5412d47f79db8a198"
TREE = "cdabb73dfc5b5c205da576eb7eb7e19bf11f109b"
BRANCH = "integrate/2026-10-23-batch-5"
RUN = 39949988422
CI = ".github/workflows/ci.yml"
RUST = ".github/workflows/rust.yml"

# Batch #912 was Rust-only: ci.yml's scope skipped the Python suite lanes and
# ran guards instead (run 39949988422, attempt 1, as GitHub listed it).
CI_JOBS = [("frontend", "success"), ("scope", "success"), ("lock-resolve", "success"),
           ("test-windows-nightly", "skipped"), ("guards", "success"),
           ("test-lite-linux", "skipped"), ("test-lite-macos", "skipped"),
           ("test-lite-windows", "skipped"), ("test", "skipped")]
# A slice of its rust.yml run (39949988499): matrix jobs report their
# expanded names, and skipped matrix jobs their unexpanded templates.
RUST_JOBS = [("Scope", "success"), ("Rust / ubuntu-latest", "success"),
             ("Rust / windows-latest", "success"), ("Candidate / ubuntu-latest", "success"),
             ("Rust daemon / disposable image", "success"),
             ("Parity checks / ubuntu-latest / cli", "success"),
             ("Parity checks / windows-latest / cli", "success"),
             ("Parity checks / ubuntu-latest / cli-live", "failure"),
             ("Parity checks / windows-latest / h5-audit", "success"),
             ("Parity live (non-gating) / ${{ matrix.os }} / ${{ matrix.suite }}", "skipped"),
             ("Parity / ubuntu-latest", "success")]


def job(name, conclusion, *, attempt=1, sha=HEAD, status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "run_attempt": attempt, "head_sha": sha}


def jobs_path(run=RUN, attempt=1, page=1):
    return f"/repos/{REPO}/actions/runs/{run}/attempts/{attempt}/jobs?per_page={batch.PER_PAGE}&page={page}"


def runs_path(workflow=CI):
    return (f"/repos/{REPO}/actions/workflows/{Path(workflow).name}/runs"
            f"?event=pull_request&head_sha={HEAD}&per_page=100")


def run(run_id=RUN, *, workflow=CI, status="completed", attempt=1, branch=BRANCH, sha=HEAD,
        created="2026-10-23T11:59:26Z"):
    return {"id": run_id, "path": workflow, "event": "pull_request", "status": status,
            "conclusion": "success" if status == "completed" else None,
            "run_attempt": attempt, "head_branch": branch, "head_sha": sha,
            "created_at": created, "pull_requests": []}


def pull(**changes):
    value = {"number": 912, "state": "closed", "merged_at": "2026-10-23T13:00:12Z",
             "merge_commit_sha": MERGE,
             "head": {"ref": BRANCH, "sha": HEAD, "repo": {"full_name": REPO}},
             "base": {"ref": "master", "repo": {"full_name": REPO}}}
    for key, item in changes.items():
        if isinstance(item, dict):
            value[key] = {**value[key], **item}
        else:
            value[key] = item
    return value


class FakeGitHub:
    """GET path -> parsed JSON; an Exception value is raised instead."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path not in self.routes:
            raise batch.ApiError(f"HTTP 404 for {path}")
        value = self.routes[path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def routes(jobs=CI_JOBS, *, workflow=CI, pulls=None, head_tree=TREE, runs=None, attempt=1):
    return {
        f"/repos/{REPO}/commits/{MERGE}/pulls": [pull()] if pulls is None else pulls,
        f"/repos/{REPO}/git/commits/{MERGE}": {"sha": MERGE, "tree": {"sha": TREE}},
        f"/repos/{REPO}/git/commits/{HEAD}": {"sha": HEAD, "tree": {"sha": head_tree}},
        runs_path(workflow): {"total_count": 1,
                              "workflow_runs": [run(workflow=workflow, attempt=attempt)]
                              if runs is None else runs},
        jobs_path(attempt=attempt): {"total_count": len(jobs),
                                     "jobs": [job(n, c, attempt=attempt) for n, c in jobs]},
    }


def decide(github, workflow=CI, **overrides):
    context = {"repo": REPO, "event": "push", "ref": "refs/heads/master", "sha": MERGE,
               "workflow": workflow, **overrides}
    return batch.decide(github, **context)


def test_a_merged_batch_names_exactly_the_jobs_that_passed_on_its_head():
    github = FakeGitHub(routes())
    passed, reason = decide(github)
    assert passed == ["frontend", "guards", "lock-resolve", "scope"]
    assert "#912" in reason and HEAD[:12] in reason


def test_a_rust_only_batch_leaves_the_python_lanes_it_skipped_to_run():
    # The batch skipped them (Rust-only scope), so the master push is their
    # backstop: skipped is not passed.
    passed, _ = decide(FakeGitHub(routes()))
    for lane in ("test", "test-lite-linux", "test-lite-windows", "test-lite-macos"):
        assert lane not in passed, lane


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "failure", "cancelled", "timed_out",
                                        "action_required", None])
def test_only_success_counts(conclusion):
    passed, _ = decide(FakeGitHub(routes([("test", conclusion), ("frontend", "success")])))
    assert passed == ["frontend"]


def test_a_job_still_running_on_the_batch_does_not_count():
    table = routes()
    table[jobs_path()]["jobs"].append(job("test", None, status="in_progress"))
    table[jobs_path()]["total_count"] += 1
    passed, _ = decide(FakeGitHub(table))
    assert "test" not in passed


def test_matrix_jobs_match_by_their_expanded_name_only():
    passed, _ = decide(FakeGitHub(routes(RUST_JOBS, workflow=RUST)), RUST)
    assert "Parity checks / ubuntu-latest / cli" in passed
    assert "Parity checks / windows-latest / cli" in passed
    assert "Rust / windows-latest" in passed
    # The near miss: cli passed, cli-live beside it failed.
    assert "Parity checks / ubuntu-latest / cli-live" not in passed
    assert not any("${{" in name for name in passed)


def test_a_name_listed_twice_in_one_attempt_is_ambiguous_and_runs():
    table = routes([("test", "success"), ("test", "failure"), ("frontend", "success")])
    assert decide(FakeGitHub(table))[0] == ["frontend"]
    table = routes([("test", "success"), ("test", "success")])
    assert decide(FakeGitHub(table))[0] == []


def test_a_job_from_another_head_does_not_count():
    table = routes()
    table[jobs_path()]["jobs"] = [job("frontend", "success", sha="0" * 39 + "1")]
    assert decide(FakeGitHub(table))[0] == []


def test_a_rerun_uses_the_latest_attempts_jobs():
    # Attempt 1 passed test; attempt 2 (the run's current attempt) failed it.
    table = routes([("test", "failure"), ("frontend", "success")], attempt=2)
    table[jobs_path(attempt=1)] = {"total_count": 2, "jobs": [job("test", "success"),
                                                             job("frontend", "success")]}
    github = FakeGitHub(table)
    assert decide(github)[0] == ["frontend"]
    assert jobs_path(attempt=1) not in github.calls


def test_every_page_of_jobs_is_read(monkeypatch):
    monkeypatch.setattr(batch, "PER_PAGE", 2)
    table = routes()
    names = [("scope", "success"), ("frontend", "success"), ("lock-resolve", "success")]
    table[jobs_path(page=1)] = {"total_count": 3, "jobs": [job(n, c) for n, c in names[:2]]}
    table[jobs_path(page=2)] = {"total_count": 3, "jobs": [job(n, c) for n, c in names[2:]]}
    assert decide(FakeGitHub(table))[0] == ["frontend", "lock-resolve", "scope"]


def test_a_short_jobs_listing_is_an_error():
    table = routes()
    table[jobs_path()]["total_count"] = len(CI_JOBS) + 5
    table[jobs_path(page=2)] = {"total_count": len(CI_JOBS) + 5, "jobs": []}
    passed, reason = decide(FakeGitHub(table))
    assert passed == [] and "jobs" in reason


@pytest.mark.parametrize("event, ref", [("pull_request", "refs/pull/912/merge"),
                                        ("merge_group", "refs/heads/gh-readonly-queue/master/x"),
                                        ("schedule", "refs/heads/master"),
                                        ("workflow_dispatch", "refs/heads/master"),
                                        ("push", "refs/heads/integrate/2026-10-23-batch-5")])
def test_anything_but_a_push_to_master_skips_nothing_and_asks_nothing(event, ref):
    github = FakeGitHub(routes())
    passed, reason = decide(github, event=event, ref=ref)
    assert passed == [] and github.calls == []
    assert "master push" in reason


@pytest.mark.parametrize("pulls", [[], [pull(), pull(number=913)]])
def test_zero_or_two_associated_pull_requests_skip_nothing(pulls):
    passed, reason = decide(FakeGitHub(routes(pulls=pulls)))
    assert passed == [] and f"{len(pulls)} pull requests" in reason


@pytest.mark.parametrize("changes, words", [
    ({"merged_at": None}, "not merged"),
    ({"merge_commit_sha": "1" * 40}, "merge commit"),
    ({"head": {"ref": "claude/some-change"}}, "integrate/"),
    ({"head": {"ref": "integrate"}}, "integrate/"),
    ({"head": {"repo": {"full_name": "someone/Pseudolife-MCP"}}}, "fork"),
    ({"head": {"repo": None}}, "fork"),
    ({"base": {"ref": "release"}}, "master"),
])
def test_a_pull_request_that_is_not_this_batch_merge_skips_nothing(changes, words):
    passed, reason = decide(FakeGitHub(routes(pulls=[pull(**changes)])))
    assert passed == [] and words in reason


def test_a_tree_mismatch_skips_nothing():
    passed, reason = decide(FakeGitHub(routes(head_tree="f" * 40)))
    assert passed == [] and "tree" in reason


@pytest.mark.parametrize("runs, words", [
    ([], "no completed"),
    ([run(status="in_progress")], "not completed"),
    # The newest run decides: an older green run never stands in for it.
    ([run(), run(RUN + 1, status="queued", created="2026-10-23T12:30:00Z")], "not completed"),
    ([run(workflow=RUST)], "no completed"),
    ([run(branch="integrate/2026-10-23-batch-4")], "no completed"),
    ([run(sha="2" * 40)], "no completed"),
])
def test_a_missing_or_unfinished_batch_run_skips_nothing(runs, words):
    passed, reason = decide(FakeGitHub(routes(runs=runs)))
    assert passed == [] and words in reason


def test_the_newest_completed_run_is_used():
    newer = run(RUN + 7, created="2026-10-23T12:30:00Z")
    table = routes(runs=[run(), newer])
    table[jobs_path(run=RUN + 7)] = {"total_count": 1, "jobs": [job("frontend", "success")]}
    assert decide(FakeGitHub(table))[0] == ["frontend"]


@pytest.mark.parametrize("error", [batch.ApiError("HTTP 403 Resource not accessible by integration"),
                                   urllib.error.URLError("timed out"), OSError("reset"),
                                   ValueError("Expecting value"), KeyError("tree")])
@pytest.mark.parametrize("step", ["pulls", "tree", "runs", "jobs"])
def test_any_api_failure_skips_nothing(error, step):
    table = routes()
    path = {"pulls": f"/repos/{REPO}/commits/{MERGE}/pulls",
            "tree": f"/repos/{REPO}/git/commits/{HEAD}",
            "runs": runs_path(), "jobs": jobs_path()}[step]
    table[path] = error
    passed, reason = decide(FakeGitHub(table))
    assert passed == [] and reason


@pytest.mark.parametrize("value", [{"unexpected": "shape"}, "text", [None], [{"number": 912}]])
def test_an_unexpected_response_shape_skips_nothing(value):
    table = routes(pulls=value)
    assert decide(FakeGitHub(table))[0] == []


def environment(tmp_path, **changes):
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/master", "GITHUB_SHA": MERGE,
           "GITHUB_REPOSITORY": REPO, "GITHUB_TOKEN": "fixture-token",
           "GITHUB_API_URL": "https://api.github.invalid"}
    env.update(changes)
    return output, {k: v for k, v in env.items() if v is not None}


def outputs(output):
    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and lines[0].startswith("passed=")
    return json.loads(lines[0].removeprefix("passed="))


def test_main_writes_the_passed_jobs_as_one_line_of_json(tmp_path, monkeypatch, capsys):
    output, env = environment(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    table = routes()
    monkeypatch.setattr(batch, "github_get", lambda token, api: FakeGitHub(table))
    assert batch.main(["--workflow", CI, "--github-output", str(output)]) == 0
    assert outputs(output) == ["frontend", "guards", "lock-resolve", "scope"]
    printed = capsys.readouterr().out
    assert "skipping 4 job(s) batch #912 passed" in printed


def test_main_without_a_token_skips_nothing(tmp_path, monkeypatch, capsys):
    output, env = environment(tmp_path, GITHUB_TOKEN=None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert batch.main(["--workflow", CI, "--github-output", str(output)]) == 0
    assert outputs(output) == []
    assert "running every job: no GITHUB_TOKEN" in capsys.readouterr().out


def test_main_survives_an_http_error_and_skips_nothing(tmp_path, monkeypatch, capsys):
    output, env = environment(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {},
                                     io.BytesIO(b'{"message": "Resource not accessible"}'))

    monkeypatch.setattr(batch.urllib.request, "urlopen", refuse)
    assert batch.main(["--workflow", CI, "--github-output", str(output)]) == 0
    assert outputs(output) == []
    printed = capsys.readouterr().out
    assert printed.count("\n") == 1 and "running every job" in printed and "403" in printed


def test_main_survives_a_bug_and_skips_nothing(tmp_path, monkeypatch, capsys):
    output, env = environment(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def broken(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(batch, "decide", broken)
    assert batch.main(["--workflow", CI, "--github-output", str(output)]) == 0
    assert outputs(output) == []
    assert "running every job" in capsys.readouterr().out


def test_the_request_carries_the_token_and_the_api_version(monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def capture(request, timeout):
        seen["url"], seen["headers"], seen["timeout"] = request.full_url, dict(request.header_items()), timeout
        return Response(b'{"ok": true}')

    monkeypatch.setattr(batch.urllib.request, "urlopen", capture)
    get = batch.github_get("fixture-token", "https://api.github.invalid/")
    assert get(f"/repos/{REPO}/commits/{MERGE}/pulls") == {"ok": True}
    assert seen["url"] == f"https://api.github.invalid/repos/{REPO}/commits/{MERGE}/pulls"
    assert seen["headers"]["Authorization"] == "Bearer fixture-token"
    assert seen["headers"]["Accept"] == "application/vnd.github+json"
    assert seen["headers"]["X-github-api-version"] == "2022-11-28"
    assert 0 < seen["timeout"] <= 60


# The workflows: both scope jobs run the script on a master push only, with
# the least privilege it needs, and never fail because of it.

def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


SCOPE_PERMISSIONS = {"contents": "read", "actions": "read", "pull-requests": "read"}


@pytest.mark.parametrize("name, scope_id", [("ci.yml", "scope"), ("rust.yml", "scope")])
def test_both_scope_jobs_ask_on_a_master_push_only(name, scope_id):
    workflow = load(name)
    assert workflow["permissions"] == {"contents": "read"}
    scope = workflow["jobs"][scope_id]
    assert scope["permissions"] == SCOPE_PERMISSIONS
    for job_id, job in workflow["jobs"].items():
        if job_id != scope_id:
            assert "permissions" not in job, job_id
    assert scope["outputs"]["batch_passed"] == "${{ steps.batch.outputs.passed }}"
    step = next(s for s in scope["steps"] if s.get("id") == "batch")
    assert step["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/master'"
    # A crash leaves the output empty, which runs everything; it must not
    # fail scope, which would fail rust.yml's parity matrix closed.
    assert step["continue-on-error"] is True
    assert step["env"] == {"GITHUB_TOKEN": "${{ github.token }}"}
    assert step["run"].split() == ["python3", "ops/ci_batch_passed.py", "--workflow",
                                   f".github/workflows/{name}", "--github-output", '"$GITHUB_OUTPUT"']
    checkout = next(s for s in scope["steps"] if s.get("uses") == "actions/checkout@v4")
    assert scope["steps"].index(checkout) < scope["steps"].index(step)


PASSED = "fromJSON(needs.scope.outputs.batch_passed || '[]')"
CI_LANES = ("lock-resolve", "test", "test-lite-linux", "test-lite-windows", "test-lite-macos", "frontend")


@pytest.mark.parametrize("lane", CI_LANES)
def test_each_ci_lane_skips_only_on_its_own_batch_pass(lane):
    job = load("ci.yml")["jobs"][lane]
    assert job["needs"] == "scope"
    condition = job["if"]
    # Exact element match on the lane's own job name, so test never matches
    # test-lite-linux; `|| '[]'` keeps a failed or empty scope running it.
    assert f"!contains({PASSED}, '{lane}')" in condition
    assert condition.startswith("${{ !cancelled() && ")
    assert "github.event_name" not in condition


def test_ci_jobs_outside_the_lanes_keep_their_conditions():
    jobs = load("ci.yml")["jobs"]
    assert jobs["guards"]["if"] == "needs.scope.outputs.python == 'false'"
    assert jobs["test-windows-nightly"]["if"] == ("github.event_name == 'schedule' || "
                                                  "github.event_name == 'workflow_dispatch'")
    assert "needs" not in jobs["test-windows-nightly"]


def test_rust_skips_only_the_image_check_and_parity_pairs():
    jobs = load("rust.yml")["jobs"]
    consulted = {job_id for job_id, job in jobs.items() if "batch_passed" in str(job.get("if", ""))}
    assert consulted == {"daemon-image"}
    assert f"!contains({PASSED}, 'Rust daemon / disposable image')" in jobs["daemon-image"]["if"]
    assert jobs["daemon-image"]["name"] == "Rust daemon / disposable image"
    # The Rust job, the candidate build, parity-live and the gate never read
    # it: Rust and the eval and judges shards compare against the event's
    # oracle, which differs between a pull request and a push.
    for job_id in ("rust", "candidate", "parity-live", "parity", "parity-checks"):
        assert "batch_passed" not in json.dumps(jobs[job_id].get("if", "")), job_id
