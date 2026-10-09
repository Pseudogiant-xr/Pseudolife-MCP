"""Collect completed Rust Actions runs, or summarize a saved timing artifact."""

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import statistics
import subprocess


REPOSITORY = "Pseudogiant-xr/Pseudolife-MCP"


def gh(*args):
    return json.loads(subprocess.check_output(["gh", *args], text=True))


def seconds(start, end):
    return (datetime.fromisoformat(end.replace("Z", "+00:00"))
            - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()


def collect(count):
    # A single snapshot, not a poll; never mix pending jobs into the baseline.
    runs = gh("run", "list", "--repo", REPOSITORY, "--workflow", "rust.yml",
              "--status", "completed", "--limit", str(count), "--json",
              "databaseId,headSha,event,conclusion,createdAt,url")
    for run in runs:
        run.update(gh("run", "view", str(run["databaseId"]), "--repo", REPOSITORY,
                      "--json", "jobs,attempt,startedAt"))
        for job in run["jobs"]:
            job.pop("databaseId", None)
            for step in job["steps"]:
                step.pop("number", None)
    return {"repository": REPOSITORY, "workflow": "rust.yml", "runs": runs}


def summarize(artifact):
    jobs, steps = defaultdict(list), defaultdict(list)
    walls = []
    for run in artifact["runs"]:
        if run["conclusion"] != "success":
            continue
        active = [job for job in run["jobs"] if job["conclusion"] == "success"]
        if not active:
            continue
        start = run["startedAt"]
        if run["attempt"] > 1 and seconds(run["createdAt"], start) <= 0:
            raise ValueError("retry has no distinct attempt start")
        if any(seconds(start, job["startedAt"]) < 0 for job in active):
            raise ValueError("job timestamps precede the selected attempt")
        walls.append(seconds(start, max(job["completedAt"] for job in active)))
        for job in active:
            jobs[job["name"]].append(seconds(job["startedAt"], job["completedAt"]))
            for step in job["steps"]:
                if step["conclusion"] == "success":
                    steps[(job["name"], step["name"])].append(seconds(step["startedAt"], step["completedAt"]))
    if not walls:
        raise ValueError("no successful runs to summarize")
    median_rows = lambda items: sorted(
        [{"name": name, "samples": len(values), "median_seconds": statistics.median(values)}
         for name, values in items], key=lambda row: row["median_seconds"], reverse=True)
    return {"completed_runs": len(artifact["runs"]), "successful_runs": len(walls),
            "wall_median_seconds": statistics.median(walls), "wall_min_seconds": min(walls),
            "wall_max_seconds": max(walls), "jobs": median_rows(jobs.items()),
            "steps": median_rows((" / ".join(key), value) for key, value in steps.items())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="summarize offline without GitHub calls")
    parser.add_argument("--collect", type=Path, help="write a new raw timing artifact")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True, help="write the ranked summary")
    args = parser.parse_args()
    if bool(args.input) == bool(args.collect) or args.count < 1:
        parser.error("choose exactly one of --input/--collect; count must be positive")
    artifact = json.loads(args.input.read_text(encoding="utf-8")) if args.input else collect(args.count)
    if args.collect:
        args.collect.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    result = summarize(artifact)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"{result['successful_runs']}/{result['completed_runs']} successful completed runs; "
          f"median wall {result['wall_median_seconds']} seconds")


if __name__ == "__main__":
    main()
