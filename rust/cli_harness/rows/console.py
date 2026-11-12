"""CLI-CONSOLE-PRINT: the shapes of ``pseudolife-mcp console`` that open no browser.

Canonical argv: ``console --help``, ``console --no-open [--to PAGE]
[--json]``, ``console --list [--json]``, ``console --revoke SESSION_ID
[--json]``, and argparse's and ``main``'s errors on those options. The
daemon is a fixture serving ``/api/console/link``, ``/api/console/sessions``
and ``/api/console/sessions/revoke`` in the shapes ``console_cli`` reads;
both arms see identical bytes and the requests each arm sends are compared.

No case lets the oracle open a browser: every case with a daemon passes
``--no-open``, ``--list`` or a non-empty ``--revoke`` (checked when the
row loads), and the deferral cases, which the oracle answers on its own
path, run against the closed loopback port, where no sign-in link can come
back. Scope and deferrals: ``rows/console.md``.
"""

from __future__ import annotations

import base64
import json
import re

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


_TRACEBACK = re.compile(rb"\ATraceback \(most recent call last\):\r?\n.*\n(?P<last>[A-Za-z]+"
                        rb"Error: [^\r\n]*)\r?\n\Z", re.DOTALL)
HEADER = "ID                SIGNED IN AS         HOW      SINCE             LAST USED         BROWSER\n"
DECLARED_CRASHES = {
    # `code in _REFUSALS` with a list or dict code.
    "console-unhashable-traceback": (rb"TypeError: unhashable type: ", ""),
    # `int(payload.get("expires_in") or 300)` on text that is no integer.
    "console-expires-traceback": (rb"ValueError: invalid literal for int() with base 10: ", ""),
    # `time.localtime` on NaN, after the list's header line.
    "console-stamp-nan-traceback": (rb"ValueError: Invalid value NaN (not a number)", HEADER),
    # `time.localtime` past time_t, after the header.
    "console-stamp-overflow-traceback": (
        rb"OverflowError: timestamp out of range for platform time_t", HEADER),
    # Windows' CRT refuses a negative time, after the header.
    "console-stamp-negative-traceback": (rb"OSError: [Errno 22] Invalid argument", HEADER),
    # A lone surrogate printed to strict UTF-8 stdout, after the header.
    "console-surrogate-traceback": (rb"UnicodeEncodeError: 'utf-8' codec can't encode", HEADER),
}


def _declared_crash(name: str, marker: bytes, stdout: str):
    def rule(obs: dict) -> None:
        """Declared: the oracle dies with this uncaught exception (exit 1, a
        traceback naming its own source files, after ``stdout`` if any);
        the candidate prints its deferral line, exit 1, after the same
        request. Only that traceback with exit 1 maps; requests stay
        compared."""
        if obs.get("arm") != "python" or obs["exit"] != 1:
            return
        match = _TRACEBACK.match(normalize._get(obs, "stderr"))
        if (match and match.group("last").startswith(marker)
                and normalize._get(obs, "stdout") == _native(stdout)):
            normalize._put(obs, "stdout", b"")
            normalize._put(obs, "stderr", _native(DEFERRAL))
    normalize.rule(name)(rule)


for _name, (_marker, _stdout) in DECLARED_CRASHES.items():
    _declared_crash(_name, _marker, _stdout)


def _arm(arm, obs):
    obs["arm"] = arm.name


# --- fixture daemon --------------------------------------------------------------

def link(shape=None, status: int = 200):
    """``/api/console/link`` answering for the origin the request names;
    ``shape(origin)`` returns the fields to change."""
    def route(request):
        origin = json.loads(request["body"])["origin"]
        value = {"url": f"{origin}/ui/#/signin?code={CODE}", "code": CODE, "origin": origin,
                 "principal": "fixture", "expires_in": 300}
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


# --- cases -----------------------------------------------------------------------

def _opens_no_browser(argv: list[str]) -> bool:
    """The oracle reaches ``open_browser`` only on the sign-in path without
    ``--no-open``: rows with a daemon must be off that path."""
    words = argv[1:]
    if "--list" in words or "--no-open" in words:
        return True
    for index, word in enumerate(words):
        if word == "--revoke" and index + 1 < len(words) and words[index + 1]:
            return True
        if word.startswith("--revoke=") and word != "--revoke=":
            return True
    return False


