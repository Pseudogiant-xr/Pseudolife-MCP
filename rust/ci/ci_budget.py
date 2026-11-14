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
import base64
import copy
import fnmatch
import hashlib
import itertools
import json
import math
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import uuid

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
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # PyYAML reads an unquoted on: as True; a quoted 'on': stays a string.
        triggers = loaded.get(True) or loaded.get("on") or {}
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
TOKENS = re.compile(r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<number>-?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))"
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
    """GitHub's &&: the left operand if it is falsy, else the right one."""
    known = truthy(left)
    if known is not UNKNOWN:
        return right if known else left
    return False if truthy(right) is False else UNKNOWN


def either(left, right):
    """GitHub's ||: the left operand if it is truthy, else the right one."""
    known = truthy(left)
    if known is not UNKNOWN:
        return left if known else right
    return UNKNOWN


def number(value) -> float:
    """GitHub's coercion to a number for a comparison of unlike types."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip() or 0)
        except ValueError:
            return math.nan
    return math.nan


def equal(left, right):
    """GitHub's ==: strings compare ignoring case; unlike types compare as
    numbers (null, false and '' are all 0)."""
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    if type(left) is type(right):
        return left == right
    return number(left) == number(right)


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
        if self.index >= len(self.items):
            raise ValueError("the expression ends early")
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
            return float(int(text, 16)) if "0x" in text.lower() else float(text)
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
    try:
        return truthy(Evaluator(event, needs_of(job), skipped).run(text)) is not False
    except ValueError:
        # A condition this reader cannot follow might hold: it counts.
        return True


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


def runner_os(label: str) -> str:
    """The OS and architecture rust-cache keys on, from a runs-on label."""
    text = label.lower()
    family = ("Windows" if "windows" in text else "Linux" if "ubuntu" in text or "linux" in text
              else "macOS" if "macos" in text else label)
    return family + ("-ARM64" if "arm" in text else "")


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
            if EXPRESSION.fullmatch(str(job.get("runs-on", ""))):
                try:
                    # A conditional pool selector is still known for the
                    # suites that cannot move. Keep uncertain paths unknown.
                    labels = [
                        Evaluator(event, needs_of(job), set(), combo).run(condition_text(job["runs-on"]))
                        for event in (*BUDGET_EVENTS, "schedule", "workflow_dispatch")
                    ]
                    if all(isinstance(label, str) for label in labels) and len(set(labels)) == 1:
                        runs_on = labels[0]
                except ValueError:
                    pass
            for step in job.get("steps", []):
                if not str(step.get("uses", "")).startswith("Swatinem/rust-cache"):
                    continue
                options = step.get("with", {})
                conditions = [condition_text(step.get("if", "true")), condition_text(options.get("save-if", "true"))]
                def may_hold(text, event):
                    try:
                        return truthy(Evaluator(event, needs_of(job), set(), combo).run(text)) is not False
                    except ValueError:
                        return True
                if not any(all(may_hold(text, event) for text in conditions if text)
                           for event in (*BUDGET_EVENTS, "schedule", "workflow_dispatch")):
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
                savers.setdefault((key, runner_os(runs_on)), []).append(label)
    # Unknown platforms may overlap any known platform with the same key.
    for (key, platform), owners in list(savers.items()):
        if platform == "?":
            for known_key, known_platform in list(savers):
                if known_key == key and known_platform != "?":
                    savers[(known_key, known_platform)].extend(owners)
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


# Generated baselines and independently mergeable author reasons. Numeric
# budgets remain in ci_budget.json; accepting a lane never rewrites them.
def fingerprint_path() -> Path:
    return BUDGET.with_name("ci_budget_fingerprints.json")


def acceptance_directory() -> Path:
    return BUDGET.parent / "ci_budget_accept"


def scope_matrix(job: dict) -> bool:
    """Whether matrix expressions read the parity scope outputs, not merely
    quote their names in a string or refer to another object's needs field."""
    def reads(value):
        if isinstance(value, dict):
            return any(reads(item) for item in value.values())
        if isinstance(value, list):
            return any(reads(item) for item in value)
        if not isinstance(value, str):
            return False
        expressions, cursor = [], 0
        while (start := value.find("${{", cursor)) >= 0:
            end, quoted = start + 3, False
            while end < len(value):
                if value[end] == "'":
                    if quoted and value[end:end + 2] == "''":
                        end += 2
                        continue
                    quoted = not quoted
                if not quoted and value[end:end + 2] == "}}":
                    expressions.append(value[start + 3:end])
                    cursor = end + 2
                    break
                end += 1
            else:
                return True
        for expression in expressions:
            try:
                parts = tokens(expression)
            except ValueError:
                return True  # An unreadable matrix expression cannot establish independence.
            for index, (kind, word) in enumerate(parts):
                if kind != "name" or word.lower() != "needs" or index and parts[index - 1][1] == ".":
                    continue
                path, cursor = ["needs"], index + 1
                while cursor < len(parts):
                    if parts[cursor][1] == "." and cursor + 1 < len(parts) and parts[cursor + 1][0] == "name":
                        path.append(parts[cursor + 1][1].lower())
                        cursor += 2
                    elif (parts[cursor][1] == "[" and cursor + 2 < len(parts)
                          and parts[cursor + 1][0] == "string" and parts[cursor + 2][1] == "]"):
                        path.append(parts[cursor + 1][1][1:-1].replace("''", "'").lower())
                        cursor += 3
                    elif parts[cursor][1] == "[" and path in (["needs"], ["needs", "scope"], ["needs", "scope", "outputs"]):
                        return True
                    else:
                        break
                if path[:3] == ["needs", "scope", "outputs"] and len(path) > 3 and path[3] in ("parity", "parity_selfhosted"):
                    return True
        return False
    return reads((job.get("strategy") or {}).get("matrix"))


