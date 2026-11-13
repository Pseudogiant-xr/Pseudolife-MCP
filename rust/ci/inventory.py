"""Check executable CI selections against the accepted row/control registries.

Since the Python freeze (14 November 2026) the contract is: every accepted CLI row
and daemon scenario replays its committed goldens in a parity shard or is named
as unreplayed here or in parity_scope.UNREPLAYED_ROWS; no job a pull request,
batch, master push or schedule starts compares against a live Python oracle,
except the weekly CLI source mutants.
"""

import ast
import itertools
from collections import Counter
import json
import re
import sys
from pathlib import Path

from test_workflow import commands

ROOT = Path(__file__).resolve().parents[2]
SYSTEMS = ["ubuntu-latest", "windows-latest"]
# Weekly since the Python freeze (14 November 2026): the Sunday schedule
# and manual CI. The weekday nightly is the other schedule.
NIGHTLY_CRON = '23 3 * * 1-6'
WEEKLY_CRON = '23 3 * * 0'
SOURCE_EVENTS = (f"(github.event_name == 'schedule' && github.event.schedule == '{WEEKLY_CRON}') "
                 "|| (github.event_name == 'workflow_dispatch' && inputs.mode == 'ci')")
PARITY_CANCELLED_STEP = {"name": "A needed job was cancelled",
                         "if": "${{ contains(needs.*.result, 'cancelled') }}", "run": "exit 1"}
PARITY_VERDICT_IF = "${{ !contains(needs.*.result, 'cancelled') }}"
PARITY_EVENT_GATE = """
$results = ConvertFrom-Json $env:GATE_RESULTS -AsHashtable
$docsOnly = ($env:GATE_EVENT -ceq 'pull_request' -or $env:GATE_EVENT -ceq 'merge_group') -and $results['scope'].result -ceq 'success' -and $results['scope'].outputs.rust -ceq 'false'
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
$sourceResult = $results['source-mutants'].result
$weekday = $env:GATE_EVENT -ceq 'schedule' -and $env:GATE_SCHEDULE -cne 'WEEKLY_CRON'
if ($sourceResult -cne 'success' -and -not (($env:GATE_EVENT -ceq 'pull_request' -or $env:GATE_EVENT -ceq 'merge_group' -or $env:GATE_EVENT -ceq 'push' -or $weekday) -and $sourceResult -ceq 'skipped')) {
  Write-Error "source-mutants did not succeed outside the PR, merge-group, push and weekday-nightly skip exception"
  exit 1
}
""".replace("WEEKLY_CRON", WEEKLY_CRON)
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
    # The security remote-bind cases: no golden; they run live under
    # LIVE_SECURITY_EXCEPTIONS below.
    "security-remote-open", "security-remote-auth",
})
# The live comparisons kept after the freeze because each checks a security
# property no Rust test asserts yet (conductor rule, 14 November 2026): the
# exact run lines, by step of the parity steps (parity-checks, and
# parity-selfhosted through the YAML anchor). Each goes once a Rust test
# covers it; nothing else may start a live comparison.
LIVE_SECURITY_JOBS = ("parity-checks", "parity-selfhosted")
LIVE_SECURITY_EXCEPTIONS = {
    "Daemon security live checks without a Rust test": (
        # The startup bind guard and token-map refusals (rust/daemon/src/main.rs
        # `fail(2, ...)` before listening): no Rust test starts the daemon there.
        "python rust/daemon/harness/run.py live --rust-bin $candidate --only security-refusals "
        "--out (Join-Path $env:RUNNER_TEMP 'daemon-security-refusals.json')",
        # Bound on 0.0.0.0: the Host/Origin gate and the bearer still hold.
        "python rust/daemon/harness/run.py live --rust-bin $bindFixture --only security-remote-open "
        "security-remote-auth --out (Join-Path $env:RUNNER_TEMP 'daemon-security-remote.json')",
    ),
}
# Run text that starts a Python oracle to compare the candidate against, or
# replays a recording the same run made from one. No job a pull request, a
# batch, a master push or a schedule starts may hold one, except the weekly
# CLI source mutants (each control compares against the checkout's frozen
# Python, rust/cli_harness/mutants.py). Stress loops are manual dispatches.
LIVE_INVOCATIONS = (
    re.compile(r"\bevals[./]rust_port\b"),
    re.compile(r"\bevals[./]rust_baseline\b"),
    re.compile(r"rust/daemon/harness/(?:run|graph_store|principals|dream_cursors)\.py\s+(?:live|mutants|oracle)\b"),
    re.compile(r"rust/daemon/harness/(?:mcp_ci|mcp_binding_sweep|mcp_run)\.py\b"),
    re.compile(r"rust/ci/body_evidence\.py\s+run\b"),
    re.compile(r"rust/ci/body_oracle\.py\b"),
    re.compile(r"rust/ci/run_coverage\.py\s+--suite\b"),
    # The CLI harness without an exact --golden (--golden-dir is not one),
    # by path or as a module; a recording from the oracle; the in-process
    # Codex planning comparison.
    re.compile(r"(?:rust/cli_harness(?:/__main__\.py)?|-m\s+rust\.cli_harness)(?=\s|$)(?!.*\s--golden(?=\s|$))"),
    re.compile(r"\s--record(?:-goldens)?(?=\s|$)"),
    re.compile(r"setup_codex_parity\.py\b"),
)
LIVE_EXEMPT_JOBS = ("source-mutants", "stress-plan", "stress-build", "stress", "stress-report")


