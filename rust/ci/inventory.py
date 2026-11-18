"""Check executable CI selections against the accepted row/control registries.

Since the Python freeze (14 November 2026) the contract is: every accepted CLI row
and daemon scenario replays its committed goldens in a parity shard or is named
as unreplayed here or in parity_scope.UNREPLAYED_ROWS; no job a pull request,
batch, master push, schedule or manual CI starts compares against a live Python
oracle. Only the explicitly selected manual stress dispatch is exempt.
"""

import ast
import itertools
import re
import sys
from pathlib import Path

from test_workflow import commands

ROOT = Path(__file__).resolve().parents[2]
SYSTEMS = ["ubuntu-latest", "windows-latest"]
# Both schedules retain the ordinary Rust checks after source-mutant retirement.
NIGHTLY_CRON = '23 3 * * 1-6'
WEEKLY_CRON = '23 3 * * 0'
PARITY_CANCELLED_STEP = {"name": "A needed job was cancelled",
                         "if": "${{ contains(needs.*.result, 'cancelled') }}", "run": "exit 1"}
PARITY_VERDICT_IF = "${{ !contains(needs.*.result, 'cancelled') }}"
PARITY_EVENT_GATE = """
$results = ConvertFrom-Json $env:GATE_RESULTS -AsHashtable
$docsOnly = $env:GATE_EVENT -cin @('pull_request', 'merge_group', 'push') -and $results['scope'].result -ceq 'success' -and $results['scope'].outputs.rust -ceq 'false'
$fastPr = $env:GATE_EVENT -ceq 'pull_request' -and $results['scope'].result -ceq 'success' -and $results['scope'].outputs.full -ceq 'false'
$pushPassed = $env:GATE_EVENT -ceq 'schedule' -and $results['scope'].result -ceq 'success' -and $results['scope'].outputs.push_passed -ceq 'true'
$noPairs = $env:GATE_EVENT -ceq 'push' -and $results['scope'].result -ceq 'success' -and $results['scope'].outputs.parity_any -ceq 'false'
foreach ($job in @('rust', 'candidate', 'parity-checks')) {
  $skipChosen = $docsOnly -or ($fastPr -and $job -cne 'rust') -or ($pushPassed -and $job -cne 'candidate') -or ($noPairs -and $job -ceq 'parity-checks')
  if ($results[$job].result -cne 'success' -and -not ($skipChosen -and $results[$job].result -ceq 'skipped')) {
    Write-Error "$job did not succeed"
    exit 1
  }
}
"""
# The parity suites that held only live comparisons against the Python
# oracle and left CI with the freeze. None may come back into the matrix.
RETIRED_SUITES = frozenset({"cli-live", "judges", "h5-audit", "h5-backup", "h5-w1a", "h5-background",
                            "h5-daemon-mutants", "h5-graph-mutants", "h5-light"})
