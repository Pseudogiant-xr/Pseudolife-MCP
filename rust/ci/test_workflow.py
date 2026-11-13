"""Keep every Rust gate while changing the workflow's execution graph."""

import argparse
import copy
from collections import Counter
import json
import re
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/rust.yml"
SYSTEMS = ["ubuntu-latest", "windows-latest"]
RUST_CHECKS = (
    "Check generated daemon schema",
    "Bind the selected Python interpreter for Rust fixtures",
    "Install the Python SDK fixture dependency",
    "Point the Rust identity contract at the checkout's package metadata",
    "Install pinned nextest in the runner temporary directory",
    "Check formatting", "Check all targets", "Clippy", "Nextest",
    "Check write-path harness targets",
    "Check all targets without default features", "Clippy without default features",
    "Nextest without default features",
    "Daemon override build configurations",
)
# Since the Python freeze (14 November 2026) every parity check replays
# committed goldens or runs a harness's own tests; no step binds an oracle.
PARITY_CHECKS = {
    "Install checkout and test dependencies": None,
    "Check CI coverage contract": "eval",
    "Compare sent YAML startup with PyYAML": "cli",
    "Offline embedding golden and mutant row": "eval",
    "Offline W2-D search harness regressions": "eval",
    "CLI differential harness": "cli",
    "Prepare disposable PostgreSQL for CLI lease row": "cli*",
    "CLI lease golden harness": "cli",
    "CLI lease operator golden harness": "cli",
    "CLI W1-C golden harness": "w1c",
    "Prepare disposable PostgreSQL for W1-C rows": "w1c",
    "Daemon background pruning golden": "cli-store",
    "Dream cursor golden harness": "cli-store",
    "Principal store golden harness": "cli-store",
    "Daemon static HTTP golden": "cli-store",
    "Daemon schema startup parity": "cli-startup",
    "Graph store golden harness": "cli-store",
    "Daemon body admission golden": "cli-store",
    "Daemon security admission golden": "cli-store",
    "Daemon security live checks without a Rust test": "cli-store",
    "Daemon resident startup parity": "cli-startup",
    "Daemon resident startup parity part 2": "cli-startup",
}
# Steps and jobs that ran live comparisons against the Python oracle and left
# CI with the freeze; none may come back.
RETIRED_STEPS = ("Bind the event-selected Python oracle", "Run every eval harness test",
                 "CLI differential harness live arms", "Daemon MCP live parity and controls",
                 "Run unchanged candidates and differential judges", "Ported rows and daemon controls",
                 "Prepare selected package metadata for the Rust identity contract",
                 "Record the Linux test_login golden while it is absent")
RETIRED_JOBS = ("frozen-probes", "parity-live", "body-evidence", "capture-windows")
# Parity uploads carry the lane prefix: empty on hosted, set by parity-selfhosted.
ARTIFACT_PREFIX = "${{ env.PARITY_ARTIFACT_PREFIX }}"
PYTEST_FILTERS = {"-k", "-m", "--ignore", "--ignore-glob", "--deselect",
                  "--collect-only", "--co", "--lf", "--last-failed", "--stepwise", "--sw"}


def commands(script):
    """Read the workflow's standalone invocations, excluding comments."""
    result = []
    for line in script.splitlines():
        line = line.strip()
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            # PowerShell here-string delimiters are not standalone commands.
            continue
        if words and words[0] == "&":
            words = words[1:]
        if words and words[0] == "cargo.exe":
            words[0] = "cargo"
        if words and words[0] in ("python", "cargo", "$oraclePython"):
            result.append(words)
    return result


def require_command(invocations, prefix, required=(), forbidden=()):
    matches = [words for words in invocations if words[:len(prefix)] == list(prefix)]
    assert any(set(required) <= set(words) and not set(forbidden).intersection(
        word.split("=", 1)[0] for word in words[len(prefix):])
               for words in matches), (prefix, required)


def row_commands(invocations, required_rows, golden):
    matches = [words for words in invocations if words[:2] == ["python", "rust/cli_harness"]]
    covered = set()
    for words in matches:
        options = {word.split("=", 1)[0] for word in words}
        if ("--golden" in words) != golden or {"--record", "--mutants", "--case"}.intersection(options):
            continue
        assert "--candidate" in words
        if not golden and "lease" in required_rows:
            assert "--skip-bank" not in words
        covered.update(words[index + 1] for index, word in enumerate(words[:-1]) if word == "--row")
    assert set(required_rows) <= covered, (required_rows, golden)


W1C_SUITES = ("w1c-doctor", "w1c-pairing", "w1c-connection", "w1c-connect")
CLI_SUITES = ("cli", "cli-store", "cli-startup")
PARITY_SUITES = ["eval", *CLI_SUITES, *W1C_SUITES]
# No pair is left out of the matrix since the freeze.
STATIC_EXCLUDE = []


# The W1-C step passes its rows through a PowerShell array (@golden), so
# row_commands() sees no --row there: the row list and how the golden pass is
# built from it are pinned here instead.
W1C_ROWS = ("transfer", "test_login", "doctor", "connect", "maintainer", "invite", "pair",
            "move", "tunnel", "console", "conductor_watch", "coordination_recovery", "maintainer_setup", "restore")
W1C_LINES = (
    "$golden = $rows | ForEach-Object { '--row'; $_ }",
)


def check_w1c_rows(script):
    lines = [line.strip() for line in script.splitlines()]
    literals = [line for line in lines if line.startswith("$rows = @(")]
    assert len(literals) == 1, literals
    assert literals[0] == "$rows = @(${{ matrix.w1c_rows }})"
    for line in W1C_LINES:
        assert line in lines, line
    invocations = commands(script)
    require_command(invocations, ("python", "rust/cli_harness", "@golden", "--golden"),
                    ("--candidate",), ("--record", "--mutants", "--case"))
    assert [words[:3] for words in invocations if words[:2] == ["python", "rust/cli_harness"]] \
        == [["python", "rust/cli_harness", "@golden"]]


def check_w1c_shards(jobs):
    job = jobs["parity-checks"]
    matrix = job["strategy"]["matrix"]
    assert matrix["os"] == SYSTEMS
    assert matrix["suite"] == PARITY_SUITES
    assert matrix["exclude"] == STATIC_EXCLUDE
    shards = [shard for shard in matrix["include"] if shard["suite"] in W1C_SUITES]
    assert len(shards) == len(W1C_SUITES)
    assert Counter(shard["suite"] for shard in shards) == Counter(W1C_SUITES)
    covered = []
    for shard in shards:
        assert set(shard) == {"suite", "w1c_rows", "w1c_timeout", "job_timeout"}
        assert re.fullmatch(r"'[a-z_]+'(?:, '[a-z_]+')*", shard["w1c_rows"])
        rows = re.findall(r"'([a-z_]+)'", shard["w1c_rows"])
        assert ("test_login" in rows) == (shard["suite"] == "w1c-connection")
        assert shard["w1c_timeout"] > 0
        assert shard["job_timeout"] > shard["w1c_timeout"]
        covered.extend(rows)
    assert Counter(covered) == Counter(W1C_ROWS), covered
    steps = {step.get("name"): step for step in job["steps"]}
    harness = steps["CLI W1-C golden harness"]
    assert harness["if"] == "startsWith(matrix.suite, 'w1c-')"
    assert harness["timeout-minutes"] == "${{ matrix.w1c_timeout }}"
    check_w1c_rows(harness["run"])
    setup = steps["Prepare disposable PostgreSQL for W1-C rows"]
    assert setup["if"] == harness["if"]
    assert setup["run"] == steps["Prepare disposable PostgreSQL for CLI lease row"]["run"]
    assert setup["env"] == steps["Prepare disposable PostgreSQL for CLI lease row"]["env"]
    assert job["steps"].index(setup) < job["steps"].index(harness)
    image = steps["Build the test_login container image"]
    assert image["if"] == "runner.os == 'Linux' && matrix.suite == 'w1c-connection'"
    assert image["run"] == "docker build -t pseudolife-pg:18 -f ops/Dockerfile.pg ops"
    assert job["steps"].index(image) < job["steps"].index(harness)
    # The Linux test_login golden is committed; the step that recorded it
    # from the Python oracle while it was absent left with the freeze.
    assert "Record the Linux test_login golden while it is absent" not in steps
    assert "Retain the recorded Linux test_login golden" not in steps
    assert (ROOT / "rust/cli_harness/goldens/test_login.linux.json").is_file()
    summaries = steps["Retain CLI harness summaries"]
    assert summaries["if"] == ("always() && (startsWith(matrix.suite, 'cli') || "
                               "startsWith(matrix.suite, 'w1c-'))")
    assert summaries["with"]["name"] == ARTIFACT_PREFIX + "cli-harness-${{ runner.os }}-${{ matrix.suite }}"
    assert summaries["with"]["path"] == "${{ runner.temp }}/cli-harness*.json"
    # A second explicit row invocation outside the shard step duplicates work.
    for step in job["steps"]:
        for words in commands(step.get("run", "")):
            if words[:2] == ["python", "rust/cli_harness"]:
                assert not any(word in W1C_ROWS for word in words)
    for step in job["steps"]:
        # The toolchain only where cargo or rustc runs: the suites that cache.
        if step.get("uses") == "dtolnay/rust-toolchain@stable":
            assert step["if"] == "matrix.suite == 'eval' || matrix.suite == 'cli'" == CACHED_IF
        if step.get("uses") == "Swatinem/rust-cache@v2":
            assert "w1c-" not in step["if"] and CACHED_SUITES == {"eval", "cli"}


