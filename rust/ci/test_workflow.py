"""Keep every Rust gate while changing the workflow's execution graph."""

import copy
import json
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
    "Run every eval harness test": "eval",
    "Offline embedding golden and mutant row": "eval",
    "CLI differential harness": "cli",
    "Prepare disposable PostgreSQL for CLI lease row": "cli",
    "CLI lease differential harness": "cli",
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
        if not line.startswith(("python ", "cargo ", "& $oraclePython ")):
            continue
        words = shlex.split(line, comments=True)
        if words[0] == "&":
            words = words[1:]
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

    parity = {step.get("name"): step for step in jobs["parity-checks"]["steps"]}
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
                        required, ("--record",))
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

    graph = commands(parity["Graph store differential and recorded oracle"]["run"])
    for mode in ("live", "golden"):
        require_command(graph, ("python", "rust/daemon/harness/graph_store.py", mode),
                        ("--candidate", "--out"), ("--record",))
    require_command(graph, ("python", "rust/daemon/harness/gen_graph_unicode.py", "--check"))
    require_command(graph, ("python", "rust/daemon/harness/test_graph_store_harness.py"))

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
    assert jobs["parity-checks"]["strategy"]["matrix"]["suite"] == ["eval", "cli", "judges"]
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
            assert actual.get("if") == (f"matrix.suite == '{suite}'" if suite else None)
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


def test_coverage_guard_rejects_a_removed_check(monkeypatch):
    changed = copy.deepcopy(workflow())
    steps = changed["jobs"]["parity-checks"]["steps"]
    steps[:] = [s for s in steps if s.get("name") != "CLI lease differential harness"]
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


@pytest.mark.parametrize("name, old, new", [
    ("Run every eval harness test", "--junitxml", "--durations=10 --junitxml"),
    ("CLI differential harness", "--row episode", "--row episode --row doctor"),
])
def test_coverage_contract_allows_additions(monkeypatch, name, old, new):
    changed = copy.deepcopy(workflow())
    step = next(s for s in changed["jobs"]["parity-checks"]["steps"]
                if s.get("name") == name)
    step["run"] = step["run"].replace(old, new)
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    test_original_checks_remain_gated_on_the_expected_shards()


@pytest.mark.parametrize("name, old, new", [
    ("Check generated daemon schema", "--check", ""),
    ("Daemon security admission parity", "security-refusals", ""),
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
    ("Offline embedding golden and mutant row", "--fixture", ""),
    ("Offline embedding golden and mutant row", "--golden", "--record"),
    ("Offline embedding golden and mutant row", "rust/daemon/harness/test_embedding.py", ""),
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
