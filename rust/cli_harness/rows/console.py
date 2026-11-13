"""CLI-CONSOLE-PRINT: ``pseudolife-mcp console``, every shape the oracle answers.

Canonical argv: ``console --help``, ``console [--no-open] [--to PAGE]
[--json] [--unless-signed-in]``, ``console --list [--json]``, ``console
--revoke SESSION_ID [--json]``, and argparse's and ``main``'s errors on those
options. The daemon is a fixture serving ``/api/console/link``,
``/api/console/sessions`` and ``/api/console/sessions/revoke`` in the shapes
``console_cli`` reads; both arms see identical bytes and the requests each
arm sends are compared.

No case lets either arm open a real browser: every case with a daemon that
reaches ``open_browser`` runs with BROWSER naming a recording stand-in in
the case's home and PATH holding only the home's ``bin`` (checked when the
row loads), so ``webbrowser.get()`` never falls back to ``os.startfile`` or
``xdg-open``; the stand-in's argv is compared. Scope and deferrals:
``rows/console.md``.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

from .. import core, normalize
from ..core import Case
from ..fixture_daemon import TLS_CA, factory, json_body, redirect, text
from ..mutants import Mutant

DEFERRAL = "pseudolife-stdio: mode 'console' is deferred in this candidate\n"
TOKEN = {"PSEUDOLIFE_MCP_TOKEN": "console-fixture-token"}
NO_TOKEN = {"PSEUDOLIFE_MCP_TOKEN": None, "PSEUDOLIFE_MCP_TOKEN_FILE": None}
# A fixed zone without daylight saving that glibc and the UCRT both read
# (UTC+10): the list's stamps are local time.
ZONE = {"TZ": "XYZ-10"}
CODE = "ABCD-1234"
FILE = "shim/src/cli/console/mod.rs"


def _native(value: str) -> bytes:
    return (value.replace("\n", "\r\n") if core.WINDOWS else value).encode()


# --- rules ---------------------------------------------------------------------

@normalize.rule("console-deferral")
def console_deferral(obs: dict) -> None:
    """A declared deferral case: the candidate must print the dispatcher's
    deferral line, exit 1, and send nothing. The oracle answers on its own
    path (against the closed port, so no link and no browser); its
    observation is replaced by that expectation."""
    if obs.get("arm") != "python":
        return
    obs["exit"] = 1
    obs["stdout"] = ""
    obs["stderr"] = base64.b64encode(_native(DEFERRAL)).decode()
    if "requests" in obs:
        obs["requests"] = []


@normalize.rule("console-deferral-after-read")
def console_deferral_after_read(obs: dict) -> None:
    """A declared deferral that comes after ``--unless-signed-in``'s session
    read and before the sign-in request: the candidate prints its deferral
    line (exit 1) having sent only that GET. The oracle's observation is
    replaced by that expectation, keeping its first request."""
    if obs.get("arm") != "python":
        return
    reads = [request for request in obs.get("requests", [])
             if request.get("target") == "/api/console/sessions"][:1]
    console_deferral(obs)
    obs["requests"] = reads


def _arm(arm, obs):
    obs["arm"] = arm.name


# --- fixture daemon --------------------------------------------------------------

def link(shape=None, status: int = 200):
    """``/api/console/link`` answering for the origin the request names;
    ``shape(origin)`` returns the fields to change."""
    def route(request):
        body = json.loads(request["body"])
        origin = body["origin"]
        page = f"&to={body['to']}" if body.get("to") else ""
        value = {"url": f"{origin}/ui/#/signin?code={CODE}{page}", "code": CODE,
                 "origin": origin, "principal": "fixture", "expires_in": 300}
        if shape is not None:
            value.update(shape(origin))
        return json_body(value, status)
    return route


SESSIONS = [
    {"session_id": "s-0123456789ab", "principal": "alice", "method": "link",
     "created_at": 1700000000, "last_used_at": 1700000039.6, "label": "Firefox on box"},
    {"session_id": None, "principal": 1.5, "created_at": True, "label": ""},
    "not a row",
    {"label": [1, "naïve"], "method": "passkey", "last_used_at": 1762000000},
    {"session_id": "s-ü", "principal": "bob", "created_at": None, "label": "Edge — work"},
]


def sessions(rows=None, operator=True, **extra):
    value = {"sessions": SESSIONS if rows is None else rows, "operator": operator, **extra}
    return {"/api/console/sessions": json_body(value)}


def revoked(flag):
    return {"/api/console/sessions/revoke": json_body({"revoked": flag})}


# --- the browser stand-in --------------------------------------------------------

# Every case that reaches ``open_browser`` (a sign-in without ``--no-open``)
# runs with BROWSER naming this stand-in in the case's own home and a PATH
# holding only the home's ``bin``: ``webbrowser.get()`` then returns a
# controller built from BROWSER, and no default launcher (``os.startfile``,
# ``xdg-open``) can run. The stand-in records its argv, then waits for the
# release file the ``during`` hook writes once the CLI under test has exited,
# so ``p.poll() is None`` holds in both arms (a stand-in that exits first
# would turn ``opened`` into ``printed`` by timing alone). ``_settle`` waits
# for every started stand-in to finish and leave before the home is
# snapshotted; only the argv records stay in the compared files.
STANDIN = """\
import json, os, sys, time
home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
state = os.path.join(home, ".standin")
os.makedirs(state, exist_ok=True)
n = 1
while True:
    try:
        fd = os.open(os.path.join(state, "started-%d" % n), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        break
    except FileExistsError:
        n += 1
os.write(fd, str(os.getpid()).encode())
os.close(fd)
launched = os.path.join(home, "launched")
os.makedirs(launched, exist_ok=True)
with open(os.path.join(launched, "launch-%d.json" % n), "w", encoding="utf-8") as out:
    json.dump(sys.argv[1:], out)
deadline = time.monotonic() + 30
while not os.path.exists(os.path.join(state, "release")) and time.monotonic() < deadline:
    time.sleep(0.02)
open(os.path.join(state, "done-%d" % n), "w").close()
"""
SEP = "\\" if core.WINDOWS else "/"
BIN = "{HOME}" + SEP + "bin"


def _interpreter() -> str:
    """The stand-in's interpreter: on Windows the windowless ``pythonw.exe``
    beside this harness's own (a console interpreter would open a window
    from the oracle, which runs without one)."""
    exe = Path(sys.executable)
    if core.WINDOWS and (exe.parent / "pythonw.exe").is_file():
        return str(exe.parent / "pythonw.exe")
    return str(exe)


def standin_browser(args: str = "%s") -> str:
    """A BROWSER value whose controller is a BackgroundBrowser running the
    stand-in (``-I``: the arm's PYTHON* variables do not reach it)."""
    return f"'{_interpreter()}' -I '{BIN}{SEP}stand-in.py' {args} &"


# Programs ``webbrowser`` may look up; check_programs proves before each arm
# that none resolves outside the home.
PROGRAMS = ("xdg-open", "xdg-settings", "gio", "gvfs-open", "x-www-browser", "kfmclient",
            "firefox", "iceweasel", "seamonkey", "mozilla", "netscape", "konqueror", "kfm",
            "galeon", "epiphany", "skipstone", "google-chrome", "chrome", "chromium",
            "chromium-browser", "opera", "mosaic", "grail", "www-browser", "links", "elinks",
            "lynx", "w3m", "osascript", "open")
# Variables that select a browser or a display: never inherited from the
# harness's own environment, so only a case can set them.
assert not {"BROWSER", "DISPLAY", "WAYLAND_DISPLAY", "TERM", "GNOME_DESKTOP_SESSION_ID",
            "KDE_FULL_SESSION"} & set(core._INHERIT)


def _standin_setup(arm):
    (arm.home / "bin").mkdir()
    (arm.home / "bin" / "stand-in.py").write_text(STANDIN, encoding="utf-8")


def _release(arm, proc):
    """Let the stand-ins go once the CLI under test has exited."""
    while proc.poll() is None:
        time.sleep(0.02)
    state = arm.home / ".standin"
    state.mkdir(exist_ok=True)
    (state / "release").write_bytes(b"")


def _gone(pid: int, deadline: float) -> bool:
    """Whether process ``pid`` has exited by ``deadline`` (read-only checks:
    a SYNCHRONIZE handle on Windows, signal 0 and /proc on POSIX)."""
    if core.WINDOWS:
        import ctypes  # noqa: PLC0415
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return True
        try:
            wait = max(0, int((deadline - time.monotonic()) * 1000))
            return kernel.WaitForSingleObject(handle, wait) == 0
        finally:
            kernel.CloseHandle(handle)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            if "\nState:\tZ" in Path(f"/proc/{pid}/status").read_text():
                return True
        except (ProcessLookupError, FileNotFoundError):
            return True
        time.sleep(0.02)
    return False


def _settle(arm, obs, launches=0):
    """Before the snapshot: wait for the stand-ins the case starts (the arm
    can exit before one has written its record), give any other a moment to
    show, then wait for each started one to finish and exit. The count is
    ``launches``, which a case declares when the CLI starts the stand-in and
    then says nothing about it (none does now: the oracle no longer dies
    after ``open_browser``), or one when the CLI reports it opened. An arm that starts fewer waits out
    the 30 seconds and then differs. A stand-in that never finished leaves
    ``launched/unfinished-N`` behind, which no arm matches."""
    _arm(arm, obs)
    state = arm.home / ".standin"
    stdout = base64.b64decode(obs["stdout"])
    opened = b"Opened the Console" in stdout or b'"state": "opened"' in stdout
    expected = max(launches, 1 if opened else 0)
    deadline = time.monotonic() + 30

    def started():
        return sorted(state.glob("started-*")) if state.exists() else []

    while len(started()) < expected and time.monotonic() < deadline:
        time.sleep(0.02)
    if core.WINDOWS:
        time.sleep(0.5)
    for marker in started():
        n = marker.name.split("-", 1)[1]
        done = state / f"done-{n}"
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        pid = int(marker.read_text() or 0)
        if not done.exists() or not pid or not _gone(pid, deadline):
            (arm.home / "launched").mkdir(exist_ok=True)
            (arm.home / "launched" / f"unfinished-{n}").write_bytes(b"")
    if state.exists():
        shutil.rmtree(state)


# --- cases -----------------------------------------------------------------------

def _reaches_browser(argv: list[str]) -> bool:
    """The oracle reaches ``open_browser`` only on the sign-in path (no
    ``--list``, no ``--revoke``: an empty one is a usage error) without
    ``--no-open``."""
    words = argv[1:]
    if "--list" in words or "--no-open" in words:
        return False
    return not any(word == "--revoke" or word.startswith("--revoke=") for word in words)


def _check_browser_env(case_id: str, env: dict, served: bool) -> None:
    """The environment that keeps every default launcher out, asserted when
    the row loads, before any arm runs: a BROWSER whose first entry decides
    (``os.startfile`` and the ``xdg-open`` family run only without one),
    naming as a program only the stand-in's interpreter or a path in the
    home, and a PATH of the home's ``bin`` alone (so no ``which`` lookup
    finds a real browser to clone)."""
    entries = [entry for entry in (env.get("BROWSER") or "").split(os.pathsep) if entry]
    if not entries or not entries[0].strip():
        raise AssertionError(f"{case_id}: BROWSER must decide the controller")
    if served and "%s" in entries[0]:
        try:
            words = shlex.split(entries[0])
        except ValueError:
            words = [BIN]  # webbrowser.get() raises: nothing starts
        if words[0] != _interpreter() and not words[0].startswith(BIN):
            raise AssertionError(f"{case_id}: BROWSER may run only the stand-in")
    if env.get("PATH") != BIN:
        raise AssertionError(f"{case_id}: PATH must hold only the home's bin")
    if env.get("WAYLAND_DISPLAY") is not None:
        raise AssertionError(f"{case_id}: WAYLAND_DISPLAY must stay unset")


def case(case_id: str, argv: list[str], *, routes=None, env=None, rules=(), setup=None,
         platforms=("windows", "linux"), timeout=40.0, tls=False,
         expect_output=False, during=None, after=_arm, programs=()) -> Case:
    full = ["console", *argv]
    env = {**TOKEN, **(env or {})}
    if "BROWSER" in env or (routes is not None and _reaches_browser(full)):
        _check_browser_env(case_id, env, routes is not None)
    return Case(case_id, full, env=env, rules=rules, setup=setup, during=during,
                after=after, platforms=platforms, timeout=timeout,
                expect_output=expect_output, programs=programs,
                daemon=factory(routes, tls) if routes is not None else None)


def browser_case(case_id: str, argv: list[str], *, browser: str | None = None,
                 display: bool = True, env=None, launches: int = 0, **kwargs) -> Case:
    """A case on the path that opens a browser, run with the stand-in's
    environment. ``display`` sets a DISPLAY nothing serves (Linux's
    ``has_display`` needs one); no case sets WAYLAND_DISPLAY. ``launches``
    is how many stand-ins ``_settle`` waits for when the CLI's output does
    not say one opened."""
    own = {"BROWSER": standin_browser() if browser is None else browser, "PATH": BIN}
    if display and not core.WINDOWS:
        own["DISPLAY"] = ":99"
    return case(case_id, argv, env={**own, **(env or {})}, setup=_standin_setup,
                during=_release, after=functools.partial(_settle, launches=launches),
                programs=PROGRAMS, **kwargs)


def deferred(case_id: str, argv: list[str], **kwargs) -> Case:
    # Never a daemon: the oracle answers these on its own path.
    return case(case_id, argv, rules=("console-deferral",), **kwargs)


def deferred_browser(case_id: str, argv: list[str], **kwargs) -> Case:
    # The closed port as well: the oracle makes no link, so opens nothing.
    return browser_case(case_id, argv, rules=("console-deferral",), **kwargs)


def _claude_registration(env: dict):
    def setup(arm):
        (arm.home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"pseudolife-memory": {"command": "pseudolife-mcp", "env": env}}}),
            encoding="utf-8")
    return setup


