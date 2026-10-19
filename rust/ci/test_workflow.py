"""Keep every Rust gate while changing the workflow's execution graph."""

import argparse
import copy
from collections import Counter
import json
import re
from pathlib import Path
import shlex
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/rust.yml"
SYSTEMS = ["ubuntu-latest", "windows-latest"]
RUST_CHECKS = (
    "Check generated daemon schema",
    "Bind the selected Python interpreter for Rust fixtures",
    "Install the Python SDK fixture dependency",
    "Bind the event-selected Python oracle",
    "Prepare selected package metadata for the Rust identity contract",
    "Install pinned nextest in the runner temporary directory",
    "Check formatting", "Check all targets", "Clippy", "Nextest",
    "Check write-path harness targets",
    "Check all targets without default features", "Clippy without default features",
    "Nextest without default features",
    "Daemon override build configurations",
)
PARITY_CHECKS = {
    "Install checkout and test dependencies": None,
    "Bind the event-selected Python oracle": None,
    "Check CI coverage contract": "eval",
    "Compare sent YAML startup with PyYAML": "cli",
    "Run every eval harness test": "eval",
    "Offline embedding golden and mutant row": "eval",
    "Offline W2-D search harness regressions": "eval",
    "CLI differential harness": "cli",
    "CLI differential harness live arms": "cli-live",
    "Prepare disposable PostgreSQL for CLI lease row": "cli*",
    "CLI lease differential harness": "cli",
    "CLI W1-C differential harness": "w1c",
    "Prepare disposable PostgreSQL for W1-C rows": "w1c",
    "Daemon background pruning golden": "cli",
    "Dream cursor differential harness": "cli",


    "Principal store differential and golden harness": "cli",
    "Daemon static HTTP parity": "cli",

    "Daemon schema startup parity": "cli-daemon",
    "Graph store differential and recorded oracle": "cli",
    "Daemon body admission parity": "cli-live",
    "Daemon security admission parity": "cli-live",
    "Daemon MCP live parity and controls": "cli-live",
    "Daemon resident startup parity": "cli-daemon",
    "Run unchanged candidates and differential judges": "judges",
}
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


W1C_SUITES = ("w1c-doctor", "w1c-pairing", "w1c-connection")
H5_SUITES = ("h5-audit", "h5-backup", "h5-w1a", "h5-background", "h5-daemon-mutants", "h5-graph-mutants")
CLI_SUITES = ("cli", "cli-daemon", "cli-live")
PARITY_SUITES = ["eval", *CLI_SUITES, "judges", *W1C_SUITES, *H5_SUITES]
# The live Python-versus-Rust comparisons hosted Windows cannot run
# deterministically (docs/specs/2026-10-19-merge-queue.md, "The required
# Parity gate"): required on Linux, reported by parity-live on Windows.
LIVE_SUITE = "cli-live"
LIVE_CHECKS = ("CLI differential harness live arms", "Daemon body admission parity",
               "Daemon security admission parity", "Daemon MCP live parity and controls")
WINDOWS_LIVE_EXCLUDE = [{"os": "windows-latest", "suite": LIVE_SUITE}]


# The W1-C step passes its rows through PowerShell arrays (@live, @golden), so
# row_commands() sees no --row there: the row list and how both passes are
# built from it are pinned here instead.
W1C_ROWS = ("transfer", "test_login", "doctor", "connect", "maintainer", "invite", "pair",
            "move", "tunnel")
W1C_LINES = (
    "$live = $rows | ForEach-Object { '--row'; $_ }",
    "$replayed = $rows | Where-Object { $_ -ne 'test_login' -or "
    "(Test-Path \"rust/cli_harness/goldens/test_login.$platform.json\") }",
    "$golden = $replayed | ForEach-Object { '--row'; $_ }",
)


def check_w1c_rows(script):
    lines = [line.strip() for line in script.splitlines()]
    literals = [line for line in lines if line.startswith("$rows = @(")]
    assert len(literals) == 1, literals
    assert literals[0] == "$rows = @(${{ matrix.w1c_rows }})"
    for line in W1C_LINES:
        assert line in lines, line
    invocations = commands(script)
    require_command(invocations, ("python", "rust/cli_harness", "@live"), ("--candidate",),
                    ("--golden", "--record", "--mutants", "--case", "--skip-bank"))
    require_command(invocations, ("python", "rust/cli_harness", "@golden", "--golden"),
                    ("--candidate",), ("--record", "--mutants", "--case"))
    assert [words[:3] for words in invocations if words[:2] == ["python", "rust/cli_harness"]] \
        == [["python", "rust/cli_harness", "@live"], ["python", "rust/cli_harness", "@golden"]]


