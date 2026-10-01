import hashlib
import io
import zipfile
from dataclasses import replace
from pathlib import Path
import pytest
from pseudolife_memory.tunnel_profiles import Profile, ProfileStore, TunnelError
from pseudolife_memory import tunnel_runtime as runtime


@pytest.mark.parametrize('phase', ['replacement', 'rollback'])
@pytest.mark.parametrize('cancellation', ['keyboard', 'sigterm'])
@pytest.mark.parametrize('boundary', ['readiness', 'handoff', 'finally', 'factory', 'registration'])
def test_refresh_cancellation_reaps_actual_children_and_private_launch(tmp_path, phase, cancellation, boundary):
    import json
    import subprocess
    import sys
    store, profile = _ready_profile(tmp_path)
    script = tmp_path / 'cancel_refresh.py'
    script.write_text('''import inspect, json, signal, subprocess, sys
from pathlib import Path
from pseudolife_memory import tunnel_runtime as runtime
from pseudolife_memory.tunnel_profiles import ProfileStore, private_write
store = ProfileStore(Path(sys.argv[1]))
profile = store.load('personal')
phase, cancellation, boundary = sys.argv[2:]
expected_children = 1 if phase == 'initial' else (3 if phase == 'rollback' else 2)
children = []
injected = []
runtime._bridge_sources = lambda: {'shim.py': b'def run_shim(): pass\\n', 'tunnel_bridge.py': b'# synthetic frozen bridge\\n'}
real_popen = subprocess.Popen
def launch_fixture(args, **kwargs):
    child = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
    children.append(child)
    return child
def ready(child, config, timeout):
    if len(children) == 1:
        record = json.loads(runtime.private_read(store.root / 'personal.process.json'))
        request = {'id': 'a' * 32, 'target': {'pid': record['pid'], 'created': record['created']}, 'snapshot': record['snapshot']}
        private_write(store.root / 'personal.reload.json', json.dumps(request).encode())
        return True
    if phase == 'rollback' and len(children) == 2:
        return False
    if boundary != 'readiness':
        return True
    if cancellation == 'sigterm':
        signal.raise_signal(signal.SIGTERM)
    raise KeyboardInterrupt
source, first_line = inspect.getsourcelines(runtime._refresh_child)
finally_line = first_line + next(index for index, line in enumerate(source) if 'for child, identity in owned_children:' in line)
factory_source, factory_first_line = inspect.getsourcelines(runtime._launch_child)
factory_return_line = factory_first_line + next(index for index, line in enumerate(factory_source) if 'return process, identity' in line)
registration_line = factory_first_line + next(index for index, line in enumerate(factory_source) if 'identity = _process_identity(process.pid)' in line)
def trace(frame, event, arg):
    helper_gap = frame.f_code is runtime._refresh_child.__code__ and frame.f_locals.get('handed_off') is not None and (boundary == 'handoff' or (boundary == 'finally' and frame.f_lineno == finally_line))
    factory_gap = boundary in ('factory', 'registration') and frame.f_code is runtime._launch_child.__code__ and frame.f_lineno == (registration_line if boundary == 'registration' else factory_return_line) and len(children) == expected_children
    if event == 'line' and (helper_gap or factory_gap) and not injected:
        owner = frame.f_back
        while owner.f_code is not runtime.run_profile.__wrapped__.__code__:
            owner = owner.f_back
        injected.append(owner.f_locals.get('process') is (None if phase == 'initial' else children[0]))
        registered.append(any(child is children[-1] for child, identity in owner.f_locals['owned_children']))
        if cancellation in ('sigterm', 'sigint'):
            signal.raise_signal(signal.SIGTERM if cancellation == 'sigterm' else signal.SIGINT)
            if boundary == 'registration':
                return trace
        raise KeyboardInterrupt
    return trace
runtime.subprocess.Popen = launch_fixture
runtime._wait_ready = ready
cancelled = False
registered = []
previous_handlers = [signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)]
if boundary != 'readiness':
    sys.settrace(trace)
try:
    runtime.run_profile(profile, store, ['pseudolife-mcp', 'tunnel', 'shim'], binary=Path(sys.executable))
except KeyboardInterrupt:
    cancelled = True
finally:
    sys.settrace(None)
    result = {'cancelled': cancelled, 'handlers_restored': previous_handlers == [signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)], 'caller_owned_old_child': injected == [True], 'caller_registered_new_child': registered == [True], 'reached_phase': len(children) == expected_children, 'all_reaped': all(child.poll() is not None for child in children), 'record_removed': not (store.root / 'personal.process.json').exists(), 'launch_files_removed': not list(store.root.glob('.launch-*'))}
    for child in children:
        if child.poll() is None:
            runtime._terminate_owned_tree(runtime.psutil.Process(child.pid))
            child.wait(timeout=10)
    print(json.dumps(result))
''', encoding='utf-8')
    completed = subprocess.run([sys.executable, '-c', script.read_text(encoding='utf-8'), str(store.root), phase, cancellation, boundary],
                               env=runtime.child_environment(profile), capture_output=True, timeout=30)
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result['cancelled'] and result['reached_phase']
    if boundary != 'readiness':
        assert result['caller_owned_old_child']
        assert result['caller_registered_new_child'] is (boundary != 'registration')
    assert result['all_reaped']
    assert result['handlers_restored']
    assert result['record_removed'] and result['launch_files_removed']
    assert store.read_key(profile.name) == 'synthetic-private-key'


