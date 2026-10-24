"""The hosted CI budget: what every job a pull request can start costs.

The account runs at most 20 hosted jobs at once, across every workflow and
repository, so a full integration-batch run is bound by its total runner
minutes rather than by its longest job (docs/dev/ci-budget.md). Each change
that adds a job, a shard or a slower step must say so: rust/ci/ci_budget.json
holds, per workflow, the measured minutes of every job a pull request can
reach, and limits on their count, their sum, the longest one and the
critical path through ``needs``. test_ci_budget.py fails when the workflow
and the budget disagree, when a limit is exceeded, or when a structural rule
below is broken.

    python rust/ci/ci_budget.py measure --workflow rust.yml RUN_ID RUN_ID [...]

rewrites that workflow's measured minutes (each job's median over two or
more completed runs, successful jobs of the latest attempt only) and leaves
the limits alone: raising a limit is a reviewed edit with its reason beside
it.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
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
# The events whose runs the budget covers: a pull request, its merge group
# and the master push that merges it.
BUDGET_EVENTS = ("pull_request", "merge_group", "push")
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}", re.S)


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


def pull_request_workflows() -> list[str]:
    """The workflow files a pull request or merge group starts."""
    import yaml
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        triggers = yaml.safe_load(path.read_text(encoding="utf-8")).get(True) or {}
        triggers = [triggers] if isinstance(triggers, str) else list(triggers)
        if {"pull_request", "pull_request_target", "merge_group"} & set(triggers):
            found.append(path.name)
    return found


def needs_of(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


# --- GitHub's expression language, three-valued -----------------------------
# A condition evaluates to True, False or UNKNOWN; anything the budget cannot
# know (outputs of jobs that run, labels, refs, variables) is UNKNOWN, and a
# job counts unless its condition is False for every budgeted event.

UNKNOWN = type("Unknown", (), {"__repr__": lambda self: "UNKNOWN"})()
TOKENS = re.compile(r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<number>\d+(?:\.\d+)?)"
                    r"|(?P<op>&&|\|\||==|!=|<=|>=|[!()<>,\[\].*])|(?P<name>[A-Za-z_][\w-]*))")
STATUS = ("success", "failure", "always", "cancelled")


def tokens(text: str) -> list[tuple[str, str]]:
    found, position = [], 0
    text = text.strip()
    while position < len(text):
        match = TOKENS.match(text, position)
        if not match or match.end() == position:
            raise ValueError(f"cannot read the expression {text!r} at {text[position:]!r}")
        kind = match.lastgroup
        found.append((kind, match.group(kind)))
        position = match.end()
    return found


def truthy(value):
    if value is UNKNOWN:
        return UNKNOWN
    return value not in (None, False, "", 0)


def both(left, right):
    left, right = truthy(left), truthy(right)
    if left is False or right is False:
        return False
    return True if left is True and right is True else UNKNOWN


def either(left, right):
    left, right = truthy(left), truthy(right)
    if left is True or right is True:
        return True
    return False if left is False and right is False else UNKNOWN


def equal(left, right):
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    return left == right


class Evaluator:
    """Evaluates one condition for one event. ``skipped`` names the needed
    jobs that never run, ``matrix`` the combination a step runs in."""

    def __init__(self, event: str, needs: list[str], skipped: set[str], matrix: dict | None = None):
        self.event, self.needs, self.skipped, self.matrix = event, needs, skipped, matrix or {}

    def run(self, text: str):
        self.items, self.index = tokens(text), 0
        value = self.disjunction()
        if self.index != len(self.items):
            raise ValueError(f"unread input in {text!r}")
        return value

    def peek(self, *values):
        return self.index < len(self.items) and self.items[self.index][1] in values

    def take(self):
        self.index += 1
        return self.items[self.index - 1]

    def disjunction(self):
        value = self.conjunction()
        while self.peek("||"):
            self.take()
            value = either(value, self.conjunction())
        return value

    def conjunction(self):
        value = self.comparison()
        while self.peek("&&"):
            self.take()
            value = both(value, self.comparison())
        return value

    def comparison(self):
        value = self.unary()
        while self.peek("==", "!=", "<", ">", "<=", ">="):
            operator = self.take()[1]
            other = self.unary()
            if operator in ("==", "!="):
                result = equal(value, other)
                value = result if operator == "==" or result is UNKNOWN else not result
            else:
                value = UNKNOWN
        return value

    def unary(self):
        if self.peek("!"):
            self.take()
            value = truthy(self.unary())
            return UNKNOWN if value is UNKNOWN else not value
        return self.postfix()

    def postfix(self):
        kind, text = self.take()
        if text == "(":
            value = self.disjunction()
            self.take()
            return value
        if kind == "string":
            return text[1:-1].replace("''", "'")
        if kind == "number":
            return float(text)
        if text in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[text]
        if self.peek("("):
            return self.call(text)
        path = [text]
        while self.peek(".", "["):
            if self.take()[1] == ".":
                path.append(self.take()[1])
            else:
                key = self.disjunction()
                self.take()
                path.append(key if isinstance(key, str) else "*")
        return self.lookup(path)

    def call(self, name: str):
        self.take()
        arguments = []
        while not self.peek(")"):
            arguments.append(self.disjunction())
            if self.peek(","):
                self.take()
        self.take()
        if name in ("always",):
            return True
        if name == "cancelled":
            return False
        if name in ("success", "failure"):
            if any(need in self.skipped for need in self.needs):
                return name == "failure" and UNKNOWN
            return UNKNOWN if self.needs else name == "success"
        if any(argument is UNKNOWN for argument in arguments):
            return UNKNOWN
        if name in ("startsWith", "endsWith", "contains") and len(arguments) == 2:
            haystack, needle = arguments
            if isinstance(haystack, list):
                return name == "contains" and any(equal(item, needle) is True for item in haystack)
            haystack, needle = str(haystack).lower(), str(needle).lower()
            return {"startsWith": haystack.startswith, "endsWith": haystack.endswith,
                    "contains": haystack.__contains__}[name](needle)
        return UNKNOWN

    def lookup(self, path: list[str]):
        head = path[0]
        if path[:2] == ["github", "event_name"]:
            return self.event
        if head == "inputs":
            return None if self.event != "workflow_dispatch" else UNKNOWN
        if head == "needs" and len(path) >= 3:
            if path[1] in self.skipped:
                return "skipped" if path[2] == "result" else ""
            return UNKNOWN
        if head == "matrix" and len(path) == 2 and path[1] in self.matrix:
            return self.matrix[path[1]]
        return UNKNOWN


def condition_text(value) -> str:
    """A condition with its ${{ }} markers removed; GitHub reads an ``if``
    as an expression with or without them."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return EXPRESSION.sub(lambda match: f"({match.group(1)})", str(value)).strip()