def _token_file(arm):
    from pseudolife_memory.credentials import _write_token_file  # noqa: PLC0415 (oracle writer)
    _write_token_file(arm.home / "console.token", "console-file-token")


def cases() -> list[Case]:
    c: list[Case] = []
    add = c.append

    # argparse: help and usage errors (COLUMNS=80, core's default).
    add(case("help", ["--help"]))
    add(case("help-short", ["-h"]))
    add(case("help-after-list", ["--list", "-h", "--to"]))
    add(case("help-after-extras", ["zz", "--help"]))
    add(case("to-missing-value", ["--no-open", "--to"]))
    add(case("revoke-missing-value", ["--revoke"]))
    add(case("json-explicit", ["--json=1"]))
    add(case("no-open-explicit-empty", ["--no-open="]))
    add(case("list-explicit", ["--list=yes"]))
    add(case("unrecognized", ["zz"]))
    add(case("unrecognized-several", ["a", "--list", "b c"]))
    add(case("extras-then-missing", ["zz", "--to"]))
    # main's own checks, before any credential is read.
    add(case("list-and-revoke", ["--list", "--revoke", "s-1"]))
    add(case("list-and-revoke-joined", ["--revoke=s-1", "--list", "--json"]))
    add(case("to-invalid", ["--no-open", "--to", "Bad"]))
    add(case("to-invalid-plain", ["--to", "Bad"]))
    add(case("to-empty", ["--no-open", "--to="]))
    add(case("to-too-long", ["--no-open", "--to", "a" * 33]))
    add(case("to-quote", ["--no-open", "--to", "it's"]))
    add(case("to-unicode", ["--list", "--to", "café\t"]))

    # Declared deferrals before any request.
    add(deferred("defer-abbreviation", ["--li"]))
    add(deferred("defer-dash-value", ["--to", "-x"]))
    add(deferred("defer-double-dash", ["--"]))
    add(deferred("defer-help-explicit", ["--help=x"]))
    add(deferred("defer-help-width", ["--help"], env={"COLUMNS": "100"}))
    # json.loads' integer digit limit moves with this variable.
    add(deferred("defer-int-digits", ["--list"], env={"PYTHONINTMAXSTRDIGITS": "640"}))

    # Credentials: none, unusable, or a URL that is no origin.
    add(case("no-token", ["--list"], env=NO_TOKEN))
    add(case("no-token-json", ["--no-open", "--json"], env=NO_TOKEN))
    add(case("token-invalid", ["--list"], env={"PSEUDOLIFE_MCP_TOKEN": "a b"}))
    add(case("token-file-empty-path", ["--list"], env={"PSEUDOLIFE_MCP_TOKEN_FILE": ""}))
    add(case("token-file-missing", ["--revoke", "s-1", "--json"],
             env={"PSEUDOLIFE_MCP_TOKEN_FILE": "{HOME}/missing.token"}))
    add(case("url-not-http", ["--list"], env={"PSEUDOLIFE_MCP_DAEMON_URL": "ftp://h"}))
    add(case("url-with-path", ["--no-open"],
             env={"PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:9/x"}))
    add(case("url-not-http-json", ["--list", "--json"],
             env={"PSEUDOLIFE_MCP_DAEMON_URL": "localhost:8765"}))
    # The closed loopback port: no daemon answered.
    add(case("unreachable-list", ["--list"]))
    add(case("unreachable-list-json", ["--list", "--json"]))
    add(case("unreachable-revoke", ["--revoke", "s-1"]))
    add(case("unreachable-no-open", ["--no-open", "--to", "board"]))

    # The sign-in link, printed.
    add(case("link", ["--no-open"], routes={"/api/console/link": link()}))
    add(case("link-json", ["--no-open", "--json"], routes={"/api/console/link": link()}))
    add(case("link-to-board", ["--no-open", "--to", "board", "--json"],
             routes={"/api/console/link": link()}))
    for name, value in (("fraction", 59.9), ("text", "300"), ("true", True), ("null", None),
                        ("zero", 0), ("negative", -600), ("hours", 7200)):
        add(case(f"link-expires-{name}", ["--no-open"],
                 routes={"/api/console/link": link(lambda _o, v=value: {"expires_in": v})}))
    add(case("link-principal-object", ["--no-open", "--json"],
             routes={"/api/console/link": link(lambda _o: {"principal": {"a": [1, None]}})}))
    add(case("link-to-board-text", ["--no-open", "--to", "board"],
             routes={"/api/console/link": link()}))
    # Old daemons answer without the page: still a link.
    add(case("link-to-board-no-page", ["--no-open", "--to", "board"],
             routes={"/api/console/link": link(lambda o: {
                 "url": f"{o}/ui/#/signin?code={CODE}"})}))
    # The code must be the link's.
    add(case("link-bad-unicode-code", ["--no-open"],
             routes={"/api/console/link": link(lambda _o: {"code": "näme ✓"})}))
    add(case("link-bad-other-code", ["--no-open", "--json"],
             routes={"/api/console/link": link(lambda _o: {"code": "ABCD-1235"})}))
    for name, shape in (("no-code", lambda _o: {"code": None}),
                        ("code-number", lambda _o: {"code": 5}),
                        ("other-origin", lambda o: {"url": "http://127.0.0.1:2/ui/#/signin?code=A"}),
                        ("control-url", lambda o: {"url": f"{o}/ui/#/signin?code=A B"}),
                        ("origin-not-http", lambda o: {"origin": "ftp://h"}),
                        ("no-url", lambda _o: {"url": None}),
                        # Nothing but the code may follow `code=`: a batch-file
                        # browser runs through cmd.exe.
                        ("ampersand", lambda o: {"url": f"{o}/ui/#/signin?code={CODE}&calc"}),
                        ("pipe", lambda o: {"url": f"{o}/ui/#/signin?code={CODE}|calc",
                                            "code": f"{CODE}|calc"}),
                        ("caret-quote", lambda o: {"url": f'{o}/ui/#/signin?code=A^"B',
                                                   "code": 'A^"B'}),
                        ("percent", lambda o: {"url": f"{o}/ui/#/signin?code=%PATH%",
                                               "code": "%PATH%"}),
                        ("lowercase", lambda o: {"url": f"{o}/ui/#/signin?code=abcd",
                                                 "code": "abcd"}),
                        ("unasked-page", lambda o: {
                            "url": f"{o}/ui/#/signin?code={CODE}&to=board"}),
                        ("origin-ampersand", lambda _o: {
                            "origin": "http://a&calc:8765",
                            "url": f"http://a&calc:8765/ui/#/signin?code={CODE}"}),
                        ("origin-zone", lambda _o: {
                            "origin": "http://[fe80::1%25eth0]:8765",
                            "url": f"http://[fe80::1%25eth0]:8765/ui/#/signin?code={CODE}"})):
        add(case(f"link-bad-{name}", ["--no-open"], routes={"/api/console/link": link(shape)}))
    add(case("link-bad-json", ["--no-open", "--json"],
             routes={"/api/console/link": link(lambda _o: {"origin": None})}))
    add(case("link-bad-page-json", ["--no-open", "--to", "board", "--json"],
             routes={"/api/console/link": link(lambda o: {
                 "url": f"{o}/ui/#/signin?code={CODE}&to=settings"})}))

    # Refusals.
    def refuse(case_id, argv, route, path="/api/console/link"):
        add(case(case_id, argv, routes={path: route}))
    refuse("refused-unauthorized", ["--no-open"], json_body({"error": "unauthorized"}, 401))
    refuse("refused-unauthorized-json", ["--no-open", "--json"],
           json_body({"error": "unauthorized", "hint": "ignored"}, 401))
    refuse("refused-principals", ["--list"], json_body({"error": "principals_unavailable"}, 503),
           "/api/console/sessions")
    refuse("refused-hinted", ["--no-open"],
           json_body({"error": "signin_unavailable", "hint": "open the Console over https"}, 403))
    refuse("refused-unhinted", ["--no-open"], json_body({"error": "session_cannot_issue"}, 403))
    refuse("refused-hint-number", ["--no-open", "--json"],
           json_body({"error": "operator_principal_required", "hint": 7}, 403))
    refuse("refused-unknown-code", ["--no-open"], json_body({"error": "zz", "hint": ""}, 409))
    refuse("refused-code-number", ["--revoke", "s-1"], json_body({"error": 1.0}, 500),
           "/api/console/sessions/revoke")
    refuse("refused-hint-other", ["--list", "--json"], json_body({"hint": "later"}, 429),
           "/api/console/sessions")
    add(case("refused-old-daemon", ["--no-open"], routes={}))
    add(case("refused-old-daemon-json", ["--list", "--json"], routes={}))
    refuse("refused-405", ["--revoke", "s-1"], json_body({"error": "zz"}, 405),
           "/api/console/sessions/revoke")
    refuse("refused-not-json", ["--no-open"], text("<html>busy</html>", 502))
    refuse("refused-200-not-json", ["--list"], text("ok"), "/api/console/sessions")
    refuse("refused-200-array", ["--no-open", "--json"], json_body([1, 2]))
    refuse("refused-201", ["--no-open"], json_body({"url": "x"}, 201))
    refuse("refused-redirect", ["--no-open", "--json"], redirect("/api/console/elsewhere"))
    refuse("refused-surrogate-hint", ["--no-open"],
           json_body({"error": "signin_unavailable", "hint": "\udc80 x"}, 403))
    # An `error` that is a list or object reads as an unknown code.
    refuse("refused-error-list", ["--no-open"], json_body({"error": ["x"]}, 403))
    refuse("refused-error-object", ["--list", "--json"], json_body({"error": {"a": 1}}, 403),
           "/api/console/sessions")
    refuse("refused-error-list-hint", ["--revoke", "s-1"],
           json_body({"error": [], "hint": "try later"}, 409), "/api/console/sessions/revoke")

    # The session list.
    add(case("list", ["--list"], env=ZONE, routes=sessions()))
    add(case("list-json", ["--list", "--json"], routes=sessions()))
    add(case("list-paired", ["--list"], env=ZONE, routes=sessions(operator=False)))
    add(case("list-empty", ["--list"], routes=sessions(rows=[])))
    add(case("list-not-a-list", ["--list"], routes=sessions(rows={"a": 1})))
    add(case("list-only-non-rows", ["--list"], routes=sessions(rows=["x", 1], operator=False)))
    add(case("list-empty-json", ["--list", "--json"],
             routes={"/api/console/sessions": json_body({"sessions": []})}))
    add(case("list-negative-stamp", ["--list"], env=ZONE,
             routes=sessions(rows=[{"created_at": -5}]), platforms=("linux",)))

    # Revoke.
    add(case("revoke", ["--revoke", "s-0123456789ab"], routes=revoked(True)))
    add(case("revoke-json", ["--revoke", "s-1", "--json"], routes=revoked(1)))
    add(case("revoke-not-found", ["--revoke", "s-1"], routes=revoked(False)))
    add(case("revoke-not-found-json", ["--revoke=s-1", "--json"], routes=revoked(None)))
    add(case("revoke-unicode-id", ["--revoke", "s-ü ✓"], routes=revoked(True)))
    # An empty id is a usage error, before any credential is read.
    add(case("revoke-empty", ["--revoke", ""], routes={"/api/console/link": link()}))
    add(case("revoke-empty-joined", ["--no-open", "--revoke=", "--json"],
             routes={"/api/console/link": link()}))
    add(case("revoke-empty-list", ["--list", "--revoke="], routes=sessions()))
    add(case("revoke-empty-bad-page", ["--revoke", "", "--to", "Bad"]))

    # Credentials from a token file and from a client registration.
    add(case("token-file", ["--list"], env={"PSEUDOLIFE_MCP_TOKEN_FILE": "{HOME}/console.token"},
             setup=_token_file, routes=sessions(rows=[])))
    add(case("registration-token", ["--list", "--json"], env=NO_TOKEN,
             setup=_claude_registration({"PSEUDOLIFE_MCP_TOKEN": "registered-token"}),
             routes=sessions(rows=[])))

    # https: the fixture serves the pg_tls test certificate; both arms trust
    # its CA through SSL_CERT_FILE (CPython's default verify paths,
    # rustls-native-certs). Untrusted, the oracle prints the TLS error text,
    # which is not reproduced: a declared deferral (the handshake fails
    # before any request).
    trusted = {"SSL_CERT_FILE": str(TLS_CA)}
    add(case("list-https", ["--list"], env={**ZONE, **trusted}, routes=sessions(), tls=True,
             expect_output=True))
    add(case("link-https", ["--no-open", "--json"], env=trusted, tls=True, expect_output=True,
             routes={"/api/console/link": link()}))
    add(case("defer-https-untrusted", ["--list"], routes=sessions(), tls=True,
             rules=("console-deferral",)))

    # An expiry int() refuses, and a code other than the link's (a lone
    # surrogate included), are the answer's refusals, in both modes.
    add(case("bad-expires-text", ["--no-open"],
             routes={"/api/console/link": link(lambda _o: {"expires_in": "abc"})}))
    add(case("bad-expires-object", ["--no-open"],
             routes={"/api/console/link": link(lambda _o: {"expires_in": {"a": 1}})}))
    add(case("bad-expires-nan", ["--no-open"],
             routes={"/api/console/link": link(lambda _o: {"expires_in": float("nan")})}))
    add(case("bad-expires-json", ["--no-open", "--json"],
             routes={"/api/console/link": link(lambda _o: {"expires_in": [1]})}))
    add(case("bad-expires-digits", ["--no-open"],
             routes={"/api/console/link": link(lambda _o: {"expires_in": "1" * 4301})}))
    add(case("bad-code-surrogate", ["--no-open"],
             routes={"/api/console/link": link(lambda _o: {"code": "\U0001f600\udc80\ud800"})}))
    add(case("link-expires-digits", ["--no-open"],
             routes={"/api/console/link": link(lambda _o: {"expires_in": "١_2٠٠ "})}))
    add(case("link-expires-huge", ["--no-open"],
             routes={"/api/console/link": link(lambda _o: {"expires_in": 1.5e308})}))
    add(case("bad-code-surrogate-json", ["--no-open", "--json"],
             routes={"/api/console/link": link(lambda _o: {"code": "\udc80"})}))

    c.extend(browser_cases())
    # A time localtime refuses, or a lone surrogate the list would print, is
    # the list's refusal, before any line.
    add(case("list-bad-stamp-nan", ["--list"],
             routes=sessions(rows=[{"created_at": float("nan")}])))
    add(case("list-bad-stamp-overflow", ["--list"],
             routes=sessions(rows=[{"last_used_at": 1e20}])))
    add(case("list-bad-stamp-huge-int", ["--list"],
             routes=sessions(rows=[{"created_at": 10 ** 30}])))
    # Inside time_t, past any year localtime holds (glibc's EOVERFLOW, the
    # CRT's EINVAL).
    add(case("list-bad-stamp-far", ["--list"],
             routes=sessions(rows=[{"created_at": 9 * 10 ** 18}])))
    add(case("list-bad-stamp-later-row", ["--list"], env=ZONE,
             routes=sessions(rows=[{"session_id": "s-1", "created_at": 1700000000},
                                   {"session_id": "s-2", "last_used_at": float("-inf")}])))
    add(case("list-bad-stamp-negative", ["--list"], routes=sessions(rows=[{"created_at": -5}]),
             platforms=("windows",)))
    add(case("list-bad-surrogate-label", ["--list"],
             routes=sessions(rows=[{"label": "\udc80"}])))
    add(case("list-bad-surrogate-later-row", ["--list"],
             routes=sessions(rows=[{"session_id": "s-1"}, {"principal": "x\ud800"}])))
    add(case("list-surrogate-json", ["--list", "--json"],
             routes=sessions(rows=[{"label": "\udc80", "created_at": float("nan")}])))
    return c