@pytest.mark.parametrize('cancellation', ['keyboard', 'sigint', 'sigterm'])
def test_initial_registration_cancellation_reaps_actual_child(tmp_path, cancellation):
    test_refresh_cancellation_reaps_actual_children_and_private_launch(tmp_path, 'initial', cancellation, 'registration')


@pytest.mark.parametrize('phase', ['replacement', 'rollback'])
def test_refresh_registration_sigint_reaps_actual_child(tmp_path, phase):
    test_refresh_cancellation_reaps_actual_children_and_private_launch(tmp_path, phase, 'sigint', 'registration')


@pytest.mark.parametrize('failure', ['capture', 'append'])
@pytest.mark.parametrize('inspection', ['available', 'denied'])
def test_launch_registration_failure_reaps_handle_and_restores_handlers(tmp_path, monkeypatch, failure, inspection):
    import signal
    import subprocess
    import sys
    store, profile = _ready_profile(tmp_path)
    children = []
    real_popen = subprocess.Popen
    def launch_fixture(args, **kwargs):
        child = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(runtime.subprocess, 'Popen', launch_fixture)
    if failure == 'capture':
        def capture(pid):
            raise KeyboardInterrupt
        monkeypatch.setattr(runtime, '_process_identity', capture)
    class FailedLedger(list):
        def append(self, item):
            raise KeyboardInterrupt
    ledger = FailedLedger() if failure == 'append' else []
    if inspection == 'denied':
        def denied(pid):
            raise runtime.psutil.AccessDenied(pid)
        if failure == 'append':
            real_identity = runtime._process_identity
            def capture_then_deny(pid):
                identity = real_identity(pid)
                monkeypatch.setattr(runtime.psutil, 'Process', denied)
                return identity
            monkeypatch.setattr(runtime, '_process_identity', capture_then_deny)
        else:
            monkeypatch.setattr(runtime.psutil, 'Process', denied)
    handlers = [signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)]
    with runtime.runtime_profile(profile, store, ['synthetic-shim']) as config:
        try:
            with pytest.raises(KeyboardInterrupt):
                runtime._launch_child(profile, Path(sys.executable), config, store, owned_children=ledger)
            assert len(children) == 1 and children[0].poll() is not None
            assert handlers == [signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)]
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)


