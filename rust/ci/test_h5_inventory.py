"""Accepted CLI rows and source controls must have executable CI selections."""

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rust" / "daemon" / "harness"))


def test_w1a_offline_fixture_preserves_scenario_configuration(tmp_path, monkeypatch):
    import run
    import embedding_fixture

    created = []
    runtime = tmp_path / 'runtime'
    (runtime / 'capi').mkdir(parents=True)
    (runtime / 'capi/libonnxruntime.so.1').touch()
    (runtime / 'capi/onnxruntime.dll').touch()
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(__file__=str(runtime / '__init__.py')))
    monkeypatch.setattr(embedding_fixture, "create", lambda path, **kw: created.append((path, kw)))
    scenario = run.CustomConfig()
    scenario.ci_fixture = True
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    (home / "data/config.yaml").write_text(scenario.config_yaml)
    environment = scenario.prepare_home(home)
    configuration = yaml.safe_load((home / "data/config.yaml").read_text(encoding="utf-8"))
    assert created == [(home / "model", {"dimension": 1024})]
    assert configuration["memory"]["top_k"] == 3
    assert configuration["coordination"]["enabled"] is False
    assert configuration["embedding"]["backend"] == "onnx"
    assert environment["PSEUDOLIFE_DAEMON_ONNX_DIR"] == str(home / "model")


def test_ported_row_and_mutant_ci_inventory():
    from inventory import check
    check(yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8")))


def test_inventory_reads_utf8_sources_under_windows_default_encoding(monkeypatch):
    from inventory import check
    workflow = yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8'))
    read_text = Path.read_text

    def windows_read_text(path, encoding=None, errors=None):
        return read_text(path, encoding=encoding or 'cp1252', errors=errors)

    monkeypatch.setattr(Path, 'read_text', windows_read_text)
    check(workflow)


def test_source_control_target_is_bound_at_step_scope():
    workflow = yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding="utf-8"))
    source = workflow['jobs']['source-mutants']
    invocation = next(s for s in source['steps'] if s.get('name') == 'CLI source mutants')
    assert invocation.get('env') == {'CARGO_TARGET_DIR': '${{ runner.temp }}/source-mutants-target'}
    assert 'runner.temp' not in str(source.get('env', {}))


def test_cli_mutant_summary_names_witnesses_and_durations(tmp_path, monkeypatch):
    from rust.cli_harness import mutants
    mutant = mutants.Mutant("fixture-control", "mail", "unused", "a", "b")
    monkeypatch.setattr(mutants.rows, "mutants", lambda row: [mutant] if row == "mail" else [])
    monkeypatch.setattr(mutants, "run_mutant", lambda *args: (True, ["fixture-witness"]))
    monkeypatch.setattr(mutants, "_sync_source", lambda *args: None)
    output = tmp_path / "mutants.json"
    assert mutants.main(["mail"], [], None, False, out=output) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["mutants"]["fixture-control"]["caught_by"] == ["fixture-witness"]
    assert result["mutants"]["fixture-control"]["seconds"] >= 0
    assert result["survivors"] == []


@pytest.mark.parametrize("suite", ["h5-audit", "h5-backup", "h5-w1a",
                                  "h5-background", "h5-daemon-mutants", "h5-graph-mutants"])
def test_inventory_rejects_a_dropped_suite(suite):
    from inventory import check
    workflow = yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8"))
    workflow["jobs"]["parity-checks"]["strategy"]["matrix"]["suite"].remove(suite)
    with pytest.raises(AssertionError, match="missing H5 suite"):
        check(workflow)


def test_inventory_rejects_each_dropped_source_shard():
    from inventory import check
    from coverage_plan import SOURCE_SHARDS
    workflow = yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8"))
    for shard in SOURCE_SHARDS:
        mutated = copy.deepcopy(workflow)
        mutated["jobs"]["source-mutants"]["strategy"]["matrix"]["shard"].remove(shard)
        with pytest.raises(AssertionError, match="source mutant shard dropped"):
            check(mutated)


def test_inventory_rejects_a_control_dropped_from_an_executable_shard(monkeypatch):
    from inventory import check
    from coverage_plan import SOURCE_SHARDS
    row, ids = SOURCE_SHARDS['audit-0']
    monkeypatch.setitem(SOURCE_SHARDS, 'audit-0', (row, [m for m in ids if m != 'audit-body-reason-swap']))
    with pytest.raises(AssertionError, match='executable source controls'):
        check(yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding="utf-8")))


@pytest.mark.parametrize('field, value', [('mode', 'live'), ('kind', 'cli'),
                                        ('scenarios', ['extractor'])])
