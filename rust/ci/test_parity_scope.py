"""The parity matrix a run gets: every suite, or on a pull request the reached ones."""

import json
import re
from pathlib import Path

import pytest
import yaml

import parity_scope
from parity_scope import ALL, FLOOR, ROW_SUITES, SHARDS, SHIM_COMMANDS, SUITES, matrix, reached, select
from test_workflow import CODE_CHANGES, DOCS_ONLY_CHANGES, W1C_SUITES

ROOT = Path(__file__).resolve().parents[2]


def raw_workflow():
    return yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch", "merge_group"])
def test_every_event_but_a_pull_request_runs_every_suite(event):
    assert select(event, ["rust/cli_harness/rows/doctor.py"])[0] == SUITES


@pytest.mark.parametrize("files", [None, []])
def test_an_unreadable_or_empty_pull_request_diff_runs_every_suite(files):
    assert select("pull_request", files)[0] == SUITES


@pytest.mark.parametrize("files, extra", [
    # Python tests, the Console bundle, other workflows and docs: the floor.
    (["tests/test_shim.py", "CHANGELOG.md", "changelog.d/2026-10-20-x.md"], set()),
    (["frontend/src/App.tsx", ".github/workflows/ci.yml", "rust/PARITY.md", "rust/daemon/divergences.md"], set()),
    # A row, its golden or its spec reaches the shard that runs it.
    (["rust/cli_harness/rows/doctor.py"], {"w1c-doctor"}),
    (["rust/cli_harness/goldens/test_login.linux.json"], {"w1c-connection"}),
    (["rust/cli_harness/rows/invite.md", "rust/cli_harness/rows/audit.py"], {"w1c-pairing", "h5-audit"}),
    (["rust/cli_harness/rows/mail.py", "rust/cli_harness/specs/CLI-LEASE-core.md"], set()),
    # Python tests the harness never imports: the floor's MCP catalogue step.
    (["tests/test_shim.py", "tests/conftest.py"], set()),
    # One command's shim code reaches its rows and the judges.
    (["rust/shim/src/cli/doctor/mod.rs"], {"w1c-doctor", "judges"}),
    (["rust/shim/src/cli/transfer/mod.rs"], {"w1c-pairing", "judges"}),
    # Helpers other commands import reach the whole binary (Codex review:
    # tunnel uses hook_json, board_audit uses lease).
    (["rust/shim/src/cli/hook_json.rs"], set(parity_scope.SHIM) - set(FLOOR)),
    (["rust/shim/src/cli/lease/args.rs"], set(parity_scope.SHIM) - set(FLOOR)),
    (["rust/shim/src/cli/pairing/invite.rs"], set(parity_scope.SHIM) - set(FLOOR)),
    (["rust/shim/src/cli/wait_mail.rs"], set(parity_scope.SHIM) - set(FLOOR)),
    (["rust/shim/src/main.rs"], set(parity_scope.SHIM) - set(FLOOR)),
    # The daemon crate: its rows, not the CLI-only shards.
    (["rust/daemon/src/server.rs"], {"h5-background", "h5-daemon-mutants", "h5-graph-mutants"}),
    (["evals/rust_port/cli_argv.py"], {"judges"}),
    (["evals/rust_baseline/ci.py"], set()),
])
def test_a_pull_request_runs_the_floor_and_what_it_reaches(files, extra):
    suites, _ = select("pull_request", files)
    assert set(suites) == set(FLOOR) | extra
    assert suites == [suite for suite in SUITES if suite in suites]


