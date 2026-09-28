"""Executable contracts for ops/install.ps1's extractor modes.

The PowerShell twin of tests/test_installer_extractor_modes.py: family-named
modes (sidecar, claude-*, openai-*, endpoint / endpoint-fallback), the
earlier spellings as deprecated aliases, and an endpoint mode that takes any
OpenAI-compatible server and model name. Blocks run extracted from
ops/install.ps1 under pwsh with stub commands.
"""
from __future__ import annotations

import http.server
import re
import subprocess
import threading

import pytest

from tests.test_installer_client_only_ps import ROOT, _PowerShell, _output, _q

INSTALL = "ops/install.ps1"
ALIASES = {"sonnet-only": "claude-only", "sonnet-fallback": "claude-fallback",
           "codex-only": "openai-only", "codex-fallback": "openai-fallback"}
MODES = ("sidecar", "claude-only", "claude-fallback", "openai-only", "openai-fallback",
         "endpoint", "endpoint-fallback")


def _block(name: str) -> str:
    text = (ROOT / "ops" / "install.ps1").read_text(encoding="utf-8")
    assert f"# >>> {name} >>>\n" in text, f"install.ps1 has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


def _modes(ps: _PowerShell, *, extractor: str = "", model: str = "", url: str = "",
           after: str = "") -> subprocess.CompletedProcess[str]:
    return ps.run(
        f"$Extractor = '{extractor}'\n$Model = '{_q(model)}'\n$ExtractorUrl = '{_q(url)}'\n"
        + _block("extractor modes") + after
        + "\nWrite-Output \"EXTRACTOR=$Extractor\"\nWrite-Output \"MODEL=$Model\"\n"
        + "Write-Output \"URL=$ExtractorUrl\"\n")


def _lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return proc.stdout.splitlines()


@pytest.mark.parametrize("old,new", sorted(ALIASES.items()))
def test_an_old_mode_name_is_a_deprecated_alias_of_its_new_name(tmp_path, old, new):
    proc = _modes(_PowerShell(tmp_path), extractor=old)
    assert proc.returncode == 0, _output(proc)
    assert f"EXTRACTOR={new}" in _lines(proc)
    [note] = [line for line in _output(proc).splitlines() if "deprecated" in line]
    assert old in note and new in note


@pytest.mark.parametrize("mode", [m for m in MODES if not m.startswith("endpoint")])
def test_every_new_mode_name_is_accepted(tmp_path, mode):
    proc = _modes(_PowerShell(tmp_path), extractor=mode)
    assert proc.returncode == 0, _output(proc)
    assert f"EXTRACTOR={mode}" in _lines(proc)
    assert "deprecated" not in _output(proc)


def test_an_unknown_mode_is_refused_with_the_new_names(tmp_path):
    proc = _modes(_PowerShell(tmp_path), extractor="sonnet")
    assert proc.returncode == 2
    for mode in MODES:
        assert mode in _output(proc)
    assert "sonnet-only" not in _output(proc)


NOTE = ("is not in this installer's known list; passing it to the shim unchanged "
        "(add it to the lists when it is a real release)")


@pytest.mark.parametrize("mode,model", [
    ("claude-only", "claude-opus-5"), ("claude-fallback", "claude-opus-5-5"),
    ("claude-only", "claude-fable-5"), ("openai-only", "gpt-5.6-terra"),
    ("openai-fallback", "gpt-6-sol"), ("openai-only", "gpt-6-luna")])
def test_a_listed_model_resolves_silently(tmp_path, mode, model):
    proc = _modes(_PowerShell(tmp_path), extractor=mode, model=model)
    assert proc.returncode == 0, _output(proc)
    assert f"MODEL={model}" in _lines(proc)
    assert "known list" not in _output(proc)


@pytest.mark.parametrize("mode,model", [("claude-only", "claude-opus-6"),
                                        ("openai-fallback", "gpt-7-sol")])
def test_an_unlisted_model_passes_through_to_the_shim_with_a_note(tmp_path, mode, model):
    """A release is usable the day it ships: the lists are the menu, not a gate."""
    proc = _modes(_PowerShell(tmp_path), extractor=mode, model=model)
    assert proc.returncode == 0, _output(proc)
    assert f"MODEL={model}" in _lines(proc)
    [note] = [line for line in _output(proc).splitlines() if "known list" in line]
    assert note == f"model {model} {NOTE}"