def can_start(job: dict, skipped: set[str], event: str) -> bool:
    """Whether the job's condition can hold for this event. With no status
    function GitHub reads it as success() && (condition)."""
    text = condition_text(job.get("if", ""))
    if not re.search(r"\b(" + "|".join(STATUS) + r")\s*\(", text):
        text = f"success() && ({text})" if text else "success()"
    evaluator = Evaluator(event, needs_of(job), skipped)
    return evaluator.run(text) is not False


def pull_request_reachable(workflow: dict) -> list[str]:
    """The job ids a pull request, merge group or master push can start:
    those whose condition is not false for one of those events once every
    job that never starts is known to be skipped."""
    jobs = workflow["jobs"]
    skipped: set[str] = set()
    while True:
        newly = {job_id for job_id, job in jobs.items() if job_id not in skipped
                 and not any(can_start(job, skipped, event) for event in BUDGET_EVENTS)}
        if not newly:
            return [job_id for job_id in jobs if job_id not in skipped]
        skipped |= newly


# --- Matrix expansion, after GitHub's documented rules ------------------------

def combinations(job: dict) -> list[dict]:
    """The job's matrix combinations: the axes' product less the excludes;
    each include joins every original combination it overwrites no axis
    value of, or else adds a combination of its own. An axis given by an
    expression counts as one unknown value."""
    matrix = (job.get("strategy") or {}).get("matrix")
    if not matrix:
        return [{}]
    axes = {key: value if isinstance(value, list) else [f"<{key}>"]
            for key, value in matrix.items() if key not in ("include", "exclude")}
    original = [dict(zip(axes, values)) for values in itertools.product(*axes.values())]
    excludes = matrix.get("exclude") or []
    original = [combo for combo in original
                if not any(all(combo.get(key) == value for key, value in rule.items()) for rule in excludes)]
    added = []
    for entry in matrix.get("include") or []:
        matched = [combo for combo in original
                   if all(combo[key] == value for key, value in entry.items() if key in axes)]
        for combo in matched:
            combo.update(entry)
        if not matched:
            added.append(dict(entry))
    return original + added


