"""Fixture failures must fail the MCP runner without sharing or abandoning homes."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import mcp_run


@pytest.fixture
def runner(tmp_path, monkeypatch):
    homes, stopped, dropped, binaries = [], [], [], []
    fault = {"stop": False, "drop": False, "start": False}

    class Daemon:
        def __init__(self, side, home):
            self.side = side
            homes.append(home)

        def start(self, **kwargs):
            if fault["start"] and self.side == "rs":
                raise RuntimeError("startup failed after launching")
            return self

        def stop(self):
            stopped.append(self.side)
            if fault["stop"] and self.side == "rs":
                raise RuntimeError("shutdown failed")

    def rust(binary, home, *args):
        binaries.append(binary)
        return Daemon("rs", home)

    def drop(name):
        dropped.append(name)
        if fault["drop"]:
            raise RuntimeError("drop failed")

    def compare(command):
        Path(command[5]).write_text(json.dumps({"summary": {"responses": 10, "cases": 2,
            "diffs": 0, "unserved_checked": []}, "diffs": []}), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mcp_run.daemons, "scratch_root", lambda: tmp_path)
    monkeypatch.setattr(mcp_run.daemons, "python_daemon", lambda home, *args: Daemon("py", home))
    monkeypatch.setattr(mcp_run.daemons, "rust_daemon", rust)
    monkeypatch.setattr(mcp_run.pg, "name", lambda name: name)
    monkeypatch.setattr(mcp_run.pg, "create", lambda name: "postgresql://unused/fixture")
    monkeypatch.setattr(mcp_run.pg, "drop", drop)
    monkeypatch.setattr(mcp_run.subprocess, "run", compare)
    monkeypatch.setattr(sys, "argv", ["mcp_run.py", "relative-daemon", str(tmp_path / "out"), "--config2"])
    return SimpleNamespace(homes=homes, stopped=stopped, dropped=dropped, binaries=binaries, fault=fault)


def test_invocations_keep_distinct_homes(runner):
    assert mcp_run.main() == 0
    marker = runner.homes[0] / "owned"
    marker.write_text("first invocation", encoding="utf-8")
    assert mcp_run.main() == 0
    assert marker.exists()
    assert runner.homes[0] != runner.homes[2]


def test_relative_binary_is_resolved_before_daemon_cwd_changes(runner):
    assert mcp_run.main() == 0
    assert runner.binaries[0].is_absolute()


@pytest.mark.parametrize("failure", ["stop", "drop", "start"])
def test_infrastructure_failure_is_distinct_and_cleanup_continues(runner, failure):
    runner.fault[failure] = True
    try:
        code = mcp_run.main()
    except RuntimeError:
        code = 1
    assert code == 2
    assert set(runner.stopped) == {"py", "rs"}
    assert len(runner.dropped) == 2


def test_failed_comparison_cannot_reuse_an_old_report(runner, monkeypatch):
    output = Path(sys.argv[2])
    output.mkdir()
    (output / "compare.json").write_text(json.dumps({"summary": {"responses": 10, "cases": 2,
        "diffs": 1, "unserved_checked": []}, "diffs": ["config_lists[0]: old difference"]}), encoding="utf-8")
    monkeypatch.setattr(mcp_run.subprocess, "run", lambda command: SimpleNamespace(returncode=1))
    assert mcp_run.main() == 2


@pytest.mark.parametrize('failure', ['exception', 'missing', 'truncated', 'stub'])
def test_failed_or_incomplete_oracle_record_is_not_written(tmp_path, monkeypatch, capsys, failure):
    import mcp_compare
    monkeypatch.setattr(mcp_compare, 'CASES', mcp_compare.BASE_CASES)

    data = {case.__name__: [{'status': 200, 'headers': {}, 'body': {}}]
            * mcp_compare.CASE_RESPONSE_COUNTS[case.__name__] for case in mcp_compare.CASES}
    # The fixture isolates record completeness from oracle semantic checks.
    stubs = {name: {'status': 200, 'headers': {}, 'body': {}} for name in mcp_compare.UNSERVED}
    if failure == 'exception':
        def failed_case(port):
            raise TimeoutError('injected oracle timeout')
        data['stream_reconnect'] = mcp_compare.run_case(failed_case, 1)
    elif failure == 'missing':
        data.pop('stream_reconnect')
    elif failure == 'truncated':
        data['handshake'].pop()
    else:
        stubs = {'<exception>': {'exception': 'injected stub failure'}}
    monkeypatch.setattr(mcp_compare, 'run_all', lambda port: data)
    monkeypatch.setattr(mcp_compare, 'stub_tools', lambda port: stubs)
    monkeypatch.setattr(mcp_compare, 'sanity', lambda data, errors: None)
    target = tmp_path / 'record.json'
    monkeypatch.setattr(sys, 'argv', ['mcp_compare.py', 'record', '1', str(target)])
    assert mcp_compare.main() != 0
    assert not target.exists()
    output = capsys.readouterr()
    assert 'record' in output.err.lower()


def test_completed_sweep_difference_is_parity_failure(runner, monkeypatch, capsys):
    import time
    import mcp_validation_sweep as sweep
    from test_mcp_ci import baseline

    sys.argv.remove('--config2')
    sys.argv.append('--validation-sweep')
    monkeypatch.setattr(mcp_run, 'seed_stored', lambda dsn: None)
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)
    ports = iter([1, 2])
    monkeypatch.setattr(mcp_run.daemons, 'free_port', lambda: next(ports))
    def compared(command):
        report = baseline()
        report['summary']['unserved_checked'].sort()
        Path(command[5]).write_text(json.dumps(report), encoding='utf-8')
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(mcp_run.subprocess, 'run', compared)
    monkeypatch.setattr(sweep, 'EXPECTED_CASES', 1)
    monkeypatch.setattr(sweep, 'cases', lambda: iter([{'method': 'ping', 'params': {},
        'notification': False, 'field': 'fixture'}]))
    monkeypatch.setattr(sweep, 'observe', lambda port, *args, **kwargs: {'code': port})
    assert mcp_run.main() == 1
    assert 'parity failed' in capsys.readouterr().err
    assert set(runner.stopped) == {'py', 'rs'} and len(runner.dropped) == 2


def test_golden_distillation_rejects_failed_transport_case(tmp_path):
    import mcp_goldens
    response = {'body': {'sse': ['{"jsonrpc":"2.0","id":1,"result":{}}']}}
    record = {'lists': [response] * 18, 'protocol_versions': [response] * 7,
              'toolset': [response] * 53, 'calls': [response] * 4,
              'stream_reconnect': [{'exception': 'TimeoutError: injected transport failure'}]}
    source = tmp_path / 'record.json'
    source.write_text(json.dumps(record), encoding='utf-8')
    target = tmp_path / 'golden.json'
    target.write_text('previous golden', encoding='utf-8')
    with pytest.raises(ValueError, match='stream_reconnect'):
        mcp_goldens.main(source, target)
    assert target.read_text(encoding='utf-8') == 'previous golden'


@pytest.mark.parametrize('consumer', ['producer', 'distiller'])
def test_compensated_case_truncation_is_rejected(tmp_path, monkeypatch, consumer):
    import mcp_compare
    import mcp_goldens
    monkeypatch.setattr(mcp_compare, 'CASES', mcp_compare.BASE_CASES)

    counts = {'handshake': 4, 'lists': 18, 'protocol_versions': 10, 'methods': 24,
              'calls': 4, 'toolset': 53, 'stored': 30, 'list_changed_stream': 2,
              'stream_reconnect': 3, 'init_gate': 21, 'body_limit': 6,
              'messages': 13, 'concurrent': 8, 'transport': 21, 'paths': 7,
              'tool_binding': 71, 'bound_identity': 19}
    response = {'status': 200, 'headers': {},
                'body': {'sse': ['{"jsonrpc":"2.0","id":1,"result":{}}']}}
    record = {name: [response] * count for name, count in counts.items()}
    record['stub_tools'] = {name: response for name in mcp_compare.UNSERVED}
    monkeypatch.setattr(mcp_compare, 'sanity', lambda data, errors: None)
    assert not mcp_compare.recording_errors(record)
    record['protocol_versions'] = record['protocol_versions'][:6]
    record['handshake'] += [response] * 4
    assert sum(len(value) for value in record.values()) == 341
    target = tmp_path / 'output.json'
    target.write_text('previous golden', encoding='utf-8')
    if consumer == 'producer':
        monkeypatch.setattr(mcp_compare, 'run_all', lambda port: record)
        monkeypatch.setattr(mcp_compare, 'stub_tools', lambda port: record['stub_tools'])
        monkeypatch.setattr(sys, 'argv', ['mcp_compare.py', 'record', '1', str(target)])
        assert mcp_compare.main() == 2
    else:
        source = tmp_path / 'record.json'
        source.write_text(json.dumps(record), encoding='utf-8')
        with pytest.raises(ValueError, match='protocol_versions'):
            mcp_goldens.main(source, target)
    assert target.read_text(encoding='utf-8') == 'previous golden'
