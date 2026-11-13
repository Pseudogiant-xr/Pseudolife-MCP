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


def test_unless_signed_in_does_nothing_when_a_browser_is_signed_in(daemon, capsys):
    daemon["answer"] = (200, {"operator": True, "sessions": [{"session_id": "s1"}]})
    assert console_cli.main(["--unless-signed-in"], environ=ENV) == 0
    assert [c[1] for c in daemon["calls"]] == ["/api/console/sessions"]
    assert daemon["opened"] == [] and "already signed in" in capsys.readouterr().out


@pytest.mark.parametrize("listed", [(200, {"sessions": []}), (404, {"error": "not_found"})])
def test_unless_signed_in_signs_in_when_none_is_or_it_cannot_tell(daemon, monkeypatch, listed):
    answers = iter([listed, (200, LINK)])
    monkeypatch.setattr(console_cli, "request",
                        lambda url, path, token, *, body=None: daemon["calls"].append(path)
                        or next(answers))
    assert console_cli.main(["--unless-signed-in"], environ=ENV) == 0
    assert daemon["calls"] == ["/api/console/sessions", "/api/console/link"]
    assert daemon["opened"] == [LINK["url"]]


def test_a_terminal_browser_is_never_used(monkeypatch):
    import webbrowser
    opened = []

    class Lynx(webbrowser.GenericBrowser):
        def open(self, url, new=0, autoraise=True):
            opened.append(url)
            return True

    monkeypatch.setattr(webbrowser, "get", lambda using=None: Lynx("lynx"))
    assert console_cli.open_browser("http://127.0.0.1:8765/ui/") is False and opened == []

    class Firefox(webbrowser.BackgroundBrowser):
        def open(self, url, new=0, autoraise=True):
            opened.append(url)
            return True

    monkeypatch.setattr(webbrowser, "get", lambda using=None: Firefox("firefox"))
    assert console_cli.open_browser("http://127.0.0.1:8765/ui/") is True and opened


def test_elinks_is_never_used(monkeypatch):
    """Elinks is a ``UnixBrowser``, not a ``GenericBrowser``, and runs in the
    terminal when no Elinks instance takes the remote call."""
    import webbrowser
    opened = []

    class Elinks(webbrowser.Elinks):
        def open(self, url, new=0, autoraise=True):
            opened.append(url)
            return True

    monkeypatch.setattr(webbrowser, "get", lambda using=None: Elinks("elinks"))
    assert console_cli.open_browser("http://127.0.0.1:8765/ui/") is False and opened == []


_REAL_OPEN = console_cli.open_browser


# -- refusals of malformed arguments and answers ----------------------------------

def test_an_empty_revoke_is_a_usage_error(daemon, capsys):
    for argv in (["--revoke", ""], ["--revoke="], ["--list", "--revoke", ""],
                 ["--no-open", "--revoke", "", "--json"]):
        assert console_cli.main(argv, environ=ENV) == 2
        assert capsys.readouterr().err == (
            "--revoke takes a session id from --list, not an empty one\n")
    assert daemon["calls"] == [] and daemon["opened"] == []


@pytest.mark.parametrize("error", [["x"], {"a": 1}])
def test_an_error_that_is_a_list_or_object_is_a_refusal(daemon, capsys, error):
    daemon["answer"] = (403, {"error": error})
    assert console_cli.main(["--no-open"], environ=ENV) == 4
    assert capsys.readouterr().err == (
        f"pseudolife-mcp console: the daemon answered HTTP 403 ({error})\n")


def test_an_unhashable_error_with_a_hint_shows_the_hint(daemon, capsys):
    daemon["answer"] = (403, {"error": [], "hint": "try later"})
    assert console_cli.main(["--no-open", "--json"], environ=ENV) == 4
    assert json.loads(capsys.readouterr().out) == {
        "state": "refused", "status": 403, "error": [], "detail": "try later"}


BAD_EXPIRY = "the daemon's answer had no usable expiry time (expires_in)"


@pytest.mark.parametrize("value", ["abc", "5 minutes", {"a": 1}, [1], float("nan"),
                                   float("inf"), "1" * 4301])
def test_an_expiry_int_refuses_is_a_refusal_before_the_browser_opens(daemon, capsys, value):
    daemon["answer"] = (200, {**LINK, "expires_in": value})
    assert console_cli.main([], environ=ENV) == 4
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == f"pseudolife-mcp console: {BAD_EXPIRY}\n"
    assert console_cli.main(["--json"], environ=ENV) == 4
    assert json.loads(capsys.readouterr().out) == {
        "state": "refused", "error": "bad_answer", "detail": BAD_EXPIRY}
    assert daemon["opened"] == []


@pytest.mark.parametrize("value", [59.9, "300", True, None, 0, -600, 1.5e308, " ١_2٠٠ "])
def test_an_expiry_int_accepts_still_signs_in(daemon, value):
    daemon["answer"] = (200, {**LINK, "expires_in": value})
    assert console_cli.main([], environ=ENV) == 0
    assert daemon["opened"] == [LINK["url"]]


