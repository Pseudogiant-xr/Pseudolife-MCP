"""Build hand-rolled merge batches of reviewed pull requests.

Master takes one pull request at a time, and each one must pass CI on its
head merged with current master, so every merge sends the other open pull
requests back for a fresh CI run. A batch merges several reviewed pull
requests into one `integrate/<YYYY-MM-DD>-batch-N` branch that is validated
and merged once. The rule that governs batches is in CLAUDE.md ("Running
tests"); the walkthrough is `docs/dev/merge-batches.md`.

    python ops/integrate.py status            # labelled PRs, CI, mergeability
    python ops/integrate.py build --dry-run   # what a build would do
    python ops/integrate.py build             # merge the batch locally
    python ops/integrate.py build --after-drop <closed batch PR>
                                              # again without its dropped culprit,
                                              # no suite ("After a drop")
    python ops/integrate.py suite             # its full suite on the remote machine
    python ops/integrate.py harness
    python ops/integrate.py shim-tests
    python ops/integrate.py open --suite <suite.json> --harness <harness.json> --shim-tests <shim.json>
                                              # push it, open its PR
    python ops/integrate.py merge             # merge the open batch PR, once green

A pull request joins by carrying the `ready-to-batch` label. `build` runs
in a clean checkout whose HEAD is origin/master, merges each labelled head
with `git merge --no-ff` in the order the label was applied (pull request
number when unknown) onto a new local branch, and drops a member that
conflicts, aborting that merge. It pushes nothing, so the local full suite
runs on the batch head before any CI, with the Python tests under rust/ the
suite does not collect (`RUST_HARNESS`). `open` then checks the suite passed
on that exact head with the real native client built from it (the launcher
builds it on an integrate/* branch; #1634), records both results, pushes the
branch, opens the batch pull request, and
comments on each dropped member naming what it collided with (removing its
label). `build` then regenerates the producer census
(`rust/producer-census.json`) on the batch head with the head's own
`rust/producer_census.py` and commits it as the batch's one generated
commit when it changed, so no member ever regenerates it; when the script
fails there, the members whose own diff changes the census inputs are
dropped like a conflict. When the batch changes anything under rust/,
`build` last runs CI's compile step there (`RUST_CHECK`), since neither the
suite nor the harness run compiles Rust; a failure drops nothing, keeps the
branch for bisecting and is recorded in the plan, and `open` refuses it.
Nothing resolves a conflict, no member is edited
and nothing force-pushes. `merge` merges the batch pull request `open` published,
with a merge commit, only while its head is the built head, every check on
it finished without a failure and master has not moved past it; every merge
is an admin bypass, so this is the check GitHub does not make. Needs git
2.38+ (`git merge-tree --write-tree`) and an authenticated `gh`.
"""

from __future__ import annotations

import argparse
import base64
import ast
import copy
import datetime as dt
import functools
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

LABEL = "ready-to-batch"
BASE = "master"
REMOTE = "origin"
PREFIX = "integrate/"
# A batch of one is allowed: a pull request's own CI runs only the fast lanes,
# so a lone ready one (an urgent fix, a revert, a release cut) gets its full
# CI as a batch of one instead of merging directly.
MIN_MEMBERS = 1
# A release cut (a member changing project.version in pyproject.toml) is
# batched alone: the release is tagged at the batch's merge, so whatever
# shares the batch ships in that release, while its notes are the fragments
# the cut folded; a change merged beside the cut would ship with its
# fragment unfolded, described under the next release. For the same reason
# a cut must fold every fragment on master (_release_cut_alone).
CHANGELOG_FRAGMENTS = "changelog.d"
VERSION_LINE_RE = re.compile(r'version\s*=\s*"([^"]*)"\s*$')
BATCH_RE = re.compile(r"^integrate/(\d{4}-\d{2}-\d{2})-batch-(\d+)$")
MARKER_RE = r"^(<<<<<<<|>>>>>>>)( |$)"
CENSUS = "rust/producer_census.py"
CENSUS_JSON = "rust/producer-census.json"
CI_BUDGET = "rust/ci/ci_budget.json"
CI_BUDGET_CODE = "rust/ci/ci_budget.py"
CI_SCOPE_CODE = "rust/ci/parity_scope.py"
CI_FINGERPRINTS = "rust/ci/ci_budget_fingerprints.json"
CI_ACCEPT = "rust/ci/ci_budget_accept"
# The subject of the batch's one generated commit (see _regenerate_census).
CENSUS_SUBJECT = "Regenerate producer census and CI metadata for this batch"
# The Python tests under rust/ that the local full suite never collects
# (pytest's testpaths is tests/) and that need no built Rust binary: the
# pytest steps of rust.yml's `rust` job plus the eval parity shard's
# `pytest rust/ci`. #877 broke rust/daemon/harness/test_w2e_lifecycle.py and
# passed its batch's local suite this way (2026-10-21). Census currency is
# not among them: `build` regenerates the census and `open` runs CI's own
# verdict, `producer_census.py --ci-check`. 767 tests, about 140 s natively
# on Windows (2026-10-21).
# Every pytest path of the `rust` job is here (a test pins it): hosted CI has
# no disposable-bank login, so a harness database case skips there and runs
# routinely only in this run. `--require-databases` makes such a case fail,
# naming the cause and the fix, when it finds no bench login or no Docker
# engine (rust/conftest.py, rust/daemon/harness/database_gate.py), so a
# skipped database case cannot pass unseen.
# Harness unit tests that other jobs' pytest steps run (the background
# pruning, W2-D controls and recall-llm wiring tests) are here too, so a
# harness change cannot reach a batch without them running locally; every
# path here is still one a CI pytest step names (a test pins that too).
RUST_HARNESS = (
    "rust/test_contract_inventory.py", "rust/test_producer_census.py",
    "rust/daemon/harness/test_w2e_lifecycle.py", "rust/daemon/harness/test_w2e_own_decisions.py",
    "rust/daemon/harness/board/test_norm.py", "rust/daemon/harness/board/test_run.py",
    "rust/daemon/harness/board/test_lease_recovery.py",
    "rust/daemon/harness/board/test_maintainer_rollback.py", "rust/daemon/harness/board/test_base_delta.py",
    "rust/daemon/harness/test_pgdisposable_slices.py", "rust/daemon/harness/test_w2e_judge_settle.py",
    "rust/daemon/harness/test_real_bank_timer_env.py",
    "rust/daemon/harness/test_w2e_real_bank_expects.py",
    "rust/daemon/harness/mirror/test_seed.py", "rust/daemon/harness/mirror/test_forced.py",
    "rust/daemon/harness/mirror/test_base_delta.py",
    "rust/daemon/harness/test_dbstate_real_bank.py",
    "rust/daemon/harness/mirror/test_seed_keys.py",
    "rust/daemon/harness/mirror/test_real_base_routes.py",
    "rust/daemon/harness/test_w2e_recall_llm.py",
    "rust/daemon/harness/test_background_harness.py", "rust/daemon/harness/test_w2d_controls.py",
    "rust/cli_harness/test_native_runtime_contract.py",
    "rust/daemon/harness/test_json_fidelity.py",
    "rust/daemon/harness/test_schema_clocks.py", "rust/daemon/harness/test_schema_generation.py",
    "rust/daemon/harness/test_schema_guards.py", "rust/daemon/harness/test_shadow_compare.py",
    "rust/daemon/harness/test_startup_ci.py", "rust/daemon/harness/test_startup_parts.py",
    "rust/daemon/harness/test_w2d_rerank_check.py",
    "rust/daemon/harness/test_w2d_trace_compare.py", "rust/cli_harness/test_update_row.py",
    "rust/cli_harness/test_cli_harness.py",
    "rust/cli_harness/test_console_browser.py",
    "rust/ci",
)
RUST_HARNESS_COMMAND = ("python -m pytest -q -p no:cacheprovider --require-databases "
                        + " ".join(RUST_HARNESS))
# Neither the full suite nor the harness run compiles Rust, so two members
# that each compile alone can still break the batch: #991 changed record()'s
# signature, #994 called it the old way, and batch #1017 failed hosted CI
# with E0061 (2026-10-28). `build` runs CI's own compile step (the `Rust`
# job's `cargo check --locked --all-targets`) from rust/, where rustup reads
# rust-toolchain.toml, whenever the batch changes anything under rust/,
# which holds every workspace Cargo file. 48 s cold, under 1 s warm, on the
# maintainer's Windows host (2026-10-28). Clippy and fmt stay CI's.
RUST_CHECK_DIR = "rust"
RUST_CHECK_COMMAND = "cargo check --workspace --all-targets --locked"
RUST_CHECK = tuple(RUST_CHECK_COMMAND.split())
# The check runs member build scripts and proc macros, so credential-shaped
# variables are withheld, as from the census script: by name, and any value
# carrying URL credentials (a database DSN with its password). The rest
# passes through (CARGO_TARGET_DIR, the toolchain's and the linker's
# variables), since an allowlist would break builds on hosts it was not
# written on; it still runs as the operator, as the full suite does.
# Where the check runs. The maintainer's host is not for heavy work (his
# rule, 16 November 2026), so a private config beside full-suite.remote
# sends it to a dev machine: `ssh=` (an ~/.ssh/config alias), `repo=` (a clone
# there) and `role=` (what the batch body calls the machine, never its
# name). `build` sends the batch as a git bundle over ssh, so nothing is
# pushed before the operator's publication check, and the far half
# (RUST_CHECK_REMOTE_SCRIPT) runs the check in one locked gate worktree
# there. Without the file the check runs here, as before; with it, only
# PSEUDOLIFE_RUST_CHECK_WHERE=local runs it here, and an unreachable machine
# is a check that did not run, never a fall back. The tool only reads the
# file (docs/dev/dev-machines.md).
RUST_CHECK_REMOTE = Path.home() / ".pseudolife-mcp" / "locks" / "rust-check.remote"
RUST_CHECK_WHERE_ENV = "PSEUDOLIFE_RUST_CHECK_WHERE"
RUST_CHECK_SSH = ("ssh",)
# Read once, when the tool starts on master's checkout: a member that edits
# the far half cannot change the check that judges its own batch.
# Missing only from a stripped copy of ops/: then only the remote check
# refuses, never `status`, `open` or `merge`.
try:
    RUST_CHECK_REMOTE_SCRIPT: str | None = (
        Path(__file__).resolve().with_name("rust-check-remote.ps1").read_text(encoding="utf-8"))
except OSError:
    RUST_CHECK_REMOTE_SCRIPT = None
try:
    RUST_HARNESS_REMOTE_SCRIPT: str | None = (
        Path(__file__).resolve().with_name("rust-harness-remote.ps1").read_text(encoding="utf-8"))
except OSError:
    RUST_HARNESS_REMOTE_SCRIPT = None
# A cold harness environment plus the run took about 18 minutes on the
# Windows dev VM (16 November 2026, 2876 tests); this only ends a hung one.
RUST_HARNESS_REMOTE_TIMEOUT = 5400
# Where `harness` writes its result, beside the suite's (ops/remote-suite.ps1).
SUITE_RESULTS = Path.home() / ".pseudolife-mcp" / "suite-results"
HARNESS_RESULT_KIND = "rust-harness"
PYTEST_SUMMARY_RE = re.compile(r"^=*\s*(\d+ (passed|failed|errors?|skipped)\b.* in [\d.]+s.*?)\s*=*$")
SHIM_TESTS_REMOTE = Path.home() / ".pseudolife-mcp" / "locks" / "shim-tests.remote"
SHIM_TESTS_ARGS = ("nextest", "run", "--locked", "-p", "pseudolife-stdio", "--lib", "--bins",
                   "-j", "4", "--no-fail-fast")
SHIM_TESTS_COMMAND = "cargo " + " ".join(SHIM_TESTS_ARGS)
try:
    SHIM_TESTS_REMOTE_SCRIPT = Path(__file__).with_name("shim-tests-remote.sh").read_text(encoding="utf-8")
    DATABASE_LOCK_SCRIPT = Path(__file__).with_name("dev-database-lock.ps1").read_text(encoding="utf-8")
except OSError:
    SHIM_TESTS_REMOTE_SCRIPT = DATABASE_LOCK_SCRIPT = None

# The command ssh runs there: read the script from stdin's first line and
# run it, leaving the rest of stdin (the bundle) to the script. Measured
# 16 November 2026: through Windows' ssh.exe an -EncodedCommand of 8 KB ran
# and one of 16 KB was refused as "not properly encoded", so the far half
# (17 KB encoded) cannot ride on the command line itself.
RUST_CHECK_BOOTSTRAP = base64.b64encode(
    ("$ProgressPreference = 'SilentlyContinue'; "
     "& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString("
     "[Convert]::FromBase64String([Console]::In.ReadLine()))))").encode("utf-16-le")
).decode("ascii")
# A cold check on the Windows dev VM is minutes, a warm one seconds; this
# only ends a hung connection, as the keepalives end a dropped one.
RUST_CHECK_REMOTE_TIMEOUT = 3600
# The role reaches the public batch body, so it is one of these, never a
# machine's name (CLAUDE.md "Dev machines": cite a machine by role).
RUST_CHECK_ROLES = ("Windows dev VM", "Linux dev box", "remote dev machine")
RUST_CHECK_DEFAULT_ROLE = "remote dev machine"
# A path PowerShell reads as one single-quoted literal: no quote of any kind
# (it takes the typographic ones as quotes too), no `$` or backtick.
RUST_CHECK_REPO_RE = re.compile(r"[A-Za-z0-9 _.:\\/~-]+")
SECRET_ENV_RE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|DSN|DATABASE_URL"
                           r"|(^|_)PAT$|(^|_)(API_)?KEY(_ID)?$", re.IGNORECASE)
URL_CREDENTIALS_RE = re.compile(r"://[^/@\s]+:[^/@\s]*@")


class Refusal(Exception):
    """A precondition failed; nothing was changed."""