def check_executable_coverage(jobs):
    rust = {step.get("name"): step for step in jobs["rust"]["steps"]}
    require_command(commands(rust["Check write-path harness targets"]["run"]),
                    ("cargo", "check"), ("--locked", "--all-targets", "-p",
                                         "pseudolife-daemon", "--features", "harness,mutants"))
    require_command(commands(rust["Check generated daemon schema"]["run"]),
                    ("python", "rust/daemon/harness/gen_schema_sql.py"), ("--check",))
    assert rust["Check generated daemon schema"]["working-directory"] == "."
    for name, prefix in (("Check formatting", ("cargo", "fmt")),
                         ("Check all targets", ("cargo", "check")),
                         ("Clippy", ("cargo", "clippy")),
                         ("Nextest", ("cargo", "nextest", "run"))):
        if name == "Check formatting":
            require_command(commands(rust[name]["run"]), prefix, ("--all", "--check"))
            continue
        for no_defaults in (False, True):
            label = name + (" without default features" if no_defaults else "")
            required = ("--locked", "--all-targets") + (("--no-default-features",) if no_defaults else ())
            forbidden = ("--features", "--all-features") if no_defaults else ("--no-default-features",)
            require_command(commands(rust[label]["run"]), prefix, required, forbidden)

    parity_steps = jobs["parity-checks"]["steps"]
    for step in parity_steps:
        for words in commands(step.get("run", "")):
            # Global Cargo options and a toolchain may precede the subcommand.
            cargo = words[1:] if words[0] == "cargo" else []
            cargo = cargo[:cargo.index("--")] if "--" in cargo else cargo
            assert "build" not in cargo, step.get("name")
    parity = {step.get("name"): step for step in parity_steps}
    # No job compares against a live Python oracle since the freeze.
    from inventory import live_invocations
    assert not live_invocations({"jobs": jobs}), live_invocations({"jobs": jobs})
    for name in RETIRED_STEPS:
        assert name not in parity and name not in rust, name
    check_w1c_shards(jobs)
    sent_yaml = parity["Compare sent YAML startup with PyYAML"]
    require_command(commands(sent_yaml["run"]), ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_sent_yaml.py",
                     "rust/daemon/harness/test_sent_yaml_mutants.py"), PYTEST_FILTERS)
    assert 'PSEUDOLIFE_SENT_YAML_BIN' in sent_yaml["run"]
    assert 'rust/target/release/pseudolife-stdio' in sent_yaml["run"]
    assert not any(words[0] == "cargo" for words in commands(sent_yaml["run"]))
    search = parity["Offline W2-D search harness regressions"]
    require_command(commands(search["run"]), ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_w2d_controls.py",
                     "rust/daemon/harness/test_w2d_namespace.py",
                     "rust/daemon/harness/test_w2d_recent.py",
                     "rust/daemon/harness/test_config_knobs_golden.py"), PYTEST_FILTERS)
    assert search["timeout-minutes"] == 1
    # The mail, hook, episode and update rows replay their goldens in the
    # required cli shard, and so do lease and lease_operator.
    row_commands(commands(parity["CLI differential harness"]["run"]), ("mail", "hook", "episode", "update"), True)
    row_commands(commands(parity["CLI lease golden harness"]["run"]), ("lease",), True)
    row_commands(commands(parity["CLI lease operator golden harness"]["run"]), ("lease_operator",), True)
    require_command(commands(parity["CLI lease operator golden harness"]["run"]), ("python", "-m", "pytest"),
                    ("rust/cli_harness/test_lease_operator.py", "rust/cli_harness/test_lease_transport.py"),
                    PYTEST_FILTERS)
    require_command(commands(parity["CLI differential harness"]["run"]),
                    ("python", "-m", "pytest"),
                    ("rust/cli_harness/test_cli_harness.py", "rust/cli_harness/test_lease.py",
                     "rust/cli_harness/test_bank.py", "rust/cli_harness/test_backup.py",
                     "rust/cli_harness/test_update_transactions.py",
                     "rust/cli_harness/test_update_wrappers_native.py"), PYTEST_FILTERS)
    backup_proofs = parity["CLI differential harness"]["run"]
    assert '$env:PSEUDOLIFE_RUST_BINARY = $candidate' in backup_proofs
    assert "$env:PSEUDOLIFE_BACKUP_NATIVE_REQUIRED = '1'" in backup_proofs
    require_command(commands(parity["Prepare disposable PostgreSQL for CLI lease row"]["run"]),
                    ("python", "rust/cli_harness/lease_ci.py"))
    background = commands(parity["Daemon background pruning golden"]["run"])
    require_command(background, ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_background_harness.py",), PYTEST_FILTERS)
    require_command(background, ("python", "rust/daemon/harness/run.py", "golden"),
                    ("--rust-bin", "$candidate", "--only", "sweep-pruning", "--no-refusals"),
                    ("--record", "--mutants"))

    static = commands(parity["Daemon static HTTP golden"]["run"])
    require_command(static, ("python", "rust/daemon/harness/run.py", "golden"),
                    ("--rust-bin", "--only", "static-build", "static-paths", "static-missing", "static-root-link", "--out"),
                    ("--record", "--mutants"))
    require_command(static, ("python", "-m", "pytest"), ("rust/daemon/harness/test_static.py",), PYTEST_FILTERS)
    assert not any(words[0] == "cargo" for words in static)
    body = commands(parity["Daemon body admission golden"]["run"])
    require_command(body, ("python", "rust/daemon/harness/run.py", "golden"),
                    ("--rust-bin", "--only", "body-limits", "body-view-open", "body-pair-budget", "body-text-window", "--out"),
                    ("--record", "--mutants"))
    require_command(body, ("python", "-m", "pytest"), ("rust/daemon/harness/test_body_cases.py",), PYTEST_FILTERS)
    assert not any(words[0] == "cargo" for words in body)
    security = commands(parity["Daemon security admission golden"]["run"])
    require_command(security, ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_security_cases.py",
                     "rust/daemon/harness/test_principals_settle.py"), PYTEST_FILTERS)
    security_rows = ("security-open", "security-closed", "security-encodings",
                     "security-encoding-priority", "security-terminal-byte")
    require_command(security, ("python", "rust/daemon/harness/run.py", "golden"),
                    ("--rust-bin", "$candidate", "--only", "--out") + security_rows, ("--record",))
    assert not any(words[0] == "cargo" for words in security)
    # The security properties no Rust test asserts yet keep their live
    # comparison (rust/ci/inventory.py LIVE_SECURITY_EXCEPTIONS).
    kept = commands(parity["Daemon security live checks without a Rust test"]["run"])
    require_command(kept, ("python", "rust/daemon/harness/run.py", "live"),
                    ("--rust-bin", "$candidate", "--only", "security-refusals", "--out"), ("--record", "--no-refusals"))
    require_command(kept, ("python", "rust/daemon/harness/run.py", "live"),
                    ("--rust-bin", "$bindFixture", "--only", "security-remote-open", "security-remote-auth", "--out"),
                    ("--record",))
    override = commands(rust["Daemon override build configurations"]["run"])
    for release in (False, True):
        required = ("--locked", "-p", "pseudolife-daemon", "mutants::tests::loopback_bind_override_is_absent_from_production_build", "--exact")
        required += ("--release", "--features", "mutants") if release else ()
        require_command(override, ("cargo", "+1.94.0", "test"), required, () if release else ("--release", "--features"))

    schema = commands(parity["Daemon schema startup parity"]["run"])
    # Both run the storage test executable the fixtures job built.
    require_command(schema, ("python", "rust/daemon/harness/schema_ci.py"),
                    ("--out", "--no-build", "--rust-test-bin", "$storageTest"), ("--record-goldens", "--build-only"))
    assert not any(words[0] == "cargo" for words in schema)
    startup = commands(parity["Daemon resident startup parity"]["run"])
    require_command(startup, ("python", "rust/daemon/harness/startup_ci.py"),
                    ("--out", "--no-build", "--rust-test-bin", "--part"), ("--record-goldens",))
    assert not any(words[0] == "cargo" for words in startup)
    for name in ("Daemon schema startup parity", "Daemon resident startup parity"):
        assert "Join-Path 'rust/target/storage-test' $testName" in parity[name]["run"], name
    embedding = parity["Offline embedding golden and mutant row"]
    invocations = commands(embedding["run"])
    require_command(invocations, ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_embedding.py",), PYTEST_FILTERS)
    require_command(invocations, ("python", "rust/daemon/harness/run.py", "embedding"),
                    ("--fixture", "--golden", "rust/daemon/harness/goldens/embedding-fixture.json", "--rust-bin", "--out"),
                    ("--record", "--model", "--mutant"))
    assert "rust/target/release" in embedding["run"]
    assert not any(words[:2] == ["cargo", "build"] for words in invocations)

    principal = parity["Principal store golden harness"]
    invocations = commands(principal["run"])
    require_command(invocations, ("python", "rust/daemon/harness/principals.py", "golden"),
                    ("--rust-bin", "--out"), ("--record", "--mutants"))
    lines = [line.strip() for line in principal["run"].splitlines()]
    assert [line for line in lines if re.match(r"\$binaryName\s*=", line)] == [
        "$binaryName = if ($IsWindows) { 'pseudolife-daemon.exe' } else { 'pseudolife-daemon' }"]
    assert [line for line in lines if re.match(r"\$candidate\s*=", line)] == [
        "$candidate = (Resolve-Path (Join-Path 'rust/target/release' $binaryName)).Path"]
    for words in invocations:
        if words[:2] == ["python", "rust/daemon/harness/principals.py"]:
            assert words.count("--rust-bin") == 1
            assert words[words.index("--rust-bin") + 1] == "$candidate"
    download = next(step for step in parity_steps
                    if step.get("uses") == "actions/download-artifact@v4")
    assert download["with"] == {"name": "rust-shim-${{ runner.os }}", "path": "rust/target/release"}
    assert parity_steps.index(download) < parity_steps.index(principal)
    assert not any(words[:2] == ["cargo", "build"] for words in invocations)
    daemon_build = next(step for step in jobs["candidate-harness"]["steps"]
                        if step.get("name") == "Build embedding candidate")
    invocation = next(words for words in commands(daemon_build["run"])
                      if words[:2] == ["cargo", "build"])
    assert {"mutants", "principal-harness"} <= set(
        invocation[invocation.index("--features") + 1].split(","))
    steps = jobs["parity-checks"]["steps"]
    assert steps.index(parity["Prepare disposable PostgreSQL for CLI lease row"]) < steps.index(principal)
    upload = next(step for step in steps if step.get("name") == "Retain principal store outcomes")
    assert upload["if"] == "always() && matrix.suite == 'cli-store'"
    graph = commands(parity["Graph store golden harness"]["run"])
    row_parser = argparse.ArgumentParser(add_help=False)
    row_parser.add_argument("--row", choices=["store", "read"], default="store")
    for mode in ("golden",):
        for row in ("store", "read"):
            matches = [words for words in graph if words[:3] ==
                       ["python", "rust/daemon/harness/graph_store.py", mode]]
            # Match argparse's default, equals form and last-selector-wins rule.
            matches = [words for words in matches if
                       row_parser.parse_known_args(words[3:])[0].row == row]
            require_command(matches, ("python", "rust/daemon/harness/graph_store.py", mode),
                            ("--candidate", "--out"), ("--record",))
    require_command(graph, ("python", "rust/daemon/harness/gen_graph_unicode.py", "--check"))
    require_command(graph, ("python", "rust/daemon/harness/test_graph_store_harness.py"))
    require_command(graph, ("python", "rust/daemon/harness/test_graph_read_compare.py"))


def workflow():
    """rust.yml as the nightly runs it: the scope-chosen parity matrix
    expanded to every suite (rust/ci/parity_scope.py; test_parity_scope.py
    pins the expressions and the per-pull-request selection)."""
    from parity_scope import resolve
    return resolve(yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))



def test_shards_cover_both_systems_and_build_once():
    jobs = workflow()["jobs"]
    for name in ("rust", "candidate", "parity-checks", "parity"):
        assert jobs[name]["strategy"]["matrix"]["os"] == SYSTEMS
        assert jobs[name]["strategy"]["fail-fast"] is False
        assert not jobs[name].get("continue-on-error", False)
    assert jobs["rust"]["name"] == "Rust / ${{ matrix.os }}"
    assert jobs["parity"]["name"] == "Parity / ${{ matrix.os }}"
    assert jobs["parity-checks"]["needs"] == ["scope", "candidate", "candidate-harness", "fixtures", "parity-pool"]
    rust_steps = jobs["rust"]["steps"]
    override = commands(next(step["run"] for step in rust_steps
                             if step.get("name") == "Daemon override build configurations"))
    require_command(override, ("cargo", "+1.94.0", "build"),
                    ("--locked", "-p", "pseudolife-daemon", "--features", "mutants"), ("--release",))
    # The fixtures job builds the bind fixture with the override step's
    # command; the Rust job keeps building it as part of that check.
    uploads = [step for step in rust_steps if step.get("uses") == "actions/upload-artifact@v4"]
    assert len(uploads) == 1
    assert uploads[0]["name"] == "Retain ordinary PR parity precheck case IDs"
    assert uploads[0]["with"]["path"] == "${{ runner.temp }}/pr-precheck-public/**/*.json"
    assert uploads[0]["if"] == "always() && steps.precheck.outputs.enabled == 'true'"
    fixtures = jobs["fixtures"]
    assert fixtures["strategy"]["matrix"]["os"] == SYSTEMS
    assert fixtures["strategy"]["fail-fast"] is False
    assert fixtures["name"] == "Fixtures / ${{ matrix.os }}"
    fixture_steps = {step.get("name"): step for step in fixtures["steps"]}
    require_command(commands(fixture_steps["Build the debug bind fixture"]["run"]), ("cargo", "+1.94.0", "build"),
                    ("--locked", "-p", "pseudolife-daemon", "--features", "mutants"), ("--release",))
    require_command(commands(fixture_steps["Build the daemon storage test executable"]["run"]),
                    ("python", "rust/daemon/harness/schema_ci.py", "--build-only"), ("--test-bin-out",))
    produced = fixture_steps["Retain debug bind fixture"]
    consumed = next(step for step in jobs["parity-checks"]["steps"]
                    if step.get("name") == "Download debug bind fixture")
    assert produced["with"]["name"] == consumed["with"]["name"] == "daemon-bind-fixture-${{ runner.os }}"
    assert produced["with"]["if-no-files-found"] == "error"
    assert "rust/target/debug/pseudolife-daemon" in produced["with"]["path"].splitlines()
    # Only the kept live security checks bind with it since the freeze.
    assert consumed["if"] == "matrix.suite == 'cli-store'"
    assert consumed["with"]["path"] == "rust/target/debug"
    assert jobs["parity-checks"]["strategy"]["matrix"]["suite"] == PARITY_SUITES
    builds = [step for name in ("rust", "candidate", "parity-checks")
              for step in jobs[name]["steps"]
              if "cargo build --locked --release --bin pseudolife-stdio" in step.get("run", "")]
    assert len(builds) == 1
    assert builds[0]["run"].strip() == "cargo build --locked --release --bin pseudolife-stdio --bin pseudolife-daemon -j 3"
    assert not any("schema_ci.py" in step.get("run", "")
                   for step in jobs["candidate"]["steps"])
    stored = fixture_steps["Retain daemon storage test executable"]
    fetched = next(step for step in jobs["parity-checks"]["steps"]
                   if step.get("name") == "Download daemon storage test executable")
    assert stored["with"]["name"] == fetched["with"]["name"] == "daemon-storage-test-${{ runner.os }}"
    assert stored["with"]["if-no-files-found"] == "error"
    assert fetched["if"] == "matrix.suite == 'cli-startup'"
    assert fetched["with"]["path"] == "rust/target/storage-test"
    # Read-only: the Rust job's own key, never saved from here.
    cache = next(step for step in fixtures["steps"] if step.get("uses") == "Swatinem/rust-cache@v2")
    assert cache["with"] == {"prefix-key": WEEKLY_PREFIX, "workspaces": "rust", "shared-key": "rust",
                             "save-if": False}


def test_original_checks_remain_gated_on_the_expected_shards():
    jobs = workflow()["jobs"]
    # Keep an editable check inventory, not a frozen historical workflow:
    # future rows and diagnostic flags may extend a check's command.
    for job, inventory in (("rust", dict.fromkeys(RUST_CHECKS)), ("parity-checks", PARITY_CHECKS)):
        for name, suite in inventory.items():
            matches = [step for step in jobs[job]["steps"] if step.get("name") == name]
            assert len(matches) == 1, name
            actual = matches[0]
            assert actual.get("run", "").strip(), name
            assert not actual.get("continue-on-error", False)
            expected = ("startsWith(matrix.suite, 'w1c-')" if suite == "w1c" else
                        f"startsWith(matrix.suite, '{suite[:-1]}')" if suite and suite.endswith("*") else
                        f"matrix.suite == '{suite}'" if suite else None)
            if name in ("Daemon resident startup parity", "Daemon resident startup parity part 2"):
                expected = "${{ !cancelled() && matrix.suite == 'cli-startup' && steps.startup_setup.outcome == 'success' }}"
            assert actual.get("if") == expected
    check_executable_coverage(jobs)