def case(case_id: str, argv: list[str], *, routes=None, env=None, rules=(), setup=None,
         platforms=("windows", "linux"), timeout=40.0, tls=False,
         expect_output=False) -> Case:
    full = ["console", *argv]
    if routes is not None and not _opens_no_browser(full):
        raise AssertionError(f"{case_id}: a daemon case must not reach the browser path")
    return Case(case_id, full, env={**TOKEN, **(env or {})}, rules=rules, setup=setup,
                after=_arm, platforms=platforms, timeout=timeout, expect_output=expect_output,
                daemon=factory(routes, tls) if routes is not None else None)


def deferred(case_id: str, argv: list[str], **kwargs) -> Case:
    # Never a daemon: the oracle answers these on its own path.
    return case(case_id, argv, rules=("console-deferral",), **kwargs)


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
    add(deferred("defer-plain", []))
    add(deferred("defer-to-board", ["--to", "board"]))
    add(deferred("defer-json", ["--json"]))
    add(deferred("defer-unless-signed-in", ["--no-open", "--unless-signed-in"]))
    add(deferred("defer-unless-signed-in-plain", ["--unless-signed-in"]))
    add(deferred("defer-empty-revoke", ["--revoke", ""]))
    add(deferred("defer-empty-revoke-no-open", ["--no-open", "--revoke="]))
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
             routes={"/api/console/link": link(lambda _o: {"principal": {"a": [1, None]},
                                                            "code": "ü-1"})}))
    add(case("link-unicode-code", ["--no-open"],
             routes={"/api/console/link": link(lambda _o: {"code": "näme ✓"})}))
    for name, shape in (("no-code", lambda _o: {"code": None}),
                        ("code-number", lambda _o: {"code": 5}),
                        ("other-origin", lambda o: {"url": "http://127.0.0.1:2/ui/#/signin?code=A"}),
                        ("control-url", lambda o: {"url": f"{o}/ui/#/signin?code=A B"}),
                        ("origin-not-http", lambda o: {"origin": "ftp://h"}),
                        ("no-url", lambda _o: {"url": None})):
        add(case(f"link-bad-{name}", ["--no-open"], routes={"/api/console/link": link(shape)}))
    add(case("link-bad-json", ["--no-open", "--json"],
             routes={"/api/console/link": link(lambda _o: {"origin": None})}))

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

    # Declared oracle crashes after the request: the candidate defers.
    add(case("crash-unhashable-error", ["--no-open"],
             routes={"/api/console/link": json_body({"error": ["x"]}, 403)},
             rules=("console-unhashable-traceback",)))
    add(case("crash-expires-text", ["--no-open"],
             routes={"/api/console/link": link(lambda _o: {"expires_in": "abc"})},
             rules=("console-expires-traceback",)))
    add(case("crash-stamp-nan", ["--list"],
             routes=sessions(rows=[{"created_at": float("nan")}]),
             rules=("console-stamp-nan-traceback",)))
    add(case("crash-stamp-overflow", ["--list"], routes=sessions(rows=[{"last_used_at": 1e20}]),
             rules=("console-stamp-overflow-traceback",)))
    add(case("crash-stamp-negative", ["--list"], routes=sessions(rows=[{"created_at": -5}]),
             rules=("console-stamp-negative-traceback",), platforms=("windows",)))
    add(case("crash-surrogate-label", ["--list"], routes=sessions(rows=[{"label": "\udc80"}]),
             rules=("console-surrogate-traceback",)))
    return c


MUTANTS = [
    Mutant("console-plain-sign-in-admitted", "console", FILE,
           "} else if args.no_open && !args.unless_signed_in && args.revoke.is_none() {",
           "} else if !args.unless_signed_in && args.revoke.is_none() {",
           ("defer-plain", "defer-to-board")),
    Mutant("console-405-not-old", "console", FILE, "if matches!(status, 404 | 405) {",
           "if matches!(status, 404) {", ("refused-405",)),
    Mutant("console-get-without-content-type", "console", "shim/src/cli/hook_http.rs",
           "    if content_type {\n", "    if body.is_some() {\n", ("list-json",)),
    Mutant("console-stamp-rounded", "console", FILE, "value.floor() as i64",
           "value.round() as i64", ("list",)),
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