def test_unregistered_launch_reaps_inspectable_descendant(tmp_path, monkeypatch):
    import subprocess
    import sys
    import time
    store, profile = _ready_profile(tmp_path)
    marker = tmp_path / 'descendant.pid'
    script = ('import signal, subprocess, sys, time; from pathlib import Path; '
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              'signal.signal(signal.SIGTERM, lambda *args: (child.terminate(), child.wait(), sys.exit(0))); '
              'Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)')
    real_popen = subprocess.Popen
    real_identity = runtime._process_identity
    children = []
    descendant_records = []
    def launch_fixture(args, **kwargs):
        child = real_popen([sys.executable, '-c', script, str(marker)], **kwargs)
        children.append(child)
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        descendant_records.append(real_identity(int(marker.read_text())))
        return child
    monkeypatch.setattr(runtime.subprocess, 'Popen', launch_fixture)
    def capture(pid):
        raise KeyboardInterrupt
    monkeypatch.setattr(runtime, '_process_identity', capture)
    with runtime.runtime_profile(profile, store, ['synthetic-shim']) as config:
        try:
            with pytest.raises(KeyboardInterrupt):
                runtime._launch_child(profile, Path(sys.executable), config, store, owned_children=[])
            assert children[0].poll() is not None
            descendant = runtime._owned_process(descendant_records[0])
            assert descendant is None or descendant.status() == runtime.psutil.STATUS_ZOMBIE
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
            for record in descendant_records:
                descendant = runtime._owned_process(record)
                if descendant is not None and descendant.status() != runtime.psutil.STATUS_ZOMBIE:
                    runtime._terminate_owned_tree(descendant)


def archive(name='tunnel-client.exe', data=b'synthetic-binary'):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as zipped:
        zipped.writestr(name, data)
    return output.getvalue()


def test_download_checksum_before_execution(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / 'profiles')
    monkeypatch.setattr(runtime, '_download', lambda url: b'untrusted')
    with pytest.raises(TunnelError, match='checksum'):
        runtime.ensure_runtime(store)
    assert not list(store.root.rglob('tunnel-client.exe'))


def test_verified_archive_rejects_traversal(tmp_path, monkeypatch):
    payload = archive('../outside.exe')
    monkeypatch.setattr(runtime, '_download', lambda url: payload)
    monkeypatch.setattr(runtime, 'ASSETS', {('windows', 'amd64'): hashlib.sha256(payload).hexdigest()})
    monkeypatch.setattr(runtime, '_platform', lambda: ('windows', 'amd64'))
    with pytest.raises(TunnelError, match='archive'):
        runtime.ensure_runtime(ProfileStore(tmp_path / 'profiles'))
    assert not (tmp_path / 'outside.exe').exists()


def test_candidate_not_promoted_on_readiness_failure(tmp_path, monkeypatch):
    payload = archive()
    monkeypatch.setattr(runtime, '_download', lambda url: payload)
    monkeypatch.setattr(runtime, 'ASSETS', {('windows', 'amd64'): hashlib.sha256(payload).hexdigest()})
    monkeypatch.setattr(runtime, '_platform', lambda: ('windows', 'amd64'))
    store = ProfileStore(tmp_path / 'profiles')
    with pytest.raises(TunnelError):
        runtime.ensure_runtime(store, readiness=lambda binary: False)
    assert not (store.root / 'runtime.json').exists()


def test_launch_profile_uses_file_reference_not_key(tmp_path):
    store = ProfileStore(tmp_path / 'profiles')
    profile = Profile(name='personal', daemon_url='http://127.0.0.1:8765', token_file=str(tmp_path / 'daemon.token'), tunnel_id='tunnel_0123', consent=True, state='ready')
    store.save(profile)
    store.set_key(profile.name, 'synthetic-private-key')
    with runtime.runtime_profile(profile, store, ['pseudolife-mcp', 'tunnel', 'shim', '--profile', profile.name]) as path:
        config = path.read_text()
        assert 'synthetic-private-key' not in config
        assert 'file:' in config
        assert '127.0.0.1:0' in config
    assert not path.exists()
    assert not list(store.root.glob('.launch-*'))