def test_the_live_comparison_jobs_and_dispatch_modes_are_gone():
    # The Python freeze (14 November 2026): the frozen judge probes, the
    # Windows live job, the nightly body evidence and the wait-mail capture
    # compared the candidate with a live Python oracle, and left CI.
    raw = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for name in RETIRED_JOBS:
        assert name not in raw["jobs"], name
    inputs = raw[True]["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["options"] == ["ci", "stress"]
    assert not {"case_list", "frozen_head"} & set(inputs)


def test_artifact_is_from_this_run_and_executable_on_linux():
    job = workflow()["jobs"]["parity-checks"]
    download = next(s for s in job["steps"] if s.get("uses") == "actions/download-artifact@v4")
    assert download["with"] == {"name": "rust-shim-${{ runner.os }}", "path": "rust/target/release"}
    permission = next(s for s in job["steps"] if s.get("name") == "Restore executable permission")
    assert permission["if"] == "runner.os == 'Linux'"
    assert shlex.split(permission["run"]) == ["chmod", "+x",
        "rust/target/release/pseudolife-stdio", "rust/target/release/pseudolife-daemon",
        "rust/target/release/pseudolife-daemon-static"]
    candidate = workflow()["jobs"]["candidate"]
    harness = workflow()["jobs"]["candidate-harness"]
    fetched = next(s for s in job["steps"] if s.get("name") == "Download harness candidate")
    assert fetched["with"] == {"name": "rust-shim-harness-${{ runner.os }}", "path": "rust/target/release"}
    assert job["steps"].index(download) < job["steps"].index(fetched) < job["steps"].index(permission)
    build = next(s for s in harness["steps"] if s.get("name") == "Build embedding candidate")
    require_command(commands(build["run"]), ("cargo", "build"),
                    ("--locked", "--release", "-p", "pseudolife-daemon", "--bins", "--features"))
    invocation = next(words for words in commands(build["run"]) if words[:2] == ["cargo", "build"])
    assert "mutants" in invocation[invocation.index("--features") + 1].split(",")
    harness_upload = next(s for s in harness["steps"] if s.get("uses") == "actions/upload-artifact@v4")
    assert harness_upload["with"]["name"] == fetched["with"]["name"]
    assert {"rust/target/release/pseudolife-daemon", "rust/target/release/pseudolife-daemon.exe"} <= set(harness_upload["with"]["path"].splitlines())
    upload = next(s for s in candidate["steps"] if s.get("uses") == "actions/upload-artifact@v4")
    # Both artifacts download into one directory: the default daemon ships
    # only as -static, so the mutants build is the one pseudolife-daemon.
    assert not {"rust/target/release/pseudolife-daemon", "rust/target/release/pseudolife-daemon.exe"} & set(upload["with"]["path"].splitlines())
    preserved = next(s for s in candidate["steps"] if s.get("name") == "Retain default static candidate")
    assert preserved.get("working-directory") == "${{ github.workspace }}"
    names = [s.get("name") for s in candidate["steps"]]
    assert names.index("Build candidate") < names.index("Retain default static candidate")
    assert "Build embedding candidate" not in names
    assert {"rust/target/release/pseudolife-daemon-static", "rust/target/release/pseudolife-daemon-static.exe"} <= set(upload["with"]["path"].splitlines())
    assert "Copy-Item -LiteralPath" in preserved["run"]
    static = next(s for s in job["steps"] if s.get("name") == "Daemon static HTTP golden")
    assert "'pseudolife-daemon-static.exe'" in static["run"]
    assert "'pseudolife-daemon-static'" in static["run"]

    background = next(s for s in job["steps"] if s.get("name") == "Daemon background pruning golden")
    assert "Join-Path 'rust/target/release' $binaryName" in background["run"]

def test_daemon_artifact_is_built_once_and_shared_with_graph():
    jobs = workflow()["jobs"]
    builds = [step for job in jobs.values() for step in job.get("steps", [])
              if "cargo build" in step.get("run", "") and
              "-p pseudolife-daemon" in step["run"]]
    assert len(builds) == 1
    assert builds[0] in jobs["candidate-harness"]["steps"]
    words = commands(builds[0]["run"])[0]
    assert {"--locked", "--release", "--bins"} <= set(words)
    assert {"graph-harness", "mutants"} <= set(words[words.index("--features") + 1].split(","))
    upload = next(s for s in jobs["candidate-harness"]["steps"]
                  if s.get("with", {}).get("name") == "rust-shim-harness-${{ runner.os }}")
    for binary in ("pseudolife-daemon", "graph-contract"):
        for suffix in ("", ".exe"):
            assert f"rust/target/release/{binary}{suffix}" in upload["with"]["path"].splitlines()
    steps = jobs["parity-checks"]["steps"]
    download = next(s for s in steps if s.get("uses") == "actions/download-artifact@v4"
                    and s.get("with", {}).get("name") == upload["with"]["name"])
    assert "if" not in download
    assert download["with"]["path"] == "rust/target/release"
    permission = next(s for s in steps if s.get("name") == "Restore graph store executable permission")
    assert permission["if"] == "matrix.suite == 'cli-store' && runner.os == 'Linux'"
    assert permission["run"] == "chmod +x rust/target/release/graph-contract"


GATE_NEEDS = ["scope", "rust", "candidate", "parity-checks", "source-mutants"]
# The gate's first step fails when a job it needs was cancelled, and its
# verdict step then skips, so the failed step itself says a cancel caused it
# (ops/integrate.py reads that to tell a cancel from a failure).
CANCELLED_IF = "${{ contains(needs.*.result, 'cancelled') }}"
CANCELLED_STEP = {"name": "A needed job was cancelled", "if": CANCELLED_IF, "run": "exit 1"}
VERDICT_IF = "${{ !contains(needs.*.result, 'cancelled') }}"
# The schedules: weekday nightlies, and the Sunday weekly run that also runs
# the CLI source mutants (and so must not skip them).
NIGHTLY = "23 3 * * 1-6"
WEEKLY = "23 3 * * 0"


def gate_steps(jobs=None):
    """(the cancelled-need step, the verdict step) of the Parity gate."""
    steps = (jobs or workflow()["jobs"])["parity"]["steps"]
    assert len(steps) == 2 and steps[0] == CANCELLED_STEP, steps
    assert steps[1]["if"] == VERDICT_IF
    return steps[0], steps[1]


def run_verdict(needs, event, schedule=WEEKLY):
    import os
    return subprocess.run(["pwsh", "-NoProfile", "-Command", gate_steps()[1]["run"]],
                          env=dict(os.environ, GATE_RESULTS=json.dumps(needs), GATE_EVENT=event,
                                   GATE_SCHEDULE=schedule if event == "schedule" else ""),
                          capture_output=True, text=True)


def run_gate(needs, event):
    """The gate as GitHub runs it: a cancelled need fails the first step and
    skips the verdict; otherwise the verdict step decides."""
    gate_steps()
    if any(need.get("result") == "cancelled" for need in needs.values()):
        return subprocess.CompletedProcess([], 1, "", "a needed job was cancelled")
    return run_verdict(needs, event)


@pytest.mark.parametrize("event", ["schedule", "push", "pull_request", "merge_group", "workflow_dispatch"])
@pytest.mark.parametrize("output", ["true", "false", "", None])
def test_only_verified_schedules_reuse_push_coverage(event, output):
    needs = {name: {"result": "success"} for name in GATE_NEEDS}
    needs["scope"]["outputs"] = {"push_passed": output}
    for name in ("rust", "parity-checks"):
        needs[name]["result"] = "skipped"
    assert run_gate(needs, event).returncode == (0 if event == "schedule" and output == "true" else 1)


@pytest.mark.parametrize("dependency", GATE_NEEDS)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_reused_push_cannot_hide_a_nightly_failure(dependency, result):
    needs = {name: {"result": "success"} for name in GATE_NEEDS}
    needs["scope"]["outputs"] = {"push_passed": "true"}
    for name in ("rust", "parity-checks"):
        needs[name]["result"] = "skipped"
    needs[dependency]["result"] = result
    allowed = dependency in ("rust", "parity-checks") and result == "skipped"
    assert run_gate(needs, "schedule").returncode == (0 if allowed else 1)


@pytest.mark.parametrize("dependency", GATE_NEEDS)
@pytest.mark.parametrize("event", ["pull_request", "merge_group", "push", "schedule",
                                   "workflow_dispatch"])
@pytest.mark.parametrize("base", ["all-success", "docs-only", "ordinary-pr"])
def test_a_cancelled_need_fails_the_gate_as_the_verdict_alone_did(dependency, event, base):
    # The cancelled-need step changes which step fails, not the gate's
    # result: the verdict step alone also fails a cancelled need. The one
    # exception is `scope` cancelled while every other need passed, which
    # the verdict never checked (it reads only scope's outputs) and the gate
    # now fails: reachable only when `scope` alone ran past its timeout,
    # since its dependents run under `!cancelled()`.
    needs = copy.deepcopy({"all-success": {n: {"result": "success"} for n in GATE_NEEDS},
                           "docs-only": DOCS_ONLY_SKIP, "ordinary-pr": ORDINARY_PR_SKIP}[base])
    needs[dependency]["result"] = "cancelled"
    stricter = dependency == "scope" and base == "all-success"
    assert run_verdict(needs, event).returncode == (0 if stricter else 1)
    assert run_gate(needs, event).returncode == 1


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", ""])
@pytest.mark.parametrize("dependency", ["parity-checks", "source-mutants"])
@pytest.mark.parametrize("event", ["pull_request", "merge_group", "push", "schedule", "workflow_dispatch"])
def test_required_parity_gate_fails_closed(result, dependency, event):
    gate = workflow()["jobs"]["parity"]
    assert gate["needs"] == GATE_NEEDS
    assert gate["if"].startswith("always() &&")
    step = gate_steps()[1]
    assert step["env"] == {"GATE_RESULTS": "${{ toJSON(needs) }}", "GATE_EVENT": "${{ github.event_name }}",
                           "GATE_SCHEDULE": "${{ github.event.schedule }}"}
    needs = {name: {"result": "success"} for name in gate["needs"]}
    needs[dependency]["result"] = result
    # On the Sunday weekly run (the schedule run_verdict passes by default).
    executed = run_verdict(needs, event)
    accepted = result == "success" or (event in ("pull_request", "merge_group", "push")
                                       and dependency == "source-mutants" and result == "skipped")
    assert executed.returncode == (0 if accepted else 1), executed.stderr


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", ""])
def test_a_weekday_nightly_may_skip_only_the_source_mutants(result):
    # Source mutants are weekly since the Python freeze: a weekday nightly
    # skips them, but never fails or cancels them, and skips nothing else.
    needs = {name: {"result": "success"} for name in GATE_NEEDS}
    needs["source-mutants"]["result"] = result
    accepted = result in ("success", "skipped")
    assert run_verdict(needs, "schedule", NIGHTLY).returncode == (0 if accepted else 1)
    assert run_verdict(needs, "schedule", WEEKLY).returncode == (0 if result == "success" else 1)
    needs["source-mutants"]["result"] = "skipped"
    for other in ("rust", "candidate", "parity-checks"):
        skipped = copy.deepcopy(needs)
        skipped[other]["result"] = "skipped"
        assert run_verdict(skipped, "schedule", NIGHTLY).returncode == 1, other


@pytest.mark.parametrize("event", ["push", "pull_request", "merge_group", "schedule", "workflow_dispatch"])
@pytest.mark.parametrize("parity_any", ["false", "true", "", None])
def test_only_a_push_with_no_parity_pairs_left_skips_the_parity_checks(event, parity_any):
    # A master push whose merged batch passed every pair gets an empty matrix
    # (parity_scope's parity_any=false), so parity-checks is skipped.
    needs = {name: {"result": "success"} for name in GATE_NEEDS}
    needs["scope"]["outputs"] = {"parity_any": parity_any}
    needs["parity-checks"]["result"] = "skipped"
    if event != "push":
        needs["source-mutants"]["result"] = "success"
    accepted = event == "push" and parity_any == "false"
    assert run_verdict(needs, event).returncode == (0 if accepted else 1)
    # It never excuses a failed or cancelled parity job, nor a skipped Rust job.
    needs["parity-checks"]["result"] = "failure"
    assert run_verdict(needs, event).returncode == 1
    needs["parity-checks"]["result"] = "skipped"
    needs["rust"]["result"] = "skipped"
    assert run_verdict(needs, event).returncode == 1


DOCS_ONLY_SKIP = {"scope": {"result": "success", "outputs": {"rust": "false"}},
                  "rust": {"result": "skipped"}, "candidate": {"result": "skipped"},
                  "parity-checks": {"result": "skipped"}, "source-mutants": {"result": "skipped"}}


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_parity_gate_accepts_a_documentation_only_pull_request_or_merge_group(event):
    executed = run_gate(DOCS_ONLY_SKIP, event)
    assert executed.returncode == 0, executed.stderr


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_parity_gate_accepts_the_docs_skip_only_on_pull_requests(event):
    assert run_gate(DOCS_ONLY_SKIP, event).returncode == 1


@pytest.mark.parametrize("scope", [
    {"result": "success", "outputs": {"rust": "true"}},
    {"result": "success", "outputs": {}},
    {"result": "failure", "outputs": {"rust": "false"}},
    {"result": "skipped", "outputs": {}},
    {"result": "cancelled", "outputs": {"rust": "false"}},
])
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_parity_gate_rejects_skips_scope_did_not_choose(scope, event):
    needs = copy.deepcopy(DOCS_ONLY_SKIP)
    needs["scope"] = scope
    assert run_gate(needs, event).returncode == 1


@pytest.mark.parametrize("job", ["rust", "candidate", "parity-checks"])
@pytest.mark.parametrize("result", ["failure", "cancelled", ""])
def test_parity_gate_still_fails_a_broken_job_on_a_docs_pull_request(job, result):
    needs = copy.deepcopy(DOCS_ONLY_SKIP)
    needs[job]["result"] = result
    assert run_gate(needs, "pull_request").returncode == 1


# An ordinary pull request (not from integrate/*): scope says full=false, the
# Rust job runs and must pass, and the candidate build and parity matrix skip.
ORDINARY_PR_SKIP = {"scope": {"result": "success", "outputs": {"rust": "true", "full": "false"}},
                    "rust": {"result": "success"}, "candidate": {"result": "skipped"},
                    "parity-checks": {"result": "skipped"}, "source-mutants": {"result": "skipped"}}


def test_parity_gate_accepts_an_ordinary_pull_request_without_parity():
    executed = run_gate(ORDINARY_PR_SKIP, "pull_request")
    assert executed.returncode == 0, executed.stderr


@pytest.mark.parametrize("event", ["merge_group", "push", "schedule", "workflow_dispatch"])
def test_parity_gate_accepts_the_fast_lane_skip_only_on_pull_requests(event):
    assert run_gate(ORDINARY_PR_SKIP, event).returncode == 1


@pytest.mark.parametrize("scope", [
    {"result": "success", "outputs": {"rust": "true", "full": "true"}},
    {"result": "success", "outputs": {"rust": "true"}},
    {"result": "failure", "outputs": {"rust": "true", "full": "false"}},
    {"result": "skipped", "outputs": {}},
    {"result": "cancelled", "outputs": {"full": "false"}},
])
def test_parity_gate_rejects_a_fast_lane_skip_scope_did_not_choose(scope):
    needs = copy.deepcopy(ORDINARY_PR_SKIP)
    needs["scope"] = scope
    assert run_gate(needs, "pull_request").returncode == 1


@pytest.mark.parametrize("job, result", [("rust", "skipped"), ("rust", "failure"),
                                         ("rust", "cancelled"), ("candidate", "failure"),
                                         ("candidate", "cancelled"), ("parity-checks", "failure"),
                                         ("parity-checks", "")])
def test_parity_gate_still_requires_the_rust_job_on_an_ordinary_pull_request(job, result):
    # Only candidate and parity-checks may skip; the Rust job is the fast lane.
    needs = copy.deepcopy(ORDINARY_PR_SKIP)
    needs[job]["result"] = result
    assert run_gate(needs, "pull_request").returncode == 1


BATCH_REF = "integrate/2026-10-21-batch-1"


def run_scope(tmp_path, event, files, git_exit=0, base_ref="refs/heads/master", fetch_exit=0,
              head_ref=None):
    """The scope step's outputs, in order. `head_ref` is github.head_ref, set
    by Actions only for pull requests: by default a batch branch's, which
    runs everything its diff needs."""
    import os
    if head_ref is None:
        head_ref = BATCH_REF if event == "pull_request" else ""
    step = next(s for s in workflow()["jobs"]["scope"]["steps"] if s.get("id") == "scope")
    assert step["env"] == {"EVENT_NAME": "${{ github.event_name }}",
                           "MERGE_GROUP_BASE_REF": "${{ github.event.merge_group.base_ref }}",
                           "HEAD_REF": "${{ github.head_ref }}"}
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    # Stand in for `git fetch` and `git diff`, the step's only external inputs.
    # A pull request diffs its merge commit against the base parent; a merge
    # group fetches master's tip and diffs that against the group head, so
    # every unmerged queue entry counts whatever merge_group.base_sha names.
    bootstrap = r'''
function git {
    $line = $args -join ' '
    if ($env:EVENT_NAME -ceq 'merge_group' -and $line -ceq 'fetch --no-tags --depth=1 origin refs/heads/master') {
        $global:LASTEXITCODE = [int]$env:PL_FIXTURE_FETCH_EXIT
        return
    }
    $base = if ($env:EVENT_NAME -ceq 'merge_group') { 'FETCH_HEAD' } else { 'HEAD^1' }
    # Renames must list both paths, or rust/x.md -> docs/x.md reads as docs.
    if ($line -cne "diff --no-renames --name-only $base HEAD") { throw "unexpected git $args" }
    $global:LASTEXITCODE = [int]$env:PL_FIXTURE_GIT_EXIT
    if ($env:PL_FIXTURE_FILES) { $env:PL_FIXTURE_FILES -split "`n" }
}
'''
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", bootstrap + step["run"]],
                            env=dict(os.environ, EVENT_NAME=event, GITHUB_OUTPUT=str(output),
                                     HEAD_REF=head_ref,
                                     MERGE_GROUP_BASE_REF=base_ref, PL_FIXTURE_FETCH_EXIT=str(fetch_exit),
                                     PL_FIXTURE_FILES="\n".join(files), PL_FIXTURE_GIT_EXIT=str(git_exit)),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return output.read_text(encoding="utf-8-sig").split()


DOCS_ONLY_CHANGES = [
    ["CLAUDE.md"],
    ["README.md", "CHANGELOG.md", "changelog.d/2026-10-17-x.md"],
    ["docs/guide/configuration.md", "docs/atlas/atlas.json", "llms.txt", "llms-full.txt"],
    ["examples/CLAUDE.memory.md", "frontend/README.md", "README.zh-CN.md"],
    ["docs/images/diagram.svg"],
    # The port's plan pages no Rust or parity job reads (parity_scope.RUST_PLAN_DOCS).
    ["rust/CUTOVER.md"],
    ["rust/CUTOVER.md", "rust/DEV.md", "rust/SEMANTICS-CHECKLIST.md", "CLAUDE.md"],
]
CODE_CHANGES = [
    ["CLAUDE.md", "rust/README.md"],
    ["rust/daemon/src/main.rs"],
    ["evals/rust_port/README.md"],
    ["evals/README.md"],
    ["pseudolife_memory/README.md"],
    ["plugin/README.md"],
    ["plugin/hooks/session-start.sh"],
    ["ops/README.md"],
    ["tests/README.md"],
    [".github/workflows/rust.yml"],
    [".github/PULL_REQUEST_TEMPLATE.md"],
    ["pyproject.toml"],
    ["README.md", "pseudolife_memory/service.py"],
    ["docs/helper.py"],
    ["docs/atlas/atlas.js"],
    ["docs/run.ps1"],
    ["docs/setup.sh"],
    ["notes.txt"],
    ["src.md.py"],
    ["RUST/README.md"],
    # Near misses of the plan pages: one a job reads, another directory, case,
    # suffix, and a plan page beside code.
    ["rust/PARITY.md"],
    ["rust/PORTING.md"],
    ["rust/PORT-STATE.md"],
    ["rust/daemon/CUTOVER.md"],
    ["RUST/CUTOVER.md"],
    ["rust/cutover.md"],
    ["rust/CUTOVER.md.txt"],
    ["rust/CUTOVER.md", "rust/daemon/src/main.rs"],
    ["Rust/daemon/notes.md"],
    ["Plugin/skills/x/SKILL.md"],
    ['"docs/caf\\303\\251.md"'],
]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("files", DOCS_ONLY_CHANGES)
def test_scope_skips_a_documentation_only_pull_request(tmp_path, files, event):
    assert run_scope(tmp_path, event, files) == ["full=true", "rust=false"]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("files", CODE_CHANGES)
def test_scope_runs_everything_for_any_other_change(tmp_path, files, event):
    assert run_scope(tmp_path, event, files) == ["full=true", "rust=true"]


def test_scope_runs_everything_for_a_mixed_merge_group(tmp_path):
    # A docs PR and a code PR queued together: the group is not docs-only.
    group = ["CLAUDE.md", "docs/guide/configuration.md", "rust/daemon/src/main.rs"]
    assert run_scope(tmp_path, "merge_group", group) == ["full=true", "rust=true"]


@pytest.mark.parametrize("base_ref, fetch_exit", [
    ("", 0), ("master", 0), ("refs/heads/topic", 0), ("refs/heads/master2", 0),
    ("REFS/HEADS/MASTER", 0), ("refs/heads/master", 128)])
def test_scope_runs_everything_without_a_fetched_master_tip(tmp_path, base_ref, fetch_exit):
    assert run_scope(tmp_path, "merge_group", ["CLAUDE.md"], base_ref=base_ref,
                     fetch_exit=fetch_exit) == ["full=true", "rust=true"]


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_scope_never_skips_outside_pull_requests(tmp_path, event):
    assert run_scope(tmp_path, event, ["CLAUDE.md"]) == ["full=true", "rust=true"]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("files, git_exit", [([], 0), (["CLAUDE.md"], 128)])
def test_scope_runs_everything_on_an_empty_or_unreadable_diff(tmp_path, files, git_exit, event):
    assert run_scope(tmp_path, event, files, git_exit) == ["full=true", "rust=true"]


# Ordinary pull requests skip the candidate build and the parity matrix (their
# integration batch's integrate/* pull request runs them) but keep the Rust job.
ORDINARY_REFS = ["claude/some-change", "fix/x", "integrate", "integrated/x",
                 "Integrate/2026-10-21-batch-1", "x/integrate/y", "master"]


@pytest.mark.parametrize("head_ref", ORDINARY_REFS)
@pytest.mark.parametrize("files, rust", [(["rust/daemon/src/main.rs"], "true"),
                                         (["pseudolife_memory/service.py"], "true"),
                                         (["CLAUDE.md"], "false")])
def test_scope_marks_an_ordinary_pull_request_not_full(tmp_path, head_ref, files, rust):
    # `rust` is still classified by the diff, so the Rust job runs for code.
    assert run_scope(tmp_path, "pull_request", files, head_ref=head_ref) == ["full=false", f"rust={rust}"]


@pytest.mark.parametrize("ci_input", [".github/workflows/rust.yml", ".github/workflows/ci.yml",
                                      "pyproject.toml", "ops/requirements.lock.txt",
                                      "rust/Cargo.lock", "frontend/package-lock.json",
                                      "rust/rust-toolchain.toml"])
def test_scope_keeps_an_ordinary_pull_request_changing_ci_inputs_fast(tmp_path, ci_input):
    # Only batches spend the heavy lanes (maintainer, 2026-10-29). A workflow
    # or dependency edit runs its full lanes on its batch's head, under the
    # edited workflow, before it can reach master.
    assert run_scope(tmp_path, "pull_request", ["rust/daemon/src/main.rs", ci_input],
                     head_ref="claude/some-change") == ["full=false", "rust=true"]


@pytest.mark.parametrize("files, git_exit", [([], 0), (["rust/daemon/src/main.rs"], 128)])
def test_scope_keeps_an_ordinary_pull_request_with_an_unreadable_diff_full(tmp_path, files,
                                                                          git_exit):
    assert run_scope(tmp_path, "pull_request", files, git_exit,
                     head_ref="claude/some-change") == ["full=true", "rust=true"]


@pytest.mark.parametrize("head_ref", [BATCH_REF, "integrate/2026-10-07-rust-modes", ""])
def test_scope_runs_everything_for_a_batch_or_unnamed_pull_request(tmp_path, head_ref):
    # An empty head ref (Actions did not supply one) fails safe to full.
    assert run_scope(tmp_path, "pull_request", ["rust/daemon/src/main.rs"],
                     head_ref=head_ref) == ["full=true", "rust=true"]


@pytest.mark.parametrize("event", ["merge_group", "push", "schedule", "workflow_dispatch"])
def test_scope_reads_the_head_ref_only_for_pull_requests(tmp_path, event):
    assert run_scope(tmp_path, event, ["rust/daemon/src/main.rs"],
                     head_ref="claude/some-change") == ["full=true", "rust=true"]


def concurrency_group(event, ref="refs/heads/master", mode=None, run_id="101", head_ref=""):
    """Evaluate the workflow's group expression the way Actions does."""
    expression = workflow()["concurrency"]["group"]
    assert expression.startswith("${{ ") and expression.endswith(" }}")
    body = expression[4:-3]
    # Actions' && / || return an operand, like Python's and / or.
    assert set(re.findall(r"[a-z_.]+(?=\s*[!=]=)", body)) <= {"github.event_name",
                                                             "github.head_ref"}
    body = body.replace("&&", " and ").replace("||", " or ")
    body = re.sub(r"!(?=[A-Za-z])", " not ", body)
    body = body.replace("github.event_name", "event").replace("github.head_ref", "head_ref")
    body = body.replace("github.run_id", "run_id").replace("github.workflow", "'Rust shim'")
    body = body.replace("github.ref", "ref")
    import ast
    tree = ast.parse(body, mode="eval")
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.NotEq,
               ast.UnaryOp, ast.Not, ast.Name, ast.Load, ast.Constant, ast.Call)
    # Only comparisons, and/or/not and format()/startsWith() calls.
    assert all(isinstance(node, allowed) for node in ast.walk(tree)), body
    assert all(node.func.id in ("format", "startsWith")
               for node in ast.walk(tree) if isinstance(node, ast.Call))
    return eval(compile(tree, "<concurrency>", "eval"),
                {"__builtins__": {}, "format": lambda text, *args: text.format(*args),
                 # Actions' startsWith ignores case.
                 "startsWith": lambda text, prefix: text.lower().startswith(prefix.lower())},
                {"event": event, "mode": mode, "run_id": run_id, "ref": ref,
                 "head_ref": head_ref})


