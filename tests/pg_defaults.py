"""Where the test suites find the dev Postgres, and how they tell "no
server" from "wrong password".

Plain module (no pytest import) so ``conftest.py``, ``pg_fixtures.py`` and
``helpers.py`` can all share it. Two facts it exists to encode:

* The dev container's role password is whatever ``ops/.env`` says
  (``POSTGRES_PASSWORD``, the same value compose hands the container), not a
  constant. After the 2026-09-14 credential rotation the suite's hard-coded
  ``pseudolife`` default stopped matching, every PG-backed test skipped with
  "password authentication failed", and ``pytest tests/`` still exited 0
  with about a thousand skips — the "skips silently, which is not a pass"
  trap CLAUDE.md warns about, in a new form.
* A reachable server that rejects the credentials is a misconfiguration,
  never a reason to skip. Skips are for a server that is not there.
"""
from __future__ import annotations

import errno
import functools
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

COMPOSE_DEFAULT_PASSWORD = "pseudolife"
DEV_HOST_PORT = "127.0.0.1:5433"
DEV_ROLE = "pseudolife"
# The compose stack's env file; the installer writes POSTGRES_PASSWORD there.
ENV_FILE = Path(__file__).resolve().parent.parent / "ops" / ".env"
# What the server says when it is THERE but will not have you: bad password
# (28P01), and the rest of SQLSTATE class 28 — unknown role, no pg_hba entry.
_AUTH_FAILURE_MARKERS = ("password authentication failed",
                         "no password supplied",
                         "no pg_hba.conf entry",
                         "does not exist")  # FATAL: role "x" does not exist
_UNAVAILABLE_MARKERS = (
    "connection refused",
    "connection timed out",
    "could not translate host name",
    "host is unknown",
    "name or service not known",
    "network is unreachable",
    "no route to host",
    "nodename nor servname provided",
    "temporary failure in name resolution",
    "timeout expired",
    "timed out",
)
_UNAVAILABLE_ERRNOS = {
    errno.ECONNREFUSED,
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
    10060,  # WSAETIMEDOUT
    10061,  # WSAECONNREFUSED
    10065,  # WSAEHOSTUNREACH
}


def _compose_value(raw: str, line_number: int) -> str:
    """Parse the Compose ``.env`` value forms needed for a password.

    Single-quoted values are literal. Unquoted and double-quoted values use
    Compose variable expansion; rather than reimplement that language here,
    reject ``$`` clearly and direct the operator to the explicit test override.
    The exception never includes the value.
    """
    raw = raw.strip()
    if not raw:
        return ""

    quote_char = raw[0] if raw[0] in "'\"" else None
    if quote_char is None:
        for index, char in enumerate(raw):
            if char == "#" and index > 0 and raw[index - 1].isspace():
                raw = raw[:index].rstrip()
                break
        if "$" in raw:
            raise ValueError(
                f"ops env line {line_number}: unsupported Compose variable "
                "expansion in POSTGRES_PASSWORD; use a single-quoted literal "
                "or PSEUDOLIFE_TEST_PG_PASSWORD"
            )
        return raw

    chars: list[str] = []
    escaped = False
    closing = None
    escape_map = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
    for index, char in enumerate(raw[1:], start=1):
        if quote_char == '"' and escaped:
            chars.append(escape_map.get(char, char))
            escaped = False
            continue
        if quote_char == '"' and char == "\\":
            escaped = True
            continue
        if quote_char == "'" and char == "\\" and index + 1 < len(raw) \
                and raw[index + 1] == "'":
            chars.append("'")
            escaped = True
            continue
        if quote_char == "'" and escaped:
            escaped = False
            continue
        if char == quote_char:
            closing = index
            break
        chars.append(char)
    if closing is None:
        raise ValueError(
            f"ops env line {line_number}: unterminated quoted POSTGRES_PASSWORD"
        )
    tail = raw[closing + 1:].strip()
    if tail and not tail.startswith("#"):
        raise ValueError(
            f"ops env line {line_number}: unexpected text after POSTGRES_PASSWORD"
        )
    value = "".join(chars)
    if quote_char == '"' and "$" in value:
        raise ValueError(
            f"ops env line {line_number}: unsupported Compose variable "
            "expansion in POSTGRES_PASSWORD; use a single-quoted literal "
            "or PSEUDOLIFE_TEST_PG_PASSWORD"
        )
    return value


def env_file_password(path: Path | None = None) -> str | None:
    """``POSTGRES_PASSWORD`` from the compose env file, or ``None``."""
    try:
        return _read_env_file_password(path)
    except ValueError as exc:
        # Parser frames contain the file's credentials. Preserve only their
        # value-free diagnosis, including under pytest --showlocals.
        raise ValueError(str(exc)) from None


def _read_env_file_password(path: Path | None) -> str | None:
    path = ENV_FILE if path is None else path
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    value = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if key == "POSTGRES_PASSWORD":
            value = _compose_value(raw, line_number)
    return value or None


