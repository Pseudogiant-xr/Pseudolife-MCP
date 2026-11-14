"""The parity matrix a run gets: every suite, or on a pull request the reached ones."""

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import parity_scope
from parity_scope import ALL, FLOOR, ROW_SUITES, SHARDS, SHIM_COMMANDS, SUITES, matrix, reached, select
from test_workflow import CODE_CHANGES, DOCS_ONLY_CHANGES, W1C_SUITES

ROOT = Path(__file__).resolve().parents[2]
# The pairs the matrix always leaves out: none since the Python freeze.
STATIC = [dict(pair) for pair in parity_scope.STATIC_EXCLUDE]


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
    (["rust/daemon/divergences/coordination-board-completion-w2/w2f-pairing-http.md",
      "rust/daemon/harness/run_scenarios/README.md"], set()),
    (["rust/parity/complete-console-registration/get-api-trace.md",
      "rust/parity/parity-rows/_section.md"], set()),
    # The producer census is data no harness reads; `ops/integrate.py build`
    # commits it to every batch whose members change what it scans.
    (["rust/producer-census.json"], set()),
    # A row, its golden or its spec reaches the shard that runs it.
    (["rust/cli_harness/rows/doctor.py"], {"w1c-doctor"}),
    (["rust/cli_harness/goldens/test_login.linux.json"], {"w1c-connection"}),
    (["rust/cli_harness/rows/invite.md", "rust/cli_harness/rows/audit.py"], {"w1c-pairing"}),
    # Rows no shard replays since the freeze reach the floor only.
    (["rust/cli_harness/rows/backup.py", "rust/cli_harness/goldens/migrate_flat.linux.json"], set()),
    (["rust/cli_harness/rows/mail.py", "rust/cli_harness/specs/CLI-LEASE-core.md"], set()),
    # Python tests the harness never imports: the floor's MCP catalogue step.
    (["tests/test_shim.py", "tests/conftest.py"], set()),
    # One command's shim code reaches its rows.
    # Doctor's users include console (its registration search).
    (["rust/shim/src/cli/doctor/mod.rs"], {"w1c-doctor", "w1c-connection"}),
    (["rust/shim/src/cli/update/engine.rs"], {"w1c-doctor", "w1c-connection"}),
    (["rust/shim/src/cli/backup/mod.rs"], {"w1c-doctor", "w1c-connection"}),
    (["rust/shim/src/cli/migrate_flat.rs"], set()),
    (["rust/shim/src/cli/transfer/mod.rs"], {"w1c-pairing"}),
    # Helpers other commands import reach the whole binary (Codex review:
    # tunnel uses hook_json, board_audit uses lease).
    (["rust/shim/src/cli/hook_json.rs"], set(parity_scope.SHIM) - set(FLOOR)),
    (["rust/shim/src/cli/lease/args.rs"], set(parity_scope.SHIM) - set(FLOOR)),
    (["rust/shim/src/cli/pairing/invite.rs"], set(parity_scope.SHIM) - set(FLOOR)),
    (["rust/shim/src/cli/wait_mail.rs"], set(parity_scope.SHIM) - set(FLOOR)),
    (["rust/shim/src/main.rs"], set(parity_scope.SHIM) - set(FLOOR)),
    # The daemon crate: its shards (the floor), not the CLI-only ones.
    (["rust/daemon/src/server.rs"], set()),
    # No shard runs the differential instrument's own tests or its oracle
    # binding since the Python freeze (14 November 2026).
    (["evals/rust_port/cli_argv.py"], set()),
    (["evals/rust_port/oracle_selection.py", "evals/rust_port/phase1_oracle.py"], set()),
    (["evals/rust_baseline/ci.py"], set()),
])
def test_a_pull_request_runs_the_floor_and_what_it_reaches(files, extra):
    suites, _ = select("pull_request", files)
    assert set(suites) == set(FLOOR) | extra
    assert suites == [suite for suite in SUITES if suite in suites]


