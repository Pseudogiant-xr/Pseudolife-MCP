"""Private verification observes real calls without retaining bank payloads."""
import json
import io
import os
import sys
from contextlib import contextmanager
from dataclasses import replace

import pytest

from pseudolife_memory import tunnel_bridge as bridge
from pseudolife_memory.tunnel_profiles import Profile, ProfileStore, private_read, private_write
from pseudolife_memory.tunnel_profiles import TunnelError


@contextmanager
def active_launch(store, profile):
    from pseudolife_memory import tunnel_runtime as runtime
    snapshot = runtime.snapshot_bridge(profile, store, ['fixture', 'tunnel', 'shim'])
    with runtime.runtime_profile(profile, store, snapshot['command']) as config:
        record = {**runtime._process_identity(os.getpid()), 'snapshot': snapshot, 'ready': True, 'config': str(config),
                  'launch_identity': runtime.capture_launch_identity(profile, store, snapshot, config),
                  'config_digest': runtime._config_digest(config)}
        private_write(store.root / (profile.name + '.process.json'), json.dumps(record).encode())
        yield config


@pytest.fixture
def store(tmp_path):
    result = ProfileStore(tmp_path / 'profiles')
    token = tmp_path / 'daemon-token'
    private_write(token, b'fixture-daemon-token')
    result.save(Profile('dot', 'http://127.0.0.1:8765', str(token),
                        tunnel_id='tunnel_0123', consent=True, state='ready'))
    result.set_key('dot', 'fixture-tunnel-key')
    from pseudolife_memory import tunnel_runtime as runtime
    profile = result.load('dot')
    snapshot = runtime.snapshot_bridge(profile, result, ['fixture', 'tunnel', 'shim'])
    with runtime.runtime_profile(profile, result, snapshot['command']) as config:
        record = {**runtime._process_identity(os.getpid()), 'snapshot': snapshot, 'ready': True, 'config': str(config),
                  'launch_identity': runtime.capture_launch_identity(profile, result, snapshot, config),
                  'config_digest': runtime._config_digest(config)}
        private_write(result.root / 'dot.process.json', json.dumps(record).encode())
        yield result


def request(identifier, tool, nonce):
    arguments = {'query': nonce} if tool == 'memory_search' else {'action': 'list', 'project': nonce}
    return {'jsonrpc': '2.0', 'id': identifier, 'method': 'tools/call',
            'params': {'name': tool, 'arguments': arguments}}


def response(identifier, **result):
    return {'jsonrpc': '2.0', 'id': identifier, 'result': {'content': [], **result}}


def test_expiry_only_metadata_preserves_proof_and_frozen_observer_without_refresh(store, monkeypatch):
    from pseudolife_memory import tunnel_runtime as runtime
    profile = store.load('dot')
    observer = bridge.Observer(store, 'dot', profile)
    challenge = bridge.begin_challenge(store, profile)
    for index, tool in enumerate(bridge.TOOLS):
        observer.request(request(index, tool, challenge['nonce']))
        observer.response(response(index))
    assert bridge.verification_status(store, profile)['verified']
    updated = replace(profile, runtime_key_expires_at='2099-12-01')
    store.save(updated)
    monkeypatch.setattr(runtime, 'update_profile', lambda *args: pytest.fail('expiry-only edit must not refresh'))
    monkeypatch.setattr(runtime, 'status_profile', lambda *args: {'running': True, 'ready': True})
    assert runtime.start_profile(updated, store, ['fixture', 'tunnel', 'shim'])['ready']
    assert bridge.verification_status(store, updated)['verified']
    challenge = bridge.begin_challenge(store, updated)
    for index, tool in enumerate(bridge.TOOLS):
        observer.request(request(index, tool, challenge['nonce']))
        observer.response(response(index))
    assert bridge.verification_status(store, updated)['verified']


@pytest.mark.parametrize('content', [[None], [{}], [{'type': 'text', 'text': 42}]])
def test_invalid_sdk_content_never_creates_cloud_receipts(store, content):
    challenge = bridge.begin_challenge(store, store.load('dot'))
    observer = bridge.Observer(store, 'dot')
    for index, tool in enumerate(bridge.TOOLS):
        observer.request(request(index, tool, challenge['nonce']))
        observer.response(response(index, content=content))
    status = bridge.verification_status(store, store.load('dot'))
    assert status['successful_calls'] == 0
    assert not status['verified']