def _run(argv: list[str], *, input: str | None = None, check: bool = True,
         **kwargs) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", input=input,
                              **kwargs)
    except FileNotFoundError:
        raise Refusal(f"{argv[0]} is not on PATH") from None
    if check and proc.returncode != 0:
        raise Refusal(f"`{' '.join(argv)}` failed ({proc.returncode}): "
                      f"{proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["git", *args], check=check)


def gh(*args: str, input: str | None = None) -> str:
    """Run gh and return its stdout (tests replace this function)."""
    return _run(["gh", *args], input=input).stdout


def _out(*args: str) -> str:
    return git(*args).stdout.strip()


@dataclass
class Member:
    number: int
    title: str
    sha: str
    url: str
    review: str
    checks: str
    labeled_at: str | None = None
    draft: bool = False
    checks_why: str = ""   # for `unverified`: the read that failed; for `failing`: the red checks
    report_only_red: tuple[str, ...] = ()   # red REPORT_ONLY_CHECK runs, shown, not counted
    snapshot_required: bool | None = None
    snapshot: str = "not required"
    snapshot_why: str = ""
    rust_jobs: str = ""
    labels: tuple[str, ...] = ()   # label names, for the Python freeze exception


@dataclass
class Dropped:
    member: Member
    reason: str
    comment: bool = True
    commented: bool = False
    unlabelled: bool = False


@dataclass
class Plan:
    branch: str
    base_sha: str
    label: str = LABEL
    head: str = ""
    pr: int = 0      # the batch pull request, once `open` published it
    merged: list[Member] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    census_commit: str = ""   # the generated census commit, when there is one
    census_drift: list[str] = field(default_factory=list)   # manual shapes it withdrew
    # Members whose head the batch already contained when `build` reached
    # them (a stacked child merged first, or a head already on master): no
    # merge commit of their own, so `open` and `merge` do not demand one.
    contained: list[int] = field(default_factory=list)
    # Pull requests whose own labelled change to the frozen package is in the
    # batch through another member's merge (a stacked child) although they
    # did not merge themselves: `open` and `merge` read their labels again.
    carried: list[int] = field(default_factory=list)
    # `passed` or `not needed`, which `open` accepts, or `failed: ...` or
    # `not run: ...`; empty for a plan built before the check existed.
    rust_check: str = ""
    # Where it ran: empty for this host with no remote configured (as before),
    # `local` when forced here, else the remote machine's role.
    rust_check_where: str = ""
    # Set by `build --after-drop` (docs/dev/merge-batches.md, "After a drop"):
    # the closed batch this one repeats without its culprit, and the file
    # lists that let it skip the suite and carry the closed batch's passes.
    after_drop: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        date, n = BATCH_RE.match(self.branch).groups()
        numbers = ", ".join(f"#{m.number}" for m in self.merged)
        return f"integrate: {date} batch {n} ({numbers})"

    @staticmethod
    def path(branch: str) -> Path:
        """Where `build` leaves the plan for `open`: inside this worktree's
        git directory, so it is never tracked or shared between worktrees."""
        name = branch.removeprefix(PREFIX) + ".json"
        return Path(_out("rev-parse", "--git-path", f"integrate-plans/{name}"))

    def save(self) -> None:
        path = Plan.path(self.branch)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        # Left out when empty, so the loader before `carried` existed
        # (Plan(**raw)) still reads the plan: a revert of the change that
        # added it, landed as a batch, runs that loader at `open`.
        if not data["carried"]:
            del data["carried"]
        if not data["rust_check_where"]:
            del data["rust_check_where"]
        if not data["after_drop"]:
            del data["after_drop"]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def load(branch: str) -> Plan | None:
        path = Plan.path(branch)
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        # A field this loader does not know (one a newer or reverted tool
        # wrote) is dropped, so a plan crosses tool versions; a missing one
        # takes its default.
        known = {f.name for f in fields(Plan)}
        raw = {k: v for k, v in raw.items() if k in known}
        raw["merged"] = [Member(**m) for m in raw["merged"]]
        for d in raw["dropped"]:
            d.pop("waiting", None)   # census waiters, from plans built before 2026-10-27
        raw["dropped"] = [Dropped(Member(**d.pop("member")), **d) for d in raw["dropped"]]
        return Plan(**raw)


# The ruleset's required checks (docs/specs/2026-10-19-merge-queue.md, read
# from ruleset 19138403): `merge` waits until each has reported, so a head
# where only the fast `scope` or `guards` finished never passes.
REQUIRED_CHECKS = ("test", "Parity / ubuntu-latest", "Parity / windows-latest")
# The only conclusions `merge` counts as passing; anything else is red or
# still running.
PASSING = {"SUCCESS", "SKIPPED", "NEUTRAL"}
# Members confirm on GitHub's own schedule after the merge: ask for up to
# MEMBER_WAIT_SECONDS before accepting a head that is on master unmarked.
MEMBER_WAIT_SECONDS = 120
MEMBER_POLL_SECONDS = 10
_sleep = time.sleep
_clock = time.monotonic

# Conclusions that are a known failure on a member's own head. CANCELLED is
# not one: a run cancelled by hand (maintainer, 2026-10-29: cancel CI that is
# not part of a batch) or by a re-trigger gave no verdict, and the batch's CI
# is its members' CI. `merge` still counts a cancelled batch check as red
# (PASSING, above).
FAILED = {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
CANCELLED = "CANCELLED"
# (workflow name, check-name prefix) of the one report-only check on a
# member's head: rust.yml's frozen-probes job, "Frozen judge probes (report
# only) / <os>", which runs only on ordinary pull requests, gates nothing
# and is shown but never counted, because a lost probe runner (issue #1519)
# says nothing about the change. A test pins that only that job produces
# the prefix. `merge` does not use it: the probe never runs in a batch.
REPORT_ONLY_CHECK = ("Rust shim", "Frozen judge probes (report only) / ")
# GitHub reports a job that ran past its timeout-minutes as CANCELLED, not
# TIMED_OUT, and the runner annotates it with this message (jobs
# 116301727515 and 116323387861, 2026-10-15: "The job has exceeded the
# maximum execution time of 25m0s"; runners in 2023-24 wrote "The job
# running on runner <name> has exceeded ..."). Such a head likely hangs, so
# it stays a known failure.
TIMEOUT_ANNOTATION = re.compile(
    r"The job (running on runner .+ )?has exceeded the maximum execution time")
_ACTIONS_JOB = re.compile(r"/actions/runs/\d+/job/(\d+)(?:[?#].*)?$")
SNAPSHOT_WORKFLOW = ".github/workflows/rust.yml"
SNAPSHOT_JOBS = ("Rust / ubuntu-latest", "Rust / windows-latest")
SNAPSHOT_STEP = "Replay touched PR goldens"
SNAPSHOT_SELECTOR = "rust/ci/pr_precheck.py"

# Execute only the frozen base's selector code in an isolated interpreter;
# the checkout's Python files are inputs read as text, never imported.
_SNAPSHOT_INPUTS = """import json, pathlib, sys, types
data = json.load(sys.stdin)
root = pathlib.Path(data['root'])
scope = types.ModuleType('parity_scope')
scope.__file__ = str(root / 'rust/ci/parity_scope.py')
sys.modules['parity_scope'] = scope
exec(compile(data['scope'], scope.__file__, 'exec'), scope.__dict__)
selector = types.ModuleType('snapshot_selector')
selector.__file__ = str(root / 'rust/ci/pr_precheck.py')
exec(compile(data['selector'], selector.__file__, 'exec'), selector.__dict__)
files = data['files']
docs_only = any(scope.is_docs(path) for path in files) and all(
    scope.is_docs(path) or path == 'rust/producer-census.json' for path in files)
print(json.dumps(not docs_only and any(selector.rows_for(path, {}) for path in files)))
"""


def _has_snapshot_selector(base: str) -> bool:
    # A pre-feature base has no checkpoint to require. A failed Git read
    # raises instead of being mistaken for an absent selector.
    return bool(_out("ls-tree", base, "--", SNAPSHOT_SELECTOR))


@functools.lru_cache(maxsize=None)
def _base_has_snapshot_admission(base: str) -> bool:
    """Read the frozen builder's Member schema without executing its code."""
    try:
        tree = ast.parse(_out("show", f"{base}:ops/integrate.py"))
    except (Refusal, SyntaxError) as exc:
        raise Refusal(_unread("the base builder's admission schema", exc)
                      + "; fix that read and retry open") from None
    return any(isinstance(node, ast.ClassDef) and node.name == "Member" and any(
        isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
        and field.target.id == "snapshot_required" for field in node.body)
        for node in tree.body)


def _snapshot_requirement(member: Member, base: str) -> tuple[bool | None, str]:
    try:
        if not _has_snapshot_selector(base):
            return False, ""
        if _fetch_head(member.number) != member.sha:
            return None, "the PR head moved while snapshot admission was read; retry build"
        merged = git("merge-tree", "--write-tree", "--name-only", "--no-messages",
                     base, member.sha, check=False)
        if merged.returncode == 1:
            # Keep conflicts on the existing merge/drop path, rather than
            # waiting for CI that GitHub cannot run on a conflicting PR.
            # Budget-only conflicts retain normalization and batch CI.
            return False, "snapshot admission deferred to conflict handling (budget normalization or drop)"
        if merged.returncode != 0 or not merged.stdout.strip():
            raise ValueError("the PR merge tree could not be read")
        tree = merged.stdout.splitlines()[0]
        # CI diffs its synthetic PR merge against the base parent. A
        # three-dot diff would retain runtime edits already landed on base.
        files = _out("diff", "--name-only", "--no-renames", base, tree).splitlines()
        payload = {"root": _out("rev-parse", "--show-toplevel"),
                   "selector": _out("show", f"{base}:{SNAPSHOT_SELECTOR}"),
                   "scope": _out("show", f"{base}:{CI_SCOPE_CODE}"),
                   "files": sorted(files)}
        result = _run([sys.executable, "-I", "-c", _SNAPSHOT_INPUTS],
                      input=json.dumps(payload), check=False)
        if result.returncode:
            return None, "the frozen snapshot input selector failed; retry build after fixing its inputs"
        required = json.loads(result.stdout)
        if type(required) is not bool:
            raise ValueError("snapshot input selector returned no boolean")
        return required, ""
    except (Refusal, OSError, ValueError) as exc:
        return None, _unread("snapshot inputs", exc) + "; retry build"


def _snapshot_state(sha: str, rollup: list[dict] | None,
                    required: bool | None) -> tuple[str, str]:
    """Read the exact Actions jobs and step, never their output text."""
    if required is False:
        return "not required", ""
    if required is None:
        return "unverified", "snapshot inputs have not been verified; rebuild the batch"
    eligible, runs = [], {}
    try:
        for check in rollup or []:
            name = check.get("name")
            if name not in SNAPSHOT_JOBS or "context" in check:
                continue
            match = _ACTIONS_JOB.search(check.get("detailsUrl") or "")
            if not match:
                continue
            meta = json.loads(gh("api", f"repos/{{owner}}/{{repo}}/actions/jobs/{match.group(1)}"))
            run_id = int(meta["run_id"])
            if run_id not in runs:
                runs[run_id] = json.loads(gh("api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}"))
            run = runs[run_id]
            if (meta.get("head_sha") != sha or run.get("head_sha") != sha
                    or meta.get("name") != name or run.get("event") != "pull_request"
                    or str(run.get("path", "")).split("@", 1)[0] != SNAPSHOT_WORKFLOW
                    or int(meta["run_attempt"]) != int(run["run_attempt"])):
                continue
            eligible.append({"name": name, "workflowName": SNAPSHOT_WORKFLOW,
                             "status": meta["status"], "conclusion": meta.get("conclusion"),
                             "startedAt": meta.get("started_at"), "completedAt": meta.get("completed_at"),
                             "snapshot_meta": meta})
        latest = {check["name"]: check["snapshot_meta"] for check in _latest_checks(eligible)}
        waiting = []
        for name in SNAPSHOT_JOBS:
            meta = latest.get(name)
            steps = meta.get("steps", []) if meta else []
            matches = [step for step in steps if step.get("name") == SNAPSHOT_STEP]
            if len(matches) != 1:
                if meta and _check_state(meta) == CANCELLED:
                    waiting.append(f"{name} was cancelled before replay; push a new commit or rerun the Rust workflow")
                elif meta and meta.get("status") == "completed":
                    waiting.append(f"{name}: exact replay step absent or ambiguous; push a head containing "
                                   "the step (merge master forward if needed), or rerun the Rust workflow")
                else:
                    waiting.append(name)
                continue
            step = matches[0]
            conclusion = str(step.get("conclusion") or "").upper()
            if conclusion in FAILED:
                return "failing", f"{name}: {SNAPSHOT_STEP} is red on the PR head"
            if step.get("status") != "completed" or conclusion not in PASSING:
                if conclusion == CANCELLED or _check_state(meta) == CANCELLED:
                    waiting.append(f"{name} was cancelled before replay finished; push a new commit or rerun the Rust workflow")
                else:
                    waiting.append(name)
                continue
            if conclusion == "SKIPPED":
                prior = [s for s in steps if int(s["number"]) < int(step["number"])]
                if any(_check_state(s) == CANCELLED for s in prior):
                    waiting.append(f"{name} was cancelled before replay; push a new commit or rerun the Rust workflow")
                elif any(_check_state(s) in FAILED for s in prior):
                    # continue-on-error prerequisites can fail legitimately
                    # when this OS has no goldens. The job's verdict decides.
                    if _check_state(meta) in FAILED:
                        return "failing", f"{name}: snapshot replay skipped in a failed job"
                    if meta.get("status") != "completed" or _check_state(meta) not in PASSING:
                        waiting.append(name)
        if waiting:
            return "pending", "own-head snapshot replay is pending: " + ", ".join(waiting)
        return "satisfied", ""
    except (Refusal, KeyError, TypeError, ValueError) as exc:
        return "unverified", _unread("snapshot job metadata", exc) + "; retry build/open"


def _check_state(check: dict) -> str:
    """A CheckRun's conclusion or a StatusContext's state, upper-cased."""
    return (check.get("conclusion") or check.get("state") or "").upper()


def _checks(rollup: list[dict] | None) -> str:
    """One word for a head's CI: green, pending, cancelled, unverified,
    failing or none (`_checks_why`)."""
    return _checks_why(rollup)[0]


def _report_only(check: dict) -> bool:
    """Whether a rollup entry is the report-only check: an Actions check run
    of exactly REPORT_ONLY_CHECK's workflow whose name starts with its prefix."""
    workflow, prefix = REPORT_ONLY_CHECK
    return (check.get("__typename") != "StatusContext" and "context" not in check
            and check.get("workflowName") == workflow
            and str(check.get("name") or "").startswith(prefix))


def _report_only_red(rollup: list[dict] | None) -> tuple[str, ...]:
    """The report-only checks whose latest run did not pass, by name."""
    return tuple(sorted({_check_name(check) for check in _latest_checks(rollup)
                         if _report_only(check) and not _unfinished(check)
                         and _check_state(check) not in PASSING}))


def _checks_why(rollup: list[dict] | None) -> tuple[str, str]:
    """(one word for a head's CI, why) over every check but the report-only
    one (REPORT_ONLY_CHECK, which `_report_only_red` lists separately); for
    `failing` the why names the red checks."""
    counted = [check for check in rollup or [] if not _report_only(check)]
    word, why = _counted_checks_why(counted if rollup else rollup)
    if word == "failing" and not why:
        red = sorted({_check_name(check) for check in _standing_failures(counted)})
        why = ", ".join(red)
    return word, why


def _counted_checks_why(rollup: list[dict] | None) -> tuple[str, str]:
    """(one word for a head's CI, why): green, pending, cancelled,
    unverified, failing or none; `failing` and `unverified` keep a member
    out of a batch, and for `unverified` the why names the read that failed.

    A cancelled Actions job that is the latest attempt of its check is read
    for the runner's timeout annotation: a timeout is `failing`, and
    annotations that cannot be read leave the head `unverified` (retry).
    A FAILURE of a gate job counts as cancelled when it failed at its
    cancelled-need step only because jobs it needs were cancelled
    (`_gate_cancelled`); any other failure is `failing` before any read.

    A failure is counted unless a run of the same check finished after it
    started (`_standing_failures`): a head carries one run per event for
    workflows such as Python freeze, so a failure made before a
    `freeze-exception` label is superseded by the labelled event's run,
    while a newer failure still outweighs an older success, and an older
    run that is still going hides nothing."""
    if not rollup:
        return "none", ""
    failures = _standing_failures(rollup)
    if any(_check_state(check) != "FAILURE"
           or not _ACTIONS_JOB.search(check.get("detailsUrl") or "")
           for check in failures):
        return "failing", ""                # no gate's failure: nothing to read
    local: dict = {}
    if failures:
        local = _local_workflows()
        gates = _local_gates(local)
        if not all(_gate_named(check, gates) for check in failures):
            return "failing", ""            # the checkout defines no such gate
    latest = {id(check) for check in _latest_checks(rollup)}
    pending = cancelled = False
    unread = ""                             # the first read that failed
    runs: dict = {}
    probes: dict = {}
    for check in failures:
        verdict, why = _gate_cancelled(check, runs, probes, local)
        if verdict is False:
            return "failing", ""
        cancelled = True
        unread = unread or (why if verdict is None else "")
    for check in rollup:
        if _check_state(check) == CANCELLED:
            if id(check) not in latest:
                continue                    # an attempt a rerun replaced
            job = _ACTIONS_JOB.search(check.get("detailsUrl") or "")
            if not job:
                cancelled = True            # no Actions job: no annotations
                continue
            timed_out, why = _probe_timeout(job.group(1), probes)
            if timed_out:
                return "failing", ""
            cancelled = True
            unread = unread or (why if timed_out is None else "")
    pending = any(_unfinished(check) for check in rollup
                  if _check_state(check) != CANCELLED)
    return (("unverified", unread) if unread else ("pending", "") if pending
            else ("cancelled", "") if cancelled else ("green", ""))


def _unread(what: str, exc: BaseException) -> str:
    """`<what> could not be read (<cause>)`, with gh's first line as the cause."""
    if isinstance(exc, Refusal):
        cause = (str(exc).strip().splitlines() or ["no answer"])[0]
    else:
        cause = f"unexpected reply: {type(exc).__name__}: {exc}"
    return f"{what} could not be read ({cause})"


def _probe_timeout(job_id: str, probes: dict) -> tuple[bool | None, str]:
    """Whether a cancelled Actions job ran past its timeout-minutes, from the
    runner's annotation read by the job's id (its check-run id); None (with
    why) when the annotations cannot be read. `probes` caches the answer by
    job id within one `_checks_why`."""
    if job_id in probes:
        return probes[job_id]
    try:
        # Every page (a job's own annotations can come first), one JSON
        # string per message so a multi-line message is matched whole.
        out = gh("api", f"repos/{{owner}}/{{repo}}/check-runs/{job_id}/annotations",
                 "--paginate", "--jq", ".[].message | @json")
        messages = [json.loads(line) for line in out.splitlines() if line.strip()]
        answer: tuple[bool | None, str] = (
            any(isinstance(m, str) and TIMEOUT_ANNOTATION.match(m) for m in messages), "")
    except (Refusal, ValueError) as exc:
        answer = (None, _unread(f"the annotations of job {job_id}", exc))
    probes[job_id] = answer
    return answer


# A gate job runs under `always()` and fails unless the jobs it needs passed,
# so a cancel that stopped those jobs still leaves it FAILURE: rust.yml's
# `Parity / <os>` on #1059's head 3dda95a1 (run 40992819326, job
# 123859514294, 2026-10-30) failed only because `Rust / <os>` was cancelled.
# Its check run carries no cause (annotation "Process completed with exit
# code 1"), so the cause is read from the workflow, the job's steps and the
# run's jobs. Without `always()` a job is skipped once its run is cancelled,
# so it cannot fail this way and its FAILURE stays its own.
_ALWAYS = re.compile(r"\balways\(\s*\)")
_EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.S)
# The gate step that fails exactly when a job it needs was cancelled (and
# skips the verdict step then), so a failure there has no other cause; a
# gate without it fails at its verdict, which a broken verdict script would
# do as well (Codex's repro on #1068: a `throw` there beside a cancelled
# need), so it is never downgraded. Only these keys, which change nothing
# the step does beyond its condition and command.
CANCEL_STEP_IF = "${{ contains(needs.*.result, 'cancelled') }}"
CANCEL_STEP_RUN = "exit 1"
CANCEL_STEP_KEYS = {"name", "id", "if", "run"}
# Gate job keys that change how a step starts (its directory, shell,
# environment or machine) before its script runs, so the cancelled-need
# step could fail on them: a gate carrying any is never downgraded. Its
# other keys, and the workflow's `defaults` and `env`, must equal the
# checkout's copy, which the gate already runs under.
GATE_STEP_SETTINGS = {"defaults", "env", "container", "services"}
WORKFLOW_STEP_SETTINGS = ("defaults", "env")
# The runner's own first step: workflow step i (from 0) is API step i + 2.
SETUP_STEP = "Set up job"
# A job conclusion that is no failure on the head: a skip the gate admits or
# rejects by design, or a cancel (whose timeout is read separately).
_NO_VERDICT = {"success", "skipped", "neutral", "cancelled"}


def _yaml():
    try:
        import yaml   # PyYAML, a project dependency; only a failed check needs it
    except ImportError:
        raise Refusal("telling a gate failed by cancelled jobs from a real failure "
                      "reads the workflow with PyYAML, a project dependency this "
                      "Python lacks; nothing was changed: run `python -m pip install "
                      "pyyaml` (or use the project's environment) and try again") from None
    return yaml


def _template(name: str) -> re.Pattern:
    """A `name:` template as the names GitHub gives its runs: every `${{ }}`
    expression matches any text (a skipped matrix job keeps it literally)."""
    return re.compile(".+".join(re.escape(part) for part in _EXPRESSION.split(name)))


def _job_pattern(job_id: str, job: dict) -> re.Pattern:
    """What a workflow job's runs are named: its `name` template, or its id,
    which a matrix leg suffixes with its values in parentheses. A named
    matrix job whose name holds no expression may get that suffix too; no
    run here confirms it, so such a job matches none of its runs and its
    gate is never downgraded (it stays failing)."""
    name = job.get("name")
    if not isinstance(name, str):
        return re.compile(re.escape(job_id) + r"( \(.+\))?")
    return _template(name)


def _workflow(text: str, where: str) -> dict:
    """A workflow file parsed, its jobs keyed by id as text."""
    yaml = _yaml()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{where} is no YAML: {exc}") from exc
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        raise ValueError(f"{where} defines no jobs")
    return dict(data, jobs={str(k): v for k, v in jobs.items() if isinstance(v, dict)})


def _needs(job: dict) -> set[str]:
    needs = job.get("needs") or []
    return {needs} if isinstance(needs, str) else {str(n) for n in needs}


def _is_gate(job: dict) -> bool:
    return bool(_needs(job)) and bool(_ALWAYS.search(str(job.get("if", ""))))


def _local_workflows() -> dict[str, dict]:
    """This checkout's workflows by workflow name. `build` reads them on
    origin/master and `open` on the batch branch, whose members can add or
    change a gate: that only widens the gate filter, and a gate whose
    settings differ from the batch's copy is still refused."""
    _yaml()
    folder = Path(_out("rev-parse", "--show-toplevel")) / ".github" / "workflows"
    workflows = {}
    for path in sorted(folder.glob("*.y*ml")):
        try:
            data = _workflow(path.read_text(encoding="utf-8"), path.name)
        except (OSError, ValueError):
            continue                        # unreadable here: no gate seen
        workflows.setdefault(str(data.get("name") or f".github/workflows/{path.name}"), data)
    return workflows


def _local_gates(workflows: dict[str, dict]) -> list[tuple[str, re.Pattern]]:
    """(workflow name, run-name pattern) of every gate job in the checkout's
    workflows, the filter that keeps a plain failure from any API read. A
    gate only the member's head defines is not seen, so its failure stays
    failing."""
    return [(name, _job_pattern(job_id, job))
            for name, data in workflows.items()
            for job_id, job in data["jobs"].items() if _is_gate(job)]


def _gate_named(check: dict, gates: list[tuple[str, re.Pattern]]) -> bool:
    return any(workflow == check.get("workflowName") and pattern.fullmatch(_check_name(check))
               for workflow, pattern in gates)


def _is_cancel_step(step: dict) -> bool:
    """Named, with exactly `if: CANCEL_STEP_IF` and `run: exit 1`, and no key
    but CANCEL_STEP_KEYS."""
    return (set(step) <= CANCEL_STEP_KEYS and isinstance(step.get("name"), str)
            and isinstance(step.get("if"), str) and step["if"].strip() == CANCEL_STEP_IF
            and isinstance(step.get("run"), str) and step["run"].strip() == CANCEL_STEP_RUN)


def _gate_steps(head: dict, gate_id: str, local: dict | None) -> tuple[list[str], int] | None:
    """(the gate's step names, its cancelled-need step's index) when the
    gate at the head can only fail that step on a cancel: no key in
    GATE_STEP_SETTINGS, its other keys but `steps` and the workflow's
    `defaults` and `env` equal to `local` (the checkout's copy), every step a
    `run` step (no action, so no pre or post step) with a unique literal
    name, and exactly one cancelled-need step. None otherwise."""
    gate = head["jobs"][gate_id]
    mine = (local or {}).get("jobs", {}).get(gate_id)
    if not isinstance(mine, dict) or GATE_STEP_SETTINGS & set(gate):
        return None
    if ({k: v for k, v in gate.items() if k != "steps"}
            != {k: v for k, v in mine.items() if k != "steps"}):
        return None
    if any(head.get(key) != local.get(key) for key in WORKFLOW_STEP_SETTINGS):
        return None
    steps = gate.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        return None
    names = [s.get("name") for s in steps]
    if (any(not isinstance(n, str) or "${{" in n for n in names)
            or len(set(names)) != len(names)
            or any("uses" in s or not isinstance(s.get("run"), str) for s in steps)):
        return None
    cancels = [i for i, s in enumerate(steps) if _is_cancel_step(s)]
    return (names, cancels[0]) if len(cancels) == 1 else None


def _failed_at_cancel_step(steps: list, names: list[str], index: int) -> bool:
    """Whether a gate job's API steps are the workflow's, in place, and the
    one that failed is its cancelled-need step: API step 1 is the runner's
    SETUP_STEP, workflow step i is API step i + 2 under its own name, and
    every other step succeeded or skipped."""
    ran = {int(step["number"]): (str(step["name"]), step.get("conclusion")) for step in steps}
    if ran.get(1, ("",))[0] != SETUP_STEP:
        return False
    if any(ran.get(i + 2, ("",))[0] != name for i, name in enumerate(names)):
        return False
    failed = [number for number, (_, c) in ran.items() if c not in ("success", "skipped")]
    return failed == [index + 2] and ran[index + 2][1] == "failure"


def _gate_cancelled(check: dict, runs: dict, probes: dict,
                    local: dict[str, dict]) -> tuple[bool | None, str]:
    """Whether a FAILURE check is a gate that failed only because jobs it
    needs were cancelled, with why when that cannot be read. All must hold:

    - its job in the workflow file at the run's head commit runs under
      `always()`, has `needs` and can fail its cancelled-need step on
      nothing but a cancel (`_gate_steps`, against the checkout's copy in
      `local`). GitHub runs a pull request's workflow from its merge ref,
      which the API does not name, so every job the run lists must match a
      job of that file, or the downgrade is refused;
    - that step is, by position and name, the only one that failed, on this
      leg and on every other failed leg of the gate in the run attempt,
      whether the rollup lists that leg or not (`_failed_at_cancel_step`),
      or the leg ran no step at all in an attempt that itself concluded
      `cancelled` (cancelled while starting, so it gave no verdict);
    - no other job in the run attempt failed, timed out or needs action
      (the report-only check aside, REPORT_ONLY_CHECK), and none the gate
      needs is unfinished;
    - a job it needs, and nothing outside its needs, was cancelled, and no
      such job ran past its timeout.

    False when that is not proven; None when GitHub's record cannot be read.
    `runs` caches each run attempt's and job's read, `probes` the timeout
    reads."""
    job = _ACTIONS_JOB.search(check.get("detailsUrl") or "")
    if not job:
        return False, ""

    def job_meta(job_id: str) -> dict:
        if ("job", job_id) not in runs:
            runs[("job", job_id)] = json.loads(
                gh("api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}"))
        return runs[("job", job_id)]

    what = f"job {job.group(1)}"
    try:
        meta = job_meta(job.group(1))
        key = (int(meta["run_id"]), int(meta["run_attempt"]))
        if key not in runs:
            what = f"run {key[0]}"
            run = json.loads(gh("api", f"repos/{{owner}}/{{repo}}/actions/runs/{key[0]}"))
            path, head = str(run["path"]).split("@", 1)[0], str(meta["head_sha"])
            what = f"{path} at {head[:12]}"
            data = _workflow(gh("api", "-H", "Accept: application/vnd.github.raw+json",
                                f"repos/{{owner}}/{{repo}}/contents/{path}?ref={head}"), what)
            what = f"the jobs of run {key[0]} attempt {key[1]}"
            out = gh("api", "--paginate",
                     f"repos/{{owner}}/{{repo}}/actions/runs/{key[0]}/attempts/{key[1]}/jobs"
                     # 100 a page made GitHub answer 502 on every try for a
                     # 140-job attempt (run 40995606796, 2026-10-30); 50 worked.
                     "?per_page=50", "--jq", ".jobs[] | @json")
            # The run's record describes its latest attempt only.
            ended = run.get("conclusion") if int(run.get("run_attempt") or 0) == key[1] else None
            runs[key] = (data, [json.loads(line) for line in out.splitlines() if line.strip()],
                         ended)
        data, listed, ended = runs[key]
        defs = data["jobs"]
        what = f"the jobs of run {key[0]} attempt {key[1]}"
        patterns = {job_id: _job_pattern(job_id, d) for job_id, d in defs.items()}

        def owners(name: str) -> set[str]:
            return {job_id for job_id, p in patterns.items() if p.fullmatch(name)}

        gate = owners(str(meta["name"]))
        if len(gate) != 1:
            return False, ""                # not one job of this workflow
        gate_id = gate.pop()
        definition = defs[gate_id]
        needs = _needs(definition)
        shape = _gate_steps(data, gate_id, local.get(str(check.get("workflowName"))))
        if not _is_gate(definition) or shape is None:
            return False, ""                # no gate, or none that names a cancel
        names, index = shape
        if any(not owners(str(other["name"])) for other in listed):
            return False, ""                # the run is not this file's
        legs = [str(meta["id"])] + [
            str(other["id"]) for other in listed
            if owners(str(other["name"])) == {gate_id} and other.get("id") != meta.get("id")
            and other.get("conclusion") is not None
            and other.get("conclusion") not in _NO_VERDICT]
        stepless = []
        for leg in legs:
            what = f"job {leg}"
            leg_meta = job_meta(leg)
            steps = leg_meta.get("steps")
            if steps == [] and ended == "cancelled" and leg_meta.get("conclusion") == "failure":
                # Cancelling the attempt while this leg was starting failed it
                # before any step ran (#1074's 7cb16da6), so it gave no
                # verdict; the needed jobs below still decide.
                stepless.append(leg)
                continue
            if not isinstance(steps, list) or not steps:
                return None, f"the steps of job {leg} could not be read (none listed)"
            what = f"the steps of job {leg}"
            if not _failed_at_cancel_step(steps, names, index):
                return False, ""            # this leg failed for another reason
        what = f"the jobs of run {key[0]} attempt {key[1]}"
        cancelled = []
        for other in listed:
            named = owners(str(other["name"]))
            if other.get("id") == meta.get("id") or named == {gate_id}:
                continue                    # a leg of this gate, read above
            if _report_only({"workflowName": check.get("workflowName"),
                             "name": other.get("name")}):
                continue                    # shown, never counted (REPORT_ONLY_CHECK)
            conclusion = other.get("conclusion")
            if conclusion is not None and conclusion not in _NO_VERDICT:
                return False, ""            # a job of the run failed
            if not named & needs:
                continue
            if conclusion is None:
                return False, ""            # a needed job never finished
            if conclusion == "cancelled" and named <= needs:
                cancelled.append(str(other["id"]))
    except (Refusal, ValueError, KeyError, TypeError, AttributeError) as exc:
        return None, _unread(what, exc)
    if not cancelled:
        if stepless:
            # The cancel reached only the gate: nothing failed, but no
            # verdict was given either, so a rerun of its CI decides.
            return None, (f"gate job {stepless[0]} was cancelled before it ran a step "
                          "and no job it needs was cancelled (rerun its CI)")
        return False, ""                    # no needed job was cancelled
    verdict: tuple[bool | None, str] = (True, "")
    for job_id in cancelled:
        timed_out, why = _probe_timeout(job_id, probes)
        if timed_out:
            return False, ""
        if timed_out is None and verdict[0]:
            verdict = (None, why)
    return verdict


def _unfinished(check: dict) -> bool:
    return (check.get("status") not in (None, "COMPLETED")
            or _check_state(check) in ("", "PENDING", "EXPECTED"))


def _check_name(check: dict) -> str:
    return check.get("name") or check.get("context") or "an unnamed check"


def _labeled_at(number: int, label: str) -> str | None:
    """When the label was last applied (ISO 8601 sorts as text), if known."""
    jq = (f'.[] | select(.event == "labeled" and .label.name == "{label}")'
          " | .created_at")
    try:
        out = gh("api", "--paginate", f"repos/{{owner}}/{{repo}}/issues/{number}/events",
                 "--jq", jq)
    except Refusal:
        return None
    times = [line.strip() for line in out.splitlines() if line.strip()]
    return max(times) if times else None


def ready(label: str, base_sha: str | None = None) -> list[Member]:
    """Open labelled pull requests on master, in label-applied order."""
    fields = "number,title,headRefOid,url,isDraft,reviewDecision,statusCheckRollup,labels"
    rows = json.loads(gh("pr", "list", "--state", "open", "--label", label,
                         "--base", BASE, "--limit", "100", "--json", fields) or "[]")
    members = []
    for r in rows:
        checks, why = _checks_why(r.get("statusCheckRollup"))
        members.append(Member(number=r["number"], title=r["title"], sha=r["headRefOid"],
                              url=r["url"], review=r.get("reviewDecision") or "NONE",
                              checks=checks, draft=bool(r.get("isDraft")), checks_why=why,
                              report_only_red=_report_only_red(r.get("statusCheckRollup")),
                              labels=tuple(label.get("name", "") for label in r.get("labels") or []
                                           if isinstance(label, dict))))
    base_sha = base_sha or _out("rev-parse", f"{REMOTE}/{BASE}")
    for m, row in zip(members, rows):
        jobs = {c.get("name"): c for c in _latest_checks(row.get("statusCheckRollup") or [])}
        m.rust_jobs = "; ".join(f"{name}: {_check_state(jobs[name]).lower() or str(jobs[name].get('status', 'pending')).lower()}"
                              if name in jobs else f"{name}: absent"
                              for name in ("Rust / ubuntu-latest", "Rust / windows-latest"))
        if not m.draft and m.checks not in ("failing", "unverified"):
            m.snapshot_required, why = _snapshot_requirement(m, base_sha)
            m.snapshot, m.snapshot_why = _snapshot_state(
                m.sha, row.get("statusCheckRollup"), m.snapshot_required)
            m.snapshot_why = why or m.snapshot_why
        m.labeled_at = _labeled_at(m.number, label)
    # Known label times first, oldest first; the rest by number.
    return sorted(members, key=lambda m: (m.labeled_at is None, m.labeled_at or "",
                                          m.number))


def _fetch_head(number: int) -> str:
    git("fetch", "-q", REMOTE, f"pull/{number}/head")
    return _out("rev-parse", "FETCH_HEAD")


def _merge_tree(ours: str, theirs: str) -> tuple[bool, list[str]]:
    """Would `theirs` merge into `ours` cleanly? Touches no working tree."""
    proc = git("merge-tree", "--write-tree", "--name-only", "--no-messages",
               ours, theirs, check=False)
    if proc.returncode not in (0, 1):
        raise Refusal("`git merge-tree --write-tree` failed (needs git 2.38+): "
                      + proc.stderr.strip())
    lines = proc.stdout.splitlines()[1:]
    return proc.returncode == 0, [line for line in lines if line]


def _changed(sha: str, base: str) -> set[str]:
    # No rename detection: a rename lists both its old and its new path.
    return set(_out("diff", "--name-only", "--no-renames", f"{base}...{sha}").splitlines())


def _open_batches() -> list[tuple[int, str, str, str]]:
    """(number, branch, url, head sha) of every open pull request from an
    `integrate/` branch of this repository, however named. A fork's branch
    can carry any name, so fork pull requests are not batches."""
    rows = json.loads(gh("pr", "list", "--state", "open", "--limit", "500", "--json",
                         "number,headRefName,url,headRefOid,isCrossRepository")
                      or "[]")
    return [(r["number"], r["headRefName"], r.get("url", ""), r.get("headRefOid", ""))
            for r in rows
            if r.get("headRefName", "").startswith(PREFIX)
            and not r.get("isCrossRepository", False)]


def _used_numbers(date: str) -> set[int]:
    """Batch numbers taken on `date`: by earlier batch pull requests (whose
    branches GitHub deletes on merge), remote branches and local branches."""
    rows = json.loads(gh("pr", "list", "--state", "all", "--limit", "300",
                         "--search", "integrate in:title",
                         "--json", "number,headRefName") or "[]")
    names = [r.get("headRefName", "") for r in rows]
    remote = _out("ls-remote", "--heads", REMOTE, f"{PREFIX}*")
    names += [line.split("refs/heads/", 1)[1]
              for line in remote.splitlines() if "refs/heads/" in line]
    names += _out("for-each-ref", "--format=%(refname:short)",
                  f"refs/heads/{PREFIX}").splitlines()
    used = set()
    for name in names:
        match = BATCH_RE.match(name)
        if match and match.group(1) == date:
            used.add(int(match.group(2)))
    return used


def _preflight() -> tuple[str, str]:
    """Refuse unless the checkout is clean and at origin/master.

    Returns (what to switch back to, origin/master's sha)."""
    if _out("status", "--porcelain", "--untracked-files=no"):
        raise Refusal("the checkout has uncommitted changes to tracked files; "
                      "commit or discard them, or run from a fresh worktree")
    git("fetch", "-q", REMOTE, BASE)
    base_sha = _out("rev-parse", f"{REMOTE}/{BASE}")
    if _out("rev-parse", "HEAD") != base_sha:
        raise Refusal(f"HEAD is not {REMOTE}/{BASE} ({base_sha[:12]}); run "
                      f"`git switch --detach {REMOTE}/{BASE}` (or use a fresh "
                      "worktree) and try again")
    branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    return branch or base_sha, base_sha


def _collision(member: Member, conflicted: list[str], merged: list[Member],
               base_sha: str) -> str:
    clean, files = _merge_tree(base_sha, member.sha)
    if not clean:
        return (f"conflicts with {BASE} itself in {', '.join(files)}; merge "
                f"{BASE} forward, rerun its targeted tests, then re-apply the label")
    culprits = [f"#{m.number}" for m in merged
                if _changed(m.sha, base_sha) & set(conflicted)]
    who = ", ".join(culprits) or ("the batch so far ("
                                  + ", ".join(f"#{m.number}" for m in merged) + ")")
    return (f"conflicts with {who} in {', '.join(conflicted)}; once that "
            f"merges, merge {BASE} forward, rerun its targeted tests, then re-apply the label")


def _not_ready(m: Member) -> str | None:
    """Why a labelled pull request sits this batch out, keeping its label.

    Other CI can be pending, cancelled or absent: the batch's CI is its
    CI. The own-head snapshot checkpoint must finish when required, and a
    failing head is a known failure and waits."""
    if m.draft:
        return "is a draft"
    if m.checks == "failing":
        red = f" ({m.checks_why})" if m.checks_why else ""
        return (f"CI on its head is failing{red}; it joins a batch once a rerun of "
                "the failed job passes or a push replaces that head")
    if m.checks == "unverified":
        why = m.checks_why or "GitHub's record of its CI could not be read"
        return (f"{why}, so a timeout or a failure is not ruled out; it joins a "
                "batch built once that read succeeds")
    if m.snapshot in ("pending", "failing", "unverified"):
        return m.snapshot_why or "own-head snapshot replay has not finished successfully"
    return None


def _project_version(rev: str) -> str | None:
    """`project.version` in pyproject.toml at `rev`, read as text (no tomllib
    on Python 3.10): the first `version = "..."` line of the [project] table."""
    shown = git("show", f"{rev}:pyproject.toml", check=False)
    if shown.returncode != 0:
        return None
    in_project = False
    for line in shown.stdout.splitlines():
        if line.startswith("["):
            in_project = line.strip() == "[project]"
        elif in_project and (match := VERSION_LINE_RE.match(line)):
            return match.group(1)
    return None


def _fragments(rev: str) -> set[str]:
    """The changelog fragments at `rev`: changelog.d/*.md but its README, as
    ops/assemble_changelog.py folds them."""
    listed = git("ls-tree", "-z", "--name-only", rev, f"{CHANGELOG_FRAGMENTS}/", check=False)
    return {path for path in listed.stdout.split("\0")
            if path.endswith(".md") and path != f"{CHANGELOG_FRAGMENTS}/README.md"}


def _is_release_cut(base_sha: str, m: Member) -> bool:
    """A member whose own diff changes project.version."""
    common = git("merge-base", base_sha, m.sha, check=False)
    return common.returncode == 0 and _project_version(m.sha) != _project_version(common.stdout.strip())


def _unfolded_reason(base_sha: str, fork: str, m: Member) -> str | None:
    """Why a release cut forked at ``fork`` may not merge onto ``base_sha``:
    the merge would leave changelog fragments unfolded."""
    at_base, at_fork, at_cut = _fragments(base_sha), _fragments(fork), _fragments(m.sha)
    # What the merge keeps: master's fragments the cut did not fold, and any
    # the cut adds.
    unfolded = sorted((at_base - (at_fork - at_cut)) | (at_cut - at_fork))
    if not unfolded:
        return None
    return (f"it is a release cut, but merging it onto {BASE} leaves "
            f"{len(unfolded)} changelog fragment(s) unfolded "
            f"({', '.join(unfolded[:3])}{', ...' if len(unfolded) > 3 else ''}), "
            "so the release would ship changes its notes do not describe; merge "
            f"{BASE} forward, run `python ops/assemble_changelog.py` again, "
            "rerun the cut's pinned tests, then re-apply the label")


def _release_cut_alone(plan: Plan, members: list[Member], refused: dict | None = None) -> list[Member]:
    """The members to merge: all of them, or only the first ready release cut
    (a member whose own diff changes project.version), with the others
    left out keeping their labels.

    The cut is alone because the release is tagged at the batch's merge:
    whatever shares the batch ships in the release, but only the fragments
    the cut folded are its notes. For the same reason a cut that would leave
    a fragment unfolded on the batch head (one that landed on master after
    the cut was folded, or one the cut adds) is left out, with what to do.
    A cut the freeze refuses (``refused``: its own change, or one its head
    carries) is not chosen: it is dropped with the rest of the merge, and the
    other members batch as usual."""
    refused = refused or {}
    for m in members:
        if _not_ready(m) or _fetch_head(m.number) != m.sha:
            continue
        # The same test _merge_members leaves a member out by (its own entry,
        # or _carried: a refused one its head carries that does not merely
        # wait on a labelled parent). A cut skipped here for any other reason
        # would still merge there, beside the other members.
        if m.sha in refused or _carried(refused, m):
            continue
        base = _out("merge-base", plan.base_sha, m.sha)
        if _project_version(m.sha) == _project_version(base):
            continue
        reason = _unfolded_reason(plan.base_sha, base, m)
        if reason:
            plan.dropped.append(Dropped(m, reason))
            print(f"  dropped #{m.number}: {reason}")
            # Another cut among the rest is still batched alone.
            return _release_cut_alone(plan, [other for other in members if other is not m], refused)
        for other in members:
            if other is not m:
                plan.dropped.append(Dropped(
                    other, f"#{m.number} is a release cut, which is batched alone: the "
                           "release is tagged at the batch's merge, so anything beside "
                           "the cut would ship without being in the release notes; this "
                           "joins the next batch", comment=False))
        print(f"  #{m.number} is a release cut; it is batched alone")
        return [m]
    return members


def _budget_text(rev: str, path: str) -> str | None:
    shown = git("show", f"{rev}:{path}", check=False)
    return shown.stdout if shown.returncode == 0 else None


def _budget_json(rev: str, path: str):
    text = _budget_text(rev, path)
    return None if text is None else json.loads(text)


def _data_module(source: str, name: str):
    # The caller supplies master's immutable source, never member code.
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / f"{name}.py"
        path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=None)
