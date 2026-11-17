"""Batch-head workflow selection uses data, never candidate imports or commands."""
import json
from pathlib import Path

import pytest
import yaml

from ops import harness_selection as selection


@pytest.fixture
def tree():
    return {
        selection.WORKFLOW: yaml.safe_dump({"jobs": {
            "rust": {"steps": [{"run": "python -m pytest -q rust/test_base.py"}]},
            "parity-checks": {"steps": [{"run": "python -m pytest -q rust/test_extra.py rust/test_native.py"}]},
        }}),
        "ops/integrate.py": 'RUST_HARNESS = ("rust/test_base.py", "rust/ci")\n',
        selection.CLASSIFICATION: json.dumps({"schema": 1, "files": {
            "rust/test_extra.py": {"kind": "unit", "reason": "mocked calls"},
            "rust/test_native.py": {"kind": "native", "reason": "built candidate"},
        }}),
        "rust/test_base.py": "def test_base(): pass\n",
        "rust/test_extra.py": "def test_extra(): pass\n",
        "rust/test_native.py": "def test_native(): pass\n",
        "rust/ci/test_guard.py": "def test_guard(): pass\n",
    }


def recipe(tree):
    return selection.recipe(tree.__getitem__, tuple(tree))


def workflow(tree, job, command):
    value = yaml.safe_load(tree[selection.WORKFLOW])
    value["jobs"][job]["steps"].append({"run": command})
    tree[selection.WORKFLOW] = yaml.safe_dump(value)


def test_recipe_adds_candidate_free_parity_but_keeps_native_cases_in_their_lane(tree):
    result = recipe(tree)
    assert result.commands == (selection.Command("pytest", (
        "rust/ci", "rust/test_base.py", "rust/test_extra.py")),)
    assert result.record()[0]["paths"] == list(result.commands[0].paths)
    assert "--require-databases" in result.display()


def test_a_batch_head_with_an_unlisted_rust_job_path_refuses(tree):
    workflow(tree, "rust", "python -m pytest -q rust/test_new.py")
    with pytest.raises(selection.SelectionError, match="RUST_HARNESS omits.*rust/test_new.py"):
        recipe(tree)


def test_reading_candidate_source_does_not_execute_it(tree):
    tree["ops/integrate.py"] += "raise AssertionError('candidate executed')\n"
    assert recipe(tree).commands[0].paths


@pytest.mark.parametrize("source", ["jobs: [", "{}", "jobs: {rust: {steps: null}}"])
def test_unreadable_workflow_refuses(tree, source):
    tree[selection.WORKFLOW] = source
    with pytest.raises(selection.SelectionError):
        recipe(tree)


@pytest.mark.parametrize("command", [
    "python -m pytest -q rust/test_unknown.py",
    "python rust/new_harness.py --unit",
    "python -m unknown_unit_runner rust/test_extra.py",
    "uv run python rust/test_extra.py",
    "& python rust/test_extra.py",
])
def test_unknown_parity_commands_or_classifications_refuse(tree, command):
    workflow(tree, "parity-checks", command)
    with pytest.raises(selection.SelectionError):
        recipe(tree)


@pytest.mark.parametrize("job,command", [
    ("parity-checks", "& $python rust/test_extra.py"),
    ("parity-checks", "& $python -m unittest rust/test_extra.py"),
    ("parity-checks", "py rust/test_extra.py"),
    ("parity-checks", "py -m unittest rust/test_extra.py"),
    ("rust", "python rust/test_extra.py"),
    ("rust", "python -m unittest rust/test_extra.py"),
    ("new-job", "python -m pytest rust/test_extra.py"),
    ("new-job", "python rust/test_extra.py"),
    ("new-job", "& $python rust/test_extra.py"),
    ("new-job", "python -m unittest discover"),
])
def test_every_job_refuses_an_unclassified_test_line_with_its_location(tree, job, command):
    value = yaml.safe_load(tree[selection.WORKFLOW])
    value["jobs"].setdefault(job, {"steps": []})["steps"].append(
        {"name": "Unclassified test command", "run": "# setup\n" + command})
    tree[selection.WORKFLOW] = yaml.safe_dump(value)
    with pytest.raises(selection.SelectionError) as error:
        recipe(tree)
    message = str(error.value)
    assert job in message and "Unclassified test command" in message and "line 2" in message


