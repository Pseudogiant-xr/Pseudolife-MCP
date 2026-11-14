"""Differential HTTP/CLI proof; the CLIs are disposable stubs, never accounts.

Invoke through the extractor_shim Cargo integration test, which supplies the
target-native --stub-exe fixture alongside --binary.

Only generated completion/tool IDs and per-process instruction-file paths
are normalized. HTTP status and response bodies are then compared as bytes;
argv, stdin, instruction bytes and selected child environment are compared too.
"""
from __future__ import annotations

import argparse
import contextlib
import concurrent.futures
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("autostart_oracle", ROOT / "ops/shim_autostart.py")
autostart_oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autostart_oracle)
STUB_EXE = None
STUB = r'''
import base64, json, os, socket, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
stdin = sys.stdin.buffer.read()
instructions = None
normalized = []
for index, arg in enumerate(args):
    if index and args[index - 1] == '--system-prompt-file':
        instructions = base64.b64encode(Path(arg).read_bytes()).decode()
        arg = '<instructions>'
    if arg.startswith('model_instructions_file='):
        instructions = base64.b64encode(Path(arg.split('=', 1)[1]).read_bytes()).decode()
        arg = 'model_instructions_file=<instructions>'
    normalized.append(arg)
record = {'argv': normalized, 'stdin': base64.b64encode(stdin).decode(),
          'instructions': instructions,
          'cwd': os.getcwd().replace(os.environ.get('R6_ROOT', '\0'), '<root>'),
          'environment': {k: os.environ.get(k) for k in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_EFFORT')}}
with open(os.environ['R6_JOURNAL'], 'a', encoding='utf-8') as f:
    f.write(json.dumps(record) + '\n')
timeline = Path(os.environ['R6_JOURNAL'] + '.timeline')
with timeline.open('a') as f:
    f.write('start\n')
plan = json.loads(Path(os.environ['R6_PLAN']).read_text())
if plan.get('gate'):
    with socket.create_connection(('127.0.0.1', plan['gate'])) as gate:
        gate.sendall(b'ready')
        gate.settimeout(30)
        gate.recv(1)
if plan.get('descendant'):
    witness_port = plan['descendant']
    subprocess.Popen([sys.executable, '-c', 'import socket;s=socket.create_connection(("127.0.0.1",' + str(witness_port) + '));s.sendall(b"ready");s.settimeout(30);s.recv(1)'])
time.sleep(plan.get('sleep', 0))
sys.stdout.buffer.write(plan.get('stdout', '').encode())
sys.stderr.buffer.write(plan.get('stderr', '').encode())
with timeline.open('a') as f:
    f.write('end\n')
sys.exit(plan.get('rc', 0))
'''


def normalize(body: bytes) -> bytes:
    body = re.sub(rb'"id": "((?:claude|codex)-shim-)\d+"', rb'"id": "\1<time>"', body)
    body = re.sub(rb'"id": "call_[0-9a-f]{8}"', b'"id": "call_<random>"', body)
    return body


def canned(kind, text):
    if kind == "claude":
        return {"stdout": json.dumps({"result": text})}
    return {"stdout": json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}) + "\n"}


class RequestConnection(http.client.HTTPConnection):
    """Send each fixture request together, before reading its one response."""

    def request(self, *args, **kwargs):
        self.pending = bytearray()
        super().request(*args, **kwargs)
        # http.client otherwise writes headers and body separately. Python's
        # early refusals can close between those writes on Windows (VM loop,
        # 2026-11-03). Keep identical framing/body bytes without a late write or
        # a transport retry. Split-body behavior has its own native regression.
        super().send(bytes(self.pending))

    def send(self, data):
        self.pending.extend(data)


def call(port, method, path, body=None):
    con = RequestConnection("127.0.0.1", port, timeout=10)
    try:
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        con.request(method, path, body, {"Content-Type": "application/json"})
        response = con.getresponse()
        return response.status, response.read()
    finally:
        con.close()


