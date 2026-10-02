"""``pseudolife-mcp pair``: join a bank with a pairing code.

    pseudolife-mcp pair <daemon-url> [<code> | --read-code] [--token-file PATH] [--json]

Runs on the machine that joins. The daemon's operator ran
``pseudolife-mcp invite <machine>`` and passed on a short-lived, single-use
pairing code. This command:

1. Validates the URL the way ``connect`` does and asks ``/health`` (no
   proxy, no redirect followed), which must answer ``status: ok`` with
   ``"auth": true``. Anything else is refused before any change.
2. Mints the bearer token here (``secrets.token_urlsafe(32)``) and writes it
   to a new owner-only file: ``--token-file``, else
   ``~/.pseudolife-mcp/pairing-<8 hex>.token``. An existing file is never
   overwritten.
3. ``POST /api/pair`` with the code and the token's SHA-256, with no
   ``Origin`` and no ``Authorization`` header. The token itself never
   leaves this machine except as the bearer of the final check.
4. On success, moves the file to ``~/.pseudolife-mcp/<principal>.token``
   (unless ``--token-file`` was given) without ever replacing a file, and
   only when the returned name is a valid principal name: a server, or
   anyone on a plain-HTTP path, cannot steer the path. Then verifies the
   token with ``connect``'s authenticated check.
5. A lost response (timeout, reset, a 5xx) is retried with the same body:
   the daemon answers an idempotent retry with the original 200. A refusal
   of the first attempt removes the file. Once any attempt's outcome was
   unknown, a later refusal (a 429, say) proves nothing, so the file is
   kept; when the outcome stays unknown, or the check fails, the file is
   kept and named too: the code may be spent, and the file is the only copy
   of a token the daemon may now accept.

The token and the code are never printed, and neither is in the JSON
report.

Exit codes: 0 paired and verified; 2 usage (bad URL, no code, both a code
and ``--read-code``, a malformed code); 4 refused, nothing kept; 5 outcome
unknown or the check failed, the token file kept and named.

Standard library only (plus the package's standard-library modules), so a
shim runtime and the installers can run it.
"""
from __future__ import annotations

import argparse
import getpass
import io
import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import urllib.request

from pseudolife_memory import client_config, codex_connection, runtimes
from pseudolife_memory.credentials import CredentialError
from pseudolife_memory.daemon_url import _NoRedirectHandler
from pseudolife_memory.principals import (
    RESERVED_PRINCIPALS, normalize_pairing_code, secret_sha256, valid_principal_name)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 4
EXIT_UNKNOWN = 5

#: Retries of a POST whose answer was lost; the backoff between attempts.
RETRIES = 3
BACKOFF_S = (1.0, 2.0, 4.0)
HEALTH_TIMEOUT_S = 5.0
POST_TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 65536

RATE_LIMITED = "pairing is rate-limited on the daemon, try again in a minute"
_OLD_DAEMON = ("the daemon does not offer pairing (HTTP {status}); update the daemon first, "
               "then re-run with the same code")
# connect's warning, for a remote daemon reached over plain HTTP.
_PLAIN_HTTP = ("WARNING: {url} is plain HTTP: the link itself is unencrypted, so it must be a "
               "private network such as a tailnet, or a TLS reverse proxy must front the daemon.")
# Whether a move is the POSIX link-then-unlink (Windows renames instead).
_POSIX_MOVE = os.name != "nt"
_KEPT = ("{path} is kept: the code may already be spent, and this file is the only copy of a "
         "token the daemon may now accept. Re-run `pseudolife-mcp connect <url> --token-file "
         "{path}` once the daemon answers; delete the file only if the operator re-invites this "
         "machine")


# -- seams (the tests replace these) ---------------------------------------------

def sleep(seconds: float) -> None:
    time.sleep(seconds)


def interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler)


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def probe_health(url: str, timeout: float = HEALTH_TIMEOUT_S) -> dict | None:
    """``<url>/health`` as a dict, or ``None`` when nothing usable answers."""
    try:
        with _opener().open(url + "/health", timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_RESPONSE_BYTES)
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001 - refused, reset, timed out, unreadable
        return None
    payload = _json(body)
    return payload if isinstance(payload, dict) else None


