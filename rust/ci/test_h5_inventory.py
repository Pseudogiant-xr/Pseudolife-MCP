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
    configuration = yaml.safe_load((home / "data/config.yaml").read_text())
    assert created == [(home / "model", {"dimension": 1024})]
    assert configuration["memory"]["top_k"] == 3
    assert configuration["coordination"]["enabled"] is False
    assert configuration["embedding"]["backend"] == "onnx"
    assert environment["PSEUDOLIFE_DAEMON_ONNX_DIR"] == str(home / "model")


def test_ported_row_and_mutant_ci_inventory():
    from inventory import check
    check(yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text()))


def test_cli_mutant_summary_names_witnesses_and_durations(tmp_path, monkeypatch):
    from rust.cli_harness import mutants
    mutant = mutants.Mutant("fixture-control", "mail", "unused", "a", "b")
    monkeypatch.setattr(mutants.rows, "mutants", lambda row: [mutant] if row == "mail" else [])
    monkeypatch.setattr(mutants, "run_mutant", lambda *args: (True, ["fixture-witness"]))
    monkeypatch.setattr(mutants, "_sync_source", lambda *args: None)
    output = tmp_path / "mutants.json"
    assert mutants.main(["mail"], [], None, False, out=output) == 0
    result = json.loads(output.read_text())
    assert result["mutants"]["fixture-control"]["caught_by"] == ["fixture-witness"]
    assert result["mutants"]["fixture-control"]["seconds"] >= 0
    assert result["survivors"] == []


@pytest.mark.parametrize("suite", ["h5-audit", "h5-backup", "h5-w1a",
                                  "h5-daemon-mutants", "h5-graph-mutants"])
def test_inventory_rejects_a_dropped_suite(suite):
    from inventory import check
    workflow = yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text())
    workflow["jobs"]["parity-checks"]["strategy"]["matrix"]["suite"].remove(suite)
    with pytest.raises(AssertionError, match="missing H5 suite"):
        check(workflow)


def test_inventory_rejects_each_dropped_source_shard():
    from inventory import check
    from coverage_plan import SOURCE_SHARDS
    workflow = yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text())
    for shard in SOURCE_SHARDS:
        mutated = copy.deepcopy(workflow)
        mutated["jobs"]["source-mutants"]["strategy"]["matrix"]["shard"].remove(shard)
        with pytest.raises(AssertionError, match="source mutant shard dropped"):
            check(mutated)


def test_inventory_rejects_each_dropped_named_mutant(monkeypatch):
    import inventory
    from coverage_plan import DAEMON_CONTROLS, SOURCE_MUTANTS, SUITES
    workflow = yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text())
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


def test_linux_test_login_selects_its_bundled_icu(tmp_path):
    from run_coverage import source_environment
    installation = tmp_path / '.pg0/installation/18.1.0'
    (installation / 'bin').mkdir(parents=True)
    (installation / 'lib').mkdir()
    environment = source_environment('test_login', 'linux', tmp_path, {'LD_LIBRARY_PATH': 'existing'})
    assert environment['LD_LIBRARY_PATH'] == str(installation / 'lib') + ':existing'
    assert source_environment('test_login', 'windows', tmp_path, {}) == {}


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
