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

import os
from pathlib import Path
from urllib.parse import quote

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


def env_file_password(path: Path | None = None) -> str | None:
    """``POSTGRES_PASSWORD`` from the compose env file, or ``None``."""
    path = ENV_FILE if path is None else path
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    value = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        if key.strip() == "POSTGRES_PASSWORD":
            value = raw.strip().strip("'\"")  # last assignment wins, like compose
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
        """The part after the credentials — safe to put in messages. A DSN
        without an ``@`` (keyword form) cannot be split safely, so it is
        withheld entirely: the safe direction."""
        head, sep, tail = self.rpartition("@")
        return tail if sep else "<redacted dsn>"

    def __repr__(self) -> str:
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


def auth_failure_message(url_hint: str, exc: BaseException) -> str:
    return (
        f"test Postgres at {url_hint} is reachable but rejected the "
        f"credentials ({exc}). This is a misconfiguration, not a missing "
        f"server, so PG-backed tests ERROR instead of skipping. Fix: set "
        f"PSEUDOLIFE_TEST_DATABASE_URL, or PSEUDOLIFE_TEST_PG_PASSWORD, or "
        f"check POSTGRES_PASSWORD in ops/.env"
    )