def test_process_env_does_not_inherit_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('PSEUDOLIFE_MCP_TOKEN', 'synthetic-daemon-token')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-openai-key')
    profile = Profile(name='personal', daemon_url='http://127.0.0.1:8765', token_file=str(tmp_path / 'daemon.token'))
    env = runtime.child_environment(profile)
    assert 'PSEUDOLIFE_MCP_TOKEN' not in env
    assert 'OPENAI_API_KEY' not in env
    assert env['PSEUDOLIFE_MCP_TOKEN_FILE'] == profile.token_file


def test_status_never_probes_pid_with_os_kill(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.os, 'kill', lambda *a: pytest.fail('unsafe process probe'))
    assert runtime.status_profile('personal', ProfileStore(tmp_path / 'profiles'))['running'] is False

def test_profile_guard_serializes_lifetime(tmp_path):
    store = ProfileStore(tmp_path / 'profiles')
    with runtime.profile_lock('personal', store):
        with pytest.raises(TunnelError, match='running'):
            with runtime.profile_lock('personal', store):
                pass


def test_lifecycle_readiness_failure_preserves_key_and_cleans(tmp_path, monkeypatch):
    import subprocess
    import sys
    real_popen = subprocess.Popen
    store = ProfileStore(tmp_path / 'profiles')
    profile = Profile(name='personal', daemon_url='http://127.0.0.1:8765', token_file=str(tmp_path/'daemon.token'), tunnel_id='tunnel_0123', consent=True, state='ready')
    store.save(profile)
    store.set_key(profile.name, 'synthetic-private-key')
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda argv, **kwargs: real_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs))
    monkeypatch.setattr(runtime, '_ready', lambda config: False)
    with pytest.raises(TunnelError, match='readiness'):
        runtime.run_profile(profile, store, ['pseudolife-mcp'], binary=Path(sys.executable), ready_timeout=0.05)
    assert store.read_key(profile.name) == 'synthetic-private-key'
    assert not list(store.root.glob('.launch-*'))
    assert runtime.status_profile(profile.name, store)['running'] is False


def test_doctor_drops_vendor_secrets(tmp_path, monkeypatch):
    import subprocess
    import json
    store = ProfileStore(tmp_path/'profiles')
    profile = Profile(name='personal', daemon_url='http://127.0.0.1:8765', token_file=str(tmp_path/'daemon.token'), tunnel_id='tunnel_0123', consent=True, state='ready')
    store.save(profile)
    store.set_key(profile.name, 'synthetic-private-key')
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps({'result':'ok','next':'synthetic-private-key','checks':[{'id':'api_key','status':'PASS','summary':'synthetic-private-key'}]}).encode(), b''))
    result = runtime.doctor_profile(profile,store,['pseudolife-mcp'],binary=Path('unused'))
    assert 'synthetic-private-key' not in json.dumps(result)
    assert result['cloud_verified'] is False

def test_doctor_missing_runtime_does_not_download(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path/'profiles')
    profile = Profile(name='personal', daemon_url='http://127.0.0.1:8765', token_file=str(tmp_path/'daemon.token'), tunnel_id='tunnel_0123', consent=True, state='ready')
    monkeypatch.setattr(runtime, '_download', lambda url: pytest.fail('read-only doctor downloaded'))
    assert runtime.doctor_profile(profile,store,['pseudolife-mcp'])['ok'] is False


def test_stale_pid_identity_never_terminates_replacement(tmp_path, monkeypatch):
    from pseudolife_memory.tunnel_profiles import private_write
    import json
    import os
    store = ProfileStore(tmp_path/'profiles')
    store._prepare()
    private_write(store.root/'personal.process.json', json.dumps({'pid':os.getpid(),'created':-1,'exe':'unused','argv':[]}).encode())
    monkeypatch.setattr(runtime.psutil.Process, 'terminate', lambda process: pytest.fail('wrong process terminated'))
    assert runtime.stop_profile('personal',store)['stopped']