def test_only_an_ordinary_pull_request_supersedes_its_own_runs():
    # A newer push to an ordinary pull request cancels its older run, per PR.
    pr1 = concurrency_group("pull_request", "refs/pull/1/merge", head_ref="fix/x", run_id="1")
    assert pr1 == concurrency_group("pull_request", "refs/pull/1/merge", head_ref="fix/x",
                                    run_id="2")
    assert pr1 != concurrency_group("pull_request", "refs/pull/2/merge", head_ref="fix/y")
    assert workflow()["concurrency"]["cancel-in-progress"] is True
    # Every other run has a group of its own, so nothing cancels it (a newer
    # run in a group cancels a pending one even without cancel-in-progress):
    # master pushes, the nightly, manual CI, wait-mail captures, stress
    # loops, merge groups and integrate/* batch runs.
    queued = "refs/heads/gh-readonly-queue/master/pr-830-" + "a" * 40
    for kwargs in ({"event": "push"}, {"event": "schedule"},
                   {"event": "workflow_dispatch", "mode": "ci"},
                   {"event": "workflow_dispatch", "mode": "wait-mail"},
                   {"event": "workflow_dispatch", "mode": "stress"},
                   {"event": "merge_group", "ref": queued},
                   {"event": "pull_request", "ref": "refs/pull/3/merge",
                    "head_ref": "integrate/2026-10-29-batch-1"},
                   {"event": "pull_request", "ref": "refs/pull/3/merge", "head_ref": ""}):
        assert concurrency_group(**kwargs, run_id="1") != concurrency_group(**kwargs, run_id="2")
        assert concurrency_group(**kwargs, run_id="1") != pr1


def test_merge_queue_trigger_and_image_base():
    events = workflow().get("on", workflow().get(True))
    assert "merge_group" in events and "pull_request" in events
    step = next(s for s in workflow()["jobs"]["daemon-image"]["steps"]
                if s.get("id") == "image-inputs")
    assert step["env"]["BASE_SHA"] == ("${{ github.event.pull_request.base.sha || "
                                       "github.event.merge_group.base_sha || github.event.before }}")