@pytest.mark.parametrize("path", [
    ".github/workflows/rust.yml", "rust/ci/coverage_plan.py", "rust/ci/parity_scope.py",
    "rust/Cargo.lock", "rust/rust-toolchain.toml", "rust/shared/float_repr.rs",
    "rust/daemon/harness/run.py", "rust/producer-census.json",
    "evals/rust_port/oracle_selection.py", "evals/rust_port/phase1_oracle.py",
    "pseudolife_memory/cli/doctor.py", "plugin/hooks/session-start.sh", "ops/Dockerfile.pg",
    "pyproject.toml", "uv.lock", "examples/hooks.py", "something/new.rs", "Rust/daemon/src/x.rs",
    # Shared CLI harness code: the daemon harness's disposable banks import
    # it and every shard's PostgreSQL prep runs it (reviewer, 2026-10-20).
    "rust/cli_harness/runner.py", "rust/cli_harness/pg_create.py", "rust/cli_harness/lease_ci.py",
    "rust/cli_harness/rows/_bank.py", "rust/cli_harness/rows/lease_transport.py",
    # Test modules the harness imports (rows seed banks from them).
    "tests/test_transfer_cli.py", "tests/pg_defaults.py", "tests/maintainer_authenticator.py",
    "tests/test_coordination_audit.py",
    # evals/rust_port modules the harness imports.
    "evals/rust_port/sent_http.py", "evals/rust_port/processes.py", "evals/rust_port/harness.py",
    # Reached through relative imports, and the packages' __init__ (reviewer,
    # 2026-10-20: oracle_selection imports .oracle_pins).
    "evals/rust_port/oracle_pins.py", "evals/rust_port/__init__.py", "tests/__init__.py",
])
def test_anything_it_cannot_place_runs_every_suite(path):
    assert reached(path) == ALL
    assert select("pull_request", ["tests/test_shim.py", path])[0] == SUITES


def test_the_import_scan_follows_test_helpers_transitively(tmp_path, monkeypatch):
    # rust/ imports tests.a, which imports tests.b; tests.c is unused.
    (tmp_path / "rust").mkdir()
    (tmp_path / "rust/row.py").write_text("from tests.a import seed\n", encoding="utf-8")
    (tmp_path / "evals/rust_port").mkdir(parents=True)
    (tmp_path / "evals/rust_port/judge.py").write_text("from tests import d, e\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/a.py").write_text("import tests.b as helper\n", encoding="utf-8")
    (tmp_path / "tests/b.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests/c.py").write_text("from tests.a import seed\n", encoding="utf-8")
    # Relative imports inside the package, and a list spanning lines.
    (tmp_path / "tests/d.py").write_text("from .f import x\nfrom . import (\n    g,\n    h as other,\n)\n",
                                         encoding="utf-8")
    for name in ("e", "f", "g", "h"):
        (tmp_path / f"tests/{name}.py").write_text("y = 2\n", encoding="utf-8")
    # evals.rust_port: the oracle modules seed the closure; a relative import
    # from one of them is reached though no harness names it.
    (tmp_path / "evals/rust_port/oracle_selection.py").write_text("from .pins import P\n", encoding="utf-8")
    (tmp_path / "evals/rust_port/pins.py").write_text("P = 1\n", encoding="utf-8")
    (tmp_path / "evals/rust_port/unrelated.py").write_text("Q = 1\n", encoding="utf-8")
    monkeypatch.setattr(parity_scope, "ROOT", tmp_path)
    parity_scope.harness_imports.cache_clear()
    try:
        assert parity_scope.harness_imports("tests") == {"a", "b", "d", "e", "f", "g", "h"}
        rust_port = parity_scope.harness_imports("evals.rust_port")
        assert {"oracle_selection", "pins"} <= rust_port and "unrelated" not in rust_port
    finally:
        parity_scope.harness_imports.cache_clear()


def test_the_floor_is_cli_w1a_and_eval():
    # The brief from the 2026-10-20 root-cause report: cli, h5-w1a and eval
    # on every Rust pull request; cli is now three shards.
    assert set(FLOOR) == {"eval", "cli", "cli-daemon", "cli-live", "h5-w1a"}
    assert set(FLOOR) <= set(SUITES)


@pytest.mark.parametrize("files", DOCS_ONLY_CHANGES)
def test_documentation_matches_the_scope_job(files):
    assert all(parity_scope.is_docs(path) for path in files)


@pytest.mark.parametrize("files", CODE_CHANGES)
def test_code_matches_the_scope_job(files):
    assert not all(parity_scope.is_docs(path) for path in files)