def default_password(env: os._Environ | dict | None = None,
                     env_file: Path | None = None) -> str:
    """``PSEUDOLIFE_TEST_PG_PASSWORD``, else ``ops/.env``, else the compose
    default. The env var exists for machines whose compose env file is
    elsewhere (CI sets a full ``PSEUDOLIFE_TEST_DATABASE_URL`` instead)."""
    env = os.environ if env is None else env
    explicit = env.get("PSEUDOLIFE_TEST_PG_PASSWORD")
    if explicit:
        return explicit
    return env_file_password(env_file) or COMPOSE_DEFAULT_PASSWORD


def default_admin_url(env: os._Environ | dict | None = None,
                      env_file: Path | None = None) -> str:
    """Admin (``postgres`` db) URL of the dev container. The password is
    percent-encoded: a generated one may carry ``@`` ``/`` ``:`` ``#`` or
    ``%``, any of which would re-split the URI or be mis-decoded by libpq."""
    password = quote(default_password(env, env_file), safe="")
    return f"postgresql://{DEV_ROLE}:{password}@{DEV_HOST_PORT}/postgres"


def conninfo_with_dbname(conninfo: str, dbname: str) -> str:
    """Replace only ``dbname`` in a libpq URI or keyword DSN.

    psycopg validates and parses both forms. URI input stays a URI so existing
    test-run isolation callers keep their public shape; keyword input stays a
    keyword DSN. Query/options are preserved in both cases.
    """
    conninfo = RedactedUrl(conninfo)
    try:
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        conninfo_to_dict(conninfo)
        if conninfo.lstrip().lower().startswith(("postgresql://", "postgres://")):
            parts = urlsplit(conninfo)
            query = urlencode([(key, value) for key, value in
                               parse_qsl(parts.query, keep_blank_values=True)
                               if key != "dbname"])
            return urlunsplit(parts._replace(path="/" + quote(dbname, safe=""),
                                            query=query))
        return make_conninfo(conninfo, dbname=dbname)
    except Exception:  # noqa: BLE001 - normalize without echoing the DSN
        raise ValueError("invalid PostgreSQL test connection string; value withheld") \
            from None


def conninfo_dbname(conninfo: str) -> str:
    """Return a test connection's database name without string-splitting it."""
    conninfo = RedactedUrl(conninfo)
    try:
        from psycopg.conninfo import conninfo_to_dict

        dbname = conninfo_to_dict(conninfo).get("dbname")
    except Exception:  # noqa: BLE001 - never echo a credential-bearing value
        raise ValueError("invalid PostgreSQL test connection string; value withheld") \
            from None
    if not dbname:
        raise ValueError(
            "PSEUDOLIFE_TEST_DATABASE_URL must name a database; value withheld"
        )
    return dbname


def bench_admin_url(env: os._Environ | dict | None = None,
                    env_file: Path | None = None) -> str:
    """Resolve the eval-backed suites' admin DSN.

    An explicit bench value wins. Otherwise the ordinary test override's
    endpoint, credentials and options are reused with only ``dbname`` changed
    to ``postgres``; the local compose default is the final fallback.
    """
    source = os.environ if env is None else env
    explicit = source.get("PSEUDOLIFE_BENCH_ADMIN_URL")
    test_url = source.get("PSEUDOLIFE_TEST_DATABASE_URL")
    fallback_env = {
        "PSEUDOLIFE_TEST_PG_PASSWORD": source["PSEUDOLIFE_TEST_PG_PASSWORD"]
    } if source.get("PSEUDOLIFE_TEST_PG_PASSWORD") else {}
    env = None
    del source
    if explicit:
        return RedactedUrl(explicit)
    if test_url:
        test_url = RedactedUrl(test_url)
        return RedactedUrl(conninfo_with_dbname(test_url, "postgres"))
    return RedactedUrl(default_admin_url(fallback_env, env_file))


class RedactedUrl(str):
    """A connection URL whose ``repr`` hides the password.

    pytest prints every traceback frame's *arguments* (``url = '...'``), so
    a function that takes the URL as a parameter and lets an error escape
    would put the password in the test report. Rebinding the parameter to
    this type keeps the value usable (``str`` content is intact — psycopg
    connects with it) while the report shows ``postgresql://user:***@host``.
    """

    __slots__ = ()

    @property
    def host(self) -> str:
        """Show only simple URI targets; withhold keyword/query DSNs entirely.

        Query parameters can themselves hold a password, and an ``@`` in a
        keyword-form password must not be mistaken for a URI delimiter.
        """
        if not self.startswith(("postgresql://", "postgres://")) or "?" in self or "#" in self:
            return "<redacted dsn>"
        head, sep, tail = self.rpartition("@")
        return tail if sep else "<redacted dsn>"

    def __repr__(self) -> str:
        if self.host == "<redacted dsn>":
            return "'<redacted dsn>'"
        head, sep, tail = self.rpartition("@")
        if not sep:
            return "'<redacted dsn>'"
        scheme, _, creds = head.partition("://")
        user = creds.partition(":")[0]
        return repr(f"{scheme}://{user}:***@{tail}")


