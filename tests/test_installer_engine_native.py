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
@pytest.mark.parametrize("flag,installed", [("", "none"), ("", "rust-unaccepted"), ("rust", "none"),
                                            ("python", "rust-unaccepted"), ("", "rust"), ("python", "rust")],
                         ids=["default", "unfinished-rust", "explicit-rust", "unfinished-rust-asks-python",
                              "rust-installed", "rust-installed-asks-python"])
@pytest.mark.parametrize("transport", ["shim", "http"], ids=["no-token", "http-without-token"])
def test_a_bearer_the_install_cannot_mint_is_decided_before_anything_is_pulled(tmp_path, script, flag,
                                                                               installed, transport):
    # Conductor round-4 delta: an install that will have no bearer token
    # (--no-token, or the http transport with none set: the engine flag block
    # leaves the bearer flag empty for both) gets none from stage 5b. Stage 4b
    # knows that before its prepare pulls the image and fills the models
    # volume. A full run reaches the refusal through this checkout's own
    # unfinished Rust install; an explicit --engine rust is refused earlier
    # by the engine flag block, so that row holds stage 4b's own guard. (The
    # shim transport always mints a bearer otherwise: the native client
    # needs no pipx or pip, so "no shim tooling" is no longer a case.)
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
                      "$engineDefaulted=$false\n$bankExisted=$false\n" + stub + stage
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
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE='{flag}'\nRUST_BEARER=''\nTRANSPORT={transport}\nBANK_EXISTED=''\n"
                         "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS\n" + stub + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE"\n')
        calls, output = shell.logged(), proc.stdout + proc.stderr
    prepares = [call for call in calls if "prepare" in call]
    python_way = "or rerun with -Engine python." if script.endswith(".ps1") else "or rerun with --engine python."
    if flag == "python" and installed == "rust-unaccepted":
        # The refusal's last way out works where it is offered: this
        # checkout's own unfinished Rust install reruns as Python. (Another
        # release's unfinished container is refused by prepare whichever
        # engine is asked for, rust/shim/src/cli/installer/engine.rs
        # refuse_foreign_unaccepted, and that refusal names its own fix; the
        # stub here stands for the same-release case.)
        assert proc.returncode == 0, output
        assert "RESULT:python" in output
        assert [call for call in prepares if "--engine rust" in call] == []
    elif flag == "rust" or installed in ("rust-unaccepted", "rust"):
        # A Rust install that is not a default refuses before it pulls anything.
        assert proc.returncode == 2, output
        assert "cannot mint one" in output and "Nothing was pulled or started" in output
        # Where an accepted Rust daemon is installed, the engine stays what
        # detection found (`rust`, rust/shim/src/cli/installer/engine.rs) and
        # the Python engine is only switched to later, so a rerun with it meets
        # this same refusal: the text offers it only where detection found no
        # accepted Rust daemon.
        assert (python_way in output) == (installed != "rust"), output
        # The cause is the flag this install was given, never missing pip tooling
        # (#1609): the native client needs no pipx or pip.
        assert ("-NoToken" if script.endswith(".ps1") else "--no-token") in output, output
        if script.endswith(".ps1"):
            # A recorded opt-out is cleared only by an explicit -NoToken:$false,
            # printed literally (not PowerShell's expanded "False").
            assert "rerun with -NoToken:$false -Transport shim" in output, output
        assert "pipx" not in output and "pip-capable" not in output, output
        assert prepares == []
    else:
        assert proc.returncode == 0, output
        assert "RESULT:python" in output
        assert "no bearer token for the default Rust daemon" in output
        assert [call for call in prepares if "--engine rust" in call] == []


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("token_state,defaulted,expected,installed", [
    ("minted", True, "rust", "none"), ("present", True, "rust", "none"), ("opted-out", True, "python", "none"),
    ("failed", True, "python", "none"), ("http", False, "refused", "none"), ("failed", False, "refused", "none"),
    ("present", False, "rust", "none"), ("http", False, "refused", "rust"), ("failed", False, "refused", "rust"),
])
def test_the_rust_engine_follows_the_token_the_install_produced(tmp_path, script, token_state, defaulted, expected,
                                                                installed):
    # Codex P1 (round 3): stage 4b predicted a bearer that stage 5b never
    # minted (then: the shim transport with no pipx or pip; still: a mint
    # that fails), so the engine follows the token state stage 5b recorded.
    if script.endswith(".ps1"):
        # The refusal endings are their own block (the bearer refusal ways).
        stage = (_stage(script, "# >>> bearer refusal ways >>>", "# <<< bearer refusal ways <<<")
                 + _stage(script, "# >>> engine token >>>", "# <<< engine token <<<"))
        ps = _PowerShell(tmp_path)
        proc = ps.run("function Step($text) { Write-Output \"STEP: $text\" }\n"
                      f"$installedEngine='{installed}'\n"
                      f"$composeEngine='rust'\n$engineDefaulted=${'true' if defaulted else 'false'}\n"
                      f"$engineFiles=@('C:\\fixture\\engine.yml')\n$tokenState='{token_state}'\n" + stage
                      + "Write-Output \"RESULT:$composeEngine|$($engineFiles -join ',')\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        # The refusal endings are their own block (the bearer refusal ways).
        stage = (_stage(script, "# >>> bearer refusal ways >>>", "# <<< bearer refusal ways <<<")
                 + _stage(script, "# >>> engine token >>>", "# <<< engine token <<<"))
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run('step() { echo "STEP: $*"; }\n'
                         f"INSTALLED_ENGINE={installed}\n"
                         f"COMPOSE_ENGINE=rust\nENGINE_DEFAULTED='{'1' if defaulted else ''}'\n"
                         f"engine_files=(/fixture/engine.yml)\nTOKEN_STATE='{token_state}'\n" + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE|${engine_files[*]-}"\n')
        output = proc.stdout + proc.stderr
    if expected == "refused":
        assert proc.returncode == 2, output
        assert "needs its bearer token" in output and "The daemon was not started" in output
        # As at stage 4b: no Python-engine rerun where an accepted Rust daemon is installed.
        python_way = "or rerun with -Engine python." if script.endswith(".ps1") else "or rerun with --engine python."
        assert (python_way in output) == (installed != "rust"), output
        return
    assert proc.returncode == 0, output
    result = next(line for line in output.splitlines() if line.startswith("RESULT:"))
    engine, files = result[len("RESULT:"):].split("|")
    assert engine == expected
    assert bool(files) == (expected == "rust")
    assert ("python after all" in output) == (expected == "python")


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("flag,token", [("python", "none"), ("", "none"), ("rust", "none"), ("python", "set")],
                         ids=["asks-python", "no-flag", "asks-rust", "asks-python-token-set"])
@pytest.mark.parametrize("transport", ["shim", "http"], ids=["no-token", "http-without-token"])
def test_an_accepted_rust_host_asking_for_python_is_told_the_switch_needs_the_token(tmp_path, script, flag, token,
                                                                                     transport):
    # #1968 case 2 in the native installers: --engine python on an accepted
    # Rust host only schedules the switch, which pseudolife-mcp update makes
    # only with the bearer (rust/cli_harness/test_update_transactions.py
    # test_release_switch_off_rust_needs_the_bearer).
    from tests.test_installer_engine import SWITCH_NEEDS_TOKEN, _switch_rerun
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
                "    if ($args[0] -eq 'detect') { return 'rust' }\n"
                "    if ($args[0] -eq 'prepare') { return 'C:\\fixture\\engine.yml' }\n}\n"
                "function Get-EnvValue($name) { return $null }\n")
        proc = shell.run(f"$ClientOnly=$false\n$Engine='{flag}'\n$rustBearer=${str(to_come).lower()}\n"
                         f"$Transport='{'shim' if to_come else transport}'\n"
                         "$env:PSEUDOLIFE_MCP_TOKEN=$null\n$env:PSEUDOLIFE_MCP_TOKENS=$null\n"
                         "$engineDefaulted=$false\n$bankExisted=$true\n" + stub + stage
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
                'case "$1" in detect) echo rust ;; prepare) echo /fixture/engine.yml ;; esac; }\n'
                'step() { echo "STEP: $*"; }\nget_env() { :; }\n')
        proc = shell.run(f"CLIENT_ONLY=''\nENGINE='{flag}'\nRUST_BEARER='{'1' if to_come else ''}'\n"
                         f"TRANSPORT={'shim' if to_come else transport}\nBANK_EXISTED=1\n"
                         "unset PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKENS\n" + stub + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE|$SWITCH_ENGINE"\n')
        output = proc.stdout + proc.stderr
    if to_come:
        assert proc.returncode == 0, output
        assert "RESULT:rust|python" in output, output
        assert "switching to python after the install" in output, output
        return
    assert proc.returncode == 2, output
    assert "needs its bearer token" in output and "Nothing was pulled or started" in output, output
    assert ("or rerun with -Engine python." if ps else "or rerun with --engine python.") not in output, output
    switched = flag == "python"
    assert (SWITCH_NEEDS_TOKEN in output) == switched, output
    assert (_switch_rerun(script) in output) == switched, output
    # The native client is the shim here: no pip tooling is named.
    assert "pipx" not in output and "pip-capable" not in output, output
    assert [call for call in shell.logged() if "prepare" in call] == []


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("flag", ["python", ""])
@pytest.mark.parametrize("token_state", ["http", "failed"])
def test_stage_5b_tells_an_accepted_rust_host_the_switch_needs_the_token(tmp_path, script, flag, token_state):
    from tests.test_installer_engine import SWITCH_NEEDS_TOKEN, _switch_rerun
    ways = ("# >>> bearer refusal ways >>>", "# <<< bearer refusal ways <<<")
    stage = _stage(script, *ways) + _stage(script, "# >>> engine token >>>", "# <<< engine token <<<")
    if script.endswith(".ps1"):
        ps = _PowerShell(tmp_path)
        proc = ps.run("function Step($text) { Write-Output \"STEP: $text\" }\n"
                      f"$installedEngine='rust'\n$switchEngine='{flag}'\n"
                      "$composeEngine='rust'\n$engineDefaulted=$false\n"
                      f"$engineFiles=@('C:\\fixture\\engine.yml')\n$tokenState='{token_state}'\n" + stage
                      + "Write-Output \"RESULT:$composeEngine\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run('step() { echo "STEP: $*"; }\n'
                         f"INSTALLED_ENGINE=rust\nSWITCH_ENGINE='{flag}'\n"
                         "COMPOSE_ENGINE=rust\nENGINE_DEFAULTED=''\n"
                         f"engine_files=(/fixture/engine.yml)\nTOKEN_STATE='{token_state}'\n" + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE"\n')
        output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "needs its bearer token" in output and "The daemon was not started" in output, output
    assert ("or rerun with -Engine python." if script.endswith(".ps1") else "or rerun with --engine python.") \
        not in output, output
    assert (SWITCH_NEEDS_TOKEN in output) == (flag == "python"), output
    assert (_switch_rerun(script) in output) == (flag == "python"), output


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
                      "$engineDefaulted=$false\n$bankExisted=$true\n"
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
        assert python_way in output, output
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
