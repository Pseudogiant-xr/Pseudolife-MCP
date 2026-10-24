"""Which Rust parity shards a run needs: the scope job's parity matrix.

rust.yml's parity-checks and parity-selfhosted jobs take their suite list and
include entries from this module, through the scope job's ``parity`` output.
The nightly, manual CI and merge groups run every suite, and so does a master
push, less what its merged batch passed (below). A pull request runs the
suites its diff reaches, never fewer than FLOOR; anything this module cannot
place runs everything. A divergence in a suite a pull request skipped
surfaces on the master push that merges it, since that suite never passed on
the batch.

A master push leaves out each (os, suite) pair its merged batch already
passed on the same tree (ops/ci_batch_passed.py names them), except the
EVENT_BOUND suites, whose result depends on the event's oracle.

Before 2026-10-20 every pull request push queued 24 parity jobs behind the
account's 20-job cap (32 Rust jobs in all), and cancel-on-push threw away most
of them: 190 of 400 rust.yml runs from 2026-10-11 to 10-19 were cancelled.

Standard library only: the scope job runs it with the runner's own python3.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]

# Every parity suite, in matrix order. Windows step medians on 2026-10-10,
# 14 successful runs (eval, cli, judges): 317s/476s/552.5s.
SUITES = ["eval", "cli", "cli-store", "cli-daemon", "cli-daemon-2", "cli-live", "judges",
          "w1c-doctor", "w1c-pairing", "w1c-connection", "w1c-connect",
          "h5-audit", "h5-backup", "h5-w1a", "h5-background", "h5-daemon-mutants", "h5-graph-mutants",
          "h5-light"]

# Per-suite matrix values (the include list). eval and judges have none and
# keep the job's 210-minute fallback.
SHARDS = [
    # Hosted Windows cli was one serial job: 70.4 and 61.5 min on master
    # runs 39205533092 and 39236836263 (2026-10-19; Linux 40.3 and 42.7).
    # Split three ways so no shard runs past about half an hour, then on
    # 2026-10-24 the cli and cli-daemon shards in two again: under the
    # account's 20-job cap a 23-26 minute shard that starts in the second
    # wave set the batch's critical path (batch run 40047950017: Windows
    # cli 23.2 min, cli-daemon 25.9, started 9.6 min late). Windows step
    # times from that run, plus about 3.5 min setup per shard:
    # cli keeps the sent-YAML check and the CLI rows' self-tests and goldens,
    # lease and lease operator steps, 10.0 min.
    {"suite": "cli", "job_timeout": 35},
    # cli-store: dream cursor, static, graph store, background and
    # principal steps, 9.6 min.
    {"suite": "cli-store", "job_timeout": 35},
    # Schema startup 4.7 min, then 11 resident startup cases at about 29 s
    # each, on the storage test binary the fixtures job built.
    {"suite": "cli-daemon", "startup_part": 1, "job_timeout": 40},
    # The other 11 cases (every mutant's control among them), 9 startup
    # mutants, the clock retry and the writer guard, about 10.6 min
    # (startup_cases.part_cases).
    {"suite": "cli-daemon-2", "startup_part": 2, "job_timeout": 40},
    # Same runs, the four live steps: Windows 22.4 min (body 2.5, security
    # 8.6, MCP 8.9, CLI live arms about 2.5) plus about 4 min setup; Linux
    # 14 min. About twice the Windows total.
    {"suite": "cli-live", "job_timeout": 55},
    # W1-C row sums: master run 38076979343, 2026-10-10, Windows.
    # Live + golden: doctor 15.3m; pairing 17.6m; connection 20.1m.
    # Add ~2.5m checkout/dependency/PostgreSQL setup, then allow twice the
    # total. Connection also keeps the existing 20m record budget.
    {"suite": "w1c-doctor", "w1c_rows": "'doctor'", "w1c_timeout": 32, "job_timeout": 36},
    {"suite": "w1c-pairing", "w1c_rows": "'transfer', 'invite', 'pair'", "w1c_timeout": 36,
     "job_timeout": 40},
    # Connection's five rows ran 20.2 min on Windows in batch run
    # 40047950017 (2026-10-24; live + golden: test_login 7.3, connect 6.3,
    # maintainer 3.5, tunnel 1.9, move 1.2), so they split in two: 10.4 and
    # 9.8 min. test_login stays here with its container image and record.
    {"suite": "w1c-connection", "w1c_rows": "'test_login', 'move', 'tunnel'",
     "w1c_timeout": 26, "job_timeout": 50},
    {"suite": "w1c-connect", "w1c_rows": "'connect', 'maintainer'", "w1c_timeout": 25,
     "job_timeout": 29},
    # Hosted step timings for rust.yml runs 2026-10-11 to 10-20, about 300
    # successful steps per suite and OS (h5_linux_measurements.json
    # "hosted_step_distribution"). Linux step budgets are five times each
    # suite's ubuntu p99 (minutes: audit 4.2, backup 1.6, W1-A 7.9,
    # background 2.0, daemon controls 4.3, graph controls 9.3): master
    # f82141c4's ubuntu runner ran 4.6-5.4x slow and timed graph controls
    # out at 14 minutes with no mutant surviving, while no other step ran
    # past 1.6x its median. Graph controls get 50, above the 44-46 minutes
    # that runner would have needed. It spent 9 minutes outside the step, so
    # each job keeps 15 minutes beyond its Linux step budget. Windows
    # budgets stay at 1.5-6.7x the hosted Windows maximum (13.5 and 19.9
    # minutes for the two mutant suites): no slow Windows runner was seen,
    # but one is not ruled out, and five times p99 there would mean 68- and
    # 91-minute steps.
    {"suite": "h5-audit", "h5_timeout": 21, "h5_windows_timeout": 20, "job_timeout": 36},
    {"suite": "h5-backup", "h5_timeout": 9, "h5_windows_timeout": 10, "job_timeout": 24},
    {"suite": "h5-w1a", "h5_timeout": 40, "h5_windows_timeout": 40, "job_timeout": 56},
    {"suite": "h5-background", "h5_timeout": 10, "h5_windows_timeout": 20, "job_timeout": 30},
    {"suite": "h5-daemon-mutants", "h5_timeout": 22, "h5_windows_timeout": 20, "job_timeout": 37},
    {"suite": "h5-graph-mutants", "h5_timeout": 50, "h5_windows_timeout": 30, "job_timeout": 65},
    # Linux only: h5-audit, h5-backup and h5-background in one job
    # (coverage_plan.COMPOSITES), with the sum of their Linux step budgets.
    # Windows runs the three as their own shards and never this one.
    {"suite": "h5-light", "h5_timeout": 40, "h5_windows_timeout": 40, "job_timeout": 55},
]

# The hosted matrix's systems, and the pair it always leaves out: hosted
# Windows' live comparisons run in the non-gating parity-live job instead.
SYSTEMS = ("ubuntu-latest", "windows-latest")
STATIC_EXCLUDE = ({"os": "windows-latest", "suite": "cli-live"},)
# The H5 suites Linux runs inside h5-light (maintainer decision 2026-10-24:
# two fewer short jobs per run under the 20-job cap). Every pair below is
# left out of the matrix, and selecting any of these suites selects
# h5-light, so Linux still runs each of them wherever it did before.
LINUX_LIGHT = ("h5-audit", "h5-backup", "h5-background")
STATIC_EXCLUDE += ({"os": "windows-latest", "suite": "h5-light"},) + tuple(
    {"os": "ubuntu-latest", "suite": suite} for suite in LINUX_LIGHT)
# Suites that compare against the event-selected Python oracle
# (evals/rust_port/oracle_selection.py): the merge base on a pull request,
# the pushed head on a push. A batch's pass compared the base, so a master
# push always runs them, whatever the batch passed.
EVENT_BOUND = ("eval", "judges")

# Every pull request that runs parity at all runs these: the CLI rows most
# changes touch (mail, hook, episode, lease), the daemon store, startup, live
# and W1-A rows, and the eval harness. cli-store and cli-daemon-2 hold steps
# the cli and cli-daemon shards ran until 2026-10-24.
FLOOR = ("eval", "cli", "cli-store", "cli-daemon", "cli-daemon-2", "cli-live", "h5-w1a")
ALL = tuple(SUITES)

# The rust/cli_harness rows outside the floor, by the suite that runs them
# (the W1-C rows match their shard's w1c_rows; tests pin both).
ROW_SUITES = {
    "mail": "cli", "hook": "cli", "episode": "cli", "lease": "cli", "lease_operator": "cli",
    "doctor": "w1c-doctor",
    "transfer": "w1c-pairing", "invite": "w1c-pairing", "pair": "w1c-pairing",
    "test_login": "w1c-connection", "connect": "w1c-connect", "maintainer": "w1c-connect",
    "move": "w1c-connection", "tunnel": "w1c-connection",
    "audit": "h5-audit", "backup": "h5-backup",
}
# The pseudolife-stdio binary: every CLI row, plus the differential judges,
# which cover its whole argv, help and version surface.
SHIM = ("cli", "cli-live", "judges", "w1c-doctor", "w1c-pairing", "w1c-connection", "w1c-connect",
        "h5-audit", "h5-backup")
# The pseudolife-daemon crate (and its graph and dream contract binaries).
DAEMON = ("cli", "cli-store", "cli-daemon", "cli-daemon-2", "cli-live", "eval", "h5-w1a", "h5-background",
          "h5-daemon-mutants", "h5-graph-mutants")
# rust/shim/src/cli entries that only one command's code uses, by the rows
# that compare it; any other shim file reaches the whole binary. Shared
# helpers (lease, pairing, hook_http, hook_json, doorbell_*) are left out:
# other commands import them. test_parity_scope.py scans the shim source and
# fails if another command starts using a mapped entry.
SHIM_COMMANDS = {
    "backup": ("backup",), "backup_help.txt": ("backup",), "board_audit": ("audit",),
    "connect": ("connect",), "doctor": ("doctor",), "maintainer": ("maintainer",),
    "move_cli.rs": ("move",), "test_login": ("test_login",),
    "transfer": ("transfer",), "transfer.rs": ("transfer",), "tunnel": ("tunnel",),
    "episode.rs": ("episode",), "episode_input.rs": ("episode",),
    "briefing_hook.rs": ("hook",), "briefing_help.txt": ("hook",),
}
# The scope step's documentation-only rule, which Python's re cannot take
# verbatim (its lookbehind is variable-width); tests check both agree.
CODE_DIRS = re.compile(r"(?i)(rust|evals|pseudolife_memory|plugin|ops|tests|\.github)/")
CODE_SUFFIX = re.compile(r"\.(py|ps1|psm1|sh|bash|bat|cmd|js|mjs|cjs|ts|rs|toml|yml|yaml)$")


def is_docs(path: str) -> bool:
    """A file no Rust or parity job reads (CLAUDE.md "Docs-only changes")."""
    if CODE_DIRS.match(path):
        return False
    if re.fullmatch(r'[^/"].*\.md', path) or re.fullmatch(r"llms(-full)?\.txt", path):
        return True
    return path.startswith("docs/") and not CODE_SUFFIX.search(path)


def reached(path: str) -> tuple[str, ...]:
    """The suites one changed file reaches, beyond the floor; ALL if unknown."""
    parts = PurePosixPath(path).parts
    if not parts:
        return ALL
    if is_docs(path):
        return ()
    top = parts[0]
    if top == "rust":
        rest = parts[1:]
        if not rest:
            return ALL
        area = rest[0]
        if path.endswith(".md") and area != "cli_harness":
            return ()  # documents the port; no harness reads it
        if len(rest) == 1:
            # Cargo files, the toolchain pin and the evidence JSON.
            return ALL
        if area == "cli_harness":
            if rest[1] in ("rows", "goldens", "specs"):
                row = row_of(rest[1], rest[-1])
                if row in ROW_SUITES:
                    return (ROW_SUITES[row],)
            # Shared harness code: the daemon harness's disposable banks and
            # every shard's PostgreSQL prep import it too.
            return ALL
        if area == "shim":
            if rest[1:3] == ("src", "cli") and len(rest) > 3 and rest[3] in SHIM_COMMANDS:
                return tuple(ROW_SUITES[row] for row in SHIM_COMMANDS[rest[3]]) + ("judges",)
            return SHIM
        if area == "daemon":
            # The harness is shared with the CLI rows; the crate is not.
            return ALL if rest[1] == "harness" else DAEMON
        return ALL  # rust/ci, rust/shared and anything new
    if top == "evals" and len(parts) > 1:
        if parts[1] == "rust_port":
            # Every shard binds its Python oracle through ORACLE_MODULES; the
            # harness imports others; the package's __init__ runs for all.
            if parts[-1] == "__init__.py" or module_of(parts[2:]) in harness_imports("evals.rust_port"):
                return ALL
            return ("eval", "judges")
        if parts[1] == "rust_baseline":
            return ("eval",)
    if top == ".github":
        return ALL if path == ".github/workflows/rust.yml" else ()
    if top == "tests":
        # A test module the harness imports (rows seed banks with
        # tests.test_transfer_cli, judges start fake embedders) reaches every
        # suite; any other Python test only the floor's MCP catalogue step.
        if parts[-1] == "__init__.py" or module_of(parts[1:]) in harness_imports("tests"):
            return ALL
        return ()
    # The Console bundle: the floor's static-serving rows.
    if top == "frontend":
        return ()
    # The Python oracle, the plugin, ops, packaging and anything else.
    return ALL


def module_of(parts: tuple[str, ...]) -> str:
    """The top-level module a file under a package belongs to."""
    return parts[0].removesuffix(".py") if parts else ""


# The evals.rust_port modules every parity shard runs to bind its oracle.
ORACLE_MODULES = ("oracle_selection", "phase1_oracle", "provenance")
# The scan's own tests name packages in strings; they import nothing.
SCAN_EXCLUDES = ("rust/ci/test_parity_scope.py",)


@functools.lru_cache(maxsize=None)
def harness_imports(package: str) -> frozenset[str]:
    """The top-level modules of ``package`` (tests or evals.rust_port) that
    the parity harnesses import, directly or through each other, absolute or
    relative imports alike."""
    sources = [ROOT / "rust"] + ([ROOT / "evals/rust_port"] if package == "tests" else [])
    directory = ROOT / package.replace(".", "/")
    escaped = re.escape(package)
    absolute = re.compile(rf"\b(?:from|import)\s+{escaped}\.(\w+)|"
                          rf"\bfrom\s+{escaped}\s+import\s+(\([^)]*\)|[\w ,]+)")
    # Inside the package: from .x import y, from . import (x, y).
    relative = re.compile(r"\bfrom\s+\.(\w+)|\bfrom\s+\.\s+import\s+(\([^)]*\)|[\w ,]+)")

    def names(pattern, text: str) -> set[str]:
        found = set()
        for dotted, listed in pattern.findall(text):
            found.update([dotted] if dotted else
                         (name.strip() for name in listed.strip("()").replace("\n", " ").split(",")
                          if name.strip() and name.strip() != "as"))
        return {name.split()[0] for name in found}

    excluded = {ROOT / path for path in SCAN_EXCLUDES}
    seen: set[str] = set()
    pending = set(ORACLE_MODULES) if package == "evals.rust_port" else set()
    for source in sources:
        for path in source.rglob("*.py"):
            if path not in excluded:
                pending |= names(absolute, path.read_text(encoding="utf-8", errors="replace"))
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for path in [directory / f"{name}.py", *(directory / name).rglob("*.py")]:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                pending |= names(absolute, text) | names(relative, text)
    return frozenset(seen)


def row_of(kind: str, name: str) -> str:
    """The row a rows/, goldens/ or specs/ file belongs to."""
    if kind == "specs":
        # CLI-LEASE-core.md -> lease
        match = re.match(r"CLI-([A-Z_]+)", name)
        return match.group(1).lower() if match else ""
    return name.split(".", 1)[0]


def select(event: str, files: list[str] | None) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """The suites to run, in matrix order, and why each file widened them."""
    if event != "pull_request" or not files:
        return list(SUITES), {}
    chosen, reasons = set(FLOOR), {}
    for path in files:
        suites = reached(path)
        if set(suites) - chosen:
            reasons[path] = suites
        chosen.update(suites)
    if chosen & set(LINUX_LIGHT):
        chosen.add("h5-light")
    return [suite for suite in SUITES if suite in chosen], reasons


SUITE_EXPRESSION = "${{ fromJSON(needs.scope.outputs.parity).suite }}"
INCLUDE_EXPRESSION = "${{ fromJSON(needs.scope.outputs.parity).include }}"
EXCLUDE_EXPRESSION = "${{ fromJSON(needs.scope.outputs.parity).exclude }}"
# The self-hosted lane has no os axis to exclude a composite from, and an
# include entry for a suite its matrix lacks would become a job with no
# runner, queued for a day. It takes the same values less the hosted-only
# composites; each runner runs their parts as their own shards.
HOSTED_ONLY = ("h5-light",)
SELFHOSTED_SUITE_EXPRESSION = "${{ fromJSON(needs.scope.outputs.parity_selfhosted).suite }}"
SELFHOSTED_INCLUDE_EXPRESSION = "${{ fromJSON(needs.scope.outputs.parity_selfhosted).include }}"


def resolve(workflow: dict) -> dict:
    """The workflow with every scope-chosen parity matrix expanded to the full
    matrix, as the nightly (and a master push with nothing to leave out) runs
    it; for the checks that read the YAML."""
    for name in ("parity-checks", "parity-selfhosted"):
        values = workflow["jobs"][name]["strategy"]["matrix"]
        if values.get("suite") == SUITE_EXPRESSION:
            values["suite"] = list(SUITES)
        if values.get("include") == INCLUDE_EXPRESSION:
            values["include"] = [dict(shard) for shard in SHARDS]
        if values.get("exclude") == EXCLUDE_EXPRESSION:
            values["exclude"] = [dict(pair) for pair in STATIC_EXCLUDE]
        full = selfhosted(matrix(list(SUITES)))
        if values.get("suite") == SELFHOSTED_SUITE_EXPRESSION:
            values["suite"] = full["suite"]
        if values.get("include") == SELFHOSTED_INCLUDE_EXPRESSION:
            values["include"] = full["include"]
    return workflow


def selfhosted(values: dict) -> dict:
    """The self-hosted lane's suite and include values: ``values`` less the
    hosted-only composites, so every include entry has a combination."""
    return {"suite": [suite for suite in values["suite"] if suite not in HOSTED_ONLY],
            "include": [dict(entry) for entry in values["include"] if entry["suite"] not in HOSTED_ONLY]}


def matrix(suites: list[str], exclude: list[dict] | None = None) -> dict:
    """The parity matrix's suite, include and exclude values for these suites.

    An include entry for a suite the run does not select would add a job of
    its own, with no os, so only the selected suites' entries go in.
    parity-selfhosted reads suite and include only (it has no os axis).
    """
    return {"suite": list(suites), "include": [dict(shard) for shard in SHARDS if shard["suite"] in suites],
            "exclude": [dict(pair) for pair in STATIC_EXCLUDE] if exclude is None else exclude}


def check_name(os_name: str, suite: str) -> str:
    """The job name GitHub reports for one parity-checks pair."""
    return f"Parity checks / {os_name} / {suite}"


def pruned(suites: list[str], passed: list[str], event: str) -> dict:
    """The matrix for these suites, less the pairs a master push's merged
    batch passed (``passed``: exact job names from ops/ci_batch_passed.py).

    Only a push reads ``passed``, and never for an EVENT_BOUND suite. A pair
    leaves through an exclude entry; a suite with no pair left leaves the
    suite list and takes its include entry with it, since an include matching
    no combination would add an os-less job. EVENT_BOUND keeps the push's
    matrix from ever being empty.
    """
    if event != "push":
        return matrix(suites)
    names = set(passed)
    static = {(pair["os"], pair["suite"]) for pair in STATIC_EXCLUDE}
    kept, exclude = [], [dict(pair) for pair in STATIC_EXCLUDE]
    for suite in suites:
        remaining = [os_name for os_name in SYSTEMS if (os_name, suite) not in static]
        left_out = [] if suite in EVENT_BOUND else [
            os_name for os_name in remaining if check_name(os_name, suite) in names]
        if len(left_out) == len(remaining):
            continue
        kept.append(suite)
        exclude.extend({"os": os_name, "suite": suite} for os_name in left_out)
    return matrix(kept, exclude)


def left_out(suites: list[str], values: dict) -> list[str]:
    """The os/suite pairs ``values`` leaves out of these suites' full matrix."""
    static = {(pair["os"], pair["suite"]) for pair in STATIC_EXCLUDE}
    excluded = {(pair["os"], pair["suite"]) for pair in values["exclude"]}
    return sorted(f"{os_name}/{suite}" for suite in suites for os_name in SYSTEMS
                  if (os_name, suite) not in static
                  and (suite not in values["suite"] or (os_name, suite) in excluded))