def check_w1c_shards(jobs):
    job = jobs["parity-checks"]
    matrix = job["strategy"]["matrix"]
    assert matrix["os"] == SYSTEMS
    assert matrix["suite"] == PARITY_SUITES
    assert matrix["exclude"] == WINDOWS_LIVE_EXCLUDE
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
    harness = steps["CLI W1-C differential harness"]
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
    record = steps["Record the Linux test_login golden while it is absent"]
    assert record["id"] == "test_login_linux_golden"
    assert record["if"] == ("!cancelled() && runner.os == 'Linux' && "
                            "matrix.suite == 'w1c-connection' && "
                            "hashFiles('rust/cli_harness/goldens/test_login.linux.json') == ''")
    require_command(commands(record["run"]), ("python", "rust/cli_harness"),
                    ("--row", "test_login", "--record", "-v", "--out"))
    retained = steps["Retain the recorded Linux test_login golden"]
    assert retained["if"] == "!cancelled() && steps.test_login_linux_golden.outcome == 'success'"
    assert retained["with"]["name"] == ARTIFACT_PREFIX + "test_login-linux-golden"
    assert retained["with"]["path"].splitlines() == [
        "rust/cli_harness/goldens/test_login.linux.json",
        "${{ runner.temp }}/test-login-linux-record.json"]
    summaries = steps["Retain CLI harness summaries"]
    assert summaries["if"] == ("always() && (startsWith(matrix.suite, 'cli') || "
                               "startsWith(matrix.suite, 'w1c-'))")
    assert summaries["with"]["name"] == ARTIFACT_PREFIX + "cli-harness-${{ runner.os }}-${{ matrix.suite }}"
    assert summaries["with"]["path"] == "${{ runner.temp }}/cli-harness*.json"
    # A second explicit row invocation outside the shard step duplicates work.
    for step in job["steps"]:
        if step is record:
            continue
        for words in commands(step.get("run", "")):
            if words[:2] == ["python", "rust/cli_harness"]:
                assert not any(word in W1C_ROWS for word in words)
    for step in job["steps"]:
        if step.get("uses") == "dtolnay/rust-toolchain@stable":
            assert step["if"] == "!startsWith(matrix.suite, 'w1c-')"
        if step.get("uses") == "Swatinem/rust-cache@v2":
            assert "w1c-" not in step["if"] and CACHED_SUITES == {"eval", "cli", "cli-daemon"}


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
    check_w1c_shards(jobs)
    sent_yaml = parity["Compare sent YAML startup with PyYAML"]
    require_command(commands(sent_yaml["run"]), ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_sent_yaml.py",
                     "rust/daemon/harness/test_sent_yaml_mutants.py"), PYTEST_FILTERS)
    assert 'PSEUDOLIFE_SENT_YAML_BIN' in sent_yaml["run"]
    assert 'rust/target/release/pseudolife-stdio' in sent_yaml["run"]
    assert not any(words[0] == "cargo" for words in commands(sent_yaml["run"]))
    require_command(commands(parity["Run every eval harness test"]["run"]),
                    ("python", "-m", "pytest"),
                    ("evals/rust_port", "evals/rust_baseline", "-p",
                     "evals.rust_port.collection_guard", "--eval-collection-guard"), PYTEST_FILTERS)
    search = parity["Offline W2-D search harness regressions"]
    require_command(commands(search["run"]), ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_w2d_controls.py",
                     "rust/daemon/harness/test_w2d_namespace.py",
                     "rust/daemon/harness/test_w2d_recent.py"), PYTEST_FILTERS)
    assert search["timeout-minutes"] == 1
    # The mail, hook and episode rows replay their goldens in the required
    # cli shard; their live arms run beside the other live comparisons.
    row_commands(commands(parity["CLI differential harness"]["run"]), ("mail", "hook", "episode"), True)
    row_commands(commands(parity["CLI differential harness live arms"]["run"]),
                 ("mail", "hook", "episode"), False)
    for golden in (False, True):
        row_commands(commands(parity["CLI lease differential harness"]["run"]), ("lease",), golden)
    require_command(commands(parity["CLI differential harness"]["run"]),
                    ("python", "-m", "pytest"),
                    ("rust/cli_harness/test_cli_harness.py", "rust/cli_harness/test_lease.py",
                     "rust/cli_harness/test_bank.py", "rust/cli_harness/test_backup.py"), PYTEST_FILTERS)
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

    static = commands(parity["Daemon static HTTP parity"]["run"])
    for mode in ("live", "golden"):
        require_command(static, ("python", "rust/daemon/harness/run.py", mode),
                        ("--rust-bin", "--only", "static-build", "static-paths", "static-missing", "static-root-link", "--out"),
                        ("--record", "--mutants"))
    require_command(static, ("python", "-m", "pytest"), ("rust/daemon/harness/test_static.py",), PYTEST_FILTERS)
    assert not any(words[0] == "cargo" for words in static)
    body = commands(parity["Daemon body admission parity"]["run"])
    for mode in ("live", "golden"):
        require_command(body, ("python", "rust/daemon/harness/run.py", mode),
                        ("--rust-bin", "--only", "body-limits", "body-view-open", "body-pair-budget", "body-text-window", "--out"),
                        ("--record", "--mutants"))
    require_command(body, ("python", "-m", "pytest"), ("rust/daemon/harness/test_body_cases.py",), PYTEST_FILTERS)
    assert not any(words[0] == "cargo" for words in body)
    mcp = commands(parity["Daemon MCP live parity and controls"]["run"])
    require_command(mcp, ("python", "-m", "pip", "install"),
                    ("pydantic==2.14.0", "pydantic-core==2.50.0"))
    require_command(mcp, ("python", "-m", "pytest"),
                    ("tests/test_rust_mcp_catalogue.py", "rust/daemon/harness/test_mcp_mount.py",
                     "rust/daemon/harness/test_mcp_mutant_verdict.py", "rust/daemon/harness/test_mcp_ci.py", "rust/daemon/harness/test_mcp_runner.py", "rust/daemon/harness/test_mcp_validation.py",
                     "rust/daemon/harness/test_mcp_wire.py"), PYTEST_FILTERS)
    require_command(mcp, ("python", "rust/daemon/harness/mcp_ci.py"),
                    ("--default-bin", "$candidate", "--mutant-bin", "$mutants", "--out"))
    assert not any(words[0] == "cargo" for words in mcp)
    security = commands(parity["Daemon security admission parity"]["run"])
    require_command(security, ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_security_cases.py",
                     "rust/daemon/harness/test_principals_settle.py"), PYTEST_FILTERS)
    security_rows = ("security-open", "security-closed", "security-encodings",
                     "security-encoding-priority", "security-terminal-byte")
    for mode in ("live", "golden", "mutants"):
        required = ("--rust-bin", "--only", "--out") + security_rows
        if mode == "live":
            required += ("security-refusals",)
        if mode == "mutants":
            required += ("--mutants", "security-origin-open", "security-host-open",
                         "security-browser-last", "security-auth-first", "security-drop-latin1",
                         "security-unavailable-401")
        require_command(security, ("python", "rust/daemon/harness/run.py", mode),
                        required, ("--record",) + (("--no-refusals",) if mode == "live" else ()))
    require_command(security, ("python", "rust/daemon/harness/run.py", "live"),
                    ("--rust-bin", "$bindFixture", "--only", "security-remote-open", "security-remote-auth", "--out"),
                    ("--record",))
    assert not any(words[0] == "cargo" for words in security)
    override = commands(rust["Daemon override build configurations"]["run"])
    for release in (False, True):
        required = ("--locked", "-p", "pseudolife-daemon", "mutants::tests::loopback_bind_override_is_absent_from_production_build", "--exact")
        required += ("--release", "--features", "mutants") if release else ()
        require_command(override, ("cargo", "+1.94.0", "test"), required, () if release else ("--release", "--features"))

    schema = commands(parity["Daemon schema startup parity"]["run"])
    require_command(schema, ("python", "rust/daemon/harness/schema_ci.py"),
                    ("--out", "--test-bin-out"), ("--record-goldens",))
    assert not any(words[0] == "cargo" for words in schema)
    startup = commands(parity["Daemon resident startup parity"]["run"])
    require_command(startup, ("python", "rust/daemon/harness/startup_ci.py"),
                    ("--out", "--no-build", "--rust-test-bin"), ("--record-goldens",))
    assert not any(words[0] == "cargo" for words in startup)
    assert "daemon-storage-test-path.txt" in parity["Daemon resident startup parity"]["run"]
    embedding = parity["Offline embedding golden and mutant row"]
    invocations = commands(embedding["run"])
    require_command(invocations, ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_embedding.py",), PYTEST_FILTERS)
    require_command(invocations, ("python", "rust/daemon/harness/run.py", "embedding"),
                    ("--fixture", "--golden", "rust/daemon/harness/goldens/embedding-fixture.json", "--rust-bin", "--out"),
                    ("--record", "--model", "--mutant"))
    assert "rust/target/release" in embedding["run"]
    assert not any(words[:2] == ["cargo", "build"] for words in invocations)

    principal = parity["Principal store differential and golden harness"]
    invocations = commands(principal["run"])
    for mode in ("live", "golden"):
        require_command(invocations, ("python", "rust/daemon/harness/principals.py", mode),
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
    daemon_build = next(step for step in jobs["candidate"]["steps"]
                        if step.get("name") == "Build embedding candidate")
    invocation = next(words for words in commands(daemon_build["run"])
                      if words[:2] == ["cargo", "build"])
    assert {"mutants", "principal-harness"} <= set(
        invocation[invocation.index("--features") + 1].split(","))
    steps = jobs["parity-checks"]["steps"]
    assert steps.index(parity["Prepare disposable PostgreSQL for CLI lease row"]) < steps.index(principal)
    upload = next(step for step in steps if step.get("name") == "Retain principal store outcomes")
    assert upload["if"] == "always() && matrix.suite == 'cli'"
    graph = commands(parity["Graph store differential and recorded oracle"]["run"])
    row_parser = argparse.ArgumentParser(add_help=False)
    row_parser.add_argument("--row", choices=["store", "read"], default="store")
    for mode in ("live", "golden"):
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

    script = parity["Run unchanged candidates and differential judges"]["run"]
    invocations = commands(script)
    files = ("test_cli_wait_mail.py", "test_wait_mail_phase2d_native.py", "test_wait_mail_reduction_native.py",
             "test_wait_mail_producer_native.py", "test_wait_mail_candidate_contract.py",
             "test_wait_mail_measurement.py", "test_wait_mail_listener_events.py")
    require_command(invocations, ("$oraclePython", "-m", "pytest"),
                    tuple("evals/rust_port/" + name for name in files), PYTEST_FILTERS)
    for module, flag in (("lease_headers", "--candidate"), ("cli_argv", "--candidate-json")):
        require_command(invocations, ("$oraclePython", "-m", "evals.rust_port." + module), (flag,))
    for variable, module in (("$judgeBootstrap", "phase1_ci"), ("$phase2Bootstrap", "cli_dispatch"),
                             ("$processBootstrap", "cli_process")):
        assert f'module_command("evals.rust_port.{module}"' in script
        require_command(invocations, ("$oraclePython", "-c", variable),
                        ("--oracle-root", "--candidate-json", "--candidate-root", "--out"))
    process = next(words for words in invocations if words[:3] == ["$oraclePython", "-c", "$processBootstrap"])
    modes = process[process.index("--modes") + 1:]
    modes = modes[:next((index for index, word in enumerate(modes) if word.startswith("--")), len(modes))]
    assert {"help", "version", "lease"} <= set(modes)


def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))