def _budget_tools(base_sha: str):
    """Master's budget reader/resolver, frozen by immutable commit ID."""
    source = _budget_text(base_sha, CI_BUDGET_CODE)
    if source is None:
        return None
    try:
        module = _data_module(source, "_batch_ci_budget")
        supported = all(hasattr(module, name) for name in (
            "definition_fingerprints", "parse_fingerprint_baseline", "parse_acceptance_document",
            "parse_acceptance", "approval_reason", "scope_matrix", "fingerprints", "UNRESOLVED",
            "budget_data", "merge_budget_data"))
        if not supported:
            if _budget_text(base_sha, CI_FINGERPRINTS) is not None:
                raise ValueError("master's fingerprint baseline has no compatible reader")
            return None  # The initial format migration is bootstrapped on its own head.
        scope = _budget_text(base_sha, CI_SCOPE_CODE)
        resolver = _data_module(scope, "_batch_parity_scope") if scope is not None else None
    except (Exception, SystemExit) as error:
        raise Refusal(f"{BASE}'s CI budget reader cannot load ({type(error).__name__}); fix it on {BASE} before building") from None
    return module, resolver, scope


def _budget_records(rev: str, module) -> list[dict]:
    listing = git("ls-tree", "-r", "--name-only", rev, "--", CI_ACCEPT, check=False)
    if listing.returncode != 0:
        raise ValueError("CI acceptance files could not be listed")
    records = []
    for path in listing.stdout.splitlines():
        if path.endswith(".json"):
            records.extend(module.parse_acceptance_document(_budget_json(rev, path)))
    return records


def _budget_definitions(rev: str, names, module) -> tuple[dict, dict, str | None]:
    import yaml
    definitions, raw = {}, {}
    scope = _budget_text(rev, CI_SCOPE_CODE)
    for name in names:
        if (not isinstance(name, str) or "/" in name or "\\" in name
                or not name.endswith((".yml", ".yaml"))):
            raise ValueError("CI budget workflow names must be YAML basenames")
        source = _budget_text(rev, f".github/workflows/{name}")
        workflow = {"jobs": {}} if source is None else yaml.safe_load(source)
        if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
            raise ValueError("invalid workflow data")
        raw[name] = workflow
        definitions[name] = module.definition_fingerprints(name, workflow, scope)
    return definitions, raw, scope


def _budget_member_evidence(base_sha: str, member: Member) -> list[dict]:
    """Verify every changed lane's reason before a generated baseline can cover it."""
    tools = _budget_tools(base_sha)
    if tools is None:
        return []
    module, resolver, base_scope = tools
    common = _out("merge-base", base_sha, member.sha)
    # An author cannot make a changed lane look measured by regenerating this file.
    if (_budget_text(base_sha, CI_FINGERPRINTS) is not None
            and _budget_text(common, CI_FINGERPRINTS) != _budget_text(member.sha, CI_FINGERPRINTS)):
        raise ValueError("PR changed generated fingerprints; restore the baseline and run ci_budget.py accept")
    data = _budget_json(member.sha, CI_BUDGET)
    before = _budget_json(common, CI_BUDGET)
    names = set(data or {}) | set(before or {})
    current, raw, scope = _budget_definitions(member.sha, names, module)
    previous, _, _ = _budget_definitions(common, names, module)
    records = _budget_records(member.sha, module)
    baseline_document = _budget_json(common, CI_FINGERPRINTS)
    baselines = module.parse_fingerprint_baseline(baseline_document) if baseline_document is not None else {}
    imported = []
    missing: list[str] = []   # every job without a reviewed reason, named at once
    for name in sorted(names):
        entry = (data or {}).get(name, {})
        old_entry = (before or {}).get(name, {})
        legacy = "fingerprints" in old_entry
        if not legacy and ("fingerprints" in entry or "accepted" in entry):
            raise ValueError("PR reintroduced legacy CI budget fields; record an acceptance fragment")
        recorded = old_entry.get("fingerprints", {}) if legacy else baselines.get(name, {})
        changed = {job for job in current[name].keys() | previous[name].keys()
                   if current[name].get(job) != previous[name].get(job)}
        legacy_actual = module.fingerprints(raw[name])
        if legacy and name == "rust.yml" and any(module.scope_matrix(raw[name]["jobs"].get(job, {})) for job in changed):
            if resolver is None or scope is None or scope.replace("\r\n", "\n") != (base_scope or "").replace("\r\n", "\n"):
                raise ValueError("legacy parity resolver differs; merge master and record an acceptance fragment")
            legacy_actual = module.fingerprints(resolver.resolve(copy.deepcopy(raw[name])))
        for job in sorted(changed):
            value = current[name].get(job)
            reason = module.approval_reason(records, name, job, recorded.get(job), value)
            if reason is None and legacy:
                expected = legacy_actual.get(job)
                if value is not None and (expected == module.UNRESOLVED or entry.get("fingerprints", {}).get(job) != expected):
                    raise ValueError(f"{name}/{job} has unverified legacy cost data; run ci_budget.py accept")
                reason = next((row.get("reason") for row in entry.get("accepted", [])
                               if row.get("job") == job and row.get("fingerprint") == expected
                               and isinstance(row.get("reason"), str) and row["reason"].strip()), None)
                runs = entry.get("measured_on", [])
                if reason is None and runs != old_entry.get("measured_on", []) and len(runs) >= 2:
                    reason = "Re-measured from hosted runs " + ", ".join(map(str, runs))
                if reason:
                    imported.append({"workflow": name, "job": job,
                                     "fingerprint": expected or previous[name].get(job), "reason": reason})
            if reason is None:
                missing.append(f"{name}/{job}")
        if legacy:
            existing = {json.dumps(row, sort_keys=True) for row in old_entry.get("accepted", [])}
            imported.extend({"workflow": name, **row} for row in entry.get("accepted", [])
                            if json.dumps(row, sort_keys=True) not in existing)
    if missing:
        raise ValueError(f"{', '.join(missing)} changed without a reviewed cost reason; "
                         "run ci_budget.py accept" + (" for each" if len(missing) > 1 else ""))
    # Historical records remain audit evidence, never fresh cost approvals.
    unique = {json.dumps(row, sort_keys=True): row for row in imported}
    for row in unique.values():
        module.parse_acceptance(row, legacy=True)
    return list(unique.values())


