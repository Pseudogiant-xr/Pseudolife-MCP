"""Pinned vendor runtime installation and owned tunnel process lifecycle."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import threading
from functools import wraps
import sys
import subprocess
import tempfile
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile
import uuid
import site

import psutil

from pseudolife_memory.tunnel_profiles import (
    Profile, ProfileStore, TunnelError, checked_path, private_read, private_write, key_expiry,
)

VERSION = '0.0.15'
ASSETS = {
    ('darwin', 'amd64'): '9dcae1e2fb121287e73271edb7b853dda52aa86b7bfca1df91bc275371261bdb',
    ('darwin', 'arm64'): 'b2cae3aa9df45b4c2fe9b1d700ebacce39f9feb6a6b46b86e6499f9a51bf72ff',
    ('linux', 'amd64'): '8c836dc5d68d68b663d9a5c5b28ff9fa780d9f7a3fffb1c306880b8f32fab5f1',
    ('linux', 'arm64'): 'c51bfd883fc22e3445494a03c0179875176564bde470661b308fd83af5d01abb',
    ('windows', 'amd64'): '3b53133a1e24d43f63088d843860cb1701a4c3ed6390de2e19f69089e43bddc1',
    ('windows', 'arm64'): '571e0d59ed9e86d1b105dc34f3267865f654de6968b01efd7c847f0af657d11d',
}


def _platform() -> tuple[str, str]:
    system = platform.system().lower()
    architecture = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    if (system, architecture) not in ASSETS:
        raise TunnelError('vendor tunnel runtime is unavailable for this platform')
    return system, architecture


def _download(url: str) -> bytes:
    # No credential headers are supplied to the vendor's public release URLs.
    try:
        with build_opener().open(Request(url, headers={'User-Agent': 'pseudolife-mcp'}), timeout=60) as response:
            data = response.read(256 * 1024 * 1024 + 1)
        if len(data) > 256 * 1024 * 1024:
            raise TunnelError('vendor archive exceeds the download limit')
        return data
    except OSError:
        raise TunnelError('verified vendor runtime download failed') from None


def _binary_ready(binary: Path) -> bool:
    try:
        result = subprocess.run([str(binary), '--help'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def installed_runtime(store: ProfileStore, version: str = VERSION) -> Path:
    if version != VERSION:
        raise TunnelError('only the checksum-pinned vendor version is supported')
    system, architecture = _platform()
    target = checked_path(store.root / f'tunnel-client-v{version}-{system}-{architecture}')
    binary_name = 'tunnel-client.exe' if system == 'windows' else 'tunnel-client'
    if not target.is_dir():
        raise TunnelError('vendor tunnel runtime is not installed')
    try:
        manifest = json.loads(private_read(target / 'checksums.json'))
        if not isinstance(manifest, dict) or not manifest:
            raise ValueError
        for relative, digest in manifest.items():
            if not isinstance(relative, str) or '/' in relative or '\\' in relative or ':' in relative or relative in {'.', '..'}:
                raise ValueError
            path = checked_path(target / relative)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError
        binary = checked_path(target / binary_name)
        if binary_name not in manifest:
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise TunnelError('installed vendor runtime integrity check failed') from None
    return binary

def ensure_runtime(store: ProfileStore, version: str = VERSION, *, readiness=None) -> Path:
    if version != VERSION:
        raise TunnelError('only the checksum-pinned vendor version is supported')
    store._prepare()
    system, architecture = _platform()
    name = f'tunnel-client-v{version}-{system}-{architecture}'
    target = checked_path(store.root / name)
    binary_name = 'tunnel-client.exe' if system == 'windows' else 'tunnel-client'
    marker = store.root / 'runtime.json'
    if target.exists():
        return installed_runtime(store, version)
    payload = _download(f'https://github.com/openai/tunnel-client/releases/download/v{version}/{name}.zip')
    if hashlib.sha256(payload).hexdigest() != ASSETS[(system, architecture)]:
        raise TunnelError('vendor archive checksum verification failed')
    stage = Path(tempfile.mkdtemp(prefix='.runtime-', dir=store.root))
    if os.name == 'nt':
        from pseudolife_memory.credentials import _secure_windows_file
        _secure_windows_file(stage)
    manifest = {}
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.infolist():
                # Vendor bundles flat executables, notices and manifests; never
                # extract links or archive-selected directories into the host.
                if member.is_dir():
                    continue
                if '/' in member.filename or '\\' in member.filename or ':' in member.filename or member.filename in {'.', '..'} or ((member.external_attr >> 16) & 0o170000) == 0o120000:
                    raise TunnelError('vendor archive has an unsafe member')
                if member.filename in manifest or member.file_size > 256 * 1024 * 1024:
                    raise TunnelError('vendor archive has an unsafe member')
                content = archive.read(member)
                path = stage / member.filename
                private_write(path, content)
                if member.filename in {binary_name, 'cloudflared', 'cloudflared.exe'} and os.name != 'nt':
                    path.chmod(0o700)
                manifest[member.filename] = hashlib.sha256(content).hexdigest()
        if binary_name not in manifest:
            raise TunnelError('vendor archive does not contain the wrapper')
        private_write(stage / 'checksums.json', json.dumps(manifest).encode('utf-8'))
        if not (readiness or _binary_ready)(stage / binary_name):
            raise TunnelError('vendor runtime readiness failed; previous runtime preserved')
        checked_path(target)
        os.replace(stage, target)
        private_write(marker, json.dumps({'version': version, 'directory': name}).encode('utf-8'))
        return target / binary_name
    except (OSError, zipfile.BadZipFile):
        raise TunnelError('vendor runtime installation failed; previous runtime preserved') from None
    finally:
        if stage.exists():
            # Only files created from the verified flat archive are removed.
            for path in stage.iterdir():
                path.unlink()
            stage.rmdir()


def _process_identity(pid: int) -> dict:
    process = psutil.Process(pid)
    return {'pid': pid, 'created': process.create_time(), 'exe': process.exe(), 'argv': process.cmdline()}


def _bridge_sources() -> dict[str, bytes]:
    return {name: checked_path(Path(__file__).parent / name).read_bytes()
            for name in ('tunnel_bridge.py', 'shim.py')}


def snapshot_bridge(profile: Profile, store: ProfileStore, command: list[str]) -> dict:
    """Freeze the bridge and stdio shim, not the shared host dependencies.

    A direct interpreter launcher keeps rollback independent of an updated CLI
    alias. Shared helpers and dependencies retain their installed versions.
    """
    from pseudolife_memory import __version__
    sources = _bridge_sources()
    interpreter = str(checked_path(Path(getattr(sys, '_base_executable', sys.executable)).resolve()))
    dependencies = [str(checked_path(path)) for path in site.getsitepackages()]
    launch = ("import importlib.util,sys\n"
              + 'sys.path[1:1]=' + repr(dependencies) + '\n'
              + "from pathlib import Path\n"
              + "root=Path(__file__).parent\n"
              + "def load(name):\n"
              + " spec=importlib.util.spec_from_file_location('_frozen_'+name,root/(name+'.py'))\n"
              + " module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module);return module\n"
              + "if sys.argv[1]=='stdio':\n load('shim').run_shim()\n"
              + "else:\n"
              + " from pseudolife_memory.tunnel_profiles import ProfileStore\n"
              + " store=ProfileStore(sys.argv[3])\n"
              + " import json,os\n"
              + " from pseudolife_memory.tunnel_profiles import Profile,private_read\n"
              + " launch_path=os.environ.get('PSEUDOLIFE_TUNNEL_LAUNCH_PROFILE')\n"
              + " if not launch_path: raise SystemExit('private launch profile missing; start the managed tunnel')\n"
              + " profile=Profile(**json.loads(private_read(Path(launch_path))))\n"
              + " raise SystemExit(load('tunnel_bridge').run_bridge(store,profile,command=[sys.executable,str(root/'launch.py'),'stdio']))\n").encode('utf-8')
    sources['launch.py'] = launch
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    identity = {'version': __version__, 'interpreter': interpreter, 'hashes': hashes}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    store._prepare()
    parent = checked_path(store.root / '.bridges')
    directory = checked_path(parent / digest)
    for path in (parent, directory):
        if not path.exists():
            path.mkdir(mode=0o700)
            if os.name == 'nt':
                from pseudolife_memory.credentials import _secure_windows_file
                _secure_windows_file(path)
    snapshot = dict(identity, digest=digest, command=[interpreter, str(directory / 'launch.py'), 'bridge', profile.name, str(store.root)])
    manifest = directory / 'manifest.json'
    if not manifest.exists():
        for name, content in sources.items():
            private_write(directory / name, content)
        private_write(manifest, json.dumps(identity | {'digest': digest}).encode())
    return validate_snapshot(store, snapshot)


def validate_snapshot(store: ProfileStore, snapshot: dict) -> dict:
    try:
        digest = snapshot['digest']
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError
        directory = checked_path(store.root / '.bridges' / digest)
        manifest = json.loads(private_read(directory / 'manifest.json'))
        if manifest != {key: snapshot[key] for key in ('version', 'interpreter', 'hashes', 'digest')}:
            raise ValueError
        identity = {key: manifest[key] for key in ('version', 'interpreter', 'hashes')}
        if hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() != digest:
            raise ValueError
        if set(manifest['hashes']) != {'tunnel_bridge.py', 'shim.py', 'launch.py'}:
            raise ValueError
        for name, expected in manifest['hashes'].items():
            if hashlib.sha256(private_read(directory / name, 1024 * 1024)).hexdigest() != expected:
                raise ValueError
        command = snapshot['command']
        if len(command) != 5 or command[:3] != [manifest['interpreter'], str(directory / 'launch.py'), 'bridge'] or command[4] != str(store.root):
            raise ValueError
        from pseudolife_memory.tunnel_profiles import validate_name
        validate_name(command[3])
        if not checked_path(manifest['interpreter']).is_file():
            raise ValueError
        return snapshot
    except (KeyError, TypeError, ValueError, OSError):
        raise TunnelError('immutable tunnel bridge snapshot is unavailable; running profile preserved') from None


def _profile_digest(profile: Profile) -> str:
    if profile.runtime_version not in (None, VERSION):
        raise TunnelError('active tunnel runtime version is unsupported; refresh with the pinned version')
    metadata = {key: getattr(profile, key) for key in ('name', 'daemon_url', 'token_file', 'tunnel_id', 'organization_id', 'minimum_catalog')}
    metadata['runtime_version'] = profile.runtime_version or VERSION
    return hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()


def _launch_binding(profile: Profile, snapshot: dict, key: bytes) -> dict:
    from pseudolife_memory.credentials import CredentialProvider, CredentialError
    try:
        token = CredentialProvider(path=profile.token_file).snapshot().token
        return {'profile_digest': _profile_digest(profile),
                'runtime_key_digest': hashlib.sha256(key).hexdigest(),
                'daemon_token_digest': hashlib.sha256(token.encode()).hexdigest(),
                'bridge_snapshot': snapshot['digest'], 'shim_interpreter': snapshot['interpreter']}
    except CredentialError:
        raise TunnelError('daemon credential reference is unavailable; refresh the tunnel after repair') from None


def expected_launch_identity(profile: Profile, store: ProfileStore, snapshot: dict) -> dict:
    profile.validate()
    validate_snapshot(store, snapshot)
    return _launch_binding(profile, snapshot, store.read_key(profile.name).encode())


def _config_digest(config: Path) -> str:
    return hashlib.sha256(private_read(config)).hexdigest()


def capture_launch_identity(profile: Profile, store: ProfileStore, snapshot: dict, config: Path) -> dict:
    """Read the actual protected launch artifacts; never substitute saved keys."""
    validate_snapshot(store, snapshot)
    try:
        frozen = Profile(**json.loads(private_read(config.parent / 'launch-profile.json')))
        frozen.validate()
        data = json.loads(private_read(config))
        if frozen.name != profile.name or snapshot['command'][3] != frozen.name or _profile_digest(frozen) != _profile_digest(profile):
            raise ValueError
        control = data['control_plane']
        if control.get('base_url') != 'https://api.openai.com' or control['tunnel_id'] != frozen.tunnel_id or control.get('organization_id') != frozen.organization_id or control['api_key'] != 'file:' + str(config.parent / 'key'):
            raise ValueError
        if data['mcp']['commands'][0]['command'] != _command_string(snapshot['command']):
            raise ValueError
        return _launch_binding(frozen, snapshot, private_read(config.parent / 'key', 16384))
    except (ValueError, TypeError, KeyError):
        raise TunnelError('active tunnel launch evidence is invalid; refresh the tunnel') from None


def active_launch_identity(name: str, store: ProfileStore) -> dict:
    """Validated active identity; callers must compare it to expected identity."""
    try:
        record = json.loads(private_read(_record_path(name, store)))
        if not isinstance(record, dict) or record.get('ready') is not True or not record.get('launch_identity') or _owned_process(record) is None:
            raise ValueError
        snapshot = validate_snapshot(store, record['snapshot'])
        config = checked_path(record['config'])
        if config.name != 'profile.yaml' or config.parent.parent != store.root or not config.parent.name.startswith('.launch-' + name + '-'):
            raise ValueError
        frozen = Profile(**json.loads(private_read(config.parent / 'launch-profile.json')))
        identity = capture_launch_identity(frozen, store, snapshot, config)
        if _config_digest(config) != record.get('config_digest') or identity != record['launch_identity']:
            raise ValueError
        return identity
    except (ValueError, TypeError, KeyError, OSError):
        raise TunnelError('active tunnel identity is unavailable or changed; refresh the tunnel before verification') from None


def child_environment(profile: Profile) -> dict[str, str]:
    allowed = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR',
               'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'LANG', 'LC_ALL',
               'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(PSEUDOLIFE_MCP_DAEMON_URL=profile.daemon_url,
               PSEUDOLIFE_MCP_TOKEN_FILE=profile.token_file,
               PSEUDOLIFE_MCP_NO_SPAWN='1', PSEUDOLIFE_WRITER_ID='tunnel')
    return env


def _command_string(command: list[str]) -> str:
    if not command or any(not isinstance(part, str) or '\0' in part or '\n' in part or '\r' in part for part in command):
        raise TunnelError('invalid tunnel MCP command')
    # The vendor parses this command string with Go shellwords on all hosts.
    # POSIX quoting preserves backslashes in Windows paths through that parser.
    return shlex.join(command)


@contextmanager
def runtime_profile(profile: Profile, store: ProfileStore, command: list[str], *, key: str | None = None):
    profile.validate()
    if not profile.consent or profile.state != 'ready':
        raise TunnelError('tunnel launch requires completed explicit opt-in')
    store._prepare()
    stage = Path(tempfile.mkdtemp(prefix=f'.launch-{profile.name}-', dir=store.root))
    if os.name == 'nt':
        from pseudolife_memory.credentials import _secure_windows_file
        _secure_windows_file(stage)
    try:
        key_file = stage / 'key'
        private_write(key_file, (key if key is not None else store.read_key(profile.name)).encode('utf-8'))
        private_write(stage / 'launch-profile.json', json.dumps(asdict(profile)).encode())
        config = {
            'config_version': 1,
            'control_plane': {'base_url': 'https://api.openai.com', 'tunnel_id': profile.tunnel_id,
                              'api_key': 'file:' + str(key_file)},
            'health': {'listen_addr': '127.0.0.1:0', 'url_file': str(stage / 'health.url')},
            'admin_ui': {'open_browser': False},
            'log': {'level': 'info', 'format': 'json'},
            'mcp': {'commands': [{'channel': 'main', 'command': _command_string(command)}]},
        }
        if profile.organization_id:
            config['control_plane']['organization_id'] = profile.organization_id
        target = stage / 'profile.yaml'
        private_write(target, json.dumps(config).encode('utf-8'))
        yield target
    finally:
        for name in ('key', 'profile.yaml', 'health.url', 'launch-profile.json'):
            checked_path(stage / name).unlink(missing_ok=True)
        stage.rmdir()


def _record_path(name: str, store: ProfileStore) -> Path:
    from pseudolife_memory.tunnel_profiles import validate_name
    return checked_path(store.root / (validate_name(name) + '.process.json'))


def _owned_process(record: dict):
    try:
        process = psutil.Process(record['pid'])
        with process.oneshot():
            if process.create_time() != record['created'] or os.path.normcase(process.exe()) != os.path.normcase(record['exe']):
                return None
            if process.cmdline() != record['argv']:
                return None
            if process.status() == psutil.STATUS_ZOMBIE:
                return None
        return process
    except psutil.AccessDenied:
        raise TunnelError('tunnel process identity cannot be inspected safely') from None
    except (psutil.NoSuchProcess, KeyError, TypeError):
        return None


def status_profile(name: str, store: ProfileStore) -> dict:
    path = _record_path(name, store)
    expiry = key_expiry(store.load(name)) if store.profile_path(name).exists() else 'unknown'
    if not path.exists():
        return {'running': False, 'profile': name, 'ready': False, 'key_expiry': expiry}
    try:
        record = json.loads(private_read(path))
    except (ValueError, TypeError):
        raise TunnelError('tunnel process record is invalid') from None
    if not isinstance(record, dict):
        raise TunnelError('tunnel process record is invalid')
    process = _owned_process(record)
    return {'running': process is not None, 'profile': name, 'ready': bool(process and record.get('ready')), 'key_expiry': expiry}


def _ready(config: Path) -> bool:
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    health = config.parent / 'health.url'
    try:
        from pseudolife_memory.tunnel_profiles import validate_url
        url = checked_path(health).read_text(encoding='utf-8').strip()
        validate_url(url, local=True)
        from urllib.parse import urlsplit
        if urlsplit(url).hostname not in {'127.0.0.1', '::1', 'localhost'}:
            return False
        with build_opener(NoRedirect).open(url.rstrip('/') + '/readyz', timeout=1) as response:
            return response.status == 200
    except (OSError, TunnelError, UnicodeError):
        return False


@contextmanager
def profile_lock(name: str, store: ProfileStore, *, purpose: str = 'run'):
    from pseudolife_memory import credentials
    from pseudolife_memory.tunnel_profiles import validate_name
    store._prepare()
    if purpose not in {'run', 'update'}:
        raise TunnelError('invalid tunnel lock purpose')
    suffix = '.lock' if purpose == 'run' else '.update.lock'
    target = checked_path(store.root / (validate_name(name) + suffix))
    try:
        descriptor = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        try:
            if os.name == 'nt':
                credentials._secure_windows_file(target)
            os.write(descriptor, b'0')
        finally:
            os.close(descriptor)
    except FileExistsError:
        pass
    descriptor = os.open(target, os.O_RDWR | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        credentials._validate_file(descriptor)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise TunnelError('this tunnel profile is already running or starting') from None
        yield
    finally:
        os.close(descriptor)


def _cleanup_orphans(name: str, store: ProfileStore) -> None:
    # The profile lifetime lock must be held before removing interrupted launch
    # files. A still-owned wrapper prevents cleanup even if its supervisor died.
    for stage in store.root.glob(f'.launch-{name}-*'):
        checked_path(stage)
        if stage.is_dir():
            members = list(stage.iterdir())
            if any(path.name not in {'key', 'profile.yaml', 'health.url', 'launch-profile.json'} for path in members):
                raise TunnelError('interrupted tunnel launch directory requires inspection')
            for path in members:
                checked_path(path).unlink()
            stage.rmdir()


def _with_profile_lock(function):
    @wraps(function)
    def guarded(profile, store, *args, **kwargs):
        with profile_lock(profile.name, store):
            if status_profile(profile.name, store)['running']:
                raise TunnelError('this tunnel profile is already running')
            _cleanup_orphans(profile.name, store)
            previous = None
            if threading.current_thread() is threading.main_thread():
                previous = signal.getsignal(signal.SIGTERM)
                def interrupted(signum, frame):
                    raise KeyboardInterrupt
                signal.signal(signal.SIGTERM, interrupted)
            try:
                return function(profile, store, *args, **kwargs)
            finally:
                if previous is not None:
                    signal.signal(signal.SIGTERM, previous)
    return guarded


def _terminate_owned_tree(process) -> None:
    try:
        descendants = [(child, child.create_time()) for child in process.children(recursive=True)]
    except psutil.NoSuchProcess:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    for child, created in reversed(descendants):
        try:
            if child.create_time() == created and child.is_running():
                child.terminate()
                try:
                    child.wait(timeout=5)
                except psutil.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass


@_with_profile_lock

def run_profile(profile: Profile, store: ProfileStore, command: list[str], *, binary: Path | None = None, ready_timeout: float = 30) -> int:
    """Foreground supervisor used by opted-in host service managers.

    The protected launch files exist only while this supervisor owns the child.
    """
    if key_expiry(profile) == 'expired':
        raise TunnelError('tunnel runtime key is expired; renew it before starting')
    if status_profile(profile.name, store)['running']:
        raise TunnelError('this tunnel profile is already running')
    binary = binary or ensure_runtime(store, profile.runtime_version or VERSION)
    snapshot = snapshot_bridge(profile, store, command) if 'tunnel' in command and 'shim' in command else None
    if snapshot:
        command = snapshot['command']
    with runtime_profile(profile, store, command) as config:
        record_path = _record_path(profile.name, store)
        process = None
        owned_children = []
        try:
            evidence = capture_launch_identity(profile, store, snapshot, config) if snapshot else None
            process, record = _launch_child(profile, binary, config, store, owned_children=owned_children)
            record.update(ready=False, config=str(config), binary=str(binary), snapshot=snapshot,
                          launch_identity=evidence, config_digest=_config_digest(config),
                          supervisor=_process_identity(os.getpid()), refresh_protocol=1)
            private_write(record_path, json.dumps(record).encode())
            if not _wait_ready(process, config, ready_timeout):
                raise TunnelError('tunnel readiness failed; profile and key preserved')
            record['ready'] = True
            private_write(record_path, json.dumps(record).encode())
            request_path = store.root / (profile.name + '.reload.json')
            while process.poll() is None:
                if request_path.exists():
                    request = {}
                    try:
                        request = json.loads(private_read(request_path))
                        if not isinstance(request, dict) or not re.fullmatch(r'[a-f0-9]{32}', request.get('id', '')):
                            raise ValueError
                        if request.get('target') != {'pid': record['pid'], 'created': record['created']}:
                            raise ValueError
                        candidate = validate_snapshot(store, request['snapshot'])
                        if candidate['command'][3] != profile.name:
                            raise ValueError
                        process, record, result = _refresh_child(profile, store, config, process, record, candidate, owned_children=owned_children)
                        private_write(record_path, json.dumps(record).encode())
                        owned_children[:] = [(child, identity) for child, identity in owned_children if child.poll() is None]
                    except (TunnelError, ValueError, KeyError, TypeError):
                        result = {'state': 'failed', 'changed': False, 'running': process.poll() is None, 'rolled_back': False}
                    private_write(store.root / (profile.name + '.reload.result.json'), json.dumps(dict(result, id=request.get('id') if isinstance(request, dict) else None)).encode())
                    request_path.unlink(missing_ok=True)
                time.sleep(0.1)
            return process.wait()
        except (OSError, psutil.Error):
            raise TunnelError('tunnel process could not be started') from None
        finally:
            # This ledger is shared with the launch factory, before helpers return.
            # It survives cancellation during refresh cleanup or caller handoff.
            for child, identity in owned_children:
                if child.poll() is None:
                    owned = _owned_process(identity)
                    if owned is not None:
                        _terminate_owned_tree(owned)
                        child.wait(timeout=10)
            record_path.unlink(missing_ok=True)


@contextmanager
def _defer_launch_interrupts():
    pending = []
    previous = {}
    if threading.current_thread() is threading.main_thread():
        def defer(signum, frame):
            pending.append((signum, frame))
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.getsignal(number)
            signal.signal(number, defer)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if pending:
            number, frame = pending[0]
            handler = previous[number]
            if callable(handler):
                handler(number, frame)
            elif handler == signal.SIG_DFL:
                signal.raise_signal(number)


def _reap_unregistered_child(process) -> None:
    # This is the acquired, unreaped Popen object, with no competing waiter.
    # Its handle remains the root authority if identity inspection was interrupted.
    descendants = []
    if process.poll() is None:
        try:
            descendants = [(child, child.create_time()) for child in psutil.Process(process.pid).children(recursive=True)]
        except (OSError, psutil.Error):
            pass
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        finally:
            for child, created in reversed(descendants):
                try:
                    if child.create_time() == created and child.is_running():
                        _terminate_owned_tree(child)
                except psutil.NoSuchProcess:
                    pass


def _launch_child(profile: Profile, binary: Path, config: Path, store: ProfileStore, *, owned_children=None):
    log_path = store.root / (profile.name + '.runtime.log')
    if not log_path.exists():
        private_write(log_path, b'')
    with checked_path(log_path).open('ab') as log:
        env = child_environment(profile)
        env['PSEUDOLIFE_TUNNEL_LAUNCH_PROFILE'] = str(config.parent / 'launch-profile.json')
        process = None
        with _defer_launch_interrupts():
            try:
                process = subprocess.Popen([str(binary), 'run', '--profile-file', str(config)], stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, env=env,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                identity = _process_identity(process.pid)
                if owned_children is not None:
                    owned_children.append((process, identity))
            except BaseException:
                if process is not None:
                    _reap_unregistered_child(process)
                raise
    return process, identity


def _wait_ready(process, config: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        if _ready(config):
            return True
        time.sleep(0.1)
    return False


def _refresh_child(profile, store, config, process, record, candidate, *, owned_children=None):
    old_snapshot = validate_snapshot(store, record['snapshot'])
    candidate = validate_snapshot(store, candidate)
    old_config = private_read(config)
    old_key = private_read(config.parent / 'key', 16384)
    old_profile_data = private_read(config.parent / 'launch-profile.json')
    old_profile = Profile(**json.loads(old_profile_data))
    candidate_profile = store.load(profile.name)
    candidate_key = store.read_key(profile.name).encode()
    candidate_expected = expected_launch_identity(candidate_profile, store, candidate)
    data = json.loads(old_config)
    data['mcp']['commands'][0]['command'] = _command_string(candidate['command'])
    data['control_plane']['tunnel_id'] = candidate_profile.tunnel_id
    if candidate_profile.organization_id:
        data['control_plane']['organization_id'] = candidate_profile.organization_id
    else:
        data['control_plane'].pop('organization_id', None)
    old_process = _owned_process(record)
    if old_process is None:
        raise TunnelError('owned tunnel changed before refresh; no process was stopped')
    _terminate_owned_tree(old_process)
    process.wait(timeout=10)
    next_process = None
    owned_children = [] if owned_children is None else owned_children
    handed_off = None
    try:
        checked_path(config.parent / 'health.url').unlink(missing_ok=True)
        private_write(config, json.dumps(data).encode())
        private_write(config.parent / 'key', candidate_key)
        private_write(config.parent / 'launch-profile.json', json.dumps(asdict(candidate_profile)).encode())
        evidence = capture_launch_identity(candidate_profile, store, candidate, config)
        if evidence != candidate_expected:
            raise TunnelError('candidate identity changed before launch; previous launch restored')
        next_process, identity = _launch_child(candidate_profile, Path(record['binary']), config, store, owned_children=owned_children)
        if not _wait_ready(next_process, config, 30):
            raise TunnelError('refreshed tunnel did not become ready')
        refreshed = dict(record, **identity, snapshot=candidate, ready=True, launch_identity=evidence, config_digest=_config_digest(config))
        handed_off = next_process
        return next_process, refreshed, {'state': 'refreshed', 'changed': True, 'running': True, 'rolled_back': False}
    except (TunnelError, OSError, psutil.Error):
        if next_process is not None and next_process.poll() is None:
            _terminate_owned_tree(psutil.Process(next_process.pid))
            next_process.wait(timeout=10)
        checked_path(config.parent / 'health.url').unlink(missing_ok=True)
        private_write(config, old_config)
        private_write(config.parent / 'key', old_key)
        private_write(config.parent / 'launch-profile.json', old_profile_data)
        try:
            restored_process, identity = _launch_child(old_profile, Path(record['binary']), config, store, owned_children=owned_children)
        except (OSError, TunnelError, psutil.Error):
            return process, dict(record, ready=False), {'state': 'failed', 'changed': False, 'running': False, 'rolled_back': False}
        restored = dict(record, **identity, snapshot=old_snapshot, ready=False)
        if _wait_ready(restored_process, config, 30):
            restored['ready'] = True
            handed_off = restored_process
            return restored_process, restored, {'state': 'rolled-back', 'changed': False, 'running': True, 'rolled_back': True}
        _terminate_owned_tree(psutil.Process(restored_process.pid))
        restored_process.wait(timeout=10)
        return restored_process, restored, {'state': 'failed', 'changed': False, 'running': False, 'rolled_back': False}
    finally:
        # Until return, the outer supervisor still owns the terminated old child.
        # A handoff marker is valid only on normal return, never during cancellation.
        for child, identity in owned_children:
            if (child is not handed_off or sys.exc_info()[0] is not None) and child.poll() is None:
                owned = _owned_process(identity)
                if owned is not None:
                    _terminate_owned_tree(owned)
                    child.wait(timeout=10)


def stop_profile(name: str, store: ProfileStore) -> dict:
    path = _record_path(name, store)
    if not path.exists():
        return {'stopped': True, 'profile': name}
    try:
        record = json.loads(private_read(path))
    except (ValueError, TypeError):
        raise TunnelError('tunnel process record is invalid') from None
    if not isinstance(record, dict):
        raise TunnelError('tunnel process record is invalid')
    process = _owned_process(record)
    if process:
        try:
            _terminate_owned_tree(process)
        except psutil.Error:
            raise TunnelError('owned tunnel process could not be stopped') from None
    return {'stopped': True, 'profile': name}


def stable_command() -> list[str]:
    """Find the installed launcher rather than binding persistence to a venv."""
    candidates = [shutil.which('pseudolife-mcp'), str(Path.home() / '.pseudolife-mcp' / 'bin' / ('pseudolife-mcp.exe' if os.name == 'nt' else 'pseudolife-mcp'))]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and not ({p.lower() for p in Path(candidate).parts} & {'.venv', 'venv', 'site-packages'}):
            return [str(checked_path(candidate))]
    if sys.prefix == sys.base_prefix:
        return [str(checked_path(sys.executable)), '-m', 'pseudolife_memory.cli']
    raise TunnelError('a stable installed pseudolife-mcp launcher is required')


def start_profile(profile: Profile, store: ProfileStore, command: list[str], *, ready_timeout: float = 45) -> dict:
    """Launch the installed CLI supervisor and verify its first ready output."""
    profile.validate()
    if key_expiry(profile) == 'expired':
        raise TunnelError('tunnel runtime key is expired; renew it before starting')
    if not profile.consent or profile.state != 'ready':
        raise TunnelError('tunnel launch requires completed explicit opt-in')
    existing = status_profile(profile.name, store)
    if existing['running']:
        record = json.loads(private_read(_record_path(profile.name, store)))
        if not isinstance(record, dict) or not record.get('snapshot'):
            raise TunnelError('running tunnel predates safe refresh; perform an explicit stop and start')
        try:
            active = active_launch_identity(profile.name, store)
        except TunnelError:
            active = None
        expected = expected_launch_identity(profile, store, record['snapshot'])
        if active != expected:
            result = update_profile(profile, store, command)
            if result.get('state') != 'refreshed':
                raise TunnelError('replacement configuration was not adopted; previous launch preserved; inspect tunnel status')
            return status_profile(profile.name, store)
        return existing
    store._prepare()
    ensure_runtime(store, profile.runtime_version or VERSION)
    launch = stable_command() + ['tunnel', 'run', '--profile', profile.name, '--profile-dir', str(store.root)]
    path = store.root / (profile.name + '.supervisor.log')
    private_write(path, b'')
    supervisor = None
    try:
        with path.open('ab') as log:
            supervisor = subprocess.Popen(launch, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                          env=child_environment(profile), start_new_session=os.name != 'nt',
                                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + ready_timeout
        while supervisor.poll() is None and time.monotonic() < deadline:
            status = status_profile(profile.name, store)
            if status['ready']:
                return status
            time.sleep(0.1)
        if supervisor.poll() is None:
            stop_profile(profile.name, store)
            supervisor.terminate()
            try:
                supervisor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=10)
        raise TunnelError('tunnel supervisor did not become ready; profile and key preserved')
    except BaseException as error:
        if supervisor is not None and supervisor.poll() is None:
            stop_profile(profile.name, store)
            supervisor.terminate()
            try:
                supervisor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=10)
        if isinstance(error, OSError):
            raise TunnelError('tunnel supervisor could not be started') from None
        raise


def doctor_profile(profile: Profile, store: ProfileStore, command: list[str], *, key: str | None = None, binary: Path | None = None) -> dict:
    if binary is None:
        try:
            binary = installed_runtime(store, profile.runtime_version or VERSION)
        except TunnelError:
            return {'ok': False, 'checks': [{'id': 'runtime_installed', 'status': 'FAIL'}], 'cloud_verified': False}
    try:
        with runtime_profile(profile, store, command, key=key) as config:
            result = subprocess.run([str(binary), 'doctor', '--profile-file', str(config), '--json'],
                                    env=child_environment(profile), stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=30,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or not isinstance(data.get('checks', []), list):
            raise ValueError
        checks = [{'id': item['id'], 'status': item['status']} for item in data.get('checks', [])
                  if isinstance(item, dict) and isinstance(item.get('id'), str)
                  and re.fullmatch(r'[a-z0-9_]{1,80}', item['id'])
                  and item.get('status') in {'PASS', 'FAIL', 'WARN', 'SKIP'}]
        return {'ok': result.returncode == 0 and data.get('result') == 'ok', 'checks': checks,
                'cloud_verified': False}
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        raise TunnelError('vendor doctor did not return a usable sanitized result') from None


def verify_key(profile: Profile, store: ProfileStore, key: str, command: list[str]) -> bool:
    from pseudolife_memory.credentials import _decode_token, CredentialError
    try:
        _decode_token(key.encode('utf-8'))
    except (AttributeError, UnicodeError, CredentialError):
        raise TunnelError('invalid tunnel private key') from None
    # This checks configuration/key readability, not cloud identity. The remote
    # challenge receipt remains the only end-to-end cloud evidence.
    binary = ensure_runtime(store, profile.runtime_version or VERSION)
    return doctor_profile(profile, store, command, key=key, binary=binary)['ok']


def _request_refresh(profile: Profile, store: ProfileStore, snapshot: dict) -> dict:
    request_path = store.root / (profile.name + '.reload.json')
    response_path = store.root / (profile.name + '.reload.result.json')
    if request_path.exists():
        raise TunnelError('a tunnel refresh is pending; inspect tunnel status before retrying')
    request_id = uuid.uuid4().hex
    record = json.loads(private_read(_record_path(profile.name, store)))
    target = {'pid': record['pid'], 'created': record['created']}
    private_write(request_path, json.dumps({'id': request_id, 'snapshot': snapshot, 'target': target}).encode())
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        if response_path.exists():
            response = json.loads(private_read(response_path))
            if isinstance(response, dict) and response.get('id') == request_id:
                return {key: response[key] for key in ('state', 'changed', 'running', 'rolled_back')}
        if not _record_path(profile.name, store).exists():
            raise TunnelError('tunnel stopped before refresh completion; inspect status and start explicitly')
        time.sleep(0.1)
    # Leave a pending request visible; timeout does not imply that the refresh
    # failed or authorize killing the service-owned supervisor.
    raise TunnelError('tunnel refresh outcome is pending; inspect tunnel status before retrying')


def update_profile(profile: Profile, store: ProfileStore, command: list[str]) -> dict:
    """Refresh a running bridge inside its service-owned supervisor.

    Idle profiles stay idle. Runtime selection remains checksum-pinned; no
    upstream latest version or credential rotation occurs during client updates.
    """
    if not status_profile(profile.name, store)['running']:
        return {'profile': profile.name, 'version': VERSION, 'changed': False, 'running': False, 'state': 'unchanged'}
    ensure_runtime(store, profile.runtime_version or VERSION)
    with profile_lock(profile.name, store, purpose='update'):
        record = json.loads(private_read(_record_path(profile.name, store)))
        if not isinstance(record, dict) or not record.get('snapshot') or not record.get('launch_identity') or record.get('refresh_protocol') != 1:
            raise TunnelError('running tunnel predates safe refresh; perform an explicit stop and start')
        if _owned_process(record) is None or _owned_process(record.get('supervisor', {})) is None:
            raise TunnelError('owned tunnel identity changed; running profile preserved')
        validate_snapshot(store, record['snapshot'])
        snapshot = snapshot_bridge(profile, store, command)
        result = _request_refresh(profile, store, snapshot)
        return dict(result, profile=profile.name, version=VERSION)