def test_h5_step_budgets_fit_their_jobs():
    shards = {shard["suite"]: shard for shard in workflow()["jobs"]["parity-checks"]["strategy"]["matrix"]["include"]
              if shard["suite"] in H5_SUITES}
    assert set(shards) == set(H5_SUITES)
    for name, shard in shards.items():
        # Checkout, toolchain, pip, artifacts and PostgreSQL prep take about
        # 15 minutes of the job on Linux, so the h5 step must leave that much.
        assert 0 < shard["h5_timeout"] and shard["h5_timeout"] + 15 <= shard["job_timeout"], name
        assert 0 < shard["h5_windows_timeout"] < shard["job_timeout"], name
    # Hosted ubuntu 2026-10-12: the audit step ran about 5 minutes and hit a
    # 4-minute budget three times with every compared case matching.
    assert shards["h5-audit"]["h5_timeout"] >= 10


def test_shards_cover_both_systems_and_build_once():
    jobs = workflow()["jobs"]
    for name in ("rust", "candidate", "parity-checks", "parity"):
        assert jobs[name]["strategy"]["matrix"]["os"] == SYSTEMS
        assert jobs[name]["strategy"]["fail-fast"] is False
        assert not jobs[name].get("continue-on-error", False)
    assert jobs["rust"]["name"] == "Rust / ${{ matrix.os }}"
    assert jobs["parity"]["name"] == "Parity / ${{ matrix.os }}"
    assert jobs["parity-checks"]["needs"] == ["candidate", "rust"]
    rust_steps = jobs["rust"]["steps"]
    override = commands(next(step["run"] for step in rust_steps
                             if step.get("name") == "Daemon override build configurations"))
    require_command(override, ("cargo", "+1.94.0", "build"),
                    ("--locked", "-p", "pseudolife-daemon", "--features", "mutants"), ("--release",))
    produced = next(step for step in rust_steps if step.get("name") == "Retain debug bind fixture")
    consumed = next(step for step in jobs["parity-checks"]["steps"]
                    if step.get("name") == "Download debug bind fixture")
    assert produced["with"]["name"] == consumed["with"]["name"] == "daemon-bind-fixture-${{ runner.os }}"
    assert produced["with"]["if-no-files-found"] == "error"
    assert "rust/target/debug/pseudolife-daemon" in produced["with"]["path"].splitlines()
    assert consumed["if"] == "matrix.suite == 'cli-live' || matrix.suite == 'h5-w1a'"
    assert consumed["with"]["path"] == "rust/target/debug"
    assert jobs["parity-checks"]["strategy"]["matrix"]["suite"] == PARITY_SUITES
    builds = [step for name in ("rust", "candidate", "parity-checks")
              for step in jobs[name]["steps"]
              if "cargo build --locked --release --bin pseudolife-stdio" in step.get("run", "")]
    assert len(builds) == 1
    assert builds[0]["run"].strip() == "cargo build --locked --release --bin pseudolife-stdio --bin pseudolife-daemon -j 3"
    assert not any("schema_ci.py" in step.get("run", "")
                   for step in jobs["candidate"]["steps"])


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
                        "startsWith(matrix.suite, 'cli')" if suite == "cli*" else
                        f"matrix.suite == '{suite}'" if suite else None)
            assert actual.get("if") == expected
    check_executable_coverage(jobs)


