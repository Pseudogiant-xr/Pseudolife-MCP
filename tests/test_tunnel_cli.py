"""Guided setup is resumable and leaves installed client registrations alone."""
from dataclasses import replace
import json
import os

import pytest

from pseudolife_memory import tunnel_cli as cli
from pseudolife_memory.tunnel_profiles import Profile, ProfileStore, TunnelError, private_write


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / '.codex'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / '.claude'))
    for key in ('PSEUDOLIFE_MCP_TOKEN', 'PSEUDOLIFE_MCP_TOKEN_FILE', 'PSEUDOLIFE_MCP_DAEMON_URL', 'OPENAI_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    token = tmp_path / 'token'
    private_write(token, b'fixture-daemon-token')
    store = ProfileStore(tmp_path / 'tunnels')
    monkeypatch.setattr(cli, 'handshake', lambda url, token: {'ok': True, 'tool_count': 20})
    return store, token


def test_setup_resumes_without_erasing_key_consent_or_local_config(isolated, monkeypatch, capsys):
    store, token = isolated
    config = token.parent / '.claude' / '.claude.json'
    config.parent.mkdir()
    config.write_bytes(b'{"mcpServers":{"unrelated":{"command":"fixture"}}}')
    before = config.read_bytes()
    args = ['setup', '--profile-dir', str(store.root), '--daemon-url', 'http://127.0.0.1:8765',
            '--token-file', str(token), '--accept-access', '--tunnel-id', 'tunnel_0123']
    assert cli.main(args) == 0
    store.set_key('dot', 'fixture-tunnel-key')
    approved = replace(store.load('dot'), autostart_consent=True)
    store.save(approved)
    monkeypatch.setattr(cli, 'interactive', lambda: False)
    assert cli.main(['setup', '--profile-dir', str(store.root)]) == 0
    assert store.load('dot').autostart_consent
    assert store.read_key('dot') == 'fixture-tunnel-key'
    assert config.read_bytes() == before
    output = capsys.readouterr().out
    assert 'fixture-tunnel-key' not in output
    assert 'cloud' in output.lower()


def test_ambiguous_credentials_fail_before_any_profile_write(isolated, monkeypatch):
    store, token = isolated
    other = token.parent / 'other-token'
    private_write(other, b'other-fixture-token')
    monkeypatch.setattr(cli, 'registration_blocks', lambda env: iter([
        ('first', {'PSEUDOLIFE_MCP_TOKEN_FILE': str(token), 'PSEUDOLIFE_MCP_DAEMON_URL': 'http://127.0.0.1:8765'}),
        ('second', {'PSEUDOLIFE_MCP_TOKEN_FILE': str(other), 'PSEUDOLIFE_MCP_DAEMON_URL': 'http://127.0.0.1:8766'})]))
    assert cli.main(['setup', '--profile-dir', str(store.root), '--accept-access']) == 2
    assert store.list() == []


def test_status_is_readonly_and_never_promotes_runtime_ready_to_cloud(isolated, monkeypatch, capsys):
    store, token = isolated
    store.save(Profile('dot', 'http://127.0.0.1:8765', str(token), consent=True,
                       tunnel_id='tunnel_0123', state='ready'))
    store.set_key('dot', 'fixture-tunnel-key')
    before = store.profile_path('dot').read_bytes()
    monkeypatch.setattr(cli, 'runtime_status', lambda name, store: {'ready': True, 'running': True})
    assert cli.main(['status', '--profile-dir', str(store.root), '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['cloud']['verified'] is False
    assert result['runtime']['ready'] is True
    assert store.profile_path('dot').read_bytes() == before


def test_absent_update_does_not_create_profiles(isolated):
    store, token = isolated
    assert cli.main(['update', '--profile-dir', str(store.root)]) == 3
    assert not store.root.exists()


def test_full_catalog_requires_explicit_option_and_explains_scope(isolated, capsys):
    store, token = isolated
    assert cli.main(['setup', '--profile-dir', str(store.root), '--daemon-url', 'http://127.0.0.1:8765',
                     '--token-file', str(token), '--accept-access', '--catalog', 'full']) == 0
    assert store.load('dot').minimum_catalog == 'full'
    assert 'principal' in capsys.readouterr().out.lower()


def test_handshake_failure_redacts_transport_output_and_does_not_write(isolated, monkeypatch, capsys):
    store, token = isolated
    monkeypatch.setattr(cli, 'handshake', lambda *args: {'ok': False, 'error': 'SECRET TRANSPORT OUTPUT'})
    assert cli.main(['setup', '--profile-dir', str(store.root), '--daemon-url', 'http://127.0.0.1:8765',
                     '--token-file', str(token), '--accept-access']) == 2
    assert 'SECRET' not in str(capsys.readouterr())
    assert store.list() == []


def test_failed_key_renewal_preserves_profile_key_and_proof(isolated, monkeypatch, capsys):
    store, token = isolated
    profile = Profile('dot', 'http://127.0.0.1:8765', str(token), consent=True,
                      tunnel_id='tunnel_0123', state='ready')
    store.save(profile)
    store.set_key('dot', 'fixture-tunnel-key')
    before = store.profile_path('dot').read_bytes()
    candidate = token.parent / 'candidate'
    private_write(candidate, b'candidate-fixture-key')
    from pseudolife_memory import tunnel_runtime
    monkeypatch.setattr(tunnel_runtime, 'stable_command', lambda: ['fixture-shim'])
    monkeypatch.setattr(tunnel_runtime, 'verify_key', lambda *args: False)
    assert cli.main(['setup', '--profile-dir', str(store.root), '--key-file', str(candidate),
                     '--key-expires-at', '2026-12-01T00:00:00Z']) == 2
    assert store.profile_path('dot').read_bytes() == before
    assert store.read_key('dot') == 'fixture-tunnel-key'
    assert 'candidate-fixture-key' not in str(capsys.readouterr())


def _saved_tunnel(isolated, monkeypatch, *, running=True):
    """A ready saved profile under a patched default root; the in-process
    refresh fails the test: a refresh freezes the refreshing process's code."""
    store, token = isolated
    store.save(Profile('dot', 'http://127.0.0.1:8765', str(token), consent=True,
                       tunnel_id='tunnel_0123', state='ready'))
    store.set_key('dot', 'fixture-tunnel-key')
    from pseudolife_memory import tunnel_profiles, tunnel_runtime
    monkeypatch.setattr(tunnel_profiles, 'ProfileStore', lambda: store)
    monkeypatch.setattr(tunnel_profiles, 'default_root', lambda: store.root)
    monkeypatch.setattr(tunnel_runtime, 'status_profile', lambda name, store: {'running': running, 'ready': running})
    monkeypatch.setattr(tunnel_runtime, 'update_profile', lambda *a: pytest.fail('refreshed in this process'))
    return store


def _launcher(tmp_path, monkeypatch, installed=True):
    """The runtimes launcher (overridden paths), with a complete runtime."""
    import os
    windows = os.name == 'nt'
    launcher = tmp_path / 'bin' / ('pseudolife-mcp.exe' if windows else 'pseudolife-mcp')
    monkeypatch.setenv('PSEUDOLIFE_SHIM_RUNTIMES', str(tmp_path / 'runtimes'))
    monkeypatch.setenv('PSEUDOLIFE_SHIM_LAUNCHER', str(launcher))
    if installed:
        scripts = tmp_path / 'runtimes' / '000001' / ('Scripts' if windows else 'bin')
        scripts.mkdir(parents=True)
        (scripts.parent / 'runtime.json').write_text('{"version": "0.15.0"}', encoding='utf-8')
        (scripts / launcher.name).write_text('console', encoding='utf-8')
        launcher.parent.mkdir()
        launcher.write_text('launcher', encoding='utf-8')
    return launcher


def _cli_calls(monkeypatch, code=0, output=None):
    from pseudolife_memory import client_updates
    calls = []
    if output is None:
        output = json.dumps({'state': 'current', 'needs_attention': False, 'detail': '1 saved tunnel profiles checked',
                             'profiles': [{'profile': 'dot', 'state': 'refreshed', 'needs_attention': False}]})
    def run(argv, **kwargs):
        calls.append(([str(part) for part in argv], kwargs))
        return code, output
    monkeypatch.setattr(client_updates, 'run_cli', run)
    return calls


def test_client_update_refreshes_running_tunnels_through_the_new_launcher_after_the_shim(isolated, tmp_path, monkeypatch):
    # This test runs from a checkout: the refresh still never runs in it.
    store = _saved_tunnel(isolated, monkeypatch)
    before = store.profile_path('dot').read_bytes()
    launcher = _launcher(tmp_path, monkeypatch)
    from pseudolife_memory import client_updates
    order = []
    monkeypatch.setattr(client_updates, 'update_shim', lambda source, repo: order.append('shim') or {'state': 'installed:0.15.1', 'detail': 'new runtime'})
    calls = _cli_calls(monkeypatch)
    real = client_updates.run_cli
    monkeypatch.setattr(client_updates, 'run_cli', lambda argv, **kw: order.append('tunnel') or real(argv, **kw))
    report = client_updates.run_steps(('shim',), repo=None, source='fixture')
    assert order == ['shim', 'tunnel']
    assert calls[0][0] == [str(launcher), 'tunnel', 'update'] and calls[0][1]['timeout'] >= 75
    assert report['tunnel']['state'] == 'current' and report['tunnel']['profiles'][0]['state'] == 'refreshed'
    assert report['ok'] is True
    assert store.profile_path('dot').read_bytes() == before


def test_the_launcher_refresh_runs_without_python_import_overrides(isolated, tmp_path, monkeypatch):
    # An exported checkout path would otherwise be frozen into the bridge.
    _saved_tunnel(isolated, monkeypatch)
    _launcher(tmp_path, monkeypatch)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE', 'PYTHONSTARTUP', 'PYTHONSAFEPATH'):
        monkeypatch.setenv(key, str(tmp_path / 'checkout'))
    from pseudolife_memory import client_updates
    calls = _cli_calls(monkeypatch)
    client_updates.run_steps((), repo=None, source='fixture')
    env = calls[0][1]['env']
    assert not [key for key in env if key.upper().startswith('PYTHON')]
    assert env.get('PATH') == os.environ.get('PATH')


def test_a_launcher_refresh_that_times_out_says_so(isolated, tmp_path, monkeypatch):
    import subprocess
    _saved_tunnel(isolated, monkeypatch)
    launcher = _launcher(tmp_path, monkeypatch)
    from pseudolife_memory import client_updates
    def hung(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
    monkeypatch.setattr(client_updates.subprocess, 'run', hung)
    report = client_updates.run_steps((), repo=None, source='fixture')
    detail = report['tunnel']['detail']
    assert report['tunnel']['state'] == 'failed' and report['ok'] is False
    assert 'timed out after 150 s' in detail and f'"{launcher}" tunnel update' in detail and 'exit' not in detail


@pytest.mark.parametrize('checkout', [False, True])
def test_client_update_without_an_installed_launcher_never_refreshes_in_process(isolated, tmp_path, monkeypatch, checkout):
    _saved_tunnel(isolated, monkeypatch)
    _launcher(tmp_path, monkeypatch, installed=False)
    from pseudolife_memory import client_updates
    monkeypatch.setattr(client_updates, 'checkout_root', lambda: tmp_path / 'checkout' if checkout else None)
    calls = _cli_calls(monkeypatch)
    report = client_updates.run_steps((), repo=None, source='fixture')
    assert calls == []
    assert report['tunnel']['state'] == 'skipped'
    assert 'pseudolife-mcp tunnel update' in report['tunnel']['detail']
    assert report['ok'] is True


def test_a_failed_shim_step_leaves_running_tunnels_on_their_release(isolated, tmp_path, monkeypatch):
    _saved_tunnel(isolated, monkeypatch)
    launcher = _launcher(tmp_path, monkeypatch)
    from pseudolife_memory import client_updates
    monkeypatch.setattr(client_updates, 'update_shim', lambda source, repo: {'state': 'failed', 'detail': 'pip failed'})
    calls = _cli_calls(monkeypatch)
    report = client_updates.run_steps(('shim',), repo=None, source='fixture')
    assert calls == []
    assert report['tunnel']['state'] == 'skipped'
    assert 'shim step failed' in report['tunnel']['detail'] and f'{launcher}' in report['tunnel']['detail']


def test_idle_saved_tunnels_need_no_refresh(isolated, tmp_path, monkeypatch):
    _saved_tunnel(isolated, monkeypatch, running=False)
    _launcher(tmp_path, monkeypatch)
    from pseudolife_memory import client_updates
    calls = _cli_calls(monkeypatch)
    report = client_updates.run_steps((), repo=None, source='fixture')
    assert calls == []
    assert report['tunnel']['state'] == 'current' and report['ok'] is True


@pytest.mark.parametrize('code, output', [
    (2, 'SECRET private launcher output\n'),
    (1, 'SECRET traceback text'),
    (1, json.dumps({'state': 'failed', 'needs_attention': True, 'detail': '1 saved tunnel profiles checked',
                    'profiles': [{'profile': 'dot', 'state': 'rolled-back', 'needs_attention': True}]})),
])
def test_a_failed_launcher_refresh_fails_the_step_without_echoing_output(isolated, tmp_path, monkeypatch, code, output):
    _saved_tunnel(isolated, monkeypatch)
    launcher = _launcher(tmp_path, monkeypatch)
    from pseudolife_memory import client_updates
    _cli_calls(monkeypatch, code, output)
    report = client_updates.run_steps((), repo=None, source='fixture')
    assert report['tunnel']['state'] == 'failed' and report['ok'] is False
    assert 'SECRET' not in json.dumps(report)
    if output.startswith('{'):
        assert report['tunnel']['profiles'][0]['state'] == 'rolled-back'
    else:
        assert f'exit {code}' in report['tunnel']['detail'] and str(launcher) in report['tunnel']['detail']


def test_update_transport_failure_is_sanitized(isolated, monkeypatch):
    store, token = isolated
    store.save(Profile('dot', 'http://127.0.0.1:8765', str(token), consent=True,
                       tunnel_id='tunnel_0123', state='ready'))
    store.set_key('dot', 'fixture-tunnel-key')
    from pseudolife_memory import tunnel_runtime
    monkeypatch.setattr(tunnel_runtime, 'stable_command', lambda: ['fixture-shim'])
    def failure(*args):
        raise RuntimeError('SECRET TRANSPORT OUTPUT')
    monkeypatch.setattr(tunnel_runtime, 'update_profile', failure)
    report = cli.update_existing_profiles(store)
    assert report['state'] == 'failed'
    assert 'SECRET' not in json.dumps(report)


def test_combined_doctor_tunnel_report_is_readonly_and_absent_is_unchanged(isolated, monkeypatch):
    store, token = isolated
    assert cli.saved_tunnel_diagnostics(store) == []
    assert not store.root.exists()
    store.save(Profile('dot', 'http://127.0.0.1:8765', str(token), consent=True,
                       tunnel_id='tunnel_0123', state='ready'))
    store.set_key('dot', 'fixture-tunnel-key')
    before = store.profile_path('dot').read_bytes()
    monkeypatch.setattr(cli, 'runtime_status', lambda *args: {'running': True, 'ready': True})
    report = cli.saved_tunnel_diagnostics(store)
    assert report[0]['cloud']['verified'] is False
    assert report[0]['runtime']['ready'] is True
    assert report[0]['key_expiry']['known'] is False
    assert store.profile_path('dot').read_bytes() == before
    assert 'fixture-tunnel-key' not in json.dumps(report)


def _redirected_home(tmp_path, monkeypatch):
    from pathlib import Path
    from pseudolife_memory import credentials
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'home')
    def redirected(path):
        raise credentials.CredentialError('credential path must not contain redirects')
    monkeypatch.setattr(credentials, '_reject_ancestor_redirects', redirected)


def test_redirected_home_without_tunnel_profiles_leaves_doctor_and_update_alone(tmp_path, monkeypatch):
    from pseudolife_memory import client_updates
    _redirected_home(tmp_path, monkeypatch)
    assert cli.saved_tunnel_diagnostics() == []
    report = client_updates.run_steps((), repo=None, source='fixture')
    assert 'tunnel' not in report
    assert report['ok'] is True


@pytest.mark.parametrize('present', ['directory', 'dangling-link'])
def test_redirected_tunnel_directory_is_reported_not_raised(tmp_path, monkeypatch, present):
    from pseudolife_memory import client_updates
    root = tmp_path / 'home' / '.pseudolife-mcp' / 'tunnel'
    root.parent.mkdir(parents=True)
    if present == 'directory':
        root.mkdir()
    else:
        try:
            root.symlink_to(tmp_path / 'missing', target_is_directory=True)
        except OSError:
            pytest.skip('this host does not permit disposable symlinks')
    _redirected_home(tmp_path, monkeypatch)
    assert cli.saved_tunnel_diagnostics()[0]['state'] == 'unavailable'
    report = client_updates.run_steps((), repo=None, source='fixture')
    assert report['tunnel']['state'] == 'failed'
    assert report['ok'] is False


@pytest.mark.parametrize('state', ['rolled-back', 'rollback-incomplete', 'failed'])
def test_failed_refresh_needs_attention_in_update_and_status(isolated, monkeypatch, capsys, state):
    store, token = isolated
    profile = Profile('dot', 'http://127.0.0.1:8765', str(token), consent=True,
                      tunnel_id='tunnel_0123', state='ready')
    store.save(profile)
    store.set_key('dot', 'fixture-tunnel-key')
    before = store.profile_path('dot').read_bytes()
    from pseudolife_memory import tunnel_runtime
    monkeypatch.setattr(tunnel_runtime, 'stable_command', lambda: ['fixture-shim'])
    result = {'state': state, 'changed': False, 'running': state == 'rolled-back',
              'rolled_back': state == 'rolled-back'}
    monkeypatch.setattr(tunnel_runtime, 'update_profile', lambda *args: result)
    report = cli.update_existing_profiles(store)
    assert report['state'] == 'failed'
    assert report['needs_attention'] is True
    private_write(store.root / 'dot.reload.result.json', json.dumps({'id': '1' * 32, **result}).encode())
    monkeypatch.setattr(cli, 'runtime_status', lambda *args: {'ready': state == 'rolled-back', 'running': result['running']})
    assert cli.main(['status', '--profile-dir', str(store.root), '--json']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['update']['state'] == state
    assert status['update']['needs_attention'] is True
    assert cli.saved_tunnel_diagnostics(store)[0]['update']['needs_attention'] is True
    assert store.profile_path('dot').read_bytes() == before
    assert store.read_key('dot') == 'fixture-tunnel-key'


@pytest.mark.parametrize('expiry, expired', [('2099-12-01', False),
                                            ('2099-12-01T02:00:00+02:00', False),
                                            ('2000-01-01', True), (None, None)])
def test_accepted_expiry_is_safe_in_status_and_combined_doctor(isolated, monkeypatch, capsys, expiry, expired):
    store, token = isolated
    profile = Profile('dot', 'http://127.0.0.1:8765', str(token), consent=True,
                      tunnel_id='tunnel_0123', state='ready', runtime_key_expires_at=expiry)
    store.save(profile)
    monkeypatch.setattr(cli, 'runtime_status', lambda *args: {'running': False, 'ready': False})
    assert cli.main(['status', '--profile-dir', str(store.root), '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['key_expiry']['expired'] is expired
    assert report['key_expiry']['known'] is (expiry is not None)
    assert cli.saved_tunnel_diagnostics(store)[0]['key_expiry'] == report['key_expiry']


def test_naive_expiry_is_rejected_at_setup_with_clear_validation(isolated, capsys):
    store, token = isolated
    assert cli.main(['setup', '--profile-dir', str(store.root), '--daemon-url', 'http://127.0.0.1:8765',
                     '--token-file', str(token), '--accept-access', '--key-expires-at', '2099-12-01T02:00:00']) == 2
    assert 'timezone' in capsys.readouterr().err
    assert store.list() == []


@pytest.mark.parametrize('explicit', ['--accept-autostart', '--persistent'])
def test_documented_autostart_consent_installs_without_redundant_flag(isolated, monkeypatch, explicit):
    store, token = isolated
    profile = Profile('dot', 'http://127.0.0.1:8765', str(token), consent=True,
                      tunnel_id='tunnel_0123', state='ready')
    store.save(profile)
    from pseudolife_memory import tunnel_runtime, tunnel_service
    monkeypatch.setattr(tunnel_runtime, 'stable_command', lambda: ['fixture-shim'])
    called = []
    def install(profile, store, command, **kwargs):
        if not kwargs.get('persistent') or not profile.autostart_consent:
            raise TunnelError('persistent tunnel access requires explicit opt-in')
        called.append(kwargs)
        return {'installed': True}
    monkeypatch.setattr(tunnel_service, 'install_service', install)
    assert cli.main(['service', 'install', '--profile-dir', str(store.root), explicit]) == 0
    assert called[0]['persistent'] is True
    assert store.load('dot').autostart_consent is True


def test_saved_autostart_consent_alone_does_not_grant_new_persistence(isolated, monkeypatch):
    store, token = isolated
    profile = Profile('dot', 'http://127.0.0.1:8765', str(token), consent=True,
                      tunnel_id='tunnel_0123', state='ready', autostart_consent=True)
    store.save(profile)
    before = store.profile_path('dot').read_bytes()
    from pseudolife_memory import tunnel_runtime, tunnel_service
    monkeypatch.setattr(tunnel_runtime, 'stable_command', lambda: ['fixture-shim'])
    called = []
    monkeypatch.setattr(tunnel_service, 'install_service', lambda *a, **kw: called.append(kw) or {'installed': True})
    assert cli.main(['service', 'install', '--profile-dir', str(store.root)]) == 2
    assert called == []
    assert store.profile_path('dot').read_bytes() == before
