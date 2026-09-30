"""Guided setup is resumable and leaves installed client registrations alone."""
from dataclasses import replace
import json

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


def test_global_client_update_checks_saved_tunnel_without_registration_mutation(isolated, monkeypatch):
    store, token = isolated
    store.save(Profile('dot', 'http://127.0.0.1:8765', str(token), consent=True,
                       tunnel_id='tunnel_0123', state='ready'))
    store.set_key('dot', 'fixture-tunnel-key')
    before = store.profile_path('dot').read_bytes()
    from pseudolife_memory import client_updates, tunnel_profiles, tunnel_runtime
    monkeypatch.setattr(tunnel_profiles, 'ProfileStore', lambda: store)
    monkeypatch.setattr(cli, 'ProfileStore', lambda: store)
    monkeypatch.setattr(tunnel_runtime, 'stable_command', lambda: ['fixture-shim'])
    observed = []
    monkeypatch.setattr(tunnel_runtime, 'update_profile', lambda *args: observed.append(args[0].name) or {'changed': False})
    report = client_updates.run_steps((), repo=None, source='fixture')
    assert report['ok'] and report['tunnel']['state'] == 'current'
    assert observed == ['dot']
    assert store.profile_path('dot').read_bytes() == before


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
