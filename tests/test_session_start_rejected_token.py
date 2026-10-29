"""The SessionStart answer says so when the daemon rejected the caller's bearer.

2026-10-30: two client machines had no memory for 11 days because their
bearer tokens had been rotated away. ``/api/hook/session-start`` answers 200
to a rejected bearer with the public instructions only, and the plugin hooks
warn only when the request itself fails, so those sessions started looking
normal. A caller that sent a bearer the daemon rejected now gets one notice
first, naming the cause and the fix. A caller that sent no bearer (the
near-miss: a hook with no token configured) keeps today's answer byte for
byte, as does a bearer the daemon could not check (the principal store is
unavailable, so the cause is not known) and any caller of a daemon with
authentication off.
"""
from __future__ import annotations

import pytest

from pseudolife_memory.principal_store import PrincipalSnapshot
from pseudolife_memory.principals import install_store, installed_store
from pseudolife_memory.web import session_hook
from pseudolife_memory.web.api import build_console_app
from pseudolife_memory.web.fixtures import FixtureService
from tests.asgi_helpers import call, stub_mcp

TOKEN = "fixture-daemon-token"
WRONG = "fixture-rotated-token-4f2a"
HEAD = "Pseudolife-MCP: the memory daemon rejected this machine's token"


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
    service = FixtureService()
    service.data_dir = tmp_path
    return service


def _app(svc, token=TOKEN):
    return build_console_app(stub_mcp, token, lambda: {"status": "ok"}, svc)


def _start(svc, authorization=None, token=TOKEN, query=""):
    headers = [(b"authorization", authorization.encode("latin-1"))] if authorization else None
    status, body = call(_app(svc, token), "GET", "/api/hook/session-start",
                        headers=headers, query=query)
    assert status == 200
    return body.decode("utf-8")


def test_the_notice_names_the_cause_and_the_fix():
    text = session_hook.token_rejected_notice()
    assert text == (
        "Pseudolife-MCP: the memory daemon rejected this machine's token, so this "
        "session has no memory access (the token was revoked, rotated or never issued "
        "for this daemon). Fix: on the daemon host run `pseudolife-mcp invite <name>` "
        "(add `--replace` if that name is still paired), then on this machine "
        "`pseudolife-mcp connect <daemon-url> --read-code`; `pseudolife-mcp doctor` "
        "shows the state.")


def test_the_notice_names_the_reported_launcher():
    text = session_hook.token_rejected_notice('"C:/Program Files/pl/pseudolife-mcp.exe"')
    assert '`"C:/Program Files/pl/pseudolife-mcp.exe" connect <daemon-url> --read-code`' in text
    assert '`"C:/Program Files/pl/pseudolife-mcp.exe" doctor`' in text
    # The daemon host's command is not the caller's launcher.
    assert "`pseudolife-mcp invite <name>`" in text


def test_a_rejected_bearer_opens_with_the_notice_before_the_no_bearer_answer(svc):
    plain = _start(svc)
    text = _start(svc, f"Bearer {WRONG}")
    assert text == session_hook.token_rejected_notice() + "\n\n" + plain


def test_the_answer_never_echoes_the_rejected_token(svc):
    text = _start(svc, f"Bearer {WRONG}", query="session_id=s1&source=startup")
    assert WRONG not in text
    assert WRONG[:8] not in text and WRONG[-6:] not in text


def test_the_notice_precedes_a_version_notice(svc):
    text = _start(svc, f"Bearer {WRONG}", query="plugin_version=0.0.1")
    assert text.startswith(HEAD)
    assert text.index(HEAD) < text.index("Pseudolife-MCP: plugin 0.0.1")


def test_a_rejected_bearer_names_the_launcher_the_hook_reported(svc):
    text = _start(svc, f"Bearer {WRONG}", query="launcher=/opt/pl/bin/pseudolife-mcp")
    assert "`/opt/pl/bin/pseudolife-mcp doctor`" in text


def test_a_rejected_bearer_registers_no_session(svc):
    text = _start(svc, f"Bearer {WRONG}", query="session_id=s1&source=startup")
    assert "Session episode:" not in text
    assert "(fixture)" not in text


@pytest.mark.parametrize("authorization", [
    None,                     # no Authorization header: the hook has no token
    "Bearer",                 # the scheme with no token
    "Bearer  \t ",            # only HTTP whitespace after the scheme
    f"Basic {WRONG}",         # not a bearer
])
def test_no_bearer_keeps_todays_answer(svc, authorization):
    text = _start(svc, authorization)
    assert HEAD not in text
    assert text == _start(svc)
    assert text == session_hook.hook_session_start(svc, authorized=False)


def test_the_accepted_bearer_gets_no_notice(svc):
    text = _start(svc, f"Bearer {TOKEN}")
    assert HEAD not in text and "(fixture)" in text


def test_a_daemon_with_auth_off_never_says_it(svc):
    for authorization in (None, f"Bearer {WRONG}"):
        text = _start(svc, authorization, token=None)
        assert HEAD not in text and "(fixture)" in text


def test_the_last_authorization_header_decides_as_the_resolver_does(svc):
    """The resolver reads the last ``Authorization`` header; so does the
    notice, or a valid-last pair would be told its token was rejected."""
    app = _app(svc)
    pairs = {
        "valid last": [(b"authorization", f"Bearer {WRONG}".encode()),
                       (b"authorization", f"Bearer {TOKEN}".encode())],
        "valid first": [(b"authorization", f"Bearer {TOKEN}".encode()),
                        (b"authorization", f"Bearer {WRONG}".encode())],
        "basic last": [(b"authorization", f"Bearer {WRONG}".encode()),
                       (b"authorization", f"Basic {WRONG}".encode())],
    }
    answers = {name: call(app, "GET", "/api/hook/session-start", headers=h)[1].decode()
               for name, h in pairs.items()}
    assert HEAD not in answers["valid last"] and "(fixture)" in answers["valid last"]
    assert answers["valid first"].startswith(HEAD)
    assert HEAD not in answers["basic last"]


@pytest.fixture
def unloaded():
    previous = installed_store()
    install_store(PrincipalSnapshot())
    yield
    install_store(previous)


def test_a_bearer_the_daemon_cannot_check_gets_no_notice(svc, unloaded):
    """With the principal store unavailable the bearer may be a valid stored
    one: saying it was revoked would state a false cause."""
    text = _start(svc, f"Bearer {WRONG}")
    assert HEAD not in text
    assert text == _start(svc)