def unused_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def server(binary, kind, root, plan, *, emulate=False, override=None, ttl=None, timeout=None, managed=False, discover=False, logging=False):
    root.mkdir(parents=True)
    script = root / "stub.py"
    script.write_text(STUB, encoding="utf-8")
    cli = root / ("stub.exe" if STUB_EXE else "stub.cmd" if os.name == "nt" else "stub")
    if STUB_EXE:
        shutil.copyfile(STUB_EXE, cli)
    elif os.name == "nt":
        cli.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        cli.write_text(f'#!{sys.executable}\n' + STUB, encoding="utf-8")
        cli.chmod(0o700)
    plan_path, journal = root / "plan.json", root / "journal.jsonl"
    plan_path.write_text(json.dumps(plan))
    home = root / "home"
    home.mkdir()
    # No inherited credentials, daemon connection or interactive CLI homes.
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TMP", "TEMP", "TMPDIR") if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / "codex"),
               CLAUDE_CONFIG_DIR=str(home / "claude"), USER="fixture", LOGNAME="fixture",
               USERNAME="fixture", R6_PLAN=str(plan_path), R6_JOURNAL=str(journal),
               R6_STUB_PYTHON=sys.executable, R6_STUB_SCRIPT=str(script),
               R6_ROOT=str(root),
               CLAUDECODE="fixture-session", CLAUDE_CODE_SESSION_ID="fixture-id", CLAUDE_EFFORT="low")
    cli_argument = str(cli)
    if discover:
        assert os.name == "nt"
        (root / kind).write_text("#!/bin/sh\nexit 93\n")
        (root / (kind + ".cmd")).write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        env["PATH"] = str(root) + os.pathsep + env["PATH"]
        env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
        cli_argument = kind
    port = unused_port()
    argv = ([str(binary), "extractor-shim", kind] if binary else
            [sys.executable, str(ROOT / "evals" / f"{kind}_shim.py")])
    argv += ["--cli", cli_argument, "--port", str(port)]
    if logging:
        argv += ["--log", str(root / "native.log")]
    if emulate:
        argv += ["--emulate-tools"]
    if override:
        argv += ["--system-prompt-file", str(override)]
    if ttl is not None:
        argv += ["--health-ttl", str(ttl)]
    if timeout is not None:
        argv += ["--call-timeout", str(timeout)]
    if managed:
        (root / "ops").mkdir()
        values = {"cli": str(cli), "python": sys.executable, "model": "claude-fixture" if kind == "claude" else "gpt-fixture",
                  "host": "127.0.0.1", "port": str(port), "log": str(root / "managed.log")}
        if kind == "claude":
            values["prompt_file"] = str(ROOT / "evals/prompts/sonnet_extractor_v5.md")
        autostart_oracle.write_config(root, kind, values)
        if binary:
            argv = [str(binary), "extractor-shim", kind, "--repo", str(root)]
        else:
            settings = autostart_oracle.resolve(kind, root)
            argv = autostart_oracle.shim_argv(kind, ROOT, settings)
            env = autostart_oracle.child_environment(env)
    log = (root / "server.log").open("wb")
    proc = subprocess.Popen(argv, env=env, cwd=root if managed and not binary else None,
                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"server exited {proc.returncode}: {(root / 'server.log').read_text(errors='replace')}")
            try:
                call(port, "GET", "/models")
                break
            except OSError:
                time.sleep(.05)
        else:
            raise AssertionError("server never bound its disposable port")
        yield port, plan_path, journal, proc, cli
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)
        log.close()


