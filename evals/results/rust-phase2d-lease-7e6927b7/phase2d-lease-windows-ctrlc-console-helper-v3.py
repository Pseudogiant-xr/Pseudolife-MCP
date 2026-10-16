"""One owned hidden-console CTRL_C_EVENT observation; no comparator policy."""
import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import pathlib
import subprocess
import time
import traceback

import psutil

p = argparse.ArgumentParser()
p.add_argument('--config', type=pathlib.Path, required=True)
p.add_argument('--out', type=pathlib.Path, required=True)
args = p.parse_args()
config = json.loads(args.config.read_text(encoding='utf-8'))
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
user = ctypes.WinDLL('user32', use_last_error=True)
kernel.GetConsoleWindow.argtypes = []
kernel.GetConsoleWindow.restype = wintypes.HWND
kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
kernel.GetConsoleProcessList.restype = wintypes.DWORD
kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
user.IsWindowVisible.argtypes = [wintypes.HWND]
user.IsWindowVisible.restype = wintypes.BOOL

def members():
    buffer = (wintypes.DWORD * 64)()
    count = kernel.GetConsoleProcessList(buffer, len(buffer))
    assert 0 < count <= len(buffer), 'console membership unavailable'
    return sorted(buffer[:count])

def tick(event, **fields):
    print(json.dumps(dict(event=event, **fields)), flush=True)

def same_process(identity):
    try:
        return psutil.Process(identity['pid']).create_time() == identity['create_time']
    except psutil.NoSuchProcess:
        return False

def identity(pid):
    process = psutil.Process(pid)
    return dict(pid=pid, create_time=process.create_time(), parent_pid=process.ppid())

result = dict(status='started', helper_pid=psutil.Process().pid,
              config_sha256=hashlib.sha256(args.config.read_bytes()).hexdigest(),
              normalization=[], ctrl_event=0, process_group_id=0)
target = None
child_identity = None
actual_target_identity = None
try:
    window = kernel.GetConsoleWindow()
    result['console_window_present'] = bool(window)
    result['console_window_visible'] = bool(user.IsWindowVisible(window))
    result['initial_console_members'] = members()
    assert window and not result['console_window_visible'], 'dedicated console is not hidden'
    assert result['initial_console_members'] == [result['helper_pid']], 'console not isolated'
    tick('hidden-owned-console-verified', helper_pid=result['helper_pid'])
    # No NEW_PROCESS_GROUP or NO_WINDOW: target inherits this new console and
    # the normal Ctrl-C disposition. Helper immunity is enabled only after the
    # target has started its signal-resistant child.
    target = subprocess.Popen(config['argv'], cwd=config['cwd'], env=config['environment'],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, creationflags=0)
    result['target_identity'] = identity(target.pid)
    ready = pathlib.Path(config['ready'])
    deadline = time.monotonic() + 20
    while not ready.exists() and target.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    assert ready.exists(), 'target child readiness missing'
    result['ready_bytes_b64'] = base64.b64encode(ready.read_bytes()).decode()
    child = json.loads(ready.read_bytes())
    child_identity = identity(child['pid'])
    result['child_identity'] = child_identity
    assert child_identity['parent_pid'] == child['parent_pid'], 'child readiness mismatch'
    actual_target_identity = identity(child['parent_pid'])
    result['actual_target_identity'] = actual_target_identity
    assert actual_target_identity['pid'] == target.pid or actual_target_identity['parent_pid'] == target.pid, 'target ancestry mismatch'
    result['before_signal_console_members'] = members()
    assert result['before_signal_console_members'] == sorted(set([result['helper_pid'], target.pid, actual_target_identity['pid'], child['pid']])), 'foreign console process'
    if config.get('board_probe'):
        import httpx
        environment=config['environment']
        token=pathlib.Path(environment['PSEUDOLIFE_MCP_TOKEN_FILE']).read_text().strip()
        response=httpx.post(environment['PSEUDOLIFE_MCP_DAEMON_URL']+'/api/coordination/leases',json={'name':'ctrlc-proof'},headers={'Authorization':'Bearer '+token},timeout=10,trust_env=False,follow_redirects=False)
        result['board_before_signal']=dict(status_code=response.status_code,body_b64=base64.b64encode(response.content).decode(),body_sha256=hashlib.sha256(response.content).hexdigest())
        payload=response.json()
        result['board_lease_present_before_signal']=response.status_code==200 and payload.get('truncated') is False and len(payload['leases'])==1 and payload['leases'][0]['name']=='ctrlc-proof' and payload['leases'][0].get('holder') is not None
        assert result['board_lease_present_before_signal'], 'owned board lease absent before signal'
    assert kernel.SetConsoleCtrlHandler(None, True), 'helper immunity failed'
    result['helper_immunity_after_child_ready'] = True
    result['signal_time_unix'] = time.time()
    assert kernel.GenerateConsoleCtrlEvent(0, 0), 'CTRL_C_EVENT dispatch failed'
    result['generate_console_ctrl_event_succeeded'] = True
    tick('actual-ctrl-c-event-sent', target_pid=target.pid, child_pid=child['pid'])
    stdout, stderr = target.communicate(timeout=30)
    result.update(exit_code=target.returncode, stdout_b64=base64.b64encode(stdout).decode(),
                  stderr_b64=base64.b64encode(stderr).decode(),
                  elapsed_after_signal=time.time()-result['signal_time_unix'],
                  child_absent_before_helper_cleanup=not same_process(child_identity),
                  target_absent_before_helper_cleanup=not same_process(result['target_identity']) and not same_process(actual_target_identity),
                  after_target_console_members=members())
    result['status'] = 'observed'
except Exception as error:
    result.update(status='failed', failure_type=type(error).__name__,
                  failure_assertion=str(error) if isinstance(error, AssertionError) else 'exception detail withheld',
                  failure_frames=traceback.format_tb(error.__traceback__))
finally:
    residual = []
    for owned in (child_identity, actual_target_identity, result.get('target_identity')):
        if owned and same_process(owned):
            residual.append(owned['pid'])
            process = psutil.Process(owned['pid'])
            process.kill()
            process.wait(timeout=10)
    if target is not None and target.poll() is None:
        target.wait(timeout=10)
    result['helper_cleanup_killed_pids'] = residual
    result['owned_pids_absent_after_helper_cleanup'] = all(not same_process(owned) for owned in (child_identity, actual_target_identity, result.get('target_identity')) if owned)
    args.out.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    tick('console-observation-saved', status=result['status'], residual_count=len(residual))
raise SystemExit(0 if result['status'] == 'observed' else 1)
