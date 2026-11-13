"""The early replay selects owners conservatively without loading product code."""
import json

import pytest

import pr_precheck as precheck
from parity_scope import SHIM_COMMANDS, reached


def test_transitive_relative_and_function_local_imports_are_owners():
    sources = {
        "pseudolife_memory/__init__.py": "",
        "pseudolife_memory/doctor_cli.py": "def main():\n from .health import probe\n",
        "pseudolife_memory/health.py": "from .platform import paths\n",
        "pseudolife_memory/platform/__init__.py": "",
        "pseudolife_memory/platform/paths.py": "raise RuntimeError('never import me')\n",
    }
    graph = precheck.import_graph(sources)
    paths = precheck.dependencies(graph, ["pseudolife_memory/doctor_cli.py"])
    assert paths == set(sources)


def test_a_literal_product_module_name_is_not_an_import():
    graph = precheck.import_graph({"pseudolife_memory/a.py": "label = 'pseudolife_memory.b'\n",
                                  "pseudolife_memory/b.py": ""})
    assert precheck.dependencies(graph, ["pseudolife_memory/a.py"]) == {"pseudolife_memory/a.py"}


def test_source_owners_follow_each_rows_transitive_imports(tmp_path):
    package = tmp_path / "pseudolife_memory"
    package.mkdir()
    (package / "doctor_cli.py").write_text("from . import health\n")
    (package / "health.py").write_text("value = 1\n")
    (package / "lease_cli.py").write_text("value = 2\n")
    owners = precheck.python_owners(tmp_path)
    assert owners["pseudolife_memory/doctor_cli.py"] == ("doctor",)
    assert owners["pseudolife_memory/health.py"] == ("doctor",)
    assert owners["pseudolife_memory/lease_cli.py"] == ("lease", "lease_operator")


@pytest.mark.parametrize("path", ["pseudolife_memory/new_helper.py", "rust/shim/src/new_shared.rs",
                                  "rust/shared/src/lib.rs", "rust/ci/pr_precheck.py", "ops/new-runtime.py"])
def test_unknown_runtime_inputs_select_every_row(path):
    assert precheck.rows_for(path, {}) == precheck.ROWS


def test_a_mapped_doctor_source_reaches_its_row():
    assert precheck.rows_for("pseudolife_memory/doctor_cli.py", {"pseudolife_memory/doctor_cli.py": ("doctor",)}) == ("doctor",)
    assert precheck.rows_for("rust/shim/src/cli/doctor/mod.rs", {}) == ("doctor", "console")
    assert precheck.rows_for("rust/cli_harness/goldens/doctor.windows.json", {}) == ("doctor",)


@pytest.mark.parametrize("path", ["README.md", "docs/guide/install.md", "changelog.d/2026-11-05-example.md"])
def test_document_changes_need_no_replay(path):
    assert precheck.rows_for(path, {}) == ()


def test_daemon_implementation_reaches_the_row_that_invokes_its_binary():
    import tomllib
    manifest = tomllib.loads((precheck.ROOT / "rust/shim/Cargo.toml").read_text())
    workspace = tomllib.loads((precheck.ROOT / "rust/Cargo.toml").read_text())["workspace"]["dependencies"]
    resolved = [workspace[name] if isinstance(value, dict) and value.get("workspace") else value
                for name, value in manifest["dependencies"].items()]
    assert not any("path" in value for value in resolved if isinstance(value, dict))
    assert precheck.rows_for("rust/daemon/src/service.rs", {}) == ("migrate_flat",)
    assert precheck.rows_for("rust/daemon/harness/shared.py", {}) == precheck.ROWS


@pytest.mark.parametrize("path", ["rust/ci/ci_budget.json", "rust/ci/ci_budget.py",
                                  "rust/ci/ci_budget_accept/reason.json", "rust/ci/ci_budget_fingerprints.json"])
def test_budget_metadata_changes_do_not_replay_runtime_cases(path):
    assert precheck.rows_for(path, {}) == ()
    assert precheck.judges_for(path, {}, {}) == ()


@pytest.mark.parametrize("path", ["rust/Cargo.toml", "rust/Cargo.lock", "rust/rust-toolchain.toml",
                                  ".cargo/config.toml", "rust/.cargo/config.toml", "tests/__init__.py"])
def test_shared_build_inputs_and_test_package_initializer_keep_all_judges(path):
    selected = precheck.select([path])
    assert selected["linux"] == list(precheck.ROWS)
    assert selected["judges"] == list(precheck.JUDGES)