def journal_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def corpus(kind, prompt):
    yield "models", "GET", "/models", None, canned(kind, "OK")
    yield "models-v1", "GET", "/v1/models", None, canned(kind, "OK")
    yield "health", "GET", "/health", None, canned(kind, "OK")
    yield "missing", "GET", "/missing", None, canned(kind, "OK")
    yield "query-is-not-route", "GET", "/models?x=1", None, canned(kind, "OK")
    yield "missing-post", "POST", "/missing", {}, canned(kind, "OK")
    yield "unsupported-method", "PUT", "/models", {}, canned(kind, "OK")
    messages = [{"role": "system", "content": "system é 😀"}, {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "prior"}, {"role": "user", "content": "again"}]
    for model in (None, "extractor", "bench", "Qwen/Qwen3", "claude-future" if kind == "claude" else "gpt-future"):
        yield f"model-{model}", "POST", "/v1/chat/completions", {"messages": messages, "model": model}, canned(kind, "```json\n{\"é\": \"😀\"}\n```")
    for effort in ("  high ", "", 17, None):
        yield f"effort-{effort}", "POST", "/chat/completions", {"messages": messages, "reasoning_effort": effort}, canned(kind, "[]")
    yield "stream-is-ignored", "POST", "/chat/completions", {"messages": messages, "stream": True}, canned(kind, "text")
    yield "empty", "POST", "/chat/completions", {}, canned(kind, "")
    yield "ascii-del", "POST", "/chat/completions", {}, canned(kind, "\x7f")
    yield "long-system", "POST", "/chat/completions", {"messages": [{"role": "system", "content": "é" * 24001}]}, canned(kind, "OK")
    yield "production-prompt", "POST", "/chat/completions", {"messages": [{"role": "system", "content": prompt + "\nVocabulary"}]}, canned(kind, "{}")
    for raw in (b"", b"bad", b"{", b"[]", b'{"messages": null}', b'{"messages":[{"content": ["x"]}]}'):
        yield f"bad-{raw!r}", "POST", "/chat/completions", raw, canned(kind, "OK")
    yield "exit-failure", "POST", "/chat/completions", {}, {"rc": 7, "stdout": "warning\n", "stderr": "failed call"}
    yield "bounded-failure", "POST", "/chat/completions", {}, {"rc": 1, "stdout": "A" * 600, "stderr": "B" * 600}
    if kind == "claude":
        yield "error-result", "POST", "/chat/completions", {}, {"stdout": json.dumps({"is_error": True, "result": "Not logged in"})}
        yield "invalid-cli-json", "POST", "/chat/completions", {}, {"stdout": "bad"}
        yield "wrong-cli-type", "POST", "/chat/completions", {}, {"stdout": "[]"}
        yield "wrong-result-type", "POST", "/chat/completions", {}, {"stdout": '{"result":17}'}
        yield "object-error-result", "POST", "/chat/completions", {}, {"stdout": '{"is_error":true,"result":{"error":"bad"}}'}
    else:
        for value in (17, True, ["text"], {"text": "value"}, None, 0, False, [], {}):
            for rc in (0, 1):
                output = dict(canned(kind, value), rc=rc)
                yield f"reply-type-{value!r}-{rc}", "POST", "/chat/completions", {}, output
        yield "invalid-earlier-reply", "POST", "/chat/completions", {}, {"stdout": canned(kind, 17)["stdout"] + canned(kind, "last")["stdout"]}
        for rc in (0, 1):
            yield f"turn-failed-{rc}", "POST", "/chat/completions", {}, {"rc": rc, "stdout": '{"type":"turn.failed","error":{"message":"Unauthorized"}}\n'}
        yield "no-reply", "POST", "/chat/completions", {}, {"stdout": "banner\n{}\n"}
        yield "last-reply", "POST", "/chat/completions", {}, {"stdout": canned(kind, "first")["stdout"] + canned(kind, "last")["stdout"]}


def differential(binary, root):
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    prompt = (ROOT / "evals/prompts/assistant_facts_provenance.txt").read_text(encoding="utf-8")
    override = root / "override.md"
    override.write_text("header\n---\nReplacement prompt\n", encoding="utf-8")
    for kind in ("claude", "codex"):
        results, records = [], []
        for arm, exe in (("python", None), ("rust", binary)):
            arm_results = []
            with server(exe, kind, root / f"{kind}-{arm}", canned(kind, "OK"), override=override) as (port, plan, journal, _, _):
                for label, method, path, body, output in corpus(kind, prompt):
                    plan.write_text(json.dumps(output))
                    try:
                        status, response = call(port, method, path, body)
                    except OSError as error:
                        raise AssertionError(f"{root.name}/{kind}/{arm}/{label}: {error}") from error
                    arm_results.append((label, status, normalize(response)))
                records.append(journal_rows(journal))
            results.append(arm_results)
        for expected, actual in zip(*results, strict=True):
            assert actual == expected, (kind, expected, actual)
            count += 1
        assert records[0] == records[1], (kind, "CLI journal differs", records)
    return count