@pytest.mark.parametrize('content', [[None], [{}], [{'type': 'text', 'text': 42}]])
def test_invalid_sdk_catalog_content_cannot_release_full_listing(content):
    outgoing, incoming = [], []
    gate = bridge.CatalogGate('full', outgoing.append, incoming.append)
    gate.request(b'{"id":1,"method":"tools/list"}\n')
    internal = json.loads(outgoing.pop())
    gate.response((json.dumps(response(internal['id'], content=content, structuredContent={'current': 'full'})) + '\n').encode())
    assert not outgoing
    assert json.loads(incoming[0])['error']['code'] == -32603


def test_valid_sdk_results_with_structured_content_prove_cloud_access(store):
    challenge = bridge.begin_challenge(store, store.load('dot'))
    observer = bridge.Observer(store, 'dot')
    for index, tool in enumerate(bridge.TOOLS):
        observer.request(request(index, tool, challenge['nonce']))
        observer.response(response(index, content=[{'type': 'text', 'text': 'fixture result'}],
                                   structuredContent={'matches': []}))
    assert bridge.verification_status(store, store.load('dot'))['verified']


def test_malformed_results_pass_through_real_stdio_without_cloud_proof(store):
    profile = store.load('dot')
    challenge = bridge.begin_challenge(store, profile)
    program = '''import json,sys
for line in sys.stdin.buffer:
 request=json.loads(line)
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'content':[None]}}),flush=True)
'''
    calls = [request(index, tool, challenge['nonce']) for index, tool in enumerate(bridge.TOOLS)]
    source = io.BytesIO(b''.join((json.dumps(call) + '\n').encode() for call in calls))
    destination = io.BytesIO()
    assert bridge.run_bridge(store, profile, command=[sys.executable, '-c', program],
                             stdin=source, stdout=destination) == 0
    messages = [json.loads(line) for line in destination.getvalue().splitlines()]
    assert messages == [response(index, content=[None]) for index in range(len(calls))]
    assert not bridge.verification_status(store, profile)['verified']


def test_challenge_needs_both_real_successes_and_retains_no_payload(store):
    challenge = bridge.begin_challenge(store, store.load('dot'))
    first = bridge.Observer(store, 'dot')
    second = bridge.Observer(store, 'dot')
    first.request(request(4, 'memory_search', challenge['nonce']))
    second.request(request('board', 'memory_agents', challenge['nonce']))
    second.response(response('board', content=[{'type': 'text', 'text': 'PRIVATE BOARD PAYLOAD'}]))
    assert bridge.verification_status(store, store.load('dot'))['verified'] is False
    first.response(response(4, content=[{'type': 'text', 'text': 'PRIVATE MEMORY PAYLOAD'}]))
    assert bridge.verification_status(store, store.load('dot'))['verified'] is True
    persisted = b''.join(p.read_bytes() for p in store.root.rglob('*.json'))
    assert b'PRIVATE' not in persisted
    assert b'fixture-daemon-token' not in persisted
    assert b'fixture-tunnel-key' not in persisted


@pytest.mark.parametrize('bad', [response(1, isError=True), {'id': 1, 'error': {'code': -1}},
                                {'id': 1, 'result': 'invalid'}])
def test_failure_and_wrong_nonce_never_verify(store, bad):
    challenge = bridge.begin_challenge(store, store.load('dot'))
    observer = bridge.Observer(store, 'dot')
    observer.request(request(1, 'memory_search', challenge['nonce']))
    observer.response(bad)
    observer.request(request(2, 'memory_agents', 'wrong-nonce'))
    observer.response(response(2))
    assert bridge.verification_status(store, store.load('dot'))['verified'] is False


def test_profile_change_and_key_rotation_invalidate_receipts(store):
    challenge = bridge.begin_challenge(store, store.load('dot'))
    observer = bridge.Observer(store, 'dot')
    for index, tool in enumerate(('memory_search', 'memory_agents')):
        observer.request(request(index, tool, challenge['nonce']))
        observer.response(response(index))
    assert bridge.verification_status(store, store.load('dot'))['verified']
    store.set_key('dot', 'rotated-fixture-key')
    assert not bridge.verification_status(store, store.load('dot'))['verified']


