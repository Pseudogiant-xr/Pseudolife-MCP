"""Warn before a hosted job outgrows its time limit.

A bound that a slow runner CPU can reach fails a run that would have
passed: on 26 November 2026 the 0.26.0 release-cut batch lost three hosted
attempts because ops/ci_tests.sh's 50-minute pytest bound killed its
frozen-inclusive suite at 99%. A run on 25 November had already taken 49:07
against that bound (run 45512966117), and nothing noticed.

This check reads the recent completed runs of every workflow, every attempt
of every job that started on a runner, and compares each with the bounds in
this checkout:

- the job's ``timeout-minutes`` (an expression is resolved from the run:
  its event, head branch and matrix values);
- for a job that runs ``ops/ci_tests.sh``, the pytest bound of the
  selection that run used. The ``test-diagnostics`` artifact records it:
  ``process.txt`` is GNU time's record of the exact ``timeout ... pytest``
  command (``--include-frozen`` marks a frozen-inclusive run) and its
  elapsed time, and ``cpu.txt`` names the runner CPU.

A run that used ``THRESHOLD`` or more of a bound is a finding.

    python rust/ci/ci_headroom.py report [--days N]

prints the findings. ``sync`` (the scheduled ``ci-headroom.yml``) keeps them
in one issue instead of failing a run: nothing reads a red scheduled run, an
issue lists every near miss with its numbers until the bound is raised, and
it closes itself when no finding is left. A finding stays for
``RETENTION_DAYS`` (the diagnostics artifact's retention) unless the bound
it was measured against has since been raised clear of it. The scheduled run
fails only when the check itself cannot run.

Runs are data: a fork's run (its own workflow file, its branch's
artifacts) is not read at all; of an artifact only the two named members are
read, each under a size cap; and every table cell is reduced to a plain
character set before it reaches the issue.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_budget  # noqa: E402

ROOT = ci_budget.ROOT
REPOSITORY = ci_budget.REPOSITORY
WORKFLOWS = ci_budget.WORKFLOWS
CI_TESTS = ROOT / "ops/ci_tests.sh"
# About 80% (W127, 26 November 2026): a 40%-headroom bound sits at 71% of
# its slowest measured run, so a run past 80% has used up most of that
# margin. The 2026-11-12..26 survey of every ci.yml and rust.yml attempt
# found `test` at 62% of its 87-minute job bound and about 70% of its
# 72-minute frozen-inclusive pytest bound once #2012 raised them, and four
# Windows bounds above 80%.
THRESHOLD = 0.8
# The test-diagnostics artifact's retention-days in ci.yml.
RETENTION_DAYS = 14
# The runs the issue keeps for each bound, worst first, and the bounds it
# lists, worst first: a row and its state take about 870 bytes, and an issue
# body holds 65,536 characters.
PER_BOUND = 5
MAX_BOUNDS = 12
# sync's longest look back. Estimated from W127's 2026-11-12..26 survey: the
# run listings take about 90 calls (15 days of five workflows, one query per
# day, python-freeze.yml's about 130 runs a day on two pages), and each day
# of activity about 110 more: about 85 runs a day outlast too_short() (about
# 70 of rust.yml, 15 of ci.yml, none of python-freeze.yml), one jobs listing
# each, plus about 10 test-diagnostics reads. GITHUB_TOKEN allows 1000 calls
# an hour for the whole repository.
SYNC_DAYS = 3
DIAGNOSTICS = "test-diagnostics"
TITLE = "CI: a hosted job is close to its time limit"
AUTHOR = "github-actions[bot]"
STATE = re.compile(r"<!-- ci-headroom-state (\{.*?\}) -->", re.S)
# Artifact reads: a zip is refused above this size, and each member read
# from it above its own cap. A test-diagnostics zip is about 1 MB.
ARTIFACT_BYTES = 32 * 1024 * 1024
MEMBER_BYTES = 64 * 1024
CPU_CHARACTERS = re.compile(r"[^A-Za-z0-9 ()@.,+_-]")
PLAIN = re.compile(r"[^A-Za-z0-9 ./(),:;'+-]")
# The bound ops/ci_tests.sh hands to the timeout that runs pytest, for each
# selection (tests/test_hang_diagnostics.py pins the same shape).
LIVE_BOUND = re.compile(r"^bound=(\d+)m$", re.M)
FROZEN_BOUND = re.compile(r'^if \[\[ "\$\{PSEUDOLIFE_CI_INCLUDE_FROZEN:-\}" == true \]\]; then\n'
                          r"    frozen=\(--include-frozen\)\n    bound=(\d+)m\nfi$", re.M)
BOUNDED_PYTEST = re.compile(r'timeout --signal=ABRT --kill-after=\d+s "\$bound"\s*\\\s*\n\s*python -m pytest')
SELECTIONS = ("live", "frozen-inclusive")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def minutes_between(start: str, end: str) -> float:
    return (parse_time(end) - parse_time(start)).total_seconds() / 60


# --- Bounds in this checkout ---------------------------------------------------

def pytest_bounds(script: str) -> dict[str, int]:
    """Minutes ops/ci_tests.sh allows pytest, per selection. A script of the
    older one-bound shape (``timeout ... 50m``) bounds both selections alike."""
    single = re.search(r"timeout --signal=ABRT --kill-after=\d+s (\d+)m\s*\\\s*\n\s*python -m pytest", script)
    if single:
        return dict.fromkeys(SELECTIONS, int(single.group(1)))
    live, frozen = LIVE_BOUND.search(script), FROZEN_BOUND.search(script)
    if not (BOUNDED_PYTEST.search(script) and live and frozen):
        raise ValueError("ops/ci_tests.sh does not bound pytest the way this check reads it")
    return {"live": int(live.group(1)), "frozen-inclusive": int(frozen.group(1))}


@dataclass
class JobShape:
    """One job of a workflow, as this checkout defines it."""
    job_id: str
    timeout: object                   # int, an expression string, or None (360)
    combos: dict[str, dict]           # reported name -> matrix combination
    pattern: re.Pattern               # the name template, any expression matching anything
    runs_ci_tests: bool


def shapes(workflow: dict) -> list[JobShape]:
    found = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        combos = dict(zip(ci_budget.expand(job_id, job), ci_budget.combinations(job)))
        template = str(job.get("name", job_id))
        pieces = re.split(r"\$\{\{.*?\}\}", template)
        pattern = re.compile(".+?".join(re.escape(piece) for piece in pieces) + r"(?: \(.*\))?\Z")
        runs = any("bash ops/ci_tests.sh" in str(step.get("run", "")) for step in job.get("steps") or [])
        found.append(JobShape(job_id, job.get("timeout-minutes"), combos, pattern, runs))
    return found


def shape_for(found: list[JobShape], name: str) -> tuple[JobShape, dict | None] | None:
    """The job a reported name belongs to, with its matrix combination when
    the name pins one down."""
    for shape in found:
        if name in shape.combos:
            return shape, shape.combos[name]
    matches = [shape for shape in found if shape.pattern.match(name)]
    return (matches[0], None) if len(matches) == 1 else None


class RunEvaluator(ci_budget.Evaluator):
    """ci_budget's evaluator, with what a run record tells: its event, head
    branch and ref, and the scope outputs derived from them."""

    def __init__(self, workflow: str, run: dict, matrix: dict | None):
        super().__init__(run.get("event", ""), [], set(), matrix)
        self.workflow, self.record = workflow, run

    def lookup(self, path):
        if path[:2] == ["github", "head_ref"]:
            return self.record.get("head_branch", "") if self.event == "pull_request" else ""
        if path == ["needs", "scope", "outputs", "full"] and self.workflow == "rust.yml":
            return scope_full(self.record)
        return super().lookup(path)


def scope_full(run: dict) -> str:
    """rust.yml's scope output ``full`` (rust/ci/docs_scope.py rust_scope):
    'false' for a pull request from any branch but integrate/*, 'true' for
    every other run. Its error path and an empty diff say 'true' for such a
    pull request too; only a run whose scope crashed, or whose pull request
    changes nothing, sees that, and there the 100-minute bound read here is
    25 minutes too generous."""
    branch = run.get("head_branch") or ""
    ordinary = run.get("event") == "pull_request" and branch and not branch.startswith("integrate/")
    return "false" if ordinary else "true"


def job_bound(workflow: str, shape: JobShape, combo: dict | None, run: dict) -> tuple[float | None, str]:
    """(minutes, note) of the job's timeout for this run. An expression the
    run cannot settle takes the smallest number it names, and says so."""
    value = shape.timeout
    if value is None:
        return 360.0, "GitHub's default"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), ""
    text = str(value)
    inner = ci_budget.EXPRESSION.fullmatch(text.strip())
    if not inner:
        try:
            return float(text), ""
        except ValueError:
            return None, f"unreadable timeout {text!r}"
    try:
        result = RunEvaluator(workflow, run, combo).run(inner.group(1))
    except ValueError:
        result = ci_budget.UNKNOWN
    if isinstance(result, (int, float)) and not isinstance(result, bool) and result > 0:
        return float(result), ""
    if isinstance(result, str) and re.fullmatch(r"\d+(\.\d+)?", result):
        return float(result), ""
    numbers = sorted({float(n) for n in re.findall(r"(?<![\w.'])\d+(?:\.\d+)?(?![\w.'])", inner.group(1))})
    if not numbers:
        return None, f"unresolved timeout {text!r}"
    return numbers[0], "smallest of " + "/".join(f"{n:g}" for n in numbers) + " (unresolved)"


# --- What a run recorded -------------------------------------------------------

@dataclass
class Pytest:
    selection: str
    minutes: float
    killed: bool


def parse_process(text: str) -> Pytest | None:
    """GNU time's record of the bounded pytest command (ops/ci_tests.sh)."""
    command = re.search(r'^\s*Command being timed: "(.*)"\s*$', text, re.M)
    elapsed = re.search(r"^\s*Elapsed \(wall clock\) time \(h:mm:ss or m:ss\): ([\d:.]+)\s*$", text, re.M)
    status = re.search(r"^\s*Exit status: (\d+)\s*$", text, re.M)
    if not (command and elapsed and "python -m pytest" in command.group(1)):
        return None
    seconds = 0.0
    for part in elapsed.group(1).split(":"):
        seconds = seconds * 60 + float(part)
    signalled = re.search(r"^\s*Command terminated by signal", text, re.M) is not None
    # timeout exits 124 when its signal ended the command, 137 after --kill-after.
    killed = signalled or (status is not None and int(status.group(1)) in (124, 137))
    selection = "frozen-inclusive" if " --include-frozen " in f" {command.group(1)} " else "live"
    return Pytest(selection, seconds / 60, killed)


def parse_cpu(text: str) -> str:
    match = re.search(r"^Model name:\s*(.+?)\s*$", text, re.M)
    if not match:
        return "not recorded"
    return CPU_CHARACTERS.sub("", match.group(1))[:80].strip() or "not recorded"


def read_members(archive: bytes) -> dict[str, str]:
    """cpu.txt and process.txt from a diagnostics zip, each under its cap."""
    found = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for name in ("cpu.txt", "process.txt"):
            try:
                info = bundle.getinfo(name)
            except KeyError:
                continue
            if info.file_size <= MEMBER_BYTES:
                found[name] = bundle.read(info).decode("utf-8", "replace")
    return found


def artifact_for(job: dict, artifacts: list[dict]) -> dict | None:
    """The diagnostics artifact this job attempt uploaded: every attempt
    uploads one under the same name, so it is the one created while the job
    ran (it uploads in its last steps)."""
    start, end = parse_time(job["started_at"]), parse_time(job["completed_at"]) + timedelta(minutes=2)
    found = [artifact for artifact in artifacts if artifact.get("name") == DIAGNOSTICS
             and not artifact.get("expired") and artifact.get("created_at")
             and start <= parse_time(artifact["created_at"]) <= end]
    return found[0] if len(found) == 1 else None


# --- Findings ------------------------------------------------------------------

@dataclass
class Finding:
    workflow: str
    job: str
    kind: str              # "job" (timeout-minutes) or "pytest" (ops/ci_tests.sh)
    run_id: int
    attempt: int
    job_id: int
    minutes: float
    bound: float
    note: str
    cpu: str
    conclusion: str
    completed_at: str
    event: str
    head_branch: str
    selection: str = ""
    killed: bool = False

    @property
    def share(self) -> float:
        return self.minutes / self.bound

    def key(self) -> tuple:
        return (self.workflow, self.job, self.kind, self.selection, self.run_id, self.attempt)

    def group(self) -> tuple:
        """The bound this finding measures: one job can have two (the Rust
        job's 75 and 100 minutes, the pytest bound of each selection)."""
        return (self.workflow, self.job, self.kind, self.selection, self.bound)

    def run_record(self) -> dict:
        return {"event": self.event, "head_branch": self.head_branch}


@dataclass
class Reading:
    """Everything one pass read: findings, plus what it could not place."""
    findings: list[Finding] = field(default_factory=list)
    unplaced: dict[str, int] = field(default_factory=dict)
    unresolved: dict[str, str] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)
    runs: int = 0
    jobs: int = 0
    forks: int = 0
    short: int = 0


class Bounds:
    """The bounds in this checkout: every workflow's jobs and the pytest
    bounds of ops/ci_tests.sh."""

    def __init__(self, workflows: dict[str, dict], ci_tests: str):
        self.shapes = {name: shapes(workflow) for name, workflow in workflows.items()}
        self.pytest = pytest_bounds(ci_tests)

    @classmethod
    def from_checkout(cls) -> "Bounds":
        names = sorted(path.name for path in [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
        return cls({name: ci_budget.load(name) for name in names}, CI_TESTS.read_text(encoding="utf-8"))

    def job(self, workflow: str, name: str, run: dict) -> tuple[JobShape, float | None, str] | None:
        placed = shape_for(self.shapes.get(workflow, []), name)
        if placed is None:
            return None
        shape, combo = placed
        bound, note = job_bound(workflow, shape, combo, run)
        return shape, bound, note


def ran(job: dict) -> bool:
    """A job that started on a runner: one cancelled while it waited for one
    has no steps, and its times are the queue's."""
    return bool(job.get("started_at") and job.get("completed_at") and job.get("steps")
                and job.get("conclusion") not in (None, "skipped"))


def examine(bounds: Bounds, workflow: str, run: dict, jobs: list[dict],
            diagnostics, reading: Reading) -> None:
    """Add this run's findings. ``diagnostics(job)`` returns the job's
    (cpu.txt, process.txt) texts, or None when it has no artifact."""
    reading.runs += 1
    seen = set()
    for job in jobs:
        if not ran(job):
            continue
        # filter=all lists a job kept from an earlier attempt under each
        # later one, with the same times and a new id.
        identity = (job["name"], job["started_at"], job["completed_at"])
        if identity in seen:
            continue
        seen.add(identity)
        reading.jobs += 1
        placed = bounds.job(workflow, job["name"], run)
        if placed is None:
            label = f"{workflow}: {job['name']}"
            reading.unplaced[label] = reading.unplaced.get(label, 0) + 1
            continue
        shape, bound, note = placed
        if bound is None:
            reading.unresolved[f"{workflow}: {job['name']}"] = note
            continue
        texts = diagnostics(job) if shape.runs_ci_tests else None
        cpu = parse_cpu(texts[0]) if texts else "not recorded"
        common = dict(workflow=workflow, job=job["name"], run_id=int(run["id"]),
                      attempt=int(job.get("run_attempt") or run.get("run_attempt") or 1),
                      job_id=int(job["id"]), cpu=cpu, conclusion=str(job.get("conclusion")),
                      completed_at=job["completed_at"], event=str(run.get("event", "")),
                      head_branch=str(run.get("head_branch") or ""))
        minutes = minutes_between(job["started_at"], job["completed_at"])
        if minutes >= THRESHOLD * bound:
            reading.findings.append(Finding(kind="job", minutes=round(minutes, 2), bound=bound, note=note, **common))
        pytest = parse_process(texts[1]) if texts and texts[1] else None
        if pytest is not None:
            limit = float(bounds.pytest[pytest.selection])
            if pytest.minutes >= THRESHOLD * limit:
                reading.findings.append(Finding(
                    kind="pytest", minutes=round(pytest.minutes, 2), bound=limit,
                    note="killed by the bound it ran under; at least this long" if pytest.killed else "",
                    selection=pytest.selection, killed=pytest.killed, **common))


def still_holds(bounds: Bounds, finding: Finding) -> Finding | None:
    """A finding re-measured against today's bounds: gone once its bound was
    raised clear of it."""
    if finding.kind == "pytest":
        if finding.selection not in bounds.pytest:
            return None
        bound, note = float(bounds.pytest[finding.selection]), finding.note
    else:
        placed = bounds.job(finding.workflow, finding.job, finding.run_record())
        if placed is None or placed[1] is None:
            return None
        bound, note = placed[1], placed[2]
    if finding.minutes < THRESHOLD * bound:
        return None
    return Finding(**{**asdict(finding), "bound": bound, "note": note})


def keep(found: list[Finding]) -> list[Finding]:
    """A bound's worst PER_BOUND runs, plus its newest near miss when that is
    not among them: the newest one decides how long the bound stays listed,
    so dropping it would let the issue close while it is still recent."""
    worst = sorted(found, key=lambda finding: (finding.share, finding.completed_at), reverse=True)[:PER_BOUND]
    newest = max(found, key=lambda finding: (finding.completed_at, finding.share))
    return worst if newest in worst else [*worst, newest]


def merge(bounds: Bounds, previous: list[Finding], current: list[Finding], now: datetime) -> list[Finding]:
    """Today's findings plus the earlier ones that still hold and are inside
    the retention window, newest first."""
    cutoff = now - timedelta(days=RETENTION_DAYS)
    kept = {finding.key(): finding for finding in previous}
    kept.update({finding.key(): finding for finding in current})
    held = [still_holds(bounds, finding) for finding in kept.values()
            if parse_time(finding.completed_at) >= cutoff]
    groups: dict[tuple, list[Finding]] = {}
    for finding in held:
        if finding is not None:
            groups.setdefault(finding.group(), []).append(finding)
    # The worst runs of each bound, the worst bound first: what a reader
    # needs to size a raise, kept small enough for an issue body.
    ranked = [keep(found) for found in groups.values()]
    ranked.sort(key=lambda found: (found[0].share, found[0].job), reverse=True)
    return [finding for found in ranked[:MAX_BOUNDS] for finding in found]


# --- The issue -----------------------------------------------------------------

FIELDS = {"workflow": str, "job": str, "kind": str, "run_id": int, "attempt": int, "job_id": int,
          "minutes": float, "bound": float, "note": str, "cpu": str, "conclusion": str,
          "completed_at": str, "event": str, "head_branch": str, "selection": str, "killed": bool}


def parse_state(body: str) -> list[Finding]:
    """The findings an issue body carries. Anything malformed is dropped:
    the body is data, and today's runs rebuild what matters."""
    match = STATE.search(body or "")
    if not match:
        return []
    try:
        document = json.loads(match.group(1))
    except ValueError:
        return []
    found = []
    items = document.get("findings") if isinstance(document, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or set(item) != set(FIELDS):
            continue
        typed = all(type(item[name]) is kind if kind is not float
                    else type(item[name]) in (int, float) for name, kind in FIELDS.items())
        if not typed:
            continue
        values = {name: float(item[name]) if kind is float else item[name] for name, kind in FIELDS.items()}
        try:
            # merge() compares it with an aware time: a naive one is malformed.
            if parse_time(values["completed_at"]).tzinfo is None:
                continue
        except ValueError:
            continue
        if (values["kind"] not in ("job", "pytest") or not 0 < values["minutes"] < 10_000
                or not 0 < values["bound"] < 10_000
                or (values["kind"] == "pytest" and values["selection"] not in SELECTIONS)):
            continue
        found.append(Finding(**values))
    return found


def cell(text: str) -> str:
    """A table cell in plain characters: no markup, link, mention or pipe
    can reach the issue from a job name or a runner's text."""
    return PLAIN.sub("?", str(text))[:120]


def render(findings: list[Finding], now: datetime) -> str:
    rows = []
    for finding in findings:
        what = "job timeout-minutes" if finding.kind == "job" else f"pytest bound ({finding.selection})"
        if finding.note:
            what += f"; {finding.note}"
        rows.append(f"| {cell(finding.workflow)} | {cell(finding.job)} | {cell(what)} | "
                    f"[{finding.run_id}](https://github.com/{REPOSITORY}/actions/runs/{finding.run_id}"
                    f"/job/{finding.job_id}) attempt {finding.attempt} | {cell(finding.cpu)} | "
                    f"{finding.minutes:.1f} of {finding.bound:g} min | **{finding.share:.0%}** | "
                    f"{cell(finding.conclusion)} | {cell(finding.completed_at[:10])} |")
    # ">" only occurs inside JSON strings, so escaping it keeps "-->" in a
    # job name from ending the comment early.
    state = json.dumps({"findings": [asdict(finding) for finding in findings]},
                       sort_keys=True).replace(">", "\\u003e")
    return "\n".join([
        f"`rust/ci/ci_headroom.py` found hosted runs that used more than {THRESHOLD:.0%} of a time "
        "bound in the last "
        f"{RETENTION_DAYS} days. A slower runner CPU can push such a job over its bound and fail "
        "a run that would have passed. Raise the bound with headroom over the slowest run (its comment "
        "names the measurement), or make the job faster.",
        "",
        "A row leaves this issue when its bound no longer has it above the threshold or after "
        f"{RETENTION_DAYS} days; the issue closes itself when no row is left.",
        "",
        "| Workflow | Job | Bound | Run | Runner CPU | Used | Share | Conclusion | Date (UTC) |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        f"Checked {now.strftime('%Y-%m-%d %H:%M')} UTC.",
        f"<!-- ci-headroom-state {state} -->",
    ])


class GitHubError(RuntimeError):
    """A gh call failed; the message carries gh's own words (a rate limit,
    a 404, a missing login)."""


class GitHub:
    """The calls this check makes, through the gh CLI."""

    @staticmethod
    def call(command: list[str], *, data: bytes | None = None) -> bytes:
        done = subprocess.run(command, input=data, capture_output=True)
        if done.returncode != 0:
            said = done.stderr.decode("utf-8", "replace").strip() or f"exit {done.returncode}"
            raise GitHubError(f"{' '.join(command[:4])} failed: {said}")
        return done.stdout

    def json(self, path: str, *, method: str = "GET", fields: dict | None = None):
        command = ["gh", "api", "-X", method, path]
        if fields is not None:
            command += ["--input", "-"]
        out = self.call(command, data=json.dumps(fields).encode() if fields is not None else None)
        return json.loads(out) if out.strip() else None

    def pages(self, path: str, key: str) -> list[dict]:
        out = self.call(["gh", "api", "--paginate", path, "--jq", f".{key}[]"]).decode("utf-8")
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def bytes(self, path: str) -> bytes:
        return self.call(["gh", "api", path])


def shortest_bound(bounds: Bounds, workflow: str, run: dict) -> float | None:
    """The smallest bound any job of this workflow has for this run."""
    found = [job_bound(workflow, shape, combo, run)[0]
             for shape in bounds.shapes.get(workflow, [])
             for combo in (list(shape.combos.values()) or [None])]
    found = [bound for bound in found if bound is not None]
    return min(found) if found else None


def too_short(bounds: Bounds, workflow: str, run: dict) -> bool:
    """A first attempt that ran for less than THRESHOLD of the workflow's
    smallest bound holds no finding: no job of it ran longer than the run.
    (A later attempt's run times leave out the earlier attempts' jobs.) It
    saves most calls: on 2026-11-12..26, 711 of 917 ci.yml runs, every
    python-freeze.yml run and 35 of 1000 rust.yml runs."""
    if run.get("run_attempt") != 1 or not run.get("run_started_at") or not run.get("updated_at"):
        return False
    smallest = shortest_bound(bounds, workflow, run)
    return smallest is not None and minutes_between(run["run_started_at"], run["updated_at"]) < THRESHOLD * smallest


def recent_runs(github, workflow: str, since: datetime, now: datetime) -> list[dict]:
    """The completed runs with activity since ``since``. A rerun keeps its
    run's creation time, so the listing covers every run created in the
    retention window, one UTC day per query (a listing stops at 1000 runs),
    and keeps those updated since then: a job rerun today of a run created a
    week ago is read too."""
    found: dict[int, dict] = {}
    day = (now - timedelta(days=RETENTION_DAYS)).date()
    while day <= now.date():
        for run in github.pages(f"repos/{REPOSITORY}/actions/workflows/{workflow}/runs"
                                f"?status=completed&created={day.isoformat()}&per_page=100", "workflow_runs"):
            updated = run.get("updated_at")
            if updated is None or parse_time(updated) >= since:
                found.setdefault(int(run["id"]), run)
        day += timedelta(days=1)
    return list(found.values())


def read_runs(github, bounds: Bounds, since: datetime, now: datetime | None = None) -> Reading:
    reading = Reading()
    now = now or datetime.now(timezone.utc)
    for workflow in sorted(bounds.shapes):
        for run in recent_runs(github, workflow, since, now):
            # A fork's pull request runs the fork's own workflow file and
            # uploads what its branch wrote: its job names and artifacts are
            # its author's text. A fork runs only the fast lanes anyway.
            if (run.get("head_repository") or {}).get("full_name") != REPOSITORY:
                reading.forks += 1
                continue
            if too_short(bounds, workflow, run):
                reading.short += 1
                continue
            jobs = github.pages(f"repos/{REPOSITORY}/actions/runs/{run['id']}/jobs?filter=all&per_page=100", "jobs")
            cache: dict = {}

            def diagnostics(job, run=run, cache=cache):
                if "artifacts" not in cache:
                    cache["artifacts"] = github.pages(
                        f"repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts?per_page=100", "artifacts")
                artifact = artifact_for(job, cache["artifacts"])
                if artifact is None or int(artifact.get("size_in_bytes") or 0) > ARTIFACT_BYTES:
                    return None
                # One unreadable artifact costs its pytest reading, not the pass.
                try:
                    members = read_members(github.bytes(f"repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"))
                except (GitHubError, zipfile.BadZipFile) as error:
                    reading.unreadable[f"{run['id']}/{job['id']}"] = str(error)[:200]
                    return None
                return members.get("cpu.txt", ""), members.get("process.txt", "")

            examine(bounds, workflow, run, jobs, diagnostics, reading)
    return reading


def open_issue(github) -> dict | None:
    issues = github.pages(f"repos/{REPOSITORY}/issues?state=open&per_page=100", "")
    mine = [issue for issue in issues if issue.get("title") == TITLE and "pull_request" not in issue
            and (issue.get("user") or {}).get("login") == AUTHOR]
    return min(mine, key=lambda issue: issue["number"]) if mine else None


def sync(github, bounds: Bounds, current: list[Finding], now: datetime) -> str:
    """Create, update or close the one issue; returns what it did."""
    issue = open_issue(github)
    findings = merge(bounds, parse_state(issue["body"]) if issue else [], current, now)
    if not findings:
        if issue is None:
            return "no finding; no issue open"
        github.json(f"repos/{REPOSITORY}/issues/{issue['number']}/comments", method="POST",
                    fields={"body": "Every job is back under the threshold, or its near miss is older than "
                                    f"{RETENTION_DAYS} days. Closing."})
        github.json(f"repos/{REPOSITORY}/issues/{issue['number']}", method="PATCH",
                    fields={"state": "closed", "state_reason": "completed"})
        return f"closed #{issue['number']}"
    body = render(findings, now)
    if issue is None:
        created = github.json(f"repos/{REPOSITORY}/issues", method="POST", fields={"title": TITLE, "body": body})
        return f"opened #{created['number']} with {len(findings)} findings"
    if STATE.search(issue.get("body") or "") and parse_state(issue["body"]) == findings:
        return f"#{issue['number']} unchanged ({len(findings)} findings)"
    github.json(f"repos/{REPOSITORY}/issues/{issue['number']}", method="PATCH", fields={"body": body})
    return f"updated #{issue['number']} with {len(findings)} findings"


# --- Command line --------------------------------------------------------------

def summary(reading: Reading) -> str:
    lines = [f"read {reading.runs} runs, {reading.jobs} job attempts ({reading.forks} fork runs left unread, "
             f"{reading.short} runs too short to near a bound); "
             f"{len(reading.findings)} at {THRESHOLD:.0%} or more of a bound"]
    for finding in sorted(reading.findings, key=lambda finding: -finding.share):
        what = "job" if finding.kind == "job" else f"pytest {finding.selection}"
        lines.append(f"  {finding.share:6.1%}  {finding.workflow}: {finding.job} [{what}] run {finding.run_id} "
                     f"attempt {finding.attempt}: {finding.minutes:.1f} of {finding.bound:g} min"
                     f"{'; ' + finding.note if finding.note else ''}; CPU {finding.cpu}")
    if reading.unplaced:
        lines.append("jobs no current workflow defines (renamed or removed since they ran): "
                     + ", ".join(f"{name} x{count}" for name, count in sorted(reading.unplaced.items())))
    for name, note in sorted(reading.unresolved.items()):
        lines.append(f"no bound for {name}: {note}")
    for name, error in sorted(reading.unreadable.items()):
        lines.append(f"diagnostics artifact of run/job {name} unreadable: {error}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("report", "sync"),
                        help="report prints the findings; sync keeps them in one issue")
    parser.add_argument("--days", type=int, default=2,
                        help="read runs created in the last N days (default 2; the daily schedule overlaps)")
    options = parser.parse_args(argv)
    if not 1 <= options.days <= RETENTION_DAYS:
        parser.error(f"--days must be between 1 and {RETENTION_DAYS}")
    if options.command == "sync" and options.days > SYNC_DAYS:
        parser.error(f"sync reads at most {SYNC_DAYS} days: the workflow's token has 1000 API calls an hour, "
                     "shared with every other workflow of the repository; read further back with "
                     f"`report --days {options.days}` under your own login")
    now = datetime.now(timezone.utc)
    bounds = Bounds.from_checkout()
    github = GitHub()
    reading = read_runs(github, bounds, now - timedelta(days=options.days), now)
    print(summary(reading))
    if options.command == "sync":
        print(sync(github, bounds, reading.findings, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