def configuration(binary, root):
    count = 0
    for kind in ("claude", "codex"):
        pyroot, native = root / (kind + "-config-python"), root / (kind + "-config-rust")
        seed = ("UNRELATED=fixture\nexport PSEUDOLIFE_" + kind.upper() + "_SHIM_MODEL='outside value'\n\n"
                + autostart_oracle.BLOCK_BEGIN + "\nPSEUDOLIFE_CLAUDE_SHIM_PORT=1234\nPSEUDOLIFE_CODEX_SHIM_PORT=2345\n"
                + autostart_oracle.BLOCK_END + "\n")
        for directory in (pyroot, native):
            (directory / "ops").mkdir(parents=True)
            (directory / "ops/.env").write_text(seed)
        for model in ("fixture-model", "fixture:revision", ""):
            log = 'fixture\'s "log" \\ path'
            autostart_oracle.write_config(pyroot, kind, {"model": model, "host": "127.0.0.1", "log": log})
            result = subprocess.run([str(binary), "extractor-shim", "config", kind, "--repo", str(native),
                                     "--model", model, "--host", "127.0.0.1", "--log", log], capture_output=True)
            assert result.returncode == 0, result.stderr
            assert (pyroot / "ops/.env").read_text() == (native / "ops/.env").read_text(), kind
            count += 1
        answers, records = [], []
        for arm, exe in (("python", None), ("rust", binary)):
            with server(exe, kind, root / f"managed-{kind}-{arm}", canned(kind, "OK"), managed=True) as (port, _, journal, _, _):
                answers.append([call(port, "GET", "/models"), call(port, "POST", "/chat/completions", {"messages": [{"role": "user", "content": "hello"}]})])
                records.append(journal_rows(journal))
        assert [(s, normalize(b)) for s, b in answers[0]] == [(s, normalize(b)) for s, b in answers[1]], kind
        assert records[0] == records[1], (kind, "managed invocation differs", records)
        count += 2
    return count


def tools_differential(binary, root):
    request = {"messages": [{"role": "system", "content": "system"}, {"role": "user", "content": [{"type": "text", "text": "question"}]},
                            {"role": "assistant", "tool_calls": [{"id": "prior", "function": {"name": "lookup", "arguments": "{}"}}]},
                            {"role": "tool", "tool_call_id": "prior", "content": "result"}],
               "tools": [{"type": "function", "function": {"name": "lookup", "description": "Find é", "parameters": {"type": "object", "properties": {}}}}]}
    replies = ['{"tool_call":{"name":"lookup","arguments":{"x":"é"}}}',
               'Here is the call: {"tool_call":{"name":"lookup","arguments":"{bad"}}',
               '[assistant called tool lookup with {"x":1}]',
               '{"tool_call":{"name":"lookup","arguments":{}}', '{"tool_call": broken}', "plain reply"]
    arms, records = [], []
    for arm, exe in (("python", None), ("rust", binary)):
        responses = []
        with server(exe, "claude", root / f"tools-{arm}", canned("claude", "OK"), emulate=True) as (port, plan, journal, _, _):
            for reply in replies:
                plan.write_text(json.dumps(canned("claude", reply)))
                status, body = call(port, "POST", "/v1/chat/completions", request)
                responses.append((status, normalize(body)))
            records.append(journal_rows(journal))
        arms.append(responses)
    assert arms[0] == arms[1], ("tool responses", arms)
    assert records[0] == records[1], ("tool CLI journal", records)
    return len(replies)


