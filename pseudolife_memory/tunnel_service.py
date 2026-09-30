"""Owned, explicitly opted-in user services for the optional tunnel runtime."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import locale
import os
from pathlib import Path
import plistlib
import platform as host_platform
import re
import subprocess
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from pseudolife_memory.tunnel_profiles import Profile, ProfileStore, TunnelError, checked_path, private_read, private_write


def _run(command: list[str], *, check: bool = True):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        raise TunnelError('host service manager is unavailable') from None
    if check and result.returncode:
        raise TunnelError('host service manager rejected the operation')
    return result


def _text(data: bytes) -> str:
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    try:
        return data.decode('utf-8-sig')
    except UnicodeError:
        try:
            return data.decode('oem' if os.name == 'nt' else locale.getpreferredencoding(False))
        except (UnicodeError, LookupError):
            raise TunnelError('host service output cannot be decoded') from None


def _identity(system: str) -> str:
    if system == 'windows':
        # SID, not a localized account name or the elevated token's default group.
        found = re.search(rb'S-1-\d+(?:-\d+)+', _run(['whoami.exe', '/user', '/fo', 'csv', '/nh']).stdout)
        if not found:
            raise TunnelError('current service owner cannot be determined')
        return found.group().decode('ascii')
    return str(os.geteuid())


def _stable(command: list[str]) -> None:
    if not command or any(not isinstance(part, str) or any(ord(c) < 32 for c in part) for part in command) or not Path(command[0]).is_absolute():
        raise TunnelError('service command requires an absolute stable installed launcher')
    parts = {part.lower() for part in Path(command[0]).parts}
    if parts & {'.venv', 'venv', 'virtualenv', 'site-packages'}:
        raise TunnelError('service command requires a stable installed launcher')
    checked_path(command[0])


def _owner(profile: Profile, store: ProfileStore) -> str:
    return 'PseudolifeTunnelOwner:' + hashlib.sha256((str(store.root) + '\0' + profile.name).encode('utf-8')).hexdigest()


def render_service(profile: Profile, store: ProfileStore, command: list[str], *, platform: str | None = None, system_service: bool = False) -> str:
    profile.validate()
    if any(ord(c) < 32 or ord(c) == 127 for c in str(store.root)):
        raise TunnelError('service profile directory contains invalid characters')
    if 'shim' in command:
        from pseudolife_memory.tunnel_runtime import stable_command
        command = stable_command() + ['tunnel', 'run', '--profile', profile.name, '--profile-dir', str(store.root)]
    _stable(command)
    system = platform or host_platform.system().lower()
    if system_service and system != 'linux':
        raise TunnelError('system service opt-in applies only to Linux')
    name = 'pseudolife-tunnel-' + profile.name
    marker = _owner(profile, store)
    if system == 'windows':
        identity = escape(_identity(system))
        executable = escape(command[0])
        arguments = escape(subprocess.list2cmdline(command[1:]))
        return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Source>{marker}</Source></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{identity}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{identity}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><Hidden>true</Hidden><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
  <Actions Context="Author"><Exec><Command>{executable}</Command><Arguments>{arguments}</Arguments></Exec></Actions>
</Task>
'''
    if system == 'linux':
        def quoted(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
        launch = ' '.join(quoted(part) for part in command)
        hardening = ''
        wanted = 'default.target'
        if system_service:
            wanted = 'multi-user.target'
            root = quoted(str(store.root)).replace('$$', '$')
            hardening = f'User=root\nProtectSystem=strict\nProtectHome=read-only\nPrivateTmp=yes\nNoNewPrivileges=yes\nRestrictSUIDSGID=yes\nReadWritePaths={root}\nWorkingDirectory={root}\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'
        return f'''# {marker}
[Unit]
Description=Optional Pseudolife MCP tunnel ({profile.name})
After=network-online.target
[Service]
Type=simple
{hardening}ExecStart={launch}
Restart=on-failure
RestartSec=5
UMask=0077
[Install]
WantedBy={wanted}
'''
    if system == 'darwin':
        return plistlib.dumps({'Label': name, 'PseudolifeTunnelOwner': marker, 'ProgramArguments': command,
                               'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False},
                               'ProcessType': 'Background'}, fmt=plistlib.FMT_XML).decode('utf-8')
    raise TunnelError('tunnel persistence is unsupported on this platform')


def _system_directory():
    return Path('/etc/systemd/system')


def _systemctl(system):
    return ['systemctl', '--system' if system == 'linux-system' else '--user']


def _context(profile: Profile, store: ProfileStore, *, system_service=False):
    profile.validate()
    checked_path(store.root)
    system = host_platform.system().lower()
    name = 'pseudolife-tunnel-' + profile.name
    identity = _identity(system)
    if system_service and system != 'linux':
        raise TunnelError('system service opt-in applies only to Linux')
    if system == 'windows':
        target = checked_path(store.root / (name + '.xml'))
    elif system == 'linux':
        if system_service:
            if identity != '0':
                raise TunnelError('Linux system tunnel persistence requires root and explicit system opt-in')
            system = 'linux-system'
            target = checked_path(_system_directory() / (name + '.service'))
        else:
            if identity == '0':
                raise TunnelError('root Linux user persistence is unsupported; explicitly select --system-service (existing root privilege) or install as a non-root user with --linger')
            target = checked_path(Path.home() / '.config' / 'systemd' / 'user' / (name + '.service'))
    elif system == 'darwin':
        target = checked_path(Path.home() / 'Library' / 'LaunchAgents' / (name + '.plist'))
    else:
        raise TunnelError('tunnel persistence is unsupported on this platform')
    record = checked_path(store.root / (name + '.service-owner'))
    return system, name, identity, target, record


def _snapshot(system, name, identity, target):
    if system == 'windows':
        inventory = _text(_run(['schtasks.exe', '/Query', '/FO', 'CSV', '/NH']).stdout)
        present = any(row and row[0].casefold() == ('\\' + name).casefold() for row in csv.reader(io.StringIO(inventory)))
        if not present:
            return {'present': False, 'active': False, 'enabled': False}
        xml = _text(_run(['schtasks.exe', '/Query', '/TN', name, '/XML']).stdout)
        script = "$s=New-Object -ComObject Schedule.Service; $s.Connect(); [int]$s.GetFolder('\\').GetTask('" + name + "').State"
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        state = _text(_run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]).stdout).strip()
        if state not in {'1', '2', '3', '4'}:
            raise TunnelError('host task state cannot be verified')
        return {'present': True, 'active': state in {'2', '4'}, 'enabled': state != '1', 'xml': xml}
    if system.startswith('linux'):
        output = _text(_run(_systemctl(system) + ['show', name + '.service', '--property=LoadState,FragmentPath,ActiveState,UnitFileState']).stdout)
        values = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if values.get('LoadState') == 'not-found':
            return {'present': False, 'active': False, 'enabled': False}
        if values.get('LoadState') != 'loaded':
            raise TunnelError('host service state cannot be verified')
        return {'present': True, 'active': values.get('ActiveState') in {'active', 'activating'},
                'enabled': values.get('UnitFileState') == 'enabled', 'path': values.get('FragmentPath')}
    inventory = _text(_run(['launchctl', 'list']).stdout)
    present = any(line.split() and line.split()[-1] == name for line in inventory.splitlines())
    if not present:
        return {'present': False, 'active': False, 'enabled': False}
    output = _text(_run(['launchctl', 'print', f'gui/{identity}/{name}']).stdout)
    paths = [line.strip().removeprefix('path = ') for line in output.splitlines() if line.strip().startswith('path = ')]
    return {'present': True, 'active': 'state = running' in output, 'enabled': True,
            'path': paths[0] if len(paths) == 1 else None}


def _owned(profile, store, context, snapshot):
    system, name, identity, target, record = context
    if not target.exists() and not record.exists() and not snapshot['present']:
        return None
    if not target.exists() or not record.exists():
        raise TunnelError('existing service is not owned by this tunnel profile')
    payload = private_read(target)
    try:
        metadata = json.loads(private_read(record))
        if metadata != {'owner': _owner(profile, store), 'identity': identity, 'target': str(target),
                        'sha256': hashlib.sha256(payload).hexdigest()}:
            raise ValueError
        if _owner(profile, store) not in _text(payload):
            raise ValueError
        if snapshot['present']:
            if system == 'windows':
                root = ElementTree.fromstring(snapshot['xml'])
                ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
                if root.findtext('t:RegistrationInfo/t:Source', namespaces=ns) != metadata['owner'] or root.findtext('t:Principals/t:Principal/t:UserId', namespaces=ns) != identity:
                    raise ValueError
                # A changed registered action is foreign even if its marker survived.
                local = ElementTree.fromstring(_text(payload))
                for field in ('Command', 'Arguments'):
                    query = 't:Actions/t:Exec/t:' + field
                    if root.findtext(query, namespaces=ns) != local.findtext(query, namespaces=ns):
                        raise ValueError
            elif snapshot.get('path') != str(target):
                raise ValueError
    except (ValueError, TypeError, ElementTree.ParseError, UnicodeError):
        raise TunnelError('existing service is not owned by this tunnel profile') from None
    return payload


def _linger(identity):
    value = _text(_run(['loginctl', 'show-user', identity, '--property=Linger', '--value']).stdout).strip()
    if value not in {'yes', 'no'}:
        raise TunnelError('Linux linger state cannot be verified')
    return value == 'yes'


def _availability(system, identity):
    if system == 'linux-system':
        return 'boot and logout (system service with existing root privilege)'
    if system == 'linux' and _linger(identity):
        return 'boot and logout (user linger enabled)'
    return 'logged-in user session; unavailable after logout'


def _register(system, name, identity, target, *, replace=False):
    if system == 'windows':
        _run(['schtasks.exe', '/Create', '/TN', name, '/XML', str(target), '/F'])
        _run(['schtasks.exe', '/Run', '/TN', name])
    elif system.startswith('linux'):
        _run(_systemctl(system) + ['daemon-reload'])
        _run(_systemctl(system) + ['enable', '--now', name + '.service'])
        if replace:
            _run(_systemctl(system) + ['restart', name + '.service'])
    else:
        if replace:
            _run(['launchctl', 'bootout', f'gui/{identity}/{name}'])
        _run(['launchctl', 'bootstrap', f'gui/{identity}', str(target)])


def _unregister(system, name, identity):
    if system == 'windows':
        _run(['schtasks.exe', '/End', '/TN', name], check=False)
        _run(['schtasks.exe', '/Delete', '/TN', name, '/F'])
    elif system.startswith('linux'):
        _run(_systemctl(system) + ['disable', '--now', name + '.service'])
    else:
        _run(['launchctl', 'bootout', f'gui/{identity}/{name}'])


def install_service(profile: Profile, store: ProfileStore, command: list[str], *, persistent: bool = False, linger: bool = False, system_service: bool = False) -> dict:
    if not persistent or not profile.autostart_consent:
        raise TunnelError('persistent tunnel access requires explicit opt-in')
    context = _context(profile, store, system_service=system_service)
    system, name, identity, target, record = context
    if linger and system != 'linux':
        raise TunnelError('linger opt-in applies only to Linux user services')
    if 'shim' in command:
        from pseudolife_memory.tunnel_runtime import stable_command
        command = stable_command() + ['tunnel', 'run', '--profile', profile.name, '--profile-dir', str(store.root)]
    content = render_service(profile, store, command, system_service=system_service)
    payload = content.encode('utf-16' if system == 'windows' else 'utf-8')
    snapshot = _snapshot(system, name, identity, target)
    previous = _owned(profile, store, context, snapshot)
    was_linger = _linger(identity) if system == 'linux' else False
    previous_record = private_read(record) if record.exists() else None
    store._prepare()
    changed_linger = False
    try:
        private_write(target, payload)
        metadata = {'owner': _owner(profile, store), 'identity': identity, 'target': str(target),
                    'sha256': hashlib.sha256(payload).hexdigest()}
        private_write(record, json.dumps(metadata).encode('utf-8'))
        if linger and not was_linger:
            changed_linger = True
            _run(['loginctl', 'enable-linger', identity])
            if not _linger(identity):
                raise TunnelError('Linux linger enablement could not be verified')
        if not (snapshot['present'] and snapshot['enabled'] and previous == payload):
            _register(system, name, identity, target, replace=snapshot['present'] and previous != payload)
        elif not snapshot['active']:
            if system == 'windows':
                _run(['schtasks.exe', '/Run', '/TN', name])
            elif system.startswith('linux'):
                _run(_systemctl(system) + ['start', name + '.service'])
            else:
                _run(['launchctl', 'kickstart', f'gui/{identity}/{name}'])
        current = _snapshot(system, name, identity, target)
        _owned(profile, store, context, current)
        if not current['present'] or not current['enabled']:
            raise TunnelError('host service registration could not be verified')
        availability = _availability(system, identity)
    except (OSError, TunnelError):
        recovered = True
        try:
            # A previously stopped definition must not retain a newly loaded job.
            if not snapshot['present']:
                current = _snapshot(system, name, identity, target)
                if current['present']:
                    _owned(profile, store, context, current)
                    _unregister(system, name, identity)
            if previous is not None:
                private_write(target, previous)
                private_write(record, previous_record)
                if snapshot['present']:
                    _register(system, name, identity, target, replace=system.startswith('linux') or (system == 'darwin' and _snapshot(system, name, identity, target)['present']))
                    if not snapshot['active']:
                        _stop(system, name, identity)
                    if system == 'windows' and not snapshot['enabled']:
                        _run(['schtasks.exe', '/Change', '/TN', name, '/DISABLE'])
                    if system.startswith('linux') and not snapshot['enabled']:
                        _run(_systemctl(system) + ['disable', name + '.service'])
            else:
                target.unlink(missing_ok=True)
                record.unlink(missing_ok=True)
                if system.startswith('linux'):
                    _run(_systemctl(system) + ['daemon-reload'])
        except (OSError, TunnelError):
            recovered = False
        if changed_linger:
            try:
                _run(['loginctl', 'disable-linger', identity])
            except TunnelError:
                recovered = False
        raise TunnelError('service installation failed; ' + ('previous state restored' if recovered else 'rollback incomplete; inspect service status')) from None
    return {'installed': True, 'profile': profile.name, 'availability': availability}


def status_service(profile: Profile, store: ProfileStore, *, system_service: bool = False) -> dict:
    context = _context(profile, store, system_service=system_service)
    system, name, identity, target, record = context
    snapshot = _snapshot(system, name, identity, target)
    _owned(profile, store, context, snapshot)
    return {'installed': snapshot['present'], 'profile': profile.name, 'active': snapshot['active'],
            'availability': _availability(system, identity)}


def _stop(system, name, identity):
    if system == 'windows':
        _run(['schtasks.exe', '/End', '/TN', name])
    elif system.startswith('linux'):
        _run(_systemctl(system) + ['stop', name + '.service'])
    else:
        # Disable the job for this login session; KeepAlive would restart kill.
        _run(['launchctl', 'bootout', f'gui/{identity}/{name}'])


def stop_service(profile: Profile, store: ProfileStore, *, system_service: bool = False) -> dict:
    context = _context(profile, store, system_service=system_service)
    system, name, identity, target, record = context
    snapshot = _snapshot(system, name, identity, target)
    _owned(profile, store, context, snapshot)
    if snapshot['present'] and snapshot['active'] is not False:
        _stop(system, name, identity)
    return {'stopped': True, 'profile': profile.name}


def remove_service(profile: Profile, store: ProfileStore, *, system_service: bool = False) -> dict:
    context = _context(profile, store, system_service=system_service)
    system, name, identity, target, record = context
    snapshot = _snapshot(system, name, identity, target)
    _owned(profile, store, context, snapshot)
    if snapshot['present']:
        _unregister(system, name, identity)
    target.unlink(missing_ok=True)
    record.unlink(missing_ok=True)
    if system.startswith('linux'):
        _run(_systemctl(system) + ['daemon-reload'])
    # Linger is a user-wide setting: removing one profile never changes it.
    return {'removed': True, 'profile': profile.name}