# Daemon harness scenarios (rust/daemon/harness/run.py SCENARIOS) no hosted
# job replays since the freeze. Each ran only live against the Python
# oracle, or replayed a recording the same run made from it (the H5 suites).
# A new scenario gets a golden replay in a parity shard or is named here.
UNREPLAYED_SCENARIOS = frozenset({
    # h5-w1a: live with a same-run recording, then its replay.
    "tokens", "tokenless", "pair-budget", "config", "extractor", "db-down", "lease-held", "reaper",
    "dim-mismatch", "stamped", "seeded", "json-float-shortest-ties", "trust-bind", "null-embedding",
    "unconstrained-dims", "db-lost", "encodings", "map-order", "map-order-reversed", "mcp-modern",
    "mcp-modern-subscriptions", "mcp-cancellation", "mcp-modern-cancellation", "mcp-dream-rollback",
    "mcp-search-explain",
    # h5-background: live only.
    "session-reap", "session-restart", "session-resume-refusal", "session-sweep",
    "session-tombstone-restart", "sweep-recovery",
    # Live only before the freeze too (coverage_plan EXCEPTIONS['daemon_scenarios']).
    "pairing", "console-signin", "console-passkey-signin",
    # The security remote-bind cases: no golden and, since 16 November 2026,
    # no CI run; Rust tests hold every case (rust/daemon/src/http_security_tests.rs,
    # rust/daemon/tests/security_admission.rs), and run.py runs them by hand.
    "security-remote-open", "security-remote-auth",
})
# Run text that starts a Python oracle to compare the candidate against, or
# replays a recording the same run made from one. No job a pull request, a
# batch, a master push, a schedule or manual CI starts may hold one.
# Only the named manual stress dispatch is exempt.
LIVE_INVOCATIONS = (
    re.compile(r"\bevals[./]rust_port\b"),
    re.compile(r"\bevals[./]rust_baseline\b"),
    # rust/ optional: a step with `working-directory: rust` names the path
    # relative to it.
    re.compile(r"(?:rust/)?daemon/harness/(?:run|graph_store|principals|dream_cursors)\.py\s+(?:live|mutants|oracle)\b"),
    re.compile(r"(?:rust/)?daemon/harness/(?:mcp_ci|mcp_binding_sweep|mcp_run)\.py\b"),
    re.compile(r"rust/ci/body_evidence\.py\s+run\b"),
    re.compile(r"rust/ci/body_oracle\.py\b"),
    re.compile(r"rust/ci/run_coverage\.py\s+--(?:suite|source-shard)\b"),
    # The CLI harness without an exact --golden (--golden-dir is not one),
    # by path or as a module; a recording from the oracle; the in-process
    # Codex planning comparison.
    re.compile(r"(?:(?:rust/)?cli_harness(?:/__main__\.py)?|-m\s+rust\.cli_harness)(?=\s|$)(?!.*\s--golden(?=\s|$))"),
    re.compile(r"\s--record(?:-goldens)?(?=\s|$)"),
    re.compile(r"setup_codex_parity\.py\b"),
)
LIVE_EXEMPT_JOBS = ("stress-plan", "stress-build", "stress", "stress-report")


# A trailing shell or PowerShell comment, which must not supply a --golden.
TRAILING_COMMENT = re.compile(r"\s#.*$")
# Commands sharing one line; each is judged on its own.
COMMAND_SEPARATORS = re.compile(r";|&&|\|\|")


def live_invocations(workflow, exempt=True):
    """Every (job, step, line) whose run text starts a live comparison;
    ``exempt`` applies the exempt jobs, which name rust.yml's jobs and are
    off for every other workflow.

    Lexical: it catches a live comparison coming back by accident, not a
    deliberate evasion (a mode or script from a variable, a module form, a
    harness change behind an allowed line; the PR's "Known limits")."""
    found = []
    for name, job in workflow["jobs"].items():
        if exempt and name in LIVE_EXEMPT_JOBS:
            continue
        for step in job.get("steps", []):
            for line in step.get("run", "").splitlines():
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                command_text = TRAILING_COMMENT.sub("", text).replace("\\", "/")
                for command in COMMAND_SEPARATORS.split(command_text):
                    if any(p.search(command.strip()) for p in LIVE_INVOCATIONS):
                        found.append((name, step.get("name", ""), text))
                        break
    return found


def all_workflow_live_invocations(root=ROOT):
    """live_invocations over every workflow file (*.yml and *.yaml), not
    only rust.yml; the exempt job names hold in rust.yml only. Run from
    __main__ and the tests, not from check(), which takes one workflow."""
    import yaml
    found = []
    workflows = root / ".github" / "workflows"
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        found += [(path.name, *row) for row in live_invocations(
            {"jobs": workflow.get("jobs", {})}, exempt=path.name == "rust.yml")]
    return found


def assigned_list(path, name):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))


def daemon_scenarios():
    sys.path.insert(0, str(ROOT / 'rust/daemon/harness'))
    import run
    return set(run.SCENARIOS)


def exception_reasons(value):
    if isinstance(value, dict):
        for reason in value.values():
            exception_reasons(reason)
    else:
        assert isinstance(value, str) and value.strip(), 'empty or invalid exception reason'