@pytest.mark.parametrize("path", [*[f"rust/shim/src/cli/{entry}" for entry in SHIM_COMMANDS],
    "rust/shim/src/cli/doctor/mod.rs", "rust/shim/src/cli/lease/mod.rs", "rust/shim/src/cli/wait_mail.rs",
    "rust/shim/src/shared.rs", "rust/shim/README.md"])
def test_shim_precheck_judges_match_batch_ownership(path):
    expected = list(precheck.JUDGES) if "judges" in reached(path) else []
    assert precheck.select([path])["judges"] == expected


def test_report_artifact_upload_is_not_a_pr_gate():
    import yaml
    jobs = yaml.safe_load((precheck.ROOT / ".github/workflows/rust.yml").read_text())["jobs"]
    upload = next(step for step in jobs["rust"]["steps"] if step.get("name") == "Retain ordinary PR parity precheck case IDs")
    assert upload["continue-on-error"] is True
    probes = next(step for step in jobs["frozen-probes"]["steps"] if step.get("name") == "Retain frozen probe case IDs")
    assert probes["continue-on-error"] is True
    # upload-artifact@v4 refuses a second artifact of the same name in a run.
    assert probes["with"]["name"] != upload["with"]["name"]


def goldens(root, *, windows=None):
    directory = root / "rust/cli_harness/goldens"
    directory.mkdir(parents=True)
    for platform, cases in (("linux", {"case": {"exit": 0, "stdout": "eAo=", "window": [1, 2], "utc_offset": 0}}),
                            ("windows", windows or {"case": {"exit": 0, "stdout": "eAo=", "window": [10, 20], "utc_offset": 39600}})):
        (directory / f"doctor.{platform}.json").write_text(json.dumps({"platform": platform, "cases": cases}))


def test_recording_clocks_alone_do_not_require_windows(tmp_path):
    goldens(tmp_path)
    assert not precheck.windows_differs(tmp_path, "doctor")


@pytest.mark.parametrize("cases", [{"case": {"exit": 0, "stdout": "eA0K"}},
                                   {"windows-only": {"exit": 0, "stdout": "eAo="}},
                                   {"case": {"exit": 0, "stdout": "eAo=", "modes": {"file": "acl"}}}])
def test_observed_stream_case_and_mode_differences_retain_windows(tmp_path, cases):
    goldens(tmp_path, windows=cases)
    assert precheck.windows_differs(tmp_path, "doctor")


def test_missing_or_malformed_goldens_cannot_establish_equivalence(tmp_path):
    assert precheck.windows_differs(tmp_path, "doctor")
    goldens(tmp_path)
    (tmp_path / "rust/cli_harness/goldens/doctor.windows.json").write_text('{"cases": []}')
    assert precheck.windows_differs(tmp_path, "doctor")


def test_missing_diff_selects_all_linux_rows_and_different_windows_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(precheck, "python_owners", lambda root: {})
    monkeypatch.setattr(precheck, "windows_differs", lambda root, row: row == "doctor")
    plan = precheck.select(None, tmp_path)
    assert plan["linux"] == list(precheck.ROWS) and plan["windows"] == ["doctor"]
    assert plan["judges"] == list(precheck.JUDGES)


def test_lease_and_shared_cli_changes_select_their_frozen_judges():
    graph = precheck.import_graph({"pseudolife_memory/lease_cli.py": "from . import os_lock\n",
                                  "pseudolife_memory/os_lock.py": "",
                                  "pseudolife_memory/doctor_cli.py": ""})
    owners = {"pseudolife_memory/doctor_cli.py": ("doctor",)}
    assert precheck.judges_for("pseudolife_memory/cli.py", graph, owners) == precheck.JUDGES
    assert precheck.judges_for("pseudolife_memory/os_lock.py", graph, owners) == ("lease-headers", "cli-process")
    assert precheck.judges_for("pseudolife_memory/doctor_cli.py", graph, owners) == ()
    assert precheck.judges_for("rust/shim/src/cli/lease.rs", graph, owners) == precheck.JUDGES
    assert precheck.judges_for("rust/shim/src/main.rs", graph, owners) == precheck.JUDGES


@pytest.mark.parametrize("row,full", [("doctor", False), ("transfer", True), ("test_login", True),
                                    ("lease", True), ("lease_operator", True), ("maintainer", True), ("invite", True)])