def health_differential(binary, root):
    count = 0
    for kind in ("claude", "codex"):
        for reason in ("Not logged in", "rate limit"):
            arms = []
            for arm, exe in (("python", None), ("rust", binary)):
                with server(exe, kind, root / f"health-{kind}-{reason}-{arm}", {"rc": 1, "stderr": reason}) as (port, _, _, _, _):
                    arms.append(call(port, "GET", "/health"))
            assert arms[0] == arms[1], ("health", kind, arms)
            count += 1
    return count


def timeout_body(argv, seconds):
    """The timeout error both shims return for a printable argv: subprocess.TimeoutExpired's text.

    `argv` is the provider command as the stub journalled it, the fixture CLI
    shown as '<cli>'. It is derived rather than pasted, so a launch-model
    change moves both sides. (Python's repr also escapes non-printable
    characters such as U+00A0, which the native repr leaves as they are.)
    """
    return json.dumps({"error": f"Command '{argv!r}' timed out after {float(seconds)} seconds"}).encode()


def lifecycle(binary, root):
    """The native shim must serialize, reap descendants, recover and refresh once.

    Only the native arm runs. These assertions are per arm, so the Python
    shim's arm could fail only on Python behaviour, never on a native defect,
    yet it gated every Rust job (#1314). Its one cross-arm check, equal
    timeout bytes, is now an expected body built from the journalled argv.
    """
    count = 0
    for kind in ("claude", "codex"):
        count += lifecycle_arm(binary, kind, root)
    count += health_refresh(binary, root)
    return count


def lifecycle_arm(exe, kind, root):
    arm = "rust"
    print(f"START lifecycle {kind}/{arm}", flush=True)
    # Normal calls need a process-start budget independent of the tiny
    # timeout formerly used to exercise the timeout error format.
    seconds = 5
    with server(exe, kind, root / f"lifecycle-{kind}-{arm}", canned(kind, "OK"), timeout=seconds) as (port, plan, journal, _, cli):
        plan.write_text(json.dumps({**canned(kind, "OK"), "sleep": .1}))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            calls = [pool.submit(call, port, "POST", "/chat/completions", {}) for _ in range(2)]
            answers = [c.result() for c in calls]
            assert all(answer[0] == 200 for answer in answers), (kind, arm, "serialized calls", answers)
        timeline = Path(str(journal) + ".timeline").read_text().splitlines()
        assert timeline == ["start", "end"] * 3, (kind, arm, "calls overlapped", timeline)
        with provider_gate() as gate, concurrent.futures.ThreadPoolExecutor(1) as pool:
            plan.write_text(json.dumps({"gate": gate.getsockname()[1]}))
            response = pool.submit(call, port, "POST", "/chat/completions", {})
            with accept_provider(gate) as witness:
                status, body = response.result(timeout=10)
                assert status == 500 and b"timed out after 5.0 seconds" in body, (kind, arm, status, body)
                # The timeout must kill this blocked provider; it cannot
                # succeed because a fixed-duration stub sleep elapsed.
                try:
                    assert witness.recv(1) == b"", (kind, arm, "provider survived timeout")
                except ConnectionResetError:
                    pass
        encoded_cli = json.dumps(repr(str(cli)))[1:-1].encode()
        body = body.replace(encoded_cli, b"'<cli>'")
        argv = ["<cli>", *journal_rows(journal)[-1]["argv"]]
        # The stub journals an instruction file as '<instructions>' while the
        # shim quotes its real path; this empty request passes none.
        assert "<instructions>" not in argv and not any(a.startswith("model_instructions_file=") for a in argv), (kind, arm, argv)
        assert body == timeout_body(argv, seconds), (kind, arm, "timeout bytes", body, argv)
        plan.write_text(json.dumps(canned(kind, "recovered")))
        recovered = call(port, "POST", "/chat/completions", {})
        assert recovered[0] == 200, (kind, arm, "recovery", recovered)
    print(f"END lifecycle {kind}/{arm}", flush=True)
    return 3