def browser_cases() -> list[Case]:
    """The sign-in on the path that opens a browser: each arm's stand-in
    argv (the URL), its text or JSON and its exit compared."""
    c: list[Case] = []
    add = c.append
    signin = {"/api/console/link": link()}

    def signed_in(rows, **extra):
        return {"/api/console/sessions": json_body({"sessions": rows}), **extra}

    script = f"'{BIN}{SEP}stand-in.py'"
    interpreter = f"'{_interpreter()}' -I {script}"
    exe = ".exe" if core.WINDOWS else ""

    # Opened: a BackgroundBrowser from BROWSER (`... %s &`).
    add(browser_case("open", [], routes=signin))
    add(browser_case("open-json", ["--json"], routes=signin))
    add(browser_case("open-to-board", ["--to", "board", "--json"], routes=signin))
    add(browser_case("open-args", [], routes=signin,
                     browser=standin_browser("--new-tab --url=%s")))
    add(browser_case("open-quoted-ampersand", ["--json"], routes=signin,
                     browser=f"{interpreter} %s '&'"))
    # The first non-empty entry decides; the others are only registered.
    add(browser_case("open-first-entry", ["--json"], routes=signin,
                     browser=os.pathsep + standin_browser() + os.pathsep
                     + f"'{BIN}{SEP}other{exe}' %s &"))
    add(browser_case("open-hours", [], routes={"/api/console/link": link(
        lambda _o: {"expires_in": 7200})}))
    add(browser_case("open-to-board-text", ["--to", "board"], routes=signin))
    # open_browser returns False: the link is printed.
    add(browser_case("open-generic", [], routes=signin, browser=f"{interpreter} %s"))
    add(browser_case("open-generic-name", ["--json"], routes=signin,
                     browser=f"{BIN}{SEP}my-browser --flag"))
    add(browser_case("open-missing-program", [], routes=signin,
                     browser=f"'{BIN}{SEP}missing{exe}' %s &"))
    add(browser_case("open-shlex-error", ["--json"], routes=signin,
                     browser=f"{interpreter} %s & '"))
    add(browser_case("open-whitespace-entry", [], routes=signin,
                     browser=standin_browser() + os.pathsep + " "))
    add(browser_case("open-no-display", ["--json"], routes=signin, display=False,
                     platforms=("linux",)))
    add(browser_case("open-no-display-text", [], routes=signin, display=False,
                     platforms=("linux",)))
    # An answer that is no safe link, or no answer, starts nothing.
    add(browser_case("open-unsafe-link", [], routes={"/api/console/link": link(
        lambda _o: {"url": "http://127.0.0.1:2/ui/#/signin?code=A"})}))
    add(browser_case("open-file-link", ["--json"], routes={"/api/console/link": link(
        lambda o: {"url": "file:///etc/passwd", "origin": o})}))
    add(browser_case("open-refused", [], routes={"/api/console/link": json_body(
        {"error": "unauthorized"}, 401)}))
    add(browser_case("open-unreachable", ["--to", "board"]))
    # A malformed answer is refused before the browser starts.
    add(browser_case("open-bad-expires", [], routes={"/api/console/link": link(
        lambda _o: {"expires_in": "5 minutes"})}))
    add(browser_case("open-bad-code-surrogate", [], routes={"/api/console/link": link(
        lambda _o: {"code": "a\udc80"})}))
    add(browser_case("open-bad-unicode-code", ["--json"], routes={"/api/console/link": link(
        lambda _o: {"code": "näme ✓"})}))
    # Shell syntax after the code (with the code to match) starts nothing.
    add(browser_case("open-shell-pipe", [], routes={"/api/console/link": link(
        lambda o: {"url": f"{o}/ui/#/signin?code={CODE}|calc", "code": f"{CODE}|calc"})}))
    add(browser_case("open-shell-ampersand", ["--json"], routes={"/api/console/link": link(
        lambda o: {"url": f"{o}/ui/#/signin?code={CODE}&echo.>x"})}))

    # --unless-signed-in: the session list first (a read), then the sign-in.
    add(browser_case("unless-signed-in", ["--unless-signed-in"],
                     routes=signed_in([{"session_id": "s-1"}], **signin)))
    add(browser_case("unless-signed-in-json", ["--unless-signed-in", "--json"],
                     routes=signed_in([0], **signin)))
    add(browser_case("unless-none", ["--unless-signed-in"], routes=signed_in([], **signin)))
    add(browser_case("unless-none-json", ["--unless-signed-in", "--json"],
                     routes=signed_in({"a": 1}, **signin)))
    add(browser_case("unless-refused", ["--unless-signed-in"], routes={
        "/api/console/sessions": json_body({"error": "unauthorized"}, 401), **signin}))
    add(browser_case("unless-not-json", ["--unless-signed-in", "--json"], routes={
        "/api/console/sessions": text("busy"), **signin}))
    add(browser_case("unless-old-daemon", ["--unless-signed-in"], routes=signin))
    add(browser_case("unless-unreachable", ["--unless-signed-in", "--json"]))
    add(case("unless-no-open", ["--no-open", "--unless-signed-in"],
             routes=signed_in([], **signin)))
    add(case("unless-no-open-signed-in", ["--no-open", "--unless-signed-in", "--json"],
             routes=signed_in([1], **signin)))

    # Deferred before any request: a launcher this port does not start.
    add(deferred_browser("defer-browser-standard-name", [], browser="firefox"))
    add(deferred_browser("defer-browser-clone", ["--json"], browser=f"{BIN}{SEP}Lynx -dump"))
    # After the session read (a read), before the sign-in request.
    add(browser_case("defer-browser-unless", ["--unless-signed-in"], browser="w3m",
                     routes=signed_in([], **signin), rules=("console-deferral-after-read",)))
    add(deferred_browser("defer-browser-bare-name", [], browser="pythonw.exe -I x %s &",
                         platforms=("windows",)))
    add(deferred_browser("defer-browser-batch", [], browser=f"'{BIN}{SEP}b.cmd' %s &",
                         platforms=("windows",)))
    return c


