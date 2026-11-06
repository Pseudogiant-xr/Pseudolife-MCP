"""Docker-tier engine selection in the installers (G2-B)."""
import importlib.util
import os
import sys
import json
import subprocess

import pytest

from pseudolife_memory import update_cli as up
from pseudolife_memory import update_engine as eng
from tests.test_installer_client_only import ROOT, _Shell
from tests.test_installer_client_only_ps import _PowerShell, _output
from tests.test_installer_existing_upgrade import _bash_variants


@pytest.fixture
def helper(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("installer_engine_under_test", ROOT / "ops/installer_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path / "data"))
    state = {"labels": None, "calls": [], "fail": None}

    def docker(*args, capture=True, timeout=60):
        state["calls"].append(list(args))
        code, out, err = 0, "", ""
        if args[:1] == ("inspect",):
            if state["labels"] is None:
                code, err = 1, "Error: No such object: pseudolife-mcp-daemon"
            else:
                out = json.dumps(state["labels"])
        elif args[:2] == ("volume", "inspect"):
            code = 0 if args[2] in state.setdefault("volumes", set()) else 1
        elif args[:2] == ("volume", "create"):
            state["volumes"].add(args[-1])
        elif args[:1] == ("pull",):
            code = int(state["fail"] == "pull")
        elif args[:1] == ("run",) and "--schema-version" in args:
            out = str(state.get("image_schema", 58))
        elif args[:1] == ("run",):
            code = int(state["fail"] == "provision")
        return subprocess.CompletedProcess(args, code, out, err)

    monkeypatch.setattr(module, "docker", docker)
    monkeypatch.setattr(module, "preflight_disk", lambda: state["calls"].append(["disk-check"]))
    state["health"] = {"status": "ok", "ready": True, "version": "0.23.0", "schema": 58}
    state["read"] = True
    monkeypatch.setattr(module, "health", lambda url: state["health"])
    monkeypatch.setattr(eng, "authenticated_read", lambda url, token: state["read"] and token == "fixture-bearer")
    env = tmp_path / "ops.env"
    env.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-bearer\n")
    state["env"] = env
    repo = tmp_path / "checkout"
    (repo / "ops").mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.23.0"\n')
    return module, state, repo


def _started(module, state):
    """What Compose does with the overlay: the container carries its labels."""
    state["labels"] = {"com.docker.compose.project": "pseudolife-mcp", **module.read_overlay()["labels"]}


def test_fresh_rust_install_pulls_provisions_then_records_only_after_health(helper):
    module, state, repo = helper
    assert module.detect() == "none"
    [overlay] = module.prepare(repo, "rust")
    order = [c[0] for c in state["calls"] if c[0] in ("disk-check", "pull", "run")]
    assert order == ["disk-check", "pull", "run"]
    pull, provision = [c for c in state["calls"] if c[0] in ("pull", "run")]
    assert pull == ["pull", "ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.23.0"]
    assert "pseudolife-mcp_pseudolife_rust_models:/models" in provision
    assert "PSEUDOLIFE_DAEMON_MODELS_DIR=/models" in provision and not any(":/data" in a for a in provision)
    create = next(c for c in state["calls"] if c[:2] == ["volume", "create"])
    assert "com.docker.compose.project=pseudolife-mcp" in create
    assert provision[-3:] == ["provision", "models", "--if-missing"]
    text = overlay.read_text(encoding="utf-8")
    assert overlay.name == "docker-compose.engine-installer.yml" and eng.transient_overlay(overlay)
    assert "build: !reset null" in text and '"pseudolife_rust_models:/models"' in text and '"ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:0.23.0"' in text
    assert not eng.release_accepted().exists()
    _started(module, state)
    assert module.detect() == "rust-unaccepted"
    assert module.accept(env_file=state["env"]) == "accepted"
    assert module.detect() == "rust"
    # update verifies the same record the installer wrote.
    assert eng._validated_engine_selection(state["labels"])["image"].endswith("-rust:0.23.0")


def test_accept_waits_for_a_ready_daemon_at_its_release(helper):
    module, state, repo = helper
    module.prepare(repo, "rust")
    _started(module, state)
    good = {"status": "ok", "ready": True, "version": "0.23.0", "schema": 58}
    for bad, read in (({**good, "ready": False}, True), ({**good, "version": "0.22.1"}, True),
                      ({**good, "counters_import_required": True}, True), ({**good, "schema": 59}, True),
                      (good, False)):
        state["health"], state["read"] = bad, read
        with pytest.raises(module.Refusal, match="not ready"):
            module.accept(env_file=state["env"])
        assert not eng.release_accepted().exists()
    # No bearer in the env file: the authenticated read cannot pass either.
    state["health"], state["read"] = good, True
    with pytest.raises(module.Refusal, match="authenticated"):
        module.accept(env_file=state["env"].with_name("missing.env"))


def test_a_removed_container_keeps_the_accepted_rust_selection(helper):
    module, state, repo = helper
    [overlay] = module.prepare(repo, "rust")
    _started(module, state)
    module.accept(env_file=state["env"])
    before, calls = overlay.read_bytes(), len(state["calls"])
    state["labels"] = None  # docker compose down
    assert module.detect() == "rust"
    assert module.prepare(repo, "rust") == [overlay] and overlay.read_bytes() == before
    assert not any(c[0] in ("pull", "run") for c in state["calls"][calls:])


def test_a_later_checkout_selection_supersedes_a_removed_release_selection(helper):
    import os as _os
    module, state, repo = helper
    module.prepare(repo, "rust")
    _started(module, state)
    module.accept(env_file=state["env"])
    state["labels"] = None  # docker compose down after a checkout-mode switch back
    accepted = repo / eng.PYTHON_ACCEPTED
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.write_text("{}")
    later = eng.release_accepted().stat().st_mtime_ns + 1_000_000_000
    _os.utime(accepted, ns=(later, later))
    assert module.detect(repo) == "python"
    assert module.detect() == "rust"


def test_an_unaccepted_rust_daemon_is_redone_only_for_this_checkouts_release(helper):
    module, state, repo = helper
    module.prepare(repo, "rust")
    _started(module, state)  # never accepted: its health check failed
    assert module.detect() == "rust-unaccepted"
    pulls = len([c for c in state["calls"] if c[0] == "pull"])
    module.prepare(repo, "rust")
    assert len([c for c in state["calls"] if c[0] == "pull"]) == pulls + 1
    assert module.prepare(repo, "python") == []
    for release in ("0.24.0", None):  # another release, or a Rust container without labels
        state["labels"] = {eng.ENGINE_LABEL: "rust", **({eng.RELEASE_LABEL: release} if release else {})}
        for engine in ("rust", "python"):
            with pytest.raises(module.Refusal, match="docker rm -f pseudolife-mcp-daemon"):
                module.prepare(repo, engine)


def test_rerun_on_an_accepted_host_reproduces_the_overlay_without_pulling(helper):
    module, state, repo = helper
    [overlay] = module.prepare(repo, "rust")
    _started(module, state)
    module.accept(env_file=state["env"])
    before, calls = overlay.read_bytes(), len(state["calls"])
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.24.0"\n')
    assert module.prepare(repo, "rust") == [overlay]
    assert overlay.read_bytes() == before
    assert not any(c[0] in ("pull", "run") for c in state["calls"][calls:])
    assert module.accept(env_file=state["env"]) == "unchanged"


@pytest.mark.parametrize("failure,expected", [("pull", "--engine python"), ("provision", "models")])
def test_rust_refuses_before_any_container_when_the_image_or_models_are_unavailable(helper, failure, expected):
    module, state, repo = helper
    state["fail"] = failure
    with pytest.raises(module.Refusal, match=expected):
        module.prepare(repo, "rust")
    assert not module.overlay_path().exists()


def test_accept_refuses_a_container_this_install_did_not_start(helper):
    module, state, repo = helper
    module.prepare(repo, "rust")
    state["labels"] = {eng.ENGINE_LABEL: "rust", eng.RELEASE_LABEL: "0.23.0", eng.ACTIVATION_LABEL: "someone-else"}
    with pytest.raises(module.Refusal, match="not the one"):
        module.accept(env_file=state["env"])
    assert not eng.release_accepted().exists()


def test_python_and_checkout_selections(helper):
    module, state, repo = helper
    assert module.prepare(repo, "python") == []
    state["labels"] = {eng.ENGINE_LABEL: "python"}
    assert module.detect() == "python"
    state["labels"] = {eng.ENGINE_LABEL: "rust", eng.CHECKOUT_LABEL: str(repo), eng.ACTIVATION_LABEL: "a"}
    assert module.detect() == "rust-checkout"
    # The installer never rebuilds a checkout-mode Rust daemon: update does.
    with pytest.raises(module.Refusal, match="update --checkout"):
        module.prepare(repo, "rust-checkout")
    # With its container removed, the checkout's own selection still says Rust.
    state["labels"] = None
    (repo / "data/update-engine").mkdir(parents=True)
    (repo / eng.SELECTION).write_text("{}")
    assert module.detect(repo) == "rust-checkout"
    assert module.detect() == "none"


def test_unreleased_checkout_version_refuses_rust(helper):
    module, state, repo = helper
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.23.0.dev1"\n')
    with pytest.raises(module.Refusal, match="--engine python"):
        module.prepare(repo, "rust")
    assert not state["calls"] or all(c[0] == "inspect" for c in state["calls"])


def test_planner_checks_the_engine_overlays_it_was_given(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("installer_state_overlays", ROOT / "ops/installer_state.py")
    state = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(state)
    seen = []

    def run(argv):
        seen.append(argv)
        if argv[0] == "git":
            return "head" if "rev-parse" in argv else ""
        if "--hash" in argv:
            return ""
        if argv[1] == "compose":
            return json.dumps({"services": {}})
        return ""

    monkeypatch.setattr(state, "run", run)
    overlay = tmp_path / "docker-compose.engine-installer.yml"
    state.deployment(tmp_path, tmp_path / ".env", tmp_path / "absent.yml", [overlay])
    compose = [argv for argv in seen if argv[1:2] == ["compose"]]
    assert compose and all(argv[argv.index(str(overlay)) - 1] == "-f" for argv in compose)


def _stage(script, start, end):
    source = (ROOT / "ops" / script).read_text(encoding="utf-8")
    return source[source.index(start):source.index(end)]


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("installed,flag,bank,compose_engine,switch", [
    ("none", "", "", "python", ""), ("none", "rust", "", "rust", ""),
    # An existing bank with no daemon reaches Rust through update's checks.
    ("none", "rust", "1", "python", "rust"),
    ("python", "", "1", "python", ""), ("python", "rust", "1", "python", "rust"),
    ("rust", "", "1", "rust", ""), ("rust", "python", "1", "rust", "python"), ("rust", "rust", "1", "rust", ""),
    ("rust-checkout", "python", "1", "rust-checkout", "python"),
    ("rust-unaccepted", "", "1", "rust", ""), ("rust-unaccepted", "python", "1", "python", ""),
])
def test_engine_stage_keeps_the_installed_engine_and_defers_a_switch(tmp_path, script, installed, flag, bank,
                                                                     compose_engine, switch):
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 4c. create volumes")
        definition = stage[stage.index("function Invoke-EngineHelper"):stage.index("$installedEngine =")]
        ps = _PowerShell(tmp_path)
        stub = ("function Invoke-EngineHelper {\n"
                f"    Add-Content -LiteralPath '{ps.calls}' -Value ('helper|' + (@($args) -join ' '))\n"
                "    $global:LASTEXITCODE = 0\n"
                f"    if ($args[0] -eq 'detect') {{ return '{installed}' }}\n"
                "    if ($args[4] -ne 'python') { return 'C:\\fixture\\engine.yml' }\n}\n")
        proc = ps.run(f"$ClientOnly=$false\n$Engine='{flag}'\n$bankExisted=${'true' if bank else 'false'}\n" + stub
                      + stage.replace(definition, "")
                      + "Write-Output \"RESULT:$composeEngine|$switchEngine|$($engineFiles -join ',')\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 4c. create volumes")
        definition = next(line for line in stage.splitlines() if line.startswith("engine_helper()"))
        shell = _Shell(variants[0], tmp_path)
        stub = ('engine_helper() { printf "helper|%s\\n" "$*" >>"$CALL_LOG"; '
                f'case "$1" in detect) echo {installed} ;; prepare) [ "$5" = python ] || echo /fixture/engine.yml ;; esac; }}\n'
                'step() { echo "STEP: $*"; }\n')
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE='{flag}'\nBANK_EXISTED='{bank}'\n" + stub
                         + stage.replace(definition, "")
                         + 'echo "RESULT:$COMPOSE_ENGINE|$SWITCH_ENGINE|${engine_files[*]-}"\n')
        output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    result = next(line for line in output.splitlines() if line.startswith("RESULT:"))
    engine, switched, files = result[len("RESULT:"):].split("|")
    assert (engine, switched) == (compose_engine, switch)
    assert bool(files) == (compose_engine != "python")
    assert ("unchanged" in output) == (installed not in ("none", "rust-unaccepted") and not switch)
    assert ("switching to" in output) == bool(switch)


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_compose_and_planner_carry_the_engine_overlays(tmp_path, script):
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 7. bring the stack up", "# -- 8b. Claude Code plugin")
        ps = _PowerShell(tmp_path)
        state = (f"function Invoke-InstallerState {{ Add-Content -LiteralPath '{ps.calls}' -Value ('state|' + (@($args) -join ' ')); "
                 "'{\"action\":\"start\"}' }\n")
        proc = ps.run("$ClientOnly=$false\n$envFile='fixture.env'\n$composeFile='fixture.yml'\n$overrideFile='missing.yml'\n"
                      "$engineFiles=@('C:\\fixture\\engine.yml')\n" + state + ps.stub_function("docker") + stage)
        calls, output, overlay = ps.logged(), _output(proc), "C:\\fixture\\engine.yml"
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        stage = _stage(script, "# ── 7. bring the stack up", "# ── 8b. Claude Code plugin")
        proc = shell.run("CLIENT_ONLY=''\nenv_file=fixture.env\ncompose_file=fixture.yml\noverride_file=missing.yml\n"
                         "engine_files=(/fixture/engine.yml)\n"
                         'installer_state() { printf "state|%s\\n" "$*" >>"$CALL_LOG"; echo \'{"action":"start"}\'; }\n'
                         + stage)
        calls, output, overlay = shell.logged(), proc.stdout + proc.stderr, "/fixture/engine.yml"
    assert proc.returncode == 0, output
    [up_call] = [call for call in calls if " up " in call]
    assert f"-f {overlay}" in up_call
    planner = [call for call in calls if call.startswith("state|")]
    assert len(planner) == 2 and all(f"--overlay {overlay}" in call for call in planner)


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("case", ["client-only", "no-token", "http-without-token", "allowed", "http-with-token"])
def test_engine_flag_refusals(tmp_path, script, case):
    client, token = case == "client-only", case == "no-token"
    http = case.startswith("http")
    env = tmp_path / "fixture.env"
    env.write_text("PSEUDOLIFE_MCP_TOKEN=fixture\n" if case == "http-with-token" else "# no token\n")
    if script.endswith(".ps1"):
        stage = _stage(script, "# >>> engine flag >>>", "# <<< engine flag <<<")
        ps = _PowerShell(tmp_path)
        # PowerShell's ValidateSet lets "Rust" through: the block lowercases it.
        proc = ps.run(f"$ClientOnly=${str(client).lower()}\n$NoToken=${str(token).lower()}\n$Engine='Rust'\n"
                      f"$Transport='{'http' if http else 'shim'}'\n$envFile='{env}'\n"
                      + stage + "Write-Output \"ENGINE:$Engine\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# >>> engine flag >>>", "# <<< engine flag <<<")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run(f"CLIENT_ONLY='{'1' if client else ''}'\nNO_TOKEN='{'1' if token else ''}'\n"
                         f"TRANSPORT={'http' if http else 'shim'}\nenv_file='{shell.path(env)}'\n"
                         "ENGINE=rust\n" + stage + 'echo "ENGINE:$ENGINE"\n')
        output = proc.stdout + proc.stderr
    if case in ("allowed", "http-with-token"):
        assert proc.returncode == 0, output
        assert "ENGINE:rust" in output
    else:
        assert proc.returncode == 2
        assert ("client-only" if client else "token") in output.lower()
        assert "Nothing was changed" in output


def test_no_volume_is_created_before_the_engine_stage_can_refuse():
    # A refused Rust install must leave nothing a rerun's preflight would refuse.
    for script in ("install.sh", "install.ps1"):
        source = (ROOT / "ops" / script).read_text(encoding="utf-8")
        engine = source.index("# >>> daemon engine >>>")
        create = source.index("# >>> create volumes >>>")
        assert engine < create, script
        calls = [i for i, line in enumerate(source.splitlines()) if line.strip().startswith("docker volume create")]
        assert len(calls) == 1, script
        assert calls[0] > len(source[:create].splitlines()), script


def test_the_engine_flag_is_checked_after_client_only_is_inferred():
    for script, flag, first in (("install.sh", "# >>> engine flag >>>", "# ── 1. provider selection"),
                                ("install.ps1", "# >>> engine flag >>>", "# -- 1. provider selection")):
        source = (ROOT / "ops" / script).read_text(encoding="utf-8")
        inferred = source.rindex("CLIENT_ONLY=1" if script.endswith(".sh") else "$script:ClientOnly = [switch]$true")
        assert inferred < source.index(flag) < source.index(first), script


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("engine", ["python", "rust"])
def test_the_container_probe_uses_what_the_daemon_image_has(tmp_path, script, engine):
    # The Rust image has curl and no Python.
    if script.endswith(".ps1"):
        source = (ROOT / "ops" / script).read_text(encoding="utf-8")
        start = source.index("function Invoke-EndpointContainerProbe")
        end = source.index("\n}\n", start) + 3
        ps = _PowerShell(tmp_path)
        proc = ps.run(f"$composeEngine='{engine}'\n$Extractor='endpoint'\n$ExtractorUrl='http://127.0.0.1:11434/v1'\n"
                      "function ConvertTo-ExtractorContainerUrl($u) { 'http://host.docker.internal:11434/v1' }\n"
                      "function Test-LinuxHost { $false }\n" + ps.stub_function("docker") + source[start:end]
                      + "Invoke-EndpointContainerProbe\n")
        calls, output = ps.logged(), _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        source = (ROOT / "ops" / script).read_text(encoding="utf-8")
        start = source.index("container_http_probe() {")
        end = source.index("\n}\n", source.index("    esac", start)) + 3
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run(f"COMPOSE_ENGINE={engine}\n" + source[start:end]
                         + "container_http_probe http://host.docker.internal:11434/v1\n")
        calls, output = shell.logged(), proc.stdout + proc.stderr
    [probe] = [call for call in calls if call.startswith("docker|exec")]
    assert (" curl " in probe) == (engine == "rust"), output
    assert (" python " in probe) == (engine == "python"), output


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("installed", ["python", "rust-checkout"])
def test_the_delegated_switch_runs_update_with_stdin_closed(tmp_path, script, installed):
    if script.endswith(".ps1"):
        stage = _stage(script, "# >>> engine switch >>>", "# <<< engine switch <<<")
        ps = _PowerShell(tmp_path)
        # A native program, as the real shim is: `$null |` closes its stdin.
        script = tmp_path / "fake_shim.py"
        script.write_text("import sys\nread = sys.stdin.read()\n"
                          f"open({str(ps.calls)!r}, 'a').write('shim|' + ' '.join(sys.argv[1:]) + '|stdin=' + str(len(read)) + chr(10))\n")
        if os.name == "nt":
            fake = tmp_path / "fake_shim.cmd"
            fake.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        else:
            fake = tmp_path / "fake_shim"
            fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
            fake.chmod(0o755)
        proc = ps.run(f"$switchEngine='rust'\n$installedEngine='{installed}'\n"
                      f"function Install-ShimOnce {{ $true }}\n$script:shimInstallPath='{fake}'\n"
                      "function global:Invoke-RestMethod { @{ version = '0.22.1' } }\n" + stage,
                      stdin_text="y\n")
        calls, output = ps.logged(), _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        fake = tmp_path / "fake_shim.sh"
        fake.write_text('#!/bin/sh\nread -r answer || answer=EOF\nprintf "shim|%s|stdin=%s\\n" "$*" "$answer" >>"$CALL_LOG"\n')
        fake.chmod(0o755)
        stage = _stage(script, "# >>> engine switch >>>", "# <<< engine switch <<<")
        proc = shell.run(f"SWITCH_ENGINE=rust\nINSTALLED_ENGINE={installed}\nrepo=/fixture/repo\n"
                         f"SHIM_OK=1\nSHIM_PATH='{shell.path(fake)}'\nensure_shim() {{ :; }}\n"
                         "step() { echo \"STEP: $*\"; }\n"
                         "export FAKE_HEALTH='{\"status\":\"ok\",\"version\":\"0.22.1\"}'\n" + stage)
        calls, output = shell.logged(), proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    [call] = [c for c in calls if c.startswith("shim|")]
    assert "update --engine rust --daemon-only" in call
    assert ("--checkout" in call) == (installed == "rust-checkout")
    assert ("--tag 0.22.1" in call) == (installed == "python")
    assert call.endswith("stdin=EOF") or call.endswith("stdin=0"), call


def test_update_treats_the_installer_overlay_as_its_own_transient_file(tmp_path):
    assert eng.transient_overlay(up.data_dir() / "update-engine" / "docker-compose.engine-installer.yml")
