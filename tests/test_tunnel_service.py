from pathlib import Path
import pytest
from pseudolife_memory.tunnel_profiles import Profile, ProfileStore, TunnelError
from pseudolife_memory.tunnel_service import render_service, install_service


def profile(tmp_path, consent=False):
    return Profile(name='personal', daemon_url='http://127.0.0.1:8765', token_file=str(tmp_path/'daemon.token'), tunnel_id='tunnel_0123', consent=True, state='ready', autostart_consent=consent)


def test_service_install_requires_explicit_consent(tmp_path):
    with pytest.raises(TunnelError, match='opt-in'):
        install_service(profile(tmp_path), ProfileStore(tmp_path/'profiles'), ['/usr/local/bin/pseudolife-mcp', 'tunnel', 'run'])


@pytest.mark.parametrize('system', ['windows', 'linux', 'darwin'])
def test_service_private_reference_and_stable_command(tmp_path, monkeypatch, system):
    from pseudolife_memory import tunnel_service
    monkeypatch.setattr(tunnel_service, '_identity', lambda system: 'S-1-5-21-123')
    output = render_service(profile(tmp_path), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp'), 'tunnel', 'run', '--profile', 'personal'], platform=system)
    assert 'pseudolife-mcp' in output
    assert 'synthetic-private-key' not in output
    if system == 'windows':
        assert '<Hidden>true</Hidden>' in output
        assert 'InteractiveToken' in output
    elif system == 'linux':
        assert 'WantedBy=default.target' in output
    else:
        assert 'RunAtLoad' in output


def test_service_refuses_ephemeral_venv_command(tmp_path):
    with pytest.raises(TunnelError, match='stable'):
        render_service(profile(tmp_path), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'.venv'/'bin'/'python')], platform='linux')