# No mutant here may route the candidate to a default launcher
# (os.startfile's ShellExecuteW, xdg-open): each breaks a step after
# BROWSER has chosen the stand-in, or before any launch.
BROWSER_FILE = "shim/src/cli/console/browser.rs"
LAUNCH_FILE = "shim/src/cli/console/launch.rs"
MUTANTS = [
    Mutant("console-exited-counts-as-opened", "console", LAUNCH_FILE,
           "    matches!(child.try_wait(), Ok(None))",
           "    matches!(child.try_wait(), Ok(Some(_)))", ("open",)),
    Mutant("console-unless-skips-the-list", "console", FILE,
           "            if *unless {", "            if false && *unless {",
           ("unless-signed-in",)),
    Mutant("console-unsafe-link-launched", "console", FILE,
           "let safe = matches!(code, Some(J::Str(_) | J::Wtf(_))) && link_is_safe(url, origin, to);",
           "let safe = matches!(code, Some(J::Str(_) | J::Wtf(_)));", ("open-unsafe-link",)),
    Mutant("console-generic-launched", "console", BROWSER_FILE,
           'if words.last().map(String::as_str) != Some("&") {',
           'if words.last().map(String::as_str) == Some("") {', ("open-generic",)),
    Mutant("console-expiry-refusal-swallowed", "console", FILE,
           "    let Ok(minutes) = pyint::minutes(expires_in) else {",
           '    let Ok(minutes) = pyint::minutes(expires_in).or(Ok::<_, ()>("5".to_owned())) else {',
           ("open-bad-expires",)),
    Mutant("console-code-any-character", "console", FILE,
           "        && code.chars().all(|c| CODE_CHARS.contains(c))",
           "        && code.chars().all(|c| c != ' ')", ("open-shell-pipe",)),
    Mutant("console-code-unchecked", "console", FILE,
           "    if code.as_str() != Some(shown) {", "    if code.as_str().is_none() {",
           ("link-bad-other-code",)),
    Mutant("console-localtime-refusal-deferred", "console", FILE,
           "        stamp::Minute::Refused => Ok(None),",
           "        stamp::Minute::Refused => Err(Defer),", ("list-bad-stamp-far",)),
    # Only Linux gates the browser on a display.
    Mutant("console-display-ignored", "console", BROWSER_FILE,
           "    platform != Platform::Posix || display(host)", "    true",
           ("open-no-display",), platforms=("linux",)),
    Mutant("console-405-not-old", "console", FILE, "if matches!(status, 404 | 405) {",
           "if matches!(status, 404) {", ("refused-405",)),
    Mutant("console-get-without-content-type", "console", "shim/src/cli/hook_http.rs",
           "    if content_type {\n", "    if body.is_some() {\n", ("list-json",)),
    Mutant("console-stamp-rounded", "console", FILE, "let floor = value.floor();",
           "let floor = value.round();", ("list",)),
    Mutant("console-paired-note-always", "console", FILE,
           "if raw(payload, \"operator\") == Some(&J::Bool(false)) {",
           "if raw(payload, \"operator\").is_some() {", ("list",)),
    Mutant("console-null-cell-empty", "console", FILE,
           "let cell = |key: &str| raw(row, key).map(J::py_str_units).unwrap_or_default();",
           "let cell = |key: &str| pyjson::get(row, key).map(J::py_str_units).unwrap_or_default();",
           ("list",)),
    Mutant("console-hint-over-message", "console", FILE,
           "            Some(message) => J::from(*message),",
           "            Some(message) => truthy(hint).unwrap_or_else(|| J::from(*message)),",
           ("refused-unauthorized-json",)),
    # Only Windows' message text ends in a full stop.
    Mutant("console-winerror-full-stop", "console", FILE,
           "message.trim_end_matches(|c: char| c <= ' ' || c == '.')",
           "message.trim_end_matches(|c: char| c <= ' ')", ("unreachable-list",),
           platforms=("windows",)),
]
