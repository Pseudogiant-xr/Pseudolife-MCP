"""A consolidation proof must reject missing parts, selectors and OSes."""
import copy
import re

import pytest

import parity_case_sets as proof


SUITE = "${{ fromJSON(needs.scope.outputs.parity).suite }}"
INCLUDE = "${{ fromJSON(needs.scope.outputs.parity).include }}"
EXCLUDE = "${{ fromJSON(needs.scope.outputs.parity).exclude }}"


def fixture():
    common = (f"SYSTEMS = ('ubuntu-latest', 'windows-latest')\nSUITE_EXPRESSION = {SUITE!r}\n"
              f"INCLUDE_EXPRESSION = {INCLUDE!r}\nEXCLUDE_EXPRESSION = {EXCLUDE!r}\n")
    before = common + ("SUITES=['cli-daemon','cli-daemon-2','other']\n"
                       "FLOOR=('cli-daemon','cli-daemon-2')\nDAEMON=FLOOR\n"
                       "SHARDS=[{'suite':'cli-daemon','startup_part':1},"
                       "{'suite':'cli-daemon-2','startup_part':2},{'suite':'other','job_timeout':5}]\n")
    # The production DAEMON is literal; keep the fixture equally data-only.
    before = before.replace("DAEMON=FLOOR", "DAEMON=('cli-daemon','cli-daemon-2')")
    after = common + ("SUITES=['cli-startup','other']\nFLOOR=('cli-startup',)\nDAEMON=('cli-startup',)\n"
                      "SHARDS=[{'suite':'cli-startup','startup_part':1},{'suite':'other','job_timeout':5}]\n")
    run = ("python rust/daemon/harness/startup_ci.py --part ${{ matrix.startup_part }} "
           "--out daemon-startup-parity.json")
    workflow = {"jobs": {"parity-checks": {"strategy": {"matrix": {
        "os": ["ubuntu-latest", "windows-latest"], "suite": SUITE, "include": INCLUDE, "exclude": EXCLUDE}},
        "steps": [{"if": "matrix.suite == 'cli-daemon'", "run": "python schema.py", "timeout-minutes": 10},
                  {"if": "startsWith(matrix.suite, 'cli-daemon')", "run": run, "timeout-minutes": 26},
                  {"if": "matrix.suite == 'other'", "run": "python compare.py --case stable"}]}}}
    changed = copy.deepcopy(workflow)
    steps = changed["jobs"]["parity-checks"]["steps"]
    steps[0]["if"] = "matrix.suite == 'cli-startup'"
    steps[1]["if"] = "!cancelled() && matrix.suite == 'cli-startup'"
    second = copy.deepcopy(steps[1])
    second["run"] = run.replace("${{ matrix.startup_part }}", "2").replace("parity.json", "parity-part2.json")
    steps.insert(2, second)
    return workflow, before, changed, after


def test_identical_generators_and_bound_selections_prove_both_os_sets():
    report = proof.compare(*fixture())
    assert report["before_jobs"] == 6 and report["after_jobs"] == 4
    for environment in report["environments"].values():
        assert environment["startup_parts"] == {"ubuntu-latest": [1, 2], "windows-latest": [1, 2]}


@pytest.mark.parametrize("change", ["drop_part", "change_selector", "drop_os", "change_environment"])
def test_near_misses_cannot_pass_case_equality(change):
    workflow, before, changed, after = fixture()
    job = changed["jobs"]["parity-checks"]
    if change == "drop_part":
        del job["steps"][2]
    elif change == "change_selector":
        job["steps"][1]["run"] += " --skip-cases"
    elif change == "drop_os":
        job["strategy"]["matrix"]["os"] = ["ubuntu-latest"]
    else:
        job["env"] = {"SKIP_CASES": "true"}
    with pytest.raises(ValueError):
        proof.compare(workflow, before, changed, after)


def test_unrelated_scope_changes_are_rejected():
    workflow, before, changed, after = fixture()
    with pytest.raises(ValueError, match="scope code"):
        proof.compare(workflow, before, changed, after + "EVENT_BOUND=('other',)\n")


def test_only_receipt_output_names_are_normalized():
    assert proof.canonical("python startup_ci.py --out daemon-startup-parity-part2.json") == \
        "python startup_ci.py --out daemon-startup-parity.json"
    value = "python startup_ci.py --golden daemon-startup-parity-part2.json --case cli-daemon-2"
    assert proof.canonical(value) == value


def test_generator_changes_fail_before_workflow_comparison(monkeypatch, tmp_path):
    def git(*args):
        return "a" * 40 if args[0] == "rev-parse" else "tests/changed.py\n"
    monkeypatch.setattr(proof, "git", git)
    monkeypatch.setattr(proof, "ROOT", tmp_path)
    with pytest.raises(SystemExit):
        proof.main(["--before", "reviewed-parent"])


@pytest.mark.parametrize("cause", ["disk", "lease", "dependency"])
@pytest.mark.parametrize("os_name", ["ubuntu-latest", "windows-latest"])
def test_failed_shared_setup_blocks_both_startup_parts(cause, os_name):
    import yaml
    job = yaml.safe_load((proof.ROOT / proof.WORKFLOW).read_text())["jobs"]["parity-checks"]
    class Admission(proof.Context):
        def lookup(self, path):
            return "skipped" if path == ["steps", "startup_setup", "outcome"] else super().lookup(path)
    context = Admission({"os": os_name, "suite": "cli-startup"}, "self-hosted")
    parts = [step for step in job["steps"] if step.get("name", "").startswith("Daemon resident startup parity") and step.get("run")]
    assert len(parts) == 2
    assert all(proof.truthy(context.run(proof.condition_text(step["if"]))) is False for step in parts), cause


@pytest.mark.parametrize("cancelled,expected", [(False, True), (True, False)])
@pytest.mark.parametrize("os_name", ["ubuntu-latest", "windows-latest"])
def test_completed_setup_allows_comparison_continuation_but_not_cancellation(cancelled, expected, os_name):
    import yaml
    job = yaml.safe_load((proof.ROOT / proof.WORKFLOW).read_text())["jobs"]["parity-checks"]
    class Admission(proof.Context):
        def lookup(self, path):
            return "success" if path == ["steps", "startup_setup", "outcome"] else super().lookup(path)
        def call(self, name):
            value = super().call(name)
            return cancelled if name == "cancelled" else (False if name == "success" else value)
    context = Admission({"os": os_name, "suite": "cli-startup"}, "self-hosted")
    parts = [step for step in job["steps"] if step.get("name", "").startswith("Daemon resident startup parity") and step.get("run")]
    assert all(proof.truthy(context.run(proof.condition_text(step["if"]))) is expected for step in parts)
    setup = next(step for step in job["steps"] if step.get("id") == "startup_setup")
    assert not re.search(r"\b(success|failure|always|cancelled)\s*\(", setup["if"])
    assert not setup.get("continue-on-error", False)