@pytest.mark.parametrize("model", ["   ", "\t"])
def test_a_blank_model_is_refused(tmp_path, model):
    proc = _modes(_PowerShell(tmp_path), extractor="claude-only", model=model)
    assert proc.returncode == 2
    assert "-Model" in _output(proc)


def test_an_empty_model_parameter_is_refused(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = subprocess.run(
        [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / INSTALL),
         "-Extractor", "claude-only", "-Model", "", "-NoArt"],
        capture_output=True, text=True, timeout=120, env=ps.env, check=False,
        stdin=subprocess.DEVNULL)
    assert proc.returncode == 2
    assert "-Model" in _output(proc)


def test_the_sidecar_mode_takes_no_unlisted_model(tmp_path):
    proc = _modes(_PowerShell(tmp_path), extractor="sidecar", model="my-local-model")
    assert proc.returncode == 2
    assert "-Model" in _output(proc)


@pytest.mark.parametrize("mode", ["endpoint", "endpoint-fallback"])
def test_an_endpoint_mode_takes_any_model_name(tmp_path, mode):
    proc = _modes(_PowerShell(tmp_path), extractor=mode, model="org/qwen3.6-27b:q8_0",
                  url="http://pl.example.invalid:1234/v1/")
    assert proc.returncode == 0, _output(proc)
    assert "MODEL=org/qwen3.6-27b:q8_0" in _lines(proc)
    assert "URL=http://pl.example.invalid:1234/v1" in _lines(proc)


@pytest.mark.parametrize("model,url", [("", "http://pl.example.invalid:1234/v1"),
                                       ("my-model", "")])
def test_an_endpoint_mode_needs_its_url_and_model(tmp_path, model, url):
    proc = _modes(_PowerShell(tmp_path), extractor="endpoint", model=model, url=url)
    assert proc.returncode == 2
    assert "-ExtractorUrl" in _output(proc) and "-Model" in _output(proc)


def test_an_endpoint_url_must_be_http(tmp_path):
    proc = _modes(_PowerShell(tmp_path), extractor="endpoint", model="m",
                  url="pl.example.invalid:1234/v1")
    assert proc.returncode == 2
    assert "-ExtractorUrl" in _output(proc)


def test_an_extractor_url_needs_an_endpoint_mode(tmp_path):
    proc = _modes(_PowerShell(tmp_path), extractor="sidecar",
                  url="http://pl.example.invalid:1234/v1")
    assert proc.returncode == 2
    assert "-ExtractorUrl" in _output(proc)


@pytest.mark.parametrize("mode,disabled", [
    ("sidecar", False), ("claude-only", True), ("claude-fallback", False),
    ("openai-only", True), ("openai-fallback", False), ("endpoint", True),
    ("endpoint-fallback", False)])
def test_only_the_single_extractor_modes_disable_the_sidecar(tmp_path, mode, disabled):
    endpoint = mode.startswith("endpoint")
    proc = _modes(_PowerShell(tmp_path), extractor=mode, model="m" if endpoint else "",
                  url="http://pl.example.invalid:1234/v1" if endpoint else "",
                  after="\nWrite-Output \"DISABLED=$(Test-ExtractorDisablesSidecar)\"\n")
    assert proc.returncode == 0, _output(proc)
    assert f"DISABLED={disabled}" in _lines(proc)


def _env_lines(ps: _PowerShell, mode: str, *, model: str = "", url: str = "",
               port: int = 8082):
    return ps.run(f"$Extractor = '{mode}'\n$Model = '{_q(model)}'\n$ExtractorUrl = '{_q(url)}'\n"
                  f"$ShimPort = {port}\n" + _block("extractor env")
                  + "\nGet-ExtractorEnvLines | ForEach-Object { Write-Output $_ }\n")


def test_endpoint_writes_the_named_server_and_model(tmp_path):
    proc = _env_lines(_PowerShell(tmp_path), "endpoint", model="org/qwen3.6-27b",
                      url="http://pl.example.invalid:1234/v1")
    assert proc.returncode == 0, _output(proc)
    assert _lines(proc) == [
        "PSEUDOLIFE_DREAM_BASE_URL=http://pl.example.invalid:1234/v1",
        "PSEUDOLIFE_DREAM_MODEL=org/qwen3.6-27b",
        "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=primary"]