def test_inventory_rejects_a_daemon_control_without_its_mutant_witness(monkeypatch, field, value):
    from inventory import check
    from coverage_plan import SUITES
    actions = copy.deepcopy(SUITES['h5-daemon-mutants'])
    next(a for a in actions if a['mutant'] == 'static-no-csp')[field] = value
    monkeypatch.setitem(SUITES, 'h5-daemon-mutants', actions)
    with pytest.raises(AssertionError, match='executable daemon control'):
        check(yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding="utf-8")))


def test_inventory_rejects_each_dropped_named_mutant(monkeypatch):
    import inventory
    from coverage_plan import DAEMON_CONTROLS, SOURCE_MUTANTS, SUITES
    workflow = yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8"))
    original_loads = json.loads
    for row, mutants in SOURCE_MUTANTS.items():
        for mutant in mutants:
            reduced = copy.deepcopy(SOURCE_MUTANTS)
            reduced[row].remove(mutant)
            with monkeypatch.context() as patch:
                patch.setattr(inventory.json, "loads", lambda text: reduced)
                with pytest.raises(AssertionError, match="source mutant inventory drift"):
                    inventory.check(workflow)
    assert json.loads is original_loads
    for mutant in DAEMON_CONTROLS:
        reduced = [action for action in SUITES['h5-daemon-mutants'] if action['mutant'] != mutant]
        with monkeypatch.context() as patch:
            patch.setitem(SUITES, 'h5-daemon-mutants', reduced)
            with pytest.raises(AssertionError):
                inventory.check(workflow)


@pytest.mark.parametrize('host_separator', [':', ';'])
def test_linux_test_login_selects_its_bundled_icu(tmp_path, monkeypatch, host_separator):
    import run_coverage
    from run_coverage import source_environment
    monkeypatch.setattr(run_coverage.os, 'pathsep', host_separator)
    installation = tmp_path / '.pg0/installation/18.1.0'
    (installation / 'bin').mkdir(parents=True)
    (installation / 'lib').mkdir()
    environment = source_environment('test_login', 'linux', tmp_path, {'LD_LIBRARY_PATH': 'existing'})
    assert environment['LD_LIBRARY_PATH'] == str(installation / 'lib') + ':existing'
    assert source_environment('test_login', 'windows', tmp_path, {}) == {}


@pytest.mark.parametrize('mutation', ['drop_golden', 'drop_record', 'wrong_kind'])
def test_inventory_rejects_lost_w1a_recording_or_replay(monkeypatch, mutation):
    from inventory import check
    from coverage_plan import SUITES
    actions = copy.deepcopy(SUITES['h5-w1a'])
    if mutation == 'drop_golden':
        actions = [a for a in actions if not (a['scenario'] == 'extractor' and a['mode'] == 'golden')]
    else:
        action = next(a for a in actions if a['scenario'] == 'extractor' and a['mode'] == 'live')
        action['record' if mutation == 'drop_record' else 'kind'] = False if mutation == 'drop_record' else 'cli'
    monkeypatch.setitem(SUITES, 'h5-w1a', actions)
    with pytest.raises(AssertionError, match='W1-A recording/replay'):
        check(yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding="utf-8")))


def test_w1a_named_golden_exceptions_match_scenario_support():
    import run
    from coverage_plan import W1A, EXCEPTIONS
    live_only = set()
    for name in W1A:
        if name in EXCEPTIONS['w1a']:
            continue
        scenario = run.SCENARIOS[name]()
        if scenario.live_only or (type(scenario).timeline is not run.Scenario.timeline and not scenario.golden_replay):
            live_only.add(name)
    assert set(EXCEPTIONS['w1a_golden']) == live_only


def test_health_db_mutant_uses_an_independent_readiness_witness(monkeypatch):
    import run
    def response(port, method, path, *args, **kwargs):
        return {'json': {'status': 'ok', 'embedder': {}}, 'status': 200}
    monkeypatch.setattr(run, 'call', response)
    assert not run.settled(1, None)
    assert run.settled(1, None, check_db=False)


def test_cli_replays_the_selected_temporary_golden_directory(tmp_path, monkeypatch):
    from rust.cli_harness import runner, core
    monkeypatch.setattr(runner, 'GOLDENS', runner.GOLDENS)
    monkeypatch.setattr(runner.producers, 'use_oracle', lambda *args: None)
    monkeypatch.setattr(runner.rows, 'load', lambda *args: [])
    observed = []
    monkeypatch.setattr(runner, 'run_row', lambda row, cases, oracle, candidate, golden, verbose:
                        observed.append(golden) or {})
    candidate = tmp_path / 'candidate'
    candidate.touch()
    (tmp_path / f'mail.{core.PLATFORM}.json').write_text('{"fixture": true}')
    assert runner.main(['--row', 'mail', '--golden', '--golden-dir', str(tmp_path),
                        '--candidate', str(candidate)]) == 0
    assert observed == [{'fixture': True}]