def test_an_action_cannot_hide_a_rust_test_command(tree):
    value = yaml.safe_load(tree[selection.WORKFLOW])
    value["jobs"]["new-job"] = {"steps": [{"name": "Delegated test", "uses": "./.github/actions/tests",
        "with": {"script": "python -m pytest rust/test_extra.py"}}]}
    tree[selection.WORKFLOW] = yaml.safe_dump(value)
    with pytest.raises(selection.SelectionError) as error:
        recipe(tree)
    assert all(word in str(error.value) for word in ("new-job", "Delegated test", "line 1", "uses"))


@pytest.mark.parametrize("command", [
    "python -m pip install pytest;python -m pytest rust/test_hidden.py",
    "python -m pip install pytest&&python -m pytest rust/test_hidden.py",
    "python -m pip install pytest||python -m pytest rust/test_hidden.py",
    "python -m pip install pytest|python -m pytest rust/test_hidden.py",
    "python -m pip install pytest&python -m pytest rust/test_hidden.py",
    "python rust/daemon/harness/run.py golden;python rust/test_hidden.py",
    "python rust/ci/pr_precheck.py --github-output out;python rust/test_hidden.py",
    "cd rust;python daemon/harness/test_hidden.py",
    "cd rust;& $python daemon/harness/test_hidden.py",
])
def test_glued_shell_operators_cannot_hide_another_command(tree, command):
    job = "rust" if "pr_precheck" in command else "parity-checks"
    workflow(tree, job, command)
    with pytest.raises(selection.SelectionError, match="compound"):
        recipe(tree)


@pytest.mark.parametrize("continuation", ["\\", "`"])
def test_comment_continuation_does_not_swallow_the_next_command(tree, continuation):
    workflow(tree, "rust", "# ignored " + continuation + "\npython -m pytest rust/test_hidden.py")
    with pytest.raises(selection.SelectionError, match="omits.*rust/test_hidden.py"):
        recipe(tree)


@pytest.mark.parametrize("job", ["rust", "candidate", "candidate-harness"])
@pytest.mark.parametrize("command", ["python daemon/harness/test_new.py", "python -m pytest daemon/harness/test_new.py"])
def test_job_working_directory_cannot_hide_a_relative_script(tree, job, command):
    value = yaml.safe_load(tree[selection.WORKFLOW])
    value["jobs"].setdefault(job, {"steps": []}).update({"defaults": {"run": {"working-directory": "rust"}}})
    value["jobs"][job]["steps"].append({"run": command})
    tree[selection.WORKFLOW] = yaml.safe_dump(value)
    with pytest.raises(selection.SelectionError):
        recipe(tree)


@pytest.mark.parametrize("prefix", ["cd rust\n", "Set-Location rust\n", ""])
def test_step_working_directory_cannot_change_a_claimed_root_selection(tree, prefix):
    value = yaml.safe_load(tree[selection.WORKFLOW])
    value["jobs"]["rust"]["steps"].append({"working-directory": "rust" if not prefix else ".",
        "run": prefix + "python -m pytest rust/test_base.py"})
    tree[selection.WORKFLOW] = yaml.safe_dump(value)
    with pytest.raises(selection.SelectionError, match="working.directory"):
        recipe(tree)


@pytest.mark.parametrize("argument", ["$env:X", "${{ matrix.f }}", "$X", "${X}"])
def test_nonliteral_python_script_arguments_refuse(tree, argument):
    workflow(tree, "rust", "python " + argument)
    with pytest.raises(selection.SelectionError):
        recipe(tree)


