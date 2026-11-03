"""``pseudolife-mcp console`` (spec 2026-11-02, section 4): it finds this
machine's daemon and token as every client command does, asks for a
sign-in link, opens it (or only prints it), and lists or revokes signed-in
browsers. The daemon is replaced by a fake ``request``."""
from __future__ import annotations

import json

import pytest

from pseudolife_memory import console_cli, doctor_cli

TOKEN = "fixture-cli-token"
LINK = {"code": "ABCD-EFGH-JKMN", "url": "http://127.0.0.1:8765/ui/#/signin?code=ABCD-EFGH-JKMN",
        "origin": "http://127.0.0.1:8765", "principal": "default", "expires_in": 300,
        "expires_at": 1.0}


@pytest.fixture
def daemon(monkeypatch):
    state = {"calls": [], "answer": (200, LINK), "opened": []}

    def fake(url, path, token, *, body=None):
        state["calls"].append((url, path, token, body))
        answer = state["answer"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(console_cli, "request", fake)
    monkeypatch.setattr(console_cli, "open_browser",
                        lambda url: state["opened"].append(url) or True)
    monkeypatch.setattr(doctor_cli, "registration_credentials", lambda env: ({}, None))
    return state


ENV = {"PSEUDOLIFE_MCP_TOKEN": TOKEN, "DISPLAY": ":0"}


def test_it_asks_for_a_link_for_the_address_it_reached_and_opens_it(daemon, capsys):
    env = {**ENV, "PSEUDOLIFE_MCP_DAEMON_URL": "HTTP://LOCALHOST:8765/"}
    assert console_cli.main([], environ=env) == 0
    assert daemon["calls"] == [("http://localhost:8765", "/api/console/link", TOKEN,
                                {"origin": "http://localhost:8765"})]
    assert daemon["opened"] == [LINK["url"]]
    out = capsys.readouterr().out
    assert "confirm the sign-in" in out and LINK["code"] in out and TOKEN not in out


def test_no_open_only_prints(daemon, capsys):
    assert console_cli.main(["--no-open"], environ=ENV) == 0
    assert daemon["opened"] == []
    assert LINK["url"] in capsys.readouterr().out


def test_without_a_display_it_prints_instead(daemon, capsys, monkeypatch):
    monkeypatch.setattr(console_cli.sys, "platform", "linux")
    assert console_cli.main([], environ={"PSEUDOLIFE_MCP_TOKEN": TOKEN}) == 0
    assert daemon["opened"] == []
    assert "Open this link" in capsys.readouterr().out


def test_to_lands_on_a_page(daemon):
    assert console_cli.main(["--to", "board", "--no-open"], environ=ENV) == 0
    assert daemon["calls"][0][3] == {"origin": "http://127.0.0.1:8765", "to": "board"}


def test_a_bad_page_is_a_usage_error(daemon):
    assert console_cli.main(["--to", "../x"], environ=ENV) == 2
    assert daemon["calls"] == []


def test_json_reports_the_link(daemon, capsys):
    assert console_cli.main(["--json", "--no-open"], environ=ENV) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "printed" and out["url"] == LINK["url"]


def test_no_token_names_the_fix(daemon, capsys):
    assert console_cli.main([], environ={}) == 3
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" in capsys.readouterr().err
    assert daemon["calls"] == []


def test_a_token_from_a_client_registration_is_used(daemon, monkeypatch):
    monkeypatch.setattr(doctor_cli, "registration_credentials", lambda env: (
        {"PSEUDOLIFE_MCP_TOKEN": "from-registration",
         "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:9000"}, "Claude Code registration"))
    assert console_cli.main(["--no-open"], environ={}) == 0
    assert daemon["calls"][0][:3] == ("http://127.0.0.1:9000", "/api/console/link",
                                      "from-registration")


def test_an_older_daemon_is_named(daemon, capsys):
    daemon["answer"] = (404, {"error": "not_found"})
    assert console_cli.main([], environ=ENV) == 4
    assert "pseudolife-mcp update" in capsys.readouterr().err


def test_the_daemons_hint_is_shown(daemon, capsys):
    daemon["answer"] = (409, {"error": "signin_origin_unsupported",
                              "hint": "run it on the daemon's host"})
    assert console_cli.main([], environ=ENV) == 4
    assert "run it on the daemon's host" in capsys.readouterr().err


def test_no_daemon_names_doctor(daemon, capsys):
    daemon["answer"] = OSError("connection refused")
    assert console_cli.main([], environ=ENV) == 3
    assert "pseudolife-mcp doctor" in capsys.readouterr().err


def test_list_and_revoke(daemon, capsys):
    daemon["answer"] = (200, {"operator": False, "sessions": [
        {"session_id": "abc123", "principal": "laptop", "method": "link", "label": "Firefox",
         "created_at": 1.0, "last_used_at": None, "expires_at": 2.0}]})
    assert console_cli.main(["--list"], environ=ENV) == 0
    out = capsys.readouterr().out
    assert "abc123" in out and "laptop" in out and "paired" in out
    daemon["answer"] = (200, {"revoked": True})
    assert console_cli.main(["--revoke", "abc123"], environ=ENV) == 0
    assert daemon["calls"][-1][1:] == ("/api/console/sessions/revoke", TOKEN,
                                       {"session_id": "abc123"})
    daemon["answer"] = (200, {"revoked": False})
    assert console_cli.main(["--revoke", "nope"], environ=ENV) == 4


def test_the_mode_is_dispatched():
    import inspect

    from pseudolife_memory import cli
    assert 'mode == "console"' in inspect.getsource(cli.main)
    assert "\n  console " in cli._USAGE


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://127.0.0.1:8765/ui/#/settings",
    "http://127.0.0.1:8765.evil.example/ui/#/signin?code=X", "javascript:alert(1)",
    "http://127.0.0.1:8765/ui/#/signin?code=X Y", None,
])
def test_only_a_sign_in_link_at_the_daemons_origin_is_opened(daemon, url):
    daemon["answer"] = (200, {**LINK, "url": url})
    assert console_cli.main([], environ=ENV) == 4
    assert daemon["opened"] == []


def test_the_daemons_bank_problem_is_named(daemon, capsys):
    daemon["answer"] = (503, {"error": "signin_unavailable", "hint": "could not reach its bank"})
    assert console_cli.main([], environ=ENV) == 4
    assert "could not reach its bank" in capsys.readouterr().err