def _merge_budget_conflict(base_sha: str, conflicted: list[str]) -> bool:
    tools = _budget_tools(base_sha)
    if tools is None or conflicted != [CI_BUDGET]:
        return False
    module = tools[0]
    try:
        stages = [json.loads(_out("show", f":{stage}:{CI_BUDGET}")) for stage in (1, 2, 3)]
        merged = module.merge_budget_data(*stages)
    except (ValueError, TypeError):
        return False  # Incompatible authored prices/limits still drop the member.
    root = Path(_out("rev-parse", "--show-toplevel")).resolve()
    target = _metadata_target(root, CI_BUDGET)
    target.write_text(json.dumps(merged, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    git("add", "--", CI_BUDGET)
    git("commit", "--no-edit")
    return True


def _metadata_target(root: Path, relative: str) -> Path:
    target = root / relative
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        raise Refusal("generated CI metadata path cannot be resolved; restore regular repository files before building") from None
    if not resolved.is_relative_to(root) or resolved != target or target.is_symlink():
        raise Refusal("generated CI metadata path is redirected; restore regular repository files before building")
    return target


def _regenerate_budget(plan: Plan, root: Path) -> set[str]:
    tools = _budget_tools(plan.base_sha)
    if tools is None:
        return set()
    module = tools[0]
    data = _budget_json("HEAD", CI_BUDGET)
    if data is None:
        raise Refusal("the merged head removed its CI budget; restore it before building")
    try:
        definitions, _, _ = _budget_definitions("HEAD", data, module)
        document = {"schema": 1, "workflows": definitions}
        module.parse_fingerprint_baseline(document)
        payloads = {CI_FINGERPRINTS: document}
        authored = module.budget_data(data)
        if authored != data:
            payloads[CI_BUDGET] = authored
        for member in plan.merged:
            imported = _budget_member_evidence(plan.base_sha, member)
            if imported:
                path = f"{CI_ACCEPT}/imported-{member.sha}.json"
                payloads[path] = {"schema": 1, "legacy": True, "source_head": member.sha, "accepted": imported}
        serialized = {path: json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
                      for path, value in payloads.items()}
        targets = {path: _metadata_target(root, path) for path in serialized}
    except (ValueError, TypeError, KeyError) as error:
        raise Refusal(f"CI budget metadata cannot be derived ({error}); fix the member before building") from None
    created = []
    try:
        for path, text in serialized.items():
            target = targets[path]
            if not target.exists():
                created.append(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
            git("add", "--", path)
    except (OSError, Refusal):
        git("reset", "-q", "--hard", "HEAD")
        for target in created:
            if target.is_file() and target.resolve().is_relative_to(root):
                target.unlink()
        raise Refusal("generated CI metadata could not be written; check the checkout's disk and permissions") from None
    return set(serialized)


FREEZE_GUARD = "rust/ci/python_freeze.py"


def _guard_text(rev: str) -> str | None:
    shown = git("show", f"{rev}:{FREEZE_GUARD}", check=False)
    return shown.stdout if shown.returncode == 0 else None


def _freeze_module(base_sha: str, members: list[Member] = (), head: str = ""):
    """Master's copy of the Python freeze guard, loaded in this process
    (master's code, never a member's: an earlier member merged into this
    checkout must not decide a later one's admission).

    Master without it refuses, with one exception told apart by history,
    not presence: when no commit on master's history ever touched the
    guard, this is the batch that introduces it (the guard and this check
    land together, so `open` and `merge` run this code against a master
    that has never had it). Its members are then judged with the one copy
    the members carry, which the batch head (``head``, when given) must
    still hold. Anything else, a master that had the guard and lost it
    above all, refuses: admitting Python changes silently is the one
    outcome this must not have."""
    text = _guard_text(base_sha)
    if text is None:
        # --full-history: default history simplification follows only the
        # parent a merge is TREESAME to, so a guard dropped while resolving a
        # merge would read as never added. A shallow clone may hide both.
        if _out("rev-parse", "--is-shallow-repository") == "true":
            raise Refusal(f"this checkout is a shallow clone, so it cannot tell whether {BASE} "
                          f"ever had {FREEZE_GUARD}; run `git fetch --unshallow {REMOTE}` "
                          "and try again")
        if _out("rev-list", "-1", "--full-history", base_sha, "--", FREEZE_GUARD):
            raise Refusal(f"{BASE} has no {FREEZE_GUARD} although it had one, so the Python "
                          "freeze cannot be applied to this batch; restore it on master. The "
                          "pull request that deletes pseudolife_memory/ removes this check "
                          "in the same change")
        copies: dict[str, list[Member]] = {}
        for m in members:
            carried = _guard_text(m.sha)
            if carried is not None:
                copies.setdefault(carried, []).append(m)
        if not copies:
            raise Refusal(f"{BASE} has never had {FREEZE_GUARD} and no member adds it, so "
                          "the Python freeze cannot be applied; build with master's own "
                          "ops/integrate.py, or include the pull request that adds the guard")
        if len(copies) > 1:
            numbers = ", ".join(f"#{m.number}" for carriers in copies.values() for m in carriers)
            raise Refusal(f"the members introducing {FREEZE_GUARD} ({numbers}) carry different "
                          "copies of it; batch one copy at a time")
        (text, carriers), = copies.items()
        if head and _guard_text(head) != text:
            raise Refusal(f"this batch introduces {FREEZE_GUARD} with #{carriers[0].number}'s "
                          "copy, but its head holds another; rebuild it")
        print(f"  {BASE} has never had {FREEZE_GUARD}: this batch introduces it, so its "
              f"members are judged with the copy #{carriers[0].number} adds")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "python_freeze.py"
        path.write_text(text, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("_batch_python_freeze", path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except (Exception, SystemExit) as exc:
            raise Refusal(f"{BASE}'s own {FREEZE_GUARD} fails to load ({exc!r}); fix it on "
                          f"{BASE} before building") from None
    return module


def _frozen_refusal(freeze, commit: str, labels) -> str | None:
    """Why a merge commit on the batch branch may not stay: what it changes
    against its first parent touches the frozen Python package and the
    member does not carry the exception label. Judged on the merge itself,
    not on a merge-base diff: with two merge bases, `git merge-base` reports
    one, and a member can set a frozen file back with an empty diff from it.
    The `Python freeze` check is early feedback on the pull request; this is
    the gate, since the batch is exempt from that check and a member's own
    run can be absent, queued or cancelled."""
    paths = freeze.changed_paths(Path.cwd(), f"{commit}^1", commit)   # the checkout `build` runs in
    if paths is None:
        return "its change could not be read for the Python freeze check; rebuild once git can read it"
    refused = freeze.frozen_paths(paths)
    if refused and freeze.EXCEPTION_LABEL not in labels:
        return freeze.refusal(refused)
    return None


def _undo_merge_if_frozen(plan: Plan, m: Member, freeze) -> bool:
    """Drop the merge just made for ``m`` when it changes frozen paths
    without the label; True when it was undone."""
    frozen = _frozen_refusal(freeze, "HEAD", m.labels)
    if not frozen:
        return False
    git("reset", "-q", "--hard", "HEAD^1")
    plan.dropped.append(Dropped(m, frozen))
    print(f"  dropped #{m.number}: {frozen}")
    return True


def _labels_now(number: int) -> tuple[str, ...]:
    view = json.loads(gh("pr", "view", str(number), "--json", "labels") or "{}")
    return tuple(label.get("name", "") for label in view.get("labels") or [] if isinstance(label, dict))


def _batch_freeze_refusal(plan: Plan) -> str | None:
    """Why the built batch may not be published or merged, judged on the
    batch as a whole with master's guard: every commit since its base must
    be a member's merge or the one generated commit, the generated commit
    touches nothing frozen, and a member merge that changes frozen paths
    still carries the exception label (read again now: it may have been
    removed since `build`). A member whose head the batch already
    contained has no merge of its own: its changes entered under the
    containing member's merge, judged with that member's labels."""
    if not _is_ancestor(plan.base_sha, plan.head):
        return (f"{plan.branch}'s head {plan.head[:12]} does not descend from its base "
                f"{plan.base_sha[:12]}; rebuild it")
    freeze = _freeze_module(plan.base_sha, plan.merged, plan.head)
    members = {m.sha: m for m in plan.merged if m.number not in plan.contained}
    seen: set[str] = set()
    generated = 0
    for commit in _out("rev-list", "--first-parent", "--reverse",
                       f"{plan.base_sha}..{plan.head}").split():
        parents = _out("rev-list", "--parents", "-n", "1", commit).split()[1:]
        if len(parents) == 2 and parents[1] in members:
            m = members[parents[1]]
            seen.add(m.sha)
            first = _frozen_refusal(freeze, commit, (freeze.EXCEPTION_LABEL,))
            if first:   # unreadable
                return f"#{m.number}'s merge {commit[:12]}: {first}"
            if _frozen_refusal(freeze, commit, ()):
                frozen = _frozen_refusal(freeze, commit, _labels_now(m.number))
                if frozen:
                    return (f"#{m.number} no longer carries the {freeze.EXCEPTION_LABEL} label its "
                            f"Python change was admitted with: {frozen}; rebuild without it, or "
                            "restore the label if the maintainer's exception stands")
            continue
        subject = _out("log", "-1", "--format=%s", commit)
        if len(parents) == 1 and subject == CENSUS_SUBJECT and generated == 0:
            generated += 1
            frozen = _frozen_refusal(freeze, commit, ())
            if frozen:
                return f"the generated commit {commit[:12]} changes the frozen package: {frozen}"
            continue
        return (f"commit {commit[:12]} ({subject!r}) on {plan.branch} is neither a member merge "
                "nor the one generated commit; nothing is fixed inside a batch, so rebuild it")
    missing = [f"#{m.number}" for m in plan.merged if m.sha not in seen
               and not (m.number in plan.contained and _is_ancestor(m.sha, plan.head))]
    if missing:
        return f"the batch lacks the merges of {', '.join(missing)}; rebuild it"
    for m in plan.merged:
        if _own_frozen_refusal(freeze, plan.base_sha, m, (freeze.EXCEPTION_LABEL,)):
            return f"#{m.number}'s change could not be read for the Python freeze check; rebuild it"
        if _own_frozen_refusal(freeze, plan.base_sha, m, ()):
            frozen = _own_frozen_refusal(freeze, plan.base_sha, m, _labels_now(m.number))
            if frozen:
                return (f"#{m.number}'s own change touches the frozen package without the "
                        f"{freeze.EXCEPTION_LABEL} label: {frozen}; rebuild without it, or "
                        "restore the label if the maintainer's exception stands")
    for number in plan.carried:
        if freeze.EXCEPTION_LABEL not in _labels_now(number):
            return (f"#{number}'s change to the frozen package is in this batch through a stacked "
                    f"pull request, and #{number} no longer carries the {freeze.EXCEPTION_LABEL} "
                    "label it was admitted with; rebuild without the stack, or restore the label "
                    "if the maintainer's exception stands")
    return None


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    return git("merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


# The files of the release-procedure skill's seven-file version cut that
# lie inside the frozen package: the only package paths a cut may change,
# label or no label.
CUT_FROZEN_FILES = frozenset({"pseudolife_memory/compose/docker-compose.yml"})
# The open pull requests build lists to find any a member's head carries; a
# listing that reaches this many may be cut off, so build refuses.
OPEN_PR_LIMIT = 500


def _own_frozen_paths(freeze, base_sha: str, sha: str) -> list[str] | None:
    """The frozen paths a pull request's own change against master touches
    (from its merge base); None when it cannot be read."""
    common = git("merge-base", base_sha, sha, check=False)
    paths = freeze.changed_paths(Path.cwd(), common.stdout.strip(), sha) if common.returncode == 0 else None
    return None if paths is None else freeze.frozen_paths(paths)


def _has_commit(sha: str) -> bool:
    return git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _open_rows() -> list[dict]:
    """Every open pull request, whatever its base: one a member's head
    carries is judged on its own change. Fails closed on a failed or
    possibly cut-off listing, since a missing row is a parent unjudged."""
    try:
        text = gh("pr", "list", "--state", "open", "--limit", str(OPEN_PR_LIMIT), "--json",
                  "number,headRefOid,labels")
    except Refusal as error:
        raise Refusal(f"build lists every open pull request, to judge any a member's head carries "
                      f"for the Python freeze, and that listing failed: {error}; rerun once "
                      "`gh pr list --state open` works (`gh auth status`, the network)") from None
    rows = json.loads(text or "[]")
    if len(rows) >= OPEN_PR_LIMIT:
        raise Refusal(f"the listing of open pull requests reached its limit of {OPEN_PR_LIMIT}, so an "
                      "open pull request a member's head carries could be missing from it and go "
                      "unjudged by the Python freeze; raise OPEN_PR_LIMIT in ops/integrate.py and rebuild")
    return rows


def _fetch_listed(members: list[Member]) -> None:
    """Fetch each listed head not yet local, so the freeze can read it."""
    for m in members:
        if not _has_commit(m.sha):
            git("fetch", "-q", REMOTE, f"pull/{m.number}/head", check=False)


class _Prepass:
    """What the freeze decided before anything merges, for every listed
    member and every open pull request a member's head carries:
    ``refused`` (sha -> (number, reason, keep the label)) and ``exceptions``
    (sha -> (number, frozen paths)) for the labelled changes to the package."""

    def __init__(self):
        self.refused: dict[str, tuple[int, str, bool]] = {}
        self.exceptions: dict[str, tuple[int, set[str]]] = {}


def _freeze_prepass(plan: Plan, freeze, members: list[Member]) -> _Prepass:
    """Each pull request is judged on its own change, whichever merge carries
    it. A member's head can carry open pull requests that are not listed
    (no batch label, or not ready): those are found among the open pull
    requests whose head is in a member's history, and judged the same way."""
    result = _Prepass()
    listed = {m.number for m in members}
    _fetch_listed(members)
    # A listed head that cannot be had (it moved after listing) is left to
    # _merge_members, which drops it as moved; nothing of it can merge.
    present = [m for m in members if _has_commit(m.sha)]
    judged: list[tuple[Member, bool]] = [(m, True) for m in present]
    history: set[str] = set()
    for m in present:
        history.update(_out("rev-list", m.sha, f"^{plan.base_sha}").split())
    heads = {m.sha for m in present}
    for row in _open_rows():
        head = row.get("headRefOid") or ""
        # Whatever its base: a parent stacked on another branch is carried
        # all the same. A head a member has is judged as that member.
        if row.get("number") in listed or head not in history or head in heads:
            continue
        labels = tuple(label.get("name", "") for label in row.get("labels") or [] if isinstance(label, dict))
        judged.append((Member(number=row["number"], title="", sha=head, url="", review="", checks="",
                              labels=labels), False))
    own: dict[str, list[str]] = {}
    for m, is_member in judged:
        paths = _own_frozen_paths(freeze, plan.base_sha, m.sha)
        if paths is None:
            # The checkout's fault or the branch's, not the change's: no
            # comment, no label removed, and nothing is built.
            raise Refusal(f"#{m.number}'s change against {BASE} could not be read for the Python "
                          f"freeze check (git merge-base or diff of {m.sha[:12]} failed): the batch "
                          "checkout is shallow or missing objects (fetch with full history), or the "
                          f"branch has no history in common with {BASE} (remove `{plan.label}` from "
                          f"#{m.number} until it does); no label was changed")
        own[m.sha] = paths
        beyond = sorted(set(paths) - CUT_FROZEN_FILES)
        if paths and beyond and _is_release_cut(plan.base_sha, m):
            result.refused[m.sha] = (m.number, (
                f"it is a release cut, and its change against {BASE} also changes "
                f"{', '.join(beyond)} inside the frozen Python package, where a cut may change only "
                f"{', '.join(sorted(CUT_FROZEN_FILES))} (the version cut), label or no label: move "
                f"that change to its own pull request, then re-apply `{plan.label}`"), False)
        elif paths and freeze.EXCEPTION_LABEL in m.labels:
            result.exceptions[m.sha] = (m.number, set(paths))
    for m, is_member in judged:
        paths = own[m.sha]
        if not paths or freeze.EXCEPTION_LABEL in m.labels or m.sha in result.refused:
            continue
        # A stacked child whose frozen files are exactly a labelled
        # parent's (nothing frozen changes from that parent's head to its
        # own) waits for that parent, keeping its label, rather than being
        # told to rewrite in Rust a change it did not make. One that edits
        # them further has a Python change of its own, refused below.
        parents = [number for sha, (number, _) in result.exceptions.items()
                   if sha != m.sha and _is_ancestor(sha, m.sha) and _frozen_between(freeze, sha, m.sha) == []]
        if parents:
            names = ", ".join(f"#{number}" for number in parents)
            result.refused[m.sha] = (m.number, (
                f"its own change against {BASE} holds {names}'s labelled change to the frozen "
                f"Python package, since it is stacked on {names}; it joins once {names} has "
                f"landed, or carries `{freeze.EXCEPTION_LABEL}` itself if the maintainer rules "
                "the exception covers it"), True)
            continue
        reason = freeze.refusal(paths)
        if is_member and set(paths) <= CUT_FROZEN_FILES and _is_release_cut(plan.base_sha, m):
            # Not "do it in Rust": a cut may still edit the packaged compose
            # copy, under an exception only the maintainer grants, per cut.
            reason += (f" This is a release cut, which may still edit that version file under an "
                       f"exception only the maintainer grants: once the maintainer has ruled it for "
                       f"this cut, it carries `{freeze.EXCEPTION_LABEL}` (applied by the maintainer, "
                       f"or by the conductor after that ruling), then re-apply `{plan.label}`")
        result.refused[m.sha] = (m.number, reason, False)
    return result


def _frozen_between(freeze, a: str, b: str) -> list[str] | None:
    """The frozen paths that differ between two commits; None when unread."""
    paths = freeze.changed_paths(Path.cwd(), a, b)
    return None if paths is None else freeze.frozen_paths(paths)


def _own_frozen_refusal(freeze, base_sha: str, m: Member, labels) -> str | None:
    """Why a member's own change against master (from its merge base) may
    not land: it touches the frozen package without the exception label.
    Judged per pull request, whichever merge carries the change: a stacked
    child merged first carries its parent's change, and the parent still
    needs its own label."""
    common = git("merge-base", base_sha, m.sha, check=False)
    paths = (freeze.changed_paths(Path.cwd(), common.stdout.strip(), m.sha)
             if common.returncode == 0 else None)
    if paths is None:
        return "its change could not be read for the Python freeze check; rebuild once git can read it"
    refused = freeze.frozen_paths(paths)
    if refused and freeze.EXCEPTION_LABEL not in labels:
        return freeze.refusal(refused)
    return None


def _carried(refused: dict, m: Member) -> list[int]:
    """The refused pull requests ``m``'s head carries. A waiting entry (keep)
    only waits on a labelled parent, and its frozen files are that parent's:
    carrying it carries nothing unruled."""
    return [number for sha, (number, _, keep) in refused.items()
            if not keep and sha != m.sha and _is_ancestor(sha, m.sha)]


def _carried_reason(freeze, carried: list[int]) -> str:
    names = ", ".join(f"#{number}" for number in carried)
    return (f"its head carries {names}'s change to the frozen Python package, which "
            f"{names} is not labelled `{freeze.EXCEPTION_LABEL}` for; it joins once "
            f"{names} lands, leaves the stack, or the maintainer rules {names}'s change "
            "an exception")


def _once_ready(base_sha: str, m: Member, freeze, prepass: _Prepass) -> list[str]:
    """What would still refuse a member that sits this batch out as not
    ready, once it is: the checks `_merge_members` runs only after
    readiness, judged against master alone. Without this, a pending replay
    hides a budget or freeze refusal until the replay has finished."""
    if not _has_commit(m.sha):
        return [f"its listed head {m.sha[:12]} is not in this checkout (it moved after listing), "
                "so it was not judged further; rebuild to judge it"]
    reasons: list[str] = []
    # The freeze's own verdict, as `_merge_members` would give it; the merge
    # check below would only repeat it, and for a child waiting on a
    # labelled parent would contradict it.
    judged = False
    if m.sha in prepass.refused:
        reasons.append(prepass.refused[m.sha][1])
        judged = True
    if carried := _carried(prepass.refused, m):
        reasons.append(_carried_reason(freeze, carried))
        judged = True
    if _is_release_cut(base_sha, m):
        unfolded = _unfolded_reason(base_sha, _out("merge-base", base_sha, m.sha), m)
        if unfolded:
            reasons.append(unfolded)
    try:
        _budget_member_evidence(base_sha, m)
    except (ValueError, TypeError, KeyError, Refusal) as error:
        reasons.append(f"CI budget metadata refused: {error}")
    merged = git("merge-tree", "--write-tree", "--name-only", "--no-messages", base_sha, m.sha, check=False)
    lines = merged.stdout.splitlines()
    if merged.returncode == 1:
        files = [line for line in lines[1:] if line]
        # `_merge_members` normalizes a conflict in the budget file alone
        # (`_merge_budget_conflict`), so only that merge can tell.
        if files != [CI_BUDGET]:
            reasons.append(f"its merge onto {BASE} conflicts in {', '.join(files) or 'files git did not name'}")
    elif merged.returncode != 0 or not lines:
        reasons.append(f"its merge onto {BASE} could not be computed ({merged.stderr.strip() or 'no tree'})")
    elif not judged:
        frozen = _frozen_between(freeze, base_sha, lines[0])
        if frozen is None:
            reasons.append(f"its merge onto {BASE} could not be read for the Python freeze check")
        elif frozen and freeze.EXCEPTION_LABEL not in m.labels:
            reasons.append(freeze.refusal(frozen))
    return list(dict.fromkeys(reasons))


def _report_once_ready(base_sha: str, waiting: list[tuple[Member, str]], freeze, prepass: _Prepass) -> None:
    """Report only: it never refuses, so a build it follows stands as built."""
    for m, _ in waiting:
        try:
            later = _once_ready(base_sha, m, freeze, prepass)
        except Refusal as error:
            later = [f"it could not be judged further ({error})"]
        print(f"  #{m.number}, once ready: " + ("nothing else refuses it" if not later else
                                                "; and ".join(later)))


def _merge_members(plan: Plan, members: list[Member], freeze=None, prepass: _Prepass | None = None) -> None:
    freeze = freeze or _freeze_module(plan.base_sha, members)
    # Every pull request's own change is judged before anything merges
    # (_freeze_prepass, over the whole listing when _merge_batch gives it),
    # so a member whose head carries a refused one (a stacked child over a
    # parent still waiting on its CI, or not listed at all) is left out too,
    # whichever order they were labelled in.
    prepass = prepass or _freeze_prepass(plan, freeze, members)
    own = prepass.refused
    for m in members:
        reason = _not_ready(m)
        if reason:
            plan.dropped.append(Dropped(m, reason, comment=False))
            continue
        head = _fetch_head(m.number)
        if head != m.sha:
            plan.dropped.append(Dropped(
                m, f"head moved from {m.sha[:12]} to {head[:12]} after listing; "
                   "rebuild to include it", comment=False))
            continue
        if m.sha in own:
            _, reason, keep = own[m.sha]
            plan.dropped.append(Dropped(m, reason, comment=not keep))
            print(f"  {'left out' if keep else 'dropped'} #{m.number}: {reason}")
            continue
        carried = _carried(own, m)
        if carried:
            reason = _carried_reason(freeze, carried)
            plan.dropped.append(Dropped(m, reason, comment=False))
            print(f"  left out #{m.number}: {reason}")
            continue
        try:
            _budget_member_evidence(plan.base_sha, m)
        except (ValueError, TypeError, KeyError) as error:
            reason = f"CI budget metadata refused: {error}"
            plan.dropped.append(Dropped(m, reason))
            print(f"  dropped #{m.number}: {reason}")
            continue
        # rerere off: a replayed resolution would be a conflict resolved
        # inside the batch, and would hide the conflict from --diff-filter=U.
        before = _out("rev-parse", "HEAD")
        proc = git("-c", "rerere.enabled=false", "merge", "--no-ff", "--no-edit", "-m",
                   f"Merge #{m.number} into {plan.branch}: {m.title}", m.sha,
                   check=False)
        if proc.returncode == 0 and _out("rev-parse", "HEAD") == before:
            # "Already up to date": the batch holds this head already (a
            # stacked child merged first, or a head on master GitHub has not
            # marked merged). No merge commit to judge; HEAD^1 would be the
            # previous member's merge, or below the base.
            if not _is_ancestor(m.sha, before):
                raise Refusal(f"merging #{m.number} made no commit, yet its head "
                              f"{m.sha[:12]} is not in the batch")
            plan.merged.append(m)
            plan.contained.append(m.number)
            print(f"  merged #{m.number} {m.sha[:12]} {m.title} (already contained)")
            continue
        if proc.returncode == 0:
            if _undo_merge_if_frozen(plan, m, freeze):
                continue
            plan.merged.append(m)
            print(f"  merged #{m.number} {m.sha[:12]} {m.title}")
            continue
        conflicted = _out("diff", "--name-only", "--diff-filter=U").splitlines()
        if _merge_budget_conflict(plan.base_sha, conflicted):
            if _undo_merge_if_frozen(plan, m, freeze):
                continue
            plan.merged.append(m)
            print(f"  merged #{m.number} {m.sha[:12]} after normalizing derived CI budget metadata")
            continue
        if git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            git("merge", "--abort")
        if not conflicted:
            raise Refusal(f"merging #{m.number} failed without a conflict: "
                          + (proc.stderr.strip() or proc.stdout.strip()))
        reason = _collision(m, conflicted, plan.merged, plan.base_sha)
        plan.dropped.append(Dropped(m, reason))
        print(f"  dropped #{m.number}: {reason}")


# The census script is member code, so it runs without the operator's tokens
# in its environment: only what Python and git need to start, on Windows or
# POSIX. (It still runs as the operator, as the full suite does.)
CENSUS_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
              "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL",
              "PYTHONUTF8", "PYTHONIOENCODING")


def _census_module(base_sha: str):
    """Master's copy of the census module, loaded in this process (master's
    code, never a member's). None when master has none."""
    shown = git("show", f"{base_sha}:{CENSUS}", check=False)
    if shown.returncode != 0:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "producer_census.py"
        path.write_text(shown.stdout, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("_batch_producer_census", path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except (Exception, SystemExit) as exc:
            raise Refusal(f"{BASE}'s own {CENSUS} fails to load ({exc!r}); the census "
                          f"is broken on {BASE} itself, so fix it there before "
                          "building") from None
    return module


def _census_inputs(base_sha: str) -> set[str] | None:
    """The census module's own CURRENCY_INPUTS, read from master's copy of
    it, so no member code runs in this process (a member changing the script
    is still caught: the script is one of them). None when master has none."""
    module = _census_module(base_sha)
    return None if module is None else set(module.CURRENCY_INPUTS)


def _census_drift(rev: str) -> set[str]:
    """The paths whose trusted manual shapes the census committed at `rev`
    withdrew (its `manual_drift`), parsed as data; empty when it has none."""
    shown = git("show", f"{rev}:{CENSUS_JSON}", check=False)
    if shown.returncode != 0:
        return set()
    try:
        drift = json.loads(shown.stdout).get("manual_drift")
    except (ValueError, AttributeError, RecursionError):
        return set()
    if not isinstance(drift, list):
        return set()
    return {d["path"] for d in drift
            if isinstance(d, dict) and isinstance(d.get("path"), str)}


def _census_run(root: Path, *args: str, event: dict | None = None
                ) -> subprocess.CompletedProcess:
    """Run the batch head's census script, member code, with only the
    environment Python and git need (CENSUS_ENV) and, for `--ci-check`, the
    GitHub event CI would give it."""
    env = {k: v for k, v in os.environ.items() if k.upper() in CENSUS_ENV}
    with tempfile.TemporaryDirectory() as tmp:
        if event is not None:
            path = Path(tmp) / "event.json"
            path.write_text(json.dumps(event), encoding="utf-8")
            env["GITHUB_EVENT_PATH"] = str(path)
        return _run([sys.executable, CENSUS, *args], check=False, cwd=root, env=env)


def _census_ci_fails(root: Path, plan: Plan) -> bool:
    """Would CI's producer census check fail on HEAD, the batch's pull
    request? Runs the step CI runs, `rust/producer_census.py --ci-check`,
    with the event of a pull request from the batch branch on its base,
    which CI gates on census currency whatever the diff."""
    event = {"pull_request": {"base": {"sha": plan.base_sha}, "head": {"ref": plan.branch}}}
    proc = _census_run(root, "--ci-check", event=event)
    if proc.returncode == 1 and "producer census is stale" in proc.stderr:
        return True
    if proc.returncode != 0:
        raise Refusal(f"`python {CENSUS} --ci-check` failed ({proc.returncode}) on "
                      f"the batch head: {proc.stderr.strip() or proc.stdout.strip()}")
    return False


def _census_failure(proc: subprocess.CompletedProcess, root: Path) -> str:
    """The census script's failure in one line, its last output line, with
    this machine's paths replaced: it is posted on a public pull request."""
    lines = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
    text = lines[-1].strip() if lines else ""
    for path, name in ((root, "<checkout>"), (Path.home(), "<home>")):
        for form in {str(path), path.as_posix()}:
            text = re.sub(re.escape(form), name, text, flags=re.IGNORECASE)
    return f"exit {proc.returncode}" + (f": {text[:300]}" if text else "")


def _regenerate_census(plan: Plan) -> str | None:
    """Regenerate the producer census on HEAD with the head's own script and
    commit it as the batch's one generated commit when it changed: derived
    data, so no member is edited and no member ever has to regenerate it
    (#960 and #974 each went stale three times on 2026-10-26 doing so).
    Returns the script's failure, leaving HEAD as it was, or None."""
    root = Path(_out("rev-parse", "--show-toplevel")).resolve()
    census_needed = _census_inputs(plan.base_sha) is not None
    if census_needed:
        if not (root / CENSUS).is_file():
            return f"the batch head has no {CENSUS}"
        proc = _census_run(root)
        if proc.returncode != 0:
            git("reset", "-q", "--hard", "HEAD")
            return _census_failure(proc, root)
    # Staged first: when a member deleted the census, the rewrite is untracked
    # and an unstaged diff would never show (or commit) it.
    if (root / CENSUS_JSON).is_file():
        git("add", "--", CENSUS_JSON)
    census_changed = _out("diff", "--name-only", "HEAD").splitlines()
    if any(path != CENSUS_JSON for path in census_changed):
        git("reset", "-q", "--hard", "HEAD")
        raise Refusal(f"`python {CENSUS}` on the batch head changed "
                      f"{', '.join(census_changed)}, not only {CENSUS_JSON}; "
                      "nothing was committed. Find the member that made it do so "
                      "and leave it out of the next build")
    try:
        budget_paths = _regenerate_budget(plan, root)
    except Refusal:
        git("reset", "-q", "--hard", "HEAD")
        raise
    changed = _out("diff", "--name-only", "HEAD").splitlines()
    if not changed:
        # A member's own census can already be current and still withdraw
        # manual shapes master's had not.
        plan.census_drift = sorted(_census_drift("HEAD") - _census_drift(plan.base_sha))
        return None
    if set(changed) - ({CENSUS_JSON} | budget_paths):
        git("reset", "-q", "--hard", "HEAD")
        raise Refusal(f"`python {CENSUS}` on the batch head changed "
                      f"{', '.join(changed)}, outside the declared generated metadata; "
                      "nothing was committed. Find the member that made it do so "
                      "(run it on each member's head) and leave it out of the next build")
    git("commit", "-q", "-m", CENSUS_SUBJECT, "-m",
        "Generated producer census and CI fingerprint metadata on the batch head; "
        "authored budgets and reviewed reasons were preserved, and no member code was edited.", "--", *changed)
    if census_needed and _census_ci_fails(root, plan):
        raise Refusal(f"the census `python {CENSUS}` just wrote on the batch head is "
                      "already stale by its own `--ci-check`, so the script does not "
                      f"reproduce its output: if a member changes {CENSUS}, leave it "
                      "out of the next build; otherwise fix the script on master")
    plan.census_commit = _out("rev-parse", "HEAD")
    plan.census_drift = sorted(_census_drift("HEAD") - _census_drift(plan.base_sha))
    print(f"  regenerated CI metadata in {plan.census_commit[:12]}")
    return None


def _census_reason(member: Member, files: list[str], failure: str,
                   culprits: list[Member]) -> str:
    others = [f"#{m.number}" for m in culprits if m is not member]
    also = (f"; {' and '.join(others)} also change{'s' if len(others) == 1 else ''} "
            "them and left this batch too" if others else "")
    return (f"changes the producer census inputs ({', '.join(files)}), and "
            f"`python {CENSUS}` fails on the batch head ({failure}){also}; make it run "
            f"on this branch merged with {BASE}, rerun its targeted tests, then "
            "re-apply the label")


def _remerge(plan: Plan, members: list[Member], dropped: list[Dropped],
             out: set[int], freeze=None, prepass: _Prepass | None = None) -> None:
    """Merge `members` but `out` again from the base, in their order."""
    git("reset", "-q", "--hard", plan.base_sha)
    plan.merged, plan.dropped, plan.contained, plan.carried = [], list(dropped), [], []
    _merge_members(plan, [m for m in members if m.number not in out], freeze, prepass)
    _record_carried(plan, prepass)


def _record_carried(plan: Plan, prepass: _Prepass | None) -> None:
    """The labelled exceptions in the batch head that did not merge themselves."""
    if prepass is None:
        return
    merged = {m.number for m in plan.merged}
    head = _out("rev-parse", "HEAD")
    plan.carried = sorted({number for sha, (number, _) in prepass.exceptions.items()
                           if number not in merged and _is_ancestor(sha, head)})


def _merge_batch(plan: Plan, members: list[Member]) -> tuple:
    """Merge the members, then regenerate derived CI metadata on the head
    (see `_regenerate_census`). When the head's census script fails, the
    members whose own diff changes the census inputs leave and the rest
    merge again from the base. Nothing else is added inside a batch."""
    # The freeze judges the whole listing (and the open pull requests it
    # carries) before the cut search narrows it to one member.
    _fetch_listed(members)       # the first landing reads the guard the members carry
    freeze = _freeze_module(plan.base_sha, members)
    prepass = _freeze_prepass(plan, freeze, members)
    judged = (freeze, prepass)
    members = _release_cut_alone(plan, members, prepass.refused)
    cut_drops = list(plan.dropped)
    _merge_members(plan, members, freeze, prepass)
    _record_carried(plan, prepass)
    _refuse_a_cut_beside_others(plan)
    inputs = _census_inputs(plan.base_sha)
    if not plan.merged or inputs is None and _budget_tools(plan.base_sha) is None:
        return judged    # master has no supported generated metadata
    drops: list[Dropped] = []
    # Each round drops at least one member, so this ends. A member a dropped
    # one had kept out (a conflict with it) merges in the next round, and is
    # searched like the rest.
    while plan.merged and (failure := _regenerate_census(plan)) is not None:
        culprits = [(m, sorted(_changed(m.sha, plan.base_sha) & inputs)) for m in plan.merged]
        culprits = [(m, files) for m, files in culprits if files]
        if not culprits:
            without = (" without the members that change its inputs" if drops else
                       f", and no member's own diff changes {', '.join(sorted(inputs))}")
            raise Refusal(f"`python {CENSUS}` fails on the batch head ({failure}){without}. "
                          f"Run it on {REMOTE}/{BASE}: if it fails there too, the cause is "
                          "master or this machine; otherwise run it on each member's head "
                          "and leave out the one that breaks it")
        left = [m for m, _ in culprits]
        for m, files in culprits:
            drops.append(Dropped(m, _census_reason(m, files, failure, left)))
            print(f"  dropped #{m.number}: {drops[-1].reason}")
        out = {d.member.number for d in drops}
        print("  merging again without " + ", ".join(f"#{n}" for n in sorted(out)))
        _remerge(plan, members, cut_drops + drops, out, freeze, prepass)
        _refuse_a_cut_beside_others(plan)
    return judged


def _refuse_a_cut_beside_others(plan: Plan) -> None:
    """A release cut is batched alone (_release_cut_alone says why); a batch
    that merged one beside anything else is refused, whatever let it through,
    so a drift between the cut search and the merge fails loudly instead of
    shipping changes outside the release notes."""
    cuts = [m.number for m in plan.merged if _is_release_cut(plan.base_sha, m)]
    if cuts and len(plan.merged) > 1:
        others = ", ".join(f"#{m.number}" for m in plan.merged if m.number not in cuts)
        raise Refusal(f"release cut {', '.join(f'#{n}' for n in cuts)} merged beside {others}, but a "
                      "cut is batched alone (the release is tagged at the batch's merge); nothing was "
                      "built. This is a fault in ops/integrate.py's cut search: build with only the "
                      "cut labelled, and report it")


def _new_markers(base_sha: str) -> list[str]:
    def files(rev: str) -> set[str]:
        proc = git("grep", "-l", "-E", MARKER_RE, rev, check=False)
        return {line.split(":", 1)[1] for line in proc.stdout.splitlines() if ":" in line}
    return sorted(files("HEAD") - files(base_sha))


def _touches_rust(base_sha: str, head: str) -> bool:
    """Does the batch change anything under rust/ but the generated census,
    which also changes when only scanned files outside rust/ did?"""
    changed = _out("diff", "--name-only", "--no-renames", base_sha, head).splitlines()
    return any(path.startswith(RUST_CHECK_DIR + "/") and path != CENSUS_JSON
               for path in changed)


def _rust_check_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not SECRET_ENV_RE.search(k) and not URL_CREDENTIALS_RE.search(v)}
    env["CARGO_TERM_COLOR"] = "never"   # plain `error[...]` lines to read
    return env


def _rust_check_remote() -> tuple[dict[str, str] | None, str]:
    """Where the check runs: (None, "") for this host, (config, "") for the
    remote in rust-check.remote, or (None, cause) when it cannot run."""
    where = os.environ.get(RUST_CHECK_WHERE_ENV, "")
    if where not in ("", "local", "remote"):
        return None, f"{RUST_CHECK_WHERE_ENV} must be local or remote, not {where!r}"
    if where == "local":
        return None, ""
    if not RUST_CHECK_REMOTE.exists() and where != "remote":
        return None, ""
    return _remote_config(RUST_CHECK_REMOTE, RUST_CHECK_ROLES, RUST_CHECK_DEFAULT_ROLE)


def _remote_config(path: Path, roles: tuple[str, ...], default_role: str) -> tuple[dict[str, str] | None, str]:
    if path.exists() and not path.is_file():
        return None, f"{path} is not a file; make it one or remove it"
    if not path.is_file():
        return None, f"no {path.name} (docs/dev/dev-machines.md)"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        # Windows PowerShell 5.1's `>` and Out-File write UTF-16.
        return None, (f"cannot read {path.name} as UTF-8 ({exc.reason}); "
                      "write it as UTF-8 (pwsh 7, or Set-Content -Encoding utf8)")
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc}"
    cfg: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*([a-z_]+)\s*=\s*(.*?)\s*$", line)
        if not m or m.group(1) not in ("ssh", "repo", "role"):
            return None, (f"{path.name} line {number} is not one of ssh=, repo= "
                          "or role=")
        if m.group(1) in cfg:
            return None, f"{path.name} gives {m.group(1)}= twice"
        cfg[m.group(1)] = m.group(2)
    bad = f"{path.name} needs"
    # An ssh destination that starts with "-" would be read as an option.
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", cfg.get("ssh", "")):
        return None, f"{bad} ssh=<an ~/.ssh/config alias>"
    if not RUST_CHECK_REPO_RE.fullmatch(cfg.get("repo", "")):
        return None, (f"{bad} repo=<the path of a clone there>, in letters, digits, "
                      "spaces and _ . : \\ / ~ - only")
    cfg.setdefault("role", default_role)
    if cfg["role"] not in roles:
        return None, (f"{bad} role= to be one of {', '.join(roles)}: the batch "
                      "body prints it, so never a machine's name")
    return cfg, ""


def _cargo_verdict(code: int, lines: list[str]) -> tuple[str, str]:
    """The plan's record and the errors to print, from cargo's exit and output."""
    if code == 0:
        return "passed", ""
    errors = [line for line in lines if line.startswith("error")]
    first = errors[0].strip() if errors else f"exit {code}"
    start = lines.index(errors[0]) if errors else max(len(lines) - 40, 0)
    return f"failed: {first[:300]}", "\n".join(lines[start:start + 60])


def _ps_literal(value: str) -> str:
    """A PowerShell single-quoted literal. ASCII only, since PowerShell also
    takes the typographic quotes (U+2018 to U+201B) as quotes."""
    if not value.isascii():
        raise Refusal(f"refusing a non-ASCII value for the far half: {value!r}")
    return "'" + value.replace("'", "''") + "'"


def _remote_far(plan: Plan, cfg: dict[str, str], script_text: str, name: str,
                params: str, timeout: int, *, shell: str = "pwsh",
                log_path: Path | None = None) -> tuple[int | None, str, list[str]]:
    """Run a far half (ops/rust-harness-remote.ps1, ops/shim-tests-remote.sh)
    on the dev machine `cfg` names, with the batch as a git bundle on ssh's
    stdin (`<branch> ^<base>`: everything master lacks), so nothing is pushed.
    Returns the far half's exit code from its own verdict line (None when it
    gave none), the cause when it did not run, and its output lines."""
    role = cfg["role"]
    ref = f"refs/heads/{plan.branch}"
    if _out("rev-parse", ref) != plan.head:
        return None, f"{plan.branch} is not at {plan.head[:12]}", []
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "batch.bundle"
        try:
            git("bundle", "create", "-q", str(bundle), ref, f"^{plan.base_sha}")
            data = base64.b64encode(bundle.read_bytes()).decode("ascii")
        except (Refusal, OSError) as exc:
            return None, f"the batch bundle could not be made: {exc}", []
    # The verdict line carries a nonce no member code sees (it travels on
    # stdin, not argv), so a line cargo or pytest passes through cannot stand
    # in for it.
    nonce = secrets.token_hex(16)
    script = ("& {\n" + script_text + "\n} -Repo " + _ps_literal(cfg["repo"])
              + " -Base " + _ps_literal(plan.base_sha) + " -Head " + _ps_literal(plan.head)
              + " -Ref " + _ps_literal(ref) + params + " -Nonce " + _ps_literal(nonce))
    if shell == "bash":
        import shlex
        script = "set -- " + " ".join(shlex.quote(x) for x in
            (cfg["repo"], plan.base_sha, plan.head, ref, nonce, params)) + "\n" + script_text
    # stdin carries the script (its first line) and then the bundle; the
    # command line carries only RUST_CHECK_BOOTSTRAP, since an encoded
    # command past about 8 KB is refused through Windows' ssh.exe.
    data = base64.b64encode(script.encode("utf-8")).decode("ascii") + "\n" + data
    argv = [*RUST_CHECK_SSH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
            "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "ForwardX11=no", cfg["ssh"],
            f"pwsh -NoProfile -NonInteractive -EncodedCommand {RUST_CHECK_BOOTSTRAP}"]
    if shell == "bash":
        import shlex
        argv[-1] = "bash -c " + shlex.quote('IFS= read -r script; exec bash -c "$(printf %s "$script" | base64 -d)"')
    try:
        proc = _gate_ssh(argv, data, timeout, log_path)
    except Refusal as exc:
        return None, f"on the {role}: {exc}", []
    except subprocess.TimeoutExpired:
        lines = log_path.read_text(encoding="utf-8").splitlines() if log_path and log_path.exists() else []
        return None, f"the {role} gave no verdict in {timeout} s; check its owned run before retrying", lines
    lines = [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
    last = lines[-1].strip() if lines else ""
    # Only the far half's own last line decides; a run cut short has none.
    tag = f"{name} {nonce}: "
    said = last.removeprefix(tag) if last.startswith(tag) else None
    exited = re.fullmatch(r"(?:cargo|pytest|nextest) exit (-?\d+)", said) if said is not None else None
    if exited and proc.returncode == int(exited.group(1)):
        return int(exited.group(1)), "", lines
    if said is not None and said.startswith("not run: "):
        return None, f"on the {role}: {said.removeprefix('not run: ')[:300]}", lines
    how = "ssh" if proc.returncode == 255 else "the far half"
    return None, f"on the {role}: {how} exit {proc.returncode}: {last[:300] or 'no output'}", lines


def _gate_ssh(argv: list[str], data: str, timeout: int, log: Path | None) -> subprocess.CompletedProcess:
    """Stream progress and lock-holder messages while feeding a possibly large bundle."""
    import threading

    lines: list[str] = []
    errors: list[str] = []
    try:
        output = log.open("w", encoding="utf-8") if log else tempfile.TemporaryFile(mode="w", encoding="utf-8")
    except OSError as exc:
        raise Refusal(f"cannot open the gate log: {exc}") from None
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        output.close()
        raise Refusal(f"cannot start ssh: {exc}") from None

    def feed() -> None:
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except (OSError, ValueError):
            pass  # SSH's exit and final verdict decide whether input arrived.

    def drain() -> None:
        try:
            for line in proc.stdout:
                lines.append(line)
                output.write(line)
                output.flush()
                print(line, end="", flush=True)
        except OSError as exc:
            errors.append(str(exc))
        finally:
            output.close()
            proc.stdout.close()

    writer = threading.Thread(target=feed, daemon=True)
    reader = threading.Thread(target=drain, daemon=True)
    writer.start()
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        proc.kill()
        proc.wait()
        raise
    finally:
        writer.join(timeout=5)
        reader.join(timeout=5)
    if reader.is_alive() or writer.is_alive():
        raise Refusal("ssh's streams did not close; check its owned remote run before retrying")
    if errors:
        raise Refusal(f"could not retain the gate output: {errors[0]}")
    return subprocess.CompletedProcess(argv, proc.returncode, "".join(lines), "")


def _remote_rust_check(plan: Plan, cfg: dict[str, str]) -> tuple[str, str]:
    """RUST_CHECK on the remote dev machine: the batch goes there as a git
    bundle on ssh's stdin (`<branch> ^<base>`: everything master lacks), and
    RUST_CHECK_REMOTE_SCRIPT checks it out and runs cargo."""
    role = cfg["role"]
    if RUST_CHECK_REMOTE_SCRIPT is None:
        return "not run: ops/rust-check-remote.ps1 is missing from this checkout", ""
    ref = f"refs/heads/{plan.branch}"
    if _out("rev-parse", ref) != plan.head:
        return f"not run: {plan.branch} is not at {plan.head[:12]}", ""
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "batch.bundle"
        try:
            git("bundle", "create", "-q", str(bundle), ref, f"^{plan.base_sha}")
            data = base64.b64encode(bundle.read_bytes()).decode("ascii")
        except (Refusal, OSError) as exc:
            return f"not run: the batch bundle could not be made: {exc}", ""
    # The verdict line carries a nonce no member code sees (it travels on
    # stdin, not argv), so a line cargo passes through cannot stand in for it.
    nonce = secrets.token_hex(16)
    try:
        script = ("& {\n" + RUST_CHECK_REMOTE_SCRIPT + "\n} -Repo " + _ps_literal(cfg["repo"])
                  + " -Base " + _ps_literal(plan.base_sha) + " -Head " + _ps_literal(plan.head)
                  + " -Ref " + _ps_literal(ref) + " -Nonce " + _ps_literal(nonce))
    except Refusal as exc:
        return f"not run: {exc}", ""
    # stdin carries the script (its first line) and then the bundle; the
    # command line carries only RUST_CHECK_BOOTSTRAP, since an encoded
    # command past about 8 KB is refused through Windows' ssh.exe.
    data = base64.b64encode(script.encode("utf-8")).decode("ascii") + "\n" + data
    # Nothing is forwarded to the machine where a member's build script runs.
    argv = [*RUST_CHECK_SSH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
            "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "ForwardX11=no",
            cfg["ssh"],
            f"pwsh -NoProfile -NonInteractive -EncodedCommand {RUST_CHECK_BOOTSTRAP}"]
    print(f"  running `{RUST_CHECK_COMMAND}` in {RUST_CHECK_DIR}/ on {plan.head[:12]} "
          f"on the {role} (the batch goes there as a git bundle; nothing is pushed)")
    try:
        proc = _run(argv, input=data, check=False, timeout=RUST_CHECK_REMOTE_TIMEOUT)
    except Refusal as exc:
        return f"not run: on the {role}: {exc}", ""
    except subprocess.TimeoutExpired:
        return (f"not run: the {role} gave no verdict in {RUST_CHECK_REMOTE_TIMEOUT} s", "")
    lines = [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
    last = lines[-1].strip() if lines else ""
    # Only the far half's own last line decides; a run cut short has none.
    tag = f"rust-check {nonce}: "
    said = last.removeprefix(tag) if last.startswith(tag) else None
    exited = re.fullmatch(r"cargo exit (-?\d+)", said) if said is not None else None
    if exited:
        cargo = [line for line in lines if not line.startswith("rust-check")]
        return _cargo_verdict(int(exited.group(1)), cargo)
    if said is not None and said.startswith("not run: "):
        return f"not run: on the {role}: {said.removeprefix('not run: ')[:300]}", ""
    last = last[:300] or "no output"
    how = "ssh" if proc.returncode == 255 else "the far half"
    return f"not run: on the {role}: {how} exit {proc.returncode}: {last}", ""


def _rust_check(plan: Plan) -> tuple[str, str]:
    """Run RUST_CHECK on the batch head when the batch changes anything
    under rust/: on the remote dev machine when rust-check.remote names one,
    else here. Returns the plan's record and cargo's errors to print, and
    records in the plan where it ran."""
    if not _touches_rust(plan.base_sha, plan.head):
        return "not needed", ""
    root = Path(_out("rev-parse", "--show-toplevel"))
    if not (root / RUST_CHECK_DIR / "Cargo.toml").is_file():
        return f"not run: the batch head has no {RUST_CHECK_DIR}/Cargo.toml", ""
    cfg, cause = _rust_check_remote()
    if cause:
        plan.rust_check_where = "unconfigured"
        return f"not run: {cause}", ""
    if cfg:
        plan.rust_check_where = cfg["role"]
        return _remote_rust_check(plan, cfg)
    if os.environ.get(RUST_CHECK_WHERE_ENV) == "local":
        plan.rust_check_where = "local"
    print(f"  running `{RUST_CHECK_COMMAND}` in {RUST_CHECK_DIR}/ on {plan.head[:12]}")
    try:
        proc = _run(list(RUST_CHECK), check=False, cwd=root / RUST_CHECK_DIR,
                    env=_rust_check_env())
    except Refusal as exc:
        return f"not run: {exc}", ""
    return _cargo_verdict(proc.returncode, proc.stderr.strip().splitlines())


def _rust_check_machine(plan: Plan) -> str:
    """The machine the plan's check ran on, as the refusals name it."""
    if plan.rust_check_where in ("", "local", "unconfigured"):
        return "this machine"
    return f"the {plan.rust_check_where}"


RUST_CHECK_OK = ("passed", "not needed")


def _rust_check_refusal(plan: Plan) -> str:
    """Why `open` refuses this plan's Rust compile check result."""
    again = (f"switch back with `git switch --detach {REMOTE}/{BASE}`, remove the "
             "local branch and build again")
    if not plan.rust_check:
        return (f"{plan.branch} was built before `build` ran the Rust compile check "
                f"(`{RUST_CHECK_COMMAND}`); {again}")
    # cargo's own words cannot tell the batch from the machine (a lock file
    # two members left inconsistent reads like a network failure, and a
    # linker or memory failure prints "could not compile"), so master decides.
    if plan.rust_check.startswith("failed"):
        return (f"the batch does not compile at {plan.head[:12]}: `{RUST_CHECK_COMMAND}` "
                f"in {RUST_CHECK_DIR}/ {plan.rust_check}. Run the same command on "
                f"{REMOTE}/{BASE} ({plan.base_sha[:12]}) on {_rust_check_machine(plan)}: "
                f"if it fails there too, the cause is master or {_rust_check_machine(plan)} "
                "(cargo's network, disk, linker or "
                "memory; a proxy with a password in its URL is withheld), so fix that "
                f"and {again}. Otherwise it is a red batch and nothing is fixed inside "
                "it: bisect it (docs/dev/merge-batches.md, \"Validating it\", step 3), "
                f"drop the culprit with a comment and remove its label, then {again}")
    if plan.rust_check_where == "unconfigured":
        return (f"the Rust compile check did not run at {plan.head[:12]} "
                f"({plan.rust_check.removeprefix('not run: ')}); fix {RUST_CHECK_REMOTE.name} "
                f"or {RUST_CHECK_WHERE_ENV} (docs/dev/dev-machines.md, \"Batch compile "
                f"check\"), then {again}")
    if plan.rust_check_where not in ("", "local"):
        return (f"the Rust compile check did not run at {plan.head[:12]} "
                f"({plan.rust_check.removeprefix('not run: ')}), and the batch changes "
                f"{RUST_CHECK_DIR}/; fix that on the {plan.rust_check_where} (ssh to it, "
                f"its clone, its cargo; docs/dev/dev-machines.md), then {again}. "
                f"{RUST_CHECK_WHERE_ENV}=local runs the check on this host instead, "
                "only where this host may take the load")
    return (f"the Rust compile check did not run at {plan.head[:12]} "
            f"({plan.rust_check.removeprefix('not run: ')}), and the batch changes "
            f"{RUST_CHECK_DIR}/; fix that (rustup's cargo on PATH), then {again}")


def _cell(text: str) -> str:
    """A pull request title made safe for one Markdown table cell."""
    return " ".join(text.split()).replace("|", "\\|").replace("`", "'")


def _rust_check_cell(plan: Plan) -> str:
    """The body's Rust compile check result; `open` publishes only these two
    (a failure's cargo output, with this machine's paths, never reaches it)."""
    if plan.rust_check == "passed":
        where = {"": "", "local": " on the maintainer's host"}.get(
            plan.rust_check_where, f" on the {plan.rust_check_where}")
        return f"passed at `{plan.head}`{where}, run by `build`"
    if plan.rust_check == "not needed":
        return (f"not needed, the batch changes nothing under `{RUST_CHECK_DIR}/` "
                "but the generated census")
    return "<run by `build`>"


# ── After a drop (docs/dev/merge-batches.md, "After a drop") ──
# The maintainer's ruling of 17 November 2026: when a batch opened and went
# red in hosted CI, and its culprit is dropped, the batch built again without
# it skips the local suite and harness (the compile check still runs) and
# merges once `test` passes on its new head, carrying the closed batch's
# passes for the jobs still running, but only when the dropped pull request
# shares no file with any remaining member or with code every test shares.
AFTER_DROP_DOC = 'docs/dev/merge-batches.md, "After a drop"'
# What the batch's own generated commit rewrites (_regenerate_census and
# _regenerate_budget): not a member's change, so not compared.
AFTER_DROP_GENERATED = (CENSUS_JSON, CI_FINGERPRINTS)
AFTER_DROP_GENERATED_PREFIXES = (f"{CI_ACCEPT}/imported-",)
# Dependency manifests and lock files, by file name.
AFTER_DROP_MANIFESTS = frozenset({"pyproject.toml", "uv.lock", "setup.py", "setup.cfg",
                                  "Cargo.toml", "Cargo.lock", "rust-toolchain.toml",
                                  "rust-toolchain", "package.json", "package-lock.json",
                                  "pnpm-lock.yaml", "yarn.lock"})
AFTER_DROP_REQUIREMENTS_RE = re.compile(r"requirements[^/]*\.txt")
# Settings a test runner, linter or build tool reads by itself, with no
# workflow naming them (`cargo nextest` reads .config/nextest.toml).
AFTER_DROP_TOOL_CONFIG = frozenset({"pytest.ini", "tox.ini", ".coveragerc", "clippy.toml",
                                    ".clippy.toml", "rustfmt.toml", ".rustfmt.toml",
                                    "deny.toml", ".python-version", ".nvmrc", ".npmrc"})
AFTER_DROP_TOOL_CONFIG_RE = re.compile(r"(tsconfig[^/]*\.json|(vite|vitest|eslint|prettier)"
                                       r"\.config\.[^/]+|\.eslintrc[^/]*|\.prettierrc[^/]*)")
# The job that must pass on the new head itself, never carried, and the job
# whose success makes a skipped one stand (both in ci.yml, workflow "CI").
AFTER_DROP_TEST = "test"
AFTER_DROP_TEST_KEY = ("check", "CI", "test")
AFTER_DROP_SCOPE_KEY = ("check", "CI", "scope")


def _after_drop_generated(path: str) -> bool:
    return path in AFTER_DROP_GENERATED or path.startswith(AFTER_DROP_GENERATED_PREFIXES)


def _after_drop_shared(path: str) -> bool:
    """Code or data every test, build or job shares, so a change to it can
    break what a remaining member's passing runs never saw."""
    name = path.rsplit("/", 1)[-1]
    return (
        (path.startswith("tests/") and not re.fullmatch(r"tests/test_[^/]*\.py", path))
        or name == "conftest.py"
        or (path.startswith("rust/") and path.endswith(".py") and not name.startswith("test_"))
        # A Rust crate's tests/<folder>/ holds modules its test binaries
        # share (tests/common/mod.rs); a top-level tests/*.rs is one binary.
        or (path.startswith("rust/") and "/tests/" in path
            and "/" in path.split("/tests/", 1)[1])
        or path.startswith(".github/")
        or "/.cargo/" in f"/{path}" or "/.config/" in f"/{path}"
        or name in AFTER_DROP_TOOL_CONFIG or bool(AFTER_DROP_TOOL_CONFIG_RE.fullmatch(name))
        or path == "ops/integrate.py"
        or name in AFTER_DROP_MANIFESTS
        or bool(AFTER_DROP_REQUIREMENTS_RE.fullmatch(name))
        or (path.startswith("rust/ci/") and path.endswith(".json")))


def _after_drop_workflow_text(rev: str) -> str:
    """Every string value of every workflow file at `rev`, joined: a path a
    workflow names in a step, an input or a filter (a CI script, a
    Dockerfile) is an input of the jobs that run it. Comments are not
    values, so a path a comment mentions does not count. A file that does
    not parse counts whole."""
    names = _out("ls-tree", "-r", "--name-only", rev, "--", ".github/workflows").splitlines()
    texts = []

    def strings(value) -> None:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            for k, v in value.items():
                strings(k)
                strings(v)
        elif isinstance(value, list):
            for v in value:
                strings(v)

    for name in names:
        raw = git("show", f"{rev}:{name}", check=False).stdout
        try:
            strings(_yaml().safe_load(raw))
        except Exception:        # unparseable, or no YAML module: the whole text
            texts.append(raw)
    return "\n".join(texts)


def _after_drop_rust_module(path: str, rev: str) -> bool:
    """A top-level Rust test file another test binary of its crate compiles
    in with `mod name;` (tests/auth_fixture.rs): shared, though it sits
    where a test binary of its own would."""
    folder, _, name = path.rpartition("/")
    if not (path.startswith("rust/") and folder.endswith("/tests") and name.endswith(".rs")):
        return False
    declares = re.compile(rf"^\s*(pub(\([^)]*\))?\s+)?mod\s+{re.escape(name[:-3])}\s*;", re.M)
    for other in _out("ls-tree", "--name-only", rev, "--", f"{folder}/").splitlines():
        if other != path and other.endswith(".rs") and declares.search(
                git("show", f"{rev}:{other}", check=False).stdout):
            return True
    return False


def _after_drop_imported(path: str, rev: str) -> bool:
    """A test module another Python file imports (a shared fixture such as
    `from .test_seed import throwaway_pg`): shared, though test-named."""
    if path.endswith(".rs"):
        return _after_drop_rust_module(path, rev)
    name = path.rsplit("/", 1)[-1]
    if not (name.startswith("test_") and name.endswith(".py")):
        return False
    stem = name[:-3]
    name_re = re.escape(stem)
    importing = re.compile(
        rf"^\s*(from\s+[\w.]*\b{name_re}\s+import\b"          # from pkg.test_x import a
        rf"|import\s+([\w.]*\.)?{name_re}\b"                     # import pkg.test_x
        rf"|from\s+[\w.]+\s+import\s+(\([^)]*|[^\n]*)\b{name_re}\b)",  # from pkg import test_x
        re.M)
    # Candidates by plain text, then the import itself (git grep's regex
    # dialect differs between builds).
    hits = git("grep", "-l", "-F", stem, rev, "--", "*.py", check=False).stdout.splitlines()
    for hit in hits:
        other = hit.split(":", 1)[-1]
        if other == path:
            continue
        text = git("show", f"{rev}:{other}", check=False).stdout
        if importing.search(text):
            return True
    return False


def _after_drop_rebuilds(number: int) -> list[int]:
    """The pull requests of batches this checkout built `--after-drop number`
    and opened: a later rebuild's checks supersede the closed batch's."""
    root = Plan.path(f"{PREFIX}x").parent
    found = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (raw.get("after_drop") or {}).get("batch") == number and raw.get("pr"):
            found.append(raw["pr"])
    return sorted(found, reverse=True)


def _after_drop_closed(number: int, base_sha: str) -> Plan:
    """The closed batch `build --after-drop <number>` repeats: its plan, here,
    checked against GitHub. Refuses, naming the condition, otherwise."""
    why = f"`build --after-drop {number}` refused ({AFTER_DROP_DOC})"
    view = json.loads(gh("pr", "view", str(number), "--json",
                         "state,headRefName,headRefOid,baseRefName,isCrossRepository"))
    state, branch = view.get("state"), view.get("headRefName", "")
    if state != "CLOSED":
        fix = ("close it first, once its hosted CI went red" if state == "OPEN" else
               "nothing to repeat: build the next batch as usual")
        raise Refusal(f"{why}: #{number} is {str(state).lower()}, not closed unmerged; {fix}")
    if (not BATCH_RE.match(branch) or view.get("baseRefName") != BASE
            or view.get("isCrossRepository")):
        raise Refusal(f"{why}: #{number} comes from {branch or 'an unknown branch'} into "
                      f"{view.get('baseRefName')}, not an {PREFIX} batch into {BASE}; "
                      "name the batch pull request that went red")
    old = Plan.load(branch)
    if old is None:
        raise Refusal(f"{why}: this checkout holds no plan for {branch}; run it in the "
                      f"checkout that built and opened #{number}, which kept its plan. A "
                      "batch that went red before it opened has no hosted results to "
                      "carry: build it again the ordinary way")
    if old.pr != number or old.head != view.get("headRefOid"):
        raise Refusal(f"{why}: the plan for {branch} records #{old.pr or '?'} at "
                      f"{old.head[:12] or '?'}, but #{number} is at "
                      f"{str(view.get('headRefOid'))[:12]}; its hosted results are not the "
                      "plan's head's. Build it again the ordinary way")
    later = _after_drop_rebuilds(number)
    if later:
        raise Refusal(f"{why}: #{number} was already built again after a drop and opened "
                      f"as #{later[0]}, whose own hosted results are newer; name #{later[0]} "
                      f"(`build --after-drop {later[0]}`) once it is closed, so its checks "
                      "are the ones carried")
    if old.base_sha != base_sha:
        raise Refusal(f"{why}: {REMOTE}/{BASE} is {base_sha[:12]}, not the closed batch's "
                      f"base {old.base_sha[:12]}, so its hosted results tested another "
                      "master. Build it again the ordinary way")
    return old


def _after_drop_record(plan: Plan, old: Plan) -> dict:
    """The conditions on the batch `build` just merged, checked against the
    closed batch `old`; the record `open` and `merge` rely on and print."""
    why = f"`build --after-drop {old.pr}` refused ({AFTER_DROP_DOC})"
    again = f"{why}; nothing was kept, so build it again the ordinary way"
    if plan.dropped:
        raise Refusal(f"{again}: this build itself left out "
                      + ", ".join(f"#{d.member.number} ({d.reason})" for d in plan.dropped))
    before = {m.number: m for m in old.merged}
    now = {m.number: m for m in plan.merged}
    joined = sorted(set(now) - set(before))
    if joined:
        raise Refusal(f"{again}: " + ", ".join(f"#{n}" for n in joined)
                      + f" joined since #{old.pr}, whose hosted results never saw "
                      + ("it" if len(joined) == 1 else "them"))
    gone = sorted(set(before) - set(now))
    if not gone:
        raise Refusal(f"{again}: every member of #{old.pr} is in it again, so nothing was "
                      f"dropped; drop the culprit first (its `{plan.label}` label removed)")
    moved = [n for n in sorted(now) if now[n].sha != before[n].sha]
    if moved:
        raise Refusal(f"{again}: " + ", ".join(
            f"#{n} moved from {before[n].sha[:12]} to {now[n].sha[:12]}" for n in moved)
            + f" since #{old.pr}, whose hosted results tested the old head")
    files = {}
    for n in [*gone, *sorted(now)]:
        sha = before[n].sha
        if git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
            raise Refusal(f"{again}: #{n}'s head {sha[:12]} from #{old.pr} is not in this "
                          "checkout, so its files cannot be compared")
        files[n] = sorted(p for p in _changed(sha, plan.base_sha) if not _after_drop_generated(p))
    # The carried jobs ran on the closed head's tree: the new head may differ
    # from it only by the dropped members' files and the generated metadata.
    dropped_files = {p for n in gone for p in files[n]}
    if git("cat-file", "-e", f"{old.head}^{{commit}}", check=False).returncode != 0:
        raise Refusal(f"{again}: the closed batch's head {old.head[:12]} is not in this "
                      "checkout, so the two heads cannot be compared")
    other = sorted(p for p in _out("diff", "--name-only", "--no-renames", old.head,
                                   plan.head).splitlines()
                   if p not in dropped_files and not _after_drop_generated(p))
    if other:
        raise Refusal(f"{again}: the new head differs from #{old.pr}'s head "
                      f"{old.head[:12]} in " + ", ".join(other[:8])
                      + (", ..." if len(other) > 8 else "")
                      + ", beyond the dropped members' files and the generated metadata")
    workflows = _after_drop_workflow_text(old.head)
    for n in gone:
        shared = [p for p in files[n]
                  if _after_drop_shared(p) or p in workflows or _after_drop_imported(p, old.head)]
        if shared:
            raise Refusal(f"{again}: dropped #{n} changes code every test or job shares ("
                          + ", ".join(shared[:8]) + (", ..." if len(shared) > 8 else "")
                          + "), so the closed batch's passes may rest on it")
        for m in sorted(now):
            common = sorted(set(files[n]) & set(files[m]))
            if common:
                raise Refusal(f"{again}: dropped #{n} and #{m} both change "
                              + ", ".join(common[:8]) + (", ..." if len(common) > 8 else ""))
    return {"batch": old.pr, "branch": old.branch, "head": old.head, "base": old.base_sha,
            "dropped": [{"number": n, "sha": before[n].sha, "files": files[n]} for n in gone],
            "remaining": [{"number": n, "sha": now[n].sha, "files": len(files[n])}
                          for n in sorted(now)]}


def _after_drop_lines(record: dict) -> list[str]:
    """What the fast path relied on, in words: printed by `build` and `open`
    and kept in the batch body."""
    dropped = record["dropped"]
    return [
        f"- Closed batch: #{record['batch']} ({record['branch']}) at `{record['head']}`, "
        f"built on `{record['base']}`, which is this batch's base too.",
        *(f"- Dropped: #{d['number']} at `{d['sha']}`, changing {len(d['files'])} file(s): "
          + ", ".join(f"`{p}`" for p in d["files"][:20])
          + (", ..." if len(d["files"]) > 20 else "") for d in dropped),
        "- Remaining, each at the head the closed batch merged: "
        + ", ".join(f"#{r['number']} at `{r['sha'][:12]}` ({r['files']} file(s))"
                    for r in record["remaining"]) + ".",
        "- No file of the dropped pull request(s) is changed by a remaining member, and none "
        "is code every test or job shares (tests/ outside tests/test_*.py, any conftest.py, "
        "Python under rust/ outside test_*.py, .github/, ops/integrate.py, dependency "
        "manifests and lock files, JSON under rust/ci/); the batch's generated metadata is "
        "not compared, and the two heads differ only in the dropped files and that metadata.",
    ]


AFTER_DROP_SCOPE = AFTER_DROP_SCOPE_KEY[-1]
# Whether a job that failed on the closed head passes on the new one by a
# skip its workflow's scope job made (scope skips a lane once the dropped
# change that triggered it is gone), and whether one skipped or neutral
# there carries when absent here (condition 2 of the conductor's plan).
# False demands a success on the new head for both. The conductor ruled
# True on 17 November 2026, as for `test` on a Rust-only rebuild, after
# Codex asked for False; it is the maintainer's to flip.
AFTER_DROP_SKIP_PASSES = True
# The scope job of each workflow whose skips can count (`_after_drop_skipped`).
AFTER_DROP_WORKFLOW_SCOPES = {"CI": AFTER_DROP_SCOPE_KEY, "Rust shim": ("check", "Rust shim", "Scope")}


def _after_drop_standing(rollup: list[dict] | None) -> list[dict]:
    """Runs on the new head that finished with anything but a pass (red,
    cancelled, stale, timed out) and no later run of the same check
    succeeded since: a rerun still going does not replace them, since the
    fast path would carry it."""
    runs = rollup or []
    bad = [c for c in runs if not _unfinished(c) and _check_state(c) not in PASSING]

    def replaced(failure: dict) -> bool:
        # Only a later success clears it: a skipped rerun passed nothing.
        return any(other is not failure and _check_key(other) == _check_key(failure)
                   and not _unfinished(other) and _check_state(other) == "SUCCESS"
                   and _when(other)[0] > _when(failure)[0] for other in runs)

    return [c for c in bad if not replaced(c)]


def _after_drop_rulings(test: str | None, scope: str | None, summaries: list[tuple[str, list[str]]],
                        plan: Plan) -> tuple[str | None, list[str], list[str]]:
    """The maintainer's rulings the fast path rests on, in one place:
    (refusal, waiting, notes).

    `test` on the new head, finished as `test` ("" while it runs or is
    missing, which the caller waits for), given the `scope` job's state
    there (None missing, "" unfinished), must succeed there, or be skipped
    because `scope` succeeded and judged the head Rust-only (17 November
    2026): then only the compile check and the quick lanes ran fresh on it.

    A summary job (`summaries`: its name and the carried jobs it needs) that
    did not pass on the closed head is a job like any other: it must pass
    on the new head, so it waits for the carried jobs it needs, and the fast
    path saves no time while they run. Whether it may be judged by its
    inputs instead is the maintainer's call (asked 17 November 2026)."""
    # None requests the pre-open local-gate ruling. The compile verdict is
    # always required; the whole remaining off-PC gate is waived after a drop.
    if test is None:
        note = (f"not run: a batch built after a drop skips the suite, harness and shim tests ({AFTER_DROP_DOC})"
                if plan.after_drop else "")
        return None, [], [note] if note else []
    notes = [f"{name} did not pass on #{plan.after_drop.get('batch')}'s head, so it must pass "
             f"on this one; it needs {', '.join(inputs)}, which {'is' if len(inputs) == 1 else 'are'} "
             "carried but still running, so it cannot finish before "
             f"{'it' if len(inputs) == 1 else 'them'} and the fast path saves no time here"
             for name, inputs in summaries]
    if test in ("", "SUCCESS"):
        return None, [], notes
    if test == "SKIPPED" and scope == "SUCCESS":
        return None, [], notes
    if test == "SKIPPED" and scope == "":
        return None, [AFTER_DROP_SCOPE], notes
    why = (f"`{AFTER_DROP_TEST}` was skipped on #{plan.pr} at {plan.head[:12]}, but "
           f"`{AFTER_DROP_SCOPE}` did not succeed there ({(scope or 'missing').lower()}), so "
           "nothing says the head is Rust-only") if test == "SKIPPED" else (
           f"`{AFTER_DROP_TEST}` was {test.lower()} on #{plan.pr} at {plan.head[:12]}")
    return (f"{why}; a batch built after a drop merges only once `{AFTER_DROP_TEST}` passed "
            f"on its new head, or was skipped by a successful `{AFTER_DROP_SCOPE}` "
            f"({AFTER_DROP_DOC}). Rerun the job, or close #{plan.pr}, switch back with "
            f"`git switch --detach {REMOTE}/{BASE}`, remove the local branch and build it "
            "again without --after-drop"), [], notes


def _after_drop_skipped(check: dict, new: dict, plan: Plan) -> str | None:
    """The refusal, or None, for a job that failed (or never finished) on
    the closed head and finished other than SUCCESS on the new one. Only a
    skip can stand for a pass there (AFTER_DROP_SKIP_PASSES), and only one
    its workflow's scope job made: that scope job succeeded on the new head,
    and nothing in that workflow's run there failed, was cancelled or timed
    out, so GitHub did not skip it because a job it needs failed. A neutral
    conclusion is never a pass for such a job."""
    name, state = _check_name(check), _check_state(check)
    batch = plan.after_drop.get("batch")
    why = (f"{name} did not pass on #{batch}'s head and was {state.lower()} on "
           f"#{plan.pr}'s head {plan.head[:12]}")
    fix = (f"; a batch built after a drop needs it to succeed on the new head, or to be "
           f"skipped by its workflow's successful scope in a clean run ({AFTER_DROP_DOC}). "
           f"Rerun it, or close #{plan.pr} and build again without --after-drop")
    if state != "SKIPPED" or not AFTER_DROP_SKIP_PASSES:
        return f"{why}, which is not a pass for a job that failed before{fix}"
    workflow = check.get("workflowName") or ""
    scope_key = AFTER_DROP_WORKFLOW_SCOPES.get(workflow)
    scope = new.get(scope_key) if scope_key else None
    if scope is None or _unfinished(scope) or _check_state(scope) != "SUCCESS":
        seen = ("no scope job" if scope_key is None else "missing" if scope is None
                else "unfinished" if _unfinished(scope) else _check_state(scope).lower())
        return f"{why}, and its workflow {workflow or '?'}'s scope job did not succeed ({seen}){fix}"
    red = sorted(_check_name(c) for c in new.values()
                 if c.get("workflowName") == workflow and not _unfinished(c)
                 and (_check_state(c) in FAILED or _check_state(c) == CANCELLED))
    if red:
        return (f"{why}, and {workflow}'s run there has " + ", ".join(red)
                + " red or cancelled, so the skip may be GitHub's for a failed need" + fix)
    return None


def _after_drop_summaries(new: dict, eligible: set) -> list[tuple[str, list[str]]]:
    """(name, carried inputs) of each unfinished check on the new head that
    is not carried itself but needs, in its workflow, a job whose run is
    (a summary job such as `Parity / <os>` over the parity shards)."""
    try:
        workflows = _local_workflows()
    except Refusal:
        return []
    found = []
    for key, check in sorted(new.items(), key=lambda kv: _check_name(kv[1])):
        if key in eligible or not _unfinished(check) or key[0] != "check":
            continue
        jobs = (workflows.get(check.get("workflowName") or "") or {}).get("jobs", {})
        job = next((j for jid, j in jobs.items()
                    if _job_pattern(jid, j).fullmatch(_check_name(check))), None)
        if job is None:
            continue
        patterns = [_job_pattern(n, jobs[n]) for n in _needs(job) if n in jobs]
        inputs = sorted({_check_name(new[k]) for k in eligible
                         if k[0] == "check" and k[1] == check.get("workflowName")
                         and any(pat.fullmatch(_check_name(new[k])) for pat in patterns)})
        if inputs:
            found.append((_check_name(check), inputs))
    return found


def _after_drop_merge(plan: Plan, view: dict) -> tuple[list[str], list[str], list[str]]:
    """For a batch built `--after-drop`: (waiting, report, notes) on the new
    head. A job still running there is excused only when it concluded
    SUCCESS on the closed batch's head; `test` must pass on the new head
    itself. Finished failures are refused before this (_merge_checks)."""
    record = plan.after_drop
    old_view = json.loads(gh("pr", "view", str(record["batch"]), "--json",
                             "state,headRefOid,statusCheckRollup"))
    if old_view.get("state") != "CLOSED":
        raise Refusal(f"closed batch #{record['batch']} now reads "
                      f"{str(old_view.get('state')).lower()}, not closed; its results are "
                      "not a closed batch's. Wait for every check on this head and merge the "
                      "ordinary way")
    if old_view.get("headRefOid") != record["head"]:
        raise Refusal(f"closed batch #{record['batch']} now reads head "
                      f"{str(old_view.get('headRefOid'))[:12]}, not {record['head'][:12]} "
                      "the record names; its results are not the ones checked. Wait for "
                      "every check on this head and merge the ordinary way")
    old = {_check_key(c): c for c in _latest_checks(old_view.get("statusCheckRollup"))}
    new = {_check_key(c): c for c in _latest_checks(view.get("statusCheckRollup"))}
    runs = json.loads(gh("run", "list", "--commit", plan.head, "--limit", "200", "--json",
                         "status,name,databaseId") or "[]")
    settled = bool(runs) and all((r.get("status") or "").upper() == "COMPLETED" for r in runs)

    def url(check: dict | None) -> str:
        return (check or {}).get("detailsUrl") or (check or {}).get("targetUrl") or "no link"

    def failed_old(key: tuple) -> bool:
        """Did it fail, or never finish, on the closed head? (A job neutral
        or skipped there, or not there at all, did not fail before.)"""
        check = old.get(key)
        return check is not None and (_unfinished(check) or _check_state(check) not in PASSING)

    def passed_old(key: tuple) -> dict | None:
        check = old.get(key)
        if check is None or _unfinished(check):
            return None
        state = (check.get("conclusion") or check.get("state") or "").upper()
        return check if state == "SUCCESS" else None

    test = new.get(AFTER_DROP_TEST_KEY)
    test_state = "" if test is None or _unfinished(test) else _check_state(test)
    scope = new.get(AFTER_DROP_SCOPE_KEY)
    scope_state = None if scope is None else "" if _unfinished(scope) else _check_state(scope)
    eligible = {k for k, c in new.items()
                if _unfinished(c) and k != AFTER_DROP_TEST_KEY and passed_old(k)}
    refusal, waiting, notes = _after_drop_rulings(test_state, scope_state,
                                                  _after_drop_summaries(new, eligible), plan)
    if refusal:
        raise Refusal(refusal)
    carried, anew = [], []
    for key, check in sorted(new.items(), key=lambda kv: _check_name(kv[1])):
        name = _check_name(check)
        if not _unfinished(check):
            if (_check_state(check) != "SUCCESS" and key != AFTER_DROP_TEST_KEY
                    and failed_old(key)):
                refusal = _after_drop_skipped(check, new, plan)
                if refusal:
                    raise Refusal(refusal)
            anew.append(f"{name}: {(check.get('conclusion') or check.get('state') or '').lower()} "
                        f"on this head ({url(check)})")
            continue
        source = passed_old(key)
        if name in waiting:
            continue          # judged above (`scope` behind a skipped `test`)
        if name == AFTER_DROP_TEST or source is None:
            waiting.append(name)
        else:
            carried.append(f"{name}: carried, success on #{record['batch']}'s head "
                           f"{record['head'][:12]} ({url(source)})")
    reported = {_check_name(c) for c in new.values()}
    for name in REQUIRED_CHECKS:
        if name == AFTER_DROP_TEST:
            if AFTER_DROP_TEST_KEY not in new:      # CI's own `test`, not a namesake
                waiting.append(f"{name} (not reported yet)")
            continue
        if name in reported:
            continue
        key = next((k for k in old if k[-1] == name), None)
        source = passed_old(key) if key else None
        if name == AFTER_DROP_TEST or source is None or not settled:
            waiting.append(f"{name} (not reported yet)")
        else:
            carried.append(f"{name}: carried (absent here once every run on this head "
                           f"finished), success on #{record['batch']}'s head "
                           f"{record['head'][:12]} ({url(source)})")
    # A job that did not pass on the closed head must pass on this one, even
    # one not reported here yet.
    for key, check in sorted(old.items(), key=lambda kv: _check_name(kv[1])):
        name = _check_name(check)
        if key in new or name in REQUIRED_CHECKS:
            continue
        state = "" if _unfinished(check) else _check_state(check)
        if state == "SUCCESS" or (AFTER_DROP_SKIP_PASSES and state in PASSING):
            continue
        if not settled:
            waiting.append(f"{name} (not reported here yet; it did not pass on "
                           f"#{record['batch']})")
        else:
            raise Refusal(f"{name} did not pass on #{record['batch']}'s head "
                          f"({(state or 'unfinished').lower()}) and never ran on #{plan.pr}'s "
                          f"head {plan.head[:12]}, whose runs have all finished, so nothing "
                          "shows it passing; a batch built after a drop needs it to pass on "
                          f"its new head ({AFTER_DROP_DOC}). Rerun it on #{plan.pr}, or close "
                          "it and build again without --after-drop")
    report = [f"Fast path after a drop ({AFTER_DROP_DOC}): #{record['batch']} closed red at "
              f"{record['head'][:12]}; this batch, at {plan.head[:12]}, leaves out "
              + ", ".join(f"#{d['number']}" for d in record["dropped"]) + "."]
    report += ["Carried from the closed batch:", *(f"- {c}" for c in carried or ["none"])]
    report += ["Passed or finished on this head:", *(f"- {a}" for a in anew or ["none"])]
    report += ["Every check on this head now:",
               *(f"- {_check_name(c)}: {_check_state(c).lower() or 'running'}"
                 for c in sorted(new.values(), key=_check_name))]
    return waiting, report, notes


def body(plan: Plan, suite: str | None = None, harness: str | None = None,
         shim: str | None = None) -> str:
    lines = [
        f"Integration batch of {len(plan.merged)} reviewed pull requests, built by "
        f"`python ops/integrate.py build` from `{BASE}` at `{plan.base_sha}`. "
        f"Merge it with a merge commit, never squash, so every member becomes "
        f"reachable from `{BASE}` and GitHub marks it merged.",
        "",
        "## Members",
        "",
        "| PR | Head merged | Review evidence | CI on its head |",
        "| --- | --- | --- | --- |",
    ]
    for m in plan.merged:
        lines.append(f"| #{m.number} {_cell(m.title)} | `{m.sha}` | {m.url} "
                     f"({m.review.lower().replace('_', ' ')}) | {m.checks} |")
    if plan.dropped:
        lines += ["", "## Dropped", ""]
        lines += [f"- #{d.member.number}: {d.reason}" for d in plan.dropped]
    if plan.census_commit or plan.census_drift:
        lines += ["", "## Generated CI metadata", ""]
        if plan.census_commit:
            lines += [f"`{plan.census_commit}` refreshes derived producer-census/CI-budget metadata "
                      "for this head in the batch's one generated commit. Authored budgets and "
                      "reviewed acceptance reasons are preserved; no member code was edited."]
        if plan.census_drift:
            lines += [""] * bool(plan.census_commit) + [
                      "Trusted manual shapes withdrawn because a member changed "
                      "their source: " + ", ".join(f"`{p}`" for p in plan.census_drift)
                      + ". Review each construction and refresh its `MANUAL_SHA256` "
                      "fingerprint in a follow-up pull request (`rust/producer-census.md`)."]
    if plan.after_drop:
        lines += ["", "## After a drop", "",
                  f"Built with `build --after-drop {plan.after_drop['batch']}` "
                  f"({AFTER_DROP_DOC}): no local suite, harness or shim run, and `merge` waits "
                  "for `test` on this head, carrying the closed batch's other passes. "
                  "What it relied on, checked by the tool:", "",
                  *_after_drop_lines(plan.after_drop)]
    if plan.markers:
        lines += ["", "## Warning: conflict markers", "",
                  "These files carry conflict markers that master does not; "
                  "check them before merging: "
                  + ", ".join(f"`{f}`" for f in plan.markers)]
    lines += [
        "", "## Validation", "",
        f"- Local full suite: {suite or '<filled in by `open`>'}",
        f"- Rust harness Python tests (`{RUST_HARNESS_COMMAND}`, which the full "
        f"suite does not collect): {harness or '<filled in by `open`>'}",
        f"- Rust compile check (`{RUST_CHECK_COMMAND}` in `{RUST_CHECK_DIR}/`, which "
        f"checks all workspace targets): {_rust_check_cell(plan)}",
        f"- Shim unit tests (`{SHIM_TESTS_COMMAND}`): {shim or '<filled in by `open`>'}",
        "- CI: this pull request's run is the combined check of its members, "
        "and their first full one: a pull request from any branch but "
        "`integrate/*` runs only the fast lanes.",
        "- A red batch is bisected locally (master plus half the members); the "
        f"culprit is dropped with a comment and loses `{plan.label}`, and nothing "
        "is fixed inside the batch (the generated census commit is not a fix).",
    ]
    return "\n".join(lines) + "\n"


def _switch_back(restore: str, base_sha: str) -> None:
    git("switch", "-q", *([restore] if restore != base_sha else ["--detach", base_sha]))


def build(args: argparse.Namespace) -> int:
    restore, base_sha = _preflight()
    # First, so a closed batch that is in fact still open is named as such.
    closed = _after_drop_closed(args.after_drop, base_sha) if args.after_drop else None
    open_prs = _open_batches()
    if open_prs:
        raise Refusal("a batch is already open: " + ", ".join(
            f"#{n} ({name})" for n, name, _, _ in open_prs)
            + "; merge or close it before building the next one")
    members = ready(args.label, base_sha)
    waiting = [(m, _not_ready(m)) for m in members if _not_ready(m)]
    if len(members) - len(waiting) < MIN_MEMBERS:
        print(f"{len(members) - len(waiting)} of {len(members)} pull request(s) "
              f"carrying `{args.label}` are ready (not a draft, CI on the head not "
              f"failing); a batch needs {MIN_MEMBERS}.")
        for m, reason in waiting:
            print(f"  #{m.number} {reason}")
        if waiting:
            # Report only: what cannot be judged is said, and this still
            # returns 0 as it did before the report existed.
            try:
                _fetch_listed(members)
                freeze = _freeze_module(base_sha, members)
                stub = Plan(branch=f"{PREFIX}{args.date}-batch-0", base_sha=base_sha, label=args.label)
                prepass = _freeze_prepass(stub, freeze, members)
            except Refusal as error:
                print(f"  (what would refuse them once ready could not be judged: {error})")
            else:
                _report_once_ready(base_sha, waiting, freeze, prepass)
        return 0
    n = max(_used_numbers(args.date), default=0) + 1
    plan = Plan(branch=f"{PREFIX}{args.date}-batch-{n}", base_sha=base_sha,
                label=args.label)
    print(f"{'[dry run] ' if args.dry_run else ''}building {plan.branch} "
          f"from {REMOTE}/{BASE} {base_sha[:12]}")
    if args.dry_run:
        git("switch", "-q", "--detach", base_sha)
    else:
        git("switch", "-q", "-c", plan.branch, base_sha)
    done = False
    judged = None
    try:
        judged = _merge_batch(plan, members)
        plan.markers = _new_markers(base_sha)
        plan.head = _out("rev-parse", "HEAD")
        if closed is not None:
            plan.after_drop = _after_drop_record(plan, closed)
        done = True
    finally:
        if git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            git("merge", "--abort", check=False)
        # Stay on the branch only when `open` will publish it; otherwise it
        # was created by this run, never pushed, and goes.
        if not (done and not args.dry_run and len(plan.merged) >= MIN_MEMBERS):
            _switch_back(restore, base_sha)
            if not args.dry_run:
                git("branch", "-q", "-D", plan.branch)

    for d in plan.dropped:
        print(f"  left out #{d.member.number}: {d.reason}")
    if waiting and judged:
        _report_once_ready(base_sha, waiting, *judged)
    if len(plan.merged) < MIN_MEMBERS:
        merged = ", ".join(f"#{m.number}" for m in plan.merged) or "none"
        print(f"{merged} merged cleanly; no batch built.")
        # No `open` follows, so the collisions are reported now.
        _report_drops(plan, None, args.dry_run)
        return 0
    if args.dry_run:
        cfg, cause = _rust_check_remote()
        on = (f", on the {cfg['role']}" if cfg else
              f", but it would not run: {cause}" if cause else "")
        rust = (f"would run `{RUST_CHECK_COMMAND}` in {RUST_CHECK_DIR}/ on the batch head{on}"
                if _touches_rust(base_sha, plan.head) else
                f"no Rust compile check: the batch changes nothing under {RUST_CHECK_DIR}/ "
                "but the census")
        print(f"\n{rust}\nwould build {plan.branch}, then after the suite open:\n"
              f"  {plan.title}\n\n{body(plan)}")
        _report_drops(plan, None, True)
        return 0
    # After the census, on the branch it leaves: a failure drops nothing and
    # keeps the branch for bisecting, and `open` refuses the plan. Saved
    # first, so a check that is interrupted still leaves a plan to refuse.
    plan.rust_check = "not run: `build` stopped during the check"
    plan.save()
    plan.rust_check, errors = _rust_check(plan)
    plan.save()
    if plan.rust_check not in RUST_CHECK_OK:
        if errors:
            print(errors, file=sys.stderr)
        print(f"\nbuilt {plan.branch} at {plan.head[:12]}, and nothing is pushed.\n"
              f"integrate: {_rust_check_refusal(plan)}. Skip the suite on this head.",
              file=sys.stderr)
        return 1
    rust = (f"passed at {plan.head[:8]}"
            + (f" on the {plan.rust_check_where}" if plan.rust_check_where not in ("", "local")
               else "") if plan.rust_check == "passed" else
            f"not needed, nothing under {RUST_CHECK_DIR}/ but the census changed")
    print(f"\nRust compile check (`{RUST_CHECK_COMMAND}` in {RUST_CHECK_DIR}/): {rust}.")
    if plan.after_drop:
        print(f"built {plan.branch} at {plan.head[:12]} after a drop, relying on:\n"
              + "\n".join(_after_drop_lines(plan.after_drop))
              + f"\nNo local suite, harness or shim run is needed ({AFTER_DROP_DOC}). Next, on "
              "this checkout: python ops/integrate.py open; then `merge` once `test` "
              "passes on the new head.")
        return 0
    print(f"built {plan.branch} at {plan.head[:12]}; nothing is pushed, so no CI "
          "has started. Next, on this checkout:\n"
          f"  1. {SUITE_COMMAND}   (the full suite on the remote machine, the batch "
          "head carried as a bundle; writes ~/.pseudolife-mcp/suite-results/*.json;\n"
          f"     {REAL_CLIENT_NOTE}; {SUITE_LOCAL_NOTE})\n"
          f"  2. python ops/integrate.py harness   ({RUST_HARNESS_COMMAND} on the Windows dev VM)\n"
          f"  3. python ops/integrate.py shim-tests   ({SHIM_TESTS_COMMAND} on the second Linux dev box)\n"
          "  4. python ops/integrate.py open --suite <suite.json> --harness <harness.json> --shim-tests <shim.json>\n"
          "A red suite: bisect locally, drop the culprit, switch back with "
          f"`git switch --detach {REMOTE}/{BASE}`, remove the local branch and build "
          "again.")
    return 0


# The local gate runs the tests that need the real native client, as the
# hosted `test` lane does: a batch whose local suite used their stand-in
# passed and then failed 18 of them hosted (#1634), and "this batch cannot
# break them" is not something a tool can judge, so there is no escape
# (conductor ruling, 16 November 2026). ops/remote-suite.ps1 turns the
# build on by itself on an integrate/* branch and records it in the result.
SUITE_COMMAND = "python ops/integrate.py suite"
REAL_CLIENT_NOTE = ("on this integrate/* branch it also builds the native client there "
                    "(cargo build --locked -p pseudolife-stdio, the dev profile, as the "
                    "hosted test lane does; needs 8 GB free there) and runs the "
                    "real-client tests, which `open` requires")
SUITE_LOCAL_NOTE = ("only while this PC is declared available may it run here instead: "
                    "PSEUDOLIFE_SUITE_WHERE=local pwsh ops/remote-suite.ps1")
# The batch head is not on origin before `open`, so ops/remote-suite.ps1
# carries it to the remote machine as a git bundle: nothing is pushed for
# the suite.
REAL_CLIENT_SAID = "real client: yes"


def _real_client_refusal(why: str, head: str) -> Refusal:
    return Refusal(f"{why}, so the tests that need the native client used their "
                   "stand-in or skipped, which the hosted `test` lane does not "
                   f"(#1634); rerun on this checkout: `{SUITE_COMMAND}` (it builds "
                   f"the client from {head[:8]}; a run by hand needs "
                   "PSEUDOLIFE_SUITE_NATIVE_CLIENT unset or 1 on this integrate/* "
                   "branch), then `open` with its new result. A build that cannot run "
                   "(under 8 GB free, cargo failing) is fixed first: the batch does "
                   "not open without it")


def _suite_results_dir() -> Path:
    return Path.home() / ".pseudolife-mcp" / "suite-results"


def _run_remote_suite(head: str) -> int:
    """The batch's full suite on the remote machine (ops/remote-suite.ps1,
    this checkout's own), with the real native client; its output streams."""
    env = {**os.environ, "PSEUDOLIFE_SUITE_WHERE": "remote",
           "PSEUDOLIFE_SUITE_NATIVE_CLIENT": "1"}
    script = Path(_out("rev-parse", "--show-toplevel")) / "ops" / "remote-suite.ps1"
    try:
        return subprocess.run(["pwsh", "-NoProfile", "-File", str(script)], env=env).returncode
    except FileNotFoundError:
        raise Refusal("pwsh is not on PATH; ops/remote-suite.ps1 needs PowerShell 7") from None


def suite(args: argparse.Namespace) -> int:
    """Run the built batch's full suite on the remote machine (its head
    travels there as a bundle; nothing is pushed) and name the result for
    `open --suite`."""
    if _out("status", "--porcelain", "--untracked-files=no"):
        raise Refusal("the checkout has uncommitted changes to tracked files; "
                      "nothing is fixed inside a batch")
    branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    plan = Plan.load(branch) if BATCH_RE.match(branch) else None
    if plan is None:
        raise Refusal(f"this checkout is not on a batch `build` left here "
                      f"({branch or 'detached HEAD'}); run `python ops/integrate.py "
                      "build` first")
    head = _out("rev-parse", "HEAD")
    if head != plan.head:
        raise Refusal(f"{branch} moved from {plan.head[:12]} to {head[:12]} after "
                      "build; nothing is fixed inside a batch, so rebuild it")
    # A head that did not pass the compile check never opens, so a full suite
    # on it would only spend the remote machine's half hour.
    if plan.rust_check not in RUST_CHECK_OK:
        raise Refusal(f"{_rust_check_refusal(plan)}. The full suite does not run on a "
                      "head that did not pass the Rust compile check")
    started = time.time()
    print(f"integrate: running {head[:12]}'s full suite on the remote machine, with the "
          "real native client; the batch head travels as a bundle, nothing is pushed",
          flush=True)
    code = _run_remote_suite(head)
    results = sorted((p for p in _suite_results_dir().glob(f"*-remote-{head[:8]}.json")
                      if p.stat().st_mtime >= started - 1), key=lambda p: p.stat().st_mtime)
    if not results:
        print(f"integrate: the suite wrote no result for {head[:12]} (exit {code}); "
              "nothing to give `open`", file=sys.stderr)
        return code or 1
    print(f"integrate: suite exit {code}; its result is {results[-1]}")
    if code == 0:
        print(f"next: python ops/integrate.py open --suite {results[-1]} "
              f"--harness <harness.json> --shim-tests <shim-tests.json>")
    return code


def _suite_line(args: argparse.Namespace, head: str) -> str:
    """The "Local full suite" line, from a passing result for exactly `head`
    that ran the real native client built from it."""
    if args.suite_note:
        if head[:8] not in args.suite_note:
            raise Refusal(f"a --suite-note must name the batch head it ran on "
                          f"(at least `{head[:8]}`) with its machine and result")
        said = args.suite_note.lower()
        if REAL_CLIENT_SAID not in said or "given by the caller" in said:
            raise _real_client_refusal(
                f"a --suite-note must say `{REAL_CLIENT_SAID}` for a client the run "
                "built (the run's own `wsl-suite: real client:` line), and this one "
                "does not", head)
        return f"{args.suite_note} (stated by the operator, not checked)"
    if not args.suite:
        raise Refusal("give the local full suite's result for this head: "
                      "`--suite <~/.pseudolife-mcp/suite-results/*.json>` from "
                      "`ops/remote-suite.ps1`, or `--suite-note \"<machine role, "
                      "commit, result>\"` for a run that wrote no JSON")
    try:
        # utf-8-sig: Windows PowerShell 5 writes the result with a BOM.
        result = json.loads(Path(args.suite).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise Refusal(f"cannot read the suite result {args.suite}: {exc}") from None
    if result.get("commit") != head:
        raise Refusal(f"the suite result tested {str(result.get('commit'))[:12]}, "
                      f"not this batch head {head[:12]}; run the suite on this "
                      "checkout")
    if "args" not in result:
        raise Refusal("the suite result does not record its pytest arguments "
                      "(written by an older ops/remote-suite.ps1), so it cannot "
                      "show a full run; rerun it, or give `--suite-note`")
    if result["args"]:
        raise Refusal(f"the suite result is a selected run ({result['args']}), not "
                      "the full suite; run `ops/remote-suite.ps1` with no pytest "
                      "arguments")
    if result.get("exit_code") != 0:
        raise Refusal(f"the suite failed on this head (exit {result.get('exit_code')}: "
                      f"{result.get('summary')}); bisect locally and drop the culprit")
    real = result.get("real_client")
    if real is not True:
        raise _real_client_refusal(
            "the suite result does not record the real native client"
            if real is None else "the suite result ran without the real native client", head)
    if result.get("real_client_commit") != head:
        raise _real_client_refusal(
            f"the suite result's native client was built from "
            f"{str(result.get('real_client_commit'))[:12]}, not this batch head "
            f"{head[:12]}", head)
    return (f"passed at `{head}` on {result.get('machine', 'an unnamed machine')}, "
            f"with the real native client built from it: {result.get('summary', '')}")


def _gate_result_line(path: str | None, head: str, kind: str) -> str:
    """Accept a completed run of the entire required selection at this head."""
    harness = kind == HARNESS_RESULT_KIND
    name = "harness" if harness else "shim-tests"
    role = "Windows dev VM" if harness else "second Linux dev box"
    command = RUST_HARNESS_COMMAND if harness else SHIM_TESTS_COMMAND
    selection = list(RUST_HARNESS if harness else SHIM_TESTS_ARGS)
    again = f"run `python ops/integrate.py {name}` and give `open --{name} <result.json>` for {head[:8]}"
    if not path:
        raise Refusal(f"give the {name} result: {again}")
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise Refusal(f"cannot read the {name} result: {exc}; {again}") from None
    if not isinstance(result, dict) or result.get("kind") != kind:
        raise Refusal(f"this is not a result `{name}` wrote; {again}")
    if result.get("commit") != head:
        raise Refusal(f"the {name} result tested {result.get('commit')}, not this batch head; {again}")
    if result.get("command") != command or result.get("selection") != selection:
        raise Refusal(f"the {name} result ran another selection; {again}")
    if result.get("machine") != role:
        raise Refusal(f"the {name} result names no known dev machine role for this gate; expected {role}; {again}")
    try:
        started, ended = [dt.datetime.fromisoformat(result[key]) for key in ("started", "ended")]
        if started.tzinfo is None or ended.tzinfo is None or ended < started:
            raise ValueError("invalid order or missing timezone")
    except (KeyError, TypeError, ValueError):
        raise Refusal(f"the {name} result lacks valid run timestamps; {again}") from None
    if result.get("exit_code") is None:
        raise Refusal(f"the {name} did not run ({result.get('not_run')}); {again}")
    summary = result.get("summary")
    passed = re.search(r"\b([1-9]\d*) passed\b", summary) if isinstance(summary, str) else None
    if (type(result.get("exit_code")) is not int or result["exit_code"] != 0 or not passed
            or result.get("not_run") or re.search(r"\b0*[1-9]\d*\s+(failed|errors?)\b", summary)):
        raise Refusal(f"the {name} failed at {head[:12]} (exit {result.get('exit_code')}: {summary}); bisect and drop the culprit")
    return f"passed at `{head}` on the {role}: {summary}"


def _refuse_red_members(plan: Plan) -> None:
    """Refuse when a merged member's own CI turned red after `build`: a
    known failure the batch would carry, and recheck snapshot admission.
    A moved head with required snapshot evidence needs a fresh build."""
    for m in plan.merged:
        if m.snapshot_required is None and _has_snapshot_selector(plan.base_sha):
            if _base_has_snapshot_admission(plan.base_sha):
                raise Refusal("the plan lacks the base builder's snapshot admission; rebuild the batch")
            # The batch can carry the first admission-aware opener while
            # its master's builder still writes plans without these fields.
            m.snapshot_required, why = _snapshot_requirement(m, plan.base_sha)
            if m.snapshot_required is None:
                raise Refusal(f"#{m.number}: {why}; nothing was opened, fix the cause and retry open")
        view = json.loads(gh("pr", "view", str(m.number), "--json",
                             "headRefOid,statusCheckRollup"))
        if view["headRefOid"] != m.sha:
            if m.snapshot_required:
                raise Refusal(f"#{m.number}'s head moved since snapshot admission; rebuild the batch")
            continue
        checks, why = _checks_why(view.get("statusCheckRollup"))
        if checks == "unverified":
            raise Refusal(f"#{m.number} on {m.sha[:12]}: {why}, so a timeout or a "
                          "failure is not ruled out; nothing was opened: run `open` "
                          "again once that read succeeds")
        if checks == "failing":
            raise Refusal(f"#{m.number}'s own CI is now failing on {m.sha[:12]}, a known "
                          "failure this batch would carry. Drop it: switch back with "
                          f"`git switch --detach {REMOTE}/{BASE}`, remove the local "
                          "branch and build again (it sits out while its head is red)")
        state, why = _snapshot_state(m.sha, view.get("statusCheckRollup"),
                                     m.snapshot_required if m.snapshot_required is not None else False)
        m.snapshot, m.snapshot_why = state, why
        if state in ("failing", "pending", "unverified"):
            raise Refusal(f"#{m.number}: {why}; nothing was opened, retry once the exact replay passes")


def _built_batch() -> tuple[str, Plan, str]:
    """The batch `build` left on this checkout: (branch, plan, head), or a
    refusal naming what to do."""
    if _out("status", "--porcelain", "--untracked-files=no"):
        raise Refusal("the checkout has uncommitted changes to tracked files; "
                      "nothing is fixed inside a batch")
    branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    plan = Plan.load(branch) if BATCH_RE.match(branch) else None
    if plan is None:
        raise Refusal(f"this checkout is not on a batch `build` left here "
                      f"({branch or 'detached HEAD'}); run `python ops/integrate.py "
                      "build` first")
    head = _out("rev-parse", "HEAD")
    if head != plan.head:
        raise Refusal(f"{branch} moved from {plan.head[:12]} to {head[:12]} after "
                      "build; nothing is fixed inside a batch, so rebuild it")
    return branch, plan, head

def harness(args: argparse.Namespace) -> int:
    return _run_gate(args, HARNESS_RESULT_KIND)


def shim_tests(args: argparse.Namespace) -> int:
    return _run_gate(args, "shim-tests")


def _run_gate(args: argparse.Namespace, kind: str) -> int:
    branch, plan, head = _built_batch()
    if plan.rust_check not in RUST_CHECK_OK:
        raise Refusal(_rust_check_refusal(plan))
    harness = kind == HARNESS_RESULT_KIND
    name = "harness" if harness else "shim-tests"
    config = RUST_CHECK_REMOTE if harness else SHIM_TESTS_REMOTE
    role = "Windows dev VM" if harness else "second Linux dev box"
    command = RUST_HARNESS_COMMAND if harness else SHIM_TESTS_COMMAND
    selection = list(RUST_HARNESS if harness else SHIM_TESTS_ARGS)
    cfg, cause = _remote_config(config, (role,), role)
    if not cfg:
        raise Refusal(f"{cause or 'no dev machine configured'}; configure {config.name} (docs/dev/dev-machines.md); no local fallback")
    script = RUST_HARNESS_REMOTE_SCRIPT if harness else SHIM_TESTS_REMOTE_SCRIPT
    if not script or (harness and not DATABASE_LOCK_SCRIPT):
        raise Refusal(f"the {name} far half or its lock helper is missing from this checkout")
    if harness:
        # Ship the helper already read by this controller, not one loaded
        # from the remote checkout after it changes to the batch head.
        helper = base64.b64encode(DATABASE_LOCK_SCRIPT.encode("utf-8")).decode("ascii")
        script = script.replace('# LOAD_DATABASE_LOCK',
            f". ([scriptblock]::Create([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{helper}'))))")
        params = " -PytestArgs @(" + ", ".join(_ps_literal(p) for p in selection) + ")"
        params += f" -LockWaitSeconds {args.lock_wait_seconds}"
    else:
        params = str(args.lock_wait_seconds)
    started = dt.datetime.now().astimezone()
    SUITE_RESULTS.mkdir(parents=True, exist_ok=True)
    stem = SUITE_RESULTS / f"{started:%Y%m%d-%H%M%S-%f}-{name}-{head[:8]}"
    log = stem.with_suffix(".log")
    print(f"integrate: {name} at {head[:12]} on the {role}; bundle only, nothing pushed", flush=True)
    print(f"log {log}", flush=True)
    code, cause, lines = _remote_far(plan, cfg, script, kind, params,
                                   RUST_HARNESS_REMOTE_TIMEOUT, shell="pwsh" if harness else "bash", log_path=log)
    summaries = ([m.group(1) for m in map(PYTEST_SUMMARY_RE.match, lines) if m] if harness else
                 [line.strip() for line in lines if "Summary [" in line])
    summary = summaries[-1] if summaries else ""
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = {"kind": kind, "machine": role, "commit": head, "branch": branch,
              "command": command, "selection": selection, "exit_code": code,
              "not_run": cause or None, "summary": summary, "started": started.isoformat(),
              "ended": dt.datetime.now().astimezone().isoformat(), "log": str(log)}
    out = stem.with_suffix(".json")
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"{name}: {summary or cause or 'no summary'}\nresult {out}\nlog {log}", flush=True)
    return code if code is not None else 3


def open_(args: argparse.Namespace) -> int:
    if _out("status", "--porcelain", "--untracked-files=no"):
        raise Refusal("the checkout has uncommitted changes to tracked files; "
                      "nothing is fixed inside a batch")
    branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    plan = Plan.load(branch) if BATCH_RE.match(branch) else None
    if plan is None:
        raise Refusal(f"this checkout is not on a batch `build` left here "
                      f"({branch or 'detached HEAD'}); run `python ops/integrate.py "
                      "build` first")
    head = _out("rev-parse", "HEAD")
    if head != plan.head:
        raise Refusal(f"{branch} moved from {plan.head[:12]} to {head[:12]} after "
                      "build; nothing is fixed inside a batch, so rebuild it")
    frozen = _batch_freeze_refusal(plan)
    if frozen:
        raise Refusal(frozen)
    open_prs = _open_batches()
    others = [(n, name) for n, name, _, _ in open_prs if name != plan.branch]
    if others:
        raise Refusal("a batch is already open: " + ", ".join(
            f"#{n} ({name})" for n, name in others))
    # A rerun after a failure past the push resumes: this branch's own pull
    # request is the one to finish, not a second batch, but only while it
    # still points at the head the suite passed on. Once it is open, a moved
    # master is its CI's concern, so the base check applies only before then.
    own = [(n, u, sha) for n, name, u, sha in open_prs if name == plan.branch]
    # The compile check is judged before the suite result, so a batch that
    # did not pass it is refused for that (`suite` does not run on it), not
    # for a missing suite result. Before the push only: a batch already open
    # resumes, as one opened from a plan that predates the check must.
    if not own and plan.rust_check not in RUST_CHECK_OK:
        raise Refusal(_rust_check_refusal(plan))
    _, _, waived = _after_drop_rulings(None, None, [], plan)
    skipped = waived[0] if waived else ""
    suite = skipped if skipped else _suite_line(args, head)
    harness = skipped if skipped else _gate_result_line(args.harness, head, HARNESS_RESULT_KIND)
    shim = skipped if skipped else _gate_result_line(args.shim_tests, head, "shim-tests")
    if plan.after_drop:
        print(f"integrate: opening {plan.branch} after a drop, relying on:\n"
              + "\n".join(_after_drop_lines(plan.after_drop)))
    text = body(plan, suite, harness, shim)
    url = None
    if own:
        number, url, sha = own[0]
        if sha != plan.head:
            raise Refusal(f"#{number} is open from {plan.branch} at {sha[:12]}, not "
                          f"the built head {plan.head[:12]}; check it by hand")
    else:
        git("fetch", "-q", REMOTE, BASE)
        if _out("rev-parse", f"{REMOTE}/{BASE}") != plan.base_sha:
            raise Refusal(f"{REMOTE}/{BASE} moved since {branch} was built; switch "
                          f"back with `git switch --detach {REMOTE}/{BASE}`, remove "
                          "the local branch and build again")
        # `build` regenerates the census; this catches a plan built without it.
        if (_census_inputs(plan.base_sha) is not None
                and _census_ci_fails(Path(_out("rev-parse", "--show-toplevel")), plan)):
            raise Refusal(f"CI's producer census check would fail on {plan.head[:12]}: "
                          "the census is stale on the batch head. Switch back with "
                          f"`git switch --detach {REMOTE}/{BASE}`, remove the local "
                          "branch and build again, which regenerates it")
        _refuse_red_members(plan)
    if url is None:
        try:
            git("push", "-q", "-u", REMOTE, plan.branch)
        except Refusal as exc:
            raise Refusal(f"{exc}\nNothing was pushed or commented on; fix the push "
                          "and rerun `open`.") from None
        try:
            url = gh("pr", "create", "--base", BASE, "--head", plan.branch,
                     "--title", plan.title, "--body-file", "-", input=text).strip()
        except Refusal as exc:
            raise Refusal(
                f"{exc}\n{plan.branch} is pushed, and no dropped member was "
                "commented on; rerun `open` with the same arguments once gh "
                "works (it finds the pull request if this one was opened).") from None
        print(f"opened {url}: {plan.title}")
    else:
        print(f"resuming {url}: {plan.title}")
    # `merge` reads the plan to know which pull request is the batch.
    plan.pr = int(url.rstrip("/").rsplit("/", 1)[-1])
    plan.save()
    try:
        _report_drops(plan, url, False, persist=True)
    except Refusal as exc:
        raise Refusal(f"{exc}\nThe batch pull request is open at {url}; rerun "
                      "`open` with the same arguments to finish reporting the "
                      "dropped members.") from None
    print(f"Once its CI is green and it has its review: python ops/integrate.py "
          f"merge   (on this checkout; it merges #{plan.pr} and nothing else)")
    return 0


def _report_drops(plan: Plan, url: str | None, dry_run: bool, *,
                  persist: bool = False) -> None:
    """Comment on each conflicting member and remove its label. With
    `persist`, each step done is saved to the plan, so a rerun of `open`
    after a failure here repeats nothing."""
    for d in plan.dropped:
        if not d.comment or (d.commented and d.unlabelled):
            continue
        if url or dry_run:
            where = f"Left out of integration batch `{plan.branch}`" + (
                f" ({url})" if url else "")
        else:
            where = "Could not join an integration batch"
        text = (f"{where}: merging this head (`{d.member.sha}`) "
                f"{d.reason}. `ops/integrate.py` removed the `{plan.label}` label; "
                "nothing was changed on this branch.")
        if dry_run:
            print(f"would comment on #{d.member.number} and remove `{plan.label}`:"
                  f"\n  {text}")
            continue
        # An author may push a fix and relabel while the batch suite runs;
        # the drop is about the head that collided, so a moved head keeps
        # its label and gets no comment.
        current = json.loads(gh("pr", "view", str(d.member.number), "--json",
                                "headRefOid"))["headRefOid"]
        if current != d.member.sha:
            print(f"#{d.member.number} moved to {current[:12]} since the build; "
                  f"left its `{plan.label}` label and posted nothing")
            d.commented = d.unlabelled = True
            if persist:
                plan.save()
            continue
        if not d.commented:
            gh("pr", "comment", str(d.member.number), "--body-file", "-", input=text)
            d.commented = True
            if persist:
                plan.save()
        gh("pr", "edit", str(d.member.number), "--remove-label", plan.label)
        d.unlabelled = True
        if persist:
            plan.save()


def _merge_plan() -> Plan:
    """The plan of the batch `open` published from this checkout."""
    branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    plan = Plan.load(branch) if branch else None
    if plan is None:
        raise Refusal(f"this checkout holds no batch plan ({branch or 'detached HEAD'}); "
                      "run `merge` on the checkout where `build` and `open` ran, which "
                      "stays on the batch branch. A batch opened before `open` kept its "
                      "plan has none: the maintainer merges it by hand after the checks "
                      "in docs/dev/merge-batches.md (\"Merging it\"), or close it and "
                      "build again. A pull request from any other branch is never "
                      f"merged directly: label it `{LABEL}` and let a batch carry it")
    if not branch.startswith(PREFIX) or not plan.branch.startswith(PREFIX):
        raise Refusal(f"{branch} is not an `{PREFIX}` batch branch; only a batch pull "
                      "request merges. A pull request from any other branch is never "
                      f"merged directly: label it `{plan.label}` and let a batch carry it")
    if plan.branch != branch:
        raise Refusal(f"the plan found for {branch} is for {plan.branch}; nothing was "
                      "merged. Run `merge` on the checkout where that batch was built "
                      "and opened")
    if not plan.pr:
        raise Refusal(f"{plan.branch} was built but not published; run `python "
                      "ops/integrate.py open` first, then wait for its CI")
    return plan


def _check_key(check: dict) -> tuple:
    """What makes two rollup entries runs of the same check: a CheckRun's
    workflow and name, a StatusContext's context."""
    if check.get("__typename") == "StatusContext" or "context" in check:
        return ("status", check.get("context", ""))
    return ("check", check.get("workflowName", ""), check.get("name", ""))


def _standing_failures(rollup: list[dict] | None) -> list[dict]:
    """The failed runs no later verdict of the same check has replaced: a
    run that started after the failure and finished passing or failing.
    An unfinished run replaces nothing (it may yet fail, and admission
    admits a pending head), nor does a cancelled or skipped one (no
    verdict), nor one that started at the same time, nor any run when
    either start time is missing or the check has no name."""
    runs = rollup or []

    def replaces(other: dict, failure: dict) -> bool:
        key = _check_key(failure)
        return (other is not failure and _check_key(other) == key and bool(key[-1])
                and not _unfinished(other) and _check_state(other) not in (CANCELLED, "SKIPPED")
                and bool(_when(failure)[0]) and bool(_when(other)[0])
                and _when(other)[0] > _when(failure)[0])

    return [check for check in runs if _check_state(check) in FAILED
            and not any(replaces(other, check) for other in runs)]


def _latest_checks(rollup: list[dict] | None) -> list[dict]:
    """The latest result of each check, so a rerun that superseded a failure
    counts: a CheckRun is keyed by workflow and name, a StatusContext by its
    context. An unfinished run always wins, since a queued rerun may carry
    no start time yet and must not lose to the older result it replaces;
    among finished runs the later start (then completion) wins, list order
    on a tie."""
    latest: dict[tuple, dict] = {}
    for check in rollup or []:
        key = _check_key(check)
        held = latest.get(key)
        if held is None or _unfinished(check) or (
                not _unfinished(held) and _when(check) >= _when(held)):
            latest[key] = check
    return list(latest.values())


def _unfinished(check: dict) -> bool:
    if check.get("__typename") == "StatusContext" or "context" in check or (
            "status" not in check):
        return (check.get("state") or "").upper() in ("", "PENDING", "EXPECTED")
    return ((check.get("status") or "").upper() != "COMPLETED"
            or not check.get("conclusion"))


def _when(check: dict) -> tuple[str, str]:
    return (check.get("startedAt") or "", check.get("completedAt") or "")


def _merge_checks(rollup: list[dict] | None) -> tuple[list[str], list[str]]:
    """(red, waiting) for a batch head: every latest check must have finished
    as SUCCESS, SKIPPED or NEUTRAL, and each required lane must be among
    them. Any other conclusion (STALE and TIMED_OUT included) is red."""
    red, waiting, names = [], [], set()
    for check in _latest_checks(rollup):
        name = _check_name(check)
        names.add(name)
        if check.get("__typename") == "StatusContext" or "context" in check or (
                "status" not in check):
            state = (check.get("state") or "").upper()
            if state in ("", "PENDING", "EXPECTED"):
                waiting.append(name)
            elif state != "SUCCESS":
                red.append(f"{name} ({state})")
            continue
        conclusion = (check.get("conclusion") or "").upper()
        if (check.get("status") or "").upper() != "COMPLETED" or not conclusion:
            waiting.append(name)
        elif conclusion not in PASSING:
            red.append(f"{name} ({conclusion})")
    waiting += [f"{name} (not reported yet)" for name in REQUIRED_CHECKS
                if name not in names]
    return red, waiting


def _refuse_unmergeable(plan: Plan, view: dict) -> list[str] | None:
    """Refuse unless the pull request is this plan's batch at the built head,
    with every check finished and none failed, and master not past it. A
    batch built after a drop waits only for what it cannot carry, and its
    report (what was carried, what passed here) is returned."""
    n = plan.pr
    head_ref = view.get("headRefName", "")
    if (head_ref != plan.branch or not head_ref.startswith(PREFIX)
            or view.get("isCrossRepository")):
        raise Refusal(f"#{n} comes from {head_ref or 'an unknown branch'}"
                      f"{' in a fork' if view.get('isCrossRepository') else ''}, not "
                      f"this batch's {plan.branch}; nothing was merged. Find the batch's "
                      f"pull request with `gh pr list --head {plan.branch}`, and rerun "
                      "`open` with the same arguments to record it")
    if view.get("baseRefName") != BASE:
        raise Refusal(f"#{n} targets {view.get('baseRefName')}, not {BASE}; a batch "
                      f"merges into {BASE} only. Close it and build again")
    if view.get("state") != "OPEN":
        raise Refusal(f"#{n} is {str(view.get('state')).lower()}, not open; nothing "
                      "to merge. Switch back with `git switch --detach "
                      f"{REMOTE}/{BASE}`, remove the local branch and build again")
    if view.get("headRefOid") != plan.head:
        raise Refusal(f"#{n}'s head is {str(view.get('headRefOid'))[:12]}, not the head "
                      f"`build` made and the suite passed on ({plan.head[:12]}); nothing "
                      f"is fixed inside a batch. Close #{n}, switch back with `git switch "
                      f"--detach {REMOTE}/{BASE}`, remove the local branch and build "
                      "again (a batch forwarded under the `merge-forward` skill is the "
                      "maintainer's to merge)")
    red, waiting = _merge_checks(view.get("statusCheckRollup"))
    if red:
        raise Refusal(f"CI failed on #{n} at {plan.head[:12]}: {', '.join(red)}. Nothing "
                      "is fixed inside a batch: a Windows-only failure with a "
                      "non-deterministic signature gets one rerun on the current merge "
                      "ref naming the test and its flake issue (open one if none exists); "
                      "a second or deterministic Windows-only failure goes to the Windows "
                      "dev VM (CLAUDE.md \"Dev machines\"); otherwise drop the member the "
                      "failing job covers, or bisect, then close the batch and build "
                      "again (docs/dev/merge-batches.md, \"Validating it\")")
    report = None
    if plan.after_drop:
        # A failure whose rerun is still running stays a failure here: the
        # latest run alone would read as pending, and pending can be carried.
        standing = sorted({_check_name(c) for c in
                           _after_drop_standing(view.get("statusCheckRollup"))})
        if standing:
            raise Refusal(f"CI failed on #{n} at {plan.head[:12]} ({', '.join(standing)}) "
                          "and no finished rerun has replaced the failure; a batch built "
                          "after a drop never carries past a failure on its own head. Wait "
                          "for the rerun to finish, then rerun `python ops/integrate.py "
                          "merge`")
        waiting, report, notes = _after_drop_merge(plan, view)
        if waiting:
            raise Refusal(f"CI on #{n} has not finished what a batch after a drop cannot "
                          f"carry ({', '.join(waiting)}): `{AFTER_DROP_TEST}` must pass on "
                          "this head, and only a job that succeeded on the closed batch's "
                          f"head (#{plan.after_drop['batch']}) is carried ({AFTER_DROP_DOC}); "
                          "rerun `python ops/integrate.py merge` once they finish"
                          + "".join(f". {note}" for note in notes))
    if waiting:
        raise Refusal(f"CI on #{n} has not finished ({', '.join(waiting)}); wait for "
                      "every check, the required lanes included, to complete, then rerun "
                      "`python ops/integrate.py merge`")
    git("fetch", "-q", REMOTE, BASE)
    master = _out("rev-parse", f"{REMOTE}/{BASE}")
    if (master != plan.base_sha and git("merge-base", "--is-ancestor", master,
                                        plan.head, check=False).returncode != 0):
        raise Refusal(f"{REMOTE}/{BASE} moved since {plan.branch} was built "
                      f"({plan.base_sha[:12]} -> {master[:12]}) and #{n}'s head does not "
                      f"contain it, so its CI did not test what {BASE} would get. Close "
                      f"#{n}, switch back with `git switch --detach {REMOTE}/{BASE}`, "
                      "remove the local branch and build the next batch; only the "
                      "`merge-forward` skill's exceptions let it merge instead, and that "
                      "merge is the maintainer's")
    return report


def _on_master(sha: str, number: int, commit: str) -> bool:
    """Is `sha` reachable from the merge commit or master? Fetches the
    member's head first when this checkout lacks it (a push after `build`)."""
    if git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
        git("fetch", "-q", REMOTE, f"pull/{number}/head", check=False)
    tips = [t for t in (commit, f"{REMOTE}/{BASE}") if t
            and git("cat-file", "-e", f"{t}^{{commit}}", check=False).returncode == 0]
    return any(git("merge-base", "--is-ancestor", sha, tip, check=False).returncode == 0
               for tip in tips)


def _confirm_members(plan: Plan, commit: str) -> list[tuple[Member, str, str]]:
    """Report each member against its current head, asking GitHub again for
    up to MEMBER_WAIT_SECONDS while one sits on master unmarked. Returns the
    members not confirmed: (member, state, head) whose head is the one the
    batch carried and yet is not on master."""
    deadline = _clock() + MEMBER_WAIT_SECONDS
    pending = list(plan.merged)
    unconfirmed = []
    while pending:
        git("fetch", "-q", REMOTE, BASE, check=False)
        last = _clock() >= deadline
        waiting = []
        for m in pending:
            view = json.loads(gh("pr", "view", str(m.number), "--json",
                                 "state,headRefOid"))
            state, head = view.get("state", ""), view.get("headRefOid", "")
            if state == "MERGED":
                print(f"  #{m.number} MERGED {m.title}")
            elif _on_master(head, m.number, commit):
                if state == "OPEN" and not last:
                    waiting.append(m)   # GitHub marks it merged shortly
                else:
                    print(f"  #{m.number} {state.lower()}: its head {head[:12]} is in "
                          f"{BASE}; GitHub has not marked it merged - {m.title}")
            elif head != m.sha:
                print(f"  #{m.number} {state.lower()}: has commits after the batch "
                      f"({head[:12]}, the batch carried {m.sha[:12]}); it keeps its "
                      "label and joins a later batch")
            elif last:
                unconfirmed.append((m, state, head))
            else:
                waiting.append(m)
        pending = waiting
        if pending:
            _sleep(MEMBER_POLL_SECONDS)
    return unconfirmed


def merge(args: argparse.Namespace) -> int:
    plan = _merge_plan()
    n = plan.pr
    view = json.loads(gh("pr", "view", str(n), "--json",
                         "headRefName,headRefOid,baseRefName,state,"
                         "isCrossRepository,statusCheckRollup"))
    if view.get("state") == "MERGED" and view.get("headRefName") == plan.branch:
        # A rerun after the merge went through: only the confirmation is left.
        print(f"#{n} is already merged; confirming its members")
    else:
        report = _refuse_unmergeable(plan, view)
        frozen = _batch_freeze_refusal(plan)
        if frozen:
            raise Refusal(frozen)
        if report:
            # Every check read again just before the comment and the click,
            # which pins the head; the comment says what this read found.
            view = json.loads(gh("pr", "view", str(n), "--json",
                                 "headRefName,headRefOid,baseRefName,state,"
                                 "isCrossRepository,statusCheckRollup"))
            report = _refuse_unmergeable(plan, view)
            text = "\n".join(report)
            print(text)
            gh("pr", "comment", str(n), "--body-file", "-", input=text + "\n")
            # And once more after the comment, just before the click: a check
            # that turned red while it was posted refuses the merge.
            view = json.loads(gh("pr", "view", str(n), "--json",
                                 "headRefName,headRefOid,baseRefName,state,"
                                 "isCrossRepository,statusCheckRollup"))
            _refuse_unmergeable(plan, view)
        try:
            gh("pr", "merge", str(n), "--merge", "--admin",
               "--match-head-commit", plan.head)
        except Refusal as exc:
            raise Refusal(f"{exc}\nIf #{n} still reads open, nothing was merged: fix the "
                          "cause and rerun `merge`; if it merged, rerun `merge` to "
                          "confirm its members") from None
    after = json.loads(gh("pr", "view", str(n), "--json", "state,mergeCommit"))
    if after.get("state") != "MERGED":
        print(f"integrate: `gh pr merge` returned, but #{n} reads "
              f"{after.get('state')}; check it on GitHub, then rerun `merge`",
              file=sys.stderr)
        return 1
    commit = (after.get("mergeCommit") or {}).get("oid", "")
    print(f"merged #{n} into {BASE} as {commit or '<merge commit unknown>'}")
    unconfirmed = _confirm_members(plan, commit)
    for m, state, head in unconfirmed:
        print(f"integrate: #{m.number} reads {state.lower()} and its head {head[:12]}, "
              f"the one this batch carried, is not on {REMOTE}/{BASE} yet; check #{n}'s "
              f"merge commit on GitHub, then rerun `merge` to confirm again",
              file=sys.stderr)
    if unconfirmed:
        return 1
    Plan.path(plan.branch).unlink(missing_ok=True)
    print("Next:\n"
          f"  1. Watch CI on master at {commit[:12] or 'the merge commit'}: `gh run list "
          f"--branch {BASE} --commit {commit or '<merge commit>'}`. A red run gets a "
          "revert or fix pull request, landed as a batch of one.\n"
          f"  2. Free this checkout: `git switch --detach {REMOTE}/{BASE}` after a "
          f"`git fetch`, then `git branch -D {plan.branch}`.")
    return 0


def status(args: argparse.Namespace) -> int:
    git("fetch", "-q", REMOTE, BASE)
    base_sha = _out("rev-parse", f"{REMOTE}/{BASE}")
    members = ready(args.label)
    if not members:
        print(f"no open pull request carries `{args.label}`")
        return 0
    print(f"{len(members)} labelled `{args.label}`, against {REMOTE}/{BASE} "
          f"{base_sha[:12]}, in batch order:")
    for m in members:
        head = _fetch_head(m.number)
        moved = "" if head == m.sha else f" (fetched head {head[:12]} differs)"
        clean, files = _merge_tree(base_sha, head)
        merge = "merges cleanly" if clean else f"conflicts with master in {', '.join(files)}"
        draft = " draft," if m.draft else ""
        shown = (f" (report-only red, not counted: {', '.join(m.report_only_red)})"
                 if m.report_only_red else "")
        print(f"#{m.number} {m.sha[:12]}{moved}:{draft} {merge}; CI {m.checks}{shown}; "
              f"review {m.review}; labelled {m.labeled_at or 'at an unknown time'} "
              f"- {m.title}")
        print(f"  {m.rust_jobs}; snapshot {m.snapshot}")
        reason = _not_ready(m)
        if head != m.sha:
            reason = "head moved while status was read; retry status"
        if reason:
            print(f"  would leave out: {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (("status", "list labelled pull requests"),
                           ("build", "merge the next batch onto a local branch")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--label", default=LABEL)
    b = sub.choices["build"]
    b.add_argument("--dry-run", action="store_true",
                   help="merge on a detached HEAD, print the plan and switch back; "
                        "creates, pushes and comments on nothing")
    b.add_argument("--date", default=dt.date.today().isoformat(),
                   help="batch date (default: today)")
    b.add_argument("--after-drop", type=int, metavar="PR",
                   help="build the closed red batch PR again without its dropped "
                        "culprit, skipping the local suite and carrying its passes "
                        f"({AFTER_DROP_DOC})")
    o = sub.add_parser("open", help="push the built batch and open its pull request")
    evidence = o.add_mutually_exclusive_group()
    evidence.add_argument("--suite", help="the passing suite result JSON for this head")
    evidence.add_argument("--suite-note",
                          help="the suite result in words, for a run without a JSON")
    o.add_argument("--harness", help="the matching successful harness result JSON")
    o.add_argument("--shim-tests", help="the matching successful shim-tests result JSON")
    for name in ("harness", "shim-tests"):
        gate = sub.add_parser(name, help=f"run the built batch's {name} gate on its dev machine")
        gate.add_argument("--lock-wait-seconds", type=int, default=1200,
                          help="maximum wait for the machine's locks (default 1200)")
    sub.add_parser("suite", help="run the built batch's full suite on the remote "
                                 "machine (its head travels as a bundle)")
    sub.add_parser("merge", help="merge the published batch pull request once every "
                                 "check on its built head passed; the one path to "
                                 "master")
    args = parser.parse_args(argv)
    if hasattr(args, "lock_wait_seconds") and args.lock_wait_seconds < 0:
        parser.error("--lock-wait-seconds must be nonnegative")
    commands = {"status": status, "build": build, "suite": suite, "open": open_, "merge": merge,
                "harness": harness, "shim-tests": shim_tests}
    try:
        return commands[args.command](args)
    except Refusal as exc:
        print(f"integrate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