def post_pair(url: str, body: bytes, timeout: float = POST_TIMEOUT_S) -> tuple[int, object]:
    """``(status, parsed JSON body)`` for one ``POST /api/pair``. Raises when
    no HTTP answer arrived (refused, reset, timed out, closed)."""
    request = urllib.request.Request(url + "/api/pair", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with _opener().open(request, timeout=timeout) as response:
            return response.status, _json(response.read(MAX_RESPONSE_BYTES))
    except urllib.error.HTTPError as exc:
        try:
            payload = _json(exc.read(MAX_RESPONSE_BYTES))
        except Exception:  # noqa: BLE001
            payload = None
        return exc.code, payload


def verify_token(url: str, token: str) -> bool:
    """``connect``'s authenticated check (no redirect followed)."""
    from pseudolife_memory import connect_cli
    return connect_cli.credential_valid(url, token)


# -- helpers ----------------------------------------------------------------------

def bank_directory() -> Path:
    return runtimes.home() / ".pseudolife-mcp"


def default_token_file() -> Path:
    return bank_directory() / f"pairing-{secrets.token_hex(4)}.token"


def _shown_url(value) -> str:
    from pseudolife_memory.connect_cli import _shown_url as shown
    return shown(value)


def _text(value) -> str | None:
    """A short printable string from the daemon, else ``None``."""
    if isinstance(value, str) and 0 < len(value) <= 128 and value.isprintable():
        return value
    return None


class _LinkedTwice(Exception):
    """The hard link to the new name exists, but the old name could not be
    removed: both files hold the token."""


def _move_no_replace(source: Path, target: Path) -> None:
    """Move ``source`` to ``target``; never replace an existing ``target``.
    POSIX: a hard link fails on an existing name, then the old name goes
    (raises :class:`_LinkedTwice` when only the link worked). Windows:
    ``os.rename`` refuses an existing target."""
    if not _POSIX_MOVE:
        os.rename(source, target)
        return
    os.link(source, target)
    try:
        os.unlink(source)
    except OSError as exc:
        raise _LinkedTwice() from exc


def _report(url: str | None) -> dict:
    return {"url": url, "state": None, "principal": None, "tier": None, "bank": None,
            "token_file": None, "warnings": [], "error": None, "exit": None}


def _done(report: dict, state: str, code: int, error: str | None = None) -> dict:
    report.update(state=state, exit=code, error=error)
    return report


def read_code_line() -> str:
    """One line from stdin (without echo on a terminal)."""
    if interactive():
        return getpass.getpass("pairing code: ")
    return sys.stdin.readline()


# -- redemption -------------------------------------------------------------------

def redeem(url: str, code: str, token_file: str | None = None) -> dict:
    """Pair this machine: ``url`` is a validated origin, ``code`` the
    canonical pairing code. Returns the report (``state`` / ``exit`` as in
    the module docstring); ``token_file`` names the file only while it
    exists."""
    report = _report(url)
    explicit = token_file is not None
    target = Path(os.path.abspath(os.path.expanduser(token_file))) if explicit else default_token_file()
    if target.exists() or target.is_symlink():
        return _done(report, "refused", EXIT_REFUSED,
                     f"{target} already exists; pairing creates a token file but never replaces one. "
                     "Nothing was changed")

    health = probe_health(url)
    if not isinstance(health, dict) or health.get("status") != "ok":
        return _done(report, "refused", EXIT_REFUSED,
                     f"the daemon at {url} did not answer /health with status ok; nothing was changed")
    if health.get("auth") is not True:
        return _done(report, "refused", EXIT_REFUSED,
                     f"the daemon at {url} does not report authentication (auth: "
                     f"{json.dumps(health.get('auth'))}); pairing needs a daemon with a bearer token. "
                     "Nothing was changed")

    token = secrets.token_urlsafe(32)
    try:
        client_config.write_token_file(target, io.BytesIO(token.encode("utf-8")))
    except client_config.HelperError as exc:
        return _done(report, "refused", EXIT_REFUSED, f"{exc}; nothing was changed")
    except (OSError, CredentialError) as exc:
        return _done(report, "refused", EXIT_REFUSED,
                     f"{type(exc).__name__} while writing {target}; check its directory's "
                     "permissions. Nothing was changed")
    report["token_file"] = str(target)
    body = json.dumps({"code": code, "token_sha256": secret_sha256(token)}).encode("utf-8")

    def refused(state: str, error: str) -> dict:
        target.unlink(missing_ok=True)
        report["token_file"] = None
        return _done(report, state, EXIT_REFUSED, error)

    def refused_after_unknown(error: str) -> dict:
        # An earlier attempt may have redeemed the code with this token, so
        # this refusal does not prove the file useless.
        return _done(report, "unknown", EXIT_UNKNOWN,
                     f"{error}, after an attempt whose answer was lost. " + _KEPT.format(path=target))

    payload = None
    uncertain = False
    for attempt in range(1 + RETRIES):
        if attempt:
            sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
        try:
            status, payload = post_pair(url, body)
        except Exception:  # noqa: BLE001 - no answer arrived; the outcome is unknown
            uncertain = True
            continue
        if status == 200 and isinstance(payload, dict):
            break
        if uncertain and 300 <= status < 500:
            return refused_after_unknown(f"the daemon then answered HTTP {status}")
        if status == 400:
            return refused("refused", "the daemon refused the pairing code (unknown, expired, "
                                      "already used or revoked); ask the operator for a new one "
                                      "(`pseudolife-mcp invite <machine>` on the daemon host)")
        if status == 429:
            return refused("rate_limited", RATE_LIMITED)
        if status in (401, 404, 405):
            return refused("refused", _OLD_DAEMON.format(status=status))
        if 300 <= status < 500:
            return refused("refused", f"the daemon refused the pairing request (HTTP {status})")
        # A 5xx, or a 200 without a readable body: the outcome is unknown.
        uncertain = True
    else:
        return _done(report, "unknown", EXIT_UNKNOWN,
                     f"no answer from the daemon at {url} after {1 + RETRIES} attempts. "
                     + _KEPT.format(path=target))

    principal = payload.get("principal")
    report["tier"] = _text(payload.get("tier"))
    report["bank"] = _text(payload.get("bank"))
    if valid_principal_name(principal) and principal not in RESERVED_PRINCIPALS:
        report["principal"] = principal
        if not explicit:
            final = bank_directory() / f"{principal}.token"
            try:
                _move_no_replace(target, final)
            except _LinkedTwice:
                report["warnings"].append(
                    f"the token is in both {final} and {target}: the new name was linked but the "
                    f"pairing name could not be removed; delete {target} once {final} works")
                target = final
                report["token_file"] = str(target)
            except OSError:
                report["warnings"].append(
                    f"{final} already exists (or could not be created); it was left as it is, and "
                    f"the new token stays in {target}")
            else:
                target = final
                report["token_file"] = str(target)
    else:
        report["warnings"].append(
            "the daemon returned a principal name that is not a valid file name; the token file "
            f"keeps its pairing name {target.name}")

    if not verify_token(url, token):
        return _done(report, "unverified", EXIT_UNKNOWN,
                     f"the daemon at {url} accepted the code but refused the new token on an "
                     "authenticated request. " + _KEPT.format(path=target))
    return _done(report, "paired", EXIT_OK)


# -- the command ------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp pair",
        description="Join a bank with a pairing code from `pseudolife-mcp invite` on the daemon "
                    "host: mint a bearer token here, write it to an owner-only file and register "
                    "only its SHA-256 with the daemon. Exit codes: 0 paired and verified, 2 usage, "
                    "4 refused (nothing kept), 5 outcome unknown or not verified (the token file "
                    "is kept and named).")
    parser.add_argument("daemon_url", metavar="daemon-url",
                        help="the daemon's origin, e.g. http://100.64.0.2:8765 (no path)")
    parser.add_argument("code", nargs="?", default=None,
                        help="the pairing code (XXXX-XXXX-XXXX); or use --read-code")
    parser.add_argument("--read-code", action="store_true",
                        help="read the code from stdin (without echo on a terminal), keeping it "
                             "out of the command line")
    parser.add_argument("--token-file", default=None,
                        help="create this owner-only token file (it must not exist) instead of "
                             "~/.pseudolife-mcp/<principal>.token")
    parser.add_argument("--json", action="store_true", help="one JSON report on stdout")
    return parser