def definition_fingerprints(name: str, raw: dict, scope_source: str | None = None) -> dict[str, str]:
    """Derive job identities from YAML and helper source as data only.

    The raw matrix plus its scope helper's digest binds exactly the same
    definitions without executing a member's resolver inside integrate.py.
    """
    result = {}
    for job_id in pull_request_reachable(raw):
        job = raw["jobs"][job_id]
        if name == "rust.yml" and scope_matrix(job):
            if scope_source is None:
                result[job_id] = UNRESOLVED
                continue
            source = scope_source.replace("\r\n", "\n")
            job = {"definition": job, "parity_scope_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}
        result[job_id] = fingerprint(job)
    return result


def current_definitions(name: str, raw: dict | None = None) -> dict[str, str]:
    source = (ROOT / "rust/ci/parity_scope.py").read_text(encoding="utf-8") if name == "rust.yml" else None
    return definition_fingerprints(name, raw if raw is not None else load(name, resolved=False), source)


def parse_fingerprint_baseline(document) -> dict:
    if (not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != 1
            or not isinstance(document.get("workflows"), dict)):
        raise ValueError("invalid generated fingerprint baseline")
    for name, jobs in document["workflows"].items():
        if not isinstance(name, str) or not isinstance(jobs, dict) or any(
                not isinstance(job, str) or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{16}", value)
                for job, value in jobs.items()):
            raise ValueError("invalid generated fingerprint baseline")
    return document["workflows"]


def read_fingerprint_baseline() -> dict:
    try:
        return parse_fingerprint_baseline(json.loads(fingerprint_path().read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as error:
        raise ValueError("generated fingerprint baseline is unavailable or malformed") from error


def parse_acceptance(record, *, legacy=False) -> dict:
    if not isinstance(record, dict) or not isinstance(record.get("workflow"), str) or not isinstance(record.get("job"), str):
        raise ValueError("acceptance must name its workflow and job")
    if not isinstance(record.get("reason"), str) or not record["reason"].strip():
        raise ValueError("acceptance needs a nonempty reason")
    value = record.get("fingerprint")
    if ("fingerprint" not in record or value is None and legacy or value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{16}", value))):
        raise ValueError("acceptance needs a definition fingerprint")
    if not legacy and ("before" not in record or record["before"] is not None and (
            not isinstance(record["before"], str) or not re.fullmatch(r"[0-9a-f]{16}", record["before"]))):
        raise ValueError("acceptance needs its baseline fingerprint")
    if not legacy and record["before"] is None and value is None:
        raise ValueError("acceptance must identify an existing or new definition")
    return {**record, "legacy": legacy}


def read_acceptances() -> tuple[list[dict], list[str]]:
    records, errors = [], []
    for path in sorted(acceptance_directory().glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
            records.extend(parse_acceptance_document(document))
        except (OSError, ValueError, TypeError) as error:
            errors.append(f"{path.name}: invalid CI budget acceptance ({type(error).__name__})")
    return records, errors


def parse_acceptance_document(document) -> list[dict]:
    if (not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != 1
            or "legacy" in document and type(document["legacy"]) is not bool):
        raise ValueError("invalid acceptance schema")
    if document.get("legacy") is True:
        if not isinstance(document.get("accepted"), list):
            raise ValueError("invalid legacy acceptance history")
        return [parse_acceptance(row, legacy=True) for row in document["accepted"]]
    return [parse_acceptance(document)]


def approval_reason(records: list[dict], name: str, job: str, before, current: str | None) -> str | None:
    return next((row["reason"] for row in records if not row.get("legacy") and row.get("workflow") == name
                 and row.get("job") == job and row.get("before") == before and row.get("fingerprint") == current
                 and isinstance(row.get("reason"), str) and row["reason"].strip()), None)


def write_acceptance(name: str, job: str, before, current: str | None, reason: str) -> Path:
    document = {"schema": 1, "workflow": name, "job": job, "before": before,
                "fingerprint": current, "reason": reason.strip()}
    parse_acceptance(document)
    directory = acceptance_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now(timezone.utc):%Y-%m-%d}-{uuid.uuid4().hex}.json"
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(document, indent=2) + "\n")
    return path


def recorded_definitions(name: str, entry: dict) -> tuple[dict, bool]:
    if "fingerprints" in entry:
        if not isinstance(entry["fingerprints"], dict):
            raise ValueError("malformed legacy fingerprints")
        return entry["fingerprints"], True
    # A workflow the generated baseline does not name yet is new: every job's
    # earlier definition is None, so each needs an accepted reason, as
    # ops/integrate.py's member check treats it (baselines.get(name, {})).
    return read_fingerprint_baseline().get(name, {}), False


def approved_definitions(name: str, workflow: dict, entry: dict, raw: dict,
                         records: list[dict]) -> tuple[dict, dict]:
    recorded, legacy = recorded_definitions(name, entry)
    current = current_definitions(name, raw)
    comparable = fingerprints(workflow) if legacy else current
    approved = {job: value for job, value in current.items() if value != UNRESOLVED and (
        recorded.get(job) == comparable.get(job) or approval_reason(records, name, job, recorded.get(job), value))}
    return current, approved


def unapproved_removals(name: str, entry: dict, raw: dict | None, records: list[dict]) -> list[str]:
    """Jobs the generated baseline records that the workflow no longer has,
    with no accepted reason for the removal. ops/integrate.py's member check
    refuses such a pull request, so `check` names them too."""
    recorded, legacy = recorded_definitions(name, entry)
    if legacy:
        return []
    current = current_definitions(name, raw)
    return sorted(job for job, before in recorded.items()
                  if job not in current and not approval_reason(records, name, job, before, None))


def budget_data(document: dict) -> dict:
    if not isinstance(document, dict) or any(not isinstance(entry, dict) for entry in document.values()):
        raise ValueError("malformed authored CI budget data")
    return {name: {key: copy.deepcopy(value) for key, value in entry.items() if key not in ("fingerprints", "accepted")}
            for name, entry in document.items()}


def merge_budget_data(base: dict, ours: dict, theirs: dict) -> dict:
    """Merge authored data only; incompatible prices/limits still conflict."""
    missing = object()
    def merge(before, left, right, path=()):
        if left == right:
            return copy.deepcopy(left) if left is not missing else missing
        if left == before:
            return copy.deepcopy(right) if right is not missing else missing
        if right == before:
            return copy.deepcopy(left) if left is not missing else missing
        if isinstance(left, dict) and isinstance(right, dict):
            old = before if isinstance(before, dict) else {}
            result = {}
            for key in dict.fromkeys([*old, *left, *right]):
                value = merge(old.get(key, missing), left.get(key, missing), right.get(key, missing), (*path, key))
                if value is not missing:
                    result[key] = value
            return result
        raise ValueError("semantic CI budget conflict: " + ".".join(path))
    return merge(budget_data(base), budget_data(ours), budget_data(theirs))


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


def _contents(path: str, sha: str) -> str:
    listing = json.loads(subprocess.run(["gh", "api", f"repos/{REPOSITORY}/contents/{path}?ref={sha}"],
                                        check=True, capture_output=True, text=True).stdout)
    return base64.b64decode(listing["content"]).decode("utf-8")


# rust/ci/parity_scope.py fills these jobs' matrices (suites, rows, parts
# and budgets per shard), so a run with another parity_scope ran other jobs.
SCOPE_RESOLVED = "needs.scope.outputs.parity"
# A job's definition at a commit that cannot be settled from what is read.
UNRESOLVED = "<unresolved>"


def definitions_at(name: str, sha: str) -> dict[str, str]:
    """The fingerprints of the jobs a pull request starts in this workflow
    as it stood at ``sha``. Nothing fetched is run: the YAML is parsed, and
    rust.yml is resolved with this checkout's parity_scope. Where that
    commit's parity_scope.py differs from this one, the jobs whose matrix it
    fills are UNRESOLVED, since their shards may have run other rows."""
    import yaml
    raw = yaml.safe_load(_contents(f".github/workflows/{name}", sha))
    source = _contents("rust/ci/parity_scope.py", sha) if name == "rust.yml" else None
    found = definition_fingerprints(name, raw, source)
    if name != "rust.yml":
        return found
    local = (ROOT / "rust/ci/parity_scope.py").read_text(encoding="utf-8")
    if source.replace("\r\n", "\n") != local.replace("\r\n", "\n"):
        found = {job: UNRESOLVED if scope_matrix(raw["jobs"][job]) else value
                 for job, value in found.items()}
    return found


def estimates(entry: dict) -> list[str]:
    return sorted(name for name, job in entry["jobs"].items() if "estimate" in job)


def profile_problems(name: str, entry: dict, reachable: list[str]) -> list[str]:
    """Separate observations never replace the full-run prices or limits."""
    profiles = entry.get("profiles", {})
    if not isinstance(profiles, dict) or set(profiles) - {"ordinary_pr"}:
        return [f"{name}: invalid measured profiles"]
    found = []
    for label, profile in profiles.items():
        prefix = f"{name}: profile {label}"
        if not isinstance(profile, dict) or set(profile) != {"measured_on", "jobs", "why"}:
            found.append(f"{prefix} has invalid fields")
            continue
        runs = profile["measured_on"]
        if (not isinstance(runs, list) or len(runs) < 2
                or any(type(run) is not int or run <= 0 for run in runs)
                or len(set(runs)) != len(runs)):
            found.append(f"{prefix} needs distinct measured run IDs")
        if not isinstance(profile["why"], str) or not profile["why"].strip():
            found.append(f"{prefix} needs a measurement reason")
        jobs = profile["jobs"]
        if not isinstance(jobs, dict) or not jobs:
            found.append(f"{prefix} has no measured jobs")
            continue
        for job, value in jobs.items():
            minutes = value.get("minutes") if isinstance(value, dict) else None
            if (job not in reachable or not isinstance(value, dict) or set(value) != {"minutes"}
                    or type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes < 0):
                found.append(f"{prefix} has an invalid job measurement: {job}")
    return found


def problems(name: str, workflow: dict, budget: dict, raw: dict | None = None) -> list[str]:
    """Everything that makes ``workflow`` disagree with its budget entry.
    ``raw`` is the unresolved workflow, whose matrix expressions still name
    the outputs a job reads; it defaults to ``workflow``."""
    entry = budget[name]
    raw = raw or workflow
    reachable = reachable_names(workflow)
    malformed = sorted(job for job, value in entry["jobs"].items()
                       if not isinstance(value.get("minutes"), (int, float)) or isinstance(value.get("minutes"), bool))
    if malformed:
        return [f"{name}: budget entries without numeric minutes: {malformed}"]
    found = profile_problems(name, entry, reachable)
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
    records, acceptance_errors = read_acceptances()
    found.extend(acceptance_errors)
    try:
        current, approved = approved_definitions(name, workflow, entry, raw, records)
    except (OSError, ValueError) as error:
        found.append(f"{name}: fingerprint state unavailable ({error})")
        current, approved = fingerprints(workflow), {}
    for job_id in current:
        if job_id not in approved:
            found.append(f"{name}: job {job_id} changed since its minutes were measured, so they may no "
                         f"longer hold. Re-measure it (python rust/ci/ci_budget.py measure --workflow {name} "
                         "<run> <run>), or record why its cost is unchanged: python rust/ci/ci_budget.py accept "
                         f"--workflow {name} --job {job_id} --reason \"...\"")
    try:
        removed_jobs = unapproved_removals(name, entry, raw, records)
    except (OSError, ValueError) as error:
        found.append(f"{name}: fingerprint state unavailable ({error})")
        removed_jobs = []
    for job_id in removed_jobs:
        found.append(f"{name}: job {job_id} was removed since its minutes were measured, and the batch "
                     "tool's member check wants a reviewed reason for that too: python rust/ci/ci_budget.py "
                     f"accept --workflow {name} --job {job_id} --reason \"...\" (what it ran and what "
                     "removing it saves)")
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


def fetch_run(run_id: str) -> dict:
    return json.loads(subprocess.run(["gh", "api", f"repos/{REPOSITORY}/actions/runs/{run_id}"],
                                     check=True, capture_output=True, text=True).stdout)


def on_master(sha: str) -> bool:
    """Whether ``sha`` is master or one of its ancestors."""
    compared = json.loads(subprocess.run(["gh", "api", f"repos/{REPOSITORY}/compare/{sha}...master"],
                                         check=True, capture_output=True, text=True).stdout)
    return compared["behind_by"] == 0


def merge_base(base: str, head: str) -> str:
    compared = json.loads(subprocess.run(["gh", "api", f"repos/{REPOSITORY}/compare/{base}...{head}"],
                                         check=True, capture_output=True, text=True).stdout)
    return compared["merge_base_commit"]["sha"]


def executed_definitions(name: str, run: dict) -> dict[str, str]:
    """The job definitions a run executed, where its head settles them. A
    push or merge group runs its head. A pull request runs the merge of its
    head into its base: each job is the head's where the branch changed it
    since the merge base, and the base's where it did not. A job whose merge
    differs from the head is left out (so measure counts its times stale):
    the API reports the pull request's base as it is now, which a retarget
    or a moved base may have changed since the run."""
    head = definitions_at(name, run["head_sha"])
    requests = run.get("pull_requests") or []
    if run.get("event") != "pull_request":
        return head
    if not requests:
        # A closed pull request's run lists none. A head master contains
        # (a merged batch's) ran as itself; any other is unsettled.
        return head if on_master(run["head_sha"]) else {}
    base_sha = requests[0]["base"]["sha"]
    base = definitions_at(name, base_sha)
    common = definitions_at(name, merge_base(base_sha, run["head_sha"]))
    merged = {job: head.get(job) if head.get(job) != common.get(job) else base.get(job)
              for job in set(head) | set(base)}
    # A job unresolved at any of the three commits is never credited: its
    # merge cannot be settled (Codex review of 7db7ce41).
    return {job: value for job, value in head.items() if merged.get(job) == value
            and UNRESOLVED not in (value, base.get(job), common.get(job))}


def stale_definitions(name: str, run_ids: list[str], current: dict[str, str]) -> dict[str, list[str]]:
    """For each job whose definition differs from ``current`` in what one
    of these runs executed: those runs. Their minutes measured another job.
    A run whose workflow cannot be read (a fork's, or one older than these
    files) counts as stale for every job."""
    stale: dict[str, list[str]] = {}
    for run_id in run_ids:
        run = fetch_run(run_id)
        try:
            executed = executed_definitions(name, run)
        except (subprocess.CalledProcessError, KeyError, ValueError) as error:
            print(f"run {run_id}: its workflow at {run['head_sha'][:12]} cannot be read "
                  f"({type(error).__name__}); its times are left out", file=sys.stderr)
            executed = {}
        for job_id, value in current.items():
            if executed.get(job_id) != value:
                stale.setdefault(job_id, []).append(run_id)
    return stale


def measure(run_ids: list[str]) -> dict[str, dict[str, float]]:
    """Each job's minutes in each of these completed runs: successful jobs
    of each run's latest attempt only (a failed or cancelled job's time says
    nothing about its cost)."""
    durations: dict[str, dict[str, float]] = {}
    for run_id in run_ids:
        run = fetch_run(run_id)
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
            durations.setdefault(job["name"], {})[run_id] = (end - start).total_seconds() / 60
    return durations


def remeasure(entry: dict, workflow: dict, samples: dict[str, dict[str, float]],
              stale: dict[str, list[str]], current: dict[str, str] | None = None) -> list[str]:
    """Rewrite ``entry``'s minutes from ``samples``: each job's median over
    the runs that ran its current definition. A job with no such sample
    keeps its earlier minutes only if its definition is unchanged since
    they were taken. Returns the jobs left with neither (nothing changes
    then)."""
    current = fingerprints(workflow) if current is None else current
    jobs, missing = {}, []
    for job_id in pull_request_reachable(workflow):
        for name in expand(job_id, workflow["jobs"][job_id]):
            fresh = [minutes for run, minutes in samples.get(name, {}).items() if run not in stale.get(job_id, [])]
            if fresh:
                jobs[name] = {"minutes": round(statistics.median(fresh), 1)}
            elif name in entry["jobs"] and entry.get("fingerprints", {}).get(job_id) == current[job_id]:
                jobs[name] = entry["jobs"][name]
            else:
                missing.append(name)
    if not missing:
        entry["jobs"] = jobs
        entry["fingerprints"] = current
        entry.pop("accepted", None)
    return missing


def profile_jobs(workflow: dict, samples: dict[str, dict[str, float]],
                 stale: dict[str, list[str]]) -> dict:
    """Price only observed jobs, requiring two current samples for each."""
    jobs = {}
    for job_id in pull_request_reachable(workflow):
        for name in expand(job_id, workflow["jobs"][job_id]):
            if name not in samples:
                continue
            fresh = [minutes for run, minutes in samples[name].items() if run not in stale.get(job_id, [])]
            if len(fresh) < 2:
                raise ValueError(f"{name} needs two successful current-definition samples")
            jobs[name] = {"minutes": round(statistics.median(fresh), 1)}
    if not jobs:
        raise ValueError("profile has no successful current-definition jobs")
    return jobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="report every disagreement with the budget")
    check.add_argument("--workflow", action="append")
    update = commands.add_parser("measure", help="rewrite a workflow's measured minutes from hosted runs")
    update.add_argument("--workflow", required=True)
    update.add_argument("--profile", choices=("batch", "ordinary-pr"), default="batch")
    update.add_argument("--reason", help="required for a separate ordinary-PR measurement profile")
    update.add_argument("runs", nargs="+")
    estimate = commands.add_parser("estimate", help="enter a job no hosted run can measure yet")
    estimate.add_argument("--workflow", required=True)
    estimate.add_argument("--job", required=True, help="the job name GitHub reports, e.g. 'Parity checks / ubuntu-latest / x'")
    estimate.add_argument("--minutes", required=True, type=float)
    estimate.add_argument("--reason", required=True, help="how the minutes were derived (steps' times on a named run)")
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
    if options.command == "estimate":
        if not options.reason.strip() or options.minutes <= 0:
            parser.error("--reason must say how the minutes were derived, and --minutes must be positive")
        workflow = load(options.workflow)
        templates = [job_id for job_id in pull_request_reachable(workflow)
                     if options.job in expand(job_id, workflow["jobs"][job_id])]
        if not templates:
            parser.error(f"no job a pull request starts in {options.workflow} is named {options.job!r}")
        entry = budget[options.workflow]
        if options.job in entry["jobs"] and "estimate" not in entry["jobs"][options.job]:
            parser.error(f"{options.job} already has measured minutes; an estimate is for a job no run has "
                         "measured (use accept for a changed job whose cost holds)")
        entry["jobs"][options.job] = {"minutes": options.minutes, "estimate": options.reason.strip()}
        BUDGET.write_text(json.dumps(budget, indent=2) + "\n", encoding="utf-8")
        print(f"{options.workflow}: {options.job} estimated at {options.minutes} minutes; re-measure it "
              f"from the batch run. Its job {templates[0]} changed, so the check also needs that job "
              "re-measured, or accepted with a reason saying why its other runs cost what they did")
        return 0
    if options.command == "accept":
        if not options.reason.strip():
            parser.error("--reason must say why the job's cost is unchanged")
        try:
            recorded, _ = recorded_definitions(options.workflow, budget[options.workflow])
            try:
                raw = load(options.workflow, resolved=False)
            except FileNotFoundError:
                current = {}
            else:
                current = current_definitions(options.workflow, raw)
            if options.job not in current and options.job not in recorded:
                parser.error(f"{options.job} is not a current or recorded job in {options.workflow}")
            path = write_acceptance(options.workflow, options.job, recorded.get(options.job), current.get(options.job), options.reason)
        except (KeyError, OSError, ValueError) as error:
            parser.error(str(error))
        print(f"{options.workflow}: {options.job} accepted as unchanged in cost: {options.reason.strip()} "
              f"(ci_budget_accept/{path.name})")
        return 0
    if len(options.runs) < 2:
        parser.error("measure takes two or more runs: one hosted run is too noisy to budget on")
    if options.profile == "ordinary-pr":
        if not options.reason or not options.reason.strip():
            parser.error("ordinary-PR profile needs --reason describing the measured selection")
        for run_id in options.runs:
            run = fetch_run(run_id)
            if (run.get("event") != "pull_request" or run.get("status") != "completed"
                    or run.get("conclusion") != "success" or not run.get("head_branch")
                    or run["head_branch"].casefold().startswith("integrate/")
                    or run.get("path", "").split("@", 1)[0] != f".github/workflows/{options.workflow}"):
                parser.error(f"ordinary-PR profile run {run_id} is not a successful ordinary PR of this workflow")
    workflow = load(options.workflow)
    reachable = reachable_names(workflow)
    raw = load(options.workflow, resolved=False)
    current = current_definitions(options.workflow, raw)
    stale = stale_definitions(options.workflow, options.runs, current)
    for job_id, runs in sorted(stale.items()):
        print(f"{job_id}: runs {runs} ran an older definition; their times for it are left out")
    entry = budget[options.workflow]
    records, errors = read_acceptances()
    if errors:
        parser.error("; ".join(errors))
    try:
        _, approved = approved_definitions(options.workflow, workflow, entry, raw, records)
        recorded, _ = recorded_definitions(options.workflow, entry)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if options.profile == "ordinary-pr":
        try:
            profile = {"measured_on": [int(run) for run in options.runs], "why": options.reason.strip(),
                       "jobs": profile_jobs(workflow, measure(options.runs), stale)}
            candidate = dict(entry, profiles={**entry.get("profiles", {}), "ordinary_pr": profile})
            errors = profile_problems(options.workflow, candidate, reachable)
            if errors:
                raise ValueError("; ".join(errors))
        except ValueError as error:
            parser.error(str(error))
        entry["profiles"] = candidate["profiles"]
        BUDGET.write_text(json.dumps(budget, indent=2) + "\n", encoding="utf-8")
        minutes = sum(job["minutes"] for job in profile["jobs"].values())
        print(f"{options.workflow}: ordinary-PR profile records {len(profile['jobs'])} jobs, "
              f"{minutes:.1f} runner-minutes; full-batch prices and limits unchanged")
        return 0
    measured = copy.deepcopy(entry)
    measured["fingerprints"] = approved
    missing = remeasure(measured, workflow, measure(options.runs), stale, current)
    if missing:
        print(f"no successful run of the current definition of {missing} in these runs, and no earlier "
              "measurement of it holds; add a run that has one", file=sys.stderr)
        return 1
    for job, value in current.items():
        if job not in approved:
            write_acceptance(options.workflow, job, recorded.get(job), value,
                             f"Re-measured from hosted runs {', '.join(options.runs)}")
    for job in recorded.keys() - current.keys():
        write_acceptance(options.workflow, job, recorded[job], None,
                         f"Job retired; re-measured from hosted runs {', '.join(options.runs)}")
    entry["jobs"] = measured["jobs"]
    entry["measured_on"] = [int(run) for run in options.runs]
    BUDGET.write_text(json.dumps(budget, indent=2) + "\n", encoding="utf-8")
    total = sum(job["minutes"] for job in entry["jobs"].values())
    print(f"{options.workflow}: {len(reachable)} jobs, {total:.1f} runner-minutes, limits {entry['limits']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