@pytest.mark.parametrize("command", ["bash rust/run-new.sh", "pwsh -File rust/run-new.ps1", "./rust/run-new.sh",
    "& ./rust/run-new.ps1", "sh daemon/run-new.sh", "./rust/run-new.PS1"])
def test_unknown_shell_wrappers_refuse(tree, command):
    value = yaml.safe_load(tree[selection.WORKFLOW])
    value["jobs"]["candidate"] = {"defaults": {"run": {"working-directory": "rust"}}, "steps": [{"run": command}]}
    tree[selection.WORKFLOW] = yaml.safe_dump(value)
    with pytest.raises(selection.SelectionError):
        recipe(tree)


@pytest.mark.parametrize("defaults", [None, {"run": []}])
def test_malformed_working_directory_defaults_are_a_reader_refusal(tree, defaults):
    value = yaml.safe_load(tree[selection.WORKFLOW])
    value["jobs"]["rust"]["defaults"] = defaults
    tree[selection.WORKFLOW] = yaml.safe_dump(value)
    with pytest.raises(selection.SelectionError, match="working.directory"):
        recipe(tree)


def test_non_test_python_source_with_unbalanced_shell_quotes_is_skipped(tree):
    value = yaml.safe_load(tree[selection.WORKFLOW])
    value["jobs"]["new-job"] = {"steps": [{"shell": "python", "run": "text = '''don't'''"}]}
    tree[selection.WORKFLOW] = yaml.safe_dump(value)
    assert recipe(tree).commands


def test_unparseable_test_command_still_refuses(tree):
    workflow(tree, "rust", 'python -m pytest "rust/test_hidden.py')
    with pytest.raises(selection.SelectionError, match="cannot parse"):
        recipe(tree)


def test_root_step_override_and_quoted_package_markers_remain_supported(tree):
    value = yaml.safe_load(tree[selection.WORKFLOW])
    value["jobs"]["rust"]["defaults"] = {"run": {"working-directory": "rust"}}
    for step in value["jobs"]["rust"]["steps"]:
        step["working-directory"] = "."
    value["jobs"]["rust"]["steps"].append({"run": 'python -m pip install "pytest; python_version>=3.11"'})
    tree[selection.WORKFLOW] = yaml.safe_dump(value)
    assert recipe(tree).commands


def test_known_parity_report_redirection_is_not_a_compound_command(tree):
    workflow(tree, "parity-checks", "python -m pytest rust/test_native.py -k witness > $log 2>&1")
    assert recipe(tree).commands


@pytest.mark.parametrize("job", ["scope", "candidate", "stress", "daemon-image"])
def test_non_selection_jobs_cannot_gain_a_direct_rust_test_line(tree, job):
    value = yaml.safe_load(tree[selection.WORKFLOW])
    value["jobs"][job] = {"steps": [{"name": "Unexpected tests", "run": "python -m pytest rust/test_extra.py"}]}
    tree[selection.WORKFLOW] = yaml.safe_dump(value)
    with pytest.raises(selection.SelectionError, match=job):
        recipe(tree)


@pytest.mark.parametrize("launcher", ["python", "python3", "pytest"])
def test_literal_pytest_launchers_are_supported(tree, launcher):
    command = launcher + (" -m pytest" if launcher != "pytest" else "")
    workflow(tree, "rust", command + " rust/test_base.py")
    assert "rust/test_base.py" in recipe(tree).commands[0].paths


@pytest.mark.parametrize("command", [
    "python -m pytest rust/test_base.py -k base",
    "python -m pytest rust/test_base.py --deselect=rust/test_base.py::test_base",
    "python -m pytest rust/test_base.py --ignore rust/test_other.py",
])
def test_a_filtered_rust_job_cannot_claim_the_entire_file(tree, command):
    workflow(tree, "rust", command)
    with pytest.raises(selection.SelectionError, match="filtered"):
        recipe(tree)