def test_capture_remains_separate_from_ordinary_ci():
    capture = workflow()["jobs"]["capture-windows"]
    assert capture["name"] == "Capture / Windows / wait-mail"
    assert capture["runs-on"] == "windows-latest"
    assert capture["if"] == "github.event_name == 'workflow_dispatch' && inputs.mode == 'wait-mail'"
    assert "needs" not in capture


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
    build = next(s for s in candidate["steps"] if s.get("name") == "Build embedding candidate")
    require_command(commands(build["run"]), ("cargo", "build"),
                    ("--locked", "--release", "-p", "pseudolife-daemon", "--bins", "--features"))
    invocation = next(words for words in commands(build["run"]) if words[:2] == ["cargo", "build"])
    assert "mutants" in invocation[invocation.index("--features") + 1].split(",")
    upload = next(s for s in candidate["steps"] if s.get("uses") == "actions/upload-artifact@v4")
    assert {"rust/target/release/pseudolife-daemon", "rust/target/release/pseudolife-daemon.exe"} <= set(upload["with"]["path"].splitlines())
    preserved = next(s for s in candidate["steps"] if s.get("name") == "Retain default static candidate")
    assert preserved.get("working-directory") == "${{ github.workspace }}"
    names = [s.get("name") for s in candidate["steps"]]
    assert names.index("Build candidate") < names.index("Retain default static candidate") < names.index("Build embedding candidate")
    assert {"rust/target/release/pseudolife-daemon-static", "rust/target/release/pseudolife-daemon-static.exe"} <= set(upload["with"]["path"].splitlines())
    assert "Copy-Item -LiteralPath" in preserved["run"]
    static = next(s for s in job["steps"] if s.get("name") == "Daemon static HTTP parity")
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
    assert builds[0] in jobs["candidate"]["steps"]
    words = commands(builds[0]["run"])[0]
    assert {"--locked", "--release", "--bins"} <= set(words)
    assert {"graph-harness", "mutants"} <= set(words[words.index("--features") + 1].split(","))
    upload = next(s for s in jobs["candidate"]["steps"]
                  if s.get("with", {}).get("name") == "rust-shim-${{ runner.os }}")
    for binary in ("pseudolife-daemon", "graph-contract"):
        for suffix in ("", ".exe"):
            assert f"rust/target/release/{binary}{suffix}" in upload["with"]["path"].splitlines()
    steps = jobs["parity-checks"]["steps"]
    download = next(s for s in steps if s.get("uses") == "actions/download-artifact@v4"
                    and s.get("with", {}).get("name") == upload["with"]["name"])
    assert "if" not in download
    assert download["with"]["path"] == "rust/target/release"
    permission = next(s for s in steps if s.get("name") == "Restore graph store executable permission")
    assert permission["if"] == "matrix.suite == 'cli' && runner.os == 'Linux'"
    assert permission["run"] == "chmod +x rust/target/release/graph-contract"


GATE_NEEDS = ["scope", "rust", "candidate", "parity-checks", "source-mutants"]


def run_gate(needs, event):
    import os
    step = workflow()["jobs"]["parity"]["steps"][0]
    return subprocess.run(["pwsh", "-NoProfile", "-Command", step["run"]],
                          env=dict(os.environ, GATE_RESULTS=json.dumps(needs), GATE_EVENT=event),
                          capture_output=True, text=True)


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", ""])
@pytest.mark.parametrize("dependency", ["parity-checks", "source-mutants"])
@pytest.mark.parametrize("event", ["pull_request", "merge_group", "push", "schedule", "workflow_dispatch"])
def test_required_parity_gate_fails_closed(result, dependency, event):
    gate = workflow()["jobs"]["parity"]
    assert gate["needs"] == GATE_NEEDS
    assert gate["if"].startswith("always() &&")
    step = gate["steps"][0]
    assert step["env"] == {"GATE_RESULTS": "${{ toJSON(needs) }}", "GATE_EVENT": "${{ github.event_name }}"}
    import os
    needs = {name: {"result": "success"} for name in gate["needs"]}
    needs[dependency]["result"] = result
    executed = subprocess.run(["pwsh", "-NoProfile", "-Command", step["run"]],
                              env=dict(os.environ, GATE_RESULTS=json.dumps(needs), GATE_EVENT=event),
                              capture_output=True, text=True)
    accepted = result == "success" or (event in ("pull_request", "merge_group", "push")
                                       and dependency == "source-mutants" and result == "skipped")
    assert executed.returncode == (0 if accepted else 1), executed.stderr


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


def run_scope(tmp_path, event, files, git_exit=0, base_ref="refs/heads/master", fetch_exit=0):
    import os
    step = next(s for s in workflow()["jobs"]["scope"]["steps"] if s.get("id") == "scope")
    assert step["env"] == {"EVENT_NAME": "${{ github.event_name }}",
                           "MERGE_GROUP_BASE_REF": "${{ github.event.merge_group.base_ref }}"}
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
    ["Rust/daemon/notes.md"],
    ["Plugin/skills/x/SKILL.md"],
    ['"docs/caf\\303\\251.md"'],
]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("files", DOCS_ONLY_CHANGES)
def test_scope_skips_a_documentation_only_pull_request(tmp_path, files, event):
    assert run_scope(tmp_path, event, files) == ["rust=false"]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("files", CODE_CHANGES)
def test_scope_runs_everything_for_any_other_change(tmp_path, files, event):
    assert run_scope(tmp_path, event, files) == ["rust=true"]


def test_scope_runs_everything_for_a_mixed_merge_group(tmp_path):
    # A docs PR and a code PR queued together: the group is not docs-only.
    group = ["CLAUDE.md", "docs/guide/configuration.md", "rust/daemon/src/main.rs"]
    assert run_scope(tmp_path, "merge_group", group) == ["rust=true"]


@pytest.mark.parametrize("base_ref, fetch_exit", [
    ("", 0), ("master", 0), ("refs/heads/topic", 0), ("refs/heads/master2", 0),
    ("REFS/HEADS/MASTER", 0), ("refs/heads/master", 128)])
def test_scope_runs_everything_without_a_fetched_master_tip(tmp_path, base_ref, fetch_exit):
    assert run_scope(tmp_path, "merge_group", ["CLAUDE.md"], base_ref=base_ref,
                     fetch_exit=fetch_exit) == ["rust=true"]


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_scope_never_skips_outside_pull_requests(tmp_path, event):
    assert run_scope(tmp_path, event, ["CLAUDE.md"]) == ["rust=true"]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("files, git_exit", [([], 0), (["CLAUDE.md"], 128)])
