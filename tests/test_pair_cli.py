"""``pseudolife-mcp pair``: join a bank with a pairing code.

Every case runs against a fake home (HOME / USERPROFILE under ``tmp_path``)
and an in-process HTTP stub for the daemon that records every request. The
stub implements the redemption contract: ``POST /api/pair`` with a code and
the token's SHA-256, an idempotent retry for the same hash, and a
bearer-checked ``/api/episodes`` that accepts a token whose hash it paired.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading

import pytest

from pseudolife_memory import client_config, pair_cli
from pseudolife_memory.credentials import _write_token_file

ROOT = Path(__file__).resolve().parents[1]
CODE = "abcd-efgh-jkmn"
CANONICAL = "ABCDEFGHJKMN"
ERRORS = {400: "pairing_refused", 429: "rate_limited", 503: "pairing_unavailable"}


# -- the stub daemon ------------------------------------------------------------

class Daemon:
    """``answers`` is consumed one per ``POST /api/pair``: ``"pair"`` redeems
    (or repeats the original 200 for the same hash), ``"drop"`` redeems and
    closes the connection without answering, an int answers that status.
    ``default`` applies once ``answers`` is empty."""

    def __init__(self, *, auth=True, status="ok", principal="laptop", tier="writer", bank="bank-1",
                 default="pair"):
        self.health = {"status": status, "auth": auth, "version": "0.0.0-fixture"}
        self.principal = principal
        self.tier = tier
        self.bank = bank
        self.answers: list = []
        self.default = default
        self.paired_hash = None
        self.accept_bearers = True
        self.requests: list[dict] = []
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _record(self, body=b""):
                daemon.requests.append({"method": self.command, "path": self.path,
                                        "headers": dict(self.headers.items()), "body": body})

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._record()
                if self.path == "/health":
                    return self._send(200, daemon.health)
                if self.path.startswith("/api/episodes"):
                    bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                    digest = hashlib.sha256(bearer.encode()).hexdigest()
                    ok = daemon.accept_bearers and bearer and digest == daemon.paired_hash
                    return self._send(200 if ok else 401, {"episodes": []} if ok else {"error": "unauthorized"})
                return self._send(404, {"error": "not_found"})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._record(body)
                if self.path != "/api/pair":
                    return self._send(404, {"error": "not_found"})
                answer = daemon.answers.pop(0) if daemon.answers else daemon.default
                if answer in ("pair", "drop"):
                    digest = json.loads(body)["token_sha256"]
                    if daemon.paired_hash is None:
                        daemon.paired_hash = digest
                    elif daemon.paired_hash != digest:
                        return self._send(400, {"error": "pairing_refused"})
                    if answer == "drop":
                        self.close_connection = True
                        return
                    return self._send(200, {"principal": daemon.principal, "tier": daemon.tier,
                                            "bank": daemon.bank})
                return self._send(answer, {"error": ERRORS.get(answer, "refused")})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def posts(self) -> list[dict]:
        return [request for request in self.requests if request["method"] == "POST"]

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
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key in ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE", "PSEUDOLIFE_MCP_TOKENS",
                "PSEUDOLIFE_MCP_DAEMON_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(pair_cli, "interactive", lambda: False)
    monkeypatch.setattr(pair_cli, "sleep", lambda seconds: None)
    return home


@pytest.fixture
def sleeps(monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(pair_cli, "sleep", seen.append)
    return seen


def bank_dir(home: Path) -> Path:
    return home / ".pseudolife-mcp"


def token_files(home: Path) -> list[Path]:
    directory = bank_dir(home)
    return sorted(directory.glob("*.token")) if directory.exists() else []


def run(capsys, *argv):
    code = pair_cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def run_json(capsys, *argv):
    code = pair_cli.main([*argv, "--json"])
    out = capsys.readouterr()
    return code, json.loads(out.out), out.out + out.err


def everything(root: Path) -> set[str]:
    return {str(path) for path in root.rglob("*")}


def assert_owner_only(path: Path):
    assert client_config.check_token_file(path) == {"status": "ready"}
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def assert_no_secret(daemon: Daemon, token: str, *texts: str):
    """The token is in no output and in no request except the verification's
    bearer; the code is in no output at all."""
    for text in texts:
        assert token not in text
        for shown in (CODE, CODE.upper(), CANONICAL):
            assert shown not in text
    for request in daemon.requests:
        assert token.encode() not in request["body"]
        for name, value in request["headers"].items():
            if token in value:
                assert (request["method"], request["path"], name.lower()) == (
                    "GET", "/api/episodes?limit=1", "authorization")


# -- success ----------------------------------------------------------------------

def test_success_writes_an_owner_only_file_named_for_the_principal(home, daemon, capsys):
    code, out, err = run(capsys, daemon.url, CODE)
    assert code == 0, err
    files = token_files(home)
    assert files == [bank_dir(home) / "laptop.token"]
    token = files[0].read_text(encoding="utf-8")
    assert_owner_only(files[0])
    [post] = daemon.posts()
    body = json.loads(post["body"])
    assert set(body) == {"code", "token_sha256"}
    assert body["code"] == CANONICAL
    assert body["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    headers = {name.lower() for name in post["headers"]}
    assert "origin" not in headers and "authorization" not in headers
    assert post["headers"]["Content-Type"] == "application/json"
    assert str(files[0]) in out and "laptop" in out and "writer" in out
    assert_no_secret(daemon, token, out, err)


def test_the_json_report_has_the_fixed_fields_and_no_secret(home, daemon, capsys):
    code, report, text = run_json(capsys, daemon.url, CODE)
    assert code == 0, text
    assert set(report) == {"url", "state", "principal", "tier", "bank", "token_file", "warnings",
                           "error", "exit"}
    assert report["state"] == "paired" and report["exit"] == 0
    assert (report["principal"], report["tier"], report["bank"]) == ("laptop", "writer", "bank-1")
    assert report["token_file"] == str(bank_dir(home) / "laptop.token")
    assert report["url"] == daemon.url
    token = Path(report["token_file"]).read_text(encoding="utf-8")
    assert_no_secret(daemon, token, text)


def test_read_code_takes_the_code_from_stdin(home, daemon, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(CODE + "\n"))
    code, out, err = run(capsys, daemon.url, "--read-code")
    assert code == 0, err
    assert json.loads(daemon.posts()[0]["body"])["code"] == CANONICAL
    assert_no_secret(daemon, token_files(home)[0].read_text(encoding="utf-8"), out, err)


def test_token_file_writes_that_path_and_does_not_move_it(home, daemon, capsys, tmp_path):
    target = tmp_path / "chosen" / "bank.token"
    code, report, text = run_json(capsys, daemon.url, CODE, "--token-file", str(target))
    assert code == 0, text
    assert report["token_file"] == str(target)
    assert_owner_only(target)
    assert token_files(home) == []


# -- usage -------------------------------------------------------------------------

def test_a_positional_code_and_read_code_together_is_usage(home, daemon, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(CODE + "\n"))
    code, report, _ = run_json(capsys, daemon.url, CODE, "--read-code")
    assert code == 2 and report["state"] == "usage"
    assert daemon.requests == [] and token_files(home) == []


@pytest.mark.parametrize("bad", ["ABCD-EFGH", "ABCD-EFGH-JKMU", "ABCD-EFGH-JKMN-P", "!!!!-????-****"])
def test_a_malformed_code_is_usage_and_writes_nothing(home, daemon, capsys, bad):
    code, out, err = run(capsys, daemon.url, bad)
    assert code == 2
    assert daemon.requests == [] and token_files(home) == []
    assert bad not in out + err


def test_no_code_is_usage(home, daemon, capsys):
    assert run(capsys, daemon.url)[0] == 2
    assert daemon.requests == []


def test_an_empty_stdin_is_usage(home, daemon, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert run(capsys, daemon.url, "--read-code")[0] == 2
    assert daemon.requests == [] and token_files(home) == []


@pytest.mark.parametrize("url", ["ftp://example.com", "http://user:secret@example.com:8765",
                                 "http://example.com:8765/mcp?key=secret"])
def test_an_invalid_url_is_usage_and_never_echoed(home, capsys, url):
    code, out, err = run(capsys, url, CODE)
    assert code == 2
    assert "secret" not in out + err
    assert token_files(home) == []


# -- refused before any change ---------------------------------------------------

def test_a_daemon_without_auth_is_refused_without_a_post(home, capsys):
    stub = Daemon(auth=False)
    try:
        code, report, _ = run_json(capsys, stub.url, CODE)
    finally:
        stub.close()
    assert code == 4 and report["state"] == "refused"
    assert stub.posts() == [] and token_files(home) == []


def test_a_degraded_health_is_refused_without_a_post(home, capsys):
    stub = Daemon(status="degraded")
    try:
        code, _report, _ = run_json(capsys, stub.url, CODE)
    finally:
        stub.close()
    assert code == 4 and stub.posts() == [] and token_files(home) == []


def test_an_unreachable_daemon_is_refused(home, capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    code, _report, _ = run_json(capsys, f"http://127.0.0.1:{port}", CODE)
    assert code == 4 and token_files(home) == []


def test_an_existing_token_file_is_refused_before_any_post(home, daemon, capsys, tmp_path):
    target = tmp_path / "existing.token"
    _write_token_file(target, "kept-" + "k" * 32)
    before = target.read_bytes()
    code, report, _ = run_json(capsys, daemon.url, CODE, "--token-file", str(target))
    assert code == 4 and report["state"] == "refused"
    assert daemon.posts() == []
    assert target.read_bytes() == before


# -- refused by the daemon ---------------------------------------------------------

def test_a_refused_code_removes_the_file(home, daemon, capsys):
    daemon.default = 400
    code, report, _ = run_json(capsys, daemon.url, CODE)
    assert code == 4 and report["state"] == "refused"
    assert report["token_file"] is None
    assert token_files(home) == []
    assert len(daemon.posts()) == 1


def test_a_rate_limited_daemon_removes_the_file_and_says_so(home, daemon, capsys):
    daemon.default = 429
    code, out, err = run(capsys, daemon.url, CODE)
    assert code == 4
    assert "pairing is rate-limited on the daemon, try again in a minute" in out + err
    assert token_files(home) == [] and len(daemon.posts()) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 405, 413, 415])
def test_other_definite_refusals_remove_the_file(home, daemon, capsys, status):
    daemon.default = status
    code, report, text = run_json(capsys, daemon.url, CODE)
    assert code == 4 and report["state"] == "refused"
    assert token_files(home) == [] and len(daemon.posts()) == 1
    if status in (401, 404, 405):
        assert "update the daemon" in report["error"]


# -- lost responses ----------------------------------------------------------------

def test_a_lost_response_is_retried_with_the_same_body(home, daemon, capsys, sleeps):
    daemon.answers = ["drop"]
    code, report, text = run_json(capsys, daemon.url, CODE)
    assert code == 0, text
    first, second = daemon.posts()
    assert first["body"] == second["body"]
    assert token_files(home) == [bank_dir(home) / "laptop.token"]
    assert len(sleeps) == 1


@pytest.mark.parametrize("answer", ["drop", 503, 500])
def test_an_unknown_outcome_keeps_the_file_and_names_it(home, daemon, capsys, sleeps, answer):
    daemon.default = answer
    code, out, err = run(capsys, daemon.url, CODE)
    assert code == 5
    [kept] = token_files(home)
    assert kept.name.startswith("pairing-")
    assert str(kept) in err
    assert "only copy" in err
    assert len(daemon.posts()) == 1 + pair_cli.RETRIES
    bodies = {post["body"] for post in daemon.posts()}
    assert len(bodies) == 1
    assert_no_secret(daemon, kept.read_text(encoding="utf-8"), out, err)


def test_a_failed_verification_keeps_the_file(home, daemon, capsys):
    daemon.accept_bearers = False
    code, report, text = run_json(capsys, daemon.url, CODE)
    assert code == 5 and report["state"] == "unverified"
    assert report["token_file"] == str(bank_dir(home) / "laptop.token")
    assert Path(report["token_file"]).exists()
    assert "only copy" in report["error"]


# -- the principal name cannot steer the path --------------------------------------

@pytest.mark.parametrize("name", ["../evil", "..", "a/b", "C:\\x", "default", "daemon", "", "a" * 65,
                                  123, None, ["laptop"], ".hidden", "Laptop"])
def test_a_rogue_principal_name_cannot_change_the_path(home, capsys, tmp_path, name):
    stub = Daemon(principal=name)
    before = everything(tmp_path)
    try:
        code, report, text = run_json(capsys, stub.url, CODE)
    finally:
        stub.close()
    assert code == 0, text
    [kept] = token_files(home)
    assert kept.parent == bank_dir(home) and kept.name.startswith("pairing-")
    assert report["token_file"] == str(kept)
    assert report["warnings"]
    assert everything(tmp_path) - before <= {str(bank_dir(home)), str(kept)}


def test_an_existing_principal_file_is_never_replaced(home, daemon, capsys):
    existing = bank_dir(home) / "laptop.token"
    _write_token_file(existing, "older-" + "o" * 32)
    before = existing.read_bytes()
    code, report, text = run_json(capsys, daemon.url, CODE)
    assert code == 0, text
    assert existing.read_bytes() == before
    kept = Path(report["token_file"])
    assert kept.name.startswith("pairing-") and kept.exists()
    assert any("laptop.token" in warning for warning in report["warnings"])


# -- the CLI -----------------------------------------------------------------------

def test_the_cli_dispatches_pair_and_usage_names_it():
    from pseudolife_memory.cli import _USAGE
    assert "\n  pair " in _USAGE
    proc = subprocess.run([sys.executable, "-m", "pseudolife_memory.cli", "pair", "--help"],
                          capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0 and "--read-code" in proc.stdout


# -- review fixes (2026-10-02) ------------------------------------------------------

@pytest.mark.parametrize("first", ["drop", 503])
@pytest.mark.parametrize("later", [429, 400])
def test_a_refusal_after_a_lost_response_keeps_the_file(home, daemon, capsys, first, later):
    """Once an attempt's outcome is unknown the code may be spent, so a
    later refusal cannot prove the token is useless: the file stays."""
    daemon.answers = [first, later]
    code, out, err = run(capsys, daemon.url, CODE)
    assert code == 5
    [kept] = token_files(home)
    assert str(kept) in err and "only copy" in err
    assert len(daemon.posts()) == 2


def test_a_remote_plain_http_url_is_warned_about(home, capsys, monkeypatch):
    monkeypatch.setattr(pair_cli, "probe_health", lambda url, timeout=None: None)
    code, report, text = run_json(capsys, "http://100.64.0.2:8765", CODE)
    assert code == 4
    assert any("plain HTTP" in line for line in report["warnings"])
    code, _out, err = run(capsys, "http://100.64.0.2:8765", CODE)
    assert "plain HTTP" in err


def test_a_loopback_url_is_not_warned_about(home, daemon, capsys):
    code, report, _text = run_json(capsys, daemon.url, CODE)
    assert code == 0 and not any("plain HTTP" in line for line in report["warnings"])


def test_a_link_whose_old_name_cannot_be_removed_names_both_files(home, daemon, capsys,
                                                                   monkeypatch):
    """POSIX move: the hard link worked but the pairing name could not be
    removed, so two files hold the token; both are named."""
    monkeypatch.setattr(pair_cli, "_POSIX_MOVE", True)
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if "pairing-" in os.fspath(path):
            raise PermissionError("fixture")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(pair_cli.os, "unlink", unlink)
    code, report, _text = run_json(capsys, daemon.url, CODE)
    assert code == 0
    files = token_files(home)
    assert bank_dir(home) / "laptop.token" in files and len(files) == 2
    assert report["token_file"] == str(bank_dir(home) / "laptop.token")
    warning = " ".join(report["warnings"])
    assert all(str(path) in warning for path in files)