def expand(job_id: str, job: dict) -> list[str]:
    """The names GitHub reports for the job's runs. A matrix job with no
    ``name`` is ``<job id> (<values>)``; an unset matrix key renders empty."""
    combos = combinations(job)
    if "name" not in job:
        return [job_id if not combo else f"{job_id} ({', '.join(str(value) for value in combo.values())})"
                for combo in combos]

    def render(combo: dict) -> str:
        def value(match):
            expression = match.group(1)
            if expression.startswith("matrix."):
                return str(combo.get(expression.removeprefix("matrix."), ""))
            return match.group(0)
        return EXPRESSION.sub(value, str(job["name"]))
    return [render(combo) for combo in combos]


def reachable_names(workflow: dict) -> list[str]:
    return [name for job_id in pull_request_reachable(workflow)
            for name in expand(job_id, workflow["jobs"][job_id])]


def critical_path(workflow: dict, measured: dict) -> tuple[float, list[str]]:
    """The longest chain of measured job minutes through ``needs`` among
    the jobs a pull request can start, queueing left out: what a run takes
    with a free slot for every job. A job's minutes are its slowest
    expansion's (``needs`` waits for the whole matrix); an unmeasured one
    counts as zero (problems() names it)."""
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


# --- Structural rules ---------------------------------------------------------

def _uploads(job: dict) -> list[str]:
    return [str(step["with"]["name"]) for step in job.get("steps", [])
            if str(step.get("uses", "")).startswith("actions/upload-artifact") and "name" in step.get("with", {})]


def _downloads(job: dict) -> list[str]:
    found = []
    for step in job.get("steps", []):
        if str(step.get("uses", "")).startswith("actions/download-artifact"):
            options = step.get("with", {})
            # With neither, the action downloads every artifact of the run.
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
        quoted = re.escape(need)
        reads_outputs = bool(re.search(rf"needs\.{quoted}\.outputs|needs\[\s*'{quoted}'\s*\]\.outputs", text))
        uploads = [EXPRESSION.sub("*", name) for name in _uploads(jobs.get(need, {}))]
        downloads = [EXPRESSION.sub("*", wanted) for wanted in _downloads(job)]
        downloads_artifact = any(fnmatch.fnmatchcase(upload, wanted) or fnmatch.fnmatchcase(wanted, upload)
                                 for upload in uploads for wanted in downloads)
        result[need] = reads_outputs or downloads_artifact
    return result


def cache_savers(workflow: dict) -> dict[tuple[str, str], list[str]]:
    """Each rust-cache key a run can save, with the runner it saves from,
    and the job runs that may save it. Swatinem/rust-cache v2 builds the
    key from prefix-key (default v0-rust), then shared-key, or key and the
    job id (unless add-job-id-key is false), then the runner's OS; one save
    per matrix combination whose step and save-if conditions can hold."""
    savers: dict[tuple[str, str], list[str]] = {}
    for job_id, job in workflow["jobs"].items():
        for combo in combinations(job):
            runs_on = EXPRESSION.sub(lambda m: str(combo.get(m.group(1).removeprefix("matrix."), "?"))
                                     if m.group(1).startswith("matrix.") else "?", str(job.get("runs-on", "?")))
            for step in job.get("steps", []):
                if not str(step.get("uses", "")).startswith("Swatinem/rust-cache"):
                    continue
                options = step.get("with", {})
                conditions = [condition_text(step.get("if", "true")), condition_text(options.get("save-if", "true"))]
                if any(Evaluator("pull_request", needs_of(job), set(), combo).run(text) is False
                       for text in conditions if text):
                    continue
                parts = [str(options.get("prefix-key") or "v0-rust")]
                if options.get("shared-key"):
                    parts.append(str(options["shared-key"]))
                else:
                    parts.append(str(options.get("key") or ""))
                    if str(options.get("add-job-id-key", "true")).lower() != "false":
                        parts.append(job_id)
                key = EXPRESSION.sub(lambda m: str(combo.get(m.group(1).removeprefix("matrix."), "?"))
                                     if m.group(1).startswith("matrix.") else "?", "-".join(filter(None, parts)))
                label = f"{job_id} {combo}" if combo else job_id
                savers.setdefault((key, runs_on), []).append(label)
    return savers


