"""The hosted CI budget: what every job a pull request can start costs.

The account runs at most 20 hosted jobs at once, across every workflow and
repository, so a full integration-batch run is bound by its total runner
minutes rather than by its longest job (docs/dev/ci-budget.md). Each change
that adds a job, a shard or a slower step must say so: rust/ci/ci_budget.json
holds, per workflow, the measured minutes of every job a pull request can
reach, and limits on their count, their sum and the longest one.
test_ci_budget.py fails when the workflow and the budget disagree, when a
limit is exceeded, or when a structural rule below is broken.

    python rust/ci/ci_budget.py measure --workflow rust.yml RUN_ID [RUN_ID ...]

rewrites that workflow's measured minutes (each job's median over the runs,
latest attempt only) and leaves the limits alone: raising a limit is a
reviewed edit with its reason beside it.
"""
from __future__ import annotations

import argparse
import fnmatch
import itertools
import json
import re
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUDGET = ROOT / "rust/ci/ci_budget.json"
WORKFLOWS = ROOT / ".github/workflows"
REPOSITORY = "Pseudogiant-xr/Pseudolife-MCP"
# Events that never come from a pull request, a merge group or a push.
UNREACHABLE_EVENTS = {"schedule", "workflow_dispatch"}
EXPRESSION = re.compile(r"\$\{\{\s*([^}]*?)\s*\}\}")


def load(name: str, resolved: bool = True) -> dict:
    """The workflow; resolved, rust.yml's scope-chosen parity matrix expands
    to the full one (rust/ci/parity_scope.py), as a full batch runs it."""
    import yaml
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    if name == "rust.yml" and resolved:
        sys.path.insert(0, str(ROOT / "rust/ci"))
        from parity_scope import resolve
        workflow = resolve(workflow)
    return workflow


