"""``pseudolife-mcp console``: open the Console in a browser, signed in.

    pseudolife-mcp console [--no-open] [--to PAGE] [--json]
    pseudolife-mcp console --list [--json]
    pseudolife-mcp console --revoke SESSION_ID

Runs on any machine that already uses Pseudolife (spec 2026-11-02, section
4). It finds this machine's daemon address and token the way every client
command does (``PSEUDOLIFE_MCP_DAEMON_URL``, ``PSEUDOLIFE_MCP_TOKEN_FILE`` /
``PSEUDOLIFE_MCP_TOKEN``, else the Claude Code or Codex registration
``doctor`` reads), asks the daemon for a one-time sign-in code (``POST
/api/console/link``), and opens the link it answers with. The code lasts 5
minutes, works once, and only at the Console address it was made for. The
link carries it in the URL fragment, which a browser never sends to a
server.

``--no-open`` only prints the link and the code: on a headless machine,
open the link on any device that reaches that address, or type the code
into the Console's sign-in dialog. On a shared machine use it too, since a
browser's command line is visible to other users there.

``--list`` shows the browsers signed in to this bank and ``--revoke`` signs
one out.

Exit codes: 0 done; 2 usage; 3 no daemon or no token found; 4 the daemon
refused.

Standard library only (plus the package's standard-library modules), so a
shim runtime and the installers can run it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from pseudolife_memory.console_sessions import normalize_origin, valid_route_id
from pseudolife_memory.daemon_url import DEFAULT_URL, _NoRedirectHandler

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_DAEMON = 3
EXIT_REFUSED = 4

TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 262144

_REFUSALS = {
    "signin_origin_unsupported": None,      # the daemon's hint says it all
    "signin_unavailable": None,
    "session_cannot_issue": None,
    "operator_principal_required": None,
    "unauthorized": ("the daemon refused this machine's token: re-pair it (`pseudolife-mcp pair`) "
                     "or set PSEUDOLIFE_MCP_TOKEN_FILE to a token the daemon accepts"),
    "principals_unavailable": ("the daemon cannot check tokens right now (its bank is "
                               "unreachable); try again in a minute"),
}
_OLD_DAEMON = ("this daemon does not offer Console sign-in yet (HTTP {status}): update it with "
               "`pseudolife-mcp update`, or open the Console and paste a token under Advanced")


# -- seams (the tests replace these) ---------------------------------------------

def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler)


def open_browser(url: str) -> bool:
    """Open ``url`` in the system browser; False when none could be."""
    import webbrowser
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001 - no browser is not an error
        return False


def has_display(environ=None) -> bool:
    """Whether a browser could show a window here: always on Windows and
    macOS; on other systems only with a graphical session."""
    environ = os.environ if environ is None else environ
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def request(url: str, path: str, token: str, *, body: dict | None = None
            ) -> tuple[int, dict | None]:
    """``(status, parsed JSON)`` for one bearer-authenticated call. Raises
    ``OSError`` when no HTTP answer arrived."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url + path, data=data, method="GET" if body is None else "POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with _opener().open(req, timeout=TIMEOUT_S) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read(MAX_RESPONSE_BYTES)
        except Exception:  # noqa: BLE001
            raw = b""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        payload = None
    return status, payload if isinstance(payload, dict) else None


# -- credentials ------------------------------------------------------------------

def credentials(environ=None) -> tuple[str | None, str | None, str]:
    """``(daemon origin, token, where they came from)``: the environment's
    ``PSEUDOLIFE_MCP_TOKEN_FILE`` / ``PSEUDOLIFE_MCP_TOKEN``, else the first
    Claude Code or Codex registration that carries one (``doctor``'s
    search). A missing piece is ``None``; the third item says why."""
    from pseudolife_memory.credentials import CredentialError, CredentialProvider
    from pseudolife_memory.doctor_cli import registration_credentials

    env = dict(os.environ if environ is None else environ)
    found, label = registration_credentials(env)
    env.update(found)
    origin = normalize_origin(env.get("PSEUDOLIFE_MCP_DAEMON_URL") or DEFAULT_URL)
    if origin is None:
        return None, None, ("PSEUDOLIFE_MCP_DAEMON_URL is not an http(s) address "
                            "(scheme, host and optional port only)")
    try:
        if "PSEUDOLIFE_MCP_TOKEN_FILE" in env:
            provider = CredentialProvider(path=env["PSEUDOLIFE_MCP_TOKEN_FILE"])
        else:
            provider = CredentialProvider(token=env.get("PSEUDOLIFE_MCP_TOKEN") or None)
        token = provider.snapshot().token
    except CredentialError as exc:
        return origin, None, f"this machine's token is unusable ({exc})"
    if not token:
        return origin, None, (
            "no token found: set PSEUDOLIFE_MCP_TOKEN_FILE (or PSEUDOLIFE_MCP_TOKEN), or run "
            "this on a machine where Claude Code or Codex already uses Pseudolife")
    return origin, token, label or "environment"