class PostgresAuthError(RuntimeError):
    """A reachable test Postgres rejected the credentials.

    Raised instead of the "no server" RuntimeError so fixtures ERROR the
    PG-backed tests rather than skipping them: the server is there, the
    configuration is wrong, and a green run would be a lie."""


class PostgresUnavailableError(RuntimeError):
    """No PostgreSQL server answered; this is the fixture's sole skip signal."""


class PostgresSetupError(RuntimeError):
    """PostgreSQL answered, but the requested test operation failed."""


def is_auth_failure(exc: BaseException) -> bool:
    """True for psycopg's password / missing-password connection failures."""
    try:
        import psycopg

        if isinstance(exc, (psycopg.errors.InvalidPassword,                 # 28P01
                            psycopg.errors.InvalidAuthorizationSpecification)):  # 28000
            return True
    except ImportError:  # pragma: no cover - psycopg gates every caller
        pass
    text = str(exc).lower()
    if "does not exist" in text and "role" not in text:
        return False  # a missing DATABASE is not an auth failure
    return any(marker in text for marker in _AUTH_FAILURE_MARKERS)


def is_server_unavailable(exc: BaseException) -> bool:
    """True only for definite transport absence, never server-side failures."""
    if getattr(exc, "sqlstate", None):
        return False
    text = str(exc).lower()
    if "fatal:" in text or "panic:" in text:
        return False  # one answering host defeats a mixed-host absence report
    if isinstance(exc, (ConnectionRefusedError, TimeoutError)):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in _UNAVAILABLE_ERRNOS:
        return True
    return any(marker in text for marker in _UNAVAILABLE_MARKERS)


# libpq renders a Windows socket error as "<text> (0x%08X/%d)". These two
# describe this host's own port table, not the server: WSAEADDRINUSE, which
# a loopback connect() returns when the local port it picked still has a
# TIME_WAIT entry to the same server (the client closed first), and
# WSAENOBUFS, when the ephemeral range is exhausted. WSAEADDRINUSE failed
# three connects to the dev server on 127.0.0.1:5433 in two full runs on
# 2026-09-25 (twice in pg_conn setup, once in PostgresStorage), with about
# twenty sessions testing against that server; a full run was measured
# holding 250-600 TIME_WAIT entries to it. A second connect picks another
# port. WSAENOBUFS was not seen; it is here as the exhaustion case of the
# same port table.
_LOCAL_PORT_ERRORS = ("/10048)", "/10055)")


def is_local_port_exhaustion(exc: BaseException) -> bool:
    """True when a connect failed for want of a usable local port, which
    says nothing about the server; never for an answer from the server."""
    if getattr(exc, "sqlstate", None):
        return False
    text = str(exc)
    return any(code in text for code in _LOCAL_PORT_ERRORS)


def retry_local_port_exhaustion(connect, *, attempts: int = 5,
                                sleep=time.sleep):
    """Wrap ``psycopg.connect`` to try again, up to ``attempts`` calls in all
    and well under a second of backoff, when the local port was the problem
    (:func:`is_local_port_exhaustion`). Every other failure is raised at
    once, and the last one if the port never frees."""
    @functools.wraps(connect)
    def connect_retrying(*args, **kwargs):
        # Hidden from pytest's tracebacks, whose argument listing would
        # print the DSN, password included, for this frame.
        __tracebackhide__ = True
        for attempt in range(attempts - 1):
            try:
                return connect(*args, **kwargs)
            except Exception as exc:
                if not is_local_port_exhaustion(exc):
                    raise
            sleep(0.05 * 2 ** attempt)
        return connect(*args, **kwargs)

    connect_retrying.retries_local_port_exhaustion = True
    return connect_retrying


def redacted_error_text(exc: BaseException, conninfo: str) -> str:
    """Error detail with DSN/password/passfile values removed."""
    text = str(exc) or type(exc).__name__
    raw_conninfo = str(conninfo)
    if raw_conninfo:
        text = text.replace(raw_conninfo, "<redacted dsn>")
    try:
        from psycopg.conninfo import conninfo_to_dict

        parsed = conninfo_to_dict(raw_conninfo)
    except Exception:  # noqa: BLE001 - malformed input still gets fixed messages
        parsed = {}
    for field in ("password", "passfile"):
        secret = parsed.get(field)
        if not secret:
            continue
        text = text.replace(secret, "***")
        text = text.replace(quote(secret, safe=""), "***")
    return text


def auth_failure_message(url_hint: str, exc: BaseException,
                         conninfo: str = "") -> str:
    detail = redacted_error_text(exc, conninfo)
    return (
        f"test Postgres at {url_hint} is reachable but rejected the "
        f"credentials ({detail}). This is a misconfiguration, not a missing "
        f"server, so PG-backed tests ERROR instead of skipping. Fix: set "
        f"PSEUDOLIFE_TEST_DATABASE_URL, or PSEUDOLIFE_TEST_PG_PASSWORD, or "
        f"check POSTGRES_PASSWORD in ops/.env"
    )


def setup_failure_message(url_hint: str, exc: BaseException,
                          conninfo: str = "") -> str:
    detail = redacted_error_text(exc, conninfo)
    return f"test Postgres at {url_hint} answered but setup failed ({detail})"