def health_refresh(exe, root):
    arm = "codex/rust"
    print(f"START health refresh {arm}", flush=True)
    with server(exe, "codex", root / "refresh-rust", canned("codex", "OK"), ttl=0) as (port, plan, journal, _, _):
        with provider_gate() as failed, provider_gate() as recovered, concurrent.futures.ThreadPoolExecutor(1) as pool:
            plan.write_text(json.dumps({"rc": 1, "stderr": "Not logged in", "gate": failed.getsockname()[1]}))
            # TTL zero requests refresh immediately. The provider handshake
            # holds it in flight until every stale response has returned.
            first = pool.submit(call, port, "GET", "/health")
            with accept_provider(failed) as failing:
                answer = first.result(timeout=10)
                assert answer[0] == 200, (arm, "initial cached health", answer)
                for _ in range(2):
                    answer = call(port, "GET", "/health")
                    assert answer[0] == 200, (arm, "stale health", answer)
                assert len(journal_rows(journal)) == 2, (arm, "duplicate failed refresh")
                # Change the next plan only after the in-flight provider
                # acknowledged reading the failure plan.
                plan.write_text(json.dumps({**canned("codex", "OK"), "gate": recovered.getsockname()[1]}))
                failing.sendall(b"release")
            wait_health(port, 503, arm)
            # Seeing the new snapshot need not mean the refresh worker has
            # cleared its in-flight flag yet; keep driving health until the
            # recovery provider acknowledges its invocation.
            with accept_provider(recovered, lambda: wait_health(port, 503, arm)) as recovering:
                answer = call(port, "GET", "/health")
                assert answer[0] == 503, (arm, "cached failure while recovering", answer)
                assert len(journal_rows(journal)) == 3, (arm, "duplicate recovery refresh")
                plan.write_text(json.dumps(canned("codex", "OK")))
                recovering.sendall(b"release")
            wait_health(port, 200, arm)
    print(f"END health refresh {arm}", flush=True)
    return 3


@contextlib.contextmanager
def provider_gate():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)
        yield listener


def accept_provider(listener, trigger=None):
    deadline = time.monotonic() + 10
    if trigger:
        listener.settimeout(.1)
    while True:
        if trigger:
            trigger()
        try:
            connection, _ = listener.accept()
            break
        except TimeoutError:
            if not trigger or time.monotonic() >= deadline:
                raise AssertionError("provider did not reach its handshake") from None
    connection.settimeout(10)
    ready = b""
    while len(ready) < 5:
        chunk = connection.recv(5 - len(ready))
        if not chunk:
            connection.close()
            raise AssertionError("provider exited before handshake")
        ready += chunk
    assert ready == b"ready", "provider handshake failed"
    return connection


def wait_health(port, expected, arm):
    deadline = time.monotonic() + 10
    while True:
        answer = call(port, "GET", "/health")
        if answer[0] == expected:
            return
        assert time.monotonic() < deadline, (arm, "health transition", expected, answer)
        time.sleep(.01)


def native_tree_cleanup(binary, root):
    """Observe child termination, not taskkill/startup wall-clock speed."""
    for kind in ("claude", "codex"):
        # Five seconds allows fixture startup on hosted Windows. The witness
        # must be ready before the timeout; neither it nor its pipes self-close
        # within the HTTP guard. A broken tree kill therefore cannot pass by
        # merely waiting for the descendant to finish naturally.
        with server(binary, kind, root / (kind + "-tree"), canned(kind, "OK"), timeout=5) as (port, plan, _, _, _):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.settimeout(10)
                plan.write_text(json.dumps({"sleep": 20, "descendant": listener.getsockname()[1]}))
                child = None
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    response = pool.submit(call, port, "POST", "/chat/completions", {})
                    try:
                        child, _ = listener.accept()
                        child.settimeout(10)
                        assert child.recv(5) == b"ready", "descendant fixture did not become ready"
                        status, body = response.result(timeout=10)
                        assert status == 500 and b"timed out after 5.0 seconds" in body, (status, body)
                        child.settimeout(1)
                        try:
                            closed = child.recv(1) == b""
                        except ConnectionResetError:
                            closed = True
                        except socket.timeout:
                            closed = False
                        assert closed, "native timeout returned while descendant remained alive"
                    finally:
                        if child is not None:
                            try:
                                child.sendall(b"release")
                            except OSError:
                                pass
                            child.close()
    return 2


