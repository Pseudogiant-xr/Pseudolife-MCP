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
    (home / "data/config.yaml").write_text(scenario.config_yaml, encoding="utf-8")
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


def test_backup_zero_retention_selection_is_platform_scoped(monkeypatch):
    from rust.cli_harness import core, runner
    from rust.cli_harness.rows import backup
    monkeypatch.setattr(backup, '_have_pg0', lambda: False)
    loaded = backup.cases()
    grammar = next(case for case in loaded if case.id == 'argparse-keep-zero')
    assert grammar.argv == ['backup', '--keep-days=0', '--help']
    assert grammar.platforms == ('windows', 'linux')
    for platform in ('windows', 'linux'):
        monkeypatch.setattr(core, 'PLATFORM', platform)
        for mode in ('live', 'record', 'golden'):
            selected = {case.id for case in runner._select(loaded, [], replay=mode != 'live')}
            assert ('file-mode-keep-zero' in selected) == (platform == 'linux')
            assert 'argparse-keep-zero' in selected


@pytest.mark.parametrize('field,value', [('platform', 'linux'), ('mode', 'golden'),
                                        ('row', 'audit'), ('case', 'file-mode')])
def test_backup_zero_exception_rejects_broader_or_partial_scope(monkeypatch, field, value):
    from coverage_plan import EXCEPTIONS
    from inventory import backup_zero_retention_scope
    scoped = copy.deepcopy(EXCEPTIONS['cli_case_selections'])
    scoped['windows-backup-zero-retention-clock-race'][field] = value
    monkeypatch.setitem(EXCEPTIONS, 'cli_case_selections', scoped)
    with pytest.raises(AssertionError, match='backup zero exception scope'):
        backup_zero_retention_scope(EXCEPTIONS)


@pytest.mark.parametrize('event', ['push', 'schedule'])
def test_inventory_rejects_missing_source_event(event):
    from inventory import check
    workflow = yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8'))
    workflow[True].pop(event, None)
    with pytest.raises(AssertionError, match='source events'):
        check(workflow)


def test_inventory_rejects_aggregate_accepting_any_skipped_job():
    import os
    import subprocess
    from inventory import check
    workflow = yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8'))
    gate = workflow['jobs']['parity']['steps'][0]
    gate['run'] = """
$results = ConvertFrom-Json $env:GATE_RESULTS -AsHashtable
foreach ($job in @('rust', 'candidate', 'parity-checks', 'source-mutants')) {
  if ($results[$job].result -cne 'success' -and $results[$job].result -cne 'skipped') {
    exit 1
  }
}
"""
    needs = {job: {'result': 'success'} for job in workflow['jobs']['parity']['needs']}
    needs['rust']['result'] = 'skipped'
    executed = subprocess.run(['pwsh', '-NoProfile', '-Command', gate['run']],
                              env=dict(os.environ, GATE_RESULTS=json.dumps(needs), GATE_EVENT='push'),
                              capture_output=True, text=True)
    assert executed.returncode == 0, executed.stderr
    with pytest.raises(AssertionError, match='Parity event gate'):
        check(workflow)