def test_image_job_proves_cached_models_without_network_and_retains_result():
    steps = workflow()["jobs"]["daemon-image"]["steps"]
    proof = next(step for step in steps if step.get("name") == "Prove network-disabled model reads after a cold restart")
    provision = next(step for step in steps if step.get("name") == "Prove offline health and authenticated CPU query inference")
    assert steps.index(proof) > steps.index(provision)
    assert proof["if"] == "steps.image-inputs.outputs.needed == 'true'"
    require_command(commands(proof["run"]), ("python", "rust/daemon/harness/image_model_proof.py"),
                    ("--image", "--postgres-image", "--models", "--out"))
    assert "w3j-daemon-rust:prep" in proof["run"]
    assert "pgvector/pgvector:pg18" in proof["run"]
    assert '"$RUNNER_TEMP/daemon-models"' in proof["run"]
    upload = next(step for step in steps if step.get("name") == "Retain network-disabled model proof")
    assert upload["if"] == "always() && steps.image-inputs.outputs.needed == 'true'"
    assert upload["with"]["path"] == "${{ runner.temp }}/image-model-proof.json"
    assert upload["with"]["if-no-files-found"] == "error"


@pytest.mark.parametrize("event, schedule, runs", [
    ("pull_request", "", False), ("merge_group", "", False), ("push", "", False),
    ("schedule", NIGHTLY, False), ("schedule", WEEKLY, True), ("workflow_dispatch", "", True)])
def test_source_mutants_run_weekly_and_on_manual_ci_only(event, schedule, runs):
    from ci_budget import Evaluator, condition_text

    class Event(Evaluator):
        def lookup(self, path):
            if path == ["github", "event", "schedule"]:
                return schedule
            if path == ["inputs", "mode"]:
                return "ci"
            if path == ["needs", "candidate", "result"]:
                return "success"
            return super().lookup(path)

    raw = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert raw[True]["schedule"] == [{"cron": NIGHTLY}, {"cron": WEEKLY}]
    condition = workflow()["jobs"]["source-mutants"]["if"]
    assert condition == ("${{ !cancelled() && ((github.event_name == 'schedule' && github.event.schedule == "
                         f"'{WEEKLY}') || "
                         "(github.event_name == 'workflow_dispatch' && inputs.mode == 'ci')) "
                         "&& needs.candidate.result == 'success' }}")
    assert Event(event, [], set()).run(condition_text(condition)) is runs


def test_scope_gates_every_heavy_job_and_fails_safe():
    jobs = workflow()["jobs"]
    scope = jobs["scope"]
    assert scope["runs-on"] == "ubuntu-latest" and "needs" not in scope
    assert scope["outputs"] == {"rust": "${{ steps.scope.outputs.rust }}",
                                "full": "${{ steps.scope.outputs.full }}",
                                "parity": "${{ steps.parity.outputs.parity }}",
                                "parity_selfhosted": "${{ steps.parity.outputs.selfhosted }}",
                                "batch_passed": "${{ steps.batch.outputs.passed }}",
                                "push_passed": "${{ steps.push.outputs.passed }}",
                                "parity_any": "${{ steps.parity.outputs.parity_any }}",
                                # The Linux pool's exact-case batch ref (test_linux_pool_workflow.py).
                                "integrate": "${{ steps.trusted.outputs.integrate }}"}
    checkout = next(s for s in scope["steps"] if s.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["fetch-depth"] == 2
    gated = ("${{ !cancelled() && (github.event_name != 'workflow_dispatch' || inputs.mode != 'ci' || needs.scope.result == 'success') "
             "&& (github.event_name != 'workflow_dispatch' || %s) "
             "&& needs.scope.outputs.rust != 'false'%s }}")
    # The candidate build also serves a stress loop; the rest is ordinary CI.
    # Only the candidate build (and through it the parity matrix) leaves an
    # ordinary pull request; the Rust job and image check are its fast lanes.
    # The image check also leaves a master push whose merged batch passed it
    # on the same tree (ops/ci_batch_passed.py); a failed scope runs it.
    for name, mode, full in (("rust", "inputs.mode == 'ci'", ""),
                             ("candidate", "(inputs.mode == 'ci' || inputs.mode == 'stress')",
                              " && needs.scope.outputs.full != 'false'"),
                             ("daemon-image", "inputs.mode == 'ci'",
                              " && !contains(fromJSON(needs.scope.outputs.batch_passed || '[]'), "
                              "'Rust daemon / disposable image')")):
        assert jobs[name]["needs"] == "scope", name
        expected = gated % (mode, full)
        if name in ("rust", "daemon-image"):
            expected = expected.replace("&& (github.event_name != 'workflow_dispatch' || inputs.mode == 'ci')",
                                        "&& needs.scope.outputs.push_passed != 'true' && (github.event_name != 'workflow_dispatch' || inputs.mode == 'ci')")
        assert jobs[name]["if"] == expected, name
    # Parity checks and source mutants inherit the skip through their needs.
    # Their success checks name rust and candidate: the implicit success()
    # would also cover scope. Parity checks need scope for their matrix, so a
    # failed scope fails them (and the gate) instead of skipping them.
    # The shards read nothing from the Rust job; the gate still needs it.
    assert jobs["parity-checks"]["needs"] == ["scope", "candidate", "candidate-harness", "fixtures", "parity-pool"]
    assert jobs["parity-checks"]["if"].startswith("${{ !cancelled() && ")
    assert jobs["parity-checks"]["if"].endswith(
        "&& needs.candidate.result == 'success' && needs.candidate-harness.result == 'success' "
        "&& needs.fixtures.result == 'success' }}")
    assert jobs["fixtures"]["needs"] == "scope"
    assert jobs["candidate-harness"]["needs"] == "scope"
    assert jobs["candidate-harness"]["if"] == jobs["candidate"]["if"]
    assert jobs["fixtures"]["if"] == gated % ("inputs.mode == 'ci'", " && needs.scope.outputs.full != 'false'")
    assert "rust" in jobs["parity"]["needs"]
    assert jobs["source-mutants"]["needs"] == ["candidate"]
    assert jobs["source-mutants"]["if"].startswith("${{ !cancelled() && ")
    assert jobs["source-mutants"]["if"].endswith("&& needs.candidate.result == 'success' }}")
    assert jobs["parity"]["needs"][0] == "scope"


@pytest.mark.parametrize("name", ["CLI lease golden harness", "Daemon background pruning golden",
                                  "Daemon security admission golden", "Daemon body admission golden"])
def test_coverage_guard_rejects_a_removed_check(monkeypatch, name):
    changed = copy.deepcopy(workflow())
    steps = changed["jobs"]["parity-checks"]["steps"]
    steps[:] = [s for s in steps if s.get("name") != name]
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


def test_coverage_guard_rejects_an_ungated_w1c_step(monkeypatch):
    changed = copy.deepcopy(workflow())
    step = next(s for s in changed["jobs"]["parity-checks"]["steps"]
                if s.get("name") == "CLI W1-C golden harness")
    del step["if"]
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


@pytest.mark.parametrize("condition", [None, "matrix.suite == 'eval'", "matrix.suite == 'cli'"])
def test_principal_store_requires_the_postgres_shard(monkeypatch, condition):
    changed = copy.deepcopy(workflow())
    step = next(s for s in changed["jobs"]["parity-checks"]["steps"]
                if s.get("name") == "Principal store golden harness")
    step["if"] = condition
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


@pytest.mark.parametrize("mode", ["golden"])
@pytest.mark.parametrize("row", ["store", "read"])
def test_graph_coverage_rejects_each_removed_invocation(mode, row):
    jobs = copy.deepcopy(workflow()["jobs"])
    step = next(s for s in jobs["parity-checks"]["steps"]
                if s.get("name") == "Graph store golden harness")
    prefix = f"python rust/daemon/harness/graph_store.py {mode} "
    lines = step["run"].splitlines()
    removed = [line for line in lines if line.strip().startswith(prefix)
               and ("--row read" in line) == (row == "read")]
    assert len(removed) == 1
    step["run"] = "\n".join(line for line in lines if line not in removed)
    with pytest.raises(AssertionError):
        check_executable_coverage(jobs)


@pytest.mark.parametrize("selector", ["--row store --row read", "--row=read"])
def test_graph_coverage_rejects_store_commands_selecting_read(selector):
    jobs = copy.deepcopy(workflow()["jobs"])
    step = next(s for s in jobs["parity-checks"]["steps"]
                if s.get("name") == "Graph store golden harness")
    step["run"] = "\n".join(line + " " + selector if
                            line.strip().startswith("python rust/daemon/harness/graph_store.py ")
                            and "--row read" not in line else line
                            for line in step["run"].splitlines())
    with pytest.raises(AssertionError):
        check_executable_coverage(jobs)


def test_graph_postgres_initialization_pins_linux_locale():
    step = next(s for s in workflow()["jobs"]["parity-checks"]["steps"]
                if s.get("name") == "Prepare disposable PostgreSQL for CLI lease row")
    assert step["env"]["LANG"] == "${{ runner.os == 'Linux' && 'en_US.UTF-8' || '' }}"
    assert step["env"]["LC_ALL"] == step["env"]["LANG"]
    graph = next(s for s in workflow()["jobs"]["parity-checks"]["steps"]
                 if s.get("name") == "Graph store golden harness")
    assert graph["env"]["PL_GRAPH_REQUIRE_RECORDED_LOCALE"] == "${{ runner.os == 'Linux' && '1' || '0' }}"


@pytest.mark.parametrize("features", ["mutants", "principal-harness", ""])
def test_daemon_artifact_retains_both_harness_features(monkeypatch, features):
    changed = copy.deepcopy(workflow())
    step = next(s for s in changed["jobs"]["candidate-harness"]["steps"]
                if s.get("name") == "Build embedding candidate")
    invocation = commands(step["run"])[0]
    original = invocation[invocation.index("--features") + 1]
    step["run"] = step["run"].replace(original, features)
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


@pytest.mark.parametrize("name, old, new", [
    ("Offline W2-D search harness regressions", "--junitxml", "--durations=10 --junitxml"),
    ("CLI differential harness", "--row episode", "--row episode --row audit"),
])
def test_coverage_contract_allows_additions(monkeypatch, name, old, new):
    changed = copy.deepcopy(workflow())
    step = next(s for s in changed["jobs"]["parity-checks"]["steps"]
                if s.get("name") == name)
    step["run"] = step["run"].replace(old, new)
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    test_original_checks_remain_gated_on_the_expected_shards()


@pytest.mark.parametrize("name, old, new", [
    ("Compare sent YAML startup with PyYAML", "rust/daemon/harness/test_sent_yaml.py", ""),
    ("Compare sent YAML startup with PyYAML", "rust/daemon/harness/test_sent_yaml_mutants.py", ""),
    ("Check generated daemon schema", "--check", ""),
    ("Daemon security admission golden", "security-terminal-byte", ""),
    ("Daemon security admission golden", "run.py golden", "run.py live"),
    ("Daemon security admission golden", "rust/daemon/harness/test_security_cases.py", ""),
    ("Daemon security live checks without a Rust test", "security-refusals", ""),
    ("Daemon security live checks without a Rust test", "security-remote-auth", ""),
    ("Daemon security live checks without a Rust test", "$bindFixture --only", "$candidate --only"),
    ("Daemon body admission golden", "body-text-window", ""),
    ("Daemon body admission golden", "run.py golden", "run.py mutants"),
    ("Daemon static HTTP golden", "static-root-link", ""),
    ("Daemon static HTTP golden", "run.py golden", "run.py live"),
    ("CLI lease golden harness", "--golden", ""),
    ("CLI lease operator golden harness", "--golden", ""),
    ("CLI lease operator golden harness", "rust/cli_harness/test_lease_operator.py", ""),
    ("Check generated daemon schema", "python rust/daemon/harness/gen_schema_sql.py", "# python rust/daemon/harness/gen_schema_sql.py"),
    ("Daemon resident startup parity", "--no-build", ""),
    ("Daemon resident startup parity", "--rust-test-bin $candidate", ""),
    ("Daemon resident startup parity", "--no-build", "--no-build --record-goldens"),
    ("Daemon schema startup parity", "--out", "--record-goldens --out"),
    ("Daemon schema startup parity", "python rust/daemon/harness/schema_ci.py", "# python rust/daemon/harness/schema_ci.py"),
    ("CLI differential harness", "--row hook", ""),
    ("CLI differential harness", "rust/cli_harness/test_backup.py", ""),
    ("CLI differential harness", "rust/cli_harness/test_update_transactions.py", ""),
    ("CLI differential harness", "rust/cli_harness/test_update_wrappers_native.py", ""),
    ("CLI differential harness", "--row update", ""),
    ("CLI differential harness", "$env:PSEUDOLIFE_RUST_BINARY = $candidate", "$env:PSEUDOLIFE_RUST_BINARY = 'debug-only'"),
    ("CLI differential harness", "$env:PSEUDOLIFE_BACKUP_NATIVE_REQUIRED = '1'", "$env:PSEUDOLIFE_BACKUP_NATIVE_REQUIRED = '0'"),
    ("Graph store golden harness", "python rust/daemon/harness/graph_store.py golden", "# python rust/daemon/harness/graph_store.py golden"),
    ("Graph store golden harness", "graph_store.py golden --row read", "graph_store.py live --row read"),
    ("CLI differential harness", "--golden", ""),
    ("Clippy", "--all-targets", ""),
    ("Check write-path harness targets", "harness,mutants", "mutants"),
    ("Check write-path harness targets", "--all-targets", ""),
    ("Nextest without default features", "--no-default-features", ""),
    ("Nextest without default features", "--no-default-features", "--no-default-features --features codex-delivery"),
    ("Nextest", "cargo nextest run", "cargo --version #"),
    ("Principal store golden harness", "principals.py golden", "principals.py oracle"),
    ("Principal store golden harness", "principals.py golden", "principals.py live"),
    ("Principal store golden harness", "rust/target/release", "rust/target/debug"),
    ("Offline embedding golden and mutant row", "--fixture", ""),
    ("Offline embedding golden and mutant row", "--golden", "--record"),
    ("Offline embedding golden and mutant row", "rust/daemon/harness/test_embedding.py", ""),
    ("Offline W2-D search harness regressions", "rust/daemon/harness/test_w2d_controls.py", ""),
    ("Offline W2-D search harness regressions", "rust/daemon/harness/test_w2d_namespace.py", ""),
    ("Offline W2-D search harness regressions", "rust/daemon/harness/test_config_knobs_golden.py", ""),
    ("CLI W1-C golden harness", "$rows = @(${{ matrix.w1c_rows }})", "$rows = @('move')"),
    ("CLI W1-C golden harness", "python rust/cli_harness @golden",
     "# python rust/cli_harness @golden"),
    ("CLI W1-C golden harness", "@golden --golden", "@golden"),
    ("CLI W1-C golden harness", "$golden = $rows |", "$golden = @('move') |"),
    ("Daemon background pruning golden", "rust/daemon/harness/test_background_harness.py", ""),
    ("Daemon background pruning golden", "sweep-pruning", "session-reap"),
    ("Daemon background pruning golden", "run.py golden", "run.py golden --record"),
])
def test_coverage_contract_rejects_reduced_commands(monkeypatch, name, old, new):
    changed = copy.deepcopy(workflow())
    job = "rust" if name in RUST_CHECKS else "parity-checks"
    step = next(s for s in changed["jobs"][job]["steps"] if s.get("name") == name)
    assert old in step["run"]
    step["run"] = step["run"].replace(old, new)
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


def test_dream_reuses_candidate_and_checks_cancel_control():
    jobs = workflow()["jobs"]
    step = next(s for s in jobs["parity-checks"]["steps"]
                if s.get("name") == "Dream cursor golden harness")
    invocations = commands(step["run"])
    require_command(invocations, ("python", "rust/daemon/harness/dream_cursors.py", "golden"),
                    ("--ci-fixture", "--rust-bin"))
    # Its eight controls compared each mutant with the Python oracle live,
    # and left with the Python freeze (14 November 2026).
    assert not [words for words in invocations
                if words[:2] == ["python", "rust/daemon/harness/dream_cursors.py"] and words[2] != "golden"]
    require_command(invocations, ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_dream_cursors.py",
                     "rust/daemon/harness/test_dream_cursor_cancel.py"), PYTEST_FILTERS)
    assert not any(words[0] == "cargo" for words in invocations)
    assert "rust/target/release" in step["run"]
    assert "dream-cancel-drop-task" in step["run"]
    assert "$cancelExit -ne 1" in step["run"]
    # The control's expected AssertionError is tailed into a passing step's
    # log; label it, or it reads as a failure (runs 39033993990, 39088038765).
    label = step["run"].index("the test below MUST fail (expected)")
    assert label < step["run"].index("Get-Content -Tail 20 $cancelLog")
    build = next(s for s in jobs["candidate-harness"]["steps"] if s.get("name") == "Build embedding candidate")
    words = commands(build["run"])[0]
    assert "contract-harness" in words[words.index("--features") + 1].split(",")
    upload = next(s for s in jobs["candidate-harness"]["steps"] if s.get("uses") == "actions/upload-artifact@v4")
    assert {"rust/target/release/dream-contract", "rust/target/release/dream-contract.exe"} <= set(upload["with"]["path"].splitlines())