@pytest.mark.parametrize("job", ["rust", "parity-checks"])
def test_unknown_pytest_launchers_refuse_but_installing_pytest_is_allowed(tree, job):
    workflow(tree, job, "python -m pip install pytest pyyaml")
    assert recipe(tree).commands
    workflow(tree, job, "uv run python -m pytest rust/test_base.py")
    with pytest.raises(selection.SelectionError, match="unsupported pytest launcher"):
        recipe(tree)


@pytest.mark.parametrize("option", ["--lf", "--deselect=rust/test_extra.py::test_extra", "--ignore-glob=*.py"])
def test_unclassified_pytest_filters_cannot_narrow_parity_units(tree, option):
    workflow(tree, "parity-checks", "python -m pytest rust/test_extra.py " + option)
    with pytest.raises(selection.SelectionError):
        recipe(tree)


@pytest.mark.parametrize("arguments", ["$env:EXTRA_TESTS", "${EXTRA_TESTS}",
    "tests/test_outside_rust.py", "&& python rust/new_harness.py", "--junitxml", "-p"])
def test_unreadable_or_unclassified_pytest_arguments_refuse(tree, arguments):
    workflow(tree, "rust", "python -m pytest rust/test_base.py " + arguments)
    with pytest.raises(selection.SelectionError):
        recipe(tree)


def test_controller_reads_the_requested_immutable_head_instead_of_its_own_list(tree, monkeypatch):
    from ops import integrate

    head = "a" * 40
    reads = []
    def source(rev, path):
        reads.append((rev, path))
        return tree.get(path)
    monkeypatch.setattr(integrate, "_budget_text", source)
    monkeypatch.setattr(integrate, "_out", lambda *args: "\n".join(tree))
    monkeypatch.setattr(integrate, "RUST_HARNESS", ("rust/test_wrong_checkout.py",))
    integrate._harness_recipe.cache_clear()
    try:
        result = integrate._harness_recipe(head)
        assert "rust/test_extra.py" in result.commands[0].paths
        assert "rust/test_wrong_checkout.py" not in result.commands[0].paths
        assert reads and all(rev == head for rev, _ in reads)
        assert (head, selection.WORKFLOW) in reads
    finally:
        integrate._harness_recipe.cache_clear()


def test_known_native_only_filtered_mutation_command_stays_in_its_lane(tree):
    workflow(tree, "parity-checks", "python -m pytest rust/test_native.py -k witness")
    assert recipe(tree).commands[0].paths
    workflow(tree, "parity-checks", "python -m pytest rust/test_extra.py -k witness")
    with pytest.raises(selection.SelectionError, match="filtered"):
        recipe(tree)


@pytest.mark.parametrize("kind", ["unit_tests", "native_tests"])
def test_mixed_file_selects_only_its_candidate_free_nodes(tree, kind):
    value = json.loads(tree[selection.CLASSIFICATION])
    value["files"]["rust/test_extra.py"] = {"kind": "mixed", "reason": "separate native proof",
        kind: ["test_extra"] if kind == "unit_tests" else ["test_native"]}
    tree[selection.CLASSIFICATION] = json.dumps(value)
    tree["rust/test_extra.py"] += "def test_native(): pass\n"
    assert "rust/test_extra.py::test_extra" in recipe(tree).commands[0].paths
    assert "rust/test_extra.py::test_native" not in recipe(tree).commands[0].paths


def test_stale_mixed_selector_refuses_instead_of_losing_coverage(tree):
    value = json.loads(tree[selection.CLASSIFICATION])
    value["files"]["rust/test_extra.py"] = {"kind": "mixed", "reason": "fixture",
        "unit_tests": ["test_missing"]}
    tree[selection.CLASSIFICATION] = json.dumps(value)
    with pytest.raises(selection.SelectionError, match="missing tests"):
        recipe(tree)