def batch_passed(raw: str | None) -> list[str] | None:
    """The job names in --batch-passed, or None when it is not a JSON list of
    strings (then nothing is left out)."""
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return None
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        return None
    return value


def changed_files() -> list[str] | None:
    """The pull request's files: its merge commit against the base parent."""
    try:
        result = subprocess.run(["git", "diff", "--no-renames", "--name-only", "HEAD^1", "HEAD"],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", required=True)
    parser.add_argument("--github-output", required=True)
    parser.add_argument("--batch-passed", default="",
                        help="push only: a JSON list of the job names the merged batch passed")
    options = parser.parse_args(argv)
    files = changed_files() if options.event == "pull_request" else None
    suites, reasons = select(options.event, files)
    passed = batch_passed(options.batch_passed)
    if passed is None:
        print("parity: the batch's passed jobs could not be read; leaving nothing out")
        passed = []
    values = pruned(suites, passed, options.event)
    if options.event == "pull_request" and files is None:
        print("parity: the diff could not be read; running every suite")
    elif options.event == "pull_request":
        print(f"parity: floor {', '.join(FLOOR)}")
        for path, widened in reasons.items():
            print(f"parity: {path} -> {'every suite' if widened == ALL else ', '.join(widened)}")
    if left_out(suites, values):
        print("parity: the merged batch passed these on the same tree, so they are left out: "
              + ", ".join(left_out(suites, values)))
    print(f"parity: running {len(values['suite'])} of {len(SUITES)} suites: {', '.join(values['suite'])}")
    with open(options.github_output, "a", encoding="utf-8") as output:
        output.write(f"parity={json.dumps(values, separators=(',', ':'))}\n")
        output.write(f"selfhosted={json.dumps(selfhosted(values), separators=(',', ':'))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