MISMATCH = "the daemon's sign-in code does not match its link"


@pytest.mark.parametrize("code", ["a\udc80", "\udc80", "ABCD-EFGH-JKMX", "\x1b[2J", "näme"])
def test_a_code_other_than_the_links_is_a_refusal(daemon, capsys, code):
    daemon["answer"] = (200, {**LINK, "code": code})
    assert console_cli.main([], environ=ENV) == 4
    captured = capsys.readouterr()
    assert daemon["opened"] == [] and captured.out == ""
    assert captured.err == f"pseudolife-mcp console: {MISMATCH}\n"
    assert console_cli.main(["--json", "--no-open"], environ=ENV) == 4
    assert json.loads(capsys.readouterr().out) == {
        "state": "refused", "error": "bad_answer", "detail": MISMATCH}


def test_a_link_with_no_code_still_names_the_link(daemon, capsys):
    daemon["answer"] = (200, {**LINK, "code": None})
    assert console_cli.main(["--json"], environ=ENV) == 4
    assert json.loads(capsys.readouterr().out) == {
        "state": "refused", "error": "bad_answer",
        "detail": "the daemon's answer had no sign-in link"}


BAD_STAMPS = [float("nan"), float("inf"), float("-inf"), 1e20, 10 ** 30, -(10 ** 30), 2 ** 63]


@pytest.mark.parametrize("field", ["created_at", "last_used_at"])
@pytest.mark.parametrize("stamp", BAD_STAMPS)
def test_a_stamp_this_machine_cannot_show_is_a_refusal(daemon, capsys, field, stamp):
    daemon["answer"] = (200, {"operator": True, "sessions": [
        {"session_id": "s-1", "created_at": 1.0}, {"session_id": "s-2", field: stamp}]})
    assert console_cli.main(["--list"], environ=ENV) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("pseudolife-mcp console: the daemon's session list had a time "
                            f"this machine cannot show ({field})\n")


def test_a_negative_stamp_is_refused_only_where_localtime_refuses_it(daemon, capsys):
    daemon["answer"] = (200, {"operator": True, "sessions": [{"created_at": -5}]})
    code = console_cli.main(["--list"], environ=ENV)
    captured = capsys.readouterr()
    if console_cli.sys.platform == "win32":
        assert code == 4 and "cannot show (created_at)" in captured.err
    else:
        assert code == 0 and captured.err == ""


@pytest.mark.parametrize("row", [{"label": "\udc80"}, {"session_id": "a\udc80b"},
                                 {"principal": "x\ud800"}, {"method": "\udfff"}])
def test_a_lone_surrogate_in_the_list_is_a_refusal(daemon, capsys, row):
    daemon["answer"] = (200, {"operator": True, "sessions": [{"session_id": "s-1"}, row]})
    assert console_cli.main(["--list"], environ=ENV) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("pseudolife-mcp console: the daemon's session list had text "
                            "that cannot be printed (a lone surrogate)\n")


def test_a_lone_surrogate_in_the_json_list_still_prints(daemon, capsys):
    daemon["answer"] = (200, {"operator": True, "sessions": [{"label": "\udc80"}]})
    assert console_cli.main(["--list", "--json"], environ=ENV) == 0
    assert json.loads(capsys.readouterr().out)["sessions"] == [{"label": "\udc80"}]


