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
    "CLI differential harness": "cli",
    "Prepare disposable PostgreSQL for CLI lease row": "cli",
    "CLI lease differential harness": "cli",
    "CLI W1-C differential harness": "w1c",
    "Prepare disposable PostgreSQL for W1-C rows": "w1c",
    "Daemon background pruning golden": "cli",
    "Dream cursor differential harness": "cli",


    "Principal store differential and golden harness": "cli",
    "Daemon static HTTP parity": "cli",

    "Daemon schema startup parity": "cli",
    "Graph store differential and recorded oracle": "cli",
    "Daemon body admission parity": "cli",
    "Daemon security admission parity": "cli",
    "Daemon resident startup parity": "cli",
    "Run unchanged candidates and differential judges": "judges",
}
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
    assert matrix["suite"] == ["eval", "cli", "judges", *W1C_SUITES]
    assert "exclude" not in matrix
    shards = matrix["include"]
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
    assert retained["with"]["name"] == "test_login-linux-golden-${{ matrix.suite }}"
    assert retained["with"]["path"].splitlines() == [
        "rust/cli_harness/goldens/test_login.linux.json",
        "${{ runner.temp }}/test-login-linux-record.json"]
    summaries = steps["Retain CLI harness summaries"]
    assert summaries["if"] == ("always() && (matrix.suite == 'cli' || "
                               "startsWith(matrix.suite, 'w1c-'))")
    assert summaries["with"]["name"] == "cli-harness-${{ runner.os }}-${{ matrix.suite }}"
    assert summaries["with"]["path"] == "${{ runner.temp }}/cli-harness*.json"
    # A second explicit row invocation outside the shard step duplicates work.
    for step in job["steps"]:
        if step is record:
            continue
        for words in commands(step.get("run", "")):
            if words[:2] == ["python", "rust/cli_harness"]:
                assert not any(word in W1C_ROWS for word in words)
    for step in job["steps"]:
        if step.get("uses") in ("dtolnay/rust-toolchain@stable", "Swatinem/rust-cache@v2"):
            assert step["if"] == "!startsWith(matrix.suite, 'w1c-')"


def check_executable_coverage(jobs):
    rust = {step.get("name"): step for step in jobs["rust"]["steps"]}
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
    for name, rows in (("CLI differential harness", ("mail", "hook", "episode")),
                       ("CLI lease differential harness", ("lease",))):
        invocations = commands(parity[name]["run"])
        for golden in (False, True):
            row_commands(invocations, rows, golden)
    require_command(commands(parity["CLI differential harness"]["run"]),
                    ("python", "-m", "pytest"),
                    ("rust/cli_harness/test_cli_harness.py", "rust/cli_harness/test_lease.py",
                     "rust/cli_harness/test_bank.py"), PYTEST_FILTERS)
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
    security = commands(parity["Daemon security admission parity"]["run"])
    require_command(security, ("python", "-m", "pytest"),
                    ("rust/daemon/harness/test_security_cases.py",), PYTEST_FILTERS)
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
    assert consumed["if"] == "matrix.suite == 'cli'"
    assert consumed["with"]["path"] == "rust/target/debug"
    assert jobs["parity-checks"]["strategy"]["matrix"]["suite"] == ["eval", "cli", "judges", *W1C_SUITES]
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
            expected = "startsWith(matrix.suite, 'w1c-')" if suite == "w1c" else (
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


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", ""])
def test_required_parity_gate_fails_closed(result):
    gate = workflow()["jobs"]["parity"]
    assert gate["needs"] == ["rust", "candidate", "parity-checks"]
    assert gate["if"].startswith("always() &&")
    step = gate["steps"][0]
    assert step["env"] == {"GATE_RESULTS": "${{ toJSON(needs) }}"}
    import os
    needs = {name: {"result": "success"} for name in gate["needs"]}
    needs["parity-checks"]["result"] = result
    executed = subprocess.run(["pwsh", "-NoProfile", "-Command", step["run"]],
                              env=dict(os.environ, GATE_RESULTS=json.dumps(needs)),
                              capture_output=True, text=True)
    assert executed.returncode == (0 if result == "success" else 1), executed.stderr


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
    ("Graph store differential and recorded oracle", "python rust/daemon/harness/graph_store.py live", "# python rust/daemon/harness/graph_store.py live"),
    ("CLI differential harness", "--golden", ""),
    ("Run unchanged candidates and differential judges", "--modes help version lease", "--modes help version"),
    ("Run unchanged candidates and differential judges", "& $oraclePython -c $judgeBootstrap", "# & $oraclePython -c $judgeBootstrap"),
    ("Run every eval harness test", "evals/rust_baseline", ""),
    ("Clippy", "--all-targets", ""),
    ("Nextest without default features", "--no-default-features", ""),
    ("Nextest without default features", "--no-default-features", "--no-default-features --features codex-delivery"),
    ("Nextest", "cargo nextest run", "cargo --version #"),
    ("Principal store differential and golden harness", "principals.py live", "principals.py oracle"),
    ("Principal store differential and golden harness", "principals.py golden", "principals.py live"),
    ("Principal store differential and golden harness", "rust/target/release", "rust/target/debug"),
    ("Offline embedding golden and mutant row", "--fixture", ""),
    ("Offline embedding golden and mutant row", "--golden", "--record"),
    ("Offline embedding golden and mutant row", "rust/daemon/harness/test_embedding.py", ""),
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
    assert matrix["suite"] == ["eval", "cli", "judges", *W1C_SUITES]
    assert matrix["os"] == SYSTEMS
    step = next(s for s in jobs["parity-checks"]["steps"]
                if s.get("name") == "CLI W1-C differential harness")
    assert step["if"] == "startsWith(matrix.suite, 'w1c-')"
    assert jobs["parity"]["needs"] == ["rust", "candidate", "parity-checks"]
    check_executable_coverage(jobs)


@pytest.mark.parametrize("row", W1C_ROWS)
@pytest.mark.parametrize("mutation", ["drop", "duplicate"])
def test_w1c_coverage_rejects_each_missing_or_duplicate_row(row, mutation):
    jobs = copy.deepcopy(workflow()["jobs"])
    shards = jobs["parity-checks"]["strategy"]["matrix"]["include"]
    shard = next(s for s in shards if f"'{row}'" in s["w1c_rows"])
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
    assert job["timeout-minutes"] == (
        "${{ matrix.job_timeout || (matrix.suite == 'cli' && 100 || 210) }}")