def main():
    global STUB_EXE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--stub-exe", required=True, type=Path)
    parser.add_argument("--suite", choices=("all", "contract", "lifecycle"), default="all")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.suite != "lifecycle":
        subprocess.run([sys.executable, str(Path(__file__).with_name("test_prompt.py"))], check=True)
        subprocess.run([sys.executable, str(Path(__file__).with_name("test_transport.py"))], check=True)
    if args.suite != "contract":
        subprocess.run([sys.executable, str(Path(__file__).with_name("test_native_lifecycle.py"))], check=True)
        subprocess.run([sys.executable, str(Path(__file__).with_name("test_lifecycle.py"))], check=True)
    with tempfile.TemporaryDirectory(prefix="r6-extractor-") as scratch:
        root = Path(scratch)
        if os.name == "nt":
            # Native executables and npm-style batch launchers are separate
            # fixtures; both must preserve the complete system prompt.
            STUB_EXE = args.stub_exe.resolve(strict=True)
        count = 0
        if args.suite != "contract":
            count += lifecycle(args.binary.resolve(), root)
            count += native_tree_cleanup(args.binary.resolve(), root)
        if args.suite != "lifecycle":
            count += differential(args.binary.resolve(), root)
            count += tools_differential(args.binary.resolve(), root)
            count += health_differential(args.binary.resolve(), root)
            count += configuration(args.binary.resolve(), root)
            count += settings_privacy(args.binary.resolve(), root)
            count += native_regressions(args.binary.resolve(), root)
            if os.name == "nt":
                STUB_EXE = None
                count += differential(args.binary.resolve(), root / "batch")
                count += tools_differential(args.binary.resolve(), root / "batch")
    result = {"cases": count, "status": "passed", "real_cli_calls": 0}
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


def settings_privacy(binary, root):
    for kind in ("claude", "codex"):
        directory = root / (kind + "-private-settings")
        with server(binary, kind, directory, canned(kind, "OK"), managed=True) as (port, _, _, proc, _):
            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.request("GET", "/health")
            response = connection.getresponse()
            assert response.status == 200
            assert not any("config" in key.lower() or "fingerprint" in key.lower() for key, _ in response.getheaders()), "settings leaked in health headers"
            response.read()
            connection.close()
            env = dict(os.environ, HOME=str(directory / "home"), USERPROFILE=str(directory / "home"))
            env.pop("XDG_CACHE_HOME", None)
            env.pop("LOCALAPPDATA", None)
            args = [str(binary), "extractor-shim", "verify-settings", str(proc.pid), kind, "--repo", str(directory)]
            assert subprocess.run(args, env=env, capture_output=True).returncode == 0
            settings = directory / "ops/.env"
            original = settings.read_text()
            # Unknown keys (including within this provider's prefix), another
            # provider, ordering and quoting are not effective settings.
            other = "CODEX" if kind == "claude" else "CLAUDE"
            changed = original + f"\nUNRELATED_PASSWORD=synthetic-only\nPSEUDOLIFE_{kind.upper()}_SHIM_UNKNOWN=ignored\nPSEUDOLIFE_{other}_SHIM_MODEL=other-model\n"
            changed = changed.replace("=127.0.0.1", " = '127.0.0.1'")
            settings.write_text(changed)
            assert subprocess.run(args, env=env, capture_output=True).returncode == 0, "unrelated settings changed the fingerprint"
            settings.write_text(changed + f"\nPSEUDOLIFE_{kind.upper()}_SHIM_MODEL=changed-model\n")
            assert subprocess.run(args, env=env, capture_output=True).returncode != 0, "effective settings edit was missed"
    return 6