def test_endpoint_fallback_keeps_the_sidecar_as_fallback(tmp_path):
    proc = _env_lines(_PowerShell(tmp_path), "endpoint-fallback", model="m",
                      url="https://pl.example.invalid/v1")
    assert proc.returncode == 0, _output(proc)
    assert _lines(proc) == [
        "PSEUDOLIFE_DREAM_BASE_URL=https://pl.example.invalid/v1",
        "PSEUDOLIFE_DREAM_MODEL=m",
        "PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1",
        "PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor",
        "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto"]


@pytest.mark.parametrize("url,written", [
    ("http://127.0.0.1:1234/v1", "http://host.docker.internal:1234/v1"),
    ("http://localhost:11434/v1", "http://host.docker.internal:11434/v1"),
    ("http://[::1]:1234/v1", "http://host.docker.internal:1234/v1")])
def test_a_server_on_this_host_is_written_as_the_daemon_container_sees_it(
        tmp_path, url, written):
    proc = _env_lines(_PowerShell(tmp_path), "endpoint", model="m", url=url)
    assert proc.returncode == 0, _output(proc)
    assert f"PSEUDOLIFE_DREAM_BASE_URL={written}" in _lines(proc)


@pytest.mark.parametrize("mode,port,dream_mode", [
    ("claude-only", 8082, "primary"), ("openai-fallback", 8086, "auto")])
def test_the_shim_modes_still_write_the_shim_triple(tmp_path, mode, port, dream_mode):
    proc = _env_lines(_PowerShell(tmp_path), mode, port=port)
    assert proc.returncode == 0, _output(proc)
    lines = _lines(proc)
    assert f"PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:{port}/v1" in lines
    assert "PSEUDOLIFE_DREAM_MODEL=extractor" in lines
    assert f"PSEUDOLIFE_DREAM_EXTRACTOR_MODE={dream_mode}" in lines


def _preflight(ps: _PowerShell, *, status: int | None = 200,
               url: str = "http://pl.example.invalid:1234/v1"):
    """``status`` None: the stub throws, as an unreachable server does."""
    answer = ("throw 'unreachable'" if status is None
              else f"return [pscustomobject]@{{ StatusCode = {status} }}")
    return ps.run(ps.stub_function("Invoke-WebRequest", answer)
                  + f"$Extractor = 'endpoint'\n$Model = 'm'\n$ExtractorUrl = '{_q(url)}'\n"
                  + _block("endpoint preflight") + "\nInvoke-EndpointPreflight\n")


def test_endpoint_preflight_asks_the_models_route(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps)
    assert proc.returncode == 0, _output(proc)
    [call] = [line for line in ps.logged() if line.startswith("Invoke-WebRequest|")]
    assert "http://pl.example.invalid:1234/v1/models" in call
    assert "-TimeoutSec" in call and "-SkipHttpErrorCheck" in call


@pytest.mark.parametrize("status,says", [(401, "PSEUDOLIFE_DREAM_API_KEY"), (404, "/v1")])
def test_endpoint_preflight_warns_but_continues_when_the_server_answers(
        tmp_path, status, says):
    proc = _preflight(_PowerShell(tmp_path), status=status)
    assert proc.returncode == 0, _output(proc)
    assert "WARNING" in _output(proc) and says in _output(proc)


def test_endpoint_preflight_refuses_an_unreachable_server(tmp_path):
    proc = _preflight(_PowerShell(tmp_path), status=None)
    assert proc.returncode == 1
    assert "http://pl.example.invalid:1234/v1/models" in _output(proc)


def test_the_script_refuses_an_endpoint_mode_without_its_url(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = subprocess.run(
        [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / INSTALL),
         "-Extractor", "endpoint", "-Model", "m", "-NoArt"],
        capture_output=True, text=True, timeout=120, env=ps.env, check=False,
        stdin=subprocess.DEVNULL)
    assert proc.returncode == 2
    assert "-ExtractorUrl" in _output(proc)


def test_client_only_refuses_an_extractor_url(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = subprocess.run(
        [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / INSTALL),
         "-DaemonUrl", "http://100.64.0.2:8765",
         "-ExtractorUrl", "http://pl.example.invalid:1234/v1", "-NoArt"],
        capture_output=True, text=True, timeout=120, env=ps.env, check=False,
        stdin=subprocess.DEVNULL)
    assert proc.returncode == 2
    # The client-only refusal itself, not a binding error.
    [line] = [line for line in _output(proc).splitlines() if "client-only install" in line]
    assert "-ExtractorUrl" in line


