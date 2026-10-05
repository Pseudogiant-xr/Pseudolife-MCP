"""Capture hosted Actions wall and lane durations using read-only gh calls."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
import re
import subprocess

from .common import controls, distribution, provenance, write_result


def gh(*args):
    return json.loads(subprocess.check_output(["gh", *args], text=True, timeout=60))


def elapsed(start, end):
    value = (datetime.fromisoformat(end.replace("Z", "+00:00")) -
             datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    if value < 0:
        raise ValueError("negative Actions duration")
    return value


def runner_metadata(job=None, metadata_source="Actions Jobs API"):
    """Public job labels are observations; they do not establish hardware."""
    labels = job.get("labels") if job is not None else None
    public = re.compile(r"(?:ubuntu-(?:latest|\d+\.\d+)|windows-(?:latest|\d+)|"
                        r"macos-(?:latest|\d+))(?:-(?:arm|large|xlarge))?\Z")
    return {"labels": [label for label in labels if isinstance(label, str)
                       and (label == "self-hosted" or public.fullmatch(label))]
            if isinstance(labels, list) else None,
            "hardware": None,
            "metadata_source": metadata_source if job is not None else None}


def job_runners(repo, run_id):
    try:
        pages = gh("api", "--paginate", "--slurp",
                   f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    return {job["id"]: runner_metadata(job) for page in pages for job in page["jobs"]}


def summarize(runs):
    lanes = defaultdict(list)
    for run in runs:
        for job in run["jobs"]:
            lanes[job["name"]].append(job["wall_s"])
    same_head = defaultdict(list)
    for run in runs:
        same_head[run["head_sha"]].append([run["job_span_s"]])
    repeated = {head: controls(blocks) for head, blocks in same_head.items() if len(blocks) > 1}
    return {"hosted_wall_s": distribution([r["created_to_last_job_s"] for r in runs]),
            "job_span_s": distribution([r["job_span_s"] for r in runs]),
            "lanes_s": {key: distribution(value) for key, value in sorted(lanes.items())},
            "same_head_controls": repeated, "noise_floor_available": bool(repeated)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--input", help="Previously captured CI artifact; recompute summary without network")
    args = parser.parse_args()
    if not 1 <= args.count <= 30:
        parser.error("count must be 1..30")
    if args.input:
        from pathlib import Path
        captured = json.loads(Path(args.input).read_text(encoding="utf-8"))
        captured["input_provenance"] = captured.get("provenance")
        captured["input_provenance_scope"] = "Historical collector metadata; not runner hardware evidence."
        captured["provenance"] = provenance("ci-collector")
        captured["provenance_scope"] = "collector"
        for run in captured["runs"]:
            for job in run["jobs"]:
                previous = job.get("runner")
                source = "Actions Jobs API" if previous and previous.get("metadata_source") == "Actions Jobs API" else None
                job["runner"] = runner_metadata(previous, source)
        captured["summary"] = summarize(captured["runs"])
        captured.pop("analysis_provenance", None)
        write_result(args.out, captured)
        return
    repo = gh("repo", "view", "--json", "nameWithOwner")["nameWithOwner"]
    listed = gh("run", "list", "--workflow", "ci.yml", "--branch", "master",
                "--status", "success", "--limit", str(args.count), "--json", "databaseId")
    runs = []
    for item in listed:
        run = gh("run", "view", str(item["databaseId"]), "--json",
                 "databaseId,headSha,createdAt,event,conclusion,url,jobs")
        runners = job_runners(repo, run["databaseId"])
        jobs = []
        for job in run["jobs"]:
            if job["conclusion"] == "skipped":
                continue
            steps = [{"name": step["name"], "conclusion": step["conclusion"],
                      "wall_s": elapsed(step["startedAt"], step["completedAt"])}
                     for step in job["steps"] if step["conclusion"] != "skipped"
                     and step.get("startedAt") and step.get("completedAt")]
            jobs.append({"name": job["name"], "conclusion": job["conclusion"],
                         "wall_s": elapsed(job["startedAt"], job["completedAt"]), "steps": steps,
                         "runner": runners.get(job["databaseId"], runner_metadata())})
        starts = [j["startedAt"] for j in run["jobs"] if j["conclusion"] != "skipped"]
        ends = [j["completedAt"] for j in run["jobs"] if j["conclusion"] != "skipped"]
        # Workflow identity comes from the immutable commit's Contents API.
        blob = gh("api", f"repos/{repo}/contents/.github/workflows/ci.yml?ref={run['headSha']}")
        runs.append({"run_id": run["databaseId"], "head_sha": run["headSha"],
                     "workflow_blob_sha": blob["sha"], "url": run["url"],
                     "created_at": run["createdAt"], "event": run["event"],
                     "conclusion": run["conclusion"], "jobs": jobs,
                     "created_to_last_job_s": elapsed(run["createdAt"], max(ends)),
                     "job_span_s": elapsed(min(starts), max(ends))})
        print(json.dumps({"collected_run": run["databaseId"], "lanes": len(jobs)}), flush=True)
    write_result(args.out, {"schema": 1, "status": "hosted-observational-baseline",
                           "provenance": provenance("ci-collector"), "provenance_scope": "collector", "runs": runs,
                           "summary": summarize(runs), "limitations": [
                               "Successful completed master CI runs selected newest-first; revisions vary.",
                               "Created-to-last-job includes queue time; job span separates it.",
                               "Hosted hardware and background load are not controlled.",
                               "Provenance host describes the collector; runner hardware is unknown. Only public job labels are retained.",
                               "No same-head rerun noise floor unless repeated identical heads appear; no speedup claim."]})


if __name__ == "__main__":
    main()
