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


def decide(get: Get, *, repo: str, event: str, ref: str, sha: str, workflow: str) -> tuple[bool, str]:
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
                return True, f"{workflow} push run {run.get('id')} passed on {sha}"
    except (ApiError, OSError, ValueError) as error:
        return False, f"GitHub check failed ({type(error).__name__})"
    return False, f"no successful {workflow} master push on {sha}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--github-output", required=True)
    options = parser.parse_args(argv)
    passed, reason = False, "no GITHUB_TOKEN"
    try:
        env = os.environ
        if env.get("GITHUB_TOKEN"):
            passed, reason = decide(github_get(env["GITHUB_TOKEN"], env.get("GITHUB_API_URL") or
                                             "https://api.github.com"),
                                    repo=env.get("GITHUB_REPOSITORY", ""),
                                    event=env.get("GITHUB_EVENT_NAME", ""), ref=env.get("GITHUB_REF", ""),
                                    sha=env.get("GITHUB_SHA", ""), workflow=options.workflow)
    except Exception as error:  # noqa: BLE001 - an unavailable check must never suppress coverage
        passed, reason = False, f"check failed ({type(error).__name__})"
    print(f"nightly: {'reuse push coverage' if passed else 'run all coverage'}: {reason}")
    with open(options.github_output, "a", encoding="utf-8") as output:
        output.write(f"passed={str(passed).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
