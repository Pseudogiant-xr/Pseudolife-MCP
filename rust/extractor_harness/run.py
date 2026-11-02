"""Differential HTTP/CLI proof; the CLIs are disposable stubs, never accounts.

python rust/extractor_harness/run.py --binary /path/to/pseudolife-stdio

Only generated completion/tool IDs, per-process instruction-file paths and
the fixture CLI path in timeout diagnostics are normalized. HTTP status and response bodies are then compared as bytes;
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
import base64, json, os, subprocess, sys, time
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
if plan.get('descendant'):
    marker = str(Path(os.environ['R6_JOURNAL'] + '.survivor'))
    subprocess.Popen([sys.executable, '-c', 'import time,pathlib;time.sleep(.8);pathlib.Path(' + repr(marker) + ').write_text("survived")'])
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


def call(port, method, path, body=None):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
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
def server(binary, kind, root, plan, *, emulate=False, override=None, ttl=None, timeout=None, managed=False):
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
    port = unused_port()
    argv = ([str(binary), "extractor-shim", kind] if binary else
            [sys.executable, str(ROOT / "evals" / f"{kind}_shim.py")])
    argv += ["--cli", str(cli), "--port", str(port)]
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
    for model in (None, "extractor", "bench", "claude-future" if kind == "claude" else "gpt-future"):
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
                    status, response = call(port, method, path, body)
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
        for model in ("fixture-model", 'fixture\'s "model" \\ path', ""):
            autostart_oracle.write_config(pyroot, kind, {"model": model, "host": "127.0.0.1"})
            result = subprocess.run([str(binary), "extractor-shim", "config", kind, "--repo", str(native),
                                     "--model", model, "--host", "127.0.0.1"], capture_output=True)
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


def lifecycle(binary, root):
    """Both arms must serialize, reap descendants, recover and refresh once."""
    count = 0
    for kind in ("claude", "codex"):
        timeout_answers = []
        for arm, exe in (("python", None), ("rust", binary)):
            with server(exe, kind, root / f"lifecycle-{kind}-{arm}", canned(kind, "OK"), timeout=.25) as (port, plan, journal, _, cli):
                plan.write_text(json.dumps({**canned(kind, "OK"), "sleep": .1}))
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    calls = [pool.submit(call, port, "POST", "/chat/completions", {}) for _ in range(2)]
                    assert all(c.result()[0] == 200 for c in calls)
                timeline = Path(str(journal) + ".timeline").read_text().splitlines()
                assert timeline == ["start", "end"] * 3, (arm, "calls overlapped", timeline)
                plan.write_text(json.dumps({"sleep": 2, "descendant": True}))
                start = time.monotonic()
                status, body = call(port, "POST", "/chat/completions", {})
                assert status == 500 and b"timed out after 0.25 seconds" in body, (status, body)
                assert time.monotonic() - start < .75, (arm, "descendant kept pipes open")
                encoded_cli = json.dumps(repr(str(cli)))[1:-1].encode()
                body = body.replace(encoded_cli, b"'<cli>'")
                timeout_answers.append(body)
                plan.write_text(json.dumps(canned(kind, "recovered")))
                assert call(port, "POST", "/chat/completions", {})[0] == 200
                time.sleep(.9)
                assert not Path(str(journal) + ".survivor").exists(), (arm, "descendant survived timeout")
                count += 3
        assert timeout_answers[0] == timeout_answers[1], (kind, "timeout bytes", timeout_answers)
    for arm, exe in (("python", None), ("rust", binary)):
        with server(exe, "codex", root / f"refresh-{arm}", canned("codex", "OK"), ttl=.15) as (port, plan, journal, _, _):
            time.sleep(.16)
            plan.write_text(json.dumps({"rc": 1, "stderr": "Not logged in", "sleep": .35}))
            start = time.monotonic()
            assert call(port, "GET", "/health")[0] == 200
            assert call(port, "GET", "/health")[0] == 200
            assert time.monotonic() - start < .25, (arm, "stale health blocked")
            time.sleep(.45)
            assert call(port, "GET", "/health")[0] == 503
            assert len(journal_rows(journal)) == 2, (arm, "duplicate refresh")
            plan.write_text(json.dumps(canned("codex", "OK")))
            time.sleep(.17)
            assert call(port, "GET", "/health")[0] == 503
            time.sleep(.1)
            assert call(port, "GET", "/health")[0] == 200
            count += 3
    return count


def main():
    global STUB_EXE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="r6-extractor-") as scratch:
        root = Path(scratch)
        if os.name == "nt":
            # Native executables and npm-style batch launchers are separate
            # fixtures; both must preserve the complete system prompt.
            assert shutil.disk_usage(root).free >= 8 * 1024**3, "Rust fixture build needs 8 GB free"
            STUB_EXE = root / "fixture.exe"
            built = subprocess.run(["rustc", "--edition=2024", str(Path(__file__).with_name("stub.rs")), "-o", str(STUB_EXE)], capture_output=True)
            assert built.returncode == 0, built.stderr.decode(errors="replace")
        count = differential(args.binary.resolve(), root)
        count += tools_differential(args.binary.resolve(), root)
        count += health_differential(args.binary.resolve(), root)
        count += lifecycle(args.binary.resolve(), root)
        count += configuration(args.binary.resolve(), root)
        if os.name == "nt":
            STUB_EXE = None
            count += differential(args.binary.resolve(), root / "batch")
            count += tools_differential(args.binary.resolve(), root / "batch")
    result = {"cases": count, "status": "passed", "real_cli_calls": 0}
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
