"""Skip repeated nightly coverage after the same workflow passed on this master SHA.

Nightly-only evidence and its build prerequisites stay in the workflows.
Any missing evidence or API error leaves all jobs enabled. Standard library
only, so scope needs no dependency install.
"""
from __future__ import annotations

import argparse
import os
from pathlib import PurePosixPath
from urllib.parse import urlencode

from ops.ci_batch_passed import ApiError, Get, github_get


def decide(get: Get, *, repo: str, event: str, ref: str, sha: str, workflow: str,
           not_if_passed: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Whether this nightly may reuse a successful master push of ``workflow``
    on ``sha``. Not when one of ``not_if_passed`` succeeded in that run: in
    ci.yml, `guards` runs exactly when scope skipped the Python lanes (a
    Rust-only batch's push, since the Python freeze), and such a run still
    concludes success. A lane skipped because the batch had passed it on the
    same tree is covered, so an ordinary batch push stays reusable."""
    if event != "schedule" or ref != "refs/heads/master":
        return False, "not a scheduled master run"
    query = urlencode({"event": "push", "head_sha": sha, "branch": "master",
                       "status": "success", "per_page": 100})
    try:
        listing = get(f"/repos/{repo}/actions/workflows/{PurePosixPath(workflow).name}/runs?{query}")
        if not isinstance(listing, dict) or not isinstance(listing.get("workflow_runs"), list):
            return False, "workflow runs could not be read"
        for run in listing["workflow_runs"]:
            if (isinstance(run, dict) and run.get("path") == workflow
                    and run.get("event") == "push" and run.get("head_sha") == sha
                    and run.get("head_branch") == "master"
                    and isinstance(run.get("head_repository"), dict)
                    and run["head_repository"].get("full_name") == repo
                    and run.get("status") == "completed" and run.get("conclusion") == "success"):
                ran = _passed_jobs(get, repo, run.get("id"), not_if_passed)
                if ran:
                    return False, (f"{workflow} push run {run.get('id')} ran {', '.join(ran)}, so it "
                                   "skipped lanes this nightly runs")
                return True, f"{workflow} push run {run.get('id')} passed on {sha}"
    except (ApiError, OSError, ValueError) as error:
        return False, f"GitHub check failed ({type(error).__name__})"
    return False, f"no successful {workflow} master push on {sha}"


def _passed_jobs(get: Get, repo: str, run_id, names: tuple[str, ...]) -> list[str]:
    """Which of ``names`` concluded success in the run (all of them when its
    job listing cannot be read, so an unreadable run is never reused)."""
    if not names:
        return []
    listing = get(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    jobs = listing.get("jobs") if isinstance(listing, dict) else None
    if not isinstance(jobs, list):
        return list(names)
    passed = {job.get("name") for job in jobs if isinstance(job, dict) and job.get("conclusion") == "success"}
    return [name for name in names if name in passed]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--github-output", required=True)
    parser.add_argument("--not-if-job-passed", action="append", default=[],
                        help="a job whose success in the push run means it skipped lanes the nightly runs")
    options = parser.parse_args(argv)
    passed, reason = False, "no GITHUB_TOKEN"
    try:
        env = os.environ
        if env.get("GITHUB_TOKEN"):
            passed, reason = decide(github_get(env["GITHUB_TOKEN"], env.get("GITHUB_API_URL") or
                                             "https://api.github.com"),
                                    repo=env.get("GITHUB_REPOSITORY", ""),
                                    event=env.get("GITHUB_EVENT_NAME", ""), ref=env.get("GITHUB_REF", ""),
                                    sha=env.get("GITHUB_SHA", ""), workflow=options.workflow,
                                    not_if_passed=tuple(options.not_if_job_passed))
    except Exception as error:  # noqa: BLE001 - an unavailable check must never suppress coverage
        passed, reason = False, f"check failed ({type(error).__name__})"
    print(f"nightly: {'reuse push coverage' if passed else 'run all coverage'}: {reason}")
    with open(options.github_output, "a", encoding="utf-8") as output:
        output.write(f"passed={str(passed).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