def test_scope_runs_everything_on_an_empty_or_unreadable_diff(tmp_path, files, git_exit, event):
    assert run_scope(tmp_path, event, files, git_exit) == ["rust=true"]


def concurrency_group(event, ref="refs/heads/master", mode=None, run_id="101"):
    """Evaluate the workflow's group expression the way Actions does."""
    expression = workflow()["concurrency"]["group"]
    assert expression.startswith("${{ ") and expression.endswith(" }}")
    body = expression[4:-3]
    # Actions' && / || return an operand, like Python's and / or.
    assert set(re.findall(r"[a-z_.]+(?=\s*==)", body)) <= {"github.event_name", "inputs.mode"}
    body = body.replace("&&", " and ").replace("||", " or ")
    body = body.replace("github.event_name", "event").replace("inputs.mode", "mode")
    body = body.replace("github.run_id", "run_id").replace("github.workflow", "'Rust shim'")
    body = body.replace("github.ref", "ref")
    import ast
    tree = ast.parse(body, mode="eval")
    allowed = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.Name,
               ast.Load, ast.Constant, ast.Call)
    # Only comparisons, and/or and format() calls; nothing else is evaluated.
    assert all(isinstance(node, allowed) for node in ast.walk(tree)), body
    assert all(node.func.id == "format" for node in ast.walk(tree) if isinstance(node, ast.Call))
    return eval(compile(tree, "<concurrency>", "eval"),
                {"__builtins__": {}, "format": lambda text, *args: text.format(*args)},
                {"event": event, "mode": mode, "run_id": run_id, "ref": ref})


def test_nightly_never_shares_a_group_with_master_pushes():
    push = concurrency_group("push")
    assert concurrency_group("schedule") != push
    assert concurrency_group("schedule") == concurrency_group("schedule")
    # A newer push still supersedes an older one, and pull requests stay per-ref.
    assert concurrency_group("push") == push
    assert concurrency_group("pull_request", "refs/pull/1/merge") != concurrency_group(
        "pull_request", "refs/pull/2/merge")
    assert concurrency_group("workflow_dispatch", mode="wait-mail") != concurrency_group(
        "workflow_dispatch", mode="wait-mail", run_id="102")
    # Manual CI on master must not cancel the push run either.
    assert concurrency_group("workflow_dispatch", mode="ci") not in (push, concurrency_group("schedule"))
    assert concurrency_group("workflow_dispatch", mode="ci") == concurrency_group("workflow_dispatch", mode="ci")
    assert workflow()["concurrency"]["cancel-in-progress"] is True


def test_merge_group_runs_share_no_group_with_pull_requests_or_pushes():
    # Each queue entry has its own branch ref, so neither a PR's newer push nor
    # a master push can cancel the queue's run, and it cancels neither.
    queued = "refs/heads/gh-readonly-queue/master/pr-830-" + "a" * 40
    group = concurrency_group("merge_group", queued)
    assert group not in (concurrency_group("push"), concurrency_group("schedule"),
                         concurrency_group("pull_request", "refs/pull/830/merge"),
                         concurrency_group("workflow_dispatch", mode="ci"))
    assert group != concurrency_group(
        "merge_group", "refs/heads/gh-readonly-queue/master/pr-831-" + "b" * 40)


def test_merge_queue_trigger_and_image_base():
    events = workflow().get("on", workflow().get(True))
    assert "merge_group" in events and "pull_request" in events
    step = next(s for s in workflow()["jobs"]["daemon-image"]["steps"]
                if s.get("id") == "image-inputs")
    assert step["env"]["BASE_SHA"] == ("${{ github.event.pull_request.base.sha || "
                                       "github.event.merge_group.base_sha || github.event.before }}")


@pytest.mark.parametrize("event, runs", [("pull_request", False), ("merge_group", False), ("push", False),
                                         ("schedule", True), ("workflow_dispatch", True)])
def test_source_mutants_run_nightly_and_on_manual_ci_only(event, runs):
    condition = workflow()["jobs"]["source-mutants"]["if"]
    assert condition == ("${{ !cancelled() && (github.event_name == 'schedule' || "
                         "(github.event_name == 'workflow_dispatch' && inputs.mode == 'ci')) "
                         "&& needs.candidate.result == 'success' }}")
    assert (f"'{event}'" in condition) == runs


def test_scope_gates_every_heavy_job_and_fails_safe():
    jobs = workflow()["jobs"]
    scope = jobs["scope"]
    assert scope["runs-on"] == "ubuntu-latest" and "needs" not in scope
    assert scope["outputs"] == {"rust": "${{ steps.scope.outputs.rust }}"}
    checkout = next(s for s in scope["steps"] if s.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["fetch-depth"] == 2
    gated = ("${{ !cancelled() && (github.event_name != 'workflow_dispatch' || inputs.mode != 'wait-mail') "
             "&& needs.scope.outputs.rust != 'false' }}")
    for name in ("rust", "candidate", "daemon-image"):
        assert jobs[name]["needs"] == "scope", name
        assert jobs[name]["if"] == gated, name
    # Parity checks and source mutants inherit the skip through their needs.
    # Their success checks name the direct needs: the implicit success() also
    # covers scope, so a failed scope would skip them after rust/candidate ran.
    assert jobs["parity-checks"]["needs"] == ["candidate", "rust"]
    assert jobs["parity-checks"]["if"].startswith("${{ !cancelled() && ")
    assert jobs["parity-checks"]["if"].endswith(
        "&& needs.candidate.result == 'success' && needs.rust.result == 'success' }}")
    assert jobs["source-mutants"]["needs"] == ["candidate"]
    assert jobs["source-mutants"]["if"].startswith("${{ !cancelled() && ")
    assert jobs["source-mutants"]["if"].endswith("&& needs.candidate.result == 'success' }}")
    assert jobs["parity"]["needs"][0] == "scope"


@pytest.mark.parametrize("name", ["CLI lease differential harness", "Daemon background pruning golden"])
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
                if s.get("name") == "CLI W1-C differential harness")
    del step["if"]
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


@pytest.mark.parametrize("condition", [None, "matrix.suite == 'eval'", "matrix.suite == 'judges'"])
def test_principal_store_requires_the_postgres_shard(monkeypatch, condition):
    changed = copy.deepcopy(workflow())
    step = next(s for s in changed["jobs"]["parity-checks"]["steps"]
                if s.get("name") == "Principal store differential and golden harness")
    step["if"] = condition
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


