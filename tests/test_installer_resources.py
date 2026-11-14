"""Read-only installer checks with fixture Docker inventories, never host Docker."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from string import Template

import pytest

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"
END = "# <<< pseudolife-mcp install <<<"


@pytest.fixture
def guard(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("installer_resources", ROOT / "ops/preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Model Docker resources independently of any lite bank on the host.
    monkeypatch.setattr(module.preflight_lite, "data_dirs", lambda: [tmp_path / "fixture-lite"])
    containers, volumes, busy, calls = [], [], set(), []

    def docker_run(argv, **kwargs):
        calls.append(argv[1:])
        if argv[1:3] == ["ps", "-a"]:
            out = "\n".join(c["Id"] for c in containers)
        elif argv[1] == "inspect":
            out = json.dumps(containers)
        elif argv[1:3] == ["volume", "ls"]:
            out = "\n".join(volumes)
        elif argv[1] == "compose":
            source = Path(argv[argv.index("--env-file") + 1])
            resolved = {}
            for line in source.read_text(encoding="utf-8-sig").splitlines():
                key, sep, value = line.partition("=")
                if not sep or key not in module.FIELDS:
                    continue
                if value.startswith("'"):
                    value = value.strip("'")
                else:
                    value = Template(value.strip('"')).safe_substitute(os.environ)
                resolved[key] = value
            out = "\n".join(f"{key}={value}" for key, value in resolved.items())
        elif argv[1] == "exec":
            c = next(c for c in containers if c["Id"] == argv[3])
            initial = c["Config"]["Env"][0].partition("=")[2]
            actual = c.get("_fixture_actual_password", initial)
            return subprocess.CompletedProcess(argv, int(kwargs["input"] != actual), "", "")
        else:
            pytest.fail(f"Unexpected Docker operation: {argv[1:]}")
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(module.subprocess, "run", docker_run)
    monkeypatch.setattr(module, "port_busy", lambda port: port in busy)
    for key in ("POSTGRES_PASSWORD", "PSEUDOLIFE_BANK_VOLUME", "PSEUDOLIFE_STATE_VOLUME"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / "ops/.env"
    env.parent.mkdir()
    return module, env, containers, volumes, busy, calls


def test_resource_fixture_is_independent_of_a_host_lite_bank(monkeypatch, tmp_path, request):
    host_bank = tmp_path / "host-lite"
    cluster = host_bank / "embedded_pg"
    cluster.mkdir(parents=True)
    marker = cluster / "PG_VERSION"
    marker.write_text("18\n", encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(host_bank))

    module, env, *_ = request.getfixturevalue("guard")
    module.check(env)

    assert marker.read_text(encoding="utf-8") == "18\n"
    assert not env.exists()


def container(name="daemon", *, project="pseudolife-mcp", running=True,
              volume="pseudolife-mcp-bank", password="fixture-password"):
    return {"Id": f"fixture-{name}", "Name": f"/pseudolife-mcp-{name}",
            "Config": {"Labels": {"com.docker.compose.project": project},
                       "Env": [f"POSTGRES_PASSWORD={password}"]},
            "State": {"Running": running},
            "Mounts": [{"Type": "volume", "Name": volume}],
            "HostConfig": {"PortBindings": {"8765/tcp": [{"HostPort": "8765"}]}},
            "NetworkSettings": {"Networks": {"pseudolife-mcp_default": {"IPAddress": "10.0.0.2"}},
                                "Ports": {"8765/tcp": [{"HostPort": "8765"}]}}}


@pytest.mark.parametrize("name", ["daemon", "postgres", "extractor", "other"])
@pytest.mark.parametrize("project", ["user-stack", ""])
def test_foreign_containers_including_stopped_are_refused_without_changes(guard, name, project):
    module, env, containers, _, _, calls = guard
    env.write_bytes(b"# keep exactly\r\n")
    containers.append(container(name, project=project, running=False))
    with pytest.raises(module.Refusal, match="Use that stack's own compose file"):
        module.check(env)
    assert env.read_bytes() == b"# keep exactly\r\n"
    assert all(c[0] in ("ps", "inspect", "volume", "compose") for c in calls)


@pytest.mark.parametrize("volume", ["pseudolife-mcp-bank", "pseudolife-mcp-state"])
def test_unrecorded_volume_requires_original_env_even_if_password_would_match(guard, volume):
    module, env, _, volumes, _, _ = guard
    volumes.append(volume)
    with pytest.raises(module.Refusal, match="Restore the original ops/.env"):
        module.check(env)
    assert not env.exists()


@pytest.mark.parametrize("port", [8765, 5433])
def test_busy_port_is_a_refusal_not_a_warning(guard, port):
    module, env, _, _, busy, _ = guard
    busy.add(port)
    with pytest.raises(module.Refusal, match=f"Stop or reconfigure the service listening on {port}"):
        module.check(env)
    assert not env.exists()


def test_our_managed_install_and_its_own_bound_port_are_allowed(guard):
    module, env, containers, volumes, busy, _ = guard
    env.write_text(f"{BEGIN}\n# extractor: sidecar\n{END}\nPOSTGRES_PASSWORD=fixture-password\n", encoding="utf-8")
    volumes.extend(["pseudolife-mcp-bank", "pseudolife-mcp-state"])
    containers.append(container())
    busy.add(8765)
    module.check(env)


def test_our_container_name_does_not_excuse_an_unrelated_listener(guard):
    module, env, containers, _, busy, _ = guard
    c = container()
    c["NetworkSettings"]["Ports"] = {}
    containers.append(c)
    busy.add(8765)
    with pytest.raises(module.Refusal, match="listening on 8765"):
        module.check(env)


@pytest.mark.parametrize("protocol,address", [("udp", "127.0.0.1"), ("tcp", "::1"), ("tcp", "10.0.0.2")])
def test_only_an_ipv4_loopback_tcp_publication_can_own_the_requested_port(guard, protocol, address):
    module, env, containers, _, busy, _ = guard
    value = container()
    value["NetworkSettings"]["Ports"] = {f"8765/{protocol}": [{"HostPort": "8765", "HostIp": address}]}
    containers.append(value)
    busy.add(8765)
    with pytest.raises(module.Refusal, match="listening on 8765"):
        module.check(env)


def test_pre_rename_volumes_recorded_in_crlf_env_are_allowed(guard):
    module, env, _, volumes, _, _ = guard
    content = b"PSEUDOLIFE_BANK_VOLUME=ops_pseudolife_pgdata\r\nPSEUDOLIFE_STATE_VOLUME=ops_pseudolife_data\r\n"
    env.write_bytes(content)
    volumes.extend(["ops_pseudolife_pgdata", "ops_pseudolife_data", "pseudolife-mcp-bank"])
    module.check(env)
    assert env.read_bytes() == content


def test_a_pre_rename_bank_is_not_replaced_by_an_empty_default_bank(guard):
    module, env, _, volumes, _, _ = guard
    volumes.append("ops_pseudolife_pgdata")
    with pytest.raises(module.Refusal, match="PSEUDOLIFE_BANK_VOLUME=ops_pseudolife_pgdata"):
        module.check(env)
    assert not env.exists()


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_preflight_precedes_every_local_mutation(script):
    text = (ROOT / "ops" / script).read_text(encoding="utf-8")
    preflight = text.index('"$repo/ops/preflight.sh"' if script.endswith(".sh")
                           else '& (Join-Path $PSScriptRoot "preflight.ps1")')
    assert preflight < text.index("docker volume create")
    assert preflight < text.index("# >>> mint token >>>")
    assert preflight < text.index("# >>> sidecar override >>>")


def test_changed_postgres_password_refuses_before_writing_anything(guard):
    module, env, containers, _, _, _ = guard
    env.write_text("POSTGRES_PASSWORD=different-fixture-password\n", encoding="utf-8")
    containers.append(container("postgres"))
    with pytest.raises(module.Refusal, match="Restore POSTGRES_PASSWORD") as error:
        module.check(env)
    assert "fixture-password" not in str(error.value)


@pytest.mark.parametrize("quoted,password", [
    ("fixture$SAMPLE_PART", "fixtureresolved"), ("'fixture$$word'", "fixture$$word")])
def test_compose_resolved_password_allows_a_legitimate_rerun(guard, monkeypatch, quoted, password):
    module, env, containers, _, _, _ = guard
    monkeypatch.setenv("SAMPLE_PART", "resolved")
    env.write_text(f"POSTGRES_PASSWORD={quoted}\n", encoding="utf-8")
    containers.append(container("postgres", password=password))
    module.check(env)


def test_literal_container_password_does_not_mask_compose_interpolation(guard, monkeypatch):
    module, env, containers, _, _, _ = guard
    monkeypatch.setenv("SAMPLE_PART", "resolved")
    env.write_text("POSTGRES_PASSWORD=fixture$SAMPLE_PART\n", encoding="utf-8")
    containers.append(container("postgres", password="fixture$SAMPLE_PART"))
    with pytest.raises(module.Refusal, match="Restore POSTGRES_PASSWORD"):
        module.check(env)


def test_valid_rotated_password_is_not_compared_to_container_initialization(guard):
    module, env, containers, _, _, calls = guard
    env.write_text("POSTGRES_PASSWORD=rotated-fixture\n", encoding="utf-8")
    c = container("postgres", password="old-fixture")
    c["_fixture_actual_password"] = "rotated-fixture"
    containers.append(c)
    module.check(env)
    probe = next(call for call in calls if call[0] == "exec")
    assert probe[-1] == "10.0.0.2"  # never the initdb-trusted loopback address
    assert "rotated-fixture" not in " ".join(probe)


def test_resolved_volume_names_are_the_only_values_printed(guard, monkeypatch, capsys):
    module, env, _, _, _, _ = guard
    env.write_text('PSEUDOLIFE_BANK_VOLUME="custom-bank"\nPSEUDOLIFE_STATE_VOLUME=${STATE_NAME}\nPOSTGRES_PASSWORD=fixture-private\n', encoding="utf-8")
    monkeypatch.setenv("STATE_NAME", "custom-state")
    monkeypatch.setattr(sys, "argv", ["preflight.py", "--env-file", str(env), "--volume-names"])
    assert module.main() == 0
    assert capsys.readouterr().out == "custom-bank\ncustom-state\n"


def test_ps_volume_creation_consumes_resolved_names_and_stops_on_failure(tmp_path):
    from tests.test_installer_client_only_ps import _PowerShell, _q, _output
    text = (ROOT / "ops/install.ps1").read_text(encoding="utf-8")
    block = text.split("# >>> volumes >>>\n", 1)[1].split("# <<< volumes <<<", 1)[0]
    # Creation runs in its own block, after the engine stage can refuse.
    block += text.split("# >>> create volumes >>>\n", 1)[1].split("# <<< create volumes <<<", 1)[0]
    ps = _PowerShell(tmp_path)
    proc = ps.run(
        "$ClientOnly=$false\n$envFile='fixture.env'\n"
        # The native client's volume-names step, answering resolved names.
        "function Invoke-NativeHelper {\n"
        "    if ($args[0] -ne 'volume-names') { throw \"unexpected native step $($args[0])\" }\n"
        "    'custom-bank'; 'custom-state'; $global:LASTEXITCODE=0\n"
        "}\n"
        + ps.stub_function("docker", "if ($args[1] -eq 'ls') { $global:LASTEXITCODE=0 } else { $global:LASTEXITCODE=1 }")
        + block + "\n'CONTINUED'\n")
    assert proc.returncode != 0
    assert "Could not create volume custom-bank" in _output(proc)
    assert ps.logged() == ["docker|volume ls --format {{.Name}}", "docker|volume create custom-bank"]
    assert "CONTINUED" not in _output(proc)


def test_ipv6_only_listener_does_not_block_ipv4_compose_publication(guard, monkeypatch):
    module, _, _, _, _, _ = guard
    # The fixture replaced port_busy; retrieve the actual function from its source.
    spec = importlib.util.spec_from_file_location("port_guard", ROOT / "ops/preflight.py")
    ports = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ports)

    class Socket:
        def __init__(self, family, kind):
            self.family = family
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def setsockopt(self, *args):
            pass
        def bind(self, address):
            if self.family == ports.socket.AF_INET6:
                raise OSError(98, "IPv6-only fixture listener")

    monkeypatch.setattr(ports.socket, "socket", Socket)
    assert ports.port_busy(8765) is False


def test_recorded_volume_attached_to_a_foreign_container_is_not_ours(guard):
    module, env, containers, volumes, _, _ = guard
    env.write_text("PSEUDOLIFE_BANK_VOLUME=custom-bank\n", encoding="utf-8")
    volumes.append("custom-bank")
    c = container(project="user-stack", volume="custom-bank")
    c["Name"] = "/user-bank"
    containers.append(c)
    with pytest.raises(module.Refusal, match="attached to another stack"):
        module.check(env)


def test_docker_failure_is_closed_and_does_not_echo_docker_stderr(guard, monkeypatch):
    module, env, _, _, _, _ = guard
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, "", "fixture-secret"))
    with pytest.raises(module.Refusal, match="docker info") as error:
        module.check(env)
    assert "fixture-secret" not in str(error.value)


@pytest.mark.parametrize("script", ["preflight.sh", "preflight.ps1"])
def test_preflight_refuses_a_foreign_stack_before_install_can_continue(tmp_path, script):
    """Run the actual preflight stage with fixture Docker, on both native shells."""
    from tests.test_installer_client_only import _Shell, _stub
    from tests.test_installer_existing_upgrade import _bash_variants
    from tests.test_installer_client_only_ps import _PowerShell, _q, _output

    text = (ROOT / "ops" / script).read_text(encoding="utf-8")
    start = text.index("# -- ports" if script.endswith(".ps1") else "# ── ports")
    finish = text.index("# -- git" if script.endswith(".ps1") else "# ── git", start)
    block = text[start:finish]
    # A subprocess fixture helper loads the real check, but supplies Docker's
    # read-only replies; no child is allowed access to the host Docker socket.
    fixture = tmp_path / "repo/ops"
    fixture.mkdir(parents=True)
    helper = fixture / "preflight.py"
    helper.write_text(
        "import importlib.util, json, subprocess, sys\n"
        f"s=importlib.util.spec_from_file_location('guard', {str(ROOT / 'ops/preflight.py')!r})\n"
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
        f"inventory={container(project='user-stack', running=False)!r}\n"
        "def run(argv, **kw):\n"
        "    out='fixture' if argv[1]=='ps' else json.dumps([inventory])\n"
        "    return subprocess.CompletedProcess(argv,0,out,'')\n"
        "m.subprocess.run=run\nm.port_busy=lambda port: False\n"
        "sys.exit(m.main())\n", encoding="utf-8")
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        proc = ps.run(
            f"$PSScriptRoot='{_q(fixture)}'\n"
            f"function python {{ & '{_q(sys.executable)}' @args }}\n"
            "function docker { 'pseudolife-mcp-daemon'; $global:LASTEXITCODE=0 }\n"
            "function Get-NetTCPConnection { }\nfunction Ok {}\nfunction Warn {}\n"
            "function Fail { throw $args[0] }\n" + block + "\n'CONTINUED'\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        _stub(shell.bin / "docker", "echo pseudolife-mcp-daemon\n")
        _stub(shell.bin / "python3", f"exec '{shell.path(sys.executable)}' \"$@\"\n")
        block = block.replace('preflight_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
                              f"preflight_dir='{shell.path(fixture)}'")
        proc = shell.run(
            f"preflight_dir='{shell.path(fixture)}'\n"
            f"resource_python='{shell.path(sys.executable)}'\n"
            "ok() { :; }; warn() { :; }; fail() { exit 1; }\n"
            + block + "\necho CONTINUED\n")
        output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "CONTINUED" not in output
    assert "Use that stack's own compose file" in output


@pytest.mark.parametrize("project", ["user-stack", "", "pseudolife-mcp"])
@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_extractor_removal_rechecks_project_and_uses_verified_id(tmp_path, script, project):
    from tests.native_client_stub import HELPER_PS, HELPER_SH
    from tests.test_installer_client_only import _Shell, _stub
    from tests.test_installer_existing_upgrade import _bash_variants
    from tests.test_installer_client_only_ps import _PowerShell, _q, _output

    text = (ROOT / "ops" / script).read_text(encoding="utf-8")
    block = text.split("# >>> sidecar override >>>\n", 1)[1].split("# <<< sidecar override <<<", 1)[0]
    override = tmp_path / "override.yml"
    marker = "# fixture managed override"
    override.write_text(marker + "\n")
    identifier = "a" * 64
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        proc = ps.run(
            ps.stub_function("docker", "if ($args[0] -eq 'inspect') { "
                             f"'{identifier} {project}'" + " }\n$global:LASTEXITCODE=0")
            + f"$ClientOnly=$false\n$overrideFile='{_q(override)}'\n"
            f"$OverrideMarker='{marker}'\n$LegacyOverrideMarker='old'\n$LegacyShimOverrideMarker='older'\n"
            "function Test-ExtractorDisablesSidecar { $true }\n" + HELPER_PS + block)
        output, calls = _output(proc), ps.logged()
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        _stub(shell.bin / "docker", 'printf "docker|%s\\n" "$*" >>"$CALL_LOG"\n'
              f'if [ "$1" = inspect ]; then echo "{identifier} {project}"; fi\n')
        proc = shell.run(f"CLIENT_ONLY=''\noverride_file='{shell.path(override)}'\n"
                         f"OVERRIDE_MARKER='{marker}'\nLEGACY_OVERRIDE_MARKER=old\nLEGACY_SHIM_OVERRIDE_MARKER=older\n"
                         "extractor_disables_sidecar() { return 0; }\n" + HELPER_SH
                         # The override is written through the installer's own state step.
                         + next(line for line in text.splitlines() if line.startswith("installer_state() {"))
                         + "\n" + block)
        output, calls = proc.stdout + proc.stderr, shell.logged()
    assert proc.returncode == 0, output
    removed = [call for call in calls if call.startswith("docker|rm")]
    assert removed == ([f"docker|rm -f {identifier}"] if project == "pseudolife-mcp" else [])