def _emit(report: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report, indent=2))
        return report["exit"]
    for line in report["warnings"]:
        print(f"pair: warning: {line}", file=sys.stderr)
    if report["exit"] == EXIT_OK:
        tier = f" (tier {report['tier']})" if report["tier"] else ""
        principal = report["principal"] or "a principal whose name could not be used"
        print(f"paired: {principal}{tier} on {report['url']}")
        print(f"token file: {report['token_file']}")
    elif report["error"]:
        print(f"pair: {report['error']}", file=sys.stderr)
    return report["exit"]


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(sys.argv[2:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    def usage(line: str, url: str | None = None) -> int:
        return _emit(_done(_report(url), "usage", EXIT_USAGE, line), args.json)

    try:
        url = codex_connection._validated_daemon_url(args.daemon_url)
    except codex_connection.SetupError:
        return usage(f"the daemon URL given ({_shown_url(args.daemon_url)}) is not an http(s) origin: "
                     "give one such as http://100.64.0.2:8765, with no path, query, fragment or "
                     "credentials")
    if args.code is not None and args.read_code:
        return usage("give the pairing code as an argument or with --read-code, not both", url)
    if args.code is None and not args.read_code:
        return usage("no pairing code: give it as an argument, or pass --read-code and write it "
                     "on stdin", url)
    text = read_code_line() if args.read_code else args.code
    if args.read_code and not text.strip():
        return usage("--read-code read no code on stdin", url)
    code = normalize_pairing_code(text)
    if code is None:
        return usage("that is not a pairing code: it has 12 letters and digits, shown as "
                     "XXXX-XXXX-XXXX", url)
    report = redeem(url, code, args.token_file)
    from pseudolife_memory.daemon_url import _is_loopback_url
    if url.startswith("http://") and not _is_loopback_url(url):
        report["warnings"].insert(0, _PLAIN_HTTP.format(url=url))
    return _emit(report, args.json)
