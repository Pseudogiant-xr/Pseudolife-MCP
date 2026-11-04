"""Verify stress retries against the workflow run's job-attempt history.

One failed setup may be replaced per iteration. Once the selection step has
started, any failure requires a new loop. API errors and incomplete history
never turn mixed-attempt artifacts into clean evidence.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

TEST_STEP = "Run the selection once"
JOB = re.compile(r"Stress / (windows-latest|ubuntu-latest) / ([1-9][0-9]*)")


def classify(job: dict) -> tuple[str, str]:
    """A completed job's outcome, using step states rather than error text."""
    steps = job.get("steps")
    if job.get("status") != "completed" or not isinstance(steps, list):
        return "unverified", "incomplete job or step history"
    if any(not isinstance(s, dict) or not isinstance(s.get("name"), str) for s in steps):
        return "unverified", "malformed step history"
    tests = [s for s in steps if s["name"] == TEST_STEP]
    failed = [s["name"] for s in steps if s.get("conclusion") in ("failure", "timed_out", "cancelled")]
    if len(tests) == 1:
        test = tests[0]
        index = steps.index(test)
        after_clean = all(s.get("status") == "completed" and s.get("conclusion") in ("success", "skipped")
                          for s in steps[index + 1:])
        if (job.get("conclusion") == "success" and test.get("status") == "completed"
                and test.get("conclusion") == "success" and after_clean):
            return "pass", "passed"
        before = [s["name"] for s in steps[:index] if s.get("status") == "completed"
                  and s.get("conclusion") in ("failure", "timed_out")]
        if (job.get("conclusion") in ("failure", "timed_out", "startup_failure")
                and test.get("status") == "completed" and test.get("conclusion") == "skipped"
                and before and after_clean):
            return "setup_failure", "setup failure: " + ", ".join(before)
    elif not tests and not steps and job.get("conclusion") in ("failure", "timed_out", "startup_failure"):
        return "setup_failure", f"{job['conclusion']}: no steps ran"
    return "failure", "test or unverified failure: " + (", ".join(failed) or str(job.get("conclusion")))


def assess(document, rows: list[dict], os_name: str = "windows-latest") -> dict:
    result = {"clean": False, "retried": [], "jobs": [], "reason": "unavailable or malformed job history"}
    if (not isinstance(document, dict) or document.get("error") or not isinstance(document.get("jobs"), list)
            or type(document.get("run_id")) is not int or type(document.get("run_attempt")) is not int
            or not isinstance(document.get("head_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", document["head_sha"])):
        return result
    grouped, ids = {}, set()
    for job in document["jobs"]:
        if not isinstance(job, dict):
            return result
        match = JOB.fullmatch(str(job.get("name", "")))
        attempt, job_id = job.get("run_attempt"), job.get("id")
        if (not match or match[1] != os_name or job.get("run_id") != document["run_id"]
                or job.get("head_sha") != document["head_sha"] or type(attempt) is not int
                or not 1 <= attempt <= document["run_attempt"] or type(job_id) is not int or job_id in ids):
            return result
        ids.add(job_id)
        iteration = int(match[2])
        outcome, cause = classify(job)
        record = {"iteration": iteration, "attempt": attempt, "job_id": job_id, "outcome": outcome, "cause": cause}
        result["jobs"].append(record)
        grouped.setdefault(iteration, []).append(record)
    result["jobs"].sort(key=lambda item: (item["iteration"], item["attempt"]))
    if set(grouped) != {row["iteration"] for row in rows}:
        result["reason"] = "job history does not cover exactly the planned iterations"
        return result
    for row in rows:
        attempts = sorted(grouped[row["iteration"]], key=lambda item: item["attempt"])
        if attempts[0]["attempt"] != 1:
            result["reason"] = f"iteration {row['iteration']} has no original attempt evidence"
            return result
        last = attempts[-1]
        if (row.get("outcome") != "pass" or last["outcome"] != "pass"
                or last["attempt"] != row.get("attempt")):
            result["reason"] = f"iteration {row['iteration']} has no matching successful test execution"
            return result
        if len(attempts) == 2 and attempts[0]["outcome"] == "setup_failure" and attempts[0]["attempt"] < last["attempt"]:
            result["retried"].append(row["iteration"])
        elif len(attempts) != 1:
            result["reason"] = f"iteration {row['iteration']} retried a test execution or retried setup more than once"
            return result
    result.update(clean=bool(rows), reason="every selection passed; only one verified setup retry per iteration")
    return result


def collect(get, run_id: int, attempt: int, head_sha: str) -> dict:
    """Read every attempt, excluding jobs carried over from earlier attempts."""
    jobs, known = [], {}
    for number in range(1, attempt + 1):
        page, seen, total = 1, 0, None
        while True:
            listing = get(f"/actions/runs/{run_id}/attempts/{number}/jobs?per_page=100&page={page}")
            if (not isinstance(listing, dict) or not isinstance(listing.get("jobs"), list)
                    or type(listing.get("total_count")) is not int or listing["total_count"] < 0):
                raise ValueError("unreadable job listing")
            if total is None:
                total = listing["total_count"]
            elif total != listing["total_count"]:
                raise ValueError("job count changed during pagination")
            batch = listing["jobs"]
            for job in batch:
                if (not isinstance(job, dict) or not isinstance(job.get("name"), str)
                        or type(job.get("id")) is not int or type(job.get("run_attempt")) is not int
                        or not 1 <= job["run_attempt"] <= number
                        or job.get("run_id") != run_id or job.get("head_sha") != head_sha):
                    raise ValueError("malformed or mismatched job listing")
                job_id = job["id"]
                if job["run_attempt"] < number:
                    if known.get(job_id) != job:
                        raise ValueError("carried job has no matching earlier attempt")
                    continue
                if job_id in known:
                    raise ValueError("duplicate job in attempt history")
                known[job_id] = job
                if JOB.fullmatch(job["name"]):
                    jobs.append(job)
            seen += len(batch)
            if seen > listing["total_count"]:
                raise ValueError("job listing exceeds total_count")
            if seen == listing["total_count"]:
                break
            if not batch:
                raise ValueError("job listing ended before total_count")
            page += 1
    return {"run_id": run_id, "head_sha": head_sha, "run_attempt": attempt, "jobs": jobs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        env = os.environ
        repo = env["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("invalid repository")

        def get(path):
            command = ["gh", "api", f"/repos/{repo}{path}"]
            response = subprocess.run(command, capture_output=True, text=True, check=True)
            return json.loads(response.stdout)

        document = collect(get, int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"]), env["GITHUB_SHA"])
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        # A complete failure document lets report explain an API outage, while
        # rejecting the evidence instead of silently reverting to artifact-only.
        document = {"error": f"job history unavailable ({type(error).__name__})"}
    Path(args.out).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(document.get("error") or f"Read {len(document['jobs'])} stress job attempts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