# -- the command ------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp console",
        description="Open the Console in a browser, signed in. The link lasts 5 minutes and "
                    "works once.")
    parser.add_argument("--no-open", action="store_true",
                        help="print the link and the code without opening a browser")
    parser.add_argument("--to", metavar="PAGE",
                        help="the Console page to land on, such as board")
    parser.add_argument("--list", action="store_true",
                        help="list the browsers signed in to this bank")
    parser.add_argument("--revoke", metavar="SESSION_ID",
                        help="sign one browser out (an id from --list)")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    return parser


def _refusal(status: int, payload: dict | None) -> str:
    code = (payload or {}).get("error")
    hint = (payload or {}).get("hint")
    if code in _REFUSALS:
        return _REFUSALS[code] or hint or code
    if status in (404, 405):
        return _OLD_DAEMON.format(status=status)
    return hint or f"the daemon answered HTTP {status}" + (f" ({code})" if code else "")


def _stamp(value) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def _emit(out: dict, as_json: bool, text: str | None, code: int) -> int:
    if as_json:
        print(json.dumps(out))
    elif text:
        print(text, file=sys.stderr if code else sys.stdout)
    return code


def main(argv: list[str] | None = None, environ=None) -> int:
    args = _parser().parse_args(argv)
    if args.list and args.revoke:
        print("--list and --revoke are separate commands; give one", file=sys.stderr)
        return EXIT_USAGE
    if args.to is not None and not valid_route_id(args.to):
        print(f"--to takes a Console page id such as board, not {args.to!r}", file=sys.stderr)
        return EXIT_USAGE
    origin, token, source = credentials(environ)
    if origin is None or token is None:
        return _emit({"state": "no_credentials", "error": source}, args.json,
                     f"pseudolife-mcp console: {source}", EXIT_NO_DAEMON)
    try:
        if args.list:
            status, payload = request(origin, "/api/console/sessions", token)
        elif args.revoke:
            status, payload = request(origin, "/api/console/sessions/revoke", token,
                                      body={"session_id": args.revoke})
        else:
            body = {"origin": origin}
            if args.to:
                body["to"] = args.to
            status, payload = request(origin, "/api/console/link", token, body=body)
    except OSError as exc:
        reason = getattr(exc, "reason", exc)
        return _emit({"state": "no_daemon", "error": str(reason)}, args.json,
                     f"pseudolife-mcp console: no daemon answered at {origin} ({reason}); "
                     "check it runs (`pseudolife-mcp doctor`)", EXIT_NO_DAEMON)
    if status != 200 or payload is None:
        text = _refusal(status, payload)
        return _emit({"state": "refused", "status": status,
                      "error": (payload or {}).get("error"), "detail": text}, args.json,
                     f"pseudolife-mcp console: {text}", EXIT_REFUSED)
    if args.list:
        return _list(payload, args.json)
    if args.revoke:
        revoked = bool(payload.get("revoked"))
        text = (f"Signed out {args.revoke}." if revoked else
                f"No signed-in browser {args.revoke} that you may sign out; see --list.")
        return _emit({"state": "revoked" if revoked else "not_found"}, args.json, text,
                     EXIT_OK if revoked else EXIT_REFUSED)
    return _opened(payload, args, environ)


def _list(payload: dict, as_json: bool) -> int:
    rows = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
    if as_json:
        print(json.dumps({"state": "listed", "sessions": rows,
                          "operator": payload.get("operator")}))
        return EXIT_OK
    if not rows:
        print("No browsers are signed in. `pseudolife-mcp console` signs one in.")
        return EXIT_OK
    print(f"{'ID':<17} {'SIGNED IN AS':<20} {'HOW':<8} {'SINCE':<17} {'LAST USED':<17} BROWSER")
    for row in rows:
        if not isinstance(row, dict):
            continue
        print(f"{str(row.get('session_id', '')):<17} {str(row.get('principal', '')):<20} "
              f"{str(row.get('method', '')):<8} {_stamp(row.get('created_at')):<17} "
              f"{_stamp(row.get('last_used_at')):<17} {row.get('label') or '-'}")
    if payload.get("operator") is False:
        print("(Only your own sign-ins are shown: this machine's token is a paired one.)")
    return EXIT_OK


def _opened(payload: dict, args, environ) -> int:
    url, code = payload.get("url"), payload.get("code")
    if not isinstance(url, str) or not isinstance(code, str):
        return _emit({"state": "refused", "error": "bad_answer"}, args.json,
                     "pseudolife-mcp console: the daemon's answer had no sign-in link",
                     EXIT_REFUSED)
    opened = False
    if not args.no_open and has_display(environ):
        opened = open_browser(url)
    out = {"state": "opened" if opened else "printed", "url": url, "code": code,
           "origin": payload.get("origin"), "principal": payload.get("principal"),
           "expires_in": payload.get("expires_in")}
    if args.json:
        print(json.dumps(out))
        return EXIT_OK
    minutes = max(1, int(payload.get("expires_in") or 300) // 60)
    lead = ("Opened the Console in your browser, signed in." if opened
            else "Open this link to sign in to the Console:")
    print(lead)
    print(f"  {url}")
    print(f"Or type the code {code} into the Console's sign-in dialog at "
          f"{payload.get('origin')}/ui/. It works once, within {minutes} minutes.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