def backup_zero_retention_scope(exceptions):
    scoped = exceptions['cli_case_selections']
    assert set(scoped) == {'windows-backup-zero-retention-clock-race'}, 'backup zero exception scope changed'
    entry = scoped['windows-backup-zero-retention-clock-race']
    assert set(entry) == {'platform', 'mode', 'row', 'case', 'reason'} \
        and (entry['platform'], entry['mode'], entry['row'], entry['case']) \
        == ('windows', 'both', 'backup', 'file-mode-keep-zero'), 'backup zero exception scope changed'
    assert all(proof in entry['reason'] for proof in ('GetSystemTimeAsFileTime', '0.015625',
                                                     '254/deleted 46', '229/deleted 71')), \
        'backup zero reference measurement missing'
    tree = ast.parse((ROOT / 'rust/cli_harness/rows/backup.py').read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'Case'
             and node.args and isinstance(node.args[0], ast.Constant)
             and node.args[0].value == 'file-mode-keep-zero']
    assert len(calls) == 1, 'backup zero exception lost or duplicated case'
    options = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    assert ast.literal_eval(options['platforms']) == ('linux',) \
        and ('golden' not in options or ast.literal_eval(options['golden']) is True) \
        and 'skip_if' not in options, 'backup zero Linux live/record/golden selection changed'
    assert isinstance(calls[0].args[1], ast.List) \
        and isinstance(calls[0].args[1].elts[-1], ast.Constant) \
        and calls[0].args[1].elts[-1].value == '--keep-days=0', 'backup zero argument changed'
    native = (ROOT / 'rust/shim/src/cli/backup/mod.rs').read_text(encoding='utf-8')
    for name in ('rotation_keeps_files_equal_to_the_python_clock_cutoff',
                 'windows_clock_matches_python_within_the_same_clock_tick'):
        assert re.search(r'#\[test\]\s+fn ' + name + r'\(', native), 'backup zero native proof dropped'