def test_is_docs_agrees_with_the_scope_jobs_regex_path_by_path():
    # The same paths through PowerShell's -cnotmatch with the scope job's
    # own rule, so a difference in meaning (not only in text) fails here.
    import os
    import subprocess
    step = next(s for s in raw_workflow()["jobs"]["scope"]["steps"] if s.get("id") == "scope")
    rule = re.search(r"\$docs = '([^']+)'", step["run"]).group(1)
    paths = sorted({path for files in DOCS_ONLY_CHANGES + CODE_CHANGES for path in files} | {
        "rust/README.md", "docs/a/b.md", "docs/x.PY", "llms.txt", "llms-full.txt", "llms2.txt",
        "Docs/x.md", "TESTS/a.md", "a/b/c.md", "docs/sub/run.yaml"})
    script = ("$rule = $env:PL_RULE; foreach ($p in ($env:PL_PATHS -split \"`n\")) "
              "{ if ($p -cnotmatch $rule) { 'code' } else { 'docs' } }")
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
                            env=dict(os.environ, PL_RULE=rule, PL_PATHS="\n".join(paths)),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    verdicts = result.stdout.split()
    assert len(verdicts) == len(paths)
    for path, verdict in zip(paths, verdicts):
        assert parity_scope.is_docs(path) == (verdict == "docs"), path


def test_the_documentation_rule_is_the_scope_jobs():
    step = next(s for s in raw_workflow()["jobs"]["scope"]["steps"] if s.get("id") == "scope")
    rule = re.search(r"\$docs = '([^']+)'", step["run"]).group(1)
    assert rule == ("^(?!(?i:rust|evals|pseudolife_memory|plugin|ops|tests|\\.github)/)"
                    "([^/\"].*\\.md|llms(-full)?\\.txt|docs/.*(?<!\\.(py|ps1|psm1|sh|bash|bat|cmd|js|mjs|cjs|ts|rs"
                    "|toml|yml|yaml)))$"), "the scope job's docs rule changed; update parity_scope.is_docs"


def test_every_row_has_a_suite_and_w1c_rows_match_their_shards():
    from rust.cli_harness import rows
    assert set(ROW_SUITES) == set(rows.ROWS)
    for shard in SHARDS:
        if shard["suite"] in W1C_SUITES:
            assert {row for row, suite in ROW_SUITES.items() if suite == shard["suite"]} == \
                set(re.findall(r"'([a-z_]+)'", shard["w1c_rows"]))
    assert {row for row, suite in ROW_SUITES.items() if suite.startswith("h5-")} == {"audit", "backup"}
    assert set(ROW_SUITES.values()) <= set(SUITES)


SHIM_SOURCE = ROOT / "rust/shim/src"
# Files that declare and dispatch every command; a reference there is not a use.
DISPATCH = {"cli.rs", "main.rs", "lib.rs"}


def entry_of(path):
    """The rust/shim/src/cli entry a shim source file belongs to, or None."""
    parts = path.relative_to(SHIM_SOURCE).parts
    return parts[1] if parts[0] == "cli" and len(parts) > 1 else None


def users(entry):
    """Shim source files outside an entry that import or include it."""
    module = entry.split(".", 1)[0]
    pattern = re.compile(rf"\b{re.escape(module)}::|\buse [^;]*\b{re.escape(module)}\b|"
                         rf"(path|include_str!)\s*[=(]\s*\"[^\"]*\b{re.escape(entry)}\"")
    found = set()
    for path in SHIM_SOURCE.rglob("*.rs"):
        if path.name in DISPATCH and path.parent == SHIM_SOURCE or entry_of(path) == entry:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            found.add(path)
    return found


@pytest.mark.parametrize("entry", sorted(SHIM_COMMANDS))
def test_a_mapped_shim_entry_reaches_only_its_rows(entry):
    # A change to an entry another command imports reaches that command's
    # rows too; the map may name it only if those rows are among its own.
    for path in users(entry):
        owner = entry_of(path)
        assert owner in SHIM_COMMANDS, (entry, str(path.relative_to(ROOT)))
        assert set(SHIM_COMMANDS[owner]) <= set(SHIM_COMMANDS[entry]), (entry, str(path.relative_to(ROOT)))


@pytest.mark.parametrize("entry, user", [("hook_json.rs", "tunnel"), ("lease", "board_audit"),
                                         ("pairing", "pyjson.rs"), ("doorbell_files.rs", "doorbell_seen.rs")])
def test_the_import_scan_sees_the_shared_helpers(entry, user):
    # The near misses the map leaves out: the scan must find their users.
    assert user in {entry_of(path) for path in users(entry)}