def test_same_version_active_snapshot_refresh_invalidates_proof_and_rollback_preserves_it(store, monkeypatch):
    import os
    from pseudolife_memory import tunnel_runtime as runtime
    profile = store.load('dot')
    sources = runtime._bridge_sources()
    first = runtime.snapshot_bridge(profile, store, ['fixture', 'tunnel', 'shim'])
    record_path = store.root / 'dot.process.json'
    record = json.loads(private_read(record_path))
    config = __import__('pathlib').Path(record['config'])
    old_config = private_read(config)
    challenge = bridge.begin_challenge(store, profile)
    observer = bridge.Observer(store, 'dot')
    for index, tool in enumerate(bridge.TOOLS):
        observer.request(request(index, tool, challenge['nonce']))
        observer.response(response(index))
    assert bridge.verification_status(store, profile)['verified']
    with monkeypatch.context() as context:
        context.setattr(bridge.sys, 'executable', str(store.root / 'another-client-python'))
        assert bridge.verification_status(store, profile)['verified']
    proof_files = {p: p.read_bytes() for p in (store.root / 'dot-verification').glob('*.json')}
    monkeypatch.setattr(runtime, '_bridge_sources', lambda: {
        **sources, 'tunnel_bridge.py': sources['tunnel_bridge.py'] + b'\n# candidate revision\n'})
    candidate = runtime.snapshot_bridge(profile, store, ['fixture', 'tunnel', 'shim'])
    assert candidate['version'] == first['version']
    assert candidate['digest'] != first['digest']
    changed_config = json.loads(old_config)
    changed_config['mcp']['commands'][0]['command'] = runtime._command_string(candidate['command'])
    private_write(config, json.dumps(changed_config).encode())
    private_write(record_path, json.dumps({**record, 'snapshot': candidate,
        'launch_identity': runtime.capture_launch_identity(profile, store, candidate, config),
        'config_digest': runtime._config_digest(config)}).encode())
    assert not bridge.verification_status(store, profile)['verified']
    private_write(config, old_config)
    private_write(record_path, json.dumps(record).encode())
    assert bridge.verification_status(store, profile)['verified']
    assert {p: p.read_bytes() for p in proof_files} == proof_files


@pytest.mark.parametrize('changed', ['key', 'daemon', 'tunnel', 'organization', 'token'])
def test_old_active_launch_cannot_prove_new_saved_identity(store, changed):
    from pseudolife_memory import tunnel_runtime as runtime
    profile = store.load('dot')
    snapshot = runtime.snapshot_bridge(profile, store, ['fixture', 'tunnel', 'shim'])
    with runtime.runtime_profile(profile, store, snapshot['command']) as config:
        record = {**runtime._process_identity(os.getpid()), 'snapshot': snapshot, 'ready': True, 'config': str(config),
                  'launch_identity': runtime.capture_launch_identity(profile, store, snapshot, config),
                  'config_digest': runtime._config_digest(config)}
        private_write(store.root / 'dot.process.json', json.dumps(record).encode())
        old_observer = bridge.Observer(store, 'dot', profile)
        old_key_path = config.parent / 'key'
        old_key = old_key_path.read_bytes()
        if changed == 'key':
            store.set_key('dot', 'changed-fixture-key')
        elif changed == 'daemon':
            store.save(replace(profile, daemon_url='http://127.0.0.1:8766'))
        elif changed == 'tunnel':
            store.save(replace(profile, tunnel_id='tunnel_9876'))
        elif changed == 'organization':
            store.save(replace(profile, organization_id='org-fixture'))
        else:
            private_write(profile.token_file, b'changed-daemon-fixture-token')
        assert old_key_path.read_bytes() == old_key
        with pytest.raises(TunnelError, match='refresh|running|active'):
            bridge.begin_challenge(store, store.load('dot'))
        updated = store.load('dot')
        with active_launch(store, updated):
            challenge = bridge.begin_challenge(store, updated)
            for index, tool in enumerate(bridge.TOOLS):
                old_observer.request(request(index, tool, challenge['nonce']))
                old_observer.response(response(index))
            assert not bridge.verification_status(store, updated)['verified']
            observer = bridge.Observer(store, 'dot', updated)
            for index, tool in enumerate(bridge.TOOLS):
                observer.request(request(index, tool, challenge['nonce']))
                observer.response(response(index))
            assert bridge.verification_status(store, updated)['verified']


def test_restored_saved_key_matches_actual_old_launch_and_preserves_proof(store):
    profile = store.load('dot')
    challenge = bridge.begin_challenge(store, profile)
    observer = bridge.Observer(store, 'dot', profile)
    for index, tool in enumerate(bridge.TOOLS):
        observer.request(request(index, tool, challenge['nonce']))
        observer.response(response(index))
    proof_files = {p: p.read_bytes() for p in (store.root / 'dot-verification').glob('*.json')}
    original_key = store.read_key('dot')
    store.set_key('dot', 'changed-fixture-key')
    assert not bridge.verification_status(store, profile)['verified']
    store.set_key('dot', original_key)
    assert bridge.verification_status(store, profile)['verified']
    assert {p: p.read_bytes() for p in proof_files} == proof_files