def test_stale_launch_key_is_cleaned_before_retry(tmp_path):
    store = ProfileStore(tmp_path/'profiles')
    store._prepare()
    stale = store.root/'.launch-personal-interrupted'
    stale.mkdir()
    (stale/'key').write_text('synthetic-private-key')
    with runtime.profile_lock('personal',store):
        runtime._cleanup_orphans('personal',store)
    assert not stale.exists()

def _ready_profile(tmp_path):
    store=ProfileStore(tmp_path/'profiles')
    profile=Profile(name='personal',daemon_url='http://127.0.0.1:8765',token_file=str(tmp_path/'daemon.token'),tunnel_id='tunnel_0123',consent=True,state='ready')
    from pseudolife_memory.tunnel_profiles import private_write
    private_write(Path(profile.token_file),b'synthetic-daemon-token')
    store.save(profile)
    store.set_key(profile.name,'synthetic-private-key')
    return store,profile


def test_same_version_running_update_requests_refresh(tmp_path,monkeypatch):
    import json
    from pseudolife_memory.tunnel_profiles import private_write
    store,profile=_ready_profile(tmp_path)
    private_write(store.root/'personal.process.json',json.dumps({'snapshot':{'digest':'old'},'supervisor':{'pid':1},'refresh_protocol':1,'launch_identity':{'fixture':True}}).encode())
    monkeypatch.setattr(runtime,'ensure_runtime',lambda *a: Path('unused'))
    monkeypatch.setattr(runtime,'status_profile',lambda *a:{'running':True,'ready':True})
    monkeypatch.setattr(runtime,'_owned_process',lambda record: object())
    monkeypatch.setattr(runtime,'snapshot_bridge',lambda *a:{'digest':'new','command':['immutable-new']})
    monkeypatch.setattr(runtime,'validate_snapshot',lambda store,snapshot:snapshot)
    seen=[]
    def request(profile,store,snapshot):
        seen.append(snapshot['digest'])
        return {'state':'refreshed','changed':True,'running':True,'rolled_back':False}
    monkeypatch.setattr(runtime,'_request_refresh',request)
    result=runtime.update_profile(profile,store,['pseudolife-mcp','tunnel','shim'])
    assert seen == ['new']
    assert result['changed'] is True


def test_idle_update_does_not_start(tmp_path,monkeypatch):
    store,profile=_ready_profile(tmp_path)
    monkeypatch.setattr(runtime,'ensure_runtime',lambda *a:Path('unused'))
    monkeypatch.setattr(runtime,'start_profile',lambda *a,**k:pytest.fail('idle profile started'))
    assert runtime.update_profile(profile,store,['pseudolife-mcp'])['running'] is False


def test_idle_update_does_not_fetch_runtime(tmp_path,monkeypatch):
    store,profile=_ready_profile(tmp_path)
    fetched=[]
    def fetch(*a,**k):
        fetched.append(a)
        raise TunnelError('verified vendor runtime download failed')
    monkeypatch.setattr(runtime,'ensure_runtime',fetch)
    result=runtime.update_profile(profile,store,['pseudolife-mcp'])
    assert result=={'profile':'personal','version':runtime.VERSION,'changed':False,'running':False,'state':'unchanged'}
    assert fetched==[]


def test_frozen_bridge_command_retains_source_after_alias_change(tmp_path):
    import json
    store,profile=_ready_profile(tmp_path)
    snapshot=runtime.snapshot_bridge(profile,store,['pseudolife-mcp','tunnel','shim'])
    assert snapshot['command'][1].endswith('launch.py')
    assert 'pseudolife-mcp' not in snapshot['command']
    assert runtime.validate_snapshot(store,snapshot) == snapshot
    directory=Path(snapshot['command'][1]).parent
    old=(directory/'shim.py').read_bytes()
    assert old
    assert json.loads((directory/'manifest.json').read_text())['digest'] == snapshot['digest']


