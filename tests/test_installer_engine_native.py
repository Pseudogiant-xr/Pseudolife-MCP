"""The native installer (ops/install-native.*): the tests of test_installer_engine.py
that the native client path changed, run against the native installer.

test_installer_engine.py keeps the default (Python client) installer's tests.
"""
import importlib.util
import os
import sys
import json
import subprocess

import pytest

from pseudolife_memory import update_cli as up
from pseudolife_memory import update_engine as eng
from tests.test_installer_client_only import ROOT
from tests.test_installer_client_only_native import _Shell
from tests.test_installer_client_only_ps import _output
from tests.test_installer_client_only_ps_native import _PowerShell
from tests.test_installer_existing_upgrade import _bash_variants


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("flag,installed,bank", [("", "none", False), ("", "rust-unaccepted", False),
                                                 ("rust", "none", False), ("", "rust", False),
                                                 ("", "python", True), ("", "none", True)],
                         ids=["default", "unfinished-rust", "explicit-rust", "rust-installed",
                              "python-installed", "existing-bank"])
@pytest.mark.parametrize("transport", ["shim", "http"], ids=["no-token", "http-without-token"])
def test_a_bearer_the_install_cannot_mint_is_decided_before_anything_is_pulled(tmp_path, script, flag,
                                                                               installed, bank, transport):
    # Conductor round-4 delta: an install that will have no bearer token
    # (--no-token, or the http transport with none set: the engine flag block
    # leaves the bearer flag empty for both) gets none from stage 5b. Stage 4b
    # knows that before its prepare pulls the image and fills the models
    # volume. The Python daemon is retired (maintainer, 27 November 2026), so
    # no path falls back to it: a new install, an existing bank and a host
    # still on Python (which moves to Rust after the install) all refuse here,
    # and none names a Python way out.
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 4c. create volumes")
        definition = stage[stage.index("function Invoke-EngineHelper"):stage.index("$installedEngine =")]
        stage = stage.replace(definition, "")
        ps = _PowerShell(tmp_path)
        stub = ("function Invoke-EngineHelper {\n"
                f"    Add-Content -LiteralPath '{ps.calls}' -Value ('helper|' + (@($args) -join ' '))\n"
                "    $global:LASTEXITCODE = 0\n"
                f"    if ($args[0] -eq 'detect') {{ return '{installed}' }}\n}}\n"
                "function Get-EnvValue($name) { return $null }\n")
        proc = ps.run(f"$ClientOnly=$false\n$Engine='{flag}'\n$rustBearer=$false\n"
                      f"$Transport='{transport}'\n"
                      "$env:PSEUDOLIFE_MCP_TOKEN=$null\n$env:PSEUDOLIFE_MCP_TOKENS=$null\n"
                      f"$bankExisted=${str(bank).lower()}\n" + stub + stage
                      + "Write-Output \"RESULT:$composeEngine\"\n")
        calls, output = ps.logged(), _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 4c. create volumes")
        definition = next(line for line in stage.splitlines() if line.startswith("engine_helper()"))
        stage = stage.replace(definition, "")
        shell = _Shell(variants[0], tmp_path)
        stub = ('engine_helper() { printf "helper|%s\\n" "$*" >>"$CALL_LOG"; '
                f'case "$1" in detect) echo {installed} ;; esac; }}\n'
                'step() { echo "STEP: $*"; }\nget_env() { :; }\n')
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE='{flag}'\nRUST_BEARER=''\nTRANSPORT={transport}\n"
                         f"BANK_EXISTED='{'1' if bank else ''}'\n"
                         "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS\n" + stub + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE"\n')
        calls, output = shell.logged(), proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "cannot mint one" in output and "Nothing was pulled or started" in output
    assert "--engine python" not in output and "-Engine python" not in output, output
    assert "no bearer token for the default Rust daemon" not in output, output
    # A host that moves after the install is told the move needs the token too.
    assert ("moves to Rust after the install, and that move needs the token too" in output) == (installed == "python" or bank), output
    # The cause is the flag this install was given, never missing pip tooling
    # (#1609): the native client needs no pipx or pip.
    assert ("-NoToken" if script.endswith(".ps1") else "--no-token") in output, output
    if script.endswith(".ps1"):
        # A recorded opt-out is cleared only by an explicit -NoToken:$false,
        # printed literally (not PowerShell's expanded "False").
        assert "rerun with -NoToken:$false -Transport shim" in output, output
    assert "pipx" not in output and "pip-capable" not in output, output
    assert [call for call in calls if "prepare" in call] == []


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("token_state,compose,switch,expected", [
    ("minted", "rust", "", "rust"), ("present", "rust", "", "rust"), ("opted-out", "rust", "", "refused"),
    ("failed", "rust", "", "refused"), ("http", "rust", "", "refused"),
    ("minted", "python", "rust", "python"), ("failed", "python", "rust", "refused"),
])
def test_the_rust_engine_follows_the_token_the_install_produced(tmp_path, script, token_state, compose, switch,
                                                                expected):
    # Codex P1 (round 3): stage 4b predicted a bearer that stage 5b never
    # minted (a mint that fails), so the engine follows the token state stage
    # 5b recorded. No default falls back to Python any more: without a bearer
    # a Rust install, or the move of a Python host after the install, refuses.
    stage = (_stage(script, "# >>> bearer refusal ways >>>", "# <<< bearer refusal ways <<<")
             + _stage(script, "# >>> engine token >>>", "# <<< engine token <<<"))
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        proc = ps.run("function Step($text) { Write-Output \"STEP: $text\" }\n"
                      f"$composeEngine='{compose}'\n$switchEngine='{switch}'\n"
                      f"$engineFiles=@('C:\\fixture\\engine.yml')\n$tokenState='{token_state}'\n" + stage
                      + "Write-Output \"RESULT:$composeEngine|$($engineFiles -join ',')\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run('step() { echo "STEP: $*"; }\n'
                         f"COMPOSE_ENGINE={compose}\nSWITCH_ENGINE='{switch}'\n"
                         f"engine_files=(/fixture/engine.yml)\nTOKEN_STATE='{token_state}'\n" + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE|${engine_files[*]-}"\n')
        output = proc.stdout + proc.stderr
    assert "python after all" not in output, output
    if expected == "refused":
        assert proc.returncode == 2, output
        assert "needs its bearer token" in output and "The daemon was not started" in output
        assert "--engine python" not in output and "-Engine python" not in output, output
        assert ("moves to Rust after the install, and that move needs the token too" in output) == (switch == "rust"), output
        return
    assert proc.returncode == 0, output
    result = next(line for line in output.splitlines() if line.startswith("RESULT:"))
    engine, files = result[len("RESULT:"):].split("|")
    assert engine == expected and files


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("installed,bank", [("python", True), ("none", True), ("rust", True)],
                         ids=["python-installed", "existing-bank", "rust-installed"])
@pytest.mark.parametrize("token", ["set", "none"])
def test_a_host_still_on_python_moves_to_rust_after_the_install(tmp_path, script, installed, bank, token):
    # The Python daemon is retired: a host still on it, or a bank that exists
    # without a daemon, is brought up as it is and moved to Rust after the
    # install through pseudolife-mcp update (which checks the bank first); an
    # accepted Rust host stays. Without a bearer to come the move is refused
    # before anything is pulled, and the refusal says the move needs it.
    ps = script.endswith(".ps1")
    to_come = token == "set"
    if ps:
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 4c. create volumes")
        definition = stage[stage.index("function Invoke-EngineHelper"):stage.index("$installedEngine =")]
        stage = stage.replace(definition, "")
        shell = _PowerShell(tmp_path)
        stub = ("function Invoke-EngineHelper {\n"
                f"    Add-Content -LiteralPath '{shell.calls}' -Value ('helper|' + (@($args) -join ' '))\n"
                "    $global:LASTEXITCODE = 0\n"
                f"    if ($args[0] -eq 'detect') {{ return '{installed}' }}\n"
                "    if ($args[0] -eq 'prepare') { return 'C:\\fixture\\engine.yml' }\n}\n"
                "function Get-EnvValue($name) { return $null }\n")
        proc = shell.run(f"$ClientOnly=$false\n$Engine=''\n$rustBearer=${str(to_come).lower()}\n"
                         "$Transport='shim'\n"
                         "$env:PSEUDOLIFE_MCP_TOKEN=$null\n$env:PSEUDOLIFE_MCP_TOKENS=$null\n"
                         f"$bankExisted=${str(bank).lower()}\n" + stub + stage
                         + "Write-Output \"RESULT:$composeEngine|$switchEngine\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 4c. create volumes")
        definition = next(line for line in stage.splitlines() if line.startswith("engine_helper()"))
        stage = stage.replace(definition, "")
        shell = _Shell(variants[0], tmp_path)
        stub = ('engine_helper() { printf "helper|%s\\n" "$*" >>"$CALL_LOG"; '
                f'case "$1" in detect) echo {installed} ;; prepare) echo /fixture/engine.yml ;; esac; }}\n'
                'step() { echo "STEP: $*"; }\nget_env() { :; }\n')
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE=''\nRUST_BEARER='{'1' if to_come else ''}'\n"
                         f"TRANSPORT={'shim' if to_come else 'http'}\nBANK_EXISTED='{'1' if bank else ''}'\n"
                         "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS\n" + stub + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE|$SWITCH_ENGINE"\n')
        output = proc.stdout + proc.stderr
    moves = installed != "rust"
    if to_come:
        assert proc.returncode == 0, output
        assert (f"RESULT:{'python|rust' if moves else 'rust|'}" in output), output
        assert ("switching to rust after the install" in output) == moves, output
        return
    assert proc.returncode == 2, output
    assert "needs its bearer token" in output and "Nothing was pulled or started" in output, output
    assert "--engine python" not in output and "-Engine python" not in output, output
    assert ("moves to Rust after the install, and that move needs the token too" in output) == moves, output
    assert [call for call in shell.logged() if "prepare" in call] == []


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("switch", ["rust", ""])
@pytest.mark.parametrize("token_state", ["http", "failed"])
def test_stage_5b_tells_a_python_host_the_move_needs_the_token(tmp_path, script, switch, token_state):
    ways = ("# >>> bearer refusal ways >>>", "# <<< bearer refusal ways <<<")
    stage = _stage(script, *ways) + _stage(script, "# >>> engine token >>>", "# <<< engine token <<<")
    compose = "python" if switch else "rust"
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        proc = ps.run("function Step($text) { Write-Output \"STEP: $text\" }\n"
                      f"$switchEngine='{switch}'\n$composeEngine='{compose}'\n"
                      f"$engineFiles=@('C:\\fixture\\engine.yml')\n$tokenState='{token_state}'\n" + stage
                      + "Write-Output \"RESULT:$composeEngine\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run('step() { echo "STEP: $*"; }\n'
                         f"SWITCH_ENGINE='{switch}'\nCOMPOSE_ENGINE={compose}\n"
                         f"engine_files=(/fixture/engine.yml)\nTOKEN_STATE='{token_state}'\n" + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE"\n')
        output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "needs its bearer token" in output and "The daemon was not started" in output, output
    assert "--engine python" not in output and "-Engine python" not in output, output
    assert ("moves to Rust after the install, and that move needs the token too" in output) == (switch == "rust"), output


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("container,flag", [("foreign", ""), ("unlabelled", ""), ("same-release", "")])
@pytest.mark.parametrize("transport", ["shim", "http"], ids=["no-token", "http-without-token"])
def test_a_foreign_unfinished_rust_container_is_refused_before_the_bearer(tmp_path, monkeypatch, script, container,
                                                                           flag, transport):
    # #1968 case 1, in the native installers: the engine step runs as the
    # helper it replaced (native_helper_standin.py; the native step answers
    # alike, tests/fixtures/installer_native_goldens.json), against a docker
    # reporting the container's labels.
    from tests.test_installer_engine import _checkout_release, _fake_docker, _foreign_refusal
    labels = {eng.ENGINE_LABEL: "rust", eng.ACTIVATION_LABEL: "fixture-activation"}
    if container == "foreign":
        labels[eng.RELEASE_LABEL] = "0.0.1"
    elif container == "same-release":
        labels[eng.RELEASE_LABEL] = _checkout_release()
    expected = _foreign_refusal(tmp_path, monkeypatch, labels)
    assert (expected is None) == (container == "same-release")
    calls = tmp_path / "docker-calls.txt"
    docker = _fake_docker(tmp_path, labels, calls)
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 4c. create volumes")
        ps = _PowerShell(tmp_path)
        proc = ps.run(f"$ClientOnly=$false\n$Engine='{flag}'\n$rustBearer=$false\n$Transport='{transport}'\n"
                      "$env:PSEUDOLIFE_MCP_TOKEN=$null\n$env:PSEUDOLIFE_MCP_TOKENS=$null\n"
                      "$bankExisted=$true\n"
                      "function Get-EnvValue($name) { return $null }\n" + stage
                      + "Write-Output \"RESULT:$composeEngine\"\n", extra_env={"PSEUDOLIFE_DOCKER": docker})
        output, python_way = _output(proc), "or rerun with -Engine python."
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 4c. create volumes")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE='{flag}'\nRUST_BEARER=''\nTRANSPORT={transport}\nBANK_EXISTED=1\n"
                         "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS\nget_env() { :; }\n"
                         + stage + 'echo "RESULT:$COMPOSE_ENGINE"\n', extra_env={"PSEUDOLIFE_DOCKER": docker})
        output, python_way = proc.stdout + proc.stderr, "or rerun with --engine python."
    logged = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    assert logged and all(line.startswith("docker|inspect") for line in logged), logged
    if expected is not None:
        assert proc.returncode == 1, output
        assert expected in output, output
        assert "needs its bearer token" not in output, output
    else:
        assert proc.returncode == 2, output
        assert "needs its bearer token" in output and "Nothing was pulled or started" in output, output
        assert python_way not in output, output
        assert "docker rm -f" not in output, output
    assert "RESULT:" not in output


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("answer", [2, 1], ids=["client-without-the-step", "refused"])
def test_a_client_without_the_foreign_step_leaves_it_to_prepare(tmp_path, script, answer):
    # `engine foreign` joined installer protocol 5 before any release answered
    # 5, so a staged client without it (a usage error, exit 2) must leave the
    # install as it was: the bearer refusal, with prepare refusing a foreign
    # container on the rerun. Its refusal (exit 1) stops the install.
    if script.endswith(".ps1"):
        stage = _stage(script, "# -- 4b. daemon engine", "# -- 4c. create volumes")
        definition = stage[stage.index("function Invoke-EngineHelper"):stage.index("$installedEngine =")]
        stage = stage.replace(definition, "")
        ps = _PowerShell(tmp_path)
        stub = ("function Invoke-EngineHelper {\n"
                f"    Add-Content -LiteralPath '{ps.calls}' -Value ('helper|' + (@($args) -join ' '))\n"
                "    $global:LASTEXITCODE = 0\n"
                "    if ($args[0] -eq 'detect') { return 'rust-unaccepted' }\n"
                f"    if ($args[0] -eq 'foreign') {{ [Console]::Error.WriteLine('FOREIGN-ANSWER'); $global:LASTEXITCODE = {answer} }}\n}}\n"
                "function Get-EnvValue($name) { return $null }\n")
        proc = ps.run("$ClientOnly=$false\n$Engine=''\n$rustBearer=$false\n$Transport='shim'\n"
                      "$env:PSEUDOLIFE_MCP_TOKEN=$null\n$env:PSEUDOLIFE_MCP_TOKENS=$null\n"
                      "$engineDefaulted=$false\n$bankExisted=$true\n" + stub + stage
                      + "Write-Output \"RESULT:$composeEngine\"\n")
        calls, output = ps.logged(), _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# ── 4b. daemon engine", "# ── 4c. create volumes")
        definition = next(line for line in stage.splitlines() if line.startswith("engine_helper()"))
        stage = stage.replace(definition, "")
        shell = _Shell(variants[0], tmp_path)
        stub = ('engine_helper() { printf "helper|%s\\n" "$*" >>"$CALL_LOG"; '
                f'case "$1" in detect) echo rust-unaccepted ;; foreign) echo FOREIGN-ANSWER >&2; return {answer} ;; esac; }}\n'
                'step() { echo "STEP: $*"; }\nget_env() { :; }\n')
        proc = shell.run("CLIENT_ONLY=''\nENGINE=''\nRUST_BEARER=''\nTRANSPORT=shim\nBANK_EXISTED=1\n"
                         "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS\n" + stub + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE"\n')
        calls, output = shell.logged(), proc.stdout + proc.stderr
    assert any(call.startswith("helper|foreign --repo") for call in calls), calls
    assert "FOREIGN-ANSWER" in output, output
    if answer == 2:
        assert proc.returncode == 2, output
        assert "needs its bearer token" in output, output
    else:
        assert proc.returncode == 1, output
        assert "needs its bearer token" not in output, output
    assert [call for call in calls if "prepare" in call] == []


def _stage(script, start, end):
    source = (ROOT / "ops" / script).read_text(encoding="utf-8")
    return source[source.index(start):source.index(end)]


@pytest.mark.parametrize("script,start", [("install-native.sh", "show_generic_snippets() {"),
                                          ("install-native.ps1", "function Show-GenericSnippets {")])
def test_the_generic_snippet_needs_the_native_client_not_pip(script, start):
    # #1609: the stdio-shim snippet a native install prints names the client
    # that install put in place, not `pip install pseudolife-mcp` or pipx.
    source = (ROOT / "ops" / script).read_text(encoding="utf-8").replace("\r\n", "\n")
    body = source[source.index(start):]
    body = body[:body.index("\n}\n")]
    assert ("stdio shim (recommended - per-session identity; needs the native\n"
            "  pseudolife-mcp client, on PATH or by its full path in \"command\"):") in body
    assert "pip install" not in body and "pipx" not in body