def test_legacy_active_record_cannot_prove_without_launch_evidence(store):
    path = store.root / 'dot.process.json'
    record = json.loads(private_read(path))
    record.pop('launch_identity')
    private_write(path, json.dumps(record).encode())
    with pytest.raises(TunnelError, match='identity|launch|refresh'):
        bridge.begin_challenge(store, store.load('dot'))


def test_frozen_bridge_captures_actual_launch_before_readiness(store, monkeypatch):
    from pathlib import Path
    path = store.root / 'dot.process.json'
    record = json.loads(private_read(path))
    snapshot = record['snapshot']
    config = Path(record['config'])
    monkeypatch.setattr(bridge, '__file__', str(Path(snapshot['command'][1]).parent / 'tunnel_bridge.py'))
    monkeypatch.setenv('PSEUDOLIFE_TUNNEL_LAUNCH_PROFILE', str(config.parent / 'launch-profile.json'))
    private_write(path, json.dumps({**record, 'ready': False}).encode())
    observer = bridge.Observer(store, 'dot', store.load('dot'))
    assert observer.loaded_launch == record['launch_identity']
    private_write(path, json.dumps(record).encode())
    challenge = bridge.begin_challenge(store, store.load('dot'))
    for index, tool in enumerate(bridge.TOOLS):
        observer.request(request(index, tool, challenge['nonce']))
        observer.response(response(index))
    assert bridge.verification_status(store, store.load('dot'))['verified']


def test_no_receipt_for_duplicate_request_id_or_notification(store):
    challenge = bridge.begin_challenge(store, store.load('dot'))
    observer = bridge.Observer(store, 'dot')
    observer.request(request(1, 'memory_search', challenge['nonce']))
    observer.request({'id': 1, 'method': 'ping'})
    observer.response(response(1))
    notification = request(None, 'memory_agents', challenge['nonce'])
    notification.pop('id')
    observer.request(notification)
    observer.response(response(None))
    assert not bridge.verification_status(store, store.load('dot'))['verified']


def test_catalog_hold_expand_and_notifications_passthrough():
    outgoing, incoming = [], []
    gate = bridge.CatalogGate('full', outgoing.append, incoming.append)
    listing = b'{"jsonrpc":"2.0","id":4,"method":"tools/list"}\n'
    gate.request(listing)
    status = json.loads(outgoing.pop())
    assert status['params']['arguments'] == {'action': 'status'}
    notice = b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n'
    gate.response(notice)
    assert incoming == [notice]
    for current in ('minimal', 'core', 'full'):
        gate.response((json.dumps(response(status['id'], structuredContent={'current': current})) + '\n').encode())
        if current != 'full':
            status = json.loads(outgoing.pop())
            assert status['params']['arguments'] == {'action': 'expand'}
    assert outgoing == [listing]
    assert incoming == [notice]


def test_full_catalog_failure_returns_safe_error_and_current_passes():
    outgoing, incoming = [], []
    gate = bridge.CatalogGate('full', outgoing.append, incoming.append)
    listing = b'{"jsonrpc":"2.0","id":4,"method":"tools/list"}\n'
    gate.request(listing)
    status = json.loads(outgoing.pop())
    gate.response((json.dumps(response(status['id'], isError=True)) + '\n').encode())
    assert not outgoing
    assert json.loads(incoming[0])['error']['code'] == -32603
    outgoing.clear()
    current = bridge.CatalogGate('current', outgoing.append, incoming.append)
    current.request(listing)
    assert outgoing == [listing]


def test_domain_error_is_not_cloud_success(store):
    challenge = bridge.begin_challenge(store, store.load('dot'))
    observer = bridge.Observer(store, 'dot')
    for index, tool in enumerate(bridge.TOOLS):
        observer.request(request(index, tool, challenge['nonce']))
        observer.response(response(index, content=[{'type': 'text', 'text': '{"error":"unavailable"}'}]))
    assert not bridge.verification_status(store, store.load('dot'))['verified']


@pytest.mark.parametrize('awareness, verified', [
    ({'enabled': False, 'available': False, 'peers': [], 'truncated': False}, False),
    ({'enabled': True, 'available': False, 'peers': [], 'truncated': False, 'reason': 'principal_not_allowed'}, False),
    ({'enabled': True, 'available': False, 'peers': [], 'truncated': False, 'reason': 'not_initialized'}, False),
    ({'enabled': True, 'available': True, 'peers': [], 'truncated': False}, True)])
