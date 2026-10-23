"""Which jobs a master push may skip: the ones its merged batch already passed.

Every change reaches master through an integration batch
(docs/dev/merge-batches.md): a pull request from an ``integrate/*`` branch
whose CI passes on its head, then ``ops/integrate.py merge`` merges it with a
merge commit, and only while master is the batch's base or an ancestor of its
head. That merge commit's tree is the batch head's tree, so the master push
re-ran jobs on code that had just passed them.

On a push to master this names the jobs of one workflow that passed on the
merged batch's latest run, when every check below holds; anything else names
none, so the push runs everything as before:

- the event is a push to refs/heads/master;
- GitHub associates exactly one pull request with the pushed commit, merged,
  with that commit as its merge commit, from an ``integrate/`` branch of this
  repository into master;
- the batch head's tree is the pushed commit's tree;
- the newest pull_request run of the same workflow file on that head (and
  branch) is completed;
- the job, in that run's latest attempt, is listed once, completed with
  conclusion ``success``, on that head. ``skipped`` is not ``success``: a
  Rust-only batch skips ci.yml's Python lanes, and its master push still runs
  them.

Any API error, unexpected response or failed check names no job and prints one
line saying why. The workflows decide what a name means (a job, or a parity
matrix pair through rust/ci/parity_scope.py) and which jobs may never be
skipped.

Standard library only: the scope jobs run it with the runner's python3.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from pathlib import PurePosixPath

MASTER = "refs/heads/master"
BATCH_PREFIX = "integrate/"
PER_PAGE = 100
TIMEOUT_SECONDS = 30
API_VERSION = "2022-11-28"


class ApiError(Exception):
    """A GitHub API request that did not return JSON."""


class NotABatchPass(Exception):
    """One of the checks failed; the message says which."""


Get = Callable[[str], object]


def github_get(token: str, api_url: str) -> Get:
    """A GET helper for the REST API, authenticated with the job's token."""
    base = api_url.rstrip("/")

    def get(path: str) -> object:
        request = urllib.request.Request(base + path, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        })
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ApiError(f"HTTP {error.code} for {path}") from error

    return get


def _require(condition: object, reason: str) -> None:
    if not condition:
        raise NotABatchPass(reason)


def _tree(get: Get, repo: str, sha: str) -> str:
    commit = get(f"/repos/{repo}/git/commits/{sha}")
    _require(isinstance(commit, dict) and isinstance(commit.get("tree"), dict),
             f"no commit object for {sha[:12]}")
    tree = commit["tree"].get("sha")
    _require(isinstance(tree, str) and tree, f"no tree for {sha[:12]}")
    return tree


def _batch_pull(get: Get, repo: str, sha: str) -> dict:
    pulls = get(f"/repos/{repo}/commits/{sha}/pulls")
    _require(isinstance(pulls, list), "the associated pull requests could not be read")
    _require(len(pulls) == 1, f"{len(pulls)} pull requests are associated with {sha[:12]}, not one")
    pull = pulls[0]
    _require(isinstance(pull, dict), "the associated pull request could not be read")
    number = pull.get("number")
    head, base = pull.get("head"), pull.get("base")
    _require(isinstance(head, dict) and isinstance(base, dict), f"#{number} has no head or base")
    _require(pull.get("merged_at"), f"#{number} is not merged")
    _require(pull.get("merge_commit_sha") == sha, f"#{number}'s merge commit is not {sha[:12]}")
    ref = head.get("ref")
    _require(isinstance(ref, str) and ref.startswith(BATCH_PREFIX) and len(ref) > len(BATCH_PREFIX),
             f"#{number}'s head branch {ref!r} is not an {BATCH_PREFIX} batch")
    head_repo = head.get("repo")
    _require(isinstance(head_repo, dict) and head_repo.get("full_name") == repo,
             f"#{number}'s head is a fork or a deleted repository, not {repo}")
    _require(base.get("ref") == "master", f"#{number}'s base is {base.get('ref')!r}, not master")
    _require(isinstance(head.get("sha"), str) and head["sha"], f"#{number} has no head commit")
    return pull


