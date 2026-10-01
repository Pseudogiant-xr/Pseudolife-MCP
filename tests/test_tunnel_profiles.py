from dataclasses import replace
import json
from pathlib import Path
import pytest
from pseudolife_memory.tunnel_profiles import Profile, ProfileStore, TunnelError, validate_url


def profile(tmp_path):
    return Profile(name='personal', daemon_url='http://127.0.0.1:8765', token_file=str(tmp_path / 'daemon.token'))


def test_manifest_resumes_without_secret(tmp_path):
    store = ProfileStore(tmp_path / 'profiles')
    initial = profile(tmp_path)
    store.save(initial)
    store.set_key(initial.name, 'synthetic-private-key')
    store.save(replace(initial, consent=True))
    assert store.load(initial.name).consent
    assert store.read_key(initial.name) == 'synthetic-private-key'
    assert 'synthetic-private-key' not in store.profile_path(initial.name).read_text()
    assert store.list() == ['personal']


@pytest.mark.parametrize('name', ['../escape', 'a/b', 'a\\b', '', '.', 'CON', 'a:stream', 'x\n'])
def test_profile_name_rejected(tmp_path, name):
    with pytest.raises(TunnelError):
        ProfileStore(tmp_path).save(replace(profile(tmp_path), name=name))


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:secret@example.com', 'https://example.com/?key=x', 'https://example.com/#key', 'http://127.0.0.1@evil.example.com', 'file:///tmp/x', 'https://example.com/\n'])
def test_unsafe_urls_rejected(url):
    with pytest.raises(TunnelError):
        validate_url(url, local=True)


def test_cloud_requires_https():
    with pytest.raises(TunnelError):
        validate_url('http://127.0.0.1', local=False)
    assert validate_url('https://example.com/mcp', local=False) == 'https://example.com/mcp'


def test_manifest_rejects_unknown_secret_fields(tmp_path):
    store = ProfileStore(tmp_path / 'profiles')
    store.save(profile(tmp_path))
    target = store.profile_path('personal')
    payload = json.loads(target.read_text())
    payload['private_key'] = 'synthetic-private-key'
    target.write_text(json.dumps(payload))
    with pytest.raises(TunnelError):
        store.load('personal')


def test_existing_daemon_credential_not_copied(tmp_path):
    store = ProfileStore(tmp_path / 'profiles')
    store.save(profile(tmp_path))
    assert not (store.root / 'daemon.token').exists()

def test_key_expiry_and_catalog_are_nonsecret_metadata(tmp_path):
    from datetime import datetime, timezone
    from pseudolife_memory.tunnel_profiles import key_expiry
    store=ProfileStore(tmp_path/'profiles')
    p=replace(profile(tmp_path),minimum_catalog='full',runtime_key_expires_at='2026-10-02')
    store.save(p)
    assert store.load(p.name).minimum_catalog == 'full'
    assert key_expiry(p,now=datetime(2026,10,1,tzinfo=timezone.utc)) == 'near-expiry'
    assert key_expiry(p,now=datetime(2026,10,3,tzinfo=timezone.utc)) == 'expired'
    assert key_expiry(profile(tmp_path)) == 'unknown'


def test_key_input_requires_private_file(tmp_path):
    from pseudolife_memory.tunnel_profiles import private_write
    store=ProfileStore(tmp_path/'profiles')
    source=tmp_path/'input.key'
    private_write(source,b'synthetic-private-key')
    store.import_key('personal',source)
    assert store.read_key('personal') == 'synthetic-private-key'
    assert source.read_bytes() == b'synthetic-private-key'


@pytest.mark.skipif(__import__('os').name == 'nt', reason='POSIX symlink permission contract')
def test_symlink_profile_and_directory_rejected(tmp_path):
    target=tmp_path/'real'
    target.mkdir(mode=0o700)
    redirect=tmp_path/'redirect'
    redirect.symlink_to(target,target_is_directory=True)
    with pytest.raises(TunnelError):
        ProfileStore(redirect)
    store=ProfileStore(target)
    store.save(profile(tmp_path))
    saved=store.profile_path('personal')
    saved.unlink()
    saved.symlink_to(tmp_path/'outside.json')
    with pytest.raises(TunnelError):
        store.save(profile(tmp_path))


@pytest.mark.parametrize('value',[123,{},True])
def test_typed_profile_fields_fail_safely(tmp_path,value):
    with pytest.raises(TunnelError):
        replace(profile(tmp_path),token_file=value).validate()

def test_profile_listing_ignores_lifecycle_and_runtime_metadata(tmp_path):
    from pseudolife_memory.tunnel_profiles import private_write
    store=ProfileStore(tmp_path/'profiles')
    store.save(profile(tmp_path))
    private_write(store.root/'runtime.json',b'{"version":"0.0.15"}')
    private_write(store.root/'personal.process.json',b'{"pid":0}')
    assert store.list() == ['personal']


def _busy(real, counter, winerror, times=2):
    def call(*args, **kwargs):
        counter.append(1)
        if len(counter) <= times:
            exc = PermissionError(13, 'sharing violation')
            exc.winerror = winerror
            raise exc
        return real(*args, **kwargs)
    return call


@pytest.mark.parametrize('winerror', [5, 32])
def test_private_files_ride_out_a_windows_sharing_violation(tmp_path, monkeypatch, winerror):
    # On Windows a reader without delete sharing blocks the supervisor's
    # os.replace, and a pending replace blocks a status read's open. Both
    # clear within milliseconds; neither may surface as a broken record.
    import os
    from pseudolife_memory import tunnel_profiles
    store = ProfileStore(tmp_path / 'profiles')
    store._prepare()
    record = store.root / 'personal.process.json'
    replaced, opened = [], []
    monkeypatch.setattr(tunnel_profiles.os, 'replace', _busy(os.replace, replaced, winerror))
    tunnel_profiles.private_write(record, b'{"pid":0}')
    monkeypatch.undo()
    monkeypatch.setattr(tunnel_profiles.os, 'open', _busy(os.open, opened, winerror))
    assert tunnel_profiles.private_read(record) == b'{"pid":0}'
    assert len(replaced) == 3 and len(opened) == 3


def test_a_real_permission_error_still_fails_at_once(tmp_path, monkeypatch):
    import os
    from pseudolife_memory import tunnel_profiles
    store = ProfileStore(tmp_path / 'profiles')
    store._prepare()
    record = store.root / 'personal.process.json'
    tunnel_profiles.private_write(record, b'{"pid":0}')
    opened = []

    def denied(*args, **kwargs):
        opened.append(1)
        raise PermissionError(13, 'denied')
    monkeypatch.setattr(tunnel_profiles.os, 'open', denied)
    with pytest.raises(TunnelError):
        tunnel_profiles.private_read(record)
    assert opened == [1]