@pytest.mark.parametrize("path", [
    ".github/workflows/rust.yml", "rust/ci/coverage_plan.py", "rust/ci/parity_scope.py",
    "rust/Cargo.lock", "rust/rust-toolchain.toml", "rust/shared/float_repr.rs",
    "rust/daemon/harness/run.py",
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
    # The packages' __init__.
    "evals/rust_port/__init__.py", "tests/__init__.py",
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
    # evals.rust_port: what the harnesses import seeds the closure, and a
    # relative import from one of those is reached though no harness names
    # it. The oracle modules no longer seed it (no shard binds an oracle).
    (tmp_path / "rust/seeder.py").write_text("from evals.rust_port.fixtures import F\n", encoding="utf-8")
    (tmp_path / "evals/rust_port/fixtures.py").write_text("from .pins import P\n", encoding="utf-8")
    (tmp_path / "evals/rust_port/oracle_selection.py").write_text("from .unrelated import Q\n", encoding="utf-8")
    (tmp_path / "evals/rust_port/pins.py").write_text("P = 1\n", encoding="utf-8")
    (tmp_path / "evals/rust_port/unrelated.py").write_text("Q = 1\n", encoding="utf-8")
    monkeypatch.setattr(parity_scope, "ROOT", tmp_path)
    parity_scope.harness_imports.cache_clear()
    try:
        assert parity_scope.harness_imports("tests") == {"a", "b", "d", "e", "f", "g", "h"}
        rust_port = parity_scope.harness_imports("evals.rust_port")
        assert {"fixtures", "pins"} <= rust_port
        assert not {"oracle_selection", "unrelated"} & rust_port
    finally:
        parity_scope.harness_imports.cache_clear()


def _scan_tree(root):
    (root / "rust").mkdir()
    (root / "rust/.gitignore").write_text("/target/\n", encoding="utf-8")
    (root / "rust/harness.py").write_text("from evals.rust_port import used\n", encoding="utf-8")
    (root / "rust/target/mutant-src/rust").mkdir(parents=True)
    # A leftover mutant-source copy of the tree in the ignored cargo output.
    (root / "rust/target/mutant-src/rust/copy.py").write_text("from evals.rust_port import leaked\n", encoding="utf-8")
    (root / "evals/rust_port").mkdir(parents=True)
    for name in ("used", "leaked", "fresh"):
        (root / f"evals/rust_port/{name}.py").write_text("x = 1\n", encoding="utf-8")


def test_the_import_scan_skips_ignored_build_output(tmp_path, monkeypatch):
    # W5's report: a rust/target/mutant-src/ copy made oracle_selection reach
    # every suite and failed the floor case in a long-lived worktree.
    _scan_tree(tmp_path)
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    # The near miss: a harness file not yet added still counts.
    (tmp_path / "rust/new_harness.py").write_text("from evals.rust_port import fresh\n", encoding="utf-8")
    monkeypatch.setattr(parity_scope, "ROOT", tmp_path)
    found = parity_scope.harness_imports.__wrapped__("evals.rust_port")
    assert {"used", "fresh"} <= found and "leaked" not in found
    assert parity_scope._scanned([]) == []   # never the whole tree for no sources


def test_the_import_scan_skips_cargo_output_without_git(tmp_path, monkeypatch):
    _scan_tree(tmp_path)
    monkeypatch.setattr(parity_scope, "ROOT", tmp_path)
    def no_git(*args, **kwargs):
        raise OSError("no git")

    # Only this module's view of subprocess, not the process-wide one.
    monkeypatch.setattr(parity_scope, "subprocess",
                        SimpleNamespace(run=no_git, SubprocessError=subprocess.SubprocessError))
    found = parity_scope.harness_imports.__wrapped__("evals.rust_port")
    assert "used" in found and "leaked" not in found


def test_ordinary_pr_precheck_imports_do_not_widen_batch_routing(tmp_path, monkeypatch):
    directory = tmp_path / "rust/ci"
    directory.mkdir(parents=True)
    (directory / "pr_precheck_run.py").write_text("from evals.rust_port import early_only\nfrom tests import early_seeder\n")
    (directory / "test_pr_precheck_run.py").write_text("from evals.rust_port import early_test_only\n")
    (directory / "batch_harness.py").write_text("from evals.rust_port import batch_helper\nfrom tests import batch_seeder\n")
    monkeypatch.setattr(parity_scope, "ROOT", tmp_path)
    imports = parity_scope.harness_imports.__wrapped__
    assert "batch_helper" in imports("evals.rust_port")
    assert not {"early_only", "early_test_only"} & imports("evals.rust_port")
    assert "batch_seeder" in imports("tests") and "early_seeder" not in imports("tests")


def test_the_floor_is_cli_and_eval():
    # The brief from the 2026-10-20 root-cause report: cli, h5-w1a and eval
    # on every Rust pull request; cli became five shards (2026-10-24), and
    # cli-live and h5-w1a left with the Python freeze (14 November 2026).
    assert set(FLOOR) == {"eval", "cli", "cli-store", "cli-startup"}
    assert set(FLOOR) <= set(SUITES)


def test_no_suite_left_that_held_only_live_comparisons():
    assert not {"cli-live", "judges", "h5-audit", "h5-backup", "h5-w1a", "h5-background",
                "h5-daemon-mutants", "h5-graph-mutants", "h5-light"} & set(SUITES)
    assert parity_scope.STATIC_EXCLUDE == () and parity_scope.ORACLE_MODULES == ()


def test_startup_parts_share_one_physical_suite_on_each_os():
    assert "cli-startup" in SUITES
    assert not {"cli-daemon", "cli-daemon-2"} & set(SUITES)
    values = parity_scope.pruned(SUITES, [], "workflow_dispatch")
    pairs = {(os_name, suite) for os_name in parity_scope.SYSTEMS for suite in values["suite"]
             if not any(rule.get("os") == os_name and rule.get("suite") == suite for rule in values["exclude"])}
    assert len(pairs) == 16
    assert {(os_name, "cli-startup") for os_name in parity_scope.SYSTEMS} <= pairs


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


@pytest.mark.parametrize("files, rust", [
    (["docs/guide/x.md"], "false"),
    # ops/integrate.py build regenerates the census whenever a batch changes
    # a scanned doc; that alone must not cost a docs-only batch the skip.
    (["docs/guide/x.md", "rust/producer-census.json"], "false"),
    # Near misses: the census alone, or beside code, still runs Rust.
    (["rust/producer-census.json"], "true"),
    (["docs/guide/x.md", "rust/producer-census.json", "rust/shim/src/main.rs"], "true"),
    (["docs/guide/x.md", "rust/contract-inventory.json"], "true"),
])
def test_the_scope_job_skips_rust_for_docs_and_their_regenerated_census(tmp_path, files, rust):
    # The scope step itself, run by pwsh on a pull request whose head commit
    # changes `files`.
    import os
    import subprocess
    step = next(s for s in raw_workflow()["jobs"]["scope"]["steps"] if s.get("id") == "scope")
    def git(*args):
        subprocess.run(["git", "-c", "user.name=Scope fixture", "-c", "user.email=scope@example.com",
                        *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    for path in files:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("changed\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "head")
    output = tmp_path / "github-output.txt"
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", step["run"]],
                            cwd=tmp_path, capture_output=True, text=True,
                            env=dict(os.environ, EVENT_NAME="pull_request",
                                     HEAD_REF="integrate/2026-10-27-batch-1",
                                     GITHUB_OUTPUT=str(output)))
    assert result.returncode == 0, result.stderr
    assert f"rust={rust}" in output.read_text(encoding="utf-8-sig").split()


def test_the_documentation_rule_is_the_scope_jobs():
    step = next(s for s in raw_workflow()["jobs"]["scope"]["steps"] if s.get("id") == "scope")
    rule = re.search(r"\$docs = '([^']+)'", step["run"]).group(1)
    assert rule == ("^(rust/(CUTOVER|DEV|SEMANTICS-CHECKLIST)\\.md"
                    "|(?!(?i:rust|evals|pseudolife_memory|plugin|ops|tests|\\.github)/)"
                    "([^/\"].*\\.md|llms(-full)?\\.txt|docs/.*(?<!\\.(py|ps1|psm1|sh|bash|bat|cmd|js|mjs|cjs|ts|rs"
                    "|toml|yml|yaml))))$"), "the scope job's docs rule changed; update parity_scope.is_docs"


def test_the_plan_pages_counted_as_docs_are_read_by_no_code():
    # The scope job skips every Rust and parity job for a pull request that
    # changes only these pages, so no tracked file outside tests/ (the Python
    # suite, which a batch runs locally) may read them: code may name one
    # only on a comment line, and data files not at all. Documents and the
    # classifier's own files, which list the pages, are exempt.
    classifier = {"rust/ci/parity_scope.py", "rust/ci/test_parity_scope.py", "rust/ci/test_workflow.py"}
    import subprocess
    root = Path(__file__).resolve().parents[2]
    assert parity_scope.RUST_PLAN_DOCS == {"rust/CUTOVER.md", "rust/DEV.md", "rust/SEMANTICS-CHECKLIST.md"}
    for doc in parity_scope.RUST_PLAN_DOCS:
        assert (root / doc).is_file(), doc
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                             capture_output=True).stdout.decode("utf-8").split("\0")
    names = [doc.split("/", 1)[1] for doc in sorted(parity_scope.RUST_PLAN_DOCS)]
    readers = []
    for path in tracked:
        if (not path or path.endswith(".md") or parity_scope.is_docs(path) or path.startswith("tests/")
                or path in classifier):
            continue
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            # A comment line may name a page; a Rust attribute (#[doc = ...],
            # #![doc = include_str!(...)]) reads it. A text match cannot see a
            # glob or a built-up name, so review still matters there.
            text_line = line.lstrip()
            if text_line.startswith(("#", "//", "*", "<!--")) and not text_line.startswith(("#[", "#![")):
                continue
            readers += [f"{path}:{number}: {name}" for name in names if name in line]
    assert not readers, "code reads a plan page the scope job treats as docs: " + "; ".join(readers)


def test_every_row_has_a_suite_and_w1c_rows_match_their_shards():
    from rust.cli_harness import rows
    for shard in SHARDS:
        if shard["suite"] in W1C_SUITES:
            assert {row for row, suite in ROW_SUITES.items() if suite == shard["suite"]} == \
                set(re.findall(r"'([a-z_]+)'", shard["w1c_rows"]))
    assert set(ROW_SUITES) | set(parity_scope.UNREPLAYED_ROWS) == set(rows.ROWS)
    assert not set(ROW_SUITES) & set(parity_scope.UNREPLAYED_ROWS)
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


@pytest.mark.parametrize("path, doctor", [
    ("update/clients/codex.rs", True),
    ("update/engine.rs", True),
    ("backup/mod.rs", True),
    ("backup_help.txt", True),
    ("maintainer/mod.rs", True),
    ("doctor/mod.rs", True),
    # connect's stdin check is read by restore_cli, whose docker_backup is
    # backup's Docker tier: a connect change reaches update and doctor too.
    ("connect/mod.rs", True),
    # The near miss: a command no doctor dependency imports.
    ("transfer.rs", False),
])
def test_runtime_hook_trust_dependencies_select_doctor(path, doctor):
    suites, _ = select("pull_request", ["rust/shim/src/cli/" + path])
    assert ("w1c-doctor" in suites) is doctor


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
        assert set(row_names) <= set(ROW_SUITES) | set(parity_scope.UNREPLAYED_ROWS), entry


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
    assert len(lines) == 3 and lines[0].startswith("parity=") and lines[1].startswith("selfhosted=")
    assert lines[2] == "parity_any=true"
    values = json.loads(lines[0].removeprefix("parity="))
    assert values["suite"] == ["eval", "cli", "cli-store", "cli-startup", "w1c-doctor"]
    printed = capsys.readouterr().out
    assert "rust/cli_harness/rows/doctor.py -> w1c-doctor" in printed
    assert "running 5 of 8 suites" in printed


@pytest.mark.parametrize("event, files", [("push", ["rust/cli_harness/rows/doctor.py"]),
                                          ("pull_request", None)])
def test_the_output_names_every_suite_when_it_runs_them_all(tmp_path, monkeypatch, event, files):
    output = tmp_path / "github_output"
    monkeypatch.setattr(parity_scope, "changed_files", lambda: files)
    assert parity_scope.main(["--event", event, "--github-output", str(output)]) == 0
    values = json.loads(output.read_text(encoding="utf-8").splitlines()[0].removeprefix("parity="))
    assert values == {"suite": SUITES, "include": SHARDS, "exclude": STATIC}


def test_the_scope_job_publishes_the_matrix_and_both_parity_lanes_use_it():
    jobs = raw_workflow()["jobs"]
    scope = jobs["scope"]
    assert scope["outputs"]["parity"] == "${{ steps.parity.outputs.parity }}"
    step = next(s for s in scope["steps"] if s.get("id") == "parity")
    assert step["env"] == {"EVENT_NAME": "${{ github.event_name }}",
                           "BATCH_PASSED": "${{ steps.batch.outputs.passed }}"}
    assert step["run"].split() == ["python3", "rust/ci/parity_scope.py", "--event", '"$EVENT_NAME"',
                                   "--batch-passed", '"$BATCH_PASSED"',
                                   "--github-output", '"$GITHUB_OUTPUT"']
    assert "shell" not in step  # bash on ubuntu-latest
    batch = next(s for s in scope["steps"] if s.get("id") == "batch")
    assert scope["steps"].index(batch) < scope["steps"].index(step)
    assert scope["outputs"]["parity_selfhosted"] == "${{ steps.parity.outputs.selfhosted }}"
    assert scope["outputs"]["parity_any"] == "${{ steps.parity.outputs.parity_any }}"
    assert "needs.scope.outputs.parity_any != 'false'" in jobs["parity-checks"]["if"]
    for name, suite, include in (
            ("parity-checks", parity_scope.SUITE_EXPRESSION, parity_scope.INCLUDE_EXPRESSION),
            ("parity-selfhosted", parity_scope.SELFHOSTED_SUITE_EXPRESSION,
             parity_scope.SELFHOSTED_INCLUDE_EXPRESSION)):
        job = jobs[name]
        assert "scope" in job["needs"], name
        values = job["strategy"]["matrix"]
        assert values["suite"] == suite, name
        assert values["include"] == include, name
    # Only the hosted matrix has an os axis to exclude pairs from.
    assert jobs["parity-checks"]["strategy"]["matrix"]["exclude"] == parity_scope.EXCLUDE_EXPRESSION
    assert "exclude" not in jobs["parity-selfhosted"]["strategy"]["matrix"]
    # The Windows live job left with the Python freeze (14 November 2026).
    assert "parity-live" not in jobs


@pytest.mark.parametrize("event, files", [("schedule", None), ("workflow_dispatch", None),
                                          ("pull_request", ["rust/cli_harness/rows/audit.py"])])
def test_every_selfhosted_job_has_a_runner(tmp_path, monkeypatch, event, files):
    # An include entry matching no runner x suite combination becomes a job
    # with no runner, routed to an unserved label and queued for a day (Opus
    # review of 53fd499d: h5-light, which the hosted matrix ran on Linux only).
    monkeypatch.setattr(parity_scope, "changed_files", lambda: files)
    output = tmp_path / "github_output"
    assert parity_scope.main(["--event", event, "--github-output", str(output)]) == 0
    lane = json.loads(output.read_text(encoding="utf-8").splitlines()[1].removeprefix("selfhosted="))
    combos = [{"runner": runner, "suite": suite} for runner in ("linux", "windows") for suite in lane["suite"]]
    for entry in lane["include"]:
        matched = [combo for combo in combos if combo["suite"] == entry["suite"]]
        assert matched, entry
    assert not set(lane["suite"]) & set(parity_scope.HOSTED_ONLY)
    # Every selected suite but the hosted-only composites, whose parts the
    # composite's selection always carries (select, LINUX_LIGHT).
    from coverage_plan import COMPOSITES
    hosted = json.loads(output.read_text(encoding="utf-8").splitlines()[0].removeprefix("parity="))
    assert lane["suite"] == [suite for suite in hosted["suite"] if suite not in parity_scope.HOSTED_ONLY]
    for name in parity_scope.HOSTED_ONLY:
        if name in hosted["suite"]:
            assert set(COMPOSITES[name]) & set(lane["suite"])


def test_resolve_expands_the_full_matrix_once():
    workflow = parity_scope.resolve(raw_workflow())
    hosted = workflow["jobs"]["parity-checks"]["strategy"]["matrix"]
    assert hosted["suite"] == SUITES and hosted["include"] == SHARDS
    assert hosted["exclude"] == STATIC
    hosted["suite"].remove("eval")
    # Idempotent: an already-expanded (or mutated) matrix stays as it is.
    assert parity_scope.resolve(workflow)["jobs"]["parity-checks"]["strategy"]["matrix"]["suite"] == SUITES[1:]


# A master push whose merged batch passed some (os, suite) pairs on the same
# tree (ops/ci_batch_passed.py) leaves them out of its matrix. Since the
# Python freeze no suite compares against an event-selected oracle, so any
# pair may leave, and a push whose batch passed them all runs none.

def checks(*pairs):
    return [f"Parity checks / {os_name} / {suite}" for os_name, suite in pairs]


def expand(values, systems=("ubuntu-latest", "windows-latest")):
    """GitHub's matrix expansion: os x suite, minus excludes, then includes
    (an include matching no remaining combination adds a job of its own)."""
    combos = [{"os": os_name, "suite": suite} for os_name in systems for suite in values["suite"]]
    combos = [combo for combo in combos
              if not any(all(combo.get(k) == v for k, v in rule.items()) for rule in values["exclude"])]
    for entry in values["include"]:
        matched = [combo for combo in combos
                   if all(combo.get(k, v) == v for k, v in entry.items() if k in ("os", "suite"))]
        if matched:
            for combo in matched:
                combo.update(entry)
        else:
            combos.append(dict(entry))
    return combos


def pairs_of(combos):
    return {(combo.get("os"), combo["suite"]) for combo in combos}


FULL_PAIRS = {(os_name, suite) for os_name in ("ubuntu-latest", "windows-latest") for suite in SUITES} \
    - {(pair["os"], pair["suite"]) for pair in STATIC}


def test_a_push_with_nothing_passed_runs_the_full_matrix():
    values = parity_scope.pruned(SUITES, [], "push")
    assert values == {"suite": SUITES, "include": SHARDS, "exclude": STATIC}
    assert pairs_of(expand(values)) == FULL_PAIRS


def test_a_push_leaves_out_exactly_the_pairs_its_batch_passed():
    passed = checks(("windows-latest", "w1c-pairing"), ("ubuntu-latest", "w1c-doctor"),
                    ("windows-latest", "w1c-doctor"))
    values = parity_scope.pruned(SUITES, passed, "push")
    combos = expand(values)
    assert pairs_of(combos) == FULL_PAIRS - {("windows-latest", "w1c-pairing"), ("ubuntu-latest", "w1c-doctor"),
                                             ("windows-latest", "w1c-doctor")}
    # A suite passed on both systems leaves the suite list and its include
    # entry, or GitHub would add an os-less job for the entry.
    assert "w1c-doctor" not in values["suite"]
    assert "w1c-doctor" not in {entry["suite"] for entry in values["include"]}
    # A suite passed on one system keeps its include for the other.
    assert {"os": "windows-latest", "suite": "w1c-pairing"} in values["exclude"]
    ubuntu_pairing = next(c for c in combos if c["suite"] == "w1c-pairing")
    assert ubuntu_pairing["os"] == "ubuntu-latest" and ubuntu_pairing["job_timeout"] == 40
    assert all("os" in combo for combo in combos)


def test_a_push_whose_batch_passed_every_pair_runs_none(tmp_path):
    everything = checks(*FULL_PAIRS)
    values = parity_scope.pruned(SUITES, everything, "push")
    assert values["suite"] == [] and values["include"] == []
    # GitHub refuses a matrix with an empty suite list, so the scope job says
    # so and the workflow skips parity-checks (the Parity gate accepts that).
    output = tmp_path / "github_output"
    assert parity_scope.main(["--event", "push", "--batch-passed", json.dumps(everything),
                              "--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8").splitlines()[2] == "parity_any=false"
    assert not hasattr(parity_scope, "EVENT_BOUND")


@pytest.mark.parametrize("name", [
    # Near misses: a similar name never stands for another pair.
    "Parity checks / ubuntu-latest / cli-live", "Parity checks / ubuntu-latest / cli-daemon",
    "Parity checks / ubuntu-latest / cli ", "parity checks / ubuntu-latest / cli",
    "Parity checks / ubuntu-latest", "Parity checks / ubuntu-latest / cli / extra",
    "Parity live (non-gating) / ubuntu-latest / cli", "Parity self-hosted / ubuntu-latest / cli",
    "Parity / ubuntu-latest", "Rust / ubuntu-latest", "Parity checks / macos-latest / cli",
    "Parity checks / ubuntu-latest / not-a-suite",
    "Parity checks / ${{ matrix.os }} / ${{ matrix.suite }}",
])
def test_only_the_exact_job_name_leaves_out_ubuntu_cli(name):
    values = parity_scope.pruned(SUITES, [name], "push")
    assert ("ubuntu-latest", "cli") in pairs_of(expand(values))


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "schedule", "workflow_dispatch"])
def test_only_a_push_reads_the_batch(event):
    passed = checks(("ubuntu-latest", "cli"), ("windows-latest", "cli"))
    assert parity_scope.pruned(SUITES, passed, event) == matrix(SUITES)


def test_a_pull_request_selection_is_never_pruned():
    suites, _ = select("pull_request", ["rust/cli_harness/rows/doctor.py"])
    assert parity_scope.pruned(suites, checks(("ubuntu-latest", "cli")), "pull_request") == matrix(suites)


def run_main(tmp_path, *arguments):
    output = tmp_path / "github_output"
    assert parity_scope.main([*arguments, "--github-output", str(output)]) == 0
    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and lines[0].startswith("parity=") and lines[1].startswith("selfhosted=")
    values = json.loads(lines[0].removeprefix("parity="))
    assert json.loads(lines[1].removeprefix("selfhosted=")) == parity_scope.selfhosted(values)
    assert lines[2] == f"parity_any={str(bool(values['suite'])).lower()}"
    return values


def test_main_takes_the_batch_list_on_a_push(tmp_path, capsys):
    passed = json.dumps(checks(("ubuntu-latest", "eval"), ("windows-latest", "eval"),
                               ("windows-latest", "cli")))
    values = run_main(tmp_path, "--event", "push", "--batch-passed", passed)
    assert "eval" not in values["suite"]
    assert {"os": "windows-latest", "suite": "cli"} in values["exclude"]
    printed = capsys.readouterr().out
    assert ("parity: the merged batch passed these on the same tree, so they are left out: "
            "ubuntu-latest/eval, windows-latest/cli, windows-latest/eval") in printed


@pytest.mark.parametrize("raw", ["", "[]", "not json", '{"a": 1}', "[1, 2]", '"text"'])
def test_main_runs_the_full_matrix_on_an_empty_or_malformed_list(tmp_path, raw):
    assert run_main(tmp_path, "--event", "push", "--batch-passed", raw) == matrix(SUITES)


def test_main_without_the_batch_list_is_unchanged(tmp_path):
    assert run_main(tmp_path, "--event", "push") == matrix(SUITES)
