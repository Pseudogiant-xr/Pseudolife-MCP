"""``pseudolife-mcp expose``: put the daemon on the tailnet in one command.

Every case runs against a fake ``tailscale`` placed first on PATH (a small
Python script, with a ``.cmd`` launcher on Windows) whose state lives in a
JSON file beside it, and an in-process HTTP stub for the daemon's
``/health``. The fake records every argv it is called with, so a test can
say exactly which serve commands ran. The default install locations are a
seam the tests point at an empty directory, so a real Tailscale on the test
machine is never found or touched.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import textwrap
import threading

import pytest

from pseudolife_memory import expose_cli

BANK = "0123456789abcdef"

_FAKE = textwrap.dedent('''\
    import json, sys
    from pathlib import Path

    state_path = Path(__file__).with_name("state.json")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    args = sys.argv[1:]
    state["calls"].append(args)

    def save():
        state_path.write_text(json.dumps(state), encoding="utf-8")

    def done(out="", err="", rc=0):
        save()
        if out:
            sys.stdout.write(out)
        if err:
            sys.stderr.write(err)
        sys.exit(rc)

    if args == ["status", "--json"]:
        if state["backend"] is None:
            done(err="failed to connect to local tailscaled; it doesn't appear to be running\\n", rc=1)
        ips = ([state["ip"]] if state["ip"] else []) + ["fd00::1"]
        done(json.dumps({"BackendState": state["backend"], "Self": {"TailscaleIPs": ips}}))
    if args == ["ip", "-4"]:
        if not state["ip"]:
            done(err="no current Tailscale IPv4 address\\n", rc=1)
        done(state["ip"] + "\\n")
    if args == ["serve", "status", "--json"]:
        if state["status_fail_after_serve"] and any(c[:2] == ["serve", "--bg"] for c in state["calls"]):
            done(err="failed to get serve config: context deadline exceeded\\n", rc=1)
        done(state["serve_raw"] if state.get("serve_raw") is not None
             else json.dumps(state["serve"]))
    if args[:1] == ["serve"] and args[-1] == "off":
        if state["off_rc"]:
            done(err=state["off_err"], rc=state["off_rc"])
        if state["off_noop"]:
            done()
        port = args[1].split("=", 1)[1]
        state["serve"].get("TCP", {}).pop(port, None)
        done()
    if args[:2] == ["serve", "--bg"]:
        if state["serve_rc"]:
            done(err=state["serve_err"], rc=state["serve_rc"])
        port = args[2].split("=", 1)[1]
        target = args[3].split("://", 1)[1]
        result = state["serve_result"]
        if result == "takes":
            state["serve"].setdefault("TCP", {})[port] = {"TCPForward": target}
        elif isinstance(result, dict):
            state["serve"].setdefault("TCP", {})[port] = result
        done()
    done(err="unexpected arguments\\n", rc=64)
''')


class FakeTailscale:
    def __init__(self, root: Path):
        self.dir = root / "bin"
        self.dir.mkdir()
        # Not named tailscale.py, which a PATHEXT listing .PY would find,
        # and called by absolute path: PATH holds only this directory.
        script = self.dir / "fake_tailscale.py"
        script.write_text(_FAKE, encoding="utf-8")
        if os.name == "nt":
            (self.dir / "tailscale.cmd").write_text(
                f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            launcher = self.dir / "tailscale"
            launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                                encoding="utf-8")
            launcher.chmod(0o755)
        self.state = {"backend": "Running", "ip": "127.0.0.1", "serve": {}, "serve_raw": None,
                      "serve_rc": 0, "serve_err": "", "serve_result": "takes",
                      "off_rc": 0, "off_err": "", "off_noop": False,
                      "status_fail_after_serve": False, "calls": []}
        self.save()

    def save(self):
        (self.dir / "state.json").write_text(json.dumps(self.state), encoding="utf-8")

    def load(self) -> dict:
        self.state = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        return self.state

    def set(self, **changes):
        self.state.update(changes)
        self.save()

    def calls(self) -> list[list[str]]:
        return self.load()["calls"]

    def changing_calls(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:1] == ["serve"] and c[1:2] != ["status"]]


class Daemon:
    """``/health`` only. ``after`` holds the answers for requests after the
    first: a dict is served, ``"drop"`` closes the connection unanswered."""

    def __init__(self, *, status="ok", auth=True, bank=BANK, raw=None):
        self.health = {"status": status, "auth": auth, "version": "0.0.0-fixture", "bank": bank}
        # (status code, headers, body) served instead of the JSON payload
        self.raw = raw
        self.after: list = []
        self.seen = 0
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                daemon.seen += 1
                if daemon.raw is not None:
                    code, headers, body = daemon.raw
                    self.send_response(code)
                    for name, value in headers:
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                answer = daemon.health
                if daemon.seen > 1 and daemon.after:
                    answer = daemon.after.pop(0) if len(daemon.after) > 1 else daemon.after[0]
                if answer == "drop":
                    self.close_connection = True
                    self.connection.close()
                    return
                body = json.dumps(answer).encode()
                self.send_response(200 if answer.get("status") == "ok" else 503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def daemon():
    stub = Daemon()
    yield stub
    stub.close()


@pytest.fixture
def tailscale(tmp_path, monkeypatch):
    fake = FakeTailscale(tmp_path)
    monkeypatch.setenv("PATH", str(fake.dir))
    monkeypatch.setattr(expose_cli, "default_locations", lambda: [tmp_path / "none" / "tailscale"])
    monkeypatch.setattr(expose_cli, "interactive", lambda: False)
    return fake


def _ours(port: int) -> dict:
    return {"TCP": {str(port): {"TCPForward": f"127.0.0.1:{port}"}}}


def run(capsys, *argv) -> tuple[int, str, str]:
    code = expose_cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def run_json(capsys, *argv) -> tuple[int, dict]:
    code = expose_cli.main([*argv, "--json"])
    out, _err = capsys.readouterr()
    return code, json.loads(out)


# -- tailscale: refusals before any change --------------------------------------

def test_a_daemon_that_does_not_answer_is_refused(tailscale, capsys):
    code, out, err = run(capsys, "tailscale", "--port", "1", "--yes")
    assert code == 4
    assert "http://127.0.0.1:1/health" in err
    assert tailscale.calls() == []


def test_an_unauthenticated_daemon_is_never_exposed(tailscale, capsys):
    stub = Daemon(auth=False)
    try:
        code, out, err = run(capsys, "tailscale", "--port", str(stub.port), "--yes")
    finally:
        stub.close()
    assert code == 4
    assert '"auth": true' in err and "token" in err
    assert tailscale.calls() == []


def test_a_degraded_daemon_is_refused(tailscale, capsys):
    stub = Daemon(status="degraded")
    try:
        code, _out, err = run(capsys, "tailscale", "--port", str(stub.port), "--yes")
    finally:
        stub.close()
    assert code == 4 and "degraded" in err
    assert tailscale.calls() == []


def test_tailscale_not_installed_names_the_fix(tmp_path, monkeypatch, daemon, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(expose_cli, "default_locations", lambda: [tmp_path / "none" / "tailscale"])
    code, _out, err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 4
    assert "install Tailscale" in err and "tailscale up" in err


def test_the_default_install_location_is_found_when_path_has_none(tmp_path, monkeypatch, daemon, capsys):
    fake = FakeTailscale(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    launcher = fake.dir / ("tailscale.cmd" if os.name == "nt" else "tailscale")
    monkeypatch.setattr(expose_cli, "default_locations", lambda: [tmp_path / "none" / "x", launcher])
    code, report = run_json(capsys, "status", "--port", str(daemon.port))
    assert code == 0 and report["tailscale"] == str(launcher)


@pytest.mark.parametrize("backend", [None, "NeedsLogin", "Stopped"])
def test_tailscale_not_running_names_tailscale_up(tailscale, daemon, capsys, backend):
    tailscale.set(backend=backend)
    code, _out, err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 4
    assert "tailscale up" in err
    assert tailscale.changing_calls() == []


def test_a_foreign_serve_on_the_port_is_never_replaced(tailscale, daemon, capsys):
    port = daemon.port
    tailscale.set(serve={"TCP": {str(port): {"TCPForward": f"127.0.0.1:{port + 1}"}}})
    code, _out, err = run(capsys, "tailscale", "--port", str(port), "--yes")
    assert code == 4
    assert f"127.0.0.1:{port + 1}" in err
    assert tailscale.changing_calls() == []


@pytest.mark.parametrize("handler", [
    {"HTTPS": True},
    {"TCPForward": "127.0.0.1:{port}", "TerminateTLS": "<machine>.<tailnet>.ts.net"},
    {"TCPForward": "127.0.0.1:{port}", "ProxyProtocol": 2},
])
def test_a_serve_that_is_not_a_plain_forward_counts_as_foreign(tailscale, daemon, capsys, handler):
    port = daemon.port
    entry = {k: (v.format(port=port) if isinstance(v, str) else v) for k, v in handler.items()}
    tailscale.set(serve={"TCP": {str(port): entry}})
    code, _out, _err = run(capsys, "tailscale", "--port", str(port), "--yes")
    assert code == 4
    assert tailscale.changing_calls() == []


def test_a_foreground_serve_on_the_port_counts_as_foreign(tailscale, daemon, capsys):
    port = daemon.port
    tailscale.set(serve={"Foreground": {"session-1": _ours(port)}})
    code, _out, _err = run(capsys, "tailscale", "--port", str(port), "--yes")
    assert code == 4
    assert tailscale.changing_calls() == []


def test_an_unreadable_serve_status_is_refused(tailscale, daemon, capsys):
    tailscale.set(serve_raw="not json")
    code, _out, _err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 4
    assert tailscale.changing_calls() == []


def test_already_exposed_exits_zero_without_a_serve_call(tailscale, daemon, capsys):
    tailscale.set(serve=_ours(daemon.port))
    code, out, _err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 0
    assert "already exposed" in out
    assert f"http://127.0.0.1:{daemon.port}" in out
    assert tailscale.changing_calls() == []


def test_no_tty_without_yes_exits_2_before_any_change(tailscale, daemon, capsys):
    code, out, err = run(capsys, "tailscale", "--port", str(daemon.port))
    assert code == 2
    assert "--yes" in err
    # The plan is still shown, with the exact command.
    assert f"tailscale serve --bg --tcp={daemon.port} tcp://127.0.0.1:{daemon.port}" in out
    assert tailscale.changing_calls() == []


def test_a_declined_confirmation_exits_2(tailscale, daemon, capsys, monkeypatch):
    monkeypatch.setattr(expose_cli, "interactive", lambda: True)
    monkeypatch.setattr(expose_cli, "_ask", lambda question: False)
    code, _out, _err = run(capsys, "tailscale", "--port", str(daemon.port))
    assert code == 2
    assert tailscale.changing_calls() == []


def test_a_confirmed_run_exposes(tailscale, daemon, capsys, monkeypatch):
    monkeypatch.setattr(expose_cli, "interactive", lambda: True)
    monkeypatch.setattr(expose_cli, "_ask", lambda question: True)
    code, _out, _err = run(capsys, "tailscale", "--port", str(daemon.port))
    assert code == 0
    assert len(tailscale.changing_calls()) == 1


@pytest.mark.parametrize("message", [
    "Access denied: serve config denied\n",
    "permission denied; use 'sudo tailscale serve' or 'tailscale set --operator=$USER'\n",
])
def test_a_permission_refusal_on_linux_names_the_operator_fix(tailscale, daemon, capsys,
                                                              monkeypatch, message):
    monkeypatch.setattr(expose_cli, "platform", lambda: "linux")
    tailscale.set(serve_rc=1, serve_err=message)
    code, _out, err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 4
    assert "sudo tailscale set --operator=$USER" in err
    # One attempt, nothing to undo.
    assert [c for c in tailscale.changing_calls() if c[-1] == "off"] == []


@pytest.mark.parametrize(("system", "expected"), [
    ("win32", "elevated"),
    ("darwin", "logged in to the Tailscale app"),
])
def test_a_permission_refusal_names_the_fix_for_this_os(tailscale, daemon, capsys, monkeypatch,
                                                        system, expected):
    monkeypatch.setattr(expose_cli, "platform", lambda: system)
    tailscale.set(serve_rc=1, serve_err="Access denied: serve config denied\n")
    code, _out, err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 4
    assert expected in err
    assert "--operator" not in err


def test_the_word_operator_alone_is_not_a_permission_refusal(tailscale, daemon, capsys, monkeypatch):
    monkeypatch.setattr(expose_cli, "platform", lambda: "linux")
    tailscale.set(serve_rc=1, serve_err="invalid serve target: unknown operator in expression\n")
    code, _out, err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 4
    assert "tailscale refused the serve" in err
    assert "set --operator" not in err


def test_another_tailscale_refusal_that_changed_nothing_exits_4(tailscale, daemon, capsys):
    tailscale.set(serve_rc=1, serve_err="invalid TCP target\n")
    code, _out, err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 4
    assert "invalid TCP target" in err
    assert [c for c in tailscale.changing_calls() if c[-1] == "off"] == []


def test_a_serve_that_does_not_take_is_undone_and_exits_5(tailscale, daemon, capsys):
    port = daemon.port
    tailscale.set(serve_result={"TCPForward": f"127.0.0.1:{port}", "TerminateTLS": "x"})
    code, _out, err = run(capsys, "tailscale", "--port", str(port), "--yes")
    assert code == 5
    assert tailscale.changing_calls()[-1] == ["serve", f"--tcp={port}", "off"]
    assert str(port) not in tailscale.load()["serve"].get("TCP", {})
    assert "undone" in err


def test_a_serve_that_left_nothing_exits_5_without_an_undo(tailscale, daemon, capsys):
    tailscale.set(serve_result="absent")
    code, _out, _err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 5
    assert [c for c in tailscale.changing_calls() if c[-1] == "off"] == []


# -- tailscale: success and the advisory probe -----------------------------------

def test_success_runs_the_exact_command_and_prints_the_url(tailscale, daemon, capsys):
    port = daemon.port
    code, out, _err = run(capsys, "tailscale", "--port", str(port), "--yes")
    assert code == 0
    assert tailscale.changing_calls() == [["serve", "--bg", f"--tcp={port}", f"tcp://127.0.0.1:{port}"]]
    assert tailscale.load()["serve"] == _ours(port)
    url = f"http://127.0.0.1:{port}"
    assert url in out
    assert "same bank" in out
    # The next step for a joining machine: invite it.
    assert "pseudolife-mcp invite <machine>" in out
    assert "--token-file <file>" not in out


def test_a_failing_probe_is_reported_and_does_not_roll_back(tailscale, daemon, capsys):
    daemon.after = ["drop"]
    port = daemon.port
    code, out, err = run(capsys, "tailscale", "--port", str(port), "--yes")
    assert code == 0
    assert f"curl http://127.0.0.1:{port}/health" in out + err
    assert tailscale.load()["serve"] == _ours(port)
    assert [c for c in tailscale.changing_calls() if c[-1] == "off"] == []


def test_a_different_bank_through_the_tailnet_is_a_warning_not_a_rollback(tailscale, daemon, capsys):
    daemon.after = [dict(daemon.health, bank="f" * 16)]
    code, _out, err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 0
    assert "different bank" in err
    assert tailscale.load()["serve"] == _ours(daemon.port)


def test_the_json_report_shape(tailscale, daemon, capsys):
    port = daemon.port
    code, report = run_json(capsys, "tailscale", "--port", str(port), "--yes")
    assert code == 0
    assert report["action"] == "tailscale"
    assert report["port"] == port
    assert report["state"] == "exposed"
    assert report["url"] == f"http://127.0.0.1:{port}"
    assert report["command"] == ["tailscale", "serve", "--bg", f"--tcp={port}", f"tcp://127.0.0.1:{port}"]
    assert report["daemon"] == {"status": "ok", "auth": True, "bank": BANK}
    assert report["probe"]["reachable"] is True and report["probe"]["same_bank"] is True
    assert report["error"] is None and report["exit"] == 0
    assert isinstance(report["warnings"], list) and isinstance(report["notes"], list)


def test_the_json_report_of_a_refusal(tailscale, capsys):
    code, report = run_json(capsys, "tailscale", "--port", "1", "--yes")
    assert code == 4
    assert report["exit"] == 4 and report["error"]
    assert report["state"] is None and report["url"] is None


# -- off ------------------------------------------------------------------------

def test_off_removes_our_forward(tailscale, daemon, capsys):
    port = daemon.port
    tailscale.set(serve=_ours(port))
    code, out, _err = run(capsys, "off", "--port", str(port), "--yes")
    assert code == 0
    assert tailscale.changing_calls() == [["serve", f"--tcp={port}", "off"]]
    assert tailscale.load()["serve"].get("TCP", {}) == {}


def test_off_leaves_a_foreign_serve_alone(tailscale, daemon, capsys):
    port = daemon.port
    tailscale.set(serve={"TCP": {str(port): {"TCPForward": "127.0.0.1:9999"}}})
    code, _out, err = run(capsys, "off", "--port", str(port), "--yes")
    assert code == 4
    assert "127.0.0.1:9999" in err and "left alone" in err
    assert tailscale.changing_calls() == []


def test_off_when_not_exposed_is_a_no_op(tailscale, daemon, capsys):
    code, out, _err = run(capsys, "off", "--port", str(daemon.port), "--yes")
    assert code == 0
    assert "not exposed" in out
    assert tailscale.changing_calls() == []


def test_off_without_a_tty_or_yes_exits_2(tailscale, daemon, capsys):
    tailscale.set(serve=_ours(daemon.port))
    code, _out, _err = run(capsys, "off", "--port", str(daemon.port))
    assert code == 2
    assert tailscale.changing_calls() == []


def test_off_refused_for_permission_names_the_fix(tailscale, daemon, capsys, monkeypatch):
    monkeypatch.setattr(expose_cli, "platform", lambda: "linux")
    tailscale.set(serve=_ours(daemon.port), off_rc=1, off_err="Access denied: serve config denied\n")
    code, _out, err = run(capsys, "off", "--port", str(daemon.port), "--yes")
    assert code == 4
    assert "sudo tailscale set --operator=$USER" in err


# -- status ---------------------------------------------------------------------

def test_status_prints_the_url_when_exposed(tailscale, daemon, capsys):
    tailscale.set(serve=_ours(daemon.port))
    code, out, _err = run(capsys, "status", "--port", str(daemon.port))
    assert code == 0
    assert f"http://127.0.0.1:{daemon.port}" in out
    assert tailscale.changing_calls() == []


def test_status_json_when_exposed(tailscale, daemon, capsys):
    tailscale.set(serve=_ours(daemon.port))
    code, report = run_json(capsys, "status", "--port", str(daemon.port))
    assert code == 0
    assert report["action"] == "status"
    assert report["state"] == "exposed"
    assert report["url"] == f"http://127.0.0.1:{daemon.port}"


def test_status_not_exposed(tailscale, daemon, capsys):
    code, out, _err = run(capsys, "status", "--port", str(daemon.port))
    assert code == 0 and "not exposed" in out
    code, report = run_json(capsys, "status", "--port", str(daemon.port))
    assert report["state"] == "not_exposed" and report["url"] is None


def test_status_names_a_foreign_serve(tailscale, daemon, capsys):
    tailscale.set(serve={"TCP": {str(daemon.port): {"HTTPS": True}}})
    code, report = run_json(capsys, "status", "--port", str(daemon.port))
    assert code == 0
    assert report["state"] == "foreign" and report["url"] is None
    assert "HTTPS" in report["detail"]


def test_status_without_tailscale(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(expose_cli, "default_locations", lambda: [])
    code, report = run_json(capsys, "status")
    assert code == 0
    assert report["state"] == "unavailable" and report["url"] is None


def test_an_empty_serve_status_reads_as_nothing_served(tailscale, daemon, capsys):
    for raw in ("", "null", "{}"):
        tailscale.set(serve_raw=raw)
        code, report = run_json(capsys, "status", "--port", str(daemon.port))
        assert code == 0 and report["state"] == "not_exposed", raw


# -- Funnel ---------------------------------------------------------------------

FUNNEL_HOST = "<machine>.<tailnet>.ts.net"


def _funnelled(port: int, key_port=None, on=True) -> dict:
    return dict(_ours(port), AllowFunnel={f"{FUNNEL_HOST}:{key_port or port}": on})


def test_a_funnelled_port_is_foreign_even_with_our_forward(tailscale, daemon, capsys):
    port = daemon.port
    tailscale.set(serve=_funnelled(port))
    code, _out, err = run(capsys, "tailscale", "--port", str(port), "--yes")
    assert code == 4
    assert "Funnel (public internet)" in err
    assert tailscale.changing_calls() == []


def test_status_never_reports_a_funnelled_port_as_exposed(tailscale, daemon, capsys):
    port = daemon.port
    tailscale.set(serve=_funnelled(port))
    code, report = run_json(capsys, "status", "--port", str(port))
    assert code == 0
    assert report["state"] == "foreign" and report["url"] is None
    assert "Funnel (public internet)" in report["detail"]


def test_off_leaves_a_funnelled_port_alone(tailscale, daemon, capsys):
    port = daemon.port
    tailscale.set(serve=_funnelled(port))
    code, _out, err = run(capsys, "off", "--port", str(port), "--yes")
    assert code == 4 and "left alone" in err
    assert tailscale.changing_calls() == []


def test_a_foreground_funnel_on_the_port_is_foreign(tailscale, daemon, capsys):
    port = daemon.port
    tailscale.set(serve=dict(_ours(port), Foreground={
        "session-1": {"AllowFunnel": {f"{FUNNEL_HOST}:{port}": True}}}))
    _code, report = run_json(capsys, "status", "--port", str(port))
    assert report["state"] == "foreign" and "Funnel (public internet)" in report["detail"]


@pytest.mark.parametrize(("key_port", "on"), [(443, True), (None, False)])
def test_a_funnel_elsewhere_or_switched_off_does_not_count(tailscale, daemon, capsys, key_port, on):
    port = daemon.port
    tailscale.set(serve=_funnelled(port, key_port=key_port, on=on))
    code, out, _err = run(capsys, "tailscale", "--port", str(port), "--yes")
    assert code == 0 and "already exposed" in out


# -- the local daemon check -----------------------------------------------------

def test_a_redirect_on_the_local_port_is_not_the_daemon(tailscale, capsys):
    target = Daemon()
    # A JSON body on the redirect must not pass for the daemon's /health.
    stub = Daemon(raw=(302, [("Location", f"http://127.0.0.1:{target.port}/health"),
                             ("Content-Type", "application/json")],
                       json.dumps({"status": "ok", "auth": True}).encode()))
    try:
        code, _out, err = run(capsys, "tailscale", "--port", str(stub.port), "--yes")
    finally:
        stub.close()
        target.close()
    assert code == 4
    assert f"something that is not the Pseudolife daemon answers on 127.0.0.1:{stub.port}" in err
    assert target.seen == 0  # the redirect was not followed
    assert tailscale.calls() == []


def test_a_non_json_answer_on_the_local_port_is_not_the_daemon(tailscale, capsys):
    stub = Daemon(raw=(200, [("Content-Type", "text/html")], b"<html>hello</html>"))
    try:
        code, _out, err = run(capsys, "tailscale", "--port", str(stub.port), "--yes")
    finally:
        stub.close()
    assert code == 4
    assert f"something that is not the Pseudolife daemon answers on 127.0.0.1:{stub.port}" in err
    assert tailscale.calls() == []


# -- edge branches --------------------------------------------------------------

def test_an_unreadable_status_after_the_serve_is_reported_not_undone(tailscale, daemon, capsys):
    tailscale.set(status_fail_after_serve=True)
    code, _out, err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 5
    assert "could not verify" in err and "tailscale serve status" in err
    assert "undo failed" not in err
    assert [c for c in tailscale.changing_calls() if c[-1] == "off"] == []


def test_off_reports_a_forward_tailscale_did_not_remove(tailscale, daemon, capsys):
    tailscale.set(serve=_ours(daemon.port), off_noop=True)
    code, _out, err = run(capsys, "off", "--port", str(daemon.port), "--yes")
    assert code == 4
    assert "did not remove" in err


def test_off_leaves_a_foreground_serve_alone(tailscale, daemon, capsys):
    port = daemon.port
    tailscale.set(serve={"Foreground": {"session-1": _ours(port)}})
    code, _out, err = run(capsys, "off", "--port", str(port), "--yes")
    assert code == 4
    assert "foreground" in err and "left alone" in err
    assert tailscale.changing_calls() == []


def test_no_tailnet_ipv4_is_refused_before_any_change(tailscale, daemon, capsys):
    tailscale.set(ip="")
    code, _out, err = run(capsys, "tailscale", "--port", str(daemon.port), "--yes")
    assert code == 4 and "IPv4" in err
    assert tailscale.changing_calls() == []


def test_status_without_a_tailnet_ipv4_is_unavailable(tailscale, daemon, capsys):
    tailscale.set(ip="", serve=_ours(daemon.port))
    code, report = run_json(capsys, "status", "--port", str(daemon.port))
    assert code == 0
    assert report["state"] == "unavailable" and report["url"] is None


@pytest.mark.skipif(os.name != "nt", reason="the Windows default install location")
def test_the_windows_location_prefers_the_64_bit_program_files(monkeypatch, tmp_path):
    monkeypatch.setenv("ProgramW6432", str(tmp_path / "pf64"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    assert expose_cli.default_locations() == [tmp_path / "pf64" / "Tailscale" / "tailscale.exe"]
    monkeypatch.delenv("ProgramW6432")
    assert expose_cli.default_locations() == [tmp_path / "pf" / "Tailscale" / "tailscale.exe"]


# -- usage ----------------------------------------------------------------------

@pytest.mark.parametrize("argv", [[], ["sideways"], ["tailscale", "--port", "0"],
                                  ["tailscale", "--port", "70000"], ["status", "--yes"]])
def test_usage_errors_exit_2(tailscale, capsys, argv):
    assert expose_cli.main(argv) == 2
    assert tailscale.calls() == []


def test_the_cli_dispatches_expose(monkeypatch, tailscale, capsys):
    from pseudolife_memory import cli

    assert "expose" in cli._USAGE
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "expose", "status", "--json"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["action"] == "status"