def test_linux_service_escapes_environment_and_specifiers(tmp_path):
    output = render_service(profile(tmp_path), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp'), 'literal$HOME', '100%'], platform='linux')
    assert 'literal$$HOME' in output
    assert '100%%' in output
    assert 'PseudolifeTunnelOwner' in output


def test_lifecycle_api_is_available():
    from pseudolife_memory import tunnel_service
    assert callable(getattr(tunnel_service, 'status_service', None))
    assert callable(getattr(tunnel_service, 'stop_service', None))
    assert callable(getattr(tunnel_service, 'remove_service', None))


def test_root_linux_does_not_claim_boot_support(tmp_path, monkeypatch):
    from pseudolife_memory import tunnel_service
    monkeypatch.setattr(tunnel_service.host_platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(tunnel_service, '_identity', lambda system: '0', raising=False)
    with pytest.raises(TunnelError, match='root.*unsupported'):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pseudolife_memory import tunnel_service as service
    class Manager:
        system = 'linux'
        present = False
        active = False
        enabled = False
        linger = False
        foreign = None
        fail = None
        calls = []
        xml = None
        systemmode = False
        def run(self, args, **kwargs):
            self.calls.append(args)
            if '--system' in args:
                self.systemmode = True
                args = ['--user' if part == '--system' else part for part in args]
            code, out = 0, b''
            if self.fail and self.fail(args):
                return SimpleNamespace(returncode=1, stdout=b'', stderr=b'')
            name = 'pseudolife-tunnel-personal'
            if args[:3] == ['systemctl', '--user', 'show']:
                out = (f'LoadState={"loaded" if self.present else "not-found"}\nFragmentPath={self.foreign or self.target}\nActiveState={"active" if self.active else "inactive"}\nUnitFileState={"enabled" if self.enabled else "disabled"}\n').encode()
            elif args[:2] == ['loginctl', 'show-user']:
                out = b'yes\n' if self.linger else b'no\n'
            elif args[:2] == ['loginctl', 'enable-linger']:
                self.linger = True
            elif args[:2] == ['loginctl', 'disable-linger']:
                self.linger = False
            elif args[:3] == ['systemctl', '--user', 'enable']:
                self.present = self.active = self.enabled = True
            elif args[:3] == ['systemctl', '--user', 'disable']:
                self.enabled = False
                if '--now' in args:
                    self.active = False
            elif args[:3] == ['systemctl', '--user', 'start']:
                self.active = True
            elif args[:3] == ['systemctl', '--user', 'restart']:
                self.active = True
            elif args[:3] == ['systemctl', '--user', 'stop']:
                self.active = False
            elif args[:3] == ['systemctl', '--user', 'daemon-reload']:
                self.present = self.target.exists()
            elif args[:3] == ['schtasks.exe', '/Query', '/FO']:
                out = ('"\\'+name+'","value","value"\n').encode() if self.present else b''
            elif args[0] == 'powershell.exe':
                out = b'4' if self.active else b'3'
            elif args[:3] == ['schtasks.exe', '/Query', '/TN']:
                out = self.xml
            elif args[:2] == ['schtasks.exe', '/Create']:
                self.xml = Path(args[args.index('/XML') + 1]).read_bytes()
                self.present = self.enabled = True
            elif args[:2] == ['schtasks.exe', '/Run']:
                self.active = True
            elif args[:2] == ['schtasks.exe', '/End']:
                self.active = False
            elif args[:2] == ['schtasks.exe', '/Delete']:
                self.present = self.enabled = False
            elif args[:2] == ['launchctl', 'list']:
                out = ('123\t0\t'+name+'\n').encode() if self.present else b''
            elif args[:2] == ['launchctl', 'print']:
                out = (f'path = {self.foreign or self.target}\nstate = {"running" if self.active else "waiting"}\n').encode()
            elif args[:2] == ['launchctl', 'bootstrap']:
                self.present = self.active = self.enabled = True
            elif args[:2] == ['launchctl', 'bootout']:
                self.present = self.active = self.enabled = False
            else:
                raise AssertionError('unexpected manager command')
            return SimpleNamespace(returncode=code, stdout=out, stderr=b'')
        @property
        def target(self):
            name = 'pseudolife-tunnel-personal'
            if self.systemmode:
                return tmp_path/'system'/(name+'.service')
            if self.system == 'linux':
                return tmp_path/'home'/'.config'/'systemd'/'user'/(name+'.service')
            if self.system == 'darwin':
                return tmp_path/'home'/'Library'/'LaunchAgents'/(name+'.plist')
            return tmp_path/'profiles'/(name+'.xml')
    result = Manager()
    result.calls = []
    monkeypatch.setattr(service.host_platform, 'system', lambda: result.system)
    monkeypatch.setattr(service, '_identity', lambda system: 'S-1-5-21-123' if system == 'windows' else '1000')
    monkeypatch.setattr(service.Path, 'home', lambda: tmp_path/'home')
    monkeypatch.setattr(service.subprocess, 'run', result.run)
    return result


@pytest.mark.parametrize('system', ['linux', 'windows', 'darwin'])
def test_owned_service_lifecycle_is_idempotent(tmp_path, manager, system):
    from pseudolife_memory import tunnel_service as service
    manager.system = system
    store, selected = ProfileStore(tmp_path/'profiles'), profile(tmp_path, consent=True)
    command = [str(tmp_path/'bin'/'pseudolife-mcp'), 'tunnel', 'run']
    for _ in range(2):
        assert service.install_service(selected, store, command, persistent=True)['installed']
    assert service.status_service(selected, store)['installed']
    for _ in range(2):
        assert service.stop_service(selected, store)['stopped']
    for _ in range(2):
        assert service.remove_service(selected, store)['removed']
    assert not manager.target.exists()


def test_foreign_definition_cannot_be_overwritten(tmp_path, manager):
    from pseudolife_memory.tunnel_profiles import private_write
    private_write(manager.target, b'[Service]\nExecStart=/foreign\n')
    with pytest.raises(TunnelError, match='not owned'):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True)
    assert manager.target.read_bytes() == b'[Service]\nExecStart=/foreign\n'
    assert not any('enable' in call for call in manager.calls)


@pytest.mark.parametrize('system', ['linux', 'windows', 'darwin'])
def test_failed_registration_rolls_back_new_install(tmp_path, manager, system):
    manager.system = system
    manager.fail = lambda args: '--now' in args and 'enable' in args or '/Run' in args or 'bootstrap' in args
    with pytest.raises(TunnelError, match='previous state restored'):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True)
    assert not manager.target.exists()
    assert not manager.present


def test_linger_is_never_changed_without_explicit_opt_in(tmp_path, manager):
    selected, store = profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles')
    command = [str(tmp_path/'bin'/'pseudolife-mcp')]
    assert 'logout' in install_service(selected, store, command, persistent=True)['availability']
    assert not any('enable-linger' in call for call in manager.calls)
    assert 'boot' in install_service(selected, store, command, persistent=True, linger=True)['availability']
    from pseudolife_memory.tunnel_service import remove_service
    remove_service(selected, store)
    assert manager.linger


def test_linger_restored_if_registration_fails(tmp_path, manager):
    manager.fail = lambda args: 'enable' in args and '--now' in args
    with pytest.raises(TunnelError, match='previous state restored'):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True, linger=True)
    assert not manager.linger


