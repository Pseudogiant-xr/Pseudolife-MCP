"""Transparent tunnel-only stdio bridge and payload-free cloud call receipts."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from mcp.types import CallToolResult
from pydantic import ValidationError

from pseudolife_memory.tunnel_profiles import Profile, ProfileStore, TunnelError, private_read, private_write

TOOLS = ('memory_search', 'memory_agents')


def identity(store: ProfileStore, profile: Profile) -> str:
    from pseudolife_memory import __version__
    from pseudolife_memory.tunnel_runtime import VERSION
    metadata = asdict(profile)
    for key in ('state', 'cloud_verification_file', 'autostart_consent', 'runtime_key_expires_at'):
        metadata.pop(key, None)
    metadata.update({'package_version': __version__, 'vendor_version': profile.runtime_version or VERSION})
    metadata.update(_snapshot_identity(store, profile))
    # The digest remains private and binds proof to credential rotation as well
    # as the selected daemon, tunnel, organization and executable revision.
    payload = json.dumps(metadata, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _snapshot_identity(store: ProfileStore, profile: Profile) -> dict:
    from pseudolife_memory import tunnel_runtime as runtime
    active = runtime.active_launch_identity(profile.name, store)
    try:
        record = json.loads(private_read(store.root / (profile.name + '.process.json')))
        snapshot = runtime.validate_snapshot(store, record['snapshot'])
        expected = runtime.expected_launch_identity(profile, store, snapshot)
    except (ValueError, TypeError, KeyError):
        raise TunnelError('active tunnel launch identity is unavailable; refresh the running tunnel') from None
    if expected != active:
        raise TunnelError('saved tunnel identity differs from the active launch; run tunnel update or stop/start before verification')
    return active


def _directory(store, name):
    return store.root / (name + '-verification')


def _read(path):
    try:
        value = json.loads(private_read(path))
        return value if isinstance(value, dict) else {}
    except (TunnelError, ValueError, UnicodeError):
        return {}


def _challenge(store, name):
    value = _read(_directory(store, name) / 'challenge.json')
    if (not re.fullmatch(r'[a-f0-9]{32}', str(value.get('nonce', '')))
            or not re.fullmatch(r'[a-f0-9]{64}', str(value.get('identity', '')))
            or type(value.get('created')) not in (int, float)):
        return {}
    return value


def begin_challenge(store: ProfileStore, profile: Profile) -> dict:
    value = {'nonce': uuid.uuid4().hex, 'identity': identity(store, profile), 'created': time.time()}
    private_write(_directory(store, profile.name) / 'challenge.json', json.dumps(value).encode())
    return value


def verification_status(store: ProfileStore, profile: Profile) -> dict:
    challenge = _challenge(store, profile.name)
    try:
        matching = challenge.get('identity') == identity(store, profile)
    except Exception:
        matching = False
    verified = []
    if matching and isinstance(challenge.get('nonce'), str):
        for tool in TOOLS:
            receipt = _read(_directory(store, profile.name) / (challenge['nonce'] + '-' + tool + '.json'))
            if (receipt.get('identity') == challenge['identity'] and receipt.get('nonce') == challenge['nonce']
                    and receipt.get('tool') == tool and receipt.get('success') is True
                    and isinstance(receipt.get('observed'), (float, int))
                    and receipt['observed'] >= challenge.get('created', float('inf'))):
                verified.append(tool)
    return {'verified': len(verified) == len(TOOLS), 'successful_calls': len(verified),
            'challenge_current': matching}


def _message(raw):
    if not isinstance(raw, (bytes, str)) or len(raw) > 4 * 1024 * 1024:
        return {}
    try:
        message = json.loads(raw)
        return message if isinstance(message, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def _identifier(message):
    value = message.get('id')
    return (type(value).__name__, value) if type(value) in (str, int) else None


def _successful_result(result):
    if not isinstance(result, dict):
        return False
    try:
        CallToolResult.model_validate(result, strict=True)
    except (ValidationError, TypeError, ValueError):
        return False
    return result.get('isError', False) is False


class Observer:
    def __init__(self, store: ProfileStore, name: str, profile: Profile | None = None):
        self.store, self.name = store, name
        self.loaded_profile = profile or store.load(name)
        self.loaded_identity = None
        self.loaded_snapshot = None
        manifest_path = Path(__file__).parent / 'manifest.json'
        if manifest_path.exists():
            manifest = _read(manifest_path)
            digest = manifest.get('digest')
            self.loaded_snapshot = digest if isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest) else 'invalid'
        # Capture the launch's credentials before its first cloud request. A
        # still-running old bridge must not adopt a later active key digest.
        try:
            from pseudolife_memory import tunnel_runtime as runtime
            frozen_profile = os.environ.get('PSEUDOLIFE_TUNNEL_LAUNCH_PROFILE')
            if frozen_profile and self.loaded_snapshot:
                config = Path(frozen_profile).parent / 'profile.yaml'
                snapshot = {**manifest, 'command': [manifest['interpreter'],
                    str(manifest_path.parent / 'launch.py'), 'bridge', name, str(store.root)]}
                self.loaded_launch = runtime.capture_launch_identity(self.loaded_profile, store, snapshot, config)
            else:
                self.loaded_launch = runtime.active_launch_identity(name, store)
        except Exception:
            self.loaded_launch = None
        self.pending = {}

    def request(self, message: dict) -> None:
        identifier = _identifier(message)
        if identifier is None:
            return
        # Reused IDs invalidate an in-flight observation instead of attributing
        # a later response to an unrelated request.
        self.pending.pop(identifier, None)
        if len(self.pending) >= 256:
            return
        params = message.get('params')
        if message.get('method') != 'tools/call' or not isinstance(params, dict):
            return
        tool, arguments = params.get('name'), params.get('arguments')
        if tool not in TOOLS or not isinstance(arguments, dict):
            return
        challenge = _challenge(self.store, self.name)
        nonce = challenge.get('nonce')
        supplied = arguments.get('query') if tool == 'memory_search' else arguments.get('project')
        if (isinstance(nonce, str) and isinstance(supplied, str) and nonce in supplied
                and (tool != 'memory_agents' or arguments.get('action') == 'list')):
            try:
                if self.loaded_launch != _snapshot_identity(self.store, self.loaded_profile):
                    return
                loaded = identity(self.store, self.loaded_profile)
                if self.loaded_snapshot is not None and _snapshot_identity(self.store, self.loaded_profile)['bridge_snapshot'] != self.loaded_snapshot:
                    return
                if self.loaded_identity is None:
                    self.loaded_identity = loaded
                if loaded != self.loaded_identity or loaded != challenge['identity']:
                    return
            except Exception:
                return
            self.pending[identifier] = (tool, challenge)

    def response(self, message: dict) -> None:
        observed = self.pending.pop(_identifier(message), None)
        result = message.get('result')
        if not observed or 'error' in message or not _successful_result(result):
            return
        tool, challenge = observed
        payloads = [result.get('structuredContent')]
        payloads.extend(_message(item.get('text', '')) for item in result['content'] if isinstance(item, dict))
        if any(isinstance(payload, dict) and (payload.get('error') or payload.get('errors')
               or payload.get('ok') is False or payload.get('success') is False
               or payload.get('available') is False) for payload in payloads):
            return
        try:
            profile = self.store.load(self.name)
            current = _challenge(self.store, self.name)
            if (current != challenge or identity(self.store, profile) != challenge['identity']
                    or identity(self.store, self.loaded_profile) != self.loaded_identity
                    or self.loaded_identity != challenge['identity']):
                return
            receipt = {'identity': challenge['identity'], 'nonce': challenge['nonce'], 'tool': tool,
                       'success': True, 'observed': time.time()}
            # Independent tool files avoid lost updates across bridge processes.
            private_write(_directory(self.store, self.name) / (challenge['nonce'] + '-' + tool + '.json'),
                          json.dumps(receipt).encode())
        except Exception:
            # Proof persistence may fail; it must never damage the MCP stream.
            return


class CatalogGate:
    """Hold discovery until the explicitly requested catalog is established."""
    def __init__(self, catalog, send, emit):
        self.catalog, self.send, self.emit = catalog, send, emit
        self.state = 'ready' if catalog == 'current' else 'new'
        self.pending = []
        self.internal_id = None
        self.internal_prefix = 'pseudolife-tunnel-' + uuid.uuid4().hex + '-'
        self.internal_count = 0
        self.seen = set()
        self.expansions = 0
        self.lock = threading.RLock()
        self.timer = None
        self.finished = threading.Event()
        if self.state == 'ready':
            self.finished.set()

    def _call(self, action):
        self.internal_count += 1
        self.internal_id = self.internal_prefix + str(self.internal_count)
        self.send((json.dumps({'jsonrpc': '2.0', 'id': self.internal_id, 'method': 'tools/call',
                              'params': {'name': 'memory_toolset', 'arguments': {'action': action}}}) + '\n').encode())

    def _internal(self, identifier):
        if not isinstance(identifier, str) or not identifier.startswith(self.internal_prefix):
            return False
        suffix = identifier[len(self.internal_prefix):]
        return suffix.isascii() and suffix.isdigit() and len(suffix) <= 16 and 0 < int(suffix) <= self.internal_count

    def _failure(self):
        self.state = 'failed'
        self.finished.set()
        for raw in self.pending:
            self._error(_message(raw))
        self.pending.clear()
        if self.timer:
            self.timer.cancel()

    def _error(self, message):
        self.emit((json.dumps({'jsonrpc': '2.0', 'id': message.get('id'), 'error': {
            'code': -32603, 'message': 'Full tool discovery unavailable; run pseudolife-mcp tunnel doctor.'}}) + '\n').encode())

    def request(self, raw):
        with self.lock:
            message = _message(raw)
            identifier = message.get('id')
            if isinstance(identifier, str) and identifier.startswith(self.internal_prefix):
                self._error(message)
                return
            if type(identifier) in (str, int):
                if len(self.seen) >= 1024:
                    self.seen.clear()
                self.seen.add(identifier)
            if message.get('method') != 'tools/list' or self.catalog == 'current':
                self.send(raw)
                return
            if len(self.pending) >= 64:
                self._error(message)
                return
            self.pending.append(raw)
            if self.state in ('new', 'ready', 'failed'):
                self.state = 'waiting'
                self.expansions = 0
                self.finished.clear()
                self.timer = threading.Timer(15, self.timeout)
                self.timer.daemon = True
                self.timer.start()
                self._call('status')

    def timeout(self):
        with self.lock:
            if self.state == 'waiting':
                self._failure()

    def response(self, raw):
        with self.lock:
            message = _message(raw)
            identifier = message.get('id')
            if not self._internal(identifier):
                self.emit(raw)
                return
            if identifier != self.internal_id:
                return
            result = message.get('result')
            if self.state == 'failed':
                return
            current = None
            if _successful_result(result) and 'error' not in message:
                payload = result.get('structuredContent')
                if not isinstance(payload, dict):
                    blocks = result.get('content')
                    for block in blocks if isinstance(blocks, list) else []:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            candidate = _message(block.get('text', ''))
                            if 'current' in candidate:
                                payload = candidate
                                break
                current = payload.get('current') if isinstance(payload, dict) else None
            if current == 'full':
                self.state = 'ready'
                self.finished.set()
                if self.timer:
                    self.timer.cancel()
                for listing in self.pending:
                    self.send(listing)
                self.pending.clear()
                self.internal_id = None
            elif current in ('minimal', 'core') and self.expansions < 2:
                self.expansions += 1
                self._call('expand')
            else:
                self._failure()

    def close(self):
        self.finished.set()
        if self.timer:
            self.timer.cancel()


def run_bridge(store: ProfileStore, profile: Profile, *, command=None, stdin=None, stdout=None) -> int:
    env = {k: v for k, v in os.environ.items() if k not in {
        'PSEUDOLIFE_MCP_TOKEN', 'PSEUDOLIFE_MCP_TOKEN_FILE', 'PSEUDOLIFE_MCP_DAEMON_URL',
        'PSEUDOLIFE_MCP_TOKENS', 'OPENAI_API_KEY'}}
    env.update({'PSEUDOLIFE_MCP_DAEMON_URL': profile.daemon_url,
                'PSEUDOLIFE_MCP_TOKEN_FILE': profile.token_file, 'PSEUDOLIFE_MCP_NO_SPAWN': '1'})
    source = stdin or sys.stdin.buffer
    destination = stdout or sys.stdout.buffer
    observer = Observer(store, profile.name, profile)
    with subprocess.Popen(command or [sys.executable, '-m', 'pseudolife_memory.cli'], env=env,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as child:
        write_lock = threading.Lock()
        emit_lock = threading.Lock()

        def send(raw):
            with write_lock:
                child.stdin.write(raw)
                child.stdin.flush()

        def emit(raw):
            with emit_lock:
                destination.write(raw)
                destination.flush()

        gate = CatalogGate(profile.minimum_catalog, send, emit)
        observation_lock = threading.Lock()

        def forward():
            try:
                for raw in iter(source.readline, b''):
                    with observation_lock:
                        observer.request(_message(raw))
                    gate.request(raw)
            except (OSError, ValueError):
                pass
            finally:
                if gate.state == 'waiting':
                    gate.finished.wait(timeout=16)
                try:
                    with write_lock:
                        child.stdin.close()
                except OSError:
                    pass

        thread = threading.Thread(target=forward, daemon=True)
        thread.start()
        try:
            for raw in iter(child.stdout.readline, b''):
                with observation_lock:
                    observer.response(_message(raw))
                gate.response(raw)
        finally:
            gate.close()
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        return child.returncode
