"""Retained container transport and the shipped Windows lease caller."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

from .. import core, normalize, producers

_DOCKER = """import base64, json, os, sys
from pathlib import Path
argv = sys.argv[1:]
root = Path(os.environ['HOME'])
with (root / 'docker.jsonl').open('a', encoding='utf-8') as log:
    log.write(json.dumps(argv) + '\\n')
if argv[0] == 'inspect':
    print('true')
    sys.exit(0)
(root / 'docker.stdin').write_bytes(sys.stdin.buffer.read())
sys.stdout.buffer.write(b'container-out\\x00\\xff\\n')
sys.stderr.buffer.write(b'container-err\\x00\\xfe\\n')
sys.exit(23)
"""


def _input(arm, name, value):
    path = arm.home / name
    body = value.encode()
    path.write_bytes(body)
    arm.state.setdefault('inputs', {})[name] = body
    return path


def _remove_inputs(arm):
    # Discard only fixture inputs whose bytes the caller left unchanged.
    for name, body in arm.state.get('inputs', {}).items():
        path = arm.home / name
        if path.read_bytes() != body:
            raise AssertionError(f"fixture input changed: {name}")
        path.unlink()


def _docker_setup(arm):
    script = _input(arm, 'fake_docker.py', _DOCKER)
    if core.WINDOWS:
        wrapper = f'@echo off\r\n"{sys.executable}" "{script}" %*\r\nexit /b %errorlevel%\r\n'
    else:
        wrapper = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'
    path = _input(arm, 'fake_docker.cmd' if core.WINDOWS else 'fake_docker.sh', wrapper)
    if not core.WINDOWS:
        path.chmod(0o700)


def _docker_after(action, tail):
    def after(arm, obs):
        calls = [json.loads(line) for line in (arm.home / 'docker.jsonl').read_text().splitlines()]
        expected = [['inspect', '-f', '{{.State.Running}}', 'pseudolife-mcp-daemon'],
                    ['exec', '-i', '-e', 'PSEUDOLIFE_DAEMON_EXEC=1',
                     'pseudolife-mcp-daemon', 'python', '-m', 'pseudolife_memory.cli',
                     'lease', 'delegate' if action == 'designate' else action, *tail]]
        assert calls == expected, calls
        assert obs['exit'] == 23, obs['exit']
        assert (arm.home / 'docker.stdin').read_bytes() == b'container-input\x00\xff\n'
        assert core.decode(obs, 'stdout') == b'container-out\x00\xff\n'
        assert core.decode(obs, 'stderr').endswith(b'container-err\x00\xfe\n')
        _remove_inputs(arm)
    return after


_LAUNCHER = r"""
$ErrorActionPreference = 'Stop'
while (-not (Test-Path -LiteralPath (Join-Path $env:HOME 'binding.json'))) {
    Start-Sleep -Milliseconds 25
}
$binding = Get-Content -Raw -LiteralPath (Join-Path $env:HOME 'binding.json') | ConvertFrom-Json
. __HELPER__
$script:RepoRoot = Join-Path $env:HOME 'cwd'
function Get-LeaseCli {
    $route = [pscustomobject]@{File=$binding.File; Lead=@($binding.Lead)}
    Add-Content -LiteralPath (Join-Path $env:HOME 'routes.jsonl') -Value ($route | ConvertTo-Json -Compress)
    return $route
}
$server = $second = $hold = $null
$owned = @()
function Record-Owned($process, $role) {
    $command = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)"
    $script:owned += [pscustomobject]@{
        role=$role; pid=$process.Id; started=$process.StartTime.ToUniversalTime().ToString('o'); command=$command.CommandLine
    }
}
function Stop-Owned($process) {
    if ($null -ne $process -and -not $process.HasExited) {
        $record = $owned | Where-Object pid -eq $process.Id
        if ($record.started -ne $process.StartTime.ToUniversalTime().ToString('o')) { throw 'owned start time changed' }
        $process.Kill(); $process.WaitForExit()
    }
}
try {
    $before = Test-GpuLeaseHeld
    $server = Start-Process -FilePath $binding.Sleeper -ArgumentList '-c "import time; time.sleep(60)"' -WindowStyle Hidden -PassThru
    Record-Owned $server 'server'
    Start-GpuLease -ServerPid $server.Id -Purpose 'probe' -ExpectMinutes 2
    $hold = $script:GpuLeaseProcess
    if ($null -eq $hold) { throw 'first lease hold did not start' }
    Record-Owned $hold 'hold'
    $during = Test-GpuLeaseHeld
    $second = Start-Process -FilePath $binding.Sleeper -ArgumentList '-c "import time; time.sleep(60)"' -WindowStyle Hidden -PassThru
    Record-Owned $second 'second'
    Start-GpuLease -ServerPid $second.Id -Purpose 'probe-2'
    $refused = $null -eq $script:GpuLeaseProcess
    $script:GpuLeaseProcess = $hold
    Stop-Owned $server
    Stop-GpuLease -WaitSeconds 20
    $after = Test-GpuLeaseHeld
    [pscustomobject]@{before=$before; during=$during; refused=$refused; after=$after} |
        ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $env:HOME 'lifecycle.json')
    Write-Output ('RESULT=' + ($null -eq $before) + ',' + ($null -ne $during) + ',' + $refused + ',' + ($null -eq $after))
} finally {
    foreach ($process in @($server, $second, $hold)) {
        Stop-Owned $process
    }
    foreach ($record in $owned) {
        $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
        $record | Add-Member -NotePropertyName absent -NotePropertyValue ($null -eq $process)
    }
    ConvertTo-Json -Compress -InputObject $owned | Set-Content -LiteralPath (Join-Path $env:HOME 'owned.json')
    Write-Output 'OWNED_CLEANUP=True'
}
"""


def _launcher_setup(arm):
    producers.write_private(arm.home / 'locks' / 'instance.id', 'abcdef123456\n')
    helper = Path(__file__).resolve().parents[3] / 'evals' / 'qwen_server.ps1'
    script = _LAUNCHER.replace('__HELPER__', "'" + str(helper).replace("'", "''") + "'")
    _input(arm, 'caller.ps1', script)
    arm.state['helper'] = (helper, helper.read_bytes())
    # Keep the host's nondeterministic JIT cache from entering this CLI
    # observation. The exact seeded directory remains in every snapshot.
    (arm.home / 'AppData' / 'Local' / 'Microsoft' / 'PowerShell' /
     'StartupProfileData-NonInteractive').mkdir(parents=True)


def _launcher_binding(arm, proc):
    prefix = proc.args[:proc.args.index('lease')]
    binding = {'File': prefix[0], 'Lead': prefix[1:], 'Sleeper': sys.executable}
    body = json.dumps(binding).encode()
    path = arm.home / 'binding.tmp'
    path.write_bytes(body)
    os.replace(path, arm.home / 'binding.json')
    arm.state['inputs']['binding.json'] = body
    arm.state['binding'] = binding


def _launcher_after(arm, obs):
    from pseudolife_memory.os_lock import probe
    binding = arm.state['binding']
    routes = [json.loads(line) for line in (arm.home / 'routes.jsonl').read_text(encoding='utf-8-sig').splitlines()]
    assert routes == [{'File': binding['File'], 'Lead': binding['Lead']}] * 5, routes
    assert obs['exit'] == 0, core.decode(obs, 'stderr')
    assert probe(arm.home / 'locks' / 'lease-gpu.lock') is False
    owned = json.loads((arm.home / 'owned.json').read_text(encoding='utf-8-sig'))
    assert [p['role'] for p in owned] == ['server', 'hold', 'second']
    assert len({p['pid'] for p in owned}) == 3
    assert all(type(p['pid']) is int and p['pid'] > 0 and p['absent'] for p in owned)
    for process in owned:
        stamp = datetime.fromisoformat(process['started']).timestamp()
        assert obs['window'][0] - 1 <= stamp <= obs['window'][1]
    # Observe the actual owned processes, not just the resolution callback.
    import ctypes
    parser = ctypes.windll.shell32.CommandLineToArgvW
    parser.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parser.restype = ctypes.POINTER(ctypes.c_wchar_p)
    for record in owned:
        count = ctypes.c_int()
        parsed = parser(record['command'], ctypes.byref(count))
        try:
            argv = [parsed[index] for index in range(count.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(ctypes.cast(parsed, ctypes.c_void_p))
        if record['role'] == 'hold':
            expected = [binding['File'], *binding['Lead'], 'lease', 'hold', 'gpu',
                        '--while-pid', str(owned[0]['pid']), '--purpose', 'probe',
                        '--worktree', 'cwd', '--timeout', '0', '--expect', '2m']
            assert argv == expected, argv
            record['command'] = ['<bound-cli>', '<bound-prefix>', *argv[1 + len(binding['Lead']):]]
        else:
            assert argv == [binding['Sleeper'], '-c', 'import time; time.sleep(60)'], argv
            record['command'] = ['<owned-sleeper>', *argv[1:]]
    (arm.home / 'owned.json').write_bytes((json.dumps(owned) + '\n').encode())
    lifecycle = json.loads((arm.home / 'lifecycle.json').read_text(encoding='utf-8-sig'))
    assert lifecycle['before'] is None and lifecycle['after'] is None and lifecycle['refused'] is True
    assert isinstance(lifecycle['during'], str) and 'held' in lifecycle['during']
    # Associate only the route fields validated above; raw golden files must
    # not embed the machine's interpreter or candidate installation paths.
    route_path = arm.home / 'routes.jsonl'
    raw = route_path.read_bytes()
    for name, value, marker in (('File', binding['File'], '<bound-cli>'),
                                 ('Lead', binding['Lead'], ['<bound-prefix>'])):
        old = ('"' + name + '":' + json.dumps(value, separators=(',', ':'))).encode()
        new = ('"' + name + '":' + json.dumps(marker, separators=(',', ':'))).encode()
        assert raw.count(old) == 5
        raw = raw.replace(old, new)
    route_path.write_bytes(raw)
    obs['launcher_binding'] = {'File': '<bound-cli>', 'Lead': ['<bound-prefix>']}
    helper, original = arm.state['helper']
    assert helper.read_bytes() == original, 'shipped helper changed during the arm'
    _remove_inputs(arm)


@normalize.rule('lease-launcher-owned')
def _launcher_owned(obs):
    # These inputs were bound to proc.args before the PowerShell caller read
    # them. Keep every surrounding byte and validate each associated value.
    routes = normalize._file(obs, 'routes.jsonl')
    owned_raw = normalize._file(obs, 'owned.json')
    if routes is None or owned_raw is None:
        return
    binding = obs['launcher_binding']
    want = {'File': binding['File'], 'Lead': binding['Lead']}
    lines = [json.loads(line) for line in routes.decode('utf-8-sig').splitlines()]
    if lines != [want] * 5:
        raise ValueError('launcher CLI routes differ from the bound target')
    owned = json.loads(owned_raw.decode('utf-8-sig'))
    for record in owned:
        if not record['absent'] or not (obs['window'][0] - 1 <= datetime.fromisoformat(record['started']).timestamp() <= obs['window'][1]):
            raise ValueError('launcher owned process identity differs')
    stdout = core.decode(obs, 'stdout')
    server, hold, second = (record['pid'] for record in owned)
    tails = [f'gpu lease hold started (pid {hold}) for server pid {server}'.encode(),
             f'could not take the gpu lease for server pid {second}: another process holds the gpu lock (exit 75); the server runs unleased'.encode()]
    for tail, replacement in zip(tails, (b'<owned-hold-start>', b'<owned-second-refusal>')):
        pattern = re.compile(rb'([0-9]{2}:[0-9]{2}:[0-9]{2}) ' + re.escape(tail))
        found = pattern.search(stdout)
        if found is None or not normalize._local_seconds_in_window(found.group(1), obs['window'], obs['utc_offset']):
            raise ValueError('launcher diagnostic has an unbound PID or clock')
        stdout = pattern.sub(b'<clock> ' + replacement, stdout, count=1)
    normalize._put(obs, 'stdout', stdout)
    for record in owned:
        record['pid'] = '<owned-' + record['role'] + '>'
        record['started'] = '<owned-launch-time>'
        if record['role'] == 'hold':
            argv = record['command']
            index = argv.index('--while-pid') + 1
            if argv[index] != str(server):
                raise ValueError('launcher hold is bound to a different server PID')
            argv[index] = '<owned-server>'
    normalize._set_file(obs, 'owned.json', (json.dumps(owned) + '\n').encode())


def cases():
    env = {'PSEUDOLIFE_DOCKER': '{HOME}/fake_docker.' + ('cmd' if core.WINDOWS else 'sh'),
           'PSEUDOLIFE_MCP_DATABASE_URL': None, 'PSEUDOLIFE_DAEMON_EXEC': None}
    result = []
    for action in ('break', 'delegate', 'designate'):
        tail = ['fixture'] if action == 'break' else ['fixture', '01234567', '--for', '2m']
        result.append(core.Case('operator-container-' + action, ['lease', action, *tail],
                                env=env, stdin=b'container-input\x00\xff\n',
                                setup=_docker_setup, after=_docker_after(action, tail)))
    result.append(core.Case('hold-shipped-windows-launcher',
                            ['lease', 'run', 'launcher', '--no-board', '--', 'pwsh',
                             '-NoProfile', '-NonInteractive', '-File', '{HOME}/caller.ps1'],
                            env={'PSEUDOLIFE_LEASE_LOCK_DIR': '{HOME}/locks',
                                 'POWERSHELL_TELEMETRY_OPTOUT': '1', 'POWERSHELL_UPDATECHECK': 'Off'},
                            setup=_launcher_setup, during=_launcher_binding, after=_launcher_after,
                            timeout=45, rules=('lease-launcher-owned',), platforms=('windows',)))
    return result