@pytest.mark.parametrize('system', ['linux', 'windows', 'darwin'])
def test_foreign_registered_service_cannot_be_stopped_or_removed(tmp_path, manager, system):
    from pseudolife_memory import tunnel_service as service
    manager.system = system
    selected, store = profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles')
    command = [str(tmp_path/'bin'/'pseudolife-mcp')]
    install_service(selected, store, command, persistent=True)
    if system == 'windows':
        manager.xml = manager.xml.replace('S-1-5-21-123'.encode('utf-16-le'), 'S-1-5-21-999'.encode('utf-16-le'))
    else:
        manager.foreign = str(tmp_path/'foreign.service')
    calls = len(manager.calls)
    for operation in (service.stop_service, service.remove_service):
        with pytest.raises(TunnelError, match='not owned'):
            operation(selected, store)
    assert all('/End' not in args and '/Delete' not in args and 'stop' not in args and 'disable' not in args and 'bootout' not in args for args in manager.calls[calls:])


def test_existing_macos_job_is_not_bootstrapped_twice(tmp_path, manager):
    manager.system = 'darwin'
    selected, store = profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles')
    command = [str(tmp_path/'bin'/'pseudolife-mcp')]
    install_service(selected, store, command, persistent=True)
    manager.fail = lambda args: 'bootstrap' in args and manager.present
    assert install_service(selected, store, command, persistent=True)['installed']


def test_failed_update_restores_old_registration_and_definition(tmp_path, manager):
    from pseudolife_memory import tunnel_service as service
    selected, store = profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles')
    command = [str(tmp_path/'bin'/'pseudolife-mcp'), 'old']
    install_service(selected, store, command, persistent=True)
    old = manager.target.read_bytes()
    failed = []
    def fail_once(args):
        if 'restart' in args and not failed:
            failed.append(True)
            return True
        return False
    manager.fail = fail_once
    with pytest.raises(TunnelError, match='previous state restored'):
        install_service(selected, store, command[:-1]+['new'], persistent=True)
    assert manager.target.read_bytes() == old
    assert service.status_service(selected, store)['active']


@pytest.mark.parametrize('name', ['../escape', 'personal\nother', 'name;exit', 'NUL'])
def test_adversarial_names_fail_before_manager_contact(tmp_path, manager, name):
    from dataclasses import replace
    selected = replace(profile(tmp_path, consent=True), name=name)
    with pytest.raises(TunnelError, match='name'):
        install_service(selected, ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True)
    assert not manager.calls


def test_existing_linger_is_preserved_on_failure(tmp_path, manager):
    manager.linger = True
    manager.fail = lambda args: 'enable' in args and '--now' in args
    with pytest.raises(TunnelError):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True, linger=True)
    assert manager.linger


def _root_chain_checked(monkeypatch):
    """Records the root chain check instead of reading this host's paths."""
    from pseudolife_memory import tunnel_runtime
    seen = []
    monkeypatch.setattr(tunnel_runtime, 'require_root_chain', lambda command, store: seen.append(command[0]))
    return seen


def test_root_system_service_requires_separate_opt_in(tmp_path, manager, monkeypatch):
    from pseudolife_memory import tunnel_service as service
    monkeypatch.setattr(service, '_identity', lambda system: '0')
    monkeypatch.setattr(service, '_system_directory', lambda: tmp_path/'system')
    checked = _root_chain_checked(monkeypatch)
    selected, store = profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles')
    command = [str(tmp_path/'bin'/'pseudolife-mcp')]
    with pytest.raises(TunnelError, match='root.*unsupported'):
        install_service(selected, store, command, persistent=True)
    result = install_service(selected, store, command, persistent=True, system_service=True)
    assert 'root privilege' in result['availability']
    assert any('--system' in call for call in manager.calls)
    assert not any('loginctl' in call for call in manager.calls)
    assert checked == [command[0]]


def test_root_system_service_refuses_a_chain_a_user_could_change(tmp_path, manager, monkeypatch):
    import stat
    from types import SimpleNamespace
    from pseudolife_memory import tunnel_service as service, tunnel_runtime
    monkeypatch.setattr(service, '_identity', lambda system: '0')
    monkeypatch.setattr(service, '_system_directory', lambda: tmp_path/'system')
    launcher = '/opt/pl/bin/pseudolife-mcp'
    owners = {'/': 0, '/opt': 0, '/opt/pl': 0, '/opt/pl/bin': 1000, launcher: 0}
    monkeypatch.setattr(tunnel_runtime, 'root_command_chain', lambda command, store: [launcher])
    monkeypatch.setattr(tunnel_runtime, '_lstat', lambda path: SimpleNamespace(st_uid=owners[path], st_mode=stat.S_IFDIR | 0o755))
    with pytest.raises(TunnelError, match='/opt/pl/bin must be owned by root'):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True, system_service=True)
    assert not manager.calls
    assert not (tmp_path/'system'/'pseudolife-tunnel-personal.service').exists()


def test_root_unit_has_explicit_privilege_and_narrow_write_paths(tmp_path):
    output = render_service(profile(tmp_path), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], platform='linux', system_service=True)
    assert 'User=root' in output
    assert 'ProtectSystem=strict' in output
    assert 'NoNewPrivileges=yes' in output
    assert 'ReadWritePaths=' in output
    assert 'WantedBy=multi-user.target' in output