def native_regressions(binary, root):
    count = 0
    for kind in ("claude", "codex"):
        for field in ("model", "reasoning-effort"):
            target = root / f"{kind}-{field}-rejected.env"
            rejected = subprocess.run([str(binary), "extractor-shim", "config", kind,
                "--env-file", str(target), "--" + field, "unsafe&value"], capture_output=True)
            assert rejected.returncode != 0 and not target.exists(), rejected
            count += 1
        with server(binary, kind, root / (kind + "-validation"), canned(kind, "OK"), logging=True) as (port, _, journal, _, _):
            before = len(journal_rows(journal))
            for suffix in ("%", '"', "\n", "&"):
                for field in ("model", "reasoning_effort"):
                    value = ("claude-future" if kind == "claude" else "gpt-future") + suffix
                    status, body = call(port, "POST", "/chat/completions", {field: value})
                    assert status == 500 and b"must contain only ASCII" in body, (field, value, status, body)
                    count += 1
            assert len(journal_rows(journal)) == before, "invalid token reached the provider"
            model = "claude-future" if kind == "claude" else "gpt-future"
            for _ in range(2):
                assert call(port, "POST", "/chat/completions", {"model": model})[0] == 200
            logged = (journal.parent / "native.log").read_text()
            assert logged.count(f"unlisted model {model}; passing it through to the CLI") == 1, logged
            count += 1
        if os.name == "nt":
            with server(binary, kind, root / (kind + "-npm"), canned(kind, "OK"), discover=True) as (port, _, journal, _, _):
                assert call(port, "GET", "/health") == (200, b'{"status": "ok"}')
                assert len(journal_rows(journal)) == 1, "npm-style executable was not called"
                count += 1
    if sys.platform == "linux":
        home = root / "no-service-manager"
        home.mkdir()
        env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), PATH="")
        for kind in ("claude", "codex"):
            result = subprocess.run([str(binary), "extractor-shim", "uninstall-autostart", kind], env=env, capture_output=True)
            assert (result.returncode, result.stdout, result.stderr) == (0, b"", b""), result
            count += 1
        unit = home / ".config/systemd/user/r6-absent-manager.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=/fixture\n")
        result = subprocess.run([str(binary), "extractor-shim", "uninstall-autostart", "claude", "--name", unit.name], env=env, capture_output=True)
        assert result.returncode == 0 and b"systemctl unavailable" in result.stdout and unit.exists(), result
        count += 1
        commands = home / "commands"
        commands.mkdir()
        (commands / "bash").symlink_to(shutil.which("bash"))
        env["PATH"] = str(commands)
        target = home / "settings.env"
        result = subprocess.run([str(binary), "extractor-shim", "install-autostart", "claude",
            "--env-file", str(target), "--cli", str(root / "claude-validation/stub"),
            "--name", "r6-missing-manager.service"], env=env, capture_output=True)
        assert result.returncode != 0 and b"requires systemctl" in result.stderr and not target.exists(), result
        count += 1
        manager = commands / "systemctl"
        manager.write_text("#!/bin/sh\nexit 1\n")
        manager.chmod(0o700)
        result = subprocess.run([str(binary), "extractor-shim", "uninstall-autostart", "claude", "--name", unit.name], env=env, capture_output=True)
        assert result.returncode == 0 and b"manager unavailable" in result.stdout and unit.exists(), result
        count += 1
        for tool in ("ss", "curl", "cmp", "mktemp", "cp", "grep", "cut", "sort", "readlink", "mkdir", "rm", "sleep"):
            (commands / tool).symlink_to(shutil.which(tool))
        manager.write_text('#!/bin/sh\ncase "$*" in *LoadState*) echo masked;; esac\nexit 0\n')
        result = subprocess.run([str(binary), "extractor-shim", "install-autostart", "claude",
            "--env-file", str(target), "--cli", str(root / "claude-validation/stub"),
            "--host", "127.0.0.1", "--port", str(unused_port()), "--startup-timeout", "1",
            "--name", "r6-masked.service"], env=env, capture_output=True)
        assert result.returncode != 0 and b"systemctl --user unmask r6-masked.service" in result.stderr and not target.exists(), result
        count += 1
    return count


if __name__ == "__main__":
    main()