@pytest.mark.parametrize("control_exit, message, accepted", [
    (1, "AssertionError: cancelled waiter outcome differs", True),
    (0, "", False),
    (2, "AssertionError: unrelated native error", False),
    (1, "fixture setup failed", False),
])
def test_dream_control_survives_runner_exit_wrapper(tmp_path, control_exit, message, accepted):
    import os
    step = next(s for s in workflow()["jobs"]["parity-checks"]["steps"]
                if s.get("name") == "Dream cursor golden harness")
    # Match the hosted pwsh wrapper, including its final native exit status.
    bootstrap = r'''$ErrorActionPreference = 'stop'
function Resolve-Path { [pscustomobject]@{Path='fixture-candidate'} }
function chmod {}
function python {
    if ($args -contains 'admitted_commit_survives_cancel_and_next_pull') {
        Write-Output $env:PL_CONTROL_MESSAGE
        $global:LASTEXITCODE = [int]$env:PL_CONTROL_EXIT
    } else { $global:LASTEXITCODE = 0 }
}
'''
    wrapper = "\nif ((Test-Path -LiteralPath variable:LASTEXITCODE)) { exit $LASTEXITCODE }\n"
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), PL_CONTROL_EXIT=str(control_exit),
               PL_CONTROL_MESSAGE=message)
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                             bootstrap + step["run"] + wrapper], env=env,
                            capture_output=True, text=True)
    assert (result.returncode == 0) == accepted, result.stdout + result.stderr


@pytest.mark.parametrize("old, new", [
    ("--rust-bin $candidate", "--rust-bin stale-candidate"),
    ("principals.py golden --rust-bin $candidate", "principals.py golden --rust-bin stale-candidate"),
    ("$candidate = (Resolve-Path", "$unused = (Resolve-Path"),
    ("'pseudolife-daemon.exe'", "'stale-daemon.exe'"),
    ("'pseudolife-daemon'", "'stale-daemon'"),
])
def test_principal_commands_use_the_downloaded_candidate(monkeypatch, old, new):
    changed = copy.deepcopy(workflow())
    step = next(s for s in changed["jobs"]["parity-checks"]["steps"]
                if s.get("name") == "Principal store golden harness")
    assert old in step["run"]
    step["run"] = step["run"].replace(old, new)
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


@pytest.mark.parametrize("script", [
    "cargo build --release --bin pseudolife-daemon",
    "cargo +1.94.0 build --locked --release --bin pseudolife-daemon",
    "cargo --color never build --release --bin pseudolife-daemon",
    "& cargo build --release --bin pseudolife-daemon",
    "& 'cargo' build --manifest-path rust/Cargo.toml --release --bin pseudolife-daemon",
    "cargo.exe build --manifest-path rust/Cargo.toml --release --bin pseudolife-daemon",
])
def test_parity_rejects_a_rebuild_in_a_separate_step(monkeypatch, script):
    changed = copy.deepcopy(workflow())
    changed["jobs"]["parity-checks"]["steps"].append({
        "name": "Rebuild stale candidate", "run": script, "if": "matrix.suite == 'cli'"})
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


def test_w1c_rows_run_once_on_parallel_suites():
    jobs = workflow()["jobs"]
    matrix = jobs["parity-checks"]["strategy"]["matrix"]
    assert matrix["suite"] == PARITY_SUITES
    assert matrix["os"] == SYSTEMS
    step = next(s for s in jobs["parity-checks"]["steps"]
                if s.get("name") == "CLI W1-C golden harness")
    assert step["if"] == "startsWith(matrix.suite, 'w1c-')"
    assert jobs["parity"]["needs"] == GATE_NEEDS
    check_executable_coverage(jobs)


@pytest.mark.parametrize("row", W1C_ROWS)
@pytest.mark.parametrize("mutation", ["drop", "duplicate"])
def test_w1c_coverage_rejects_each_missing_or_duplicate_row(row, mutation):
    jobs = copy.deepcopy(workflow()["jobs"])
    shards = jobs["parity-checks"]["strategy"]["matrix"]["include"]
    shard = next(s for s in shards if f"'{row}'" in s.get("w1c_rows", ""))
    rows = re.findall(r"'([a-z_]+)'", shard["w1c_rows"])
    if mutation == "drop":
        rows.remove(row)
    else:
        rows.append(row)
    shard["w1c_rows"] = ", ".join(f"'{name}'" for name in rows)
    with pytest.raises(AssertionError):
        check_executable_coverage(jobs)


@pytest.mark.parametrize("suite", W1C_SUITES)
def test_w1c_coverage_rejects_a_missing_suite(suite):
    jobs = copy.deepcopy(workflow()["jobs"])
    jobs["parity-checks"]["strategy"]["matrix"]["suite"].remove(suite)
    with pytest.raises(AssertionError):
        check_executable_coverage(jobs)


@pytest.mark.parametrize("name, condition", [
    ("CLI W1-C golden harness", "matrix.suite == 'cli'"),
    ("Prepare disposable PostgreSQL for W1-C rows", "matrix.suite == 'cli'"),
    ("Build the test_login container image", "runner.os == 'Linux'"),
    ("Retain CLI harness summaries", "always() && matrix.suite == 'cli'"),
])
def test_w1c_coverage_rejects_changed_routing(name, condition):
    jobs = copy.deepcopy(workflow()["jobs"])
    step = next(s for s in jobs["parity-checks"]["steps"] if s.get("name") == name)
    step["if"] = condition
    with pytest.raises(AssertionError):
        check_executable_coverage(jobs)


@pytest.mark.parametrize("suite", W1C_SUITES)
@pytest.mark.parametrize("windows", [False, True])
def test_w1c_powershell_replays_every_row_of_its_shard(tmp_path, suite, windows):
    import os
    job = workflow()["jobs"]["parity-checks"]
    shard = next(s for s in job["strategy"]["matrix"]["include"] if s["suite"] == suite)
    step = next(s for s in job["steps"] if s.get("name") == "CLI W1-C golden harness")
    script = step["run"].replace("${{ matrix.w1c_rows }}", shard["w1c_rows"])
    script = script.replace("$IsWindows", "$fixtureWindows")
    script = script.replace("$IsLinux", "(-not $fixtureWindows)")
    bootstrap = r'''
$fixtureWindows = $env:PL_FIXTURE_WINDOWS -eq '1'
$global:calls = @()
function Resolve-Path { [pscustomobject]@{Path='fixture-candidate'} }
function python {
    $global:calls += ,@($args)
    $global:LASTEXITCODE = 0
}
'''
    # Replace the step's final exit only to observe the standalone calls.
    script = script.removesuffix("exit $LASTEXITCODE\n")
    script += "ConvertTo-Json -InputObject $global:calls -Compress -Depth 5\n"
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), PL_FIXTURE_WINDOWS=str(int(windows)))
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", bootstrap + script],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = json.loads(result.stdout)
    rows = re.findall(r"'([a-z_]+)'", shard["w1c_rows"])
    terminals = [call for call in calls if call[:2] == ["-m", "pytest"]]
    expected_terminals = []
    if "pair" in rows:
        expected_terminals.append("rust/cli_harness/test_pair_terminal.py")
    if "maintainer_setup" in rows:
        expected_terminals.append("rust/cli_harness/test_maintainer_setup_terminal.py")
    assert [call[3] for call in terminals] == expected_terminals
    if "maintainer_setup" in rows:
        for required in ("test_operator_transport.py", "test_audit_stats.py",
                         "test_maintainer_setup_native.py"):
            assert f"rust/cli_harness/{required}" in terminals[-1]
    calls = [call for call in calls if call not in terminals]
    # One golden pass over every row of the shard; no live pass since the
    # Python freeze (14 November 2026).
    assert len(calls) == 1
    call = calls[0]
    assert [call[i + 1] for i, word in enumerate(call[:-1]) if word == "--row"] == rows
    assert "--golden" in call
    assert call[call.index("--candidate") + 1] == "fixture-candidate"


def test_timeout_reduction_is_limited_to_cli_and_new_w1c_suites():
    job = workflow()["jobs"]["parity-checks"]
    assert job["timeout-minutes"] == "${{ matrix.job_timeout || 210 }}"
    budgets = {shard["suite"]: shard.get("job_timeout")
               for shard in job["strategy"]["matrix"]["include"]}
    # Only eval keeps the old 210-minute fallback.
    assert set(PARITY_SUITES) - set(budgets) == {"eval"}


def test_cli_budgets_preserve_separate_startup_part_limits():
    # Hosted Windows ran cli as one 61-70 minute job (master runs
    # 39205533092, 39236836263, 2026-10-19). Each shard now measures about
    # 26 minutes there, and its budget stays about twice that.
    job = workflow()["jobs"]["parity-checks"]
    budgets = {shard["suite"]: shard.get("job_timeout")
               for shard in job["strategy"]["matrix"]["include"]}
    for suite in CLI_SUITES:
        assert 0 < budgets[suite] <= (75 if suite == "cli-startup" else 60), suite
    steps = job["steps"]
    by_suite = {}
    for step in steps:
        for suite in CLI_SUITES:
            if step.get("if") == f"matrix.suite == '{suite}'" and step.get("run"):
                by_suite.setdefault(suite, []).append(step["name"])
    # Both logical parts keep their selectors and budgets in one physical job.
    assert by_suite["cli-startup"] == ["Keep the oracle's pydantic pin for the startup steps", "Daemon schema startup parity"]
    parts = {shard["suite"]: shard.get("startup_part") for shard in job["strategy"]["matrix"]["include"]}
    assert parts["cli-startup"] == 1
    assert "--part ${{ matrix.startup_part }}" in next(
        s for s in steps if s.get("name") == "Daemon resident startup parity")["run"]
    second = next(s for s in steps if s.get("name") == "Daemon resident startup parity part 2")
    assert "--part 2 " in second["run"]
    first = next(s for s in steps if s.get("name") == "Daemon resident startup parity")
    assert first["timeout-minutes"] == second["timeout-minutes"] == 26
    assert first["if"] == second["if"] == "${{ !cancelled() && matrix.suite == 'cli-startup' && steps.startup_setup.outcome == 'success' }}"
    pin = next(s for s in steps if s.get("name") == "Keep the oracle's pydantic pin for the startup steps")
    for package in ("pydantic==2.14.0", "pydantic-core==2.50.0"):
        assert package in pin["run"]
    names = [step.get("name") for step in steps]
    assert names.index("Daemon schema startup parity") < names.index("Daemon resident startup parity")
    assert names.index("Daemon resident startup parity") < names.index(second["name"])
    for upload, condition in (("Retain daemon schema parity", "matrix.suite == 'cli-startup'"),
                              ("Retain daemon resident startup parity",
                               "matrix.suite == 'cli-startup'")):
        step = next(s for s in steps if s.get("name") == upload)
        assert step["if"] == "always() && " + condition
    startup_upload = next(s for s in steps if s.get("name") == "Retain daemon resident startup parity")
    assert startup_upload["with"]["name"].endswith("-${{ matrix.suite }}")
    assert "daemon-startup-parity*.json" in startup_upload["with"]["path"]


@pytest.mark.parametrize("name", [
    "Prepare disposable PostgreSQL for W1-C rows",
    "Build the test_login container image",
])
def test_w1c_network_setup_has_a_five_minute_floor(name):
    job = workflow()["jobs"]["parity-checks"]
    step = next(s for s in job["steps"] if s.get("name") == name)
    assert step["timeout-minutes"] == 5


