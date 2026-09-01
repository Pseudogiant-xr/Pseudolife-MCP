"""codex_shim wraps the Codex CLI (ChatGPT-plan auth) as an OpenAI-compatible
endpoint, mirroring claude_shim. Two contracts matter beyond /health parity:
the argv must yield a PURE completion (no agent tools, no built-in coding
instructions), and the reply must come from the --json event stream rather
than bare stdout."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "codex_shim", REPO / "evals" / "codex_shim.py")
shim = importlib.util.module_from_spec(spec)
sys.modules["codex_shim"] = shim
spec.loader.exec_module(shim)


def _cli(monkeypatch, chat_ok: bool):
    cli = shim.CodexCli(Path("codex.exe"), "m", 30.0)
    if chat_ok:
        monkeypatch.setattr(cli, "chat", lambda s, u: "OK")
    else:
        def _fail(s, u):
            raise RuntimeError("codex exec error: Not logged in")
        monkeypatch.setattr(cli, "chat", _fail)
    return cli


# --- CLI contract -------------------------------------------------------

def test_parse_args_defaults_to_terra_on_its_own_port():
    # 8082 is the Claude shim's, 8083/8084 its opus-5/fable-5 ceiling-rung
    # instances, and 8085 the events-teacher shim default
    # (distill_datagen_events.py); the shims must run side by side.
    args = shim._parse_args([])
    assert args.port == 8086
    assert args.model == "gpt-5.6-terra"
    assert args.host == "127.0.0.1"


def test_argv_reads_prompt_from_stdin_as_json_events():
    # `codex exec -` takes the prompt on stdin; --json gives an explicit
    # event contract instead of trusting bare stdout to be banner-free.
    argv = shim.CodexCli(Path("codex"), "gpt-5.6-terra", 30.0)._argv(None)
    assert argv[1] == "exec"
    assert "-" in argv
    assert "--json" in argv
    assert argv[argv.index("--model") + 1] == "gpt-5.6-terra"


def test_argv_disables_agent_tools_for_a_pure_completion():
    # The extractor wants one completion, not an agent turn: no shell, no
    # web search, read-only sandbox, no session files left behind.
    argv = shim.CodexCli(Path("codex"), "m", 30.0)._argv(None)
    flat = " ".join(argv)
    assert "--sandbox read-only" in flat
    assert "web_search=disabled" in flat
    assert "features.shell_tool=false" in flat
    assert "--ephemeral" in argv


def test_resolve_model_per_request_override_mirrors_claude_shim():
    # The Console Dreamer card switches the dreamer live by naming a concrete
    # model in the request; endpoint aliases keep the launch default.
    assert shim.resolve_model("gpt-5.6-sol", "gpt-5.6-terra") == "gpt-5.6-sol"
    assert shim.resolve_model("codex-mini", "gpt-5.6-terra") == "codex-mini"
    for alias in ("extractor", "bench", "", None, "claude-opus-5"):
        assert shim.resolve_model(alias, "gpt-5.6-terra") == "gpt-5.6-terra"


def test_chat_threads_the_per_request_model_into_argv():
    # Without this, the card's override would flip the response's model echo
    # while every real call still ran the launch default.
    cli = shim.CodexCli(Path("codex"), "gpt-5.6-terra", 30.0)
    seen = _stub_run(cli, stdout=b'{"type":"item.completed","item":'
                                 b'{"type":"agent_message","text":"ok"}}')
    cli.chat("sys", "hi", model="gpt-5.6-luna")
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "gpt-5.6-luna"
    cli.chat("sys", "hi")
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "gpt-5.6-terra"


def test_argv_installs_system_prompt_as_model_instructions(tmp_path):
    # model_instructions_file REPLACES Codex's built-in coding-agent
    # instructions — that is the point: it strips the persona AND installs
    # the extractor contract in the slot the model weights as instructions.
    p = tmp_path / "instr.md"
    argv = shim.CodexCli(Path("codex"), "m", 30.0)._argv(p)
    assert f"model_instructions_file={p}" in " ".join(argv)


# --- reply parsing ------------------------------------------------------

def test_parse_reply_takes_the_last_agent_message():
    stream = "\n".join([
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"item.started","item":{"id":"i0","type":"agent_message"}}',
        '{"type":"item.completed","item":{"id":"i0",'
        '"type":"reasoning","text":"thinking out loud"}}',
        '{"type":"item.completed","item":{"id":"i1",'
        '"type":"agent_message","text":"{\\"claims\\": []}"}}',
        '{"type":"turn.completed","usage":{"input_tokens":1}}',
    ])
    assert shim._parse_reply(stream) == '{"claims": []}'


def test_parse_reply_strips_code_fence():
    # A fenced reply would fail the extractor's json.loads downstream.
    stream = ('{"type":"item.completed","item":{"id":"i1",'
              '"type":"agent_message","text":"```json\\n{\\"a\\": 1}\\n```"}}')
    assert shim._parse_reply(stream) == '{"a": 1}'


def test_parse_reply_raises_on_failed_turn():
    # A failed turn must NOT look like an empty extraction — the dream
    # advances its cursor on empty results, skipping memories forever.
    stream = ('{"type":"turn.failed","error":{"message":"usage limit reached"}}')
    with pytest.raises(RuntimeError, match="usage limit reached"):
        shim._parse_reply(stream)


def test_parse_reply_raises_when_no_agent_message_present():
    with pytest.raises(RuntimeError, match="no agent_message"):
        shim._parse_reply('{"type":"turn.completed","usage":{}}')


# --- chat() plumbing ----------------------------------------------------

def _stub_run(cli, rc=0, stdout=b"", stderr=b""):
    """Replace the subprocess seam, recording the argv it was handed."""
    seen = {}

    def _run(argv, payload):
        seen["argv"] = [str(a) for a in argv]
        seen["stdin"] = payload.decode()
        seen["instructions"] = next(
            (Path(a.split("=", 1)[1]).read_text(encoding="utf-8")
             for a in seen["argv"]
             if a.startswith("model_instructions_file=")), None)
        return rc, stdout, stderr
    cli._run = _run
    return seen


def test_chat_keeps_stderr_when_the_stream_carries_no_reason():
    # The shape of every pre-turn failure (logged out, bad --model, rejected
    # -c key): nonzero rc, NO json events, real cause on stderr. Reporting
    # "no agent_message" here would delete the only useful diagnostic.
    cli = shim.CodexCli(Path("codex"), "m", 30.0)
    _stub_run(cli, rc=1, stdout=b"", stderr=b"Not logged in. Run `codex login`.")
    with pytest.raises(RuntimeError, match="Not logged in"):
        cli.chat("sys", "hi")


def test_chat_prefers_a_structured_turn_failure_over_stderr():
    cli = shim.CodexCli(Path("codex"), "m", 30.0)
    _stub_run(cli, rc=1, stderr=b"generic wrapper noise",
              stdout=b'{"type":"turn.failed","error":{"message":"usage limit reached"}}')
    with pytest.raises(RuntimeError, match="usage limit reached"):
        cli.chat("sys", "hi")


def test_chat_writes_the_system_message_to_the_instructions_file():
    # Load-bearing check for the `if system:` branch in chat(): _argv is
    # tested in isolation with a hand-passed path, so without this a chat()
    # that stopped writing instructions entirely would stay green.
    cli = shim.CodexCli(Path("codex"), "m", 30.0)
    seen = _stub_run(cli, stdout=b'{"type":"item.completed","item":'
                                 b'{"type":"agent_message","text":"ok"}}')
    cli.chat("EXTRACTOR CONTRACT", "note one")
    assert seen["instructions"] == "EXTRACTOR CONTRACT"
    assert seen["stdin"] == "note one"


def test_health_probe_exercises_the_instructions_file():
    # A probe with an EMPTY system omits -c model_instructions_file entirely,
    # so /health would stay green while every real extraction ran with
    # Codex's built-in coding-agent persona (or failed on a bad -c key).
    # The probe must load-bear the mechanism the shim exists for.
    cli = shim.CodexCli(Path("codex"), "m", 30.0)
    seen = _stub_run(cli, stdout=b'{"type":"item.completed","item":'
                                 b'{"type":"agent_message","text":"OK"}}')
    ok, _ = cli.health()
    assert ok is True
    assert seen["instructions"], "health probe sent no model_instructions_file"


def test_timeout_kills_the_whole_process_tree(monkeypatch):
    # subprocess timeout kills only the DIRECT child. When the CLI resolves
    # to codex.cmd the direct child is cmd.exe and the real (node) codex
    # survives, holding an in-flight subscription call — which breaks the
    # one-call-at-a-time invariant the lock exists to enforce.
    killed = []

    class _Proc:
        pid = 4321

        def communicate(self, payload=None, timeout=None):
            if timeout is not None:
                raise shim.subprocess.TimeoutExpired("codex", timeout)
            return b"", b""

    monkeypatch.setattr(shim.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(shim, "_kill_tree", lambda p: killed.append(p.pid))
    cli = shim.CodexCli(Path("codex"), "m", 0.01)
    with pytest.raises(shim.subprocess.TimeoutExpired):
        cli.chat("sys", "hi")
    assert killed == [4321]


def test_resolve_cli_returns_a_launchable_path_for_a_bare_name(tmp_path,
                                                               monkeypatch):
    # Windows CreateProcess appends only .exe — it does not consult PATHEXT —
    # so a bare name that `which` resolves to codex.cmd must be resolved
    # BEFORE it reaches argv[0], not merely accepted by the startup guard.
    target = tmp_path / "codex.cmd"
    target.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(shim.shutil, "which",
                        lambda n: str(target) if n == "codex" else None)
    assert shim._resolve_cli(Path("codex")) == target
    assert shim._resolve_cli(Path("nope-not-here")) is None


def test_official_install_glob_picks_the_newest_hash_dir(tmp_path,
                                                         monkeypatch):
    # The official Windows installer keeps codex.exe in a rotating
    # %LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\ dir and NOT on PATH (verified
    # live 2026-08-31), so which() alone strands every such install. The
    # fallback must pick the NEWEST hash dir — the auto-updater leaves old
    # versions behind. (The glob helper is platform-free on purpose: faking
    # os.name = "nt" makes pathlib build WindowsPath on POSIX and explode,
    # which took down the CI xdist worker on the first version of this test.)
    base = tmp_path / "OpenAI" / "Codex" / "bin"
    old, new = base / "aaaa", base / "bbbb"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "codex.exe").write_text("x", encoding="utf-8")
    os.utime(old / "codex.exe", (1000, 1000))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert shim._official_install_glob() == new / "codex.exe"
    monkeypatch.delenv("LOCALAPPDATA")
    assert shim._official_install_glob() is None


@pytest.mark.skipif(os.name != "nt", reason="the os.name gate is Windows-only")
def test_resolve_cli_bare_name_reaches_the_glob(tmp_path, monkeypatch):
    # Only a BARE name falls through to the official-installer glob; an
    # explicit path the caller passed is never second-guessed.
    base = tmp_path / "OpenAI" / "Codex" / "bin" / "cafe"
    base.mkdir(parents=True)
    (base / "codex.exe").write_text("x", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(shim.shutil, "which", lambda n: None)
    assert shim._resolve_cli(Path("codex")) == base / "codex.exe"
    assert shim._resolve_cli(Path("C:/nope/codex.exe")) is None


def test_health_ttl_is_configurable():
    # Each /health refresh is a REAL CLI call — metered spend on a free
    # ChatGPT tier (~288 calls/day at the 300s default). The autostart
    # installer raises it via --health-ttl.
    assert shim._parse_args([]).health_ttl == 300.0
    assert shim._parse_args(["--health-ttl", "1800"]).health_ttl == 1800.0
    cli = shim.CodexCli(Path("codex"), "m", 30.0, health_ttl=1800.0)
    ok_calls = {"n": 0}

    def _chat(s, u, model=None):
        ok_calls["n"] += 1
        return "OK"
    cli.chat = _chat
    assert cli.health()[0] is True           # warm: one real call
    cli._health_at = time.monotonic() - 301  # stale under 300s, fresh under 1800s
    cli.health()
    time.sleep(0.1)
    assert ok_calls["n"] == 1                # no refresh: the longer TTL held


# --- HTTP layer ---------------------------------------------------------

def _serve(cli):
    """Real ThreadingHTTPServer on an ephemeral port; caller must shutdown()."""
    srv = shim.ThreadingHTTPServer(("127.0.0.1", 0), shim.make_handler(cli))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_models_listing_leads_with_the_launch_model():
    # The daemon's served-model alias resolution takes the FIRST /v1/models
    # entry as the shim's launch model — ordering is load-bearing, and the
    # listing must dedup when the launch model is one of the suggested trio.
    cli = shim.CodexCli(Path("codex"), "gpt-5.6-luna", 30.0)
    srv, base = _serve(cli)
    try:
        with urllib.request.urlopen(f"{base}/v1/models") as r:
            ids = [m["id"] for m in json.load(r)["data"]]
    finally:
        srv.shutdown()
    assert ids[0] == "gpt-5.6-luna"
    assert len(ids) == len(set(ids))
    assert {"extractor", "bench"} <= set(ids)


def test_chat_completions_round_trips_the_resolved_model(monkeypatch):
    # A concrete gpt-* in the request must reach chat() AND be echoed in the
    # response's model field (the Console card reads the echo).
    cli = shim.CodexCli(Path("codex"), "gpt-5.6-terra", 30.0)
    seen = {}

    def _chat(system, user, model=None, effort=None):
        seen["model"] = model
        return "pong"
    monkeypatch.setattr(cli, "chat", _chat)
    srv, base = _serve(cli)
    try:
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps({"model": "gpt-5.6-sol", "messages": [
                {"role": "user", "content": "hi"}]}).encode(),
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req) as r:
            out = json.load(r)
    finally:
        srv.shutdown()
    assert seen["model"] == "gpt-5.6-sol"
    assert out["model"] == "gpt-5.6-sol"
    assert out["choices"][0]["message"]["content"] == "pong"


# --- /health parity with sonnet_shim ------------------------------------

def test_health_ok_when_cli_answers(monkeypatch):
    ok, detail = _cli(monkeypatch, True).health()
    assert ok is True


def test_health_fails_when_cli_errors(monkeypatch):
    ok, detail = _cli(monkeypatch, False).health()
    assert ok is False and "Not logged in" in detail


def test_health_result_is_cached(monkeypatch):
    cli = _cli(monkeypatch, True)
    assert cli.health()[0] is True
    calls = {"n": 0}

    def _boom(s, u):
        calls["n"] += 1
        raise RuntimeError("nope")
    monkeypatch.setattr(cli, "chat", _boom)
    assert cli.health()[0] is True          # served from cache
    assert calls["n"] == 0


def test_health_stale_cache_served_while_revalidating(monkeypatch):
    # Inherited from sonnet_shim (2026-07-19): the health check runs a REAL
    # completion (seconds) while the daemon probes /health with a 3s timeout.
    # A BLOCKING refresh on cache expiry makes every post-idle probe time
    # out, so dreams silently fall back on a healthy shim.
    cli = _cli(monkeypatch, True)
    assert cli.health()[0] is True           # warm
    calls = {"n": 0}

    def _boom(s, u):
        calls["n"] += 1
        raise RuntimeError("nope")
    monkeypatch.setattr(cli, "chat", _boom)
    cli._health_at = time.monotonic() - 301  # expire the cache

    t0 = time.monotonic()
    ok, _ = cli.health()
    assert ok is True                        # stale verdict, served instantly
    assert time.monotonic() - t0 < 0.5

    deadline = time.monotonic() + 5.0        # background refresh lands
    while calls["n"] == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    while cli._health_ok is not False and time.monotonic() < deadline:
        time.sleep(0.02)
    assert cli.health()[0] is False
    assert calls["n"] == 1                   # exactly one refresh, no stampede


# --- per-request reasoning effort (2026-09-01 dreamer effort knob) -------

_OK_STREAM = (b'{"type":"item.completed","item":'
              b'{"type":"agent_message","text":"ok"}}')


def test_chat_threads_reasoning_effort_into_argv():
    # Request effort wins over the launch default (mirrors resolve_model's
    # precedence for the model).
    cli = shim.CodexCli(Path("codex"), "gpt-5.6-terra", 30.0,
                        reasoning_effort="medium")
    seen = _stub_run(cli, stdout=_OK_STREAM)
    cli.chat("sys", "hi", effort="high")
    assert "model_reasoning_effort=high" in seen["argv"]
    cli.chat("sys", "hi")
    assert "model_reasoning_effort=medium" in seen["argv"]


def test_no_effort_omits_the_config_key():
    # Unset everywhere == pre-knob behavior: the CLI inherits the host's
    # ~/.codex/config.toml, exactly as before.
    cli = shim.CodexCli(Path("codex"), "gpt-5.6-terra", 30.0)
    seen = _stub_run(cli, stdout=_OK_STREAM)
    cli.chat("sys", "hi")
    assert not any("model_reasoning_effort" in a for a in seen["argv"])


def test_chat_completions_thread_reasoning_effort(monkeypatch):
    # The daemon's extra_body lands the field in the request JSON; the
    # handler must hand it to chat() or the knob silently does nothing.
    cli = shim.CodexCli(Path("codex"), "gpt-5.6-terra", 30.0)
    seen = {}

    def _chat(system, user, model=None, effort=None):
        seen["effort"] = effort
        return "pong"
    monkeypatch.setattr(cli, "chat", _chat)
    srv, base = _serve(cli)
    try:
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps({"model": "extractor", "reasoning_effort": "low",
                             "messages": [{"role": "user", "content": "hi"}]
                             }).encode(),
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req):
            pass
    finally:
        srv.shutdown()
    assert seen["effort"] == "low"
