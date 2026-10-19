"""Which Rust parity shards a run needs: the scope job's parity matrix.

rust.yml's parity-checks and parity-selfhosted jobs take their suite list and
include entries from this module, through the scope job's ``parity`` output.
Master pushes, the nightly, manual CI and merge groups run every suite. A pull
request runs the suites its diff reaches, never fewer than FLOOR; anything
this module cannot place runs everything. A divergence in a suite a pull
request skipped surfaces on the master push that merges it.

Before 2026-10-20 every pull request push queued 24 parity jobs behind the
account's 20-job cap (32 Rust jobs in all), and cancel-on-push threw away most
of them: 190 of 400 rust.yml runs from 2026-10-11 to 10-19 were cancelled.

Standard library only: the scope job runs it with the runner's own python3.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import PurePosixPath

# Every parity suite, in matrix order. Windows step medians on 2026-10-10,
# 14 successful runs (eval, cli, judges): 317s/476s/552.5s.
SUITES = ["eval", "cli", "cli-daemon", "cli-live", "judges",
          "w1c-doctor", "w1c-pairing", "w1c-connection",
          "h5-audit", "h5-backup", "h5-w1a", "h5-background", "h5-daemon-mutants", "h5-graph-mutants"]

# Per-suite matrix values (the include list). eval and judges have none and
# keep the job's 210-minute fallback.
SHARDS = [
    # Hosted Windows cli was one serial job: 70.4 and 61.5 min on master
    # runs 39205533092 and 39236836263 (2026-10-19; Linux 40.3 and 42.7).
    # Split three ways so no shard runs past about half an hour. cli keeps
    # the CLI rows' self-tests and goldens, lease, dream cursor, static,
    # graph store, principal and background steps: about 22 min of steps on
    # Windows plus about 4 min setup.
    {"suite": "cli", "job_timeout": 55},
    # Schema startup 5.9-7.1 min, then resident startup 12.1-14.9 min on the
    # binary the schema step builds; about 26 min with setup.
    {"suite": "cli-daemon", "job_timeout": 55},
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
    {"suite": "w1c-connection", "w1c_rows": "'test_login', 'connect', 'maintainer', 'move', 'tunnel'",
     "w1c_timeout": 42, "job_timeout": 66},
    # Linux 2026-10-11, live + oracle capture + replay: audit 112s, backup
    # 34s, W1-A 435s, daemon controls 189s, graph controls 394s. Linux step
    # budgets are about twice these. Windows budgets are provisional until
    # the first hosted run; each new job stays inside the existing
    # ~56-minute slowest job. Hosted ubuntu 2026-10-12: the audit step's
    # live phase alone took 201s and the step hit its 4-minute budget on
    # three runs (PRs #661 and #737) with every compared case matching;
    # 10 minutes is about twice the observed ~5-minute step.
    {"suite": "h5-audit", "h5_timeout": 10, "h5_windows_timeout": 20, "job_timeout": 30},
    {"suite": "h5-backup", "h5_timeout": 2, "h5_windows_timeout": 10, "job_timeout": 20},
    {"suite": "h5-w1a", "h5_timeout": 15, "h5_windows_timeout": 40, "job_timeout": 56},
    # Linux 2026-10-11: six background scenarios, 9 live cases, 110s
    # combined; 4m allows about twice measured CPU work.
    {"suite": "h5-background", "h5_timeout": 4, "h5_windows_timeout": 20, "job_timeout": 30},
    {"suite": "h5-daemon-mutants", "h5_timeout": 6, "h5_windows_timeout": 20, "job_timeout": 35},
    {"suite": "h5-graph-mutants", "h5_timeout": 14, "h5_windows_timeout": 30, "job_timeout": 45},
]

# Every pull request that runs parity at all runs these: the CLI rows most
# changes touch (mail, hook, episode, lease), the daemon startup, live and
# W1-A rows, and the eval harness.
FLOOR = ("eval", "cli", "cli-daemon", "cli-live", "h5-w1a")
ALL = tuple(SUITES)

# The rust/cli_harness rows outside the floor, by the suite that runs them
# (the W1-C rows match their shard's w1c_rows; tests pin both).
ROW_SUITES = {
    "mail": "cli", "hook": "cli", "episode": "cli", "lease": "cli", "lease_operator": "cli",
    "doctor": "w1c-doctor",
    "transfer": "w1c-pairing", "invite": "w1c-pairing", "pair": "w1c-pairing",
    "test_login": "w1c-connection", "connect": "w1c-connection", "maintainer": "w1c-connection",
    "move": "w1c-connection", "tunnel": "w1c-connection",
    "audit": "h5-audit", "backup": "h5-backup",
}
CLI_HARNESS = ("cli", "cli-live", "w1c-doctor", "w1c-pairing", "w1c-connection", "h5-audit", "h5-backup")
# The pseudolife-stdio binary: every CLI row, plus the differential judges,
# which cover its whole argv, help and version surface.
SHIM = CLI_HARNESS + ("judges",)
# The pseudolife-daemon crate (and its graph and dream contract binaries).
DAEMON = ("cli", "cli-daemon", "cli-live", "eval", "h5-w1a", "h5-background",
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
            return CLI_HARNESS
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
            # Every shard binds its Python oracle through these two.
            if parts[-1] in ("oracle_selection.py", "phase1_oracle.py", "provenance.py"):
                return ALL
            return ("eval", "judges")
        if parts[1] == "rust_baseline":
            return ("eval",)
    if top == ".github":
        return ALL if path == ".github/workflows/rust.yml" else ()
    # Python tests and the Console bundle reach only floor suites: the MCP
    # catalogue test and the static-serving rows both run there.
    if top in ("tests", "frontend"):
        return ()
    # The Python oracle, the plugin, ops, packaging and anything else.
    return ALL


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
    return [suite for suite in SUITES if suite in chosen], reasons


SUITE_EXPRESSION = "${{ fromJSON(needs.scope.outputs.parity).suite }}"
INCLUDE_EXPRESSION = "${{ fromJSON(needs.scope.outputs.parity).include }}"


def resolve(workflow: dict) -> dict:
    """The workflow with every scope-chosen parity matrix expanded to the full
    matrix, as a master push runs it; for the checks that read the YAML."""
    for name in ("parity-checks", "parity-selfhosted"):
        values = workflow["jobs"][name]["strategy"]["matrix"]
        if values.get("suite") == SUITE_EXPRESSION:
            values["suite"] = list(SUITES)
        if values.get("include") == INCLUDE_EXPRESSION:
            values["include"] = [dict(shard) for shard in SHARDS]
    return workflow


def matrix(suites: list[str]) -> dict:
    """The parity matrix's suite and include values for these suites.

    An include entry for a suite the run does not select would add a job of
    its own, with no os, so only the selected suites' entries go in.
    """
    return {"suite": list(suites), "include": [dict(shard) for shard in SHARDS if shard["suite"] in suites]}


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
    options = parser.parse_args(argv)
    files = changed_files() if options.event == "pull_request" else None
    suites, reasons = select(options.event, files)
    if options.event == "pull_request" and files is None:
        print("parity: the diff could not be read; running every suite")
    elif options.event == "pull_request":
        print(f"parity: floor {', '.join(FLOOR)}")
        for path, widened in reasons.items():
            print(f"parity: {path} -> {'every suite' if widened == ALL else ', '.join(widened)}")
    print(f"parity: running {len(suites)} of {len(SUITES)} suites: {', '.join(suites)}")
    with open(options.github_output, "a", encoding="utf-8") as output:
        output.write(f"parity={json.dumps(matrix(suites), separators=(',', ':'))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