def test_board_runner_checks_execute_on_both_platforms():
    job = workflow()["jobs"]["rust"]
    assert set(job["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "windows-latest"}
    step = next(step for step in job["steps"] if step.get("name") == "Board harness lifecycle and normalization")
    assert "if" not in step
    assert step["working-directory"] == "."
    require_command(commands(step["run"]), ("python", "-m", "pytest"),
                    ("rust/daemon/harness/board/test_norm.py",
                     "rust/daemon/harness/board/test_run.py",
                     "rust/daemon/harness/test_pgdisposable_slices.py"), PYTEST_FILTERS)


def test_binaryless_harness_units_run_on_both_platforms():
    # The Python half of the shared Codex consent cases (issue #1500: broken
    # on master with no lane running it). It needs no built binary, so it
    # runs where any pull request that touches the oracle reaches it.
    # The harness unit tests beside it need no binary or extra install either
    # and were run by no lane before (found in the same sweep).
    job = workflow()["jobs"]["rust"]
    assert set(job["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "windows-latest"}
    step = next(step for step in job["steps"] if step.get("name") == "Check port inventories and producer census")
    assert "if" not in step
    assert step["working-directory"] == "."
    require_command(commands(step["run"]), ("python", "-m", "pytest"),
                    ("rust/cli_harness/test_native_runtime_contract.py",
                     "rust/daemon/harness/test_json_fidelity.py",
                     "rust/daemon/harness/test_schema_clocks.py",
                     "rust/daemon/harness/test_schema_generation.py",
                     "rust/daemon/harness/test_schema_guards.py",
                     "rust/daemon/harness/test_shadow_compare.py",
                     "rust/daemon/harness/test_startup_ci.py",
                     "rust/daemon/harness/test_startup_parts.py",
                     "rust/daemon/harness/test_w2d_rerank_check.py",
                     "rust/daemon/harness/test_w2d_trace_compare.py",
                     "rust/cli_harness/test_update_row.py",
                     # The harness's offline self-test, source-mutant anchors
                     # included: on a pull request's own checks, not only in
                     # the batch's cli shard.
                     "rust/cli_harness/test_cli_harness.py",), PYTEST_FILTERS)
    assert "httpx" in step["run"]  # test_cli_harness.py's mail cases import it


def test_rust_job_fits_a_cold_cache_and_bounds_the_override_builds():
    job = workflow()["jobs"]["rust"]
    override = next(step for step in job["steps"] if step.get("name") == "Daemon override build configurations")
    # Hosted windows 2026-10-13: a cold-cache run took 23.9 minutes; on
    # 2026-10-15 three cold runs were cancelled at 25 inside this step. A
    # cancelled run saves no cache, so the next run on that image is cold too.
    from ci_budget import Evaluator, condition_text
    class TimeoutContext(Evaluator):
        def lookup(self, path):
            return full if path == ["needs", "scope", "outputs", "full"] else super().lookup(path)
    for event, full in (("pull_request", "false"), ("pull_request", "true"),
                        ("pull_request", ""), ("push", "true"), ("schedule", "true")):
        timeout = TimeoutContext(event, ["scope"], set()).run(condition_text(job["timeout-minutes"]))
        assert timeout >= 45
        assert 0 < override["timeout-minutes"] < timeout


# --- self-hosted comparison lane (docs/specs/2026-10-19-self-hosted-parity-lane.md) ---

SELF_HOSTED = "runner.environment == 'self-hosted'"
DISK_STEP = "Check the runner's free disk"
GATE_STEP = "Yield to this machine's full suite"
VENV_STEP = "Give the shard its own Python environment"


def test_no_job_carries_the_judges_process_test_margin():
    # The judges' pinned process tests and their self-hosted margin left CI
    # with the Python freeze (14 November 2026); the security golden replay
    # keeps the hosted ten minutes on every lane.
    jobs = workflow()["jobs"]
    security = next(step for step in jobs["parity-checks"]["steps"]
                    if step.get("name") == "Daemon security admission golden")
    assert security["timeout-minutes"] == 10
    assert "PARITY_PROCESS_TEST_TIMEOUT" not in WORKFLOW.read_text(encoding="utf-8")


def test_selfhosted_lane_runs_the_hosted_shards_unchanged():
    text = WORKFLOW.read_text(encoding="utf-8")
    # Anchors, not a copy: a step or shard added to the hosted matrix reaches
    # the self-hosted lane in the same edit.
    # parity-selfhosted reuses the hosted steps and takes the same
    # scope-chosen matrix (every suite on the nightly and manual CI, its only
    # events).
    assert text.count("steps: *parity_steps") == 1
    jobs = workflow()["jobs"]
    hosted, lane = jobs["parity-checks"], jobs["parity-selfhosted"]
    assert lane["steps"] == hosted["steps"]
    # The same suites less the hosted-only composites (h5-light), whose
    # parts each runner runs as their own shards.
    from parity_scope import HOSTED_ONLY
    assert lane["strategy"]["matrix"]["suite"] == [
        suite for suite in hosted["strategy"]["matrix"]["suite"] if suite not in HOSTED_ONLY]
    assert lane["strategy"]["matrix"]["include"] == [
        entry for entry in hosted["strategy"]["matrix"]["include"] if entry["suite"] not in HOSTED_ONLY]
    # A dispatch may name the machines itself (a machine's trial before the
    # watcher lists it); the nightly has no inputs and reads the variable.
    assert lane["strategy"]["matrix"]["runner"] == (
        "${{ fromJSON(inputs.selfhosted_runners || vars.PARITY_SELFHOSTED_RUNNERS || '[\"off\"]') }}")
    assert lane["strategy"]["fail-fast"] is False
    assert lane["needs"] == ["scope", "candidate", "candidate-harness", "fixtures"]
    assert hosted["needs"] == [*lane["needs"], "parity-pool"]
    # Only the maintainer's registered runners: one label per machine.
    assert lane["runs-on"] == ["self-hosted", "pseudolife-parity-${{ matrix.runner }}"]
    assert lane["timeout-minutes"] >= 210 + int(lane["env"]["PARITY_YIELD_MINUTES"])
    assert lane["env"]["PARITY_MIN_FREE_GB"] == "8"


def test_selfhosted_lane_never_touches_a_required_check():
    jobs = workflow()["jobs"]
    lane = jobs["parity-selfhosted"]
    assert lane["continue-on-error"] is True
    for name, job in jobs.items():
        needs = job.get("needs", [])
        assert "parity-selfhosted" not in ([needs] if isinstance(needs, str) else needs), name
    assert "parity-selfhosted" not in gate_steps(jobs)[1]["run"]
    # Nightly and manual CI only: queued self-hosted jobs never hold a PR's
    # checks open.
    assert lane["if"] == (
        "${{ !cancelled() && (github.event_name == 'schedule' || "
        "(github.event_name == 'workflow_dispatch' && inputs.mode == 'ci')) "
        "&& (inputs.selfhosted_runners != '' || vars.PARITY_SELFHOSTED_RUNNERS != '') "
        "&& needs.candidate.result == 'success' && needs.candidate-harness.result == 'success' "
        "&& needs.fixtures.result == 'success' }}")
    assert "pull_request" not in lane["if"] and "'push'" not in lane["if"]
    # The trial override is a dispatch input and read nowhere else.
    trial = workflow()[True]["workflow_dispatch"]["inputs"]["selfhosted_runners"]
    assert trial["type"] == "string" and trial["required"] is False
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("inputs.selfhosted_runners") == 2


def test_both_lanes_upload_under_distinct_names():
    jobs = workflow()["jobs"]
    assert jobs["parity-checks"]["env"]["PARITY_ARTIFACT_PREFIX"] == ""
    assert jobs["parity-selfhosted"]["env"]["PARITY_ARTIFACT_PREFIX"] == "self-hosted-"
    uploads = [step for step in jobs["parity-checks"]["steps"]
               if step.get("uses") == "actions/upload-artifact@v4"]
    assert uploads
    for step in uploads:
        assert step["with"]["name"].startswith(ARTIFACT_PREFIX), step.get("name")


def test_self_hosted_only_steps_leave_the_hosted_shard_unchanged():
    steps = workflow()["jobs"]["parity-checks"]["steps"]
    disk, gate = steps[0], steps[1]
    assert disk["name"] == DISK_STEP and disk["if"] == SELF_HOSTED
    assert gate["name"] == GATE_STEP and gate["if"] == SELF_HOSTED
    assert steps[2]["uses"] == "actions/checkout@v4"
    venv = next(step for step in steps if step.get("name") == VENV_STEP)
    assert venv["if"] == SELF_HOSTED
    setup = next(step for step in steps if step.get("uses") == "actions/setup-python@v5")
    assert steps.index(setup) < steps.index(venv)
    install = next(step for step in steps if step.get("name") == "Install checkout and test dependencies")
    assert steps.index(venv) < steps.index(install)
    # Self-hosted runners save nothing to the repository cache, which the
    # hosted Rust caches need; hosted keeps both caches as before.
    assert setup["with"]["cache"] == "${{ runner.environment != 'self-hosted' && 'pip' || '' }}"
    cache = next(step for step in steps if step.get("uses") == "Swatinem/rust-cache@v2")
    assert cache["with"]["save-if"] == "${{ runner.environment != 'self-hosted' }}"
    added = [step for step in steps if SELF_HOSTED in step.get("if", "")]
    assert [step["name"] for step in added] == [DISK_STEP, GATE_STEP, VENV_STEP]


GATE_STUBS = r'''$ErrorActionPreference = 'stop'
function Get-Command { if ($env:PL_NO_CLI -eq '1') { $null } else { $true } }
function Start-Sleep { Add-Content -LiteralPath $env:PL_LOG -Value 'sleep' }
function pseudolife-mcp {
    Add-Content -LiteralPath $env:PL_LOG -Value ("call " + ($args -join ' ') + " | $env:PSEUDOLIFE_SUITE_LEASE | $env:PSEUDOLIFE_SUITE_LOCK_DIR")
    $sequence = ConvertFrom-Json $env:PL_SEQUENCE -NoEnumerate
    $calls = @(Get-Content -LiteralPath $env:PL_LOG | Where-Object { $_ -like 'call *' }).Count
    $reply = $sequence[[math]::Min($calls, $sequence.Count) - 1]
    ConvertTo-Json -Compress @{ name = $args[-1]; held = ($reply[0] -eq 1); local = @{ state = $reply[1] } }
    $global:LASTEXITCODE = $reply[0]
}
'''


@pytest.mark.parametrize("case, sequence, overrides, accepted, calls, sleeps", [
    ("free", [[0, "free"]], {}, True, 1, 0),
    ("waits for the suite", [[1, "held"], [1, "held"], [0, "free"]], {}, True, 3, 2),
    ("yields", [[1, "held"]], {"PARITY_YIELD_MINUTES": "0"}, False, 1, 0),
    ("check failed", [[70, None]], {}, False, 1, 0),
    ("no lock file reads free", [[0, None]], {}, False, 1, 0),
    ("the host's lease", [[0, "free"]], {"PSEUDOLIFE_PARITY_LEASE": "full-suite"}, False, 0, 0),
    ("no lock directory", [[0, "free"]], {"PSEUDOLIFE_PARITY_LOCK_DIR": ""}, False, 0, 0),
    ("no cli", [[0, "free"]], {"PL_NO_CLI": "1"}, False, 0, 0),
])
def test_selfhosted_gate_runs_only_on_a_free_lease(tmp_path, case, sequence, overrides,
                                                   accepted, calls, sleeps):
    import os
    gate = next(step for step in workflow()["jobs"]["parity-checks"]["steps"] if step.get("name") == GATE_STEP)
    log, summary = tmp_path / "calls.log", tmp_path / "summary.md"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ, PL_LOG=str(log), PL_SEQUENCE=json.dumps(sequence),
               GITHUB_WORKSPACE=str(tmp_path), GITHUB_STEP_SUMMARY=str(summary),
               PSEUDOLIFE_PARITY_LEASE="full-suite@fixture",
               PSEUDOLIFE_PARITY_LOCK_DIR=str(tmp_path / "locks"),
               **workflow()["jobs"]["parity-selfhosted"]["env"])
    env.pop("PSEUDOLIFE_SUITE_LEASE", None)
    env.pop("PSEUDOLIFE_SUITE_LOCK_DIR", None)
    env.update(overrides)
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                             GATE_STUBS + gate["run"]], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == accepted, case + result.stdout + result.stderr
    lines = log.read_text(encoding="utf-8").splitlines()
    made = [line for line in lines if line.startswith("call ")]
    assert len(made) == calls, lines
    assert lines.count("sleep") == sleeps, lines
    for line in made:
        assert line == f"call lease check --json full-suite@fixture | full-suite@fixture | {tmp_path / 'locks'}"
    yielded = summary.exists() and "Yielded" in summary.read_text(encoding="utf-8")
    assert yielded == (case == "yields")


@pytest.mark.parametrize("floor, accepted", [("0", True), ("1000000", False)])
def test_selfhosted_disk_check_names_a_full_disk_before_the_lease(tmp_path, floor, accepted):
    import os
    disk = workflow()["jobs"]["parity-checks"]["steps"][0]
    assert disk["name"] == DISK_STEP
    summary = tmp_path / "summary.md"
    env = dict(os.environ, GITHUB_WORKSPACE=str(tmp_path), GITHUB_STEP_SUMMARY=str(summary),
               RUNNER_TEMP=str(tmp_path), GITHUB_ENV=str(tmp_path / "environment"),
               **workflow()["jobs"]["parity-selfhosted"]["env"])
    env["PARITY_MIN_FREE_GB"] = floor
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                             "$ErrorActionPreference = 'stop'\n" + disk["run"]],
                            env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == accepted, result.stdout + result.stderr
    # Every shard records its free disk, the comparison week's disk figure.
    assert f"GB free on the runner's work drive (floor {floor} GB)" in summary.read_text(encoding="utf-8")
    if not accepted:
        assert "so the shard did not run" in result.stderr + result.stdout
        assert '"Disk use"' in result.stderr + result.stdout


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only temporary-directory adapter")
@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_selfhosted_linux_temp_files_use_the_job_work_drive(tmp_path, system):
    import os

    disk = workflow()["jobs"]["parity-checks"]["steps"][0]
    original = tmp_path / "original temp"
    original.mkdir()
    job = tmp_path / "job temp"
    job.mkdir()
    environment = tmp_path / "environment"
    env = dict(os.environ, GITHUB_WORKSPACE=str(tmp_path),
               GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"), GITHUB_ENV=str(environment),
               RUNNER_OS=system, RUNNER_TEMP=str(job), TMPDIR=str(original),
               PARITY_MIN_FREE_GB="0")
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                             "$ErrorActionPreference = 'stop'\n" + disk["run"]],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    exported = dict(line.split("=", 1) for line in environment.read_text(encoding="utf-8").splitlines()
                    if "=" in line) if environment.exists() else {}
    if system == "Linux":
        assert exported.get("TMPDIR") == str(job / "parity-tmp")
        assert (job / "parity-tmp").is_dir()
    else:
        assert "TMPDIR" not in exported
        assert not (job / "parity-tmp").exists()
    # Follow GITHUB_ENV into the next process, as the runner does: changing
    # only PowerShell's current environment would leave pytest on /tmp.
    child = subprocess.run([sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
                           env={**env, **exported}, capture_output=True, text=True, check=True)
    expected = job / "parity-tmp" if system == "Linux" else original
    assert Path(child.stdout.strip()).resolve() == expected.resolve()


# --- the required gate holds only deterministic Windows work ---

# --- Rust caches: one key per compiling suite, saved on failure, no image toolchain ---

TOOLCHAIN_STEP = "Keep only the pinned Rust toolchain"
# The parity suites that compile (sent-YAML mutants in cli, which_comparison
# in eval; the cli-daemon shards run the fixtures job's storage test binary);
# the rest restored a 71 MB registry they never read (master run
# 39236836263, 2026-10-19).
CACHED_SUITES = {"eval", "cli"}
CACHED_IF = "matrix.suite == 'eval' || matrix.suite == 'cli'"


def pinned_channel():
    text = (ROOT / "rust/rust-toolchain.toml").read_text(encoding="utf-8")
    return re.search(r'^channel = "([^"]+)"$', text, re.M).group(1)


def cached_jobs():
    jobs = workflow()["jobs"]
    return {name: job for name, job in jobs.items()
            if any(step.get("uses") == "Swatinem/rust-cache@v2" for step in job.get("steps", []))}


def test_every_rust_cache_is_saved_on_failure_and_keeps_the_lockfile_hash():
    jobs = cached_jobs()
    assert set(jobs) >= {"rust", "fixtures", "candidate", "candidate-harness", "parity-checks", "source-mutants"}
    for name, job in jobs.items():
        cache = next(step for step in job["steps"] if step.get("uses") == "Swatinem/rust-cache@v2")
        # The fixtures and frozen-probes jobs only read the Rust job's cache
        # and never save.
        if name in ("fixtures", "frozen-probes"):
            assert cache["with"]["save-if"] is False
            continue
        assert cache["with"]["cache-on-failure"] is True, name
        # Turning the environment hash off also drops the Cargo.lock hash:
        # the key would never change, and rust-cache skips the save on an
        # exact hit, so the cache would freeze at its first save.
        assert "add-rust-environment-hash-key" not in cache["with"], name


def test_every_rust_cache_hashes_only_the_pinned_toolchain():
    for name, job in cached_jobs().items():
        steps = job["steps"]
        toolchain = next(step for step in steps if step.get("uses") == "dtolnay/rust-toolchain@stable")
        assert toolchain["with"]["toolchain"] == pinned_channel(), name
        keep = next(step for step in steps if step.get("name") == TOOLCHAIN_STEP)
        cache = next(step for step in steps if step.get("uses") == "Swatinem/rust-cache@v2")
        assert steps.index(toolchain) < steps.index(keep) < steps.index(cache), name
        # Never on a self-hosted runner: its toolchains belong to its owner.
        expected = "runner.environment != 'self-hosted'"
        if cache.get("if"):
            expected += f" && ({cache['if']})"
        assert keep["if"] == expected, name
        assert keep["shell"] == "pwsh" and keep["run"] == steps_by_name("parity-checks")[TOOLCHAIN_STEP]["run"]


WEEKLY_PREFIX = "v0-rust-${{ steps.cache_week.outputs.week }}"


def test_every_rust_cache_key_starts_with_the_week():
    # An exact hit never saves again and a partial match deletes target
    # files older than a week (issue #1520): every entry is rebuilt weekly,
    # and the readers name the same week as the jobs that save.
    for path in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        for name, job in (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).items():
            steps = job.get("steps") or []
            caches = [step for step in steps if str(step.get("uses", "")).startswith("Swatinem/rust-cache")]
            for cache in caches:
                assert cache["with"]["prefix-key"] == WEEKLY_PREFIX, (path.name, name)
                week = [step for step in steps if step.get("id") == "cache_week"]
                assert len(week) == 1 and steps.index(week[0]) < steps.index(cache), (path.name, name)
                assert "if" not in week[0] and week[0]["shell"] == "pwsh", (path.name, name)
                assert "[DateTime]::UtcNow" in week[0]["run"] and "ISOWeek]::GetYear" in week[0]["run"]
                assert week[0]["run"] == steps_by_name("rust")["Name this week's Rust cache"]["run"]


def steps_by_name(job):
    return {step.get("name"): step for step in workflow()["jobs"][job]["steps"]}


def test_parity_suites_cache_under_their_own_keys():
    cache = next(step for step in workflow()["jobs"]["parity-checks"]["steps"]
                 if step.get("uses") == "Swatinem/rust-cache@v2")
    # Nine suites shared one key: the first to save fixed it, and on an
    # exact hit no later suite's compile was ever saved.
    assert cache["with"]["key"] == "${{ matrix.suite }}"
    assert "shared-key" not in cache["with"]
    assert cache["if"] == CACHED_IF
    assert CACHED_SUITES <= set(PARITY_SUITES)


TOOLCHAIN_STUBS = r'''$ErrorActionPreference = 'stop'
function rustup {
    Add-Content -LiteralPath $env:PL_LOG -Value ($args -join ' ')
    if ($args[0] -eq 'toolchain' -and $args[1] -eq 'list') {
        $env:PL_TOOLCHAINS -split ';'
        $global:LASTEXITCODE = 0
    } elseif ($args[1] -eq 'uninstall') {
        $global:LASTEXITCODE = [int]$env:PL_UNINSTALL_EXIT
    } else { $global:LASTEXITCODE = 0 }
}
'''


@pytest.mark.parametrize("toolchains, removed, exit_code, accepted", [
    (["stable-x86_64-pc-windows-msvc", "1.94.0-x86_64-pc-windows-msvc (active, default)"],
     ["stable-x86_64-pc-windows-msvc"], 0, True),
    (["1.94.0-x86_64-unknown-linux-gnu (default)"], [], 0, True),
    (["stable-x86_64-unknown-linux-gnu", "1.99.0-x86_64-unknown-linux-gnu",
      "1.94.0-x86_64-unknown-linux-gnu (default)"],
     ["stable-x86_64-unknown-linux-gnu", "1.99.0-x86_64-unknown-linux-gnu"], 0, True),
    (["stable-x86_64-pc-windows-msvc", "1.94.0-x86_64-pc-windows-msvc (default)"],
     ["stable-x86_64-pc-windows-msvc"], 1, False),
    # A near miss: 1.94.0 as a prefix of another release is not the pin.
    (["1.94.01-x86_64-pc-windows-msvc", "1.94.0-x86_64-pc-windows-msvc (default)"],
     ["1.94.01-x86_64-pc-windows-msvc"], 0, True),
])
def test_toolchain_cleanup_removes_only_unpinned_toolchains(tmp_path, toolchains, removed, exit_code, accepted):
    import os
    step = steps_by_name("parity-checks")[TOOLCHAIN_STEP]
    log = tmp_path / "rustup.log"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ, PL_LOG=str(log), PL_TOOLCHAINS=";".join(toolchains),
               PL_UNINSTALL_EXIT=str(exit_code), GITHUB_WORKSPACE=str(ROOT))
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", TOOLCHAIN_STUBS + step["run"]],
                            env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == accepted, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    uninstalled = [call.split()[-1] for call in calls if call.startswith("toolchain uninstall")]
    assert uninstalled == (removed if accepted else removed[:1])