def test_unittest_discovery_is_read_from_the_delegating_wrapper_at_the_head(tree):
    wrapper = "rust/daemon/harness/startup_ci.py"
    tree[wrapper] = '''
checks = [[sys.executable, "-m", "unittest", "discover", "-s", str(HERE), "-p", "test_startup_*.py"]]
'''
    workflow(tree, "parity-checks", "python " + wrapper + " --no-build")
    file = "rust/daemon/harness/test_startup_fidelity.py"
    tree[file] = "class Fixture: pass\n"
    value = json.loads(tree[selection.CLASSIFICATION])
    value["files"][file] = {"kind": "unit", "reason": "mocked startup"}
    tree[selection.CLASSIFICATION] = json.dumps(value)
    command, = recipe(tree).commands[1:]
    assert command.runner == "unittest-discover"
    assert command.paths == (file,)
    assert command.start == "rust/daemon/harness"
    assert command.pattern == "test_startup_*.py"
    assert "python -m unittest discover" in recipe(tree).display()
    tree[wrapper] = "checks = unknown_commands()\n"
    with pytest.raises(selection.SelectionError, match="no readable unittest"):
        recipe(tree)


def test_direct_unittest_files_keep_their_original_runner(tree):
    workflow(tree, "parity-checks", "python rust/test_extra.py")
    assert selection.Command("unittest-file", ("rust/test_extra.py",)) in recipe(tree).commands


@pytest.mark.parametrize("arguments", ["--unknown-option", "SomeOtherTest", "--help"])
def test_direct_unittest_arguments_cannot_be_silently_discarded(tree, arguments):
    workflow(tree, "parity-checks", "python rust/test_extra.py " + arguments)
    with pytest.raises(selection.SelectionError, match="unittest arguments"):
        recipe(tree)


def test_unit_fixture_dependencies_use_the_batch_workflows_exact_pins(tree):
    value = json.loads(tree[selection.CLASSIFICATION])
    value["files"]["rust/test_extra.py"]["requirements"] = ["onnx"]
    tree[selection.CLASSIFICATION] = json.dumps(value)
    workflow(tree, "parity-checks", 'python -m pip install "onnx==1.22.0" "onnxruntime==1.27.0"')
    result = recipe(tree)
    assert result.requirements == ("onnx==1.22.0",)
    assert result.record()[0]["requirements"] == ["onnx==1.22.0"]
    tree[selection.WORKFLOW] = tree[selection.WORKFLOW].replace("onnx==1.22.0", "onnx==1.23.0")
    assert recipe(tree).requirements == ("onnx==1.23.0",)


@pytest.mark.parametrize("command", ["", "python -m pip install onnx>=1.22",
    "python -m pip install onnx==1.22.0 onnx==1.23.0"])
def test_a_fixture_dependency_without_one_exact_workflow_pin_refuses(tree, command):
    value = json.loads(tree[selection.CLASSIFICATION])
    value["files"]["rust/test_extra.py"]["requirements"] = ["onnx"]
    tree[selection.CLASSIFICATION] = json.dumps(value)
    workflow(tree, "parity-checks", command)
    with pytest.raises(selection.SelectionError, match="exact workflow pin"):
        recipe(tree)


def test_missing_selected_file_refuses_before_remote_work(tree):
    tree.pop("rust/test_extra.py")
    with pytest.raises(selection.SelectionError, match="absent from the batch head"):
        recipe(tree)


def test_the_repository_recipe_includes_mocked_startup_and_all_rust_job_paths():
    root = Path(__file__).resolve().parents[1]
    paths = tuple(p.relative_to(root).as_posix() for p in (root / "rust").rglob("test_*.py"))
    # Read-only parsing: no import or test collection from the harness tree.
    result = selection.recipe(lambda path: (root / path).read_text(encoding="utf-8"), paths)
    assert any("rust/daemon/harness/test_startup_fidelity.py" in command.paths
               for command in result.commands)
    assert any("rust/daemon/harness/test_schema_executor.py" in command.paths
               for command in result.commands)
    assert all("test_sent_yaml.py" not in path for command in result.commands for path in command.paths)