def test_scope_outputs_distinguish_light_and_seeder_runtimes(tmp_path, monkeypatch, row, full):
    value = {"schema": 1, "linux": [row], "windows": [row], "judges": []}
    monkeypatch.setattr(precheck, "select", lambda files: value)
    monkeypatch.setattr(precheck, "changed_files", lambda: [])
    output = tmp_path / "output"
    assert precheck.main(["--github-output", str(output)]) == 0
    fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert fields["enabled"] == "true" and fields["full_runtime"] == str(full).lower()


def test_prechecks_are_ordinary_pr_only_and_keep_python_dependencies_isolated():
    import yaml
    jobs = yaml.safe_load((precheck.ROOT / ".github/workflows/rust.yml").read_text())["jobs"]
    steps = {step.get("name"): step for step in jobs["rust"]["steps"]}
    selected = steps["Select ordinary PR parity prechecks"]
    assert selected["if"] == "github.event_name == 'pull_request' && needs.scope.outputs.full == 'false'"
    assert jobs["rust"]["timeout-minutes"] == "${{ github.event_name == 'pull_request' && needs.scope.outputs.full == 'false' && 100 || 75 }}"
    runtime = steps["Install the selected PR precheck dependencies"]
    assert "venv --system-site-packages" in runtime["run"] and "GITHUB_ENV" not in runtime["run"]
    replay = steps["Replay touched PR goldens"]
    assert replay["env"]["PRECHECK_PYTHON"] == "${{ steps.precheck_runtime.outputs.python }}"
    assert runtime["if"] == "steps.precheck.outputs.enabled == 'true'"
    assert replay["if"] == "steps.precheck.outputs.golden == 'true'"
    assert not replay.get("continue-on-error", False)
    assert "--only goldens" in replay["run"]
    assert "--publish --only goldens" in steps["Summarize PR golden replay"]["run"]
    ordered = [step.get("name") for step in jobs["rust"]["steps"]]
    assert ordered.index(replay["name"]) < ordered.index("Nextest")
    # The report-only frozen probes run in a job of their own (issue #1519):
    # a lost probe runner took Rust / ubuntu-latest with it twice. No Rust
    # step runs them, and no job, the Parity gates included, needs theirs.
    assert not any("--only judges" in step.get("run", "") for step in jobs["rust"]["steps"])
    assert not any("precheck" in name for name in jobs)
    probe_job = jobs["frozen-probes"]
    assert probe_job["continue-on-error"] is True
    assert all("frozen-probes" not in (job.get("needs") or []) for job in jobs.values())
    assert probe_job["if"] == ("${{ !cancelled() && github.event_name == 'pull_request' && "
                               "needs.scope.outputs.full == 'false' && needs.scope.outputs.rust != 'false' }}")
    probe_steps = {step.get("name"): step for step in probe_job["steps"]}
    probes = probe_steps["Probe PR frozen judges for item-11 diagnostics"]
    assert probes["continue-on-error"] is True and probes["timeout-minutes"] == 20
    assert "!cancelled()" in probes["if"] and "precheck_runtime.outcome == 'success'" in probes["if"]
    assert "precheck_candidate.outcome == 'success'" in probes["if"] and "--only judges" in probes["run"]
    assert probe_steps["Select ordinary PR parity prechecks"]["run"] == steps["Select ordinary PR parity prechecks"]["run"]
    assert "--publish --only judges" in probe_steps["Summarize report-only frozen probes"]["run"]
    assert "venv --system-site-packages" in probe_steps["Install the selected PR precheck dependencies"]["run"]
    assert "GITHUB_ENV" not in probe_steps["Install the selected PR precheck dependencies"]["run"]


def test_precheck_data_tests_join_the_native_windows_lite_selection():
    import yaml
    jobs = yaml.safe_load((precheck.ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    scripts = "\n".join(step.get("run", "") for step in jobs["test-lite-windows"]["steps"])
    assert "rust/ci/test_pr_precheck.py" in scripts and "rust/ci/test_pr_precheck_run.py" in scripts


def test_probe_only_selection_does_not_require_golden_setup(tmp_path, monkeypatch):
    value = {"schema": 1, "linux": [], "windows": [], "judges": ["cli-argv"]}
    monkeypatch.setattr(precheck, "select", lambda files: value)
    monkeypatch.setattr(precheck, "changed_files", lambda: [])
    output = tmp_path / "output"
    assert precheck.main(["--github-output", str(output)]) == 0
    fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert fields["enabled"] == "true" and fields["golden"] == "false" and fields["judges"] == "true"