def test_nonroot_cannot_select_system_service(tmp_path, manager):
    with pytest.raises(TunnelError, match='requires root'):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True, system_service=True)


def test_failed_mac_restart_of_stopped_definition_removes_new_registration(tmp_path, manager):
    from pseudolife_memory import tunnel_service as service
    manager.system = 'darwin'
    selected, store = profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles')
    command = [str(tmp_path/'bin'/'pseudolife-mcp')]
    install_service(selected, store, command, persistent=True)
    service.stop_service(selected, store)
    original_register = service._register
    def register_then_fail(*args, **kwargs):
        original_register(*args, **kwargs)
        raise TunnelError('synthetic failure after registration')
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(service, '_register', register_then_fail)
        with pytest.raises(TunnelError, match='previous state restored'):
            install_service(selected, store, command+['new'], persistent=True)
        assert not manager.present
    finally:
        monkeypatch.undo()


def test_service_preview_replaces_shim_with_stable_supervisor(tmp_path, monkeypatch):
    from pseudolife_memory import tunnel_runtime
    monkeypatch.setattr(tunnel_runtime, 'stable_command', lambda: [str(tmp_path/'bin'/'pseudolife-mcp')])
    output = render_service(profile(tmp_path), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'.venv'/'python'), '-m', 'pseudolife_memory.cli', 'tunnel', 'shim'], platform='linux')
    assert '"run"' in output
    assert '"--profile-dir"' in output
    assert '.venv' not in output


def test_system_unit_registration_failure_is_rolled_back(tmp_path, manager, monkeypatch):
    from pseudolife_memory import tunnel_service as service
    monkeypatch.setattr(service, '_identity', lambda system: '0')
    _root_chain_checked(monkeypatch)
    monkeypatch.setattr(service, '_system_directory', lambda: tmp_path/'system')
    manager.fail = lambda args: 'enable' in args and '--now' in args
    with pytest.raises(TunnelError, match='previous state restored'):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True, system_service=True)
    assert not manager.target.exists()
    assert not manager.present


def test_service_paths_reject_symlink_ancestors(tmp_path, manager):
    link = tmp_path/'home'/'.config'
    destination = tmp_path/'redirect'
    destination.mkdir()
    link.parent.mkdir()
    try:
        link.symlink_to(destination, target_is_directory=True)
    except OSError:
        pytest.skip('this host does not permit disposable symlinks')
    with pytest.raises(TunnelError, match='redirect|safely'):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True)
    assert not manager.calls


def test_profile_consent_does_not_replace_current_persistence_opt_in(tmp_path, manager):
    with pytest.raises(TunnelError, match='opt-in'):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], linger=True)
    assert not manager.calls


def test_manager_unavailable_is_not_reported_as_installed(tmp_path, manager):
    manager.fail = lambda args: 'show' in args
    with pytest.raises(TunnelError, match='rejected'):
        install_service(profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles'), [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True)
    assert not manager.target.exists()


def test_task_xml_binds_exact_owner_and_round_trips_arguments(tmp_path, monkeypatch):
    import subprocess
    from xml.etree import ElementTree
    from pseudolife_memory import tunnel_service as service
    monkeypatch.setattr(service, '_identity', lambda system: 'S-1-5-21-123')
    command = [str(tmp_path/'bin'/'pseudolife-mcp'), 'literal & < > "quoted"', 'trail\\']
    xml = service.render_service(profile(tmp_path), ProfileStore(tmp_path/'profiles'), command, platform='windows')
    root = ElementTree.fromstring(xml)
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert root.findtext('t:Principals/t:Principal/t:UserId', namespaces=ns) == 'S-1-5-21-123'
    assert root.findtext('t:Triggers/t:LogonTrigger/t:UserId', namespaces=ns) == 'S-1-5-21-123'
    assert root.findtext('t:Actions/t:Exec/t:Arguments', namespaces=ns) == subprocess.list2cmdline(command[1:])


@pytest.mark.parametrize('system', ['linux', 'windows'])
def test_install_resumes_an_owned_stopped_service(tmp_path, manager, system):
    from pseudolife_memory import tunnel_service as service
    manager.system = system
    selected, store = profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles')
    command = [str(tmp_path/'bin'/'pseudolife-mcp')]
    install_service(selected, store, command, persistent=True)
    service.stop_service(selected, store)
    assert not manager.active
    install_service(selected, store, command, persistent=True)
    assert manager.active


def test_service_ownership_record_does_not_pollute_profile_listing(tmp_path, manager):
    selected, store = profile(tmp_path, consent=True), ProfileStore(tmp_path/'profiles')
    store.save(selected)
    install_service(selected, store, [str(tmp_path/'bin'/'pseudolife-mcp')], persistent=True)
    assert store.list() == ['personal']