@pytest.mark.parametrize("mode", ["live", "golden"])
@pytest.mark.parametrize("row", ["store", "read"])
def test_graph_coverage_rejects_each_removed_invocation(mode, row):
    jobs = copy.deepcopy(workflow()["jobs"])
    step = next(s for s in jobs["parity-checks"]["steps"]
                if s.get("name") == "Graph store differential and recorded oracle")
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
                if s.get("name") == "Graph store differential and recorded oracle")
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
                 if s.get("name") == "Graph store differential and recorded oracle")
    assert graph["env"]["PL_GRAPH_REQUIRE_RECORDED_LOCALE"] == "${{ runner.os == 'Linux' && '1' || '0' }}"


@pytest.mark.parametrize("features", ["mutants", "principal-harness", ""])
def test_daemon_artifact_retains_both_harness_features(monkeypatch, features):
    changed = copy.deepcopy(workflow())
    step = next(s for s in changed["jobs"]["candidate"]["steps"]
                if s.get("name") == "Build embedding candidate")
    invocation = commands(step["run"])[0]
    original = invocation[invocation.index("--features") + 1]
    step["run"] = step["run"].replace(original, features)
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


@pytest.mark.parametrize("name, old, new", [
    ("Run every eval harness test", "--junitxml", "--durations=10 --junitxml"),
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
    ("Daemon security admission parity", "security-refusals", ""),
    ("Daemon security admission parity", "security-refusals --out", "security-refusals --no-refusals --out"),
    ("Daemon security admission parity", "--mutants security-origin-open", "--mutants"),
    ("Daemon security admission parity", "rust/daemon/harness/test_security_cases.py", ""),
    ("Daemon security admission parity", "security-remote-open", ""),
    ("Check generated daemon schema", "python rust/daemon/harness/gen_schema_sql.py", "# python rust/daemon/harness/gen_schema_sql.py"),
    ("Daemon resident startup parity", "--no-build", ""),
    ("Daemon resident startup parity", "--rust-test-bin $candidate", ""),
    ("Daemon resident startup parity", "--no-build", "--no-build --record-goldens"),
    ("Daemon schema startup parity", "--out", "--record-goldens --out"),
    ("Daemon schema startup parity", "python rust/daemon/harness/schema_ci.py", "# python rust/daemon/harness/schema_ci.py"),
    ("CLI differential harness", "--row hook", ""),
    ("CLI differential harness", "rust/cli_harness/test_backup.py", ""),
    ("CLI differential harness", "$env:PSEUDOLIFE_RUST_BINARY = $candidate", "$env:PSEUDOLIFE_RUST_BINARY = 'debug-only'"),
    ("CLI differential harness", "$env:PSEUDOLIFE_BACKUP_NATIVE_REQUIRED = '1'", "$env:PSEUDOLIFE_BACKUP_NATIVE_REQUIRED = '0'"),
    ("Graph store differential and recorded oracle", "python rust/daemon/harness/graph_store.py live", "# python rust/daemon/harness/graph_store.py live"),
    ("CLI differential harness", "--golden", ""),
    ("Run unchanged candidates and differential judges", "--modes help version lease", "--modes help version"),
    ("Run unchanged candidates and differential judges", "& $oraclePython -c $judgeBootstrap", "# & $oraclePython -c $judgeBootstrap"),
    ("Run every eval harness test", "evals/rust_baseline", ""),
    ("Clippy", "--all-targets", ""),
    ("Check write-path harness targets", "harness,mutants", "mutants"),
    ("Check write-path harness targets", "--all-targets", ""),
    ("Nextest without default features", "--no-default-features", ""),
    ("Nextest without default features", "--no-default-features", "--no-default-features --features codex-delivery"),
    ("Nextest", "cargo nextest run", "cargo --version #"),
    ("Principal store differential and golden harness", "principals.py live", "principals.py oracle"),
    ("Principal store differential and golden harness", "principals.py golden", "principals.py live"),
    ("Principal store differential and golden harness", "rust/target/release", "rust/target/debug"),
    ("Offline embedding golden and mutant row", "--fixture", ""),
    ("Offline embedding golden and mutant row", "--golden", "--record"),
    ("Offline embedding golden and mutant row", "rust/daemon/harness/test_embedding.py", ""),
    ("Offline W2-D search harness regressions", "rust/daemon/harness/test_w2d_controls.py", ""),
    ("Offline W2-D search harness regressions", "rust/daemon/harness/test_w2d_namespace.py", ""),
    ("CLI W1-C differential harness", "$rows = @(${{ matrix.w1c_rows }})", "$rows = @('move')"),
    ("CLI W1-C differential harness", "python rust/cli_harness @golden",
     "# python rust/cli_harness @golden"),
    ("CLI W1-C differential harness", "@golden --golden", "@golden"),
    ("CLI W1-C differential harness", "@live --candidate", "@live --skip-bank --candidate"),
    ("CLI W1-C differential harness", "$golden = $replayed |", "$golden = @('move') |"),
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
                if s.get("name") == "Dream cursor differential harness")
    invocations = commands(step["run"])
    for mode in ("golden", "mutants"):
        require_command(invocations, ("python", "rust/daemon/harness/dream_cursors.py", mode),
                        ("--ci-fixture", "--rust-bin"))
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
    build = next(s for s in jobs["candidate"]["steps"] if s.get("name") == "Build embedding candidate")
    words = commands(build["run"])[0]
    assert "contract-harness" in words[words.index("--features") + 1].split(",")
    upload = next(s for s in jobs["candidate"]["steps"] if s.get("uses") == "actions/upload-artifact@v4")
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
                if s.get("name") == "Dream cursor differential harness")
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
    ("principals.py live --rust-bin $candidate", "principals.py live --rust-bin stale-candidate"),
    ("principals.py golden --rust-bin $candidate", "principals.py golden --rust-bin stale-candidate"),
    ("$candidate = (Resolve-Path", "$unused = (Resolve-Path"),
    ("'pseudolife-daemon.exe'", "'stale-daemon.exe'"),
    ("'pseudolife-daemon'", "'stale-daemon'"),
])
def test_principal_commands_use_the_downloaded_candidate(monkeypatch, old, new):
    changed = copy.deepcopy(workflow())
    step = next(s for s in changed["jobs"]["parity-checks"]["steps"]
                if s.get("name") == "Principal store differential and golden harness")
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
                if s.get("name") == "CLI W1-C differential harness")
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
    ("CLI W1-C differential harness", "matrix.suite == 'cli'"),
    ("Prepare disposable PostgreSQL for W1-C rows", "matrix.suite == 'cli'"),
    ("Build the test_login container image", "runner.os == 'Linux'"),
    ("Record the Linux test_login golden while it is absent", "runner.os == 'Linux'"),
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
@pytest.mark.parametrize("golden_exists", [False, True])
def test_w1c_powershell_passes_the_same_rows_live_and_golden(tmp_path, suite, windows, golden_exists):
    import os
    job = workflow()["jobs"]["parity-checks"]
    shard = next(s for s in job["strategy"]["matrix"]["include"] if s["suite"] == suite)
    step = next(s for s in job["steps"] if s.get("name") == "CLI W1-C differential harness")
    script = step["run"].replace("${{ matrix.w1c_rows }}", shard["w1c_rows"])
    script = script.replace("$IsWindows", "$fixtureWindows")
    bootstrap = r'''
$fixtureWindows = $env:PL_FIXTURE_WINDOWS -eq '1'
$global:calls = @()
function Resolve-Path { [pscustomobject]@{Path='fixture-candidate'} }
function Test-Path { $env:PL_FIXTURE_GOLDEN -eq '1' }
function python {
    $global:calls += ,@($args)
    $global:LASTEXITCODE = 0
}
'''
    # Replace the step's final exit only to observe both standalone calls.
    script = script.removesuffix("exit $LASTEXITCODE\n")
    script += "ConvertTo-Json -InputObject $global:calls -Compress -Depth 5\n"
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), PL_FIXTURE_WINDOWS=str(int(windows)),
               PL_FIXTURE_GOLDEN=str(int(golden_exists)))
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", bootstrap + script],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = json.loads(result.stdout)
    rows = re.findall(r"'([a-z_]+)'", shard["w1c_rows"])
    assert len(calls) == 2
    for call, golden in zip(calls, (False, True)):
        expected = [row for row in rows if not golden or row != "test_login" or golden_exists]
        actual = [call[i + 1] for i, word in enumerate(call[:-1]) if word == "--row"]
        assert actual == expected
        assert ("--golden" in call) == golden
        assert call[call.index("--candidate") + 1] == "fixture-candidate"