@pytest.mark.parametrize('event', ['schedule', 'workflow_dispatch'])
def test_inventory_rejects_source_filter_dropping_event(event):
    from inventory import check
    workflow = yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8'))
    source = workflow['jobs']['source-mutants']
    dropped = (f"github.event_name == '{event}' || " if event == 'schedule'
               else " || (github.event_name == 'workflow_dispatch' && inputs.mode == 'ci')")
    assert dropped in source['if']
    source['if'] = source['if'].replace(dropped, '')
    with pytest.raises(AssertionError, match='source event selection'):
        check(workflow)


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
    from parity_scope import resolve
    workflow = resolve(yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8")))
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


def test_h5_artifacts_do_not_walk_deliberate_scratch_symlink_loops():
    workflow = yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8'))
    upload = next(s for s in workflow['jobs']['parity-checks']['steps']
                  if s.get('name') == 'Retain H5 row and control outcomes')
    assert upload['with']['path'].splitlines() == ['${{ runner.temp }}/h5/*.json',
                                                 '${{ runner.temp }}/h5/goldens/*.json']


@pytest.mark.parametrize('name', ['session-reap', 'session-restart', 'session-resume-refusal',
                                 'session-sweep', 'session-tombstone-restart'])
def test_background_seed_uses_fixture_with_empty_model_cache(tmp_path, monkeypatch, name):
    import run
    import background_sessions
    cache = tmp_path / 'empty-cache'
    cache.mkdir()
    monkeypatch.setenv('HF_HOME', str(cache))
    monkeypatch.setattr(background_sessions.daemons, 'scratch_root', lambda: tmp_path)
    monkeypatch.setattr(background_sessions.dbstate, 'dump', lambda dsn: {})

    def prepare(scenario, home):
        path = home / 'data/config.yaml'
        config = yaml.safe_load(path.read_text(encoding='utf-8'))
        config['embedding'] = {'model_name': str(home / 'model'), 'backend': 'onnx', 'device': 'cpu'}
        path.write_text(yaml.safe_dump(config), encoding='utf-8')
        return {'TOKENIZERS_PARALLELISM': 'false'}

    monkeypatch.setattr(run.Scenario, 'prepare_home', prepare)

    def seed(argv, **kwargs):
        home = Path(kwargs['env']['PSEUDOLIFE_MCP_DATA_DIR']).parent
        config = yaml.safe_load((home / 'data/config.yaml').read_text(encoding='utf-8'))
        assert config.get('embedding', {}).get('model_name') == str(home / 'model')
        assert kwargs['env']['TOKENIZERS_PARALLELISM'] == 'false'
        assert kwargs['env']['HF_HUB_OFFLINE'] == '1' and list(cache.iterdir()) == []
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(background_sessions.subprocess, 'run', seed)
    scenario = run.SCENARIOS[name]()
    scenario.ci_fixture = True
    scenario.prepare_template('fixture-dsn')


def test_episode_daemon_uses_fixture_with_empty_model_cache(tmp_path, monkeypatch):
    from rust.cli_harness.rows import _daemon
    from rust.daemon.harness import embedding_fixture
    cache = tmp_path / 'empty-cache'
    cache.mkdir()
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setenv('HF_HOME', str(cache))
    monkeypatch.setattr(_daemon, 'CI_FIXTURE', True, raising=False)
    monkeypatch.setattr(_daemon.tempfile, 'mkdtemp', lambda **kwargs: str(data))
    monkeypatch.setattr(_daemon._bank, 'create', lambda *args, **kwargs: None)
    monkeypatch.setattr(_daemon._bank, 'drop', lambda *args: None)
    monkeypatch.setattr(_daemon._bank, 'url', lambda bank: 'postgresql://fixture@127.0.0.1:5434/pl_cf_fixture')
    monkeypatch.setattr(_daemon._bank.NAMES, 'require_process', lambda: None)
    monkeypatch.setattr(_daemon.RealDaemon, '_await_health', lambda self: None)
    monkeypatch.setattr(_daemon.RealDaemon, '_await_warmup', lambda self: None)
    monkeypatch.setitem(_daemon.ORACLE, 'python', sys.executable)
    monkeypatch.setitem(_daemon.ORACLE, 'source', ROOT)
    created = []
    monkeypatch.setattr(embedding_fixture, 'create', lambda path, **kw: created.append((path, kw)))

    def start(argv, **kwargs):
        path = data / 'config.yaml'
        config = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {}
        assert config.get('embedding', {}).get('model_name') == str(data / 'model')
        assert config['embedding']['backend'] == 'onnx' and config['embedding']['device'] == 'cpu'
        assert kwargs['env']['HF_HUB_OFFLINE'] == '1' and list(cache.iterdir()) == []
        assert created == [(data / 'model', {'dimension': 1024})]
        return SimpleNamespace(poll=lambda: 0)

    monkeypatch.setattr(_daemon.subprocess, 'Popen', start)
    daemon = _daemon.RealDaemon('pl_cf_fixture')
    daemon.shutdown()


@pytest.mark.parametrize('platform', ['linux', 'windows'])
def test_settled_encodings_records_and_replays_on_both_platforms(monkeypatch, tmp_path, platform):
    import run_coverage
    from coverage_plan import DAEMON_CONTROLS
    invoked = []
    environment = run_coverage.os.environ
    monkeypatch.setattr(run_coverage, 'os', SimpleNamespace(name='nt' if platform == 'windows' else 'posix',
                                                          environ=environment))
    monkeypatch.setattr(sys, 'argv', ['run_coverage.py', '--suite', 'h5-w1a', '--out-dir', str(tmp_path)])
    monkeypatch.setattr(run_coverage.subprocess, 'run', lambda argv, **kw: invoked.append(argv) or SimpleNamespace(returncode=0))
    assert run_coverage.main() == 0
    encodings = [a for a in invoked if a[a.index('--only') + 1] == 'encodings']
    assert [a[2] for a in encodings] == ['live', 'golden']
    assert '--record' in encodings[0]
    assert DAEMON_CONTROLS['auth-candidate-order'] == ['encodings']
    invoked.clear()
    monkeypatch.setattr(sys, 'argv', ['run_coverage.py', '--suite', 'h5-daemon-mutants',
                                    '--out-dir', str(tmp_path)])
    assert run_coverage.main() == 0
    auth = [a for a in invoked if a[a.index('--mutants') + 1] == 'auth-candidate-order']
    assert len(auth) == 1 and auth[0][auth[0].index('--only') + 1] == 'encodings'


@pytest.mark.parametrize('field, value', [('platform', 'linux'), ('mode', 'live'),
                                         ('scenario', 'map-order-reversed')])
def test_inventory_rejects_broadened_map_order_exception(monkeypatch, field, value):
    from coverage_plan import EXCEPTIONS
    from inventory import check
    entry = dict(platform='windows', mode='both', scenario='map-order', reason='observed bank initialization gap')
    entry[field] = value
    monkeypatch.setitem(EXCEPTIONS, 'w1a_selections', {'windows-map-order-unsettled-bank-init': entry})
    workflow = yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8'))
    with pytest.raises(AssertionError, match='retired unsettled-bank exception'):
        check(workflow)


@pytest.mark.parametrize('platform', ['linux', 'windows'])
def test_settled_map_order_records_and_replays_on_both_platforms(monkeypatch, tmp_path, platform):
    import run_coverage
    invoked = []
    monkeypatch.setattr(run_coverage, 'os', SimpleNamespace(name='nt' if platform == 'windows' else 'posix',
                                                          environ=run_coverage.os.environ))
    monkeypatch.setattr(sys, 'argv', ['run_coverage.py', '--suite', 'h5-w1a', '--out-dir', str(tmp_path)])
    monkeypatch.setattr(run_coverage.subprocess, 'run', lambda argv, **kw: invoked.append(argv) or SimpleNamespace(returncode=0))
    assert run_coverage.main() == 0
    selected = [(argv[2], argv[argv.index('--only') + 1]) for argv in invoked]
    assert ('live', 'map-order') in selected
    assert ('golden', 'map-order') in selected
    assert ('live', 'map-order-reversed') in selected and ('golden', 'map-order-reversed') in selected


@pytest.mark.parametrize('platform', [None, 'linux', 'windows'])
@pytest.mark.parametrize('scenario', ['encodings', 'map-order', 'map-order-reversed'])
def test_inventory_rejects_settled_auth_omissions(monkeypatch, platform, scenario):
    from coverage_plan import EXCEPTIONS, SUITES
    from inventory import check
    if platform is None:
        monkeypatch.setitem(EXCEPTIONS['w1a'], scenario, 'broad initialization exception')
        monkeypatch.setitem(SUITES, 'h5-w1a', [a for a in SUITES['h5-w1a'] if a['scenario'] != scenario])
    else:
        monkeypatch.setitem(EXCEPTIONS['w1a_platform'][platform], scenario, 'broad platform initialization exception')
    workflow = yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8'))
    with pytest.raises(AssertionError, match='protected settled-auth selections'):
        check(workflow)


@pytest.mark.parametrize('job, name', [('parity-checks', 'Ported rows and daemon controls'),
                                      ('source-mutants', 'CLI source mutants')])
@pytest.mark.parametrize('suffix', ['; exit 0', '; python -c "raise SystemExit(0)"'])
def test_inventory_rejects_failure_mask_on_invocation_line(job, name, suffix):
    from inventory import check
    workflow = yaml.safe_load((ROOT / '.github/workflows/rust.yml').read_text(encoding='utf-8'))
    step = next(s for s in workflow['jobs'][job]['steps'] if s.get('name') == name)
    lines = step['run'].splitlines()
    lines[0] += suffix
    step['run'] = '\n'.join(lines)
    with pytest.raises(AssertionError, match='driver command'):
        check(workflow)


@pytest.mark.parametrize('keep_going', [True, False])
def test_a_failed_selection_is_named_at_exit(monkeypatch, tmp_path, capsys, keep_going):
    """Run 38699015278 ended h5-w1a with exit 1 and no reason: one diff
    four minutes up the log, then twelve clean selections under
    --keep-going. The runner names every failed selection as it exits."""
    import run_coverage
    monkeypatch.setattr(sys, 'argv', ['run_coverage.py', '--suite', 'h5-w1a', '--out-dir', str(tmp_path)]
                        + (['--keep-going'] if keep_going else []))

    def run(argv, **kw):
        failed = argv[argv.index('--only') + 1] == 'dim-mismatch' and argv[2] == 'golden'
        return SimpleNamespace(returncode=1 if failed else 0)

    monkeypatch.setattr(run_coverage.subprocess, 'run', run)
    assert run_coverage.main() == 1
    out = capsys.readouterr().out.splitlines()
    assert 'h5-w1a: FAILED dim-mismatch golden (exit 1)' in out
    assert out[-1].startswith('h5-w1a: 1 of ') and out[-1].endswith(' selections failed; see h5-w1a-timings.json')