def test_failed_refresh_restarts_frozen_old_command(tmp_path,monkeypatch):
    import json
    import subprocess
    import sys
    store,profile=_ready_profile(tmp_path)
    old=runtime.snapshot_bridge(profile,store,['pseudolife-mcp','tunnel','shim'])
    candidate_command=list(old['command'])
    candidate_command[1]='immutable-new'
    candidate=dict(old,digest='candidate',command=candidate_command)
    monkeypatch.setattr(runtime,'validate_snapshot',lambda store,snapshot:snapshot)
    real_popen=subprocess.Popen
    launches=[]
    proof=store.root/'private-cloud-proof.json'
    from pseudolife_memory.tunnel_profiles import private_write
    private_write(proof,b'{"verified":true,"identity":"previous"}')
    proof_before=proof.read_bytes()
    def launch(profile,binary,config,store,*,owned_children=None):
        launches.append(json.loads(config.read_text())['mcp']['commands'][0]['command'])
        child=real_popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        identity=runtime._process_identity(child.pid)
        if owned_children is not None:
            owned_children.append((child,identity))
        return child,identity
    monkeypatch.setattr(runtime,'_launch_child',launch)
    monkeypatch.setattr(runtime,'_wait_ready',lambda process,config,timeout:len(launches)!=1)
    with runtime.runtime_profile(profile,store,old['command']) as config:
        original=real_popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        record=runtime._process_identity(original.pid)
        record.update(snapshot=old,ready=True,config=str(config),binary='unused')
        child=None
        try:
            child,restored,result=runtime._refresh_child(profile,store,config,original,record,candidate)
            assert result['state']=='rolled-back'
            assert result['rolled_back'] is True
            assert restored['snapshot']['digest']==old['digest']
            assert launches[0] == runtime._command_string(candidate_command)
            assert launches[1] == runtime._command_string(old['command'])
            assert store.read_key(profile.name)=='synthetic-private-key'
            assert proof.read_bytes()==proof_before
        finally:
            if child and child.poll() is None:
                child.terminate();child.wait(timeout=5)
            if original.poll() is None:
                original.terminate();original.wait(timeout=5)

def test_snapshot_executes_prior_code_after_installed_source_changes(tmp_path,monkeypatch):
    import subprocess
    store,profile=_ready_profile(tmp_path)
    sources={'shim.py':b"def run_shim():\n print('old-bridge-implementation')\n",'tunnel_bridge.py':b'# disposable bridge fixture\n'}
    monkeypatch.setattr(runtime,'_bridge_sources',lambda:dict(sources))
    old=runtime.snapshot_bridge(profile,store,['pseudolife-mcp','tunnel','shim'])
    sources['shim.py']=b"def run_shim():\n print('new-bridge-implementation')\n"
    new=runtime.snapshot_bridge(profile,store,['pseudolife-mcp','tunnel','shim'])
    assert old['digest'] != new['digest']
    for snapshot,expected in ((old,b'old-bridge-implementation'),(new,b'new-bridge-implementation')):
        result=subprocess.run(snapshot['command'][:2]+['stdio'],env=runtime.child_environment(profile),capture_output=True,timeout=10)
        assert result.returncode == 0
        assert result.stdout.strip() == expected
        runtime.validate_snapshot(store,snapshot)


def test_update_rejects_legacy_running_process_before_stopping(tmp_path,monkeypatch):
    import json
    from pseudolife_memory.tunnel_profiles import private_write
    store,profile=_ready_profile(tmp_path)
    private_write(store.root/'personal.process.json',json.dumps({'pid':1}).encode())
    monkeypatch.setattr(runtime,'ensure_runtime',lambda *a:Path('unused'))
    monkeypatch.setattr(runtime,'status_profile',lambda *a:{'running':True})
    monkeypatch.setattr(runtime,'stop_profile',lambda *a:pytest.fail('legacy process stopped'))
    with pytest.raises(TunnelError,match='explicit stop and start'):
        runtime.update_profile(profile,store,['pseudolife-mcp'])
    assert store.read_key(profile.name)=='synthetic-private-key'