def test_timeout_reduction_is_limited_to_cli_and_new_w1c_suites():
    job = workflow()["jobs"]["parity-checks"]
    assert job["timeout-minutes"] == "${{ matrix.job_timeout || 210 }}"
    budgets = {shard["suite"]: shard.get("job_timeout")
               for shard in job["strategy"]["matrix"]["include"]}
    # Only eval and judges keep the old 210-minute fallback.
    assert set(PARITY_SUITES) - set(budgets) == {"eval", "judges"}


def test_no_cli_shard_is_an_hour_long_serial_job():
    # Hosted Windows ran cli as one 61-70 minute job (master runs
    # 39205533092, 39236836263, 2026-10-19). Each shard now measures about
    # 26 minutes there, and its budget stays about twice that.
    job = workflow()["jobs"]["parity-checks"]
    budgets = {shard["suite"]: shard.get("job_timeout")
               for shard in job["strategy"]["matrix"]["include"]}
    for suite in CLI_SUITES:
        assert 0 < budgets[suite] <= 60, suite
    steps = job["steps"]
    by_suite = {}
    for step in steps:
        for suite in CLI_SUITES:
            if step.get("if") == f"matrix.suite == '{suite}'" and step.get("run"):
                by_suite.setdefault(suite, []).append(step["name"])
    # Resident startup reads the storage test binary the schema step builds.
    assert by_suite["cli-daemon"] == ["Daemon schema startup parity", "Daemon resident startup parity"]
    names = [step.get("name") for step in steps]
    assert names.index("Daemon schema startup parity") < names.index("Daemon resident startup parity")
    for upload in ("Retain daemon schema parity", "Retain daemon resident startup parity"):
        assert next(s for s in steps if s.get("name") == upload)["if"] == \
            "always() && matrix.suite == 'cli-daemon'"


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
                     "rust/daemon/harness/board/test_run.py"), PYTEST_FILTERS)


def test_rust_job_fits_a_cold_cache_and_bounds_the_override_builds():
    job = workflow()["jobs"]["rust"]
    override = next(step for step in job["steps"] if step.get("name") == "Daemon override build configurations")
    # Hosted windows 2026-10-13: a cold-cache run took 23.9 minutes; on
    # 2026-10-15 three cold runs were cancelled at 25 inside this step. A
    # cancelled run saves no cache, so the next run on that image is cold too.
    assert job["timeout-minutes"] >= 45
    assert 0 < override["timeout-minutes"] < job["timeout-minutes"]


# --- self-hosted comparison lane (docs/specs/2026-10-19-self-hosted-parity-lane.md) ---

SELF_HOSTED = "runner.environment == 'self-hosted'"
GATE_STEP = "Yield to this machine's full suite"
VENV_STEP = "Give the shard its own Python environment"


def test_selfhosted_lane_runs_the_hosted_shards_unchanged():
    text = WORKFLOW.read_text(encoding="utf-8")
    # Anchors, not a copy: a step or shard added to the hosted matrix reaches
    # the self-hosted lane in the same edit.
    for alias in ("suite: *parity_suites", "include: *parity_shards"):
        assert text.count(alias) == 1, alias
    # parity-selfhosted and parity-live both reuse the hosted steps.
    assert text.count("steps: *parity_steps") == 2
    jobs = workflow()["jobs"]
    hosted, lane = jobs["parity-checks"], jobs["parity-selfhosted"]
    assert lane["steps"] == hosted["steps"]
    assert lane["strategy"]["matrix"]["suite"] == hosted["strategy"]["matrix"]["suite"]
    assert lane["strategy"]["matrix"]["include"] == hosted["strategy"]["matrix"]["include"]
    assert lane["strategy"]["matrix"]["runner"] == (
        "${{ fromJSON(vars.PARITY_SELFHOSTED_RUNNERS || '[\"off\"]') }}")
    assert lane["strategy"]["fail-fast"] is False
    assert lane["needs"] == hosted["needs"] == ["candidate", "rust"]
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
    assert "parity-selfhosted" not in jobs["parity"]["steps"][0]["run"]
    # Nightly and manual CI only: their concurrency groups are their own, so
    # queued self-hosted jobs neither hold a PR's checks open nor let a newer
    # master push cancel a run whose hosted jobs already finished.
    assert lane["if"] == (
        "${{ !cancelled() && (github.event_name == 'schedule' || "
        "(github.event_name == 'workflow_dispatch' && inputs.mode == 'ci')) "
        "&& vars.PARITY_SELFHOSTED_RUNNERS != '' "
        "&& needs.candidate.result == 'success' && needs.rust.result == 'success' }}")
    assert "pull_request" not in lane["if"] and "'push'" not in lane["if"]


