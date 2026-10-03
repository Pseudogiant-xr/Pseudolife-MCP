"""Disposable stdio ownership and CI collector/runner counterexamples."""
import json
import os
import sys
from unittest.mock import patch

import psutil
import pytest

from . import ci
from .common import ROOT, child_environment
from .transport import Stdio, TOKEN, shim_fixture


def toy_stdio(pid_file, linger=False, inherited=False):
    child = ("import os,pathlib,time; "
             f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)")
    return ("import json,pathlib,subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}],stdin=subprocess.DEVNULL,"
            + ("stdout=sys.stdout,stderr=sys.stderr); " if inherited else
               "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); ")
            + f"p=pathlib.Path({str(pid_file)!r}); "
            "\nwhile not p.exists(): time.sleep(.01)\n"
            "for line in sys.stdin:\n"
            " request=json.loads(line)\n"
            " if 'id' in request:\n"
            "  result={'protocolVersion':'2025-11-25'} if request['method']=='initialize' else {'tools':[]}\n"
            "  print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)\n"
            + ("time.sleep(30)\n" if linger else ""))


def stopped(pid):
    try:
        return not psutil.Process(pid).is_running() or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.parametrize('linger,inherited', [(False, False), (True, False), (False, True)])
def test_stdio_reclaims_ordinary_descendant_after_eof_or_timeout(tmp_path, linger, inherited):
    pid_file = tmp_path / 'child-pid'
    client = Stdio([sys.executable, '-c', toy_stdio(pid_file, linger, inherited)], os.environ.copy(), tmp_path)
    try:
        assert client.initialize()['protocolVersion'] == '2025-11-25'
        assert client.request('tools/list') == {'tools': []}
        actual_wait = client.process.wait
        # Exercise the existing normal/timeout close path without a 15s test wait.
        with patch.object(client.process, 'wait', side_effect=lambda timeout=None: actual_wait(timeout=.5)):
            cleanup = client.close()
        assert stopped(int(pid_file.read_text())), 'ordinary descendant survived cleanup receipt'
        assert cleanup['cleanup_confirmed']
        assert cleanup['subtree_stopped']
        assert cleanup['forced'] is linger
    finally:
        if not client.process.owned_cleanup['subtree_stopped']:
            client._ownership.__exit__(None, None, None)
        if pid_file.exists() and not stopped(int(pid_file.read_text())):
            child = psutil.Process(int(pid_file.read_text()))
            child.kill()
            child.wait(timeout=5)


def test_stdio_preserves_real_shim_fixture_protocol_and_normal_exit(tmp_path):
    env = child_environment(tmp_path)
    with shim_fixture() as (url, observed):
        env.update({'PSEUDOLIFE_MCP_DAEMON_URL': url, 'PSEUDOLIFE_MCP_TOKEN': TOKEN})
        client = Stdio([sys.executable, '-m', 'pseudolife_memory.cli'], env, ROOT)
        try:
            assert client.initialize()['protocolVersion'] == '2025-11-25'
            assert client.request('tools/list') == {'tools': []}
        finally:
            cleanup = client.close()
        assert observed['authorized'] and observed['initialize_requests'] >= 1
        assert cleanup['exit_code'] == 0 and not cleanup['forced']
        assert cleanup['process_stopped'] and cleanup['subtree_stopped'] and cleanup['cleanup_confirmed']


def ci_fixture(*arguments):
    if arguments[:2] == ('repo', 'view'):
        return {'nameWithOwner': 'fixture/project'}
    if arguments[:2] == ('run', 'list'):
        return [{'databaseId': 1}]
    if arguments[:2] == ('run', 'view'):
        return {'databaseId': 1, 'headSha': 'fixture-head', 'createdAt': '2026-10-03T00:00:00Z',
                'event': 'push', 'conclusion': 'success', 'url': 'https://example.invalid/run/1',
                'jobs': [{'databaseId': 42, 'name': 'linux', 'conclusion': 'success', 'steps': [],
                          'startedAt': '2026-10-03T00:00:01Z', 'completedAt': '2026-10-03T00:00:02Z'}]}
    if '/jobs' in arguments[-1]:
        return [{'jobs': [{'id': 42, 'labels': ['ubuntu-latest'], 'runner_name': 'private-host'}]}]
    return {'sha': 'workflow-blob'}


def test_ci_hardware_is_collector_only_and_runner_hardware_unknown(tmp_path):
    output = tmp_path / 'ci.json'
    with patch.object(ci, 'gh', side_effect=ci_fixture), patch.object(ci, 'provenance',
            side_effect=lambda label: {'host': {'label': label, 'os': 'Windows', 'ram_bytes': 123}}), patch.object(
            sys, 'argv', ['ci', '--count', '1', '--out', str(output)]):
        ci.main()
    capture = json.loads(output.read_text())
    assert capture['provenance']['host']['label'] == 'ci-collector'
    assert capture['provenance_scope'] == 'collector'
    assert capture['runs'][0]['jobs'][0]['runner']['labels'] == ['ubuntu-latest']
    assert capture['runs'][0]['jobs'][0]['runner']['hardware'] is None
    assert 'private-host' not in output.read_text()


def test_ci_redacts_custom_runner_labels_and_treats_api_failure_as_unknown():
    metadata = ci.runner_metadata({'labels': ['ubuntu-24.04', 'self-hosted', 'private-runner',
                                             'windows-latest-private', 99], 'runner_name': 'private-host'})
    assert metadata == {'labels': ['ubuntu-24.04', 'self-hosted'], 'hardware': None,
                        'metadata_source': 'Actions Jobs API'}
    import subprocess
    with patch.object(ci, 'gh', side_effect=subprocess.CalledProcessError(1, 'gh')):
        assert ci.job_runners('fixture/project', 1) == {}
    assert ci.runner_metadata() == {'labels': None, 'hardware': None, 'metadata_source': None}


def test_offline_ci_analysis_does_not_reassert_old_runner_host_label(tmp_path):
    original = {'provenance': {'host': {'label': 'github-hosted-runners', 'ram_bytes': 123}},
                'runs': [{'head_sha': 'old', 'job_span_s': 1, 'created_to_last_job_s': 2,
                          'jobs': [{'name': 'test', 'wall_s': 1}]}]}
    source = tmp_path / 'old.json'; output = tmp_path / 'new.json'
    source.write_text(json.dumps(original))
    with patch.object(ci, 'provenance', side_effect=lambda label: {'host': {'label': label}}), patch.object(
            sys, 'argv', ['ci', '--input', str(source), '--out', str(output)]):
        ci.main()
    capture = json.loads(output.read_text())
    assert capture['provenance']['host']['label'] == 'ci-collector'
    assert capture['provenance_scope'] == 'collector'
    assert capture['input_provenance'] == original['provenance']
    assert capture['runs'][0]['jobs'][0]['runner']['hardware'] is None
    assert json.loads(source.read_text()) == original