def test_managed_supervisor_refresh_handshake_preserves_supervisor(tmp_path,monkeypatch):
    import json
    import subprocess
    import sys
    import threading
    import time
    store,profile=_ready_profile(tmp_path)
    sources={'shim.py':b"def run_shim(): pass\n",'tunnel_bridge.py':b'# old disposable bridge\n'}
    monkeypatch.setattr(runtime,'_bridge_sources',lambda:dict(sources))
    monkeypatch.setattr(runtime,'ensure_runtime',lambda *a:Path(sys.executable))
    monkeypatch.setattr(runtime,'_wait_ready',lambda *a:True)
    real_popen=subprocess.Popen
    ledgers=[]
    def launch(profile,binary,config,store,*,owned_children=None):
        process=real_popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        identity=runtime._process_identity(process.pid)
        if owned_children is not None:
            owned_children.append((process,identity))
            ledgers.append(owned_children)
        return process,identity
    monkeypatch.setattr(runtime,'_launch_child',launch)
    failures=[]
    def supervise():
        try:
            runtime.run_profile(profile,store,['pseudolife-mcp','tunnel','shim'],binary=Path(sys.executable))
        except Exception as error:
            failures.append(type(error).__name__)
    thread=threading.Thread(target=supervise)
    thread.start()
    try:
        deadline=time.monotonic()+10
        while not runtime.status_profile(profile.name,store)['ready'] and time.monotonic()<deadline:
            time.sleep(0.02)
        first=json.loads((store.root/'personal.process.json').read_text())
        sources['tunnel_bridge.py']=b'# refreshed disposable bridge\n'
        from dataclasses import replace
        store.save(replace(profile,daemon_url='http://127.0.0.1:9876',tunnel_id='tunnel_9876',organization_id='org-example'))
        store.set_key(profile.name,'synthetic-replacement-key')
        result=runtime.update_profile(profile,store,['pseudolife-mcp','tunnel','shim'])
        current=json.loads((store.root/'personal.process.json').read_text())
        assert result['state']=='refreshed'
        assert current['pid'] != first['pid']
        assert current['supervisor']==first['supervisor']
        assert current['snapshot']['digest'] != first['snapshot']['digest']
        assert len(ledgers[-1]) == 1
        assert ledgers[-1][0][0].pid == current['pid']
        assert store.read_key(profile.name)=='synthetic-replacement-key'
        assert runtime.active_launch_identity(profile.name,store)==runtime.expected_launch_identity(store.load(profile.name),store,current['snapshot'])
    finally:
        runtime.stop_profile(profile.name,store)
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not failures

def test_refresh_rechecks_owned_identity_before_stopping(tmp_path,monkeypatch):
    import subprocess
    import sys
    store,profile=_ready_profile(tmp_path)
    snapshot=runtime.snapshot_bridge(profile,store,['pseudolife-mcp','tunnel','shim'])
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        record=runtime._process_identity(child.pid)
        record.update(snapshot=snapshot,binary='unused')
        with runtime.runtime_profile(profile,store,snapshot['command']) as config:
            before=config.read_bytes()
            monkeypatch.setattr(runtime,'_owned_process',lambda record:None)
            with pytest.raises(TunnelError,match='no process was stopped'):
                runtime._refresh_child(profile,store,config,child,record,snapshot)
            assert child.poll() is None
            assert config.read_bytes()==before
            assert store.read_key(profile.name)=='synthetic-private-key'
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_corrupt_immutable_snapshot_is_rejected(tmp_path):
    store,profile=_ready_profile(tmp_path)
    snapshot=runtime.snapshot_bridge(profile,store,['pseudolife-mcp','tunnel','shim'])
    source=Path(snapshot['command'][1]).parent/'shim.py'
    source.write_bytes(b'# altered source')
    with pytest.raises(TunnelError):
        runtime.validate_snapshot(store,snapshot)