def check(workflow):
    from coverage_plan import EXCEPTIONS

    from parity_scope import resolve
    # The full matrix, as the nightly runs it (a master push leaves out the
    # pairs its merged batch passed; parity_scope.pruned).
    workflow = resolve(workflow)
    exception_reasons(EXCEPTIONS)
    backup_zero_retention_scope(EXCEPTIONS)
    rows = assigned_list("rust/cli_harness/rows/__init__.py", "ROWS")
    jobs = workflow["jobs"]
    assert "source-mutants" not in jobs, "the source-mutant job is retired"
    parity = jobs["parity-checks"]
    ordinary_ci = "github.event_name != 'workflow_dispatch' || inputs.mode == 'ci'"
    assert parity['if'] == (f"${{{{ !cancelled() && needs.scope.outputs.push_passed != 'true' "
                            f"&& needs.scope.outputs.parity_any != 'false' && ({ordinary_ci}) "
                            "&& needs.candidate.result == 'success' "
                            "&& needs.candidate-harness.result == 'success' "
                            "&& needs.fixtures.result == 'success' }}"), 'parity event selection changed'
    matrix = parity["strategy"]["matrix"]
    assert matrix["os"] == SYSTEMS
    assert not matrix.get("exclude"), 'required parity matrix leaves out a pair'
    assert not RETIRED_SUITES & set(matrix["suite"]), 'a retired live-comparison suite is back in the matrix'
    live = live_invocations(workflow)
    assert not live, f'a job starts a live comparison against the Python oracle: {live[0]}'
    # The name-exempt stress jobs hang off the stress plan, a manual dispatch.
    assert jobs["stress-plan"]["if"] == "github.event_name == 'workflow_dispatch' && inputs.mode == 'stress'", \
        'the stress plan no longer runs only on a manual stress dispatch'
    for name in ("stress-build", "stress", "stress-report"):
        assert "stress-plan" in (jobs[name].get("needs") or []), f'{name} no longer depends on the stress plan'

    # Every accepted row replays its committed goldens in some shard, or is
    # named as unreplayed (parity_scope.UNREPLAYED_ROWS).
    from parity_scope import ROW_SUITES, UNREPLAYED_ROWS
    golden_rows = set()
    for current in parity["steps"]:
        for words in commands(current.get("run", "")):
            if words[:2] == ["python", "rust/cli_harness"] and "--golden" in words:
                golden_rows.update(words[i + 1] for i, word in enumerate(words[:-1]) if word == "--row")
    w1c = next(s for s in parity["steps"] if s.get("name") == "CLI W1-C golden harness")
    harness = [w for w in commands(w1c["run"]) if w[:2] == ["python", "rust/cli_harness"]]
    assert len(harness) == 1 and harness[0][2:4] == ["@golden", "--golden"], 'W1-C rows no longer replay their goldens'
    for shard in matrix["include"]:
        golden_rows.update(re.findall(r"'([a-z_]+)'", shard.get("w1c_rows", "")))
    assert golden_rows.isdisjoint(UNREPLAYED_ROWS), 'a replayed row is still named unreplayed'
    assert set(rows) == golden_rows | set(UNREPLAYED_ROWS), 'accepted row neither replayed nor named unreplayed'
    assert set(ROW_SUITES) == golden_rows, 'parity scope routes a row no shard replays'

    # Every daemon scenario replays its committed golden, or is named.
    golden_scenarios = set()
    for step in parity['steps']:
        for words in commands(step.get('run', '')):
            if words[:3] == ['python', 'rust/daemon/harness/run.py', 'golden'] and '--only' in words:
                golden_scenarios.update(itertools.takewhile(lambda w: not w.startswith('--'),
                                                            words[words.index('--only') + 1:]))
    unknown = (golden_scenarios | UNREPLAYED_SCENARIOS) - daemon_scenarios()
    assert not unknown, f'unknown daemon scenario named: {sorted(unknown)}'
    assert golden_scenarios.isdisjoint(UNREPLAYED_SCENARIOS), 'a replayed daemon scenario is still named unreplayed'
    missing = daemon_scenarios() - golden_scenarios - UNREPLAYED_SCENARIOS
    assert not missing, f'accepted daemon scenario neither replayed nor named unreplayed: {sorted(missing)}'

    events = workflow.get('on', workflow.get(True))
    assert events.get('push') == {'branches': ['master']} \
        and events.get('schedule') == [{'cron': NIGHTLY_CRON}, {'cron': WEEKLY_CRON}], 'CI events dropped or changed'
    assert {'pull_request', 'merge_group', 'workflow_dispatch'} <= set(events), 'CI events dropped or changed'
    gate = jobs['parity']
    assert gate['needs'] == ['scope', 'rust', 'candidate', 'parity-checks'], 'Parity event gate lost dependency'
    assert gate['if'] == f'always() && ({ordinary_ci})' and not gate.get('continue-on-error'), 'Parity event gate can skip failures'
    assert len(gate['steps']) == 2, 'Parity event gate steps changed'
    cancelled, required = gate['steps']
    # A cancelled need fails its own step, which ops/integrate.py reads as
    # "cancelled, no verdict"; the verdict step skips only in that case.
    assert cancelled == PARITY_CANCELLED_STEP, 'Parity event gate can skip a cancelled job'
    assert required.get('shell') == 'pwsh' and required.get('if') == PARITY_VERDICT_IF \
        and not required.get('continue-on-error'), 'Parity event gate can skip failures'
    assert required['env'] == {'GATE_RESULTS': '${{ toJSON(needs) }}',
                              'GATE_EVENT': '${{ github.event_name }}'}, 'Parity event gate inputs changed'
    actual = [line.strip() for line in required['run'].splitlines() if line.strip()]
    expected = [line.strip() for line in PARITY_EVENT_GATE.splitlines() if line.strip()]
    assert actual == expected, 'Parity event gate accepts an unsupported result'
    assert any(s.get("name") == "ported-row-ci-inventory" for s in jobs["rust"]["steps"])

if __name__ == "__main__":
    import yaml
    check(yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8")))
    everywhere = all_workflow_live_invocations()
    assert not everywhere, f"a workflow starts a live comparison against the Python oracle: {everywhere[0]}"
    print("ported-row-ci-inventory: all named selections accounted for")
