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
@pytest.mark.parametrize("flag,installed", [("", "none"), ("", "rust-unaccepted"), ("rust", "none")],
                         ids=["default", "unfinished-rust", "explicit-rust"])
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
    if flag == "rust" or installed == "rust-unaccepted":
        # A Rust install that is not a default refuses before it pulls anything.
        assert proc.returncode == 2, output
        assert "cannot mint one" in output and "Nothing was pulled or started" in output
        # The cause is the flag this install was given, never missing pip tooling
        # (#1609): the native client needs no pipx or pip.
        assert ("-NoToken" if script.endswith(".ps1") else "--no-token") in output, output
        assert "pipx" not in output and "pip-capable" not in output, output
        assert prepares == []
    else:
        assert proc.returncode == 0, output
        assert "RESULT:python" in output
        assert "no bearer token for the default Rust daemon" in output
        assert [call for call in prepares if "--engine rust" in call] == []


@pytest.mark.parametrize("script", ["install-native.sh", "install-native.ps1"])
@pytest.mark.parametrize("token_state,defaulted,expected", [
    ("minted", True, "rust"), ("present", True, "rust"), ("opted-out", True, "python"), ("failed", True, "python"),
    ("http", False, "refused"), ("failed", False, "refused"), ("present", False, "rust"),
])
def test_the_rust_engine_follows_the_token_the_install_produced(tmp_path, script, token_state, defaulted, expected):
    # Codex P1 (round 3): stage 4b predicted a bearer that stage 5b never
    # minted (then: the shim transport with no pipx or pip; still: a mint
    # that fails), so the engine follows the token state stage 5b recorded.
    if script.endswith(".ps1"):
        stage = _stage(script, "# >>> engine token >>>", "# <<< engine token <<<")
        ps = _PowerShell(tmp_path)
        proc = ps.run("function Step($text) { Write-Output \"STEP: $text\" }\n"
                      f"$composeEngine='rust'\n$engineDefaulted=${'true' if defaulted else 'false'}\n"
                      f"$engineFiles=@('C:\\fixture\\engine.yml')\n$tokenState='{token_state}'\n" + stage
                      + "Write-Output \"RESULT:$composeEngine|$($engineFiles -join ',')\"\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        stage = _stage(script, "# >>> engine token >>>", "# <<< engine token <<<")
        shell = _Shell(variants[0], tmp_path)
        proc = shell.run('step() { echo "STEP: $*"; }\n'
                         f"COMPOSE_ENGINE=rust\nENGINE_DEFAULTED='{'1' if defaulted else ''}'\n"
                         f"engine_files=(/fixture/engine.yml)\nTOKEN_STATE='{token_state}'\n" + stage
                         + 'echo "RESULT:$COMPOSE_ENGINE|${engine_files[*]-}"\n')
        output = proc.stdout + proc.stderr
    if expected == "refused":
        assert proc.returncode == 2, output
        assert "needs its bearer token" in output and "The daemon was not started" in output
        return
    assert proc.returncode == 0, output
    result = next(line for line in output.splitlines() if line.startswith("RESULT:"))
    engine, files = result[len("RESULT:"):].split("|")
    assert engine == expected
    assert bool(files) == (expected == "rust")
    assert ("python after all" in output) == (expected == "python")


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
            "  client this installer installs: while PATH does not find\n"
            "  pseudolife-mcp, put its full path in \"command\"):") in body
    assert "pip install" not in body and "pipx" not in body