def test_active_identity_refuses_saved_key_rotation_and_connection_edits(tmp_path):
    import json
    import os
    from dataclasses import replace
    from pseudolife_memory.tunnel_profiles import private_write
    store,profile=_ready_profile(tmp_path)
    snapshot=runtime.snapshot_bridge(profile,store,['pseudolife-mcp','tunnel','shim'])
    with runtime.runtime_profile(profile,store,snapshot['command']) as config:
        evidence=runtime.capture_launch_identity(profile,store,snapshot,config)
        record=runtime._process_identity(os.getpid())
        record.update(snapshot=snapshot,ready=True,config=str(config),launch_identity=evidence,config_digest=runtime._config_digest(config))
        private_write(store.root/'personal.process.json',json.dumps(record).encode())
        active=runtime.active_launch_identity(profile.name,store)
        assert active==runtime.expected_launch_identity(profile,store,snapshot)
        store.set_key(profile.name,'synthetic-replacement-key')
        assert runtime.active_launch_identity(profile.name,store) != runtime.expected_launch_identity(profile,store,snapshot)
        store.set_key(profile.name,'synthetic-private-key')
        for changed in (replace(profile,daemon_url='http://127.0.0.1:9876'),replace(profile,tunnel_id='tunnel_9876'),replace(profile,organization_id='org-example'),replace(profile,minimum_catalog='full')):
            assert runtime.active_launch_identity(profile.name,store) != runtime.expected_launch_identity(changed,store,snapshot)
        token=Path(profile.token_file)
        private_write(token,b'synthetic-replacement-daemon-token')
        with pytest.raises(TunnelError,match='refresh'):
            runtime.active_launch_identity(profile.name,store)


def test_refresh_adopts_saved_key_and_connection_then_rollback_restores_old(tmp_path,monkeypatch):
    import json
    import subprocess
    import sys
    from dataclasses import replace
    from pseudolife_memory.tunnel_profiles import private_write
    store,profile=_ready_profile(tmp_path)
    snapshot=runtime.snapshot_bridge(profile,store,['pseudolife-mcp','tunnel','shim'])
    real_popen=subprocess.Popen
    launches=[]
    def launch(profile,binary,config,store,*,owned_children=None):
        launches.append(profile)
        child=real_popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        identity=runtime._process_identity(child.pid)
        if owned_children is not None:
            owned_children.append((child,identity))
        return child,identity
    monkeypatch.setattr(runtime,'_launch_child',launch)
    monkeypatch.setattr(runtime,'_wait_ready',lambda process,config,timeout:len(launches)>1)
    with runtime.runtime_profile(profile,store,snapshot['command']) as config:
        original=real_popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        record=runtime._process_identity(original.pid)
        identity=runtime.capture_launch_identity(profile,store,snapshot,config)
        record.update(snapshot=snapshot,ready=True,config=str(config),binary='unused',launch_identity=identity,config_digest=runtime._config_digest(config))
        store.save(replace(profile,daemon_url='http://127.0.0.1:9876',tunnel_id='tunnel_9876',organization_id='org-example'))
        store.set_key(profile.name,'synthetic-replacement-key')
        restored=None
        try:
            restored,newrecord,result=runtime._refresh_child(profile,store,config,original,record,snapshot)
            assert result['rolled_back'] is True
            assert launches[0].daemon_url=='http://127.0.0.1:9876'
            assert launches[1]==profile
            assert newrecord['launch_identity']==identity
            assert runtime.capture_launch_identity(profile,store,snapshot,config)==identity
            assert store.read_key(profile.name)=='synthetic-replacement-key'
            assert Path(config.parent/'key').read_bytes()==b'synthetic-private-key'
        finally:
            if restored and restored.poll() is None:
                restored.terminate();restored.wait(timeout=5)
            if original.poll() is None:
                original.terminate();original.wait(timeout=5)