def test_an_answer_nested_past_the_parsers_limit_is_no_json(monkeypatch):
    import io

    class Reply(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Opener:
        def open(self, req, timeout):
            return Reply(b"[" * 100000 + b"]" * 100000)

    monkeypatch.setattr(console_cli, "_opener", lambda: Opener())
    assert console_cli.request("http://127.0.0.1:1", "/api/console/sessions", TOKEN) == (
        200, None)


def test_a_reply_that_is_not_http_is_a_refusal(daemon, capsys):
    import http.client
    daemon["answer"] = http.client.BadStatusLine("garbage")
    assert console_cli.main(["--list"], environ=ENV) == 4
    assert capsys.readouterr().err == (
        "pseudolife-mcp console: the daemon's reply at http://127.0.0.1:8765 was not "
        "readable HTTP (BadStatusLine)\n")
    daemon["answer"] = http.client.IncompleteRead(b"x")
    assert console_cli.main(["--list", "--json"], environ=ENV) == 4
    assert json.loads(capsys.readouterr().out) == {
        "state": "refused", "error": "bad_reply",
        "detail": "the daemon's reply at http://127.0.0.1:8765 was not readable HTTP "
                  "(IncompleteRead)"}


# -- the link handed to the browser ------------------------------------------------

@pytest.mark.parametrize("tail", [
    "ABCD&calc", "ABCD|calc", "ABCD^calc", 'ABCD"calc', "ABCD%PATH%", "ABCD>out", "ABCD<in",
    "ABCD(x)", "ABCD!x!", "abcd", "ABCI", "ABCL", "ABCO", "ABCU", "", "ABCD&to=Board",
    "ABCD&to=board&x", "ABCD&to=", "ABCD٠", "ABCD#x", "ABCD?x", "ABCD/x",
])
def test_a_link_holding_more_than_a_code_is_refused(tail):
    origin = "http://127.0.0.1:8765"
    assert not console_cli.link_is_safe(f"{origin}/ui/#/signin?code={tail}", origin, "board")


@pytest.mark.parametrize("origin", [
    "http://a&calc:8765", "http://a|b", "http://a^b", 'http://a"b', "http://a%25b",
    "http://[fe80::1%25eth0]:8765", "http://é.example", "http://a!b", "http://a$b",
    "http://a'b", "http://a(b)", "http://a*b", "http://a,b", "http://a;b", "http://a=b",
    "http://a<b", "http://a>b", "http://a`b", "http://a{b}",
])
def test_an_origin_holding_a_shell_character_is_refused(origin):
    normalized = console_cli.normalize_origin(origin)
    assert normalized is not None, origin  # normalize_origin alone lets it through
    assert not console_cli.link_is_safe(f"{normalized}/ui/#/signin?code=ABCD", origin)


@pytest.mark.parametrize("origin,to", [
    ("http://127.0.0.1:8765", None), ("http://localhost:8765", None),
    ("http://[::1]:8765", None), ("https://console.example.com", None),
    ("HTTPS://Console.Example.COM:443/", None), ("https://my_host.example:8443", "board"),
    ("http://127.0.0.1:8765", "a-b-1"), ("http://[::ffff:127.0.0.1]:8765", None),
])
def test_a_real_link_is_safe(origin, to):
    stored = console_cli.normalize_origin(origin)
    suffix = f"&to={to}" if to else ""
    for code in ("0123-4567-89AB", "CDEF-GHJK-MNPQ", "RSTV-WXYZ-0000"):
        assert console_cli.link_is_safe(f"{stored}/ui/#/signin?code={code}{suffix}", origin, to)


def test_a_page_in_the_link_must_be_the_one_asked_for():
    origin = "http://127.0.0.1:8765"
    url = f"{origin}/ui/#/signin?code=ABCD&to=board"
    assert console_cli.link_is_safe(url, origin, "board")
    assert not console_cli.link_is_safe(url, origin, None)
    assert not console_cli.link_is_safe(url, origin, "settings")
    # An older daemon that ignores the page answers without it.
    assert console_cli.link_is_safe(f"{origin}/ui/#/signin?code=ABCD", origin, "board")


def test_the_code_alphabet_is_the_daemons():
    from pseudolife_memory.principals import PAIRING_CODE_ALPHABET
    assert console_cli._CODE_CHARS == PAIRING_CODE_ALPHABET + "-"


def test_to_board_is_signed_in_with_the_daemons_link(daemon):
    daemon["answer"] = (200, {**LINK, "url": LINK["url"] + "&to=board"})
    assert console_cli.main(["--to", "board"], environ=ENV) == 0
    assert daemon["opened"] == [LINK["url"] + "&to=board"]


def test_a_page_the_command_did_not_ask_for_is_refused(daemon, capsys):
    daemon["answer"] = (200, {**LINK, "url": LINK["url"] + "&to=board"})
    assert console_cli.main([], environ=ENV) == 4
    assert daemon["opened"] == []
    assert "no sign-in link" in capsys.readouterr().err


@pytest.mark.skipif(console_cli.sys.platform != "win32", reason="cmd.exe runs a batch file")
def test_a_batch_file_browser_never_sees_a_shell_character(daemon, monkeypatch, tmp_path):
    """A BROWSER naming a .cmd file starts it through cmd.exe, which reads
    ``&`` in the URL as a second command. The fake daemon's answer holds
    one; the real ``open_browser`` runs, against a stand-in batch file."""
    import time
    import webbrowser
    standin = tmp_path / "browser.cmd"
    standin.write_text('@echo off\r\necho %*>"%~dp0argv.txt"\r\n', encoding="ascii")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BROWSER", f"'{standin}' %s &")
    monkeypatch.setattr(webbrowser, "_tryorder", None)
    monkeypatch.setattr(webbrowser, "_browsers", {})
    controller = webbrowser.get()
    # Nothing but the stand-in can start.
    assert type(controller) is webbrowser.BackgroundBrowser
    assert controller.name == str(standin) and controller.args == ["%s"]
    monkeypatch.setattr(console_cli, "open_browser", _REAL_OPEN)
    daemon["answer"] = (200, {**LINK, "url": LINK["url"] + "&echo.>injected"})
    assert console_cli.main([], environ=ENV) == 4
    time.sleep(1.0)
    assert not (tmp_path / "injected").exists() and not (tmp_path / "argv.txt").exists()