def _batch_run(get: Get, repo: str, workflow: str, branch: str, head_sha: str) -> dict:
    name = PurePosixPath(workflow).name
    query = urllib.parse.urlencode({"event": "pull_request", "head_sha": head_sha, "per_page": 100})
    listing = get(f"/repos/{repo}/actions/workflows/{name}/runs?{query}")
    _require(isinstance(listing, dict) and isinstance(listing.get("workflow_runs"), list),
             "the batch's workflow runs could not be read")
    runs = [run for run in listing["workflow_runs"]
            if isinstance(run, dict) and run.get("path") == workflow and run.get("event") == "pull_request"
            and run.get("head_sha") == head_sha and run.get("head_branch") == branch]
    _require(runs, f"no completed {workflow} run of the batch on {head_sha[:12]}")
    newest = max(runs, key=lambda run: (str(run.get("created_at", "")), int(run.get("id") or 0)))
    _require(newest.get("status") == "completed",
             f"the batch's newest {workflow} run {newest.get('id')} is not completed")
    _require(isinstance(newest.get("id"), int) and isinstance(newest.get("run_attempt"), int),
             "the batch's run has no id or attempt")
    return newest


def _jobs(get: Get, repo: str, run: dict) -> list[dict]:
    """Every job of the run's latest attempt, across pages."""
    path = f"/repos/{repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs"
    jobs: list[dict] = []
    page = 1
    while True:
        listing = get(f"{path}?per_page={PER_PAGE}&page={page}")
        _require(isinstance(listing, dict) and isinstance(listing.get("jobs"), list)
                 and isinstance(listing.get("total_count"), int), "the batch's jobs could not be read")
        jobs.extend(listing["jobs"])
        if len(jobs) >= listing["total_count"]:
            return jobs
        _require(listing["jobs"], f"the batch's jobs listing ended at {len(jobs)} of {listing['total_count']}")
        page += 1


def decide(get: Get, *, repo: str, event: str, ref: str, sha: str, workflow: str) -> tuple[list[str], str]:
    """The job names that passed on the merged batch, sorted, and why."""
    if event != "push" or ref != MASTER:
        return [], f"not a master push ({event} on {ref})"
    try:
        pull = _batch_pull(get, repo, sha)
        number, head_sha = pull["number"], pull["head"]["sha"]
        _require(_tree(get, repo, head_sha) == _tree(get, repo, sha),
                 f"#{number}'s head {head_sha[:12]} and {sha[:12]} have different trees")
        run = _batch_run(get, repo, workflow, pull["head"]["ref"], head_sha)
        jobs = [job for job in _jobs(get, repo, run) if isinstance(job, dict)]
    except NotABatchPass as reason:
        return [], str(reason)
    except (ApiError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return [], f"the GitHub API check failed ({type(error).__name__}: {error})"
    counts = Counter(job.get("name") for job in jobs)
    passed = sorted({job["name"] for job in jobs
                     if isinstance(job.get("name"), str) and counts[job["name"]] == 1
                     and job.get("status") == "completed" and job.get("conclusion") == "success"
                     and job.get("head_sha") == head_sha})
    where = (f"{pull['head']['ref']}, head {head_sha[:12]}, run {run['id']} attempt {run['run_attempt']}, "
             f"the tree of {sha[:12]}")
    if not passed:
        return [], f"no job passed on batch #{number} ({where})"
    return passed, f"#{number} passed on the same tree ({where})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflow", required=True, help="this workflow's file, .github/workflows/<name>.yml")
    parser.add_argument("--github-output", required=True)
    options = parser.parse_args(argv)
    passed: list[str] = []
    try:
        env = os.environ
        token = env.get("GITHUB_TOKEN", "")
        if not token:
            reason = "no GITHUB_TOKEN"
        else:
            get = github_get(token, env.get("GITHUB_API_URL") or "https://api.github.com")
            passed, reason = decide(get, repo=env.get("GITHUB_REPOSITORY", ""),
                                    event=env.get("GITHUB_EVENT_NAME", ""), ref=env.get("GITHUB_REF", ""),
                                    sha=env.get("GITHUB_SHA", ""), workflow=options.workflow)
    except Exception as error:  # noqa: BLE001 - any failure must run everything, never fail scope
        passed, reason = [], f"the check itself failed ({type(error).__name__}: {error})"
    if passed:
        print(f"batch: skipping {len(passed)} job(s) batch {reason}: {', '.join(passed)}")
    else:
        print(f"batch: running every job: {reason}")
    with open(options.github_output, "a", encoding="utf-8") as output:
        output.write(f"passed={json.dumps(passed, separators=(',', ':'))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
