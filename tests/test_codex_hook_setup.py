"""Scoped consent, preservation, and real runtime coverage for hook setup."""
import argparse
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_hook_setup", ROOT / "ops/setup-codex-hooks.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


@pytest.mark.parametrize("script", ["setup-codex-hooks.py", "setup-codex-coordination.py"])
@pytest.mark.parametrize("old_package", [False, True], ids=["fresh", "upgrade"])
def test_setup_entrypoints_bootstrap_checkout_without_installed_package(tmp_path, script, old_package):
    """Installers run these helpers before installing the matching host shim."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    if old_package:
        site = tmp_path / "old-site"
        package = site / "pseudolife_memory"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        # A previously released package must not shadow the checkout helper.
        env["PYTHONPATH"] = str(site)
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / "ops" / script), "--help"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def options(**kwargs):
    return argparse.Namespace(**dict(dict(source="auto", trust="ask", instructions="auto",
                                         non_interactive=False), **kwargs))


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("answer,approved,fallback,source", [
    ("1\n", True, True, "auto"), ("\n", True, True, "auto"),
    ("2\n", False, True, "skip"), ("3\n", False, False, "skip"),
    ("", False, False, "auto"), ("unexpected\n", False, False, "skip"),
])
def test_single_consent_choice(monkeypatch, answer, approved, fallback, source):
    monkeypatch.setattr(setup.sys, "stdin", Terminal(answer))
    args = options()
    assert setup.consent(args) == (approved, fallback)
    assert args.source == source


@pytest.mark.parametrize("trust,instructions,expected", [
    ("ask", "auto", (False, False)), ("ask", "append", (False, True)),
    ("yes", "auto", (True, True)), ("no", "auto", (False, False)),
    ("no", "append", (False, True)),
])
def test_unattended_consent_is_explicit(monkeypatch, trust, instructions, expected):
    monkeypatch.setattr(setup.sys, "stdin", Terminal("1\n"))
    assert setup.consent(options(trust=trust, instructions=instructions, non_interactive=True)) == expected


@pytest.mark.parametrize("choice", ["append", "skip"])
def test_explicit_instruction_choice_survives_hook_only_prompt(monkeypatch, choice):
    monkeypatch.setattr(setup.sys, "stdin", Terminal("n\n"))
    args = options(instructions=choice)
    assert setup.consent(args) == (False, choice == "append")
    assert args.instructions == choice


def test_explicit_source_skip_does_not_prompt(monkeypatch):
    monkeypatch.setattr(setup.sys, "stdin", Terminal("1\n"))
    args = options(source="skip", instructions="append")
    assert setup.consent(args) == (False, True)
    assert setup.sys.stdin.tell() == 0


def report():
    return {"backups": []}


def test_manual_install_preserves_unrelated_hooks_and_is_idempotent(tmp_path):
    path = tmp_path / "hooks.json"
    other = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo unrelated"}]}
    path.write_text(json.dumps({"description": "User hooks", "hooks": {"SessionStart": [other]}}))
    result = report()
    setup.install_manual(tmp_path, result)
    first = path.read_bytes()
    data = json.loads(first)
    assert data["description"] == "User hooks"
    assert data["hooks"]["SessionStart"][0] == other
    assert set(data["hooks"]) == set(setup.EVENTS.values())
    assert len(result["backups"]) == 1
    assert json.loads(Path(result["backups"][0]).read_bytes())["hooks"]["SessionStart"] == [other]
    setup.install_manual(tmp_path, result)
    assert path.read_bytes() == first and len(result["backups"]) == 1


def test_script_changes_cannot_hide_under_same_manual_bundle(tmp_path):
    setup.install_manual(tmp_path, report())
    installed = next((tmp_path / "pseudolife/hooks").glob("*/lifecycle.ps1"))
    installed.write_text("modified by user")
    with pytest.raises(setup.SetupError, match="modified"):
        setup.install_manual(tmp_path, report())
    assert installed.read_text() == "modified by user"


def test_read_only_rerun_rejects_modified_trusted_manual_scripts(tmp_path, monkeypatch):
    setup.install_manual(tmp_path, report())
    installed = next((tmp_path / "pseudolife/hooks").glob("*/lifecycle.ps1"))
    definitions = setup.manual_definitions(installed.parent)
    hooks = [hook(tmp_path, event, key=name, command=definitions[name]["commandWindows"], trustStatus="trusted")
             for event, name in [*setup.EVENTS.items(),
                                 ("sessionStart", "CoordinationStart"),
                                 ("userPromptSubmit", "CoordinationPrompt")]]
    setup.vet_manual(hooks, tmp_path)
    installed.write_text("modified by user")
    with pytest.raises(setup.SetupError, match="modified"):
        setup.vet_manual(hooks, tmp_path)


def test_manual_set_rejects_duplicate_handler_in_place_of_coordination(tmp_path):
    setup.install_manual(tmp_path, report())
    directory = next((tmp_path / "pseudolife/hooks").glob("*/lifecycle.ps1")).parent
    definitions = setup.manual_definitions(directory)
    roles = [*setup.EVENTS.items(),
             ("sessionStart", "CoordinationStart"),
             ("userPromptSubmit", "CoordinationPrompt")]
    hooks = [hook(tmp_path, event, key=name, command=definitions[name]["commandWindows"])
             for event, name in roles]
    assert setup.complete_set(hooks, "manual")
    hooks[-1]["command"] = definitions["UserPromptSubmit"]["commandWindows"]
    assert not setup.owned_manual(hooks[-1] | {"eventName": "sessionStart"}, tmp_path)
    with pytest.raises(setup.SetupError, match="incomplete"):
        setup.vet_manual(hooks, tmp_path)


def test_approved_upgrade_accepts_exact_legacy_four_script_bundle(tmp_path):
    """A trusted pre-split bundle must pass vetting before it can be replaced."""
    old_names = ("lifecycle.ps1", "session-start.sh", "user-prompt-submit.sh", "session-end.sh")
    bodies = {name: f"# legacy {name}\n".encode() for name in old_names}
    digest = hashlib.sha256()
    for name in old_names:
        digest.update(name.encode() + b"\0" + bodies[name] + b"\0")
    directory = tmp_path / "pseudolife" / "hooks" / digest.hexdigest()[:20]
    directory.mkdir(parents=True)
    for name, body in bodies.items():
        (directory / name).write_bytes(body)
    definitions = setup.manual_definitions(directory)
    hooks = [hook(tmp_path, event, key=role, command=definitions[role]["commandWindows"])
             for event, role in setup.EVENTS.items()]
    setup.vet_manual(hooks, tmp_path, complete=False)
    manifest = tmp_path / "hooks.json"
    manifest.write_text(json.dumps({"hooks": {
        event: [{"hooks": [definitions[role]]}]
        for event, role in setup.EVENTS.items()}}))
    setup.install_manual(tmp_path, report())
    installed = json.loads(manifest.read_text())["hooks"]
    assert len(installed["SessionStart"]) == 2
    assert len(installed["UserPromptSubmit"]) == 2
    assert len(installed["SessionEnd"]) == 1
    assert all((directory / name).read_bytes() == body for name, body in bodies.items())


@pytest.mark.parametrize("damage", ["changed-script", "extra-file"])
def test_approved_upgrade_rejects_nonexact_legacy_bundle(tmp_path, damage):
    old_names = ("lifecycle.ps1", "session-start.sh", "user-prompt-submit.sh", "session-end.sh")
    bodies = {name: f"# legacy {name}\n".encode() for name in old_names}
    digest = hashlib.sha256()
    for name in old_names:
        digest.update(name.encode() + b"\0" + bodies[name] + b"\0")
    directory = tmp_path / "pseudolife" / "hooks" / digest.hexdigest()[:20]
    directory.mkdir(parents=True)
    for name, body in bodies.items():
        (directory / name).write_bytes(body)
    if damage == "changed-script":
        (directory / "session-start.sh").write_text("changed\n")
    else:
        (directory / "unexpected.sh").write_text("extra\n")
    definitions = setup.manual_definitions(directory)
    hooks = [hook(tmp_path, event, key=role, command=definitions[role]["commandWindows"])
             for event, role in setup.EVENTS.items()]
    with pytest.raises(setup.SetupError, match="modified"):
        setup.vet_manual(hooks, tmp_path, complete=False)


def test_manual_bash_hooks_mark_codex_context_and_recognize_legacy_commands(tmp_path):
    directory = tmp_path / "pseudolife" / "hooks" / ("a" * 20)
    directory.mkdir(parents=True)
    current = setup.manual_definitions(directory)
    legacy = setup.manual_definitions(directory, codex_marker=False)

    for event in setup.EVENTS.values():
        assert current[event]["command"].startswith(
            "env PSEUDOLIFE_CODEX_HOOK=1 bash ")
        assert "PSEUDOLIFE_CODEX_HOOK" not in current[event]["commandWindows"]
        old = hook(tmp_path, eventName=next(
            key for key, value in setup.EVENTS.items() if value == event),
            sourcePath=str(tmp_path / "hooks.json"), source="user",
            command=legacy[event]["command"])
        assert setup.owned_manual(old, tmp_path)


def hook(home, event="sessionStart", **kwargs):
    return dict(dict(key="our-key", currentHash="sha256:" + "a" * 64,
                     eventName=event, enabled=True, trustStatus="untrusted", isManaged=False,
                     sourcePath=str(home / "hooks.json"), source="user", command="echo unrelated"), **kwargs)


def test_disabled_plugin_never_replaced_by_manual(tmp_path):
    hooks = [hook(tmp_path, pluginId=setup.PLUGIN_ID, source="plugin", enabled=False)]
    with pytest.raises(setup.SetupError, match="disabled"):
        setup.select_hooks(hooks, tmp_path, "auto")


def test_explicit_manual_does_not_stack_plugin(tmp_path):
    with pytest.raises(setup.SetupError, match="already owns"):
        setup.select_hooks([hook(tmp_path, pluginId=setup.PLUGIN_ID)], tmp_path, "manual")


def test_partial_plugin_rejected():
    with pytest.raises(setup.SetupError, match="missing"):
        setup.vet_plugin([])


@pytest.mark.parametrize("ready,choice,allowed,expected", [
    (True, "auto", True, "covered-by-hooks"), (False, "auto", True, "appended"),
    (False, "auto", False, "skipped"), (True, "append", False, "appended"),
    (False, "skip", True, "skipped"),
])
def test_fallback_depends_on_readiness_and_consent(tmp_path, ready, choice, allowed, expected):
    result = report()
    assert setup.standing_instructions(tmp_path, choice, allowed, ready, result) == expected
    assert (tmp_path / "AGENTS.md").exists() == (expected == "appended")


def test_fallback_preserves_override_and_backs_it_up(tmp_path):
    override = tmp_path / "AGENTS.override.md"
    original = b"User project rules.\n"
    override.write_bytes(original)
    result = report()
    assert setup.standing_instructions(tmp_path, "auto", True, False, result) == "appended"
    assert override.read_bytes().startswith(original)
    assert Path(result["backups"][0]).read_bytes() == original
    assert not (tmp_path / "AGENTS.md").exists()
    first = override.read_bytes()
    assert setup.standing_instructions(tmp_path, "auto", True, False, result) == "present"
    assert first == override.read_bytes() and len(result["backups"]) == 1


class Writer:
    def __init__(self):
        self.calls = []

    def rpc(self, method, params):
        self.calls.append((method, params))
        return {}


def config(home):
    return {"layers": [{"name": {"type": "user", "file": str(home / "config.toml"), "profile": None},
                        "version": "snapshot-token"}]}


def test_trust_uses_runtime_hash_and_versioned_scoped_config_write(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# Keep user configuration\n")
    writer = Writer()
    entry = hook(tmp_path, key='pseudolife-memory@pseudolife-mcp:hooks/hooks.json:session_start:0:0')
    result = report()
    setup.trust_hooks(writer, config(tmp_path), [entry], tmp_path, result)
    method, params = writer.calls[0]
    assert method == "config/batchWrite" and params["expectedVersion"] == "snapshot-token"
    assert params["edits"] == [{"keyPath": setup.dotted("hooks", "state", entry["key"], "trusted_hash"),
                                "value": entry["currentHash"], "mergeStrategy": "replace"}]
    assert Path(result["backups"][0]).read_bytes() == path.read_bytes()


def test_already_trusted_does_not_write(tmp_path):
    writer = Writer()
    setup.trust_hooks(writer, config(tmp_path), [hook(tmp_path, trustStatus="trusted")], tmp_path, report())
    assert not writer.calls


def test_modified_definition_can_be_approved_for_current_hash(tmp_path):
    writer = Writer()
    setup.trust_hooks(writer, config(tmp_path), [hook(tmp_path, trustStatus="modified")], tmp_path, report())
    assert len(writer.calls[0][1]["edits"]) == 1


def test_daemon_readiness_retries_cold_start(monkeypatch):
    states = iter([{"status": "starting"}, {"status": "ok"}])
    monkeypatch.setattr(setup, "daemon_request", lambda _: next(states))
    monkeypatch.setattr(setup.time, "sleep", lambda _: None)
    setup.wait_for_daemon()


def test_daemon_readiness_failure_is_bounded(monkeypatch):
    monkeypatch.setattr(setup, "daemon_request", lambda _: {"status": "starting"})
    with pytest.raises(setup.SetupError, match="not ready"):
        setup.wait_for_daemon(timeout=0)


def test_daemon_request_reads_explicit_credential_file(tmp_path, monkeypatch):
    from pseudolife_memory.credentials import _write_token_file

    path = tmp_path / "token"
    _write_token_file(path, "file-token")
    seen = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def opened(request, timeout):
        seen.append(hmac.compare_digest(
            request.get_header("Authorization") or "", "Bearer file-token"))
        return Response()

    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(path))
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "stale-token")
    monkeypatch.setattr(setup, "urlopen", opened)
    monkeypatch.setattr(setup.json, "load", lambda _: {"status": "ok"})
    assert setup.daemon_request("/health") == {"status": "ok"}
    assert seen == [True]


def test_daemon_request_refuses_redirect_without_forwarding_authorization(
        tmp_path, monkeypatch):
    from pseudolife_memory.credentials import _write_token_file
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    target_headers = []
    class Target(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            target_headers.append(bool(self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/health")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    workers = [Thread(target=server.serve_forever, daemon=True)
               for server in (target, redirect)]
    for worker in workers:
        worker.start()
    token = tmp_path / "token"
    _write_token_file(token, "fixture-token")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(token))
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL",
                       f"http://127.0.0.1:{redirect.server_port}")
    try:
        with pytest.raises(Exception):
            setup.daemon_request("/health")
        assert target_headers == []
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(timeout=2)


# Every status urllib's default handler follows for a GET (308 since Python 3.11).
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_installer_credential_check_refuses_redirect_without_forwarding_authorization(
        status):
    """A followed redirect would carry the installer's bearer to the Location's
    host, and a 200 there would pass the credential check."""
    sent, forwarded = [], []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            forwarded.append(bool(self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"episodes":[]}')

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            sent.append(hmac.compare_digest(
                self.headers.get("Authorization") or "", "Bearer fixture-token"))
            self.send_response(status)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}{self.path}")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    workers = [threading.Thread(target=server.serve_forever, daemon=True)
               for server in (target, redirect)]
    for worker in workers:
        worker.start()
    try:
        valid = setup.installer_credential_valid(
            f"http://127.0.0.1:{redirect.server_port}", "fixture-token")
        assert sent == [True]  # The check really ran, with the bearer.
        assert forwarded == []
        assert valid is False
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(timeout=2)


@pytest.mark.parametrize("extra", [{"currentHash": "unknown"}, {"isManaged": True},
                                  {"trustStatus": "future-policy"}])
def test_unknown_or_managed_trust_never_written(tmp_path, extra):
    writer = Writer()
    with pytest.raises(setup.SetupError):
        setup.trust_hooks(writer, config(tmp_path), [hook(tmp_path, **extra)], tmp_path, report())
    assert not writer.calls


def test_failed_setup_keeps_promised_fallback_and_redacts_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    def broken():
        raise RuntimeError("secret-token")
    monkeypatch.setattr(setup, "resolve_codex", broken)
    result = setup.setup(options(trust="yes"))
    assert result["status"] == "unavailable" and result["instructions"] == "appended"
    assert "secret-token" not in json.dumps(result)


def test_close_does_not_wait_on_a_process_holding_the_app_servers_stdout(tmp_path):
    """A hook Codex killed can linger holding an inherited copy of the
    app-server's stdout: seen 2026-09-25 on Windows under load, a PowerShell
    hook stuck mid-exit for minutes while setup waited on the pipe forever."""
    # Codex(executable, ...) runs `<executable> app-server --stdio` in cwd.
    (tmp_path / "app-server").write_text(
        "import json, subprocess, sys\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    if message.get('method') == 'initialize':\n"
        "        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)\n"
        "holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                          close_fds=False)\n"
        "open('holder.pid', 'w').write(str(holder.pid))\n", encoding="utf-8")
    client = setup.Codex(sys.executable, tmp_path, tmp_path)
    started = time.monotonic()
    try:
        client.close()
        assert time.monotonic() - started < 10
    finally:
        try:
            os.kill(int((tmp_path / "holder.pid").read_text()), signal.SIGTERM)
        except OSError:  # already gone
            pass
        client.reader.join(timeout=5)
        client.proc.stdout.close()


def _powershell_pair_seconds():
    """Median wall time of the two PowerShell cold starts Codex pays for
    every Windows hook: it runs `pwsh -Command "pwsh -File lifecycle.ps1"`."""
    pwsh = shutil.which("pwsh")
    times = []
    for _ in range(3):
        started = time.monotonic()
        subprocess.run([pwsh, "-NoProfile", "-Command", "pwsh -NoProfile -Command exit"],
                       capture_output=True, timeout=120)
        times.append(time.monotonic() - started)
    return sorted(times)[1]


def _ready_unless_overloaded(result):
    """Setup must verify ready. The exception is a hook out of time while
    PowerShell starts more than twice as slowly as on the maintainer host at
    60-80% CPU (a 0.41 s pair): a judgment threshold, below which the 5 s
    hooks (0.9-2.2 s there) and SessionEnd (0.9 s of Codex's 3 s cap) still
    had room, so a failure there is reported rather than put down to load."""
    recovery = result.get("recovery") or ""
    if (result["status"] != "ready" and os.name == "nt"
            and any(phrase in recovery for phrase in (
                "did not return the expected memory context",
                "SessionEnd did not close the verification episode",
                "Codex did not respond within the setup timeout"))):
        pair = _powershell_pair_seconds()
        if pair > 0.8:
            pytest.skip(f"Machine too busy for Codex's hook budgets: two PowerShell cold "
                        f"starts took {pair:.1f} s (0.41 s at 60-80% CPU). {recovery}")
    assert result["status"] == "ready", recovery or result


@pytest.mark.parametrize("existing_config", [True, False])
def test_real_codex_manual_trust_and_lifecycle(tmp_path, monkeypatch, existing_config):
    """Use a disposable Codex home, real CLI, and in-process daemon fixture."""
    codex = shutil.which("codex")
    if not codex or (os.name == "nt" and not shutil.which("pwsh")):
        pytest.skip("Codex CLI and native hook runtime are required")
    sessions = set()
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, content_type="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self.send(b'{"status":"ok"}')
            elif parsed.path == "/api/hook/session-start":
                sid = parse_qs(parsed.query)["session_id"][0]
                sessions.add(sid)
                seen.append("start")
                self.send(b"Session episode: abcdef123456 -- fixture briefing", "text/plain; charset=utf-8")
            elif parsed.path == "/api/episodes":
                self.send(json.dumps({"episodes": [{"session_key": s} for s in sessions]}).encode())
            else:
                self.send_error(404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/api/hook/session-end"
            sessions.discard(body["session_id"])
            seen.append("end")
            self.send(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    home = tmp_path / "home"
    home.mkdir()
    if existing_config:
        (home / "config.toml").write_text('# User comment must survive\n[features]\nmemories = false\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.setenv("CODEX_CLI_PATH", codex)
    # The hooks keep their shipped budgets: this is the only live check that
    # the PowerShell hooks fit them. Measured 2026-09-25 on the 16-thread
    # maintainer host (Codex 0.156.1, pwsh 7.6.6): the double PowerShell
    # start every Windows hook pays took 0.41 s median at 60-80% CPU but
    # 3.8 s (7.1 s max, n=12) beside 16 busy processes, where the hooks ran
    # 5.2-6.7 s against 5 s budgets: the intermittent "did not return the
    # expected memory context" while other suites ran. Widening the budgets
    # here cannot help SessionEnd anyway (Codex holds it to 3 s whatever
    # hooks.json asks), so a machine that busy is skipped instead.
    try:
        result = setup.setup(options(trust="yes", non_interactive=True))
        _ready_unless_overloaded(result)
        assert result["verified"] == {"session_start": True, "user_prompt_submit": True, "session_end": True}
        assert result["instructions"] == "covered-by-hooks"
        assert seen == ["start", "end"] and not sessions
        text = (home / "config.toml").read_text()
        assert text.count("trusted_hash") == 5
        if existing_config:
            assert text.startswith("# User comment must survive")
        assert "dangerously-bypass" not in text
        # A separately trusted user hook must neither run during our probe nor
        # lose its persisted settings. Only temporary runtime overrides exclude it.
        marker = home / "unrelated-ran.txt"
        command = (f"Set-Content -LiteralPath '{str(marker).replace(chr(39), chr(39)*2)}' -Value ran"
                   if os.name == "nt" else "touch " + setup.shlex.quote(str(marker)))
        manifest_path = home / "hooks.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["hooks"]["SessionStart"].append({"hooks": [{"type": "command", "command": command,
                                                          "commandWindows": command}]})
        manifest_path.write_text(json.dumps(manifest))
        with setup.codex(codex, home, tmp_path) as client:
            current_config, current_hooks = setup.inventory(client, tmp_path)
            unrelated = [h for h in current_hooks if h["command"] == command]
            assert len(unrelated) == 1
            setup.trust_hooks(client, current_config, unrelated, home, report())
        first = (home / "config.toml").read_bytes()
        result = setup.setup(options(trust="no", non_interactive=True))
        _ready_unless_overloaded(result)
        assert (home / "config.toml").read_bytes() == first
        assert not result["backups"]
        assert not marker.exists()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