def heavy_pip_installs(workflow: dict) -> list[tuple[str, str]]:
    """Every pip install of the checkout (editable or not) or of torch, with
    PowerShell and shell line continuations joined: (job id, command)."""
    found = []
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            script = re.sub(r"[`\\]\s*\n\s*", " ", str(step.get("run", "")))
            for line in script.splitlines():
                if re.search(r"\bpip install\b", line) and re.search(
                        r"(?:^|\s)-e\s|\sinstall\s(?:.*\s)?\"?\.(?:\[|\"|\s|$)|torch==", line):
                    found.append((job_id, line.strip()))
    return found


def fingerprint(job: dict) -> str:
    """A short hash of a job's whole definition (matrix included): its
    measured minutes stand only while it is unchanged."""
    return hashlib.sha256(json.dumps(job, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def fingerprints(workflow: dict) -> dict[str, str]:
    return {job_id: fingerprint(workflow["jobs"][job_id]) for job_id in pull_request_reachable(workflow)}


def unexplained_raises(base: dict, current: dict) -> list[str]:
    """Each limit ``current`` raises over ``base`` (the budget on master)
    whose workflow's ``why`` is empty or unchanged: a raise names its
    reason."""
    found = []
    for name, entry in current.items():
        before = base.get(name, {}).get("limits")
        if not before:
            continue
        limits = entry["limits"]
        raised = [key for key, value in limits.items() if key != "why" and value > before.get(key, value)]
        if raised and (not str(limits.get("why", "")).strip() or limits.get("why") == before.get("why")):
            found.append(f"{name}: raises {raised} without a new reason in limits.why")
    return found


def estimates(entry: dict) -> list[str]:
    return sorted(name for name, job in entry["jobs"].items() if "estimate" in job)


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
                     "Measure them on hosted runs (python rust/ci/ci_budget.py measure "
                     f"--workflow {name} <run> <run>), or enter an estimate (docs/dev/ci-budget.md)")
    if removed:
        found.append(f"{name}: budgeted jobs the workflow no longer has: {removed}; re-measure")
    recorded = entry.get("fingerprints", {})
    for job_id, current in fingerprints(workflow).items():
        if recorded.get(job_id) != current:
            found.append(f"{name}: job {job_id} changed since its minutes were measured, so they may no "
                         f"longer hold. Re-measure it (python rust/ci/ci_budget.py measure --workflow {name} "
                         "<run> <run>), or record why its cost is unchanged: python rust/ci/ci_budget.py accept "
                         f"--workflow {name} --job {job_id} --reason \"...\"")
    limits = entry["limits"]
    known = [job for job in reachable if job in measured]
    minutes = sum(measured[job]["minutes"] for job in known)
    if len(reachable) > limits["jobs"]:
        found.append(f"{name}: job count {len(reachable)} is over its limit of {limits['jobs']} "
                     "(each job takes one of the account's 20 hosted slots)")
    if minutes > limits["runner_minutes"]:
        found.append(f"{name}: {minutes:.1f} measured runner-minutes per full run, over the limit of "
                     f"{limits['runner_minutes']}")
    if known:
        longest = max((measured[job]["minutes"], job) for job in known)
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
    for (key, runner), jobs in cache_savers(workflow).items():
        if len(jobs) > 1:
            shown = ", ".join(jobs[:3]) + (f" and {len(jobs) - 3} more" if len(jobs) > 3 else "")
            found.append(f"{name}: rust-cache key {key} on {runner} is saved by {len(jobs)} runs ({shown}); "
                         "give one the key and make the others read it with save-if: false")
    if entry.get("pip_no_compile"):
        for job_id, line in heavy_pip_installs(workflow):
            if "--no-compile" not in line:
                found.append(f"{name}: {job_id} installs without --no-compile: {line}")
    return found


def measure(run_ids: list[str]) -> dict[str, float]:
    """Each job's median minutes over these completed runs: successful jobs
    of each run's latest attempt only (a failed or cancelled job's time says
    nothing about its cost)."""
    durations: dict[str, list[float]] = {}
    for run_id in run_ids:
        run = json.loads(subprocess.run(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{run_id}"],
                                        check=True, capture_output=True, text=True).stdout)
        if run["status"] != "completed":
            raise ValueError(f"run {run_id} is {run['status']}; measure completed runs only")
        listing = subprocess.run(
            ["gh", "api", "--paginate", f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100&filter=latest",
             "--jq", ".jobs[]"], check=True, capture_output=True, text=True).stdout
        for line in listing.splitlines():
            job = json.loads(line)
            if job.get("run_attempt", run["run_attempt"]) != run["run_attempt"] or job["conclusion"] != "success":
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
    accept = commands.add_parser("accept", help="record that a changed job's measured cost still holds")
    accept.add_argument("--workflow", required=True)
    accept.add_argument("--job", required=True)
    accept.add_argument("--reason", required=True)
    options = parser.parse_args(argv)
    budget = json.loads(BUDGET.read_text(encoding="utf-8"))
    if options.command == "check":
        names = options.workflow or list(budget)
        found = [problem for name in names for problem in problems(name, load(name), budget, load(name, resolved=False))]
        found += [f"{name}: a pull request starts it, and the budget has no entry for it"
                  for name in pull_request_workflows() if name not in budget]
        for name in names:
            if estimates(budget[name]):
                print(f"{name}: estimated, not measured: {estimates(budget[name])}")
        print("\n".join(found) or "ci budget: every workflow within its budget")
        return int(bool(found))
    if options.command == "accept":
        if not options.reason.strip():
            parser.error("--reason must say why the job's cost is unchanged")
        current = fingerprints(load(options.workflow))
        if options.job not in current:
            parser.error(f"{options.job} is not a job a pull request starts in {options.workflow}")
        entry = budget[options.workflow]
        entry.setdefault("fingerprints", {})[options.job] = current[options.job]
        entry.setdefault("accepted", []).append(
            {"job": options.job, "fingerprint": current[options.job], "reason": options.reason.strip()})
        BUDGET.write_text(json.dumps(budget, indent=2) + "\n", encoding="utf-8")
        print(f"{options.workflow}: {options.job} accepted as unchanged in cost: {options.reason.strip()}")
        return 0
    if len(options.runs) < 2:
        parser.error("measure takes two or more runs: one hosted run is too noisy to budget on")
    workflow = load(options.workflow)
    reachable = reachable_names(workflow)
    measured = measure(options.runs)
    entry = budget[options.workflow]
    missing = [name for name in reachable if name not in measured and name not in entry["jobs"]]
    if missing:
        print(f"no successful run of {missing} in these runs; add a run that has them", file=sys.stderr)
        return 1
    # A job these runs did not run successfully keeps its earlier entry.
    entry["jobs"] = {name: {"minutes": measured[name]} if name in measured else entry["jobs"][name]
                     for name in reachable}
    entry["measured_on"] = [int(run) for run in options.runs]
    entry["fingerprints"] = fingerprints(workflow)
    entry.pop("accepted", None)
    BUDGET.write_text(json.dumps(budget, indent=2) + "\n", encoding="utf-8")
    total = sum(job["minutes"] for job in entry["jobs"].values())
    print(f"{options.workflow}: {len(reachable)} jobs, {total:.1f} runner-minutes, limits {entry['limits']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
