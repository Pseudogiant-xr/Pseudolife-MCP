"""Guided optional Secure MCP Tunnel setup; local registrations are read-only."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import getpass
import json
import os
import re
from pathlib import Path
import sys
import webbrowser

from pseudolife_memory.connect_cli import handshake
from pseudolife_memory.doctor_cli import _registration_env_blocks as registration_blocks
from pseudolife_memory.tunnel_profiles import Profile, ProfileStore, TunnelError, private_read, validate_url, parse_expiry
from pseudolife_memory.tunnel_bridge import begin_challenge, run_bridge, verification_status

TUNNELS_URL = 'https://platform.openai.com/settings/organization/tunnels'
KEYS_URL = 'https://platform.openai.com/settings/organization/api-keys'
APPS_URL = 'https://chatgpt.com/plugins'
DEFAULT_URL = 'http://127.0.0.1:8765'


def interactive():
    return sys.stdin.isatty()


def runtime_status(name, store):
    from pseudolife_memory.tunnel_runtime import status_profile
    return status_profile(name, store)


def shim_command(profile, store):
    from pseudolife_memory.tunnel_runtime import stable_command
    return stable_command() + ['tunnel', 'shim',
            '--profile', profile.name, '--profile-dir', str(store.root)]


def _discover(url=None, token_file=None):
    """Select a coherent connection; a literal credential requires file migration."""
    if url and token_file:
        return validate_url(url, local=True), str(Path(token_file).expanduser().absolute())
    env = os.environ
    if env.get('PSEUDOLIFE_MCP_TOKEN_FILE') or env.get('PSEUDOLIFE_MCP_TOKEN'):
        blocks = [('environment', env)]
    else:
        blocks = list(registration_blocks(env))
    candidates = set()
    literal_seen = False
    for _, block in blocks:
        selected_url = url or env.get('PSEUDOLIFE_MCP_DAEMON_URL') or block.get('PSEUDOLIFE_MCP_DAEMON_URL') or DEFAULT_URL
        selected_file = token_file or block.get('PSEUDOLIFE_MCP_TOKEN_FILE')
        if selected_file:
            candidates.add((selected_url, str(Path(selected_file).expanduser().absolute())))
        elif block.get('PSEUDOLIFE_MCP_TOKEN'):
            literal_seen = True
    if len(candidates) != 1 or literal_seen:
        raise TunnelError('daemon connection is missing or ambiguous; pass --daemon-url and --token-file explicitly (an owner-only credential file)')
    selected_url, selected_file = candidates.pop()
    return validate_url(selected_url, local=True), selected_file


def _verify_local(profile):
    from pseudolife_memory.client_config import check_token_file
    checked = check_token_file(Path(profile.token_file))
    if checked.get('status') != 'ready':
        raise TunnelError('daemon credential file is unavailable or unsafe; repair its owner-only access and retry setup')
    result = handshake(profile.daemon_url, ('file', profile.token_file))
    if not result.get('ok'):
        raise TunnelError('existing daemon MCP handshake failed; start or repair that daemon and run tunnel doctor (no daemon was spawned)')
    return {'ok': True, 'tool_count': result.get('tool_count', 0)}


def _key_present(store, profile):
    try:
        store.read_key(profile.name)
        return True
    except TunnelError:
        return False


def _expiry(profile):
    value = profile.runtime_key_expires_at
    if not value:
        return {'known': False, 'expired': None}
    expires = parse_expiry(value)
    return {'known': True, 'expires_at': value, 'expired': expires <= datetime.now(timezone.utc)}


def _handoff(args, step, url, text):
    print(text)
    print(url)
    if args.open_browser == step:
        webbrowser.open(url)


def _setup(args, store):
    exists = store.profile_path(args.profile).exists()
    if exists:
        profile = store.load(args.profile)
        if args.daemon_url is not None or args.token_file is not None:
            url = args.daemon_url or profile.daemon_url
            token = str(Path(args.token_file).expanduser().absolute()) if args.token_file else profile.token_file
            profile = replace(profile, daemon_url=validate_url(url, local=True), token_file=token)
    else:
        url, token = _discover(args.daemon_url, args.token_file)
        profile = Profile(args.profile, url, token)
    if args.tunnel_id is not None:
        profile = replace(profile, tunnel_id=args.tunnel_id)
    if args.organization_id is not None:
        profile = replace(profile, organization_id=args.organization_id)
    if args.catalog is not None:
        profile = replace(profile, minimum_catalog=args.catalog)
    if args.key_expires_at is not None:
        profile = replace(profile, runtime_key_expires_at=args.key_expires_at)
    elif args.read_key or args.key_file:
        # A replacement key's expiry is unknown until supplied by its owner;
        # the old key's deadline must not be attributed to the new credential.
        profile = replace(profile, runtime_key_expires_at=None)
    profile.validate()
    _verify_local(profile)
    if not profile.consent:
        print('This tunnel makes this daemon available to the selected OpenAI organization and associated ChatGPT workspace/app.')
        print('Choose an always-on host for access while your laptop sleeps; the host and daemon must remain running.')
        accepted = args.accept_access
        if not accepted and interactive():
            accepted = input('Configure this optional access path? [y/N] ').strip().lower() in ('y', 'yes')
        if not accepted:
            print('Setup needs explicit consent: rerun with --accept-access after reviewing the access path.')
            return 2
        profile = replace(profile, consent=True)
    if profile.minimum_catalog == 'full':
        print('Full catalog selected: discovery expands the reused daemon principal for its other sessions too; authentication permissions stay the same.')
    profile = replace(profile, state='ready' if profile.tunnel_id else 'pending')
    key = None
    if args.read_key or args.key_file:
        key = getpass.getpass('Tunnel API key (private, no echo): ') if args.read_key else private_read(Path(args.key_file)).decode('utf-8').strip()
        if _key_present(store, profile):
            from pseudolife_memory.tunnel_runtime import verify_key
            if not verify_key(profile, store, key, shim_command(profile, store)):
                raise TunnelError('replacement key validation failed; existing private key and profile are preserved')
            print('Replacement key passed local configuration checks. Use tunnel setup --start to adopt it, then tunnel verify to prove cloud access.')
        from pseudolife_memory.credentials import _decode_token, CredentialError
        try:
            _decode_token(key.encode('utf-8'))
        except CredentialError:
            raise TunnelError('invalid private tunnel key; saved profile and key preserved') from None
    store.save(profile)
    if key is not None:
        store.set_key(profile.name, key)
        del key
    print(f'Profile {profile.name} saved; existing local clients and plugin registrations are preserved.')
    _handoff(args, 'tunnels', TUNNELS_URL,
             'In Platform tunnel settings, select the intended organization and create or choose a tunnel; copy its tunnel ID. Creation is a separate action you approve in the browser.')
    if not profile.tunnel_id:
        print(f'Resume: pseudolife-mcp tunnel setup --profile {profile.name} --tunnel-id <tunnel_id>')
    if not _key_present(store, profile):
        _handoff(args, 'keys', KEYS_URL,
                 'Create a Restricted API key in the same Platform organization with Tunnels Read + Use. Tunnel creation/editing separately needs Read + Manage. Copy the key privately; do not paste it into chat or command arguments.')
        print(f'Resume: pseudolife-mcp tunnel setup --profile {profile.name} --read-key (or --key-file <owner-only file>); --key-expires-at <ISO timestamp> is optional.')
    if profile.tunnel_id and _key_present(store, profile):
        if args.start:
            from pseudolife_memory.tunnel_runtime import start_profile
            result = start_profile(profile, store, shim_command(profile, store))
            if not result.get('ready'):
                raise TunnelError('local tunnel readiness failed; profile and key are preserved')
        print(f'Run: pseudolife-mcp tunnel run --profile {profile.name} (foreground); setup --start starts a managed background process.')
        print('Saved changes need a ready launch: setup --start starts or refreshes the managed tunnel; an existing foreground run needs stop/start before verification.')
        _handoff(args, 'app', APPS_URL,
                 'In the intended ChatGPT workspace, enable developer mode, create a developer app, choose Connection: Tunnel and select this tunnel. Confirm the workspace association separately from the Platform organization and app creation.')
        print('Local readiness does not prove cloud access. After connecting the app, run tunnel verify and paste its read-only prompt into your dot.')
    else:
        print('Cloud verification is pending until tunnel ID, private key and browser association/app setup are complete.')
    return 0


def _status(args, store):
    profile = store.load(args.profile)
    report = {'profile': profile.name, 'state': profile.state, 'catalog': profile.minimum_catalog,
              'private_key_present': _key_present(store, profile), 'key_expiry': _expiry(profile),
              'runtime': runtime_status(profile.name, store), 'cloud': verification_status(store, profile)}
    report['update'] = _refresh_status(store, profile.name)
    if args.command == 'doctor':
        try:
            report['daemon'] = _verify_local(profile)
        except TunnelError:
            report['daemon'] = {'ok': False}
        from pseudolife_memory.tunnel_runtime import doctor_profile
        try:
            report['checks'] = doctor_profile(profile, store, shim_command(profile, store))
        except TunnelError:
            report['checks'] = {'ok': False, 'checks': [], 'cloud_verified': False}
    if args.as_json:
        print(json.dumps(report))
    else:
        print(f"Profile {profile.name}: {profile.state}; runtime running={bool(report['runtime'].get('running'))}, ready={bool(report['runtime'].get('ready'))}; cloud verified={report['cloud']['verified']} ({report['cloud']['successful_calls']}/2 calls).")
        print(f"API key expiry: {report['key_expiry'].get('expires_at', 'unknown; check Platform key settings')}.")
        if report['update']['needs_attention']:
            print(f"Last tunnel refresh: {report['update']['state']}; needs attention. Inspect tunnel doctor before retrying the update.")
        if report['key_expiry'].get('expired'):
            print(f'Renew privately: tunnel setup --profile {profile.name} --read-key --key-expires-at <ISO timestamp>.')
        if args.command == 'doctor':
            print(f"Daemon handshake: {report['daemon']['ok']}; vendor checks: {json.dumps(report['checks'])}.")
            print(f'Recovery: tunnel setup --profile {profile.name} resumes saved steps; tunnel verify prints the cloud proof prompt.')
    return 0 if args.command != 'doctor' or (report['daemon']['ok'] and report['checks'].get('ok')) else 1


def _refresh_status(store, name):
    """Allowlisted last-refresh outcome, separate from current readiness."""
    pending = store.root / (name + '.reload.json')
    if pending.exists():
        try:
            request = json.loads(private_read(pending))
            if not isinstance(request, dict) or not re.fullmatch(r'[a-f0-9]{32}', str(request.get('id', ''))):
                raise ValueError
            return {'state': 'pending', 'needs_attention': True}
        except (TunnelError, ValueError, TypeError, UnicodeError):
            return {'state': 'unavailable', 'needs_attention': True}
    path = store.root / (name + '.reload.result.json')
    if not path.exists():
        return {'state': 'none', 'needs_attention': False}
    try:
        result = json.loads(private_read(path))
        if (not isinstance(result, dict) or not re.fullmatch(r'[a-f0-9]{32}', str(result.get('id', '')))
                or result.get('state') not in {'refreshed', 'rolled-back', 'failed', 'rollback-incomplete'}):
            raise ValueError
        state = result['state']
        return {'state': state, 'needs_attention': state != 'refreshed',
                'rolled_back': result.get('rolled_back') is True}
    except (TunnelError, ValueError, TypeError, UnicodeError):
        return {'state': 'unavailable', 'needs_attention': True}


def saved_tunnel_diagnostics(store=None):
    """Read-only optional section for the ordinary combined doctor command."""
    store = store or ProfileStore()
    reports = []
    try:
        names = store.list()
    except TunnelError:
        return [{'state': 'unavailable', 'recovery': 'run tunnel doctor to repair private profile access'}]
    for name in names:
        try:
            profile = store.load(name)
            runtime = runtime_status(name, store)
            reports.append({'profile': name, 'state': profile.state, 'catalog': profile.minimum_catalog,
                            'runtime': {key: bool(runtime.get(key)) for key in ('running', 'ready')},
                            'private_key_present': _key_present(store, profile), 'key_expiry': _expiry(profile),
                            'cloud': verification_status(store, profile), 'update': _refresh_status(store, name)})
        except Exception:
            reports.append({'profile': name, 'state': 'unavailable', 'recovery': 'run tunnel doctor or resume tunnel setup'})
    return reports


def update_existing_profiles(store=None):
    """Update only saved profiles; never begins account or app setup."""
    store = store or ProfileStore()
    from pseudolife_memory.tunnel_runtime import update_profile
    results = []
    for name in store.list():
        profile = store.load(name)
        if profile.state != 'ready' or not _key_present(store, profile):
            results.append({'profile': name, 'state': 'pending'})
            continue
        try:
            result = update_profile(profile, store, shim_command(profile, store))
        except TunnelError as exc:
            result = {'state': 'failed', 'detail': str(exc)}
        except Exception:
            result = {'state': 'failed', 'detail': 'tunnel runtime update failed; saved profile and key preserved'}
        result = {**result, 'needs_attention': result.get('state') in {'failed', 'rolled-back', 'rollback-incomplete'}
                  or result.get('rolled_back') is True}
        results.append({'profile': name, **result})
    needs_attention = any(r.get('needs_attention') for r in results)
    return {'state': 'failed' if needs_attention else 'current', 'needs_attention': needs_attention,
            'detail': f'{len(results)} saved tunnel profiles checked; local client registrations preserved', 'profiles': results}


def _parser():
    parser = argparse.ArgumentParser(prog='pseudolife-mcp tunnel', description='Optional guided Secure MCP Tunnel access; resumes saved private profiles.')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('setup', 'status', 'doctor', 'verify', 'run', 'start', 'stop', 'shim', 'service', 'update'):
        child = sub.add_parser(command)
        child.add_argument('--profile', default='dot')
        child.add_argument('--profile-dir', type=Path)
        if command in ('status', 'doctor'):
            child.add_argument('--json', dest='as_json', action='store_true')
        if command == 'setup':
            child.add_argument('--daemon-url')
            child.add_argument('--token-file')
            child.add_argument('--tunnel-id')
            child.add_argument('--organization-id')
            child.add_argument('--accept-access', action='store_true')
            child.add_argument('--catalog', choices=('current', 'full'))
            key = child.add_mutually_exclusive_group()
            key.add_argument('--read-key', action='store_true')
            key.add_argument('--key-file', type=Path)
            child.add_argument('--key-expires-at')
            child.add_argument('--open-browser', choices=('tunnels', 'keys', 'app'))
            child.add_argument('--start', action='store_true')
        if command == 'service':
            child.add_argument('action', choices=('preview', 'install', 'status', 'stop', 'remove'))
            child.add_argument('--accept-autostart', action='store_true')
            child.add_argument('--persistent', action='store_true')
            child.add_argument('--linger', action='store_true', help='explicitly enable Linux user-service boot availability via linger')
            child.add_argument('--system-service', action='store_true', help='explicitly install a Linux system service as root')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    store = ProfileStore(args.profile_dir)
    try:
        if args.command == 'setup':
            return _setup(args, store)
        if args.command == 'update':
            if not store.list():
                print('No saved tunnel profiles; update does not start new setup.')
                return 3
            result = update_existing_profiles(store)
            print(json.dumps(result))
            return 1 if result['state'] == 'failed' else 0
        if args.command in ('status', 'doctor'):
            return _status(args, store)
        profile = store.load(args.profile)
        if args.command == 'verify':
            if profile.state != 'ready' or not _key_present(store, profile):
                raise TunnelError('finish tunnel setup before requesting cloud verification')
            challenge = begin_challenge(store, profile)
            nonce = challenge['nonce']
            print('Paste into the dot using the connected tunnel app:')
            print(f'Call memory_search with query="{nonce}", then memory_agents with action="list" and project="{nonce}" through this tunnel app. Report success or failure only; do not copy memory or board contents. Zero results are fine. Do not write memory or change the board. Optionally run one cloud child and repeat these same read-only calls.')
            print(f'Check: pseudolife-mcp tunnel status --profile {profile.name}; both successful real calls automatically create private receipts.')
            return 0
        if args.command == 'shim':
            return run_bridge(store, profile)
        from pseudolife_memory import tunnel_runtime as runtime
        command = shim_command(profile, store)
        if args.command == 'run':
            return runtime.run_profile(profile, store, command)
        if args.command == 'start':
            result = runtime.start_profile(profile, store, command)
            print(json.dumps(result))
            return 0 if result.get('ready') else 1
        if args.command == 'stop':
            print(json.dumps(runtime.stop_profile(profile.name, store)))
            return 0
        if args.command == 'service':
            from pseudolife_memory import tunnel_service as service
            if args.action == 'preview':
                print(service.render_service(profile, store, command, system_service=args.system_service))
                return 0
            if args.action in ('status', 'stop', 'remove'):
                function = getattr(service, args.action + '_service')
                print(json.dumps(function(profile, store, system_service=args.system_service)))
                return 0
            if not (args.accept_autostart or args.persistent):
                raise TunnelError('service install needs explicit --accept-autostart; preview it first')
            approved = replace(profile, autostart_consent=True)
            result = service.install_service(approved, store, command, persistent=args.accept_autostart or args.persistent,
                                             linger=args.linger, system_service=args.system_service)
            store.save(approved)
            print(json.dumps(result))
            return 0
    except (TunnelError, OSError, ValueError, UnicodeError) as exc:
        # Built-in exception text may contain credentials or private file data.
        text = str(exc) if isinstance(exc, TunnelError) else 'private tunnel operation failed; run tunnel doctor or resume setup'
        print(text, file=sys.stderr)
        return 2
    return 0
