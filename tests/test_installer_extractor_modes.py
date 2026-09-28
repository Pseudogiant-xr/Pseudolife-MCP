"""Executable contracts for ops/install.sh's extractor modes.

The modes are named after the extractor family, not the model that was
current when they were written: sidecar, claude-only / claude-fallback,
openai-only / openai-fallback, and endpoint / endpoint-fallback (any
OpenAI-compatible server the operator names). The earlier spellings are
accepted as deprecated aliases. Blocks run extracted from ops/install.sh with
stub CLIs in a disposable home.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from tests.test_installer_client_only import BASH, ROOT, _Shell, _q, _stub

ALIASES = {"sonnet-only": "claude-only", "sonnet-fallback": "claude-fallback",
           "codex-only": "openai-only", "codex-fallback": "openai-fallback"}
MODES = ("sidecar", "claude-only", "claude-fallback", "openai-only", "openai-fallback",
         "endpoint", "endpoint-fallback")


def _block(name: str) -> str:
    text = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    assert f"# >>> {name} >>>\n" in text, f"install.sh has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


def _modes(shell: _Shell, *, extractor: str = "", model: str = "", url: str = "",
           after: str = "") -> subprocess.CompletedProcess[str]:
    return shell.run(
        f"EXTRACTOR='{extractor}'\nMODEL='{_q(model)}'\nEXTRACTOR_URL='{_q(url)}'\n"
        + _block("extractor modes") + after
        + "\nprintf 'EXTRACTOR=%s\\nMODEL=%s\\nURL=%s\\n' \"$EXTRACTOR\" \"$MODEL\" \"$EXTRACTOR_URL\"\n")


@pytest.mark.parametrize("old,new", sorted(ALIASES.items()))
@BASH
def test_an_old_mode_name_is_a_deprecated_alias_of_its_new_name(bash, tmp_path, old, new):
    proc = _modes(_Shell(bash, tmp_path), extractor=old)
    assert proc.returncode == 0, proc.stderr
    assert f"EXTRACTOR={new}\n" in proc.stdout
    [note] = [line for line in proc.stderr.splitlines() if "deprecated" in line]
    assert old in note and new in note


@pytest.mark.parametrize("mode", [m for m in MODES if not m.startswith("endpoint")])
@BASH
def test_every_new_mode_name_is_accepted(bash, tmp_path, mode):
    proc = _modes(_Shell(bash, tmp_path), extractor=mode)
    assert proc.returncode == 0, proc.stderr
    assert f"EXTRACTOR={mode}\n" in proc.stdout
    assert "deprecated" not in proc.stderr


@BASH
def test_an_unknown_mode_is_refused_with_the_new_names(bash, tmp_path):
    proc = _modes(_Shell(bash, tmp_path), extractor="sonnet")
    assert proc.returncode == 2
    for mode in MODES:
        assert mode in proc.stderr
    assert "sonnet-only" not in proc.stderr


NOTE = ("is not in this installer's known list; passing it to the shim unchanged "
        "(add it to the lists when it is a real release)")


@pytest.mark.parametrize("mode,model", [
    ("claude-only", "claude-opus-5"), ("claude-fallback", "claude-opus-5-5"),
    ("claude-only", "claude-fable-5"), ("openai-only", "gpt-5.6-terra"),
    ("openai-fallback", "gpt-6-sol"), ("openai-only", "gpt-6-luna")])
@BASH
def test_a_listed_model_resolves_silently(bash, tmp_path, mode, model):
    proc = _modes(_Shell(bash, tmp_path), extractor=mode, model=model)
    assert proc.returncode == 0, proc.stderr
    assert f"MODEL={model}\n" in proc.stdout
    assert "known list" not in proc.stderr


@pytest.mark.parametrize("mode,model", [("claude-only", "claude-opus-6"),
                                        ("openai-fallback", "gpt-7-sol")])
@BASH
def test_an_unlisted_model_passes_through_to_the_shim_with_a_note(bash, tmp_path, mode, model):
    """A release is usable the day it ships: the lists are the menu, not a gate."""
    proc = _modes(_Shell(bash, tmp_path), extractor=mode, model=model)
    assert proc.returncode == 0, proc.stderr
    assert f"MODEL={model}\n" in proc.stdout
    [note] = [line for line in proc.stderr.splitlines() if "known list" in line]
    assert note == f"model {model} {NOTE}"


@pytest.mark.parametrize("model", ["   ", "\t"])
@BASH
def test_a_blank_model_is_refused(bash, tmp_path, model):
    proc = _modes(_Shell(bash, tmp_path), extractor="claude-only", model=model)
    assert proc.returncode == 2
    assert "--model" in proc.stderr


@BASH
def test_an_empty_model_flag_is_refused(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = shell.run(f"exec '{shell.path(ROOT / 'ops' / 'install.sh')}' "
                     "--extractor claude-only --model '' --no-art\n")
    assert proc.returncode == 2
    assert "--model" in proc.stderr
    assert shell.logged() == []


@BASH
def test_the_sidecar_mode_takes_no_unlisted_model(bash, tmp_path):
    proc = _modes(_Shell(bash, tmp_path), extractor="sidecar", model="my-local-model")
    assert proc.returncode == 2
    assert "--model" in proc.stderr


@pytest.mark.parametrize("mode", ["endpoint", "endpoint-fallback"])
@BASH
def test_an_endpoint_mode_takes_any_model_name(bash, tmp_path, mode):
    proc = _modes(_Shell(bash, tmp_path), extractor=mode, model="org/qwen3.6-27b:q8_0",
                  url="http://pl.example.invalid:1234/v1/")
    assert proc.returncode == 0, proc.stderr
    assert "MODEL=org/qwen3.6-27b:q8_0\n" in proc.stdout
    assert "URL=http://pl.example.invalid:1234/v1\n" in proc.stdout


@pytest.mark.parametrize("model,url", [("", "http://pl.example.invalid:1234/v1"),
                                       ("my-model", "")])
@BASH
def test_an_endpoint_mode_needs_its_url_and_model(bash, tmp_path, model, url):
    proc = _modes(_Shell(bash, tmp_path), extractor="endpoint", model=model, url=url)
    assert proc.returncode == 2
    assert "--extractor-url" in proc.stderr and "--model" in proc.stderr


@BASH
def test_an_endpoint_url_must_be_http(bash, tmp_path):
    proc = _modes(_Shell(bash, tmp_path), extractor="endpoint", model="m",
                  url="pl.example.invalid:1234/v1")
    assert proc.returncode == 2
    assert "--extractor-url" in proc.stderr


@BASH
def test_an_extractor_url_needs_an_endpoint_mode(bash, tmp_path):
    proc = _modes(_Shell(bash, tmp_path), extractor="sidecar",
                  url="http://pl.example.invalid:1234/v1")
    assert proc.returncode == 2
    assert "--extractor-url" in proc.stderr


@pytest.mark.parametrize("mode,disabled", [
    ("sidecar", False), ("claude-only", True), ("claude-fallback", False),
    ("openai-only", True), ("openai-fallback", False), ("endpoint", True),
    ("endpoint-fallback", False)])
@BASH
def test_only_the_single_extractor_modes_disable_the_sidecar(bash, tmp_path, mode, disabled):
    proc = _modes(_Shell(bash, tmp_path), extractor=mode, model="m" if "endpoint" in mode else "",
                  url="http://pl.example.invalid:1234/v1" if "endpoint" in mode else "",
                  after="\nif extractor_disables_sidecar; then echo DISABLED=1; else echo DISABLED=0; fi\n")
    assert proc.returncode == 0, proc.stderr
    assert f"DISABLED={int(disabled)}" in proc.stdout


def _env_lines(shell: _Shell, mode: str, *, model: str = "", url: str = ""):
    return shell.run(
        f"EXTRACTOR='{mode}'\nMODEL='{_q(model)}'\nEXTRACTOR_URL='{_q(url)}'\nSHIM_PORT=8082\n"
        + _block("extractor env") + "\nextractor_env_lines\n")


@BASH
def test_endpoint_writes_the_named_server_and_model(bash, tmp_path):
    proc = _env_lines(_Shell(bash, tmp_path), "endpoint", model="org/qwen3.6-27b",
                      url="http://pl.example.invalid:1234/v1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "PSEUDOLIFE_DREAM_BASE_URL=http://pl.example.invalid:1234/v1",
        "PSEUDOLIFE_DREAM_MODEL=org/qwen3.6-27b",
        "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=primary"]


@BASH
def test_endpoint_fallback_keeps_the_sidecar_as_fallback(bash, tmp_path):
    proc = _env_lines(_Shell(bash, tmp_path), "endpoint-fallback", model="m",
                      url="https://pl.example.invalid/v1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "PSEUDOLIFE_DREAM_BASE_URL=https://pl.example.invalid/v1",
        "PSEUDOLIFE_DREAM_MODEL=m",
        "PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1",
        "PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor",
        "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto"]


@pytest.mark.parametrize("url,written", [
    ("http://127.0.0.1:1234/v1", "http://host.docker.internal:1234/v1"),
    ("http://localhost:11434/v1", "http://host.docker.internal:11434/v1"),
    ("http://[::1]:1234/v1", "http://host.docker.internal:1234/v1")])
@BASH
def test_a_server_on_this_host_is_written_as_the_daemon_container_sees_it(
        bash, tmp_path, url, written):
    """The daemon runs in Docker, where this host is host.docker.internal
    (docker-compose.yml's extra_hosts), as the shim modes already write."""
    proc = _env_lines(_Shell(bash, tmp_path), "endpoint", model="m", url=url)
    assert proc.returncode == 0, proc.stderr
    assert f"PSEUDOLIFE_DREAM_BASE_URL={written}" in proc.stdout.splitlines()


@pytest.mark.parametrize("mode,port,dream_mode", [
    ("claude-only", "8082", "primary"), ("openai-fallback", "8086", "auto")])
@BASH
def test_the_shim_modes_still_write_the_shim_triple(bash, tmp_path, mode, port, dream_mode):
    shell = _Shell(bash, tmp_path)
    proc = shell.run(f"EXTRACTOR='{mode}'\nMODEL=''\nEXTRACTOR_URL=''\nSHIM_PORT={port}\n"
                     + _block("extractor env") + "\nextractor_env_lines\n")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert f"PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:{port}/v1" in lines
    assert "PSEUDOLIFE_DREAM_MODEL=extractor" in lines
    assert f"PSEUDOLIFE_DREAM_EXTRACTOR_MODE={dream_mode}" in lines


def _preflight(shell: _Shell, *, code: str = "200", curl_exit: int = 0,
               url: str = "http://pl.example.invalid:1234/v1"):
    _stub(shell.bin / "curl",
          "printf 'curl|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "printf '%s' \"$FAKE_CODE\"\nexit \"$FAKE_CURL_EXIT\"\n")
    return shell.run(f"EXTRACTOR=endpoint\nMODEL=m\nEXTRACTOR_URL='{_q(url)}'\n"
                     + _block("endpoint preflight") + "\nendpoint_preflight\n",
                     extra_env={"FAKE_CODE": code, "FAKE_CURL_EXIT": str(curl_exit)})


@BASH
def test_endpoint_preflight_asks_the_models_route(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _preflight(shell)
    assert proc.returncode == 0, proc.stderr
    [call] = [line for line in shell.logged() if line.startswith("curl|")]
    assert "http://pl.example.invalid:1234/v1/models" in call
    assert "--max-time" in call


@pytest.mark.parametrize("code,says", [("401", "PSEUDOLIFE_DREAM_API_KEY"),
                                       ("404", "/v1")])
@BASH
def test_endpoint_preflight_warns_but_continues_when_the_server_answers(
        bash, tmp_path, code, says):
    """Any HTTP answer means the server is reachable. A server that lists no
    models, or wants a key the installer never takes, is not refused."""
    proc = _preflight(_Shell(bash, tmp_path), code=code)
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stderr and says in proc.stderr


@BASH
def test_endpoint_preflight_refuses_an_unreachable_server(bash, tmp_path):
    proc = _preflight(_Shell(bash, tmp_path), code="000", curl_exit=7)
    assert proc.returncode == 1
    assert "http://pl.example.invalid:1234/v1/models" in proc.stderr


@BASH
def test_the_script_refuses_an_endpoint_mode_without_its_url(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = shell.run(f"exec '{shell.path(ROOT / 'ops' / 'install.sh')}' "
                     "--extractor endpoint --model m --no-art\n")
    assert proc.returncode == 2
    assert "--extractor-url" in proc.stderr
    assert shell.logged() == []


@BASH
def test_client_only_refuses_an_extractor_url(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = shell.run(f"exec '{shell.path(ROOT / 'ops' / 'install.sh')}' "
                     "--daemon-url http://100.64.0.2:8765 "
                     "--extractor-url http://pl.example.invalid:1234/v1 --no-art\n")
    assert proc.returncode == 2
    assert "--extractor-url" in proc.stderr


def test_the_new_flag_is_parsed_and_documented():
    sh = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    assert '--extractor-url) EXTRACTOR_URL="$2"; shift 2 ;;' in sh
    usage = sh.split("# >>> usage >>>", 1)[1].split("# <<< usage <<<", 1)[0]
    assert "--extractor-url" in usage
    for old in ALIASES:
        assert old in usage  # named as a deprecated spelling