def test_daemon_replays_the_selected_temporary_golden_directory(tmp_path, monkeypatch):
    import run
    monkeypatch.setattr(run, 'GOLDENS', run.GOLDENS)
    monkeypatch.setattr(run.daemons, 'scratch_root', lambda: tmp_path)
    observed = []
    def scenario(scn, binary, root, mode, record):
        observed.append(run.GOLDENS)
        return {'scenario': scn.name, 'cases': [], 'db_diffs': [], 'declared_db_writes': {}}
    monkeypatch.setattr(run, 'run_scenario', scenario)
    monkeypatch.setattr(sys, 'argv', ['run.py', 'golden', '--only', 'db-down', '--rust-bin', 'unused',
                                    '--golden-dir', str(tmp_path / 'goldens')])
    assert run.main() == 0
    assert observed == [tmp_path / 'goldens']


@pytest.mark.parametrize('job,step', [('parity-checks', 'Ported rows and daemon controls'),
                                     ('source-mutants', 'CLI source mutants')])
def test_inventory_rejects_masked_h5_exit(job, step):
    from inventory import check
    workflow = yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8'))
    selected = next(s for s in workflow['jobs'][job]['steps'] if s.get('name') == step)
    selected['run'] = selected['run'].replace('exit $LASTEXITCODE', 'exit 0')
    with pytest.raises(AssertionError, match='exit propagation'):
        check(workflow)


def test_inventory_includes_runtime_daemon_registrations():
    import run
    from inventory import daemon_scenarios
    assert daemon_scenarios() == set(run.SCENARIOS)


@pytest.mark.parametrize('mutation', ['unregistered', 'platform_removed'])
def test_inventory_rejects_runtime_source_control_drift(monkeypatch, mutation):
    from dataclasses import replace
    from inventory import check
    from rust.cli_harness import core, rows
    original = rows.mutants

    def changed(row):
        controls = original(row)
        if row == 'audit':
            if mutation == 'unregistered':
                controls = [m for m in controls if m.id != 'audit-body-reason-swap']
            else:
                controls = [replace(m, platforms=tuple(p for p in m.platforms if p != core.PLATFORM))
                            if m.id == 'audit-body-reason-swap' else m for m in controls]
        return controls

    monkeypatch.setattr(rows, 'mutants', changed)
    with pytest.raises(AssertionError, match='runtime source-control'):
        check(yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8')))


def test_inventory_rejects_empty_exception_reason(monkeypatch):
    from inventory import check
    from coverage_plan import EXCEPTIONS
    monkeypatch.setitem(EXCEPTIONS['cli_mutants']['linux'], 'connect-config-not-private', ' ')
    with pytest.raises(AssertionError, match='exception reason'):
        check(yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8')))


@pytest.mark.parametrize('mutation', ['unregistered', 'platform_removed'])
def test_dispatcher_rejects_runtime_source_control_drift(monkeypatch, tmp_path, mutation):
    from dataclasses import replace
    from rust.cli_harness import core, rows
    import run_coverage
    original = rows.mutants

    def changed(row):
        controls = original(row)
        if row == 'audit':
            if mutation == 'unregistered':
                return [m for m in controls if m.id != 'audit-body-reason-swap']
            return [replace(m, platforms=tuple(p for p in m.platforms if p != core.PLATFORM))
                    if m.id == 'audit-body-reason-swap' else m for m in controls]
        return controls

    monkeypatch.setattr(rows, 'mutants', changed)
    monkeypatch.setattr(sys, 'argv', ['run_coverage.py', '--source-shard', 'audit-0',
                                    '--out-dir', str(tmp_path)])
    with pytest.raises(AssertionError, match='runtime source-control'):
        run_coverage.main()


def test_inventory_rejects_control_excepted_on_every_os_without_global_reason(monkeypatch):
    from inventory import check
    from coverage_plan import EXCEPTIONS
    for platform in ('linux', 'windows'):
        monkeypatch.setitem(EXCEPTIONS['cli_mutants'][platform], 'audit-body-reason-swap', 'fixture exclusion')
    with pytest.raises(AssertionError, match='without any OS execution'):
        check(yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8')))


@pytest.mark.parametrize('scenario', ['session-reap', 'session-restart', 'session-resume-refusal',
                                     'session-sweep', 'session-tombstone-restart', 'sweep-recovery'])
def test_inventory_rejects_dropped_runtime_background_scenario(monkeypatch, scenario):
    from inventory import check
    from coverage_plan import SUITES
    monkeypatch.setitem(SUITES, 'h5-background', [a for a in SUITES['h5-background']
                                               if a['scenario'] != scenario])
    with pytest.raises(AssertionError, match='accepted daemon scenario'):
        check(yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8')))