def needs_of(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def pull_request_reachable(workflow: dict) -> list[str]:
    """The job ids a pull request, merge group or push can start.

    A job is unreachable when its condition compares the event only for
    equality with schedule or workflow_dispatch, or when it needs an
    unreachable job's success (implicitly, or by ``result == 'success'``).
    """
    jobs = workflow["jobs"]
    unreachable: set[str] = set()
    changed = True
    while changed:
        changed = False
        for job_id, job in jobs.items():
            if job_id in unreachable:
                continue
            condition = str(job.get("if", ""))
            equal = set(re.findall(r"github\.event_name\s*==\s*'(\w+)'", condition))
            unequal = re.search(r"github\.event_name\s*!=", condition)
            event_bound = bool(equal) and equal <= UNREACHABLE_EVENTS and not unequal
            implicit = not re.search(r"\b(always|cancelled|failure)\(\)", condition)
            dependent = any(need in unreachable and (implicit or f"needs.{need}.result == 'success'" in condition)
                            for need in needs_of(job))
            if event_bound or dependent:
                unreachable.add(job_id)
                changed = True
    return [job_id for job_id in jobs if job_id not in unreachable]


def expand(job_id: str, job: dict) -> list[str]:
    """The names GitHub gives this job's runs: its matrix, minus excludes,
    plus includes (an include matching no combination adds one of its own)."""
    name = job.get("name", job_id)
    matrix = (job.get("strategy") or {}).get("matrix")
    if not matrix:
        return [name]
    axes = {key: value for key, value in matrix.items() if key not in ("include", "exclude")}
    for key, value in axes.items():
        if not isinstance(value, list):
            raise ValueError(f"{job_id}: matrix axis {key} is not a static list ({value!r})")
    combos = [dict(zip(axes, values)) for values in itertools.product(*axes.values())]
    for rule in matrix.get("exclude") or []:
        combos = [combo for combo in combos if not all(combo.get(k) == v for k, v in rule.items())]
    for entry in matrix.get("include") or []:
        matched = [combo for combo in combos if all(combo.get(k, v) == v for k, v in entry.items() if k in axes)]
        for combo in matched:
            combo.update(entry)
        if not matched:
            combos.append(dict(entry))

    def render(combo: dict) -> str:
        return EXPRESSION.sub(lambda m: str(combo.get(m.group(1).removeprefix("matrix."), m.group(0)))
                              if m.group(1).startswith("matrix.") else m.group(0), name)
    return [render(combo) for combo in combos]


def reachable_names(workflow: dict) -> list[str]:
    return [name for job_id in pull_request_reachable(workflow)
            for name in expand(job_id, workflow["jobs"][job_id])]


def critical_path(workflow: dict, measured: dict) -> tuple[float, list[str]]:
    """The longest chain of measured job minutes through ``needs`` among
    the jobs a pull request can start, queueing left out: what a run takes
    with a free slot for every job. A job's minutes are its slowest
    expansion's; an unmeasured one counts as zero (problems() names it)."""
    jobs = workflow["jobs"]
    reachable = pull_request_reachable(workflow)
    memo: dict[str, tuple[float, list[str]]] = {}

    def finish(job_id: str) -> tuple[float, list[str]]:
        if job_id not in memo:
            own = max((measured.get(name, {}).get("minutes", 0.0) for name in expand(job_id, jobs[job_id])),
                      default=0.0)
            before = max((finish(need) for need in needs_of(jobs[job_id]) if need in reachable),
                         default=(0.0, []))
            memo[job_id] = (before[0] + own, before[1] + [job_id])
        return memo[job_id]
    return max((finish(job_id) for job_id in reachable), default=(0.0, []))


def _uploads(job: dict) -> list[str]:
    return [str(step["with"]["name"]) for step in job.get("steps", [])
            if str(step.get("uses", "")).startswith("actions/upload-artifact") and "name" in step.get("with", {})]


def _downloads(job: dict) -> list[str]:
    found = []
    for step in job.get("steps", []):
        if str(step.get("uses", "")).startswith("actions/download-artifact"):
            options = step.get("with", {})
            found.append(str(options.get("name") or options.get("pattern") or "*"))
    return found


def data_needs(workflow: dict, job_id: str) -> dict[str, bool]:
    """For each job ``job_id`` needs: does it read that job's outputs, or
    download an artifact that job uploads?"""
    jobs = workflow["jobs"]
    job = jobs[job_id]
    text = json.dumps(job)
    result = {}
    for need in needs_of(job):
        reads_outputs = f"needs.{need}.outputs" in text
        uploads = [EXPRESSION.sub("*", name) for name in _uploads(jobs.get(need, {}))]
        downloads = _downloads(job)
        downloads_artifact = any(fnmatch.fnmatchcase(upload, EXPRESSION.sub("*", wanted))
                                 or fnmatch.fnmatchcase(EXPRESSION.sub("*", wanted), upload)
                                 for upload in uploads for wanted in downloads)
        result[need] = reads_outputs or downloads_artifact
    return result


def cache_savers(workflow: dict) -> dict[str, list[str]]:
    """rust-cache key prefixes (shared-key, or key plus job id) and the jobs
    that may save under each."""
    savers: dict[str, list[str]] = {}
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if not str(step.get("uses", "")).startswith("Swatinem/rust-cache"):
                continue
            options = step.get("with", {})
            key = options.get("shared-key") or "-".join(filter(None, [options.get("key"), job_id]))
            if options.get("save-if") is False:
                savers.setdefault(str(key), [])
            else:
                savers.setdefault(str(key), []).append(job_id)
    return savers


def heavy_pip_installs(workflow: dict) -> list[tuple[str, str]]:
    """Every pip install of the checkout or of torch: (job id, line)."""
    found = []
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            for line in str(step.get("run", "")).splitlines():
                if re.search(r"\bpip install\b", line) and (" -e " in line or "torch==" in line):
                    found.append((job_id, line.strip()))
    return found


def problems(name: str, workflow: dict, budget: dict, raw: dict | None = None) -> list[str]:
    """Everything that makes ``workflow`` disagree with its budget entry.
    ``raw`` is the unresolved workflow, whose matrix expressions still name
    the outputs a job reads; it defaults to ``workflow``."""
    entry = budget[name]
    raw = raw or workflow
    found = []
    reachable = reachable_names(workflow)
    duplicates = sorted({job for job in reachable if reachable.count(job) > 1})
    if duplicates:
        found.append(f"{name}: job names repeat: {duplicates}")
    measured = entry["jobs"]
    added = sorted(set(reachable) - set(measured))
    removed = sorted(set(measured) - set(reachable))
    if added:
        found.append(f"{name}: jobs a pull request can start with no measured minutes: {added}. "
                     "Run them on a hosted run, then: python rust/ci/ci_budget.py measure "
                     f"--workflow {name} <run id>")
    if removed:
        found.append(f"{name}: budgeted jobs the workflow no longer has: {removed}; re-measure")
    limits = entry["limits"]
    minutes = sum(measured[job]["minutes"] for job in reachable if job in measured)
    longest = max((measured[job]["minutes"], job) for job in reachable if job in measured)
    if len(reachable) > limits["jobs"]:
        found.append(f"{name}: {len(reachable)} jobs a pull request can start, over the limit of "
                     f"{limits['jobs']} (each takes one of the account's 20 hosted slots)")
    if minutes > limits["runner_minutes"]:
        found.append(f"{name}: {minutes:.1f} measured runner-minutes per full run, over the limit of "
                     f"{limits['runner_minutes']}")
    if longest[0] > limits["longest_job_minutes"]:
        found.append(f"{name}: {longest[1]} measured {longest[0]} minutes, over the "
                     f"{limits['longest_job_minutes']}-minute limit for one job")
    path_minutes, path = critical_path(workflow, measured)
    if path_minutes > limits["critical_path_minutes"]:
        found.append(f"{name}: the slowest chain of needs, {' -> '.join(path)}, measures "
                     f"{path_minutes:.1f} minutes with no queueing, over the limit of "
                     f"{limits['critical_path_minutes']}: a job waits on a slower one than it did")
    reasons = entry.get("needs_without_data", {})
    for job_id in raw["jobs"]:
        for need, carries in data_needs(raw, job_id).items():
            reason = reasons.get(job_id, {}).get(need) or reasons.get(job_id, {}).get("*")
            if not carries and not reason:
                found.append(f"{name}: {job_id} needs {need} but reads none of its outputs or "
                             "artifacts, so it waits on it for nothing; drop the need or give "
                             "the reason in ci_budget.json needs_without_data")
    for key, jobs in cache_savers(workflow).items():
        if len(jobs) > 1:
            found.append(f"{name}: rust-cache key {key} is saved by {jobs}; give one job the key "
                         "and make the others read it with save-if: false")
    if entry.get("pip_no_compile"):
        for job_id, line in heavy_pip_installs(workflow):
            if "--no-compile" not in line:
                found.append(f"{name}: {job_id} installs without --no-compile: {line}")
    return found


def measure(run_ids: list[str]) -> dict[str, float]:
    """Each job's median minutes over these runs, latest attempts only."""
    durations: dict[str, list[float]] = {}
    for run_id in run_ids:
        run = json.loads(subprocess.run(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{run_id}"],
                                        check=True, capture_output=True, text=True).stdout)
        jobs = json.loads(subprocess.run(
            ["gh", "api", f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100&filter=latest"],
            check=True, capture_output=True, text=True).stdout)["jobs"]
        for job in jobs:
            if job.get("run_attempt", run["run_attempt"]) != run["run_attempt"]:
                continue
            if job["conclusion"] in (None, "skipped") or not job["started_at"] or not job["completed_at"]:
                continue
            start = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
            durations.setdefault(job["name"], []).append((end - start).total_seconds() / 60)
    return {name: round(statistics.median(values), 1) for name, values in durations.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="report every disagreement with the budget")
    check.add_argument("--workflow", action="append")
    update = commands.add_parser("measure", help="rewrite a workflow's measured minutes from hosted runs")
    update.add_argument("--workflow", required=True)
    update.add_argument("runs", nargs="+")
    options = parser.parse_args(argv)
    budget = json.loads(BUDGET.read_text(encoding="utf-8"))
    if options.command == "check":
        found = [problem for name in (options.workflow or list(budget))
                 for problem in problems(name, load(name), budget, load(name, resolved=False))]
        print("\n".join(found) or "ci budget: every workflow within its budget")
        return int(bool(found))
    workflow = load(options.workflow)
    reachable = reachable_names(workflow)
    measured = measure(options.runs)
    missing = [name for name in reachable if name not in measured]
    if missing:
        print(f"these runs never ran {missing}; add a run that did", file=sys.stderr)
        return 1
    entry = budget[options.workflow]
    entry["jobs"] = {name: {"minutes": measured[name]} for name in reachable}
    entry["measured_on"] = [int(run) for run in options.runs]
    BUDGET.write_text(json.dumps(budget, indent=2) + "\n", encoding="utf-8")
    total = sum(measured[name] for name in reachable)
    print(f"{options.workflow}: {len(reachable)} jobs, {total:.1f} runner-minutes, "
          f"limits {entry['limits']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