def test_toolchain_cleanup_refuses_without_a_pinned_channel(tmp_path):
    import os
    step = steps_by_name("parity-checks")[TOOLCHAIN_STEP]
    (tmp_path / "rust").mkdir()
    (tmp_path / "rust/rust-toolchain.toml").write_text("[toolchain]\n", encoding="utf-8")
    log = tmp_path / "rustup.log"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ, PL_LOG=str(log), PL_TOOLCHAINS="stable-x86_64-pc-windows-msvc",
               PL_UNINSTALL_EXIT="0", GITHUB_WORKSPACE=str(tmp_path))
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", TOOLCHAIN_STUBS + step["run"]],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "uninstall" not in log.read_text(encoding="utf-8")


# --- observability: runner facts, failure names, durations and junit ---

FACTS_STEP = "Record the runner image and CPU count"
CONTROL_RUN = "admitted_commit_survives_cancel_and_next_pull"


def pytest_lines(script):
    return [line.strip() for line in script.splitlines()
            if re.match(r"(& \$oraclePython|python) -m pytest\b", line.strip())]


def test_parity_shards_record_their_runner():
    steps = workflow()["jobs"]["parity-checks"]["steps"]
    facts = steps[3]
    assert facts["name"] == FACTS_STEP and "if" not in facts and facts["shell"] == "pwsh"
    assert steps[2]["uses"] == "actions/checkout@v4"


def test_parity_pytest_runs_name_failures_and_keep_reports():
    steps = workflow()["jobs"]["parity-checks"]["steps"]
    reports = []
    for step in steps:
        for line in pytest_lines(step.get("run", "")):
            if CONTROL_RUN in line:
                # The mutant control must fail; its report would read as a failure.
                assert "--junitxml" not in line
                continue
            assert " -ra " in f" {line} " and "--durations=30" in line, (step["name"], line)
            # pwsh steps name the report with Join-Path; a default-shell step
            # (bash on Linux) with the runner.temp expression.
            joined = re.search(r"--junitxml \(Join-Path \$env:RUNNER_TEMP '(junit/[^']+)'\)", line)
            reports.append("${{ runner.temp }}/" + joined.group(1) if joined else
                           re.search(r"--junitxml (\$\{\{ runner\.temp \}\}\S+|\S+)", line).group(1))
    assert len(reports) >= 12 and len(set(reports)) == len(reports), reports
    upload = next(step for step in steps if step.get("name") == "Retain parity pytest reports")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/junit/*.xml"
    assert upload["with"]["name"] == ARTIFACT_PREFIX + "parity-junit-${{ runner.os }}-${{ matrix.suite }}"
    own = [path for path in reports if path.startswith("${{ runner.temp }}/junit/")]
    # Every report lands in the shared upload (the eval harness's own report
    # left with its differential tests).
    assert set(reports) == set(own)


def test_runner_facts_name_cpus_and_image(tmp_path):
    import os
    step = next(s for s in workflow()["jobs"]["parity-checks"]["steps"] if s.get("name") == FACTS_STEP)
    summary = tmp_path / "summary.md"
    env = dict(os.environ, ImageOS="win25", ImageVersion="20261012.1.0", RUNNER_OS="Windows",
               RUNNER_ARCH="X64", RUNNER_ENVIRONMENT="github-hosted", GITHUB_STEP_SUMMARY=str(summary))
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", step["run"]],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f"image win25 20261012.1.0, {os.cpu_count()} logical CPUs" in result.stdout
    assert result.stdout.strip() in summary.read_text(encoding="utf-8-sig")


def test_job_environments_use_only_pre_runner_contexts():
    # GitHub rejects the entire workflow before dispatch when runner.temp
    # is used in job-level env; it is available only after a runner starts.
    for name, job in workflow()["jobs"].items():
        assert not re.search(r"\brunner[.\[]", json.dumps(job.get("env", {}))), name


def test_no_ordinary_ci_job_selects_or_exports_a_python_oracle():
    # The Python freeze (14 November 2026): nothing a pull request, a batch,
    # a master push or a schedule starts binds an event oracle; only the
    # manual stress loop keeps its best-effort binding.
    jobs = workflow()["jobs"]
    assert not any(s.get("id") == "ci_oracle" for s in jobs["scope"]["steps"])
    assert "oracle_base" not in jobs["scope"]["outputs"]
    for name, job in jobs.items():
        if name.startswith("stress"):
            continue
        for step in job.get("steps", []):
            assert "oracle_selection" not in step.get("run", ""), (name, step.get("name"))
            assert "phase1_oracle" not in step.get("run", ""), (name, step.get("name"))
    for name in ("rust",):
        step = next(s for s in jobs[name]["steps"]
                    if s.get("name") == "Point the Rust identity contract at the checkout's package metadata")
        assert step["run"].strip() == (
            '"PSEUDOLIFE_PORT_ORACLE_ROOT=$env:GITHUB_WORKSPACE" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append')


@pytest.mark.parametrize("name", ["rust", "candidate", "candidate-harness", "fixtures", "daemon-image"])
def test_failed_manual_ci_scope_spends_no_build_slots(name):
    from ci_budget import Evaluator
    job = workflow()["jobs"][name]
    condition = job["if"][3:-2].replace("inputs.mode", "'ci'").replace("needs.scope.result", "'failure'")
    assert Evaluator("workflow_dispatch", ["scope"], set()).run(condition) is False
    # Every other event retains its scope-failure fail-safe, so a failed
    # classifier cannot suppress valid PR, merge-group, push or nightly work.
    for event in ("pull_request", "merge_group", "push", "schedule"):
        assert Evaluator(event, ["scope"], set()).run(condition) is not False