def test_both_lanes_upload_under_distinct_names():
    jobs = workflow()["jobs"]
    assert jobs["parity-checks"]["env"]["PARITY_ARTIFACT_PREFIX"] == ""
    # Hosted Windows runs cli-live only in parity-live, so its uploads
    # cannot collide with a parity-checks shard's.
    assert jobs["parity-live"]["env"]["PARITY_ARTIFACT_PREFIX"] == ""
    assert jobs["parity-selfhosted"]["env"]["PARITY_ARTIFACT_PREFIX"] == "self-hosted-"
    uploads = [step for step in jobs["parity-checks"]["steps"]
               if step.get("uses") == "actions/upload-artifact@v4"]
    assert uploads
    for step in uploads:
        assert step["with"]["name"].startswith(ARTIFACT_PREFIX), step.get("name")


def test_self_hosted_only_steps_leave_the_hosted_shard_unchanged():
    steps = workflow()["jobs"]["parity-checks"]["steps"]
    gate = steps[0]
    assert gate["name"] == GATE_STEP and gate["if"] == SELF_HOSTED
    assert steps[1]["uses"] == "actions/checkout@v4"
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
    assert [step["name"] for step in added] == [GATE_STEP, VENV_STEP]


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
    ("disk below the floor", [[0, "free"]], {"PARITY_MIN_FREE_GB": "1000000"}, False, 0, 0),
])
def test_selfhosted_gate_runs_only_on_a_free_lease(tmp_path, case, sequence, overrides,
                                                   accepted, calls, sleeps):
    import os
    gate = workflow()["jobs"]["parity-checks"]["steps"][0]
    log, summary = tmp_path / "calls.log", tmp_path / "summary.md"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ, PL_LOG=str(log), PL_SEQUENCE=json.dumps(sequence),
               GITHUB_WORKSPACE=str(tmp_path), GITHUB_STEP_SUMMARY=str(summary),
               PSEUDOLIFE_PARITY_LEASE="full-suite@fixture",
               PSEUDOLIFE_PARITY_LOCK_DIR=str(tmp_path / "locks"),
               **workflow()["jobs"]["parity-selfhosted"]["env"])
    # Only the low-disk case may depend on this machine's free space.
    env["PARITY_MIN_FREE_GB"] = "0"
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


# --- the required gate holds only deterministic Windows work ---

LIVE_EVENTS = ("${{ !cancelled() && (github.event_name == 'push' || github.event_name == 'schedule' || "
               "(github.event_name == 'workflow_dispatch' && inputs.mode == 'ci')) "
               "&& needs.candidate.result == 'success' && needs.rust.result == 'success' }}")


def test_live_comparisons_run_in_their_own_suite():
    steps = {step.get("name"): step for step in workflow()["jobs"]["parity-checks"]["steps"]}
    for name in LIVE_CHECKS:
        assert steps[name]["if"] == f"matrix.suite == '{LIVE_SUITE}'", name
    # No other check routes to the live suite: everything else Windows runs
    # under the required gate is a build, a self-test, a golden or a replay.
    routed = {name for name, step in steps.items()
              if LIVE_SUITE in step.get("if", "") and step.get("run")}
    assert routed == set(LIVE_CHECKS)
    # The golden arm of the same rows stays in the required cli shard.
    golden = commands(steps["CLI differential harness"]["run"])
    assert not [words for words in golden if words[:2] == ["python", "rust/cli_harness"]
                and "--golden" not in words]


def test_linux_requires_the_live_comparisons_and_windows_reports_them():
    jobs = workflow()["jobs"]
    matrix = jobs["parity-checks"]["strategy"]["matrix"]
    assert LIVE_SUITE in matrix["suite"] and "ubuntu-latest" in matrix["os"]
    assert matrix["exclude"] == WINDOWS_LIVE_EXCLUDE
    live = jobs["parity-live"]
    assert live["strategy"]["matrix"] == {"os": ["windows-latest"], "suite": [LIVE_SUITE]}
    assert live["strategy"]["fail-fast"] is False
    assert live["steps"] == jobs["parity-checks"]["steps"]
    assert live["needs"] == ["candidate", "rust"]
    assert live["runs-on"] == "${{ matrix.os }}"
    assert 0 < live["timeout-minutes"] <= 60


def test_windows_live_job_reports_but_never_gates():
    jobs = workflow()["jobs"]
    live = jobs["parity-live"]
    assert live["continue-on-error"] is True
    for name, job in jobs.items():
        needs = job.get("needs", [])
        assert "parity-live" not in ([needs] if isinstance(needs, str) else needs), name
    assert "parity-live" not in jobs["parity"]["steps"][0]["run"]
    # Master pushes, the nightly and manual CI: never a pull request or a
    # merge group, whose required checks it would otherwise hold open.
    assert live["if"] == LIVE_EVENTS
    assert "pull_request" not in live["if"] and "merge_group" not in live["if"]
    assert live["name"] == "Parity live (non-gating) / ${{ matrix.os }} / ${{ matrix.suite }}"
    assert not live["name"].startswith("Parity /")


# --- Rust caches: one key per compiling suite, saved on failure, no image toolchain ---

TOOLCHAIN_STEP = "Keep only the pinned Rust toolchain"
# The parity suites that compile (sent-YAML mutants in cli, the storage test
# binary in cli-daemon, which_comparison in eval); the rest restored a 71 MB
# registry they never read (master run 39236836263, 2026-10-19).
CACHED_SUITES = {"eval", "cli", "cli-daemon"}
CACHED_IF = "matrix.suite == 'eval' || matrix.suite == 'cli' || matrix.suite == 'cli-daemon'"


def pinned_channel():
    text = (ROOT / "rust/rust-toolchain.toml").read_text(encoding="utf-8")
    return re.search(r'^channel = "([^"]+)"$', text, re.M).group(1)


def cached_jobs():
    jobs = workflow()["jobs"]
    return {name: job for name, job in jobs.items()
            if any(step.get("uses") == "Swatinem/rust-cache@v2" for step in job.get("steps", []))}


def test_every_rust_cache_is_saved_on_failure_and_keeps_the_lockfile_hash():
    jobs = cached_jobs()
    assert set(jobs) >= {"rust", "candidate", "parity-checks", "source-mutants"}
    for name, job in jobs.items():
        cache = next(step for step in job["steps"] if step.get("uses") == "Swatinem/rust-cache@v2")
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