@pytest.mark.parametrize('channel', ['text', 'structured'])
def test_unavailable_board_awareness_is_not_cloud_success(store, awareness, verified, channel):
    challenge = bridge.begin_challenge(store, store.load('dot'))
    observer = bridge.Observer(store, 'dot')
    observer.request(request(1, 'memory_search', challenge['nonce']))
    observer.response(response(1, content=[{'type': 'text', 'text': 'fixture result'}]))
    observer.request(request(2, 'memory_agents', challenge['nonce']))
    if channel == 'text':
        observer.response(response(2, content=[{'type': 'text', 'text': json.dumps(awareness)}]))
    else:
        observer.response(response(2, content=[{'type': 'text', 'text': 'fixture result'}], structuredContent=awareness))
    status = bridge.verification_status(store, store.load('dot'))
    assert status['verified'] is verified
    assert status['successful_calls'] == (2 if verified else 1)


def test_malformed_catalog_payload_fails_closed():
    outgoing, incoming = [], []
    gate = bridge.CatalogGate('full', outgoing.append, incoming.append)
    gate.request(b'{"id":1,"method":"tools/list"}\n')
    internal = json.loads(outgoing.pop())
    gate.response((json.dumps(response(internal['id'], content=[{'type': 'text', 'text': None}])) + '\n').encode())
    assert json.loads(incoming[0])['error']['code'] == -32603


def test_each_full_list_restores_catalog_after_expiry_and_retry():
    outgoing, incoming = [], []
    gate = bridge.CatalogGate('full', outgoing.append, incoming.append)
    listing = b'{"id":1,"method":"tools/list"}\n'
    gate.request(listing)
    internal = json.loads(outgoing.pop())
    gate.response((json.dumps(response(internal['id'], structuredContent={'current': 'full'})) + '\n').encode())
    assert outgoing.pop() == listing
    next_listing = b'{"id":2,"method":"tools/list"}\n'
    gate.request(next_listing)
    status = json.loads(outgoing.pop())
    assert status['params']['arguments'] == {'action': 'status'}
    gate.response((json.dumps(response(status['id'], structuredContent={'current': 'core'})) + '\n').encode())
    expand = json.loads(outgoing.pop())
    gate.response((json.dumps(response(expand['id'], structuredContent={'current': 'full'})) + '\n').encode())
    assert outgoing.pop() == next_listing
    assert incoming == []
    gate.request(b'{"id":3,"method":"tools/list"}\n')
    timed_out = json.loads(outgoing.pop())
    gate.timeout()
    assert json.loads(incoming.pop())['id'] == 3
    gate.response((json.dumps(response(timed_out['id'], structuredContent={'current': 'full'})) + '\n').encode())
    assert incoming == []
    gate.request(b'{"id":4,"method":"tools/list"}\n')
    retried = json.loads(outgoing.pop())
    assert retried['params']['arguments'] == {'action': 'status'}
    gate.close()


@pytest.mark.parametrize('catalog', ['current', 'full'])
def test_real_stdio_passthrough_cleanup_and_cloud_receipts(store, catalog):
    profile = replace(store.load('dot'), minimum_catalog=catalog)
    store.save(profile)
    with active_launch(store, profile):
        challenge = bridge.begin_challenge(store, profile)
        program = __import__('textwrap').dedent('''
    import json,sys
    for line in sys.stdin.buffer:
     request=json.loads(line)
     if request.get('method')=='tools/call' and request.get('params',{}).get('name')=='memory_toolset':
      result={'content':[], 'structuredContent':{'current':'full'}}
     else: result={'content':[]}
     print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
    ''')
        requests = [{'jsonrpc': '2.0', 'id': 'listing', 'method': 'tools/list'}]
        requests += [request(index, tool, challenge['nonce']) for index, tool in enumerate(bridge.TOOLS)]
        source = io.BytesIO(b''.join((json.dumps(r) + '\n').encode() for r in requests))
        destination = io.BytesIO()
        assert bridge.run_bridge(store, profile, command=[sys.executable, '-c', program],
                                 stdin=source, stdout=destination) == 0
        messages = [json.loads(line) for line in destination.getvalue().splitlines()]
        assert {m['id'] for m in messages} == {'listing', 0, 1}
        assert bridge.verification_status(store, profile)['verified']