def live_invocations(workflow):
    """Every (job, step, line) whose run text starts a live comparison."""
    found = []
    for name, job in workflow["jobs"].items():
        if name in LIVE_EXEMPT_JOBS:
            continue
        for step in job.get("steps", []):
            for line in step.get("run", "").splitlines():
                text = line.strip()
                if name in LIVE_SECURITY_JOBS and text in LIVE_SECURITY_EXCEPTIONS.get(step.get("name", ""), ()):
                    continue
                if text and not text.startswith("#") and any(p.search(text) for p in LIVE_INVOCATIONS):
                    found.append((name, step.get("name", ""), text))
    return found


def cli_controls():
    result = {}
    for path in (ROOT / "rust/cli_harness/rows").glob("*.py"):
        ids = {node.args[0].value for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "Mutant"}
        if ids:
            result[path.stem] = ids
    return result


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


def exit_propagation(step, invocation):
    lines = [line.strip() for line in step['run'].splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    assert step.get('shell') == 'pwsh' and len(lines) == 2 \
        and lines[1] == 'exit $LASTEXITCODE', 'source-control exit propagation changed'
    assert lines[0] == invocation, 'source-control driver command or failure propagation changed'


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
    from coverage_plan import EXCEPTIONS, SOURCE_SHARDS, PLATFORM_EXCLUSIONS, UNOBSERVED_CONTROLS
    from control_selection import runtime_controls
    from rust.cli_harness import core

    from parity_scope import resolve
    # The full matrix, as the nightly runs it (a master push leaves out the
    # pairs its merged batch passed; parity_scope.pruned).
    workflow = resolve(workflow)
    declared = cli_controls()
    selected = json.loads((ROOT / "rust/ci/source_mutants.json").read_text(encoding="utf-8"))
    assert {row: set(ids) for row, ids in selected.items()} == declared, "source mutant inventory drift"
    exception_reasons(EXCEPTIONS)
    backup_zero_retention_scope(EXCEPTIONS)
    exception_reasons(PLATFORM_EXCLUSIONS)
    exception_reasons(UNOBSERVED_CONTROLS)
    all_controls = set().union(*declared.values())
    for platform in ('linux', 'windows'):
        assert set(PLATFORM_EXCLUSIONS[platform]) <= all_controls, 'unknown platform exclusion'
    observed = set().union(*(all_controls - set(PLATFORM_EXCLUSIONS[p])
                             - set(EXCEPTIONS['cli_mutants'][p]) for p in ('linux', 'windows')))
    assert all_controls - observed == set(UNOBSERVED_CONTROLS), 'source control without any OS execution or named global exception'
    for row in declared:
        runtime_controls(row, core.PLATFORM)
    rows = assigned_list("rust/cli_harness/rows/__init__.py", "ROWS")
    assert set(rows) == set(selected), "accepted row without a source-control CI selection"
    # Payloads must retain every declaration before the dispatcher's platform
    # eligibility and named-exception filters, not merely retain shard names.
    assert set(row for row, ids in SOURCE_SHARDS.values()) == set(rows), 'executable source controls changed rows'
    executable = {row: Counter(mutant for owner, ids in SOURCE_SHARDS.values()
                               if owner == row for mutant in ids) for row in rows}
    assert executable == {row: Counter(ids) for row, ids in selected.items()}, 'executable source controls dropped or duplicated'
    assert all(set(exceptions) <= set().union(*declared.values())
               for exceptions in EXCEPTIONS['cli_mutants'].values()), 'unknown source-control exception'
    jobs = workflow["jobs"]
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

    source = jobs["source-mutants"]
    assert source["strategy"]["matrix"]["os"] == SYSTEMS
    assert set(source["strategy"]["matrix"]["shard"]) == set(SOURCE_SHARDS), "source mutant shard dropped"
    assert "exclude" not in source["strategy"]["matrix"]
    assert not source.get("continue-on-error")
    events = workflow.get('on', workflow.get(True))
    assert events.get('push') == {'branches': ['master']} \
        and events.get('schedule') == [{'cron': NIGHTLY_CRON}, {'cron': WEEKLY_CRON}], 'source events dropped or changed'
    assert {'pull_request', 'merge_group', 'workflow_dispatch'} <= set(events), 'source events dropped or changed'
    assert source['if'] == (f"${{{{ !cancelled() && ({SOURCE_EVENTS}) "
                            "&& needs.candidate.result == 'success' }}"), 'source event selection changed'
    gate = jobs['parity']
    assert gate['needs'] == ['scope', 'rust', 'candidate', 'parity-checks', 'source-mutants'], 'Parity event gate lost dependency'
    assert gate['if'] == f'always() && ({ordinary_ci})' and not gate.get('continue-on-error'), 'Parity event gate can skip failures'
    assert len(gate['steps']) == 2, 'Parity event gate steps changed'
    cancelled, required = gate['steps']
    # A cancelled need fails its own step, which ops/integrate.py reads as
    # "cancelled, no verdict"; the verdict step skips only in that case.
    assert cancelled == PARITY_CANCELLED_STEP, 'Parity event gate can skip a cancelled job'
    assert required.get('shell') == 'pwsh' and required.get('if') == PARITY_VERDICT_IF \
        and not required.get('continue-on-error'), 'Parity event gate can skip failures'
    assert required['env'] == {'GATE_RESULTS': '${{ toJSON(needs) }}',
                              'GATE_EVENT': '${{ github.event_name }}',
                              'GATE_SCHEDULE': '${{ github.event.schedule }}'}, 'Parity event gate inputs changed'
    actual = [line.strip() for line in required['run'].splitlines() if line.strip()]
    expected = [line.strip() for line in PARITY_EVENT_GATE.splitlines() if line.strip()]
    assert actual == expected, 'Parity event gate accepts an unsupported result'
    invocation = next(s for s in source["steps"] if s.get("name") == "CLI source mutants")
    assert not invocation.get("if") and not invocation.get("continue-on-error")
    assert commands(invocation["run"])[0][:6] == ["python", "rust/ci/run_coverage.py", "--source-shard", "${{", "matrix.shard", "}}"]
    exit_propagation(invocation, "python rust/ci/run_coverage.py --source-shard ${{ matrix.shard }} "
                     "--out-dir (Join-Path $env:RUNNER_TEMP 'h5-source')")
    assert not any('oracle_selection' in step.get('run', '') for step in source['steps']), \
        'source mutants select an event oracle again'
    assert any(s.get("name") == "ported-row-and-mutant-ci-inventory" for s in jobs["rust"]["steps"])

if __name__ == "__main__":
    import yaml
    check(yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8")))
    print("ported-row-and-mutant-ci-inventory: all named selections accounted for")
