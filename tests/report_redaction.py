"""Mask PostgreSQL passwords in every pytest report this process produces.

pytest renders each traceback frame's arguments (and, under
``--showlocals``, its locals). psycopg's own ``Connection.connect`` rebinds
its ``conninfo`` argument to the keyword DSN it built and keeps a ``params``
dict, so any connect failure that escapes into a test — ``pg_conn`` setup,
an in-process ``PostgresStorage`` — printed ``password=<value>`` in clear
text (local full-suite logs, 2026-09-25). ``RedactedUrl`` and the
``from None`` re-raises in tests/pg_defaults.py only reach frames the tests
own; this plugin works on the report instead, so it does not matter whose
frame held the value.

Two layers, both applied to every string in the report:

* the passwords this run knows (``ops/.env``, the test/libpq password
  variables, the DSN variables), masked wherever they appear;
* credential shapes — URI userinfo, ``password=``, ``'password': '...'`` and
  ``password = '...'`` — masked whatever the value, for a DSN whose password
  the run was never told.

Out of reach of both: a password the run was never told, held under a name
that does not say what it is (``pw = '...'`` under ``--showlocals``). A real
credential used that way belongs in ``PSEUDOLIFE_TEST_PG_PASSWORD`` or the
DSN variables, where the first layer finds it.

The report keeps its structure (colours, crash line, short summary). A
report shape this module does not recognise is flattened to masked text
rather than passed through unread. Registered by tests/conftest.py; the
nested-run guard in tests/test_report_redaction.py loads it with ``-p``.
"""
from __future__ import annotations

import functools
import os
import re
from urllib.parse import quote

import pytest

from tests.pg_defaults import COMPOSE_DEFAULT_PASSWORD, env_file_password

MASK = "***"
_PASSWORD_ENV = ("PSEUDOLIFE_TEST_PG_PASSWORD", "PGPASSWORD")
_DSN_ENV = ("PSEUDOLIFE_TEST_DATABASE_URL", "PSEUDOLIFE_BENCH_ADMIN_URL",
            "PSEUDOLIFE_MCP_DATABASE_URL")

# scheme://user:<password>@ — the value may not hold a raw '@' or whitespace.
_URI_USERINFO = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@'\"]*:)[^\s@'\"]*@")
# password=<value> as libpq and URI queries spell it: a quoted value (with
# repr-doubled escapes), else everything up to whitespace or a quote.
_KEYWORD = re.compile(
    r"(?i)\b(\w*password=)('(?:\\\\.|\\.|[^'\\])*'|[^\s'\"]+)")
# A quoted value assigned or mapped to a password-named key:
# ``password = '...'`` (rendered argument or local), ``'password': '...'``.
_QUOTED = re.compile(
    r"(?i)\b(\w*password['\"]?\s*[=:]\s*)(['\"])(?:\\\\.|\\.|(?!\2).)*\2")


def _dsn_password(dsn: str) -> str | None:
    try:
        from psycopg.conninfo import conninfo_to_dict

        return conninfo_to_dict(dsn).get("password")
    except Exception:  # noqa: BLE001 - a bad DSN leaves the shape layer
        return None


def known_passwords(environ=None, env_file=None) -> frozenset[str]:
    """Every PostgreSQL password this run could have been handed.

    The public compose default is left out: it is also the role and package
    name, so masking it would wreck every report and hide nothing.
    """
    environ = os.environ if environ is None else environ
    try:
        found = {env_file_password(env_file)}
    except ValueError:  # unsupported env-file syntax; the run refuses it too
        found = set()
    found.update(environ.get(name) for name in _PASSWORD_ENV)
    found.update(_dsn_password(environ[name]) for name in _DSN_ENV
                 if environ.get(name))
    return frozenset(value for value in found
                     if value and value != COMPOSE_DEFAULT_PASSWORD)


@functools.lru_cache(maxsize=8)
def _spellings(secrets: frozenset[str]) -> tuple[str, ...]:
    """Each secret as a report can show it: raw, percent-encoded, repr-escaped."""
    forms = set()
    for secret in secrets:
        forms.update((secret, quote(secret, safe=""), quote(secret),
                      repr(secret)[1:-1]))
    return tuple(sorted(forms, key=len, reverse=True))


def redact_text(text: str, secrets: frozenset[str]) -> str:
    for form in _spellings(secrets):
        text = text.replace(form, MASK)
    text = _URI_USERINFO.sub(rf"\g<1>{MASK}@", text)
    text = _KEYWORD.sub(rf"\g<1>{MASK}", text)
    return _QUOTED.sub(rf"\g<1>\g<2>{MASK}\g<2>", text)


# Snapshotted once, at import: tests/conftest.py imports this module before
# it pops the daemon DSN, and before any test monkeypatches a variable. The
# values it sets later (the seeded bench admin URL) carry the same ops/.env
# password, so a per-report re-read would add a file read to every report
# and nothing else.
_STARTUP_SECRETS = known_passwords()


def _redact(value, secrets: frozenset[str]):
    """Mask every string in pytest's repr tree, in place where it can.

    Only pytest's own repr classes are walked; any other object raises, and
    the caller flattens the whole report to masked text instead."""
    if isinstance(value, str):
        return redact_text(value, secrets)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        value[:] = [_redact(item, secrets) for item in value]
        return value
    if isinstance(value, tuple):  # a skip's (path, lineno, reason); chain links
        return tuple(_redact(item, secrets) for item in value)
    if type(value).__module__.startswith("_pytest.") and hasattr(value, "__dict__"):
        for name, item in vars(value).items():
            object.__setattr__(value, name, _redact(item, secrets))
        return value
    raise TypeError(f"unrecognised report value: {type(value).__name__}")


def redact_report(report, secrets: frozenset[str] | None = None) -> None:
    secrets = _STARTUP_SECRETS if secrets is None else secrets
    longrepr = getattr(report, "longrepr", None)
    sections = getattr(report, "sections", None)
    if longrepr is None and not sections:
        return  # a passing test with no captured output: nothing to render
    try:
        report.longrepr = _redact(longrepr, secrets)
    except Exception:  # noqa: BLE001 - never let an unread shape through
        try:
            report.longrepr = redact_text(str(longrepr), secrets)
        except Exception:  # noqa: BLE001
            report.longrepr = "<failure report withheld: redaction failed>"
    if sections:
        report.sections = [tuple(redact_text(str(part), secrets) for part in section)
                           for section in sections]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    redact_report(outcome.get_result())


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    redact_report(outcome.get_result())
