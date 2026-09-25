"""No PostgreSQL password in a pytest failure report, whoever owns the frame.

pytest's long traceback renders every frame's arguments, and ``--showlocals``
its locals. psycopg's ``Connection.connect`` rebinds its ``conninfo`` argument
to the keyword DSN it built and holds a ``params`` dict, so ANY connect
failure that escapes into a test (``pg_conn`` setup, an in-process
``PostgresStorage``) printed ``password=<value>`` in clear text — seen in
local full-suite logs on 2026-09-25. ``RedactedUrl`` and the ``from None``
re-raises only cover frames the tests own; tests/report_redaction.py scrubs
the report itself.

The end-to-end check runs a nested pytest session over a real failing
connect (a local listener that accepts and hangs up, so psycopg fails at once)
with a synthetic password, once with the redaction plugin and once without.
The run without it must leak on every channel, or the guard proves nothing.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from tests import pg_defaults, report_redaction

pytest.importorskip("psycopg")

ROOT = Path(__file__).resolve().parent.parent
# Synthetic values: never a real credential. SECRET is registered nowhere, so
# only the credential-shape patterns can catch it; REGISTERED is handed to the
# nested run as PSEUDOLIFE_TEST_PG_PASSWORD and appears only in a plain local,
# so only the known-password layer can catch it.
SECRET = "Synthetic-Leak-7Qz"
REGISTERED = "Registered-Only-4Kp"

_CHILD_FILES = {
    "pytest.ini": "[pytest]\n",
    "leakhelp.py": '''\
import os
import socket
import threading

SECRET = os.environ["LEAK_SECRET"]
_server = socket.create_server(("127.0.0.1", 0))
PORT = _server.getsockname()[1]


def _hang_up():
    while True:
        try:
            conn, _ = _server.accept()
        except OSError:
            return
        conn.close()


threading.Thread(target=_hang_up, daemon=True).start()


def keyword_dsn():
    return (f"host=127.0.0.1 port={PORT} password={SECRET} dbname=leak "
            "connect_timeout=3")
''',
    "test_connect_leaks.py": '''\
import os

import psycopg
import pytest

from leakhelp import PORT, SECRET


def test_keyword_dsn():
    psycopg.connect(f"host=127.0.0.1 port={PORT} user=pseudolife "
                    f"password={SECRET} dbname=leak connect_timeout=3")


def test_uri_dsn():
    psycopg.connect(f"postgresql://pseudolife:{SECRET}@127.0.0.1:{PORT}/leak"
                    "?connect_timeout=3")


def test_keyword_arguments():
    psycopg.connect(host="127.0.0.1", port=PORT, user="pseudolife",
                    password=SECRET, dbname="leak", connect_timeout=3)


@pytest.fixture
def conn():
    dsn = f"postgresql://pseudolife:{SECRET}@127.0.0.1:{PORT}/leak"
    with psycopg.connect(dsn, connect_timeout=3) as c:
        yield c


def test_fixture_setup(conn):
    pass


def test_skip_reason():
    pytest.skip(f"no server at postgresql://pseudolife:{SECRET}@127.0.0.1:{PORT}/leak")


def test_registered_password_in_plain_local():
    pw = os.environ["PSEUDOLIFE_TEST_PG_PASSWORD"]
    assert pw == "something else"
''',
    # --showlocals renders a collection error's module globals, so the value
    # is not bound to a bare module-level name here: no text layer can know
    # an unregistered value under an uninformative name (report_redaction's
    # docstring states the limit).
    "test_collect_leak.py": '''\
import psycopg

import leakhelp

psycopg.connect(leakhelp.keyword_dsn())
''',
}

# Report section header -> the synthetic value that section leaks without
# the plugin. Frame-argument channels (test call, fixture setup, collection)
# plus the rendered-local channel.
_SECTIONS = {
    "ERROR collecting test_collect_leak.py": SECRET,
    "ERROR at setup of test_fixture_setup": SECRET,
    "test_keyword_dsn": SECRET,
    "test_uri_dsn": SECRET,
    "test_keyword_arguments": SECRET,
    "test_registered_password_in_plain_local": REGISTERED,
}
_PSYCOPG_FRAME_SECTIONS = [name for name, value in _SECTIONS.items()
                           if value == SECRET]
_HEADER = re.compile(r"^_{3,} (.+?) _{3,}$", re.MULTILINE)


def _run_nested(root: Path, *, redact: bool) -> str:
    for name, text in _CHILD_FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("PYTEST_")
           and key not in ("PGPASSWORD", "PSEUDOLIFE_TEST_DATABASE_URL",
                           "PSEUDOLIFE_BENCH_ADMIN_URL",
                           "PSEUDOLIFE_MCP_DATABASE_URL")}
    env.update(PYTHONPATH=os.pathsep.join(filter(None, (str(ROOT), env.get("PYTHONPATH")))),
               LEAK_SECRET=SECRET, PSEUDOLIFE_TEST_PG_PASSWORD=REGISTERED)
    # The loudest settings: long frames with arguments, locals, untruncated
    # reprs, every outcome in the summary, and collection errors kept.
    args = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
            "--color=no", "-rA", "-vv", "--showlocals", "--tb=long",
            "--continue-on-collection-errors"]
    if redact:
        args += ["-p", "tests.report_redaction"]
    proc = subprocess.run(args, cwd=root, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=180)
    return proc.stdout + proc.stderr


def _sections(output: str) -> dict[str, str]:
    heads = list(_HEADER.finditer(output))
    return {head.group(1): output[head.end():nxt.start() if nxt else len(output)]
            for head, nxt in zip(heads, heads[1:] + [None])}


@pytest.fixture(scope="module")
def nested_reports(tmp_path_factory) -> dict[bool, str]:
    return {redact: _run_nested(tmp_path_factory.mktemp(f"redact-{redact}"),
                                redact=redact)
            for redact in (False, True)}


def test_without_the_plugin_every_channel_leaks(nested_reports):
    """The control: if these did not leak, the guard below would be
    decoration. Asserted as booleans so a failure here does not itself
    print a report full of synthetic values."""
    output = nested_reports[False]
    sections = _sections(output)
    leaking = {name: value in sections.get(name, "")
               for name, value in _SECTIONS.items()}
    assert leaking == dict.fromkeys(_SECTIONS, True)
    skip_lines = [line for line in output.splitlines()
                  if line.startswith("SKIPPED")]
    assert [SECRET in line for line in skip_lines] == [True]


def test_with_the_plugin_no_channel_leaks(nested_reports):
    output = nested_reports[True]
    assert SECRET not in output
    assert REGISTERED not in output
    # Not vacuous: every channel rendered, with psycopg's own frame and its
    # credential-bearing arguments still in the report, only masked.
    sections = _sections(output)
    assert sorted(name for name in _SECTIONS if name in sections) == sorted(_SECTIONS)
    for name in _PSYCOPG_FRAME_SECTIONS:
        body = sections[name]
        assert "conninfo = " in body and "params = " in body, name
        assert "password=***" in body and "'password': '***'" in body, name
    assert re.search(r"4 failed, 1 skipped, 2 errors", output)


def test_conftest_registers_the_report_hooks(request):
    """The nested run loads the plugin with -p; this session gets it from
    tests/conftest.py, which is what protects the real suite."""
    hook = request.config.pluginmanager.hook
    for name in ("pytest_runtest_makereport", "pytest_make_collect_report"):
        functions = [impl.function for impl in getattr(hook, name).get_hookimpls()]
        assert getattr(report_redaction, name) in functions, name


# -- the text layer ----------------------------------------------------------

@pytest.mark.parametrize("text", [
    f"user=u password={SECRET} host=h",
    f"'user=u password={SECRET}'",
    f"password='{SECRET} two' host=h",
    f"postgresql://u:{SECRET}@h:5433/db",
    f"postgres://:{SECRET}@h/db",
    f"postgresql://u@h/db?sslmode=require&password={SECRET}",
    f"{{'host': 'h', 'password': '{SECRET}', 'port': 5433}}",
    f" 'password': '{SECRET}',",
    f'{{"PGPASSWORD": "{SECRET}"}}',
    f"password   = '{SECRET}'",
    f"POSTGRES_PASSWORD={SECRET}",
])
def test_credential_shapes_are_masked_without_knowing_the_value(text):
    assert SECRET not in report_redaction.redact_text(text, frozenset())


@pytest.mark.parametrize("text", [
    "password = default_password(env, env_file)",
    "postgresql://u:***@h:5433/db",
    "connection to server at \"127.0.0.1\", port 5433 failed",
])
def test_text_without_a_credential_is_left_alone(text):
    assert report_redaction.redact_text(text, frozenset()) == text


def test_known_passwords_are_masked_in_any_position():
    secret = "pa@ss'wd\\x"
    text = (f"pw = {secret!r}; url-encoded {quote(secret, safe='')}; "
            f"bare {secret}")
    redacted = report_redaction.redact_text(text, frozenset({secret}))
    assert "pa@ss" not in redacted and "pa%40ss" not in redacted


def test_known_passwords_come_from_every_place_a_run_reads_them(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=from-env-file\n", encoding="utf-8")
    environ = {
        "PSEUDOLIFE_TEST_PG_PASSWORD": "from-test-var",
        "PGPASSWORD": "from-libpq-var",
        "PSEUDOLIFE_TEST_DATABASE_URL": "postgresql://u:from-test-dsn@h/db",
        "PSEUDOLIFE_BENCH_ADMIN_URL": "host=h password=from-bench-dsn",
        "PSEUDOLIFE_MCP_DATABASE_URL": "postgresql://u:from%40daemon@h/db",
    }
    assert report_redaction.known_passwords(environ, env_file) == {
        "from-env-file", "from-test-var", "from-libpq-var", "from-test-dsn",
        "from-bench-dsn", "from@daemon"}


def test_the_public_compose_default_is_not_a_known_password(tmp_path):
    """It is published in ops/docker-compose.yml and is also the role name:
    masking it everywhere would wreck every report and hide nothing."""
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=$UNSUPPORTED\n", encoding="utf-8")
    environ = {"PSEUDOLIFE_TEST_PG_PASSWORD": pg_defaults.COMPOSE_DEFAULT_PASSWORD,
               "PSEUDOLIFE_TEST_DATABASE_URL": "not a dsn = = ="}
    assert report_redaction.known_passwords(environ, env_file) == frozenset()


def test_an_unrecognised_report_shape_is_flattened_and_masked():
    """A plugin may hand pytest its own repr object; the walk only trusts
    pytest's classes, so anything else becomes masked text rather than
    passing through unread."""
    class _ForeignRepr:
        def toterminal(self, tw):  # pragma: no cover - never rendered here
            tw.line(str(self))

        def __str__(self):
            return f"connect failed: password={SECRET}"

    class _Report:
        longrepr = _ForeignRepr()
        sections = [("Captured log call", f"dsn postgresql://u:{SECRET}@h/db")]

    report = _Report()
    report_redaction.redact_report(report)
    assert isinstance(report.longrepr, str)
    assert SECRET not in report.longrepr and "connect failed" in report.longrepr
    assert SECRET not in report.sections[0][1]