# -- one family rule for both shells --------------------------------------------

@pytest.mark.parametrize("mode,model,passes", [
    ("claude-only", "claude-opus-6", True), ("claude-fallback", "opus-6", False),
    ("claude-only", "gpt-7-sol", False), ("claude-only", "codex-7", False),
    ("claude-only", "gpt-5.6-terra", False),
    ("openai-only", "gpt-7-sol", True), ("openai-fallback", "codex-7", True),
    ("openai-only", "opus-6", False), ("openai-only", "claude-opus-6", False),
    ("openai-only", "claude-opus-5-5", False)])
def test_a_model_id_must_belong_to_its_modes_family(tmp_path, mode, model, passes):
    proc = _modes(_PowerShell(tmp_path), extractor=mode, model=model)
    if passes:
        assert proc.returncode == 0, _output(proc)
        assert f"model {model} {NOTE}" in _output(proc)
    else:
        assert proc.returncode == 2
        assert f"-Model '{model}' does not match extractor mode {mode}" in _output(proc)
        assert "known list" not in _output(proc)


# -- free text is checked before it reaches ops/.env or a command line ----------

@pytest.mark.parametrize("mode,model", [
    ("claude-only", "claude opus"), ("claude-only", "claude-$HOME"),
    ("openai-only", 'gpt-"x"'), ("endpoint", "qwen #comment"), ("endpoint", "a;b")])
def test_a_model_id_outside_the_safe_characters_is_refused(tmp_path, mode, model):
    proc = _modes(_PowerShell(tmp_path), extractor=mode, model=model,
                  url="http://pl.example.invalid:1234/v1" if mode == "endpoint" else "")
    assert proc.returncode == 2
    assert "letters, digits" in _output(proc)


def test_an_endpoint_url_with_credentials_is_refused_and_not_echoed(tmp_path):
    proc = _modes(_PowerShell(tmp_path), extractor="endpoint", model="m",
                  url="http://user:hunter2@pl.example.invalid:1234/v1")
    assert proc.returncode == 2
    assert "credentials" in _output(proc)
    assert "hunter2" not in _output(proc)


@pytest.mark.parametrize("url", ["http://pl.example.invalid:1234/v1 #x",
                                 'http://pl.example.invalid:1234/v1"',
                                 "http://pl.example.invalid:1234/$HOME"])
def test_an_endpoint_url_outside_the_safe_characters_is_refused(tmp_path, url):
    proc = _modes(_PowerShell(tmp_path), extractor="endpoint", model="m", url=url)
    assert proc.returncode == 2
    assert "-ExtractorUrl" in _output(proc)


# -- a server on this machine must be reachable from the daemon's container -----

def _probe(ps: _PowerShell, *, url: str, docker_exit: int = 0, linux: bool = True,
           mode: str = "endpoint"):
    return ps.run(
        ps.stub_function("docker", f"$global:LASTEXITCODE = {docker_exit}")
        + f"$Extractor = '{mode}'\n$Model = 'm'\n$ExtractorUrl = '{_q(url)}'\n"
        + _block("extractor env") + _block("endpoint container probe")
        + f"function Test-LinuxHost {{ return ${'true' if linux else 'false'} }}\n"
        + "Invoke-EndpointContainerProbe\nWrite-Output \"STATE=$script:endpointContainer\"\n"
        + "Show-EndpointContainer\n")


