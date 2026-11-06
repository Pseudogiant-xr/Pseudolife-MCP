"""Docker-tier engine selection in the installers (G2-B)."""
import importlib.util
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
        elif args[:1] == ("run",):
            code = int(state["fail"] == "provision")
        return subprocess.CompletedProcess(args, code, out, err)

    monkeypatch.setattr(module, "docker", docker)
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
    assert module.detect() == "rust"
    assert module.accept() == "accepted"
    # update verifies the same record the installer wrote.
    assert eng._validated_engine_selection(state["labels"])["image"].endswith("-rust:0.23.0")


def test_rerun_on_an_accepted_host_reproduces_the_overlay_without_pulling(helper):
    module, state, repo = helper
    [overlay] = module.prepare(repo, "rust")
    _started(module, state)
    module.accept()
    before, calls = overlay.read_bytes(), len(state["calls"])
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.24.0"\n')
    assert module.prepare(repo, "rust") == [overlay]
    assert overlay.read_bytes() == before
    assert not any(c[0] in ("pull", "run") for c in state["calls"][calls:])
    assert module.accept() == "unchanged"


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
        module.accept()
    assert not eng.release_accepted().exists()


def test_python_and_checkout_selections(helper):
    module, state, repo = helper
    assert module.prepare(repo, "python") == []
    state["labels"] = {eng.ENGINE_LABEL: "python"}
    assert module.detect() == "python"
    state["labels"] = {eng.ENGINE_LABEL: "rust", eng.CHECKOUT_LABEL: str(repo), eng.ACTIVATION_LABEL: "a"}
    assert module.detect() == "rust-checkout"
    (repo / "data/update-engine").mkdir(parents=True)
    (repo / eng.SELECTION).write_text("{}")
    assert module.prepare(repo, "rust-checkout") == [repo / "ops/docker-compose.rust-daemon.yml",
                                                                      repo / eng.SELECTION]
    state["labels"][eng.CHECKOUT_LABEL] = str(repo.parent / "other")
    with pytest.raises(module.Refusal, match="checkout-mode update"):
        module.prepare(repo, "rust-checkout")


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
@pytest.mark.parametrize("installed,flag,compose_engine,switch", [
    ("none", "", "python", ""), ("none", "rust", "rust", ""), ("python", "", "python", ""),
    ("python", "rust", "python", "rust"), ("rust", "", "rust", ""), ("rust", "python", "rust", "python"),
    ("rust", "rust", "rust", ""), ("rust-checkout", "python", "rust-checkout", "python"),
])
def test_engine_stage_keeps_the_installed_engine_and_defers_a_switch(tmp_path, script, installed, flag,
                                                                     compose_engine, switch):
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 5. managed env block")
        definition = stage[stage.index("function Invoke-EngineHelper"):stage.index("$installedEngine =")]
        ps = _PowerShell(tmp_path)
        stub = ("function Invoke-EngineHelper {\n"
                f"    Add-Content -LiteralPath '{ps.calls}' -Value ('helper|' + (@($args) -join ' '))\n"
                "    $global:LASTEXITCODE = 0\n"
                f"    if ($args[0] -eq 'detect') {{ return '{installed}' }}\n"
                "    if ($args[4] -ne 'python') { return 'C:\\fixture\\engine.yml' }\n}\n")
        proc = ps.run(f"$ClientOnly=$false\n$Engine='{flag}'\n$stateVol='fixture-state'\n" + stub
                      + stage.replace(definition, "")
                      + "Write-Output \"RESULT:$composeEngine|$switchEngine|$($engineFiles -join ',')\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 5. managed env block")
        definition = next(line for line in stage.splitlines() if line.startswith("engine_helper()"))
        shell = _Shell(variants[0], tmp_path)
        stub = ('engine_helper() { printf "helper|%s\\n" "$*" >>"$CALL_LOG"; '
                f'case "$1" in detect) echo {installed} ;; prepare) [ "$5" = python ] || echo /fixture/engine.yml ;; esac; }}\n'
                'step() { echo "STEP: $*"; }\n')
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE='{flag}'\nstate_vol=fixture-state\n" + stub
                         + stage.replace(definition, "")
                         + 'echo "RESULT:$COMPOSE_ENGINE|$SWITCH_ENGINE|${engine_files[*]-}"\n')
        output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    result = next(line for line in output.splitlines() if line.startswith("RESULT:"))
    engine, switched, files = result[len("RESULT:"):].split("|")
    assert (engine, switched) == (compose_engine, switch)
    assert bool(files) == (compose_engine != "python")
    assert ("unchanged" in output) == (installed != "none")


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
def test_engine_flag_is_refused_on_a_client_only_install(tmp_path, script):
    if script.endswith(".ps1"):
        stage = _stage(script, "# >>> engine flag >>>", "# <<< engine flag <<<")
        ps = _PowerShell(tmp_path)
        proc = ps.run("$ClientOnly=$true\n$Engine='rust'\n" + stage)
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# >>> engine flag >>>", "# <<< engine flag <<<")
        proc = _Shell(variants[0], tmp_path).run("CLIENT_ONLY=1\nENGINE=rust\n" + stage)
        output = proc.stdout + proc.stderr
    assert proc.returncode == 2
    assert "client-only" in output and "Nothing was changed" in output


def test_update_treats_the_installer_overlay_as_its_own_transient_file(tmp_path):
    assert eng.transient_overlay(up.data_dir() / "update-engine" / "docker-compose.engine-installer.yml")