def test_every_mapped_shim_entry_exists():
    # A renamed command directory falls back to the whole binary, which is
    # safe; a stale entry here would hide that.
    cli = ROOT / "rust/shim/src/cli"
    for entry, row_names in SHIM_COMMANDS.items():
        assert (cli / entry).exists(), entry
        assert set(row_names) <= set(ROW_SUITES), entry


@pytest.mark.parametrize("files", [
    ["tests/test_shim.py"], ["rust/cli_harness/rows/doctor.py"], ["rust/daemon/src/server.rs"],
    ["rust/shim/src/cli/backup/mod.rs"], ["pseudolife_memory/service.py"],
])
def test_the_matrix_never_names_an_unselected_suite(files):
    # GitHub adds a job for an include entry that matches no combination:
    # it would have no os, and it would run a suite the diff never reached.
    suites, _ = select("pull_request", files)
    values = matrix(suites)
    assert values["suite"] == suites
    assert [shard["suite"] for shard in values["include"]] == [s["suite"] for s in SHARDS if s["suite"] in suites]
    assert {shard["suite"] for shard in SHARDS} <= set(SUITES)
    assert len({shard["suite"] for shard in SHARDS}) == len(SHARDS)


def test_the_output_is_one_line_of_json(tmp_path, monkeypatch, capsys):
    output = tmp_path / "github_output"
    monkeypatch.setattr(parity_scope, "changed_files", lambda: ["rust/cli_harness/rows/doctor.py"])
    assert parity_scope.main(["--event", "pull_request", "--github-output", str(output)]) == 0
    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and lines[0].startswith("parity=")
    values = json.loads(lines[0].removeprefix("parity="))
    assert values["suite"] == ["eval", "cli", "cli-daemon", "cli-live", "w1c-doctor", "h5-w1a"]
    printed = capsys.readouterr().out
    assert "rust/cli_harness/rows/doctor.py -> w1c-doctor" in printed
    assert "running 6 of 14 suites" in printed


@pytest.mark.parametrize("event, files", [("push", ["rust/cli_harness/rows/doctor.py"]),
                                          ("pull_request", None)])
def test_the_output_names_every_suite_when_it_runs_them_all(tmp_path, monkeypatch, event, files):
    output = tmp_path / "github_output"
    monkeypatch.setattr(parity_scope, "changed_files", lambda: files)
    assert parity_scope.main(["--event", event, "--github-output", str(output)]) == 0
    values = json.loads(output.read_text(encoding="utf-8").removeprefix("parity="))
    assert values == {"suite": SUITES, "include": SHARDS}


def test_the_scope_job_publishes_the_matrix_and_both_parity_lanes_use_it():
    jobs = raw_workflow()["jobs"]
    scope = jobs["scope"]
    assert scope["outputs"]["parity"] == "${{ steps.parity.outputs.parity }}"
    step = next(s for s in scope["steps"] if s.get("id") == "parity")
    assert step["env"] == {"EVENT_NAME": "${{ github.event_name }}"}
    assert step["run"].split() == ["python3", "rust/ci/parity_scope.py", "--event", '"$EVENT_NAME"',
                                   "--github-output", '"$GITHUB_OUTPUT"']
    assert "shell" not in step  # bash on ubuntu-latest
    for name in ("parity-checks", "parity-selfhosted"):
        job = jobs[name]
        assert "scope" in job["needs"], name
        values = job["strategy"]["matrix"]
        assert values["suite"] == parity_scope.SUITE_EXPRESSION, name
        assert values["include"] == parity_scope.INCLUDE_EXPRESSION, name
    # The Windows live job is one fixed shard, not chosen per diff.
    assert jobs["parity-live"]["strategy"]["matrix"]["suite"] == ["cli-live"]


def test_resolve_expands_the_full_matrix_once():
    workflow = parity_scope.resolve(raw_workflow())
    hosted = workflow["jobs"]["parity-checks"]["strategy"]["matrix"]
    assert hosted["suite"] == SUITES and hosted["include"] == SHARDS
    hosted["suite"].remove("eval")
    # Idempotent: an already-expanded (or mutated) matrix stays as it is.
    assert parity_scope.resolve(workflow)["jobs"]["parity-checks"]["strategy"]["matrix"]["suite"] == SUITES[1:]