def test_a_loopback_server_the_container_cannot_reach_is_flagged(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _probe(ps, url="http://127.0.0.1:11434/v1", docker_exit=1)
    assert proc.returncode == 0, _output(proc)
    # The probe's Python may span lines, so the logged call may too.
    log = ps.calls.read_text(encoding="utf-8")
    assert log.startswith("docker|exec pseudolife-mcp-daemon python")
    assert log.count("docker|") == 1
    assert "http://host.docker.internal:11434/v1" in log
    assert "STATE=unreachable" in _lines(proc)
    for fix in ("OLLAMA_HOST", "llama-server --host", "Serve on Local Network"):
        assert fix in _output(proc), fix
    assert re.search(r"\[!\] Extractor endpoint", proc.stdout)


def test_a_loopback_server_the_container_reaches_passes(tmp_path):
    proc = _probe(_PowerShell(tmp_path), url="http://localhost:1234/v1", linux=False)
    assert proc.returncode == 0, _output(proc)
    assert "STATE=reachable" in _lines(proc)
    assert "WARNING" not in _output(proc)
    assert "bridge" not in _output(proc)
    assert re.search(r"\[x\] Extractor endpoint", proc.stdout)


def test_linux_always_says_what_the_rewrite_means(tmp_path):
    proc = _probe(_PowerShell(tmp_path), url="http://127.0.0.1:1234/v1")
    assert proc.returncode == 0, _output(proc)
    [note] = [line for line in proc.stdout.splitlines() if "bridge" in line]
    assert "127.0.0.1" in note


@pytest.mark.parametrize("mode,url", [("endpoint", "http://pl.example.invalid:1234/v1"),
                                      ("claude-only", "")])
def test_nothing_is_probed_without_a_rewritten_loopback_url(tmp_path, mode, url):
    ps = _PowerShell(tmp_path)
    proc = _probe(ps, url=url, mode=mode)
    assert proc.returncode == 0, _output(proc)
    assert not [line for line in ps.logged() if line.startswith("docker|")]
    assert "Extractor endpoint" not in proc.stdout


# -- a redirect is a warning, as the sh preflight gives -------------------------

class _Redirect(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(302)
        self.send_header("Location", "http://pl.example.invalid/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_endpoint_preflight_treats_a_redirect_as_a_warning(tmp_path):
    """Checked against this pwsh's real Invoke-WebRequest: whether it
    returns the 3xx or throws on -MaximumRedirection 0, the answer is a
    reachable server and a warning, not a refusal."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        proc = _PowerShell(tmp_path).run(
            f"$Extractor = 'endpoint'\n$Model = 'm'\n$ExtractorUrl = '{url}'\n"
            + _block("endpoint preflight") + "\nInvoke-EndpointPreflight\n")
    finally:
        server.shutdown()
        server.server_close()
    assert proc.returncode == 0, _output(proc)
    assert "WARNING" in _output(proc) and "302" in _output(proc)


# -- the compose override the installer owns ------------------------------------

def _markers() -> str:
    text = (ROOT / INSTALL).read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines()
                     if re.match(r"^\$(Legacy(Shim)?)?OverrideMarker = ", line)) + "\n"


def _marker_texts() -> list[str]:
    return [line.split(" = ", 1)[1].strip('"') for line in _markers().splitlines()]


def _override(ps: _PowerShell, mode: str, first_line: str):
    override = ps.tmp / "docker-compose.override.yml"
    override.write_text(first_line + "\nservices:\n  mine: {}\n", encoding="utf-8")
    proc = ps.run(
        ps.stub_function("docker")
        + f"$Extractor = '{mode}'\n$Model = ''\n$ExtractorUrl = ''\n"
        "$ClientOnly = [switch]$false\n"
        f"$overrideFile = '{_q(override)}'\n" + _markers()
        + _block("extractor modes") + _block("sidecar override"))
    return proc, override


@pytest.mark.parametrize("marker", range(3))
def test_every_installer_marker_is_owned(tmp_path, marker):
    texts = _marker_texts()
    assert len(texts) == 3
    proc, override = _override(_PowerShell(tmp_path), "sidecar", texts[marker])
    assert proc.returncode == 0, _output(proc)
    assert not override.exists()
    again = tmp_path / "again"
    again.mkdir()
    proc, override = _override(_PowerShell(again), "claude-only", texts[marker])
    assert proc.returncode == 0, _output(proc)
    lines = override.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == texts[0] and 'profiles: ["disabled"]' in lines[-1]


@pytest.mark.parametrize("mode", ["sidecar", "claude-only"])
def test_an_operators_own_override_is_never_touched(tmp_path, mode):
    proc, override = _override(_PowerShell(tmp_path), mode, "# my own compose override")
    assert proc.returncode == 0, _output(proc)
    assert override.read_text(encoding="utf-8").startswith("# my own compose override\n")
    if mode == "claude-only":
        assert "not installer-managed" in proc.stdout


def test_the_new_parameter_is_declared_and_documented():
    text = (ROOT / INSTALL).read_text(encoding="utf-8")
    params = text.split("param(", 1)[1].split("\n)\n", 1)[0]
    assert "[string]$ExtractorUrl" in params
    header = text.split("param(", 1)[0]
    assert "-ExtractorUrl" in header
    for old in ALIASES:
        assert old in header  # named as a deprecated spelling
