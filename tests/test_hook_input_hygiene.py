"""What the plugin's bash hooks take from their inputs (plugin/hooks/*.sh).

- A credential file saved with a CRLF line end (Notepad, Set-Content) reads as
  its token; a carriage return or newline anywhere else still refuses the file.
- The session id and start source become query parameters or a JSON body only
  when they are id-shaped, with the character set coordination-prompt.sh uses.
- Under Git Bash the update notice names the launcher where the Windows
  installers put it (%LOCALAPPDATA%), by its drive path with forward slashes.
- lifecycle.ps1 reads a credential file below a link only when the native
  client's ``private-path-check`` answers with its exact trusted line.

POSIX bash runs these; the Git Bash branch is reached through OSTYPE, which
bash takes from the environment, and a stand-in cygpath.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugin" / "hooks"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="POSIX bash runs these; Git Bash runs lifecycle-backed ACL checks instead",
)


class _Daemon:
    def __init__(self):
        self.requests = []
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                daemon.requests.append({
                    "path": parts.path,
                    "query": parse_qs(parts.query),
                    "auth": self.headers.get("Authorization"),
                    "body": body,
                })
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            do_GET = _record
            do_POST = _record

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def paths(self, path):
        return [r for r in self.requests if r["path"] == path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)


@pytest.fixture
def daemon():
    d = _Daemon()
    yield d
    d.close()


def _env(tmp_path, url, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PSEUDOLIFE_", "CLAUDE_", "CODEX_")) and key != "PLUGIN_ROOT"}
    env.update({"HOME": str(home), "CODEX_HOME": str(home / ".codex"),
                "PSEUDOLIFE_MCP_DAEMON_URL": url,
                "PSEUDOLIFE_DIGEST_DIR": str(tmp_path / "digests"),
                "XDG_DATA_HOME": str(tmp_path / "xdg")})
    env.update(extra)
    return env


def _run(script, env, payload, *args):
    return subprocess.run(["bash", str(HOOKS / script), *args], input=payload, env=env,
                          capture_output=True, text=True, timeout=30, check=True)


def _token_file(tmp_path, data: bytes):
    path = tmp_path / "token"
    path.write_bytes(data)
    path.chmod(0o600)
    return path


START = json.dumps({"session_id": "sess-1", "source": "startup", "hook_event_name": "SessionStart"})
END = json.dumps({"session_id": "sess-1", "reason": "exit", "hook_event_name": "SessionEnd"})


@pytest.mark.parametrize("script,payload,path", [
    ("session-start.sh", START, "/api/hook/session-start"),
    ("session-end.sh", END, "/api/hook/session-end"),
])
@pytest.mark.parametrize("ending", [b"\r\n", b"\r", b"\n", b""])
def test_a_crlf_credential_file_reads_as_its_token(tmp_path, daemon, script, payload, path, ending):
    token = _token_file(tmp_path, b"fixture-token" + ending)
    _run(script, _env(tmp_path, daemon.url, PSEUDOLIFE_MCP_TOKEN_FILE=str(token)), payload)
    sent = daemon.paths(path)
    assert sent and {r["auth"] for r in sent} == {"Bearer fixture-token"}, daemon.requests


@pytest.mark.parametrize("data", [b"fixture\rtoken\r\n", b"fixture token\n", b"\r\n",
                                  # Two lines, the last with CRLF: after the CR is dropped,
                                  # the newline inside must still refuse it.
                                  b"fixture\ntoken\r\n", b"fixture\ntoken\n"])
def test_whitespace_inside_the_token_still_refuses_the_file(tmp_path, daemon, data):
    token = _token_file(tmp_path, data)
    out = _run("session-start.sh",
               _env(tmp_path, daemon.url, PSEUDOLIFE_MCP_TOKEN_FILE=str(token)), START).stdout
    assert "credential file is invalid" in out
    assert not daemon.paths("/api/hook/session-start")


@pytest.mark.parametrize("sid,src,sent_sid,sent_src", [
    ("sess-1", "startup", "sess-1", "startup"),
    ("0a1B.c_d-9", "resume", "0a1B.c_d-9", "resume"),
    ("sess&source=forged", "startup", None, None),
    ("sess 1", "startup", None, None),
    ("x" * 129, "startup", None, None),
    ("sess-1", "start&plugin_version=9", "sess-1", ""),
])
def test_session_start_sends_only_id_shaped_session_fields(tmp_path, daemon, sid, src,
                                                           sent_sid, sent_src):
    payload = json.dumps({"session_id": sid, "source": src})
    _run("session-start.sh", _env(tmp_path, daemon.url, PSEUDOLIFE_MCP_TOKEN="fixture-token"),
         payload)
    (request,) = daemon.paths("/api/hook/session-start")
    query = request["query"]
    if sent_sid is None:
        assert "session_id" not in query and "source" not in query, query
    else:
        assert query["session_id"] == [sent_sid], query
        # parse_qs drops a blank value, so a refused source is simply absent.
        assert query.get("source", [""]) == [sent_src], query
    assert "9" not in query.get("plugin_version", []), query


@pytest.mark.parametrize("sid,posted", [
    ("sess-1", True),
    ("0a1B.c_d-9", True),
    ("sess 1", False),
    ("sess,1", False),
    ("x" * 129, False),
])
def test_session_end_posts_only_an_id_shaped_session_id(tmp_path, daemon, sid, posted):
    payload = json.dumps({"session_id": sid, "reason": "exit"})
    _run("session-end.sh", _env(tmp_path, daemon.url, PSEUDOLIFE_MCP_TOKEN="fixture-token"),
         payload)
    sent = daemon.paths("/api/hook/session-end")
    if posted:
        assert [json.loads(r["body"]) for r in sent] == [{"session_id": sid}]
    else:
        assert sent == []


def _fake_cygpath(bin_dir: Path):
    """``cygpath -u`` keeps the path; ``cygpath -m`` gives a drive path with forward slashes."""
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "cygpath"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  -u) printf "%s\\n" "$2" ;;\n'
        '  -m) printf "C:%s\\n" "$2" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n")
    script.chmod(0o755)


def test_git_bash_names_the_localappdata_launcher_by_its_windows_path(tmp_path, daemon):
    # Brackets in the path must be encoded, or curl reads them as a URL range
    # and drops the whole briefing request.
    local = tmp_path / "local[1]appdata"
    launcher = local / "pseudolife-mcp" / "bin" / "pseudolife-mcp.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    fake = tmp_path / "fakebin"
    _fake_cygpath(fake)
    env = _env(tmp_path, daemon.url, PSEUDOLIFE_MCP_TOKEN="fixture-token", OSTYPE="msys",
               LOCALAPPDATA=str(local), PATH=f"{fake}{os.pathsep}{os.environ['PATH']}")
    _run("session-start.sh", env, START)
    (request,) = daemon.paths("/api/hook/session-start")
    # Forward slashes: Git Bash strips backslashes from a command run as typed.
    assert request["query"].get("launcher") == ["C:" + str(launcher)], request["query"]


def test_posix_launcher_lookup_is_unchanged(tmp_path, daemon):
    # The near miss: outside Git Bash the XDG location still names the launcher.
    launcher = tmp_path / "xdg" / "pseudolife-mcp" / "bin" / "pseudolife-mcp"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    env = _env(tmp_path, daemon.url, PSEUDOLIFE_MCP_TOKEN="fixture-token",
               LOCALAPPDATA=str(tmp_path / "unused"))
    _run("session-start.sh", env, START)
    (request,) = daemon.paths("/api/hook/session-start")
    assert request["query"].get("launcher") == [str(launcher)], request["query"]


# ── lifecycle.ps1: a link above the credential file ─────────────────────────

def _pwsh():
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    return pwsh


def _launcher(tmp_path, answer: str, code: int):
    script = tmp_path / "fake-launcher"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{answer}'\nexit {code}\n")
    script.chmod(0o755)
    return script


def _linked_token(tmp_path):
    real = tmp_path / "real-dir"
    real.mkdir()
    token = real / "token"
    token.write_bytes(b"fixture-token\n")
    token.chmod(0o600)
    linked = tmp_path / "linked-dir"
    linked.symlink_to(real, target_is_directory=True)
    return linked / "token"


def _run_lifecycle(env):
    return subprocess.run(
        [_pwsh(), "-NoProfile", "-File", str(HOOKS / "lifecycle.ps1"), "-Event", "SessionStart"],
        input=START, env=env, capture_output=True, text=True, timeout=60, check=True).stdout


@pytest.mark.parametrize("launcher", [None, ("pseudolife-private-path: refused: no", 1),
                                      ("pseudolife-private-path: trusted (old)", 0)])
def test_lifecycle_refuses_a_token_below_a_link_the_client_does_not_trust(tmp_path, daemon, launcher):
    token = _linked_token(tmp_path)
    shim = _launcher(tmp_path, *launcher) if launcher else tmp_path / "absent-launcher"
    out = _run_lifecycle(_env(tmp_path, daemon.url, PSEUDOLIFE_MCP_TOKEN_FILE=str(token),
                              PSEUDOLIFE_SHIM_LAUNCHER=str(shim)))
    assert "session briefing unavailable" in out, out
    assert not daemon.paths("/api/hook/session-start")


def test_lifecycle_follows_a_link_the_client_trusts_and_a_plain_path(tmp_path, daemon):
    # The near misses: the client's exact verdict, and no link at all.
    token = _linked_token(tmp_path)
    shim = _launcher(tmp_path, "pseudolife-private-path: trusted", 0)
    _run_lifecycle(_env(tmp_path, daemon.url, PSEUDOLIFE_MCP_TOKEN_FILE=str(token),
                        PSEUDOLIFE_SHIM_LAUNCHER=str(shim)))
    plain = tmp_path / "real-dir" / "token"
    _run_lifecycle(_env(tmp_path, daemon.url, PSEUDOLIFE_MCP_TOKEN_FILE=str(plain),
                        PSEUDOLIFE_SHIM_LAUNCHER=str(tmp_path / "absent-launcher")))
    sent = daemon.paths("/api/hook/session-start")
    assert [r["auth"] for r in sent] == ["Bearer fixture-token"] * 2, daemon.requests
