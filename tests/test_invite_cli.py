"""``pseudolife-mcp invite`` (spec 2026-10-02, Part 2b).

The database path runs against the bench Postgres with a fake ``/health``;
the Docker path against a fake ``docker`` that records every command.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from pseudolife_memory import invite_cli
from pseudolife_memory.principal_store import bank_fingerprint
from pseudolife_memory.principals import normalize_pairing_code, secret_sha256
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

BANK_ID = "fixture-invite-bank"


def _health(**over):
    payload = {"status": "ok", "auth": True, "schema": 53, "bank": bank_fingerprint(BANK_ID)}
    payload.update(over)
    return payload


@pytest.fixture
def env(monkeypatch):
    for key in ("PSEUDOLIFE_MCP_DATABASE_URL", "PSEUDOLIFE_MCP_TOKENS", "PSEUDOLIFE_MCP_TIER_MAP",
                "PSEUDOLIFE_MCP_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(invite_cli, "exposed_url", lambda port: "http://100.64.0.2:8765")
    # Never the real docker or daemon: a test that wants the Docker path
    # installs a fake runner.
    monkeypatch.setattr(invite_cli, "docker_available", lambda: False)

    def no_commands(cmd):
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(invite_cli, "run", no_commands)
    monkeypatch.setattr(invite_cli, "local_database", lambda: (
        __import__("os").environ.get("PSEUDOLIFE_MCP_DATABASE_URL"), None))
    return monkeypatch


@pytest.fixture
def bank(env, pg_conn, pg_url, tmp_path):
    """The bench database as the daemon's bank, with its bank id set."""
    from pseudolife_memory.storage.coordination import BANK_ID_META_KEY
    pg_conn.execute("INSERT INTO meta (key, value) VALUES (%s, to_jsonb(%s::text))",
                    (BANK_ID_META_KEY, BANK_ID))
    pg_conn.commit()
    env.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    env.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    state = {"health": _health()}
    env.setattr(invite_cli, "probe_health", lambda url: state["health"])
    return state


def _run(capsys, *argv):
    code = invite_cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _rows(pg_url):
    import psycopg
    with psycopg.connect(pg_url) as conn:
        return conn.execute("SELECT principal, code_hash, token_hash, tier, board FROM principals "
                            "ORDER BY principal").fetchall()


# -- names ---------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Laptop", "_x", "a/b", "..", "a" * 65, "box x"])
def test_a_malformed_name_is_a_usage_error(env, capsys, name):
    code, _out, err = _run(capsys, name)
    assert code == 2 and "not a principal name" in err


@pytest.mark.parametrize("name", ["default", "daemon", "maintainer"])
def test_reserved_names_are_refused(env, capsys, name):
    code, _out, err = _run(capsys, name)
    assert code == 4 and "reserved" in err


def test_names_the_environment_uses_are_refused(env, capsys):
    env.setenv("PSEUDOLIFE_MCP_TOKENS", "fixture-tok:desk")
    env.setenv("PSEUDOLIFE_MCP_TIER_MAP", "tower:core")
    code, _out, err = _run(capsys, "desk")
    assert code == 4 and "PSEUDOLIFE_MCP_TOKENS" in err
    code, _out, err = _run(capsys, "tower")
    assert code == 4 and "PSEUDOLIFE_MCP_TIER_MAP" in err


@pytest.mark.parametrize("value", ["0m", "25h", "soon", "-5m"])
def test_expiry_is_bounded(env, capsys, value):
    assert _run(capsys, "laptop", "--expires", value)[0] == 2


def test_one_mode_at_a_time(env, capsys):
    assert _run(capsys)[0] == 2
    assert _run(capsys, "laptop", "--list")[0] == 2


# -- the database path ---------------------------------------------------------

def test_an_invite_prints_the_code_once_and_stores_only_its_hash(bank, pg_url, capsys):
    code, out, _err = _run(capsys, "laptop", "--tier", "core")
    assert code == 0
    printed = next(word for word in out.split() if normalize_pairing_code(word) and "-" in word)
    assert f"pseudolife-mcp pair http://100.64.0.2:8765 {printed}" in out
    assert _rows(pg_url) == [("laptop", secret_sha256(normalize_pairing_code(printed)), None,
                              "core", True)]
    assert printed not in repr(_rows(pg_url))


def test_the_json_report_carries_the_code_and_the_pair_line(bank, capsys):
    code, out, _err = _run(capsys, "laptop", "--no-board", "--expires", "2h", "--json")
    report = json.loads(out)
    assert code == 0 and report["exit"] == 0 and report["board"] is False
    assert report["pair_command"] == f"pseudolife-mcp pair http://100.64.0.2:8765 {report['code']}"


def test_a_paired_name_needs_replace(bank, pg_url, capsys):
    from pseudolife_memory import principal_store
    _run(capsys, "laptop", "--json")
    import psycopg
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("UPDATE principals SET token_hash = %s, code_hash = NULL",
                     (secret_sha256("fixture-token"),))
    code, _out, err = _run(capsys, "laptop")
    assert code == 4 and "--replace" in err
    assert _run(capsys, "laptop", "--replace")[0] == 0
    assert principal_store is not None


@pytest.mark.parametrize("health,needle", [
    (_health(auth=False), '"auth": true'),
    (_health(schema=52), "update the daemon"),
    (_health(bank=None), "has not reported which bank"),
    (_health(bank="ffffffffffffffff"), "another database"),
    (None, "no healthy daemon"),
])
def test_invite_refuses_before_writing(bank, pg_url, capsys, health, needle):
    bank["health"] = health
    code, _out, err = _run(capsys, "laptop")
    assert code == 4 and needle in err
    assert _rows(pg_url) == []


def test_an_unexposed_daemon_prints_a_placeholder(bank, env, capsys):
    env.setattr(invite_cli, "exposed_url", lambda port: None)
    code, out, _err = _run(capsys, "laptop")
    assert code == 0 and "pseudolife-mcp pair <daemon-url> " in out and "expose tailscale" in out


def test_list_and_revoke(bank, pg_url, capsys):
    _run(capsys, "laptop")
    code, out, _err = _run(capsys, "--list", "--json")
    rows = json.loads(out)["principals"]
    assert code == 0 and [(r["principal"], r["state"]) for r in rows] == [("laptop", "pending")]
    assert "code" not in rows[0] and "code_hash" not in rows[0]
    code, out, _err = _run(capsys, "--revoke", "laptop", "--yes")
    assert code == 0 and "revoked" in out
    assert _run(capsys, "--revoke", "nobody", "--yes")[0] == 4
    rows = json.loads(_run(capsys, "--list", "--json")[1])["principals"]
    assert rows[0]["state"] == "revoked"


def test_revoke_asks_first(bank, env, capsys):
    env.setattr(invite_cli, "interactive", lambda: False)
    assert _run(capsys, "--revoke", "laptop")[0] == 2


def test_the_reserved_name_check_is_load_bearing_for_the_database(bank, pg_url, capsys):
    """A reserved name never reaches the table."""
    for name in ("default", "daemon", "maintainer"):
        _run(capsys, name)
    assert _rows(pg_url) == []


# -- the Docker path -----------------------------------------------------------

class _Docker:
    def __init__(self, version="pseudolife-mcp invite 1\n", code=0, stdout="invited\n"):
        self.calls = []
        self.inputs = []
        self.version, self.code, self.stdout = version, code, stdout
        self.running = True
        self.psql = {"code": 0, "stdout": ""}

    def __call__(self, cmd, input=None):
        self.calls.append(cmd)
        self.inputs.append(input)
        if cmd[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, "true\n" if self.running else "false\n", "")
        if "pseudolife-mcp-postgres" in cmd:
            return subprocess.CompletedProcess(cmd, self.psql["code"], self.psql["stdout"], "")
        if "--version-check" in cmd:
            if self.version is None:
                return subprocess.CompletedProcess(cmd, 2, "", "unknown mode 'invite'")
            return subprocess.CompletedProcess(cmd, 0, self.version, "")
        return subprocess.CompletedProcess(cmd, self.code, self.stdout, "")


@pytest.fixture
def docker(env):
    fake = _Docker()
    env.setattr(invite_cli, "run", fake)
    env.setattr(invite_cli, "docker_available", lambda: True)
    return fake


def test_without_a_dsn_it_runs_inside_the_daemon_container(docker, capsys):
    code, out, _err = _run(capsys, "laptop", "--tier", "minimal", "--replace")
    assert code == 0 and out == "invited\n"
    _inspect, check, run = docker.calls
    assert check == ["docker", "exec", "pseudolife-mcp-daemon", "python", "-m",
                     "pseudolife_memory.cli", "invite", "--version-check"]
    assert run == ["docker", "exec", "-i", "pseudolife-mcp-daemon", "python", "-m",
                   "pseudolife_memory.cli", "invite", "laptop", "--tier", "minimal", "--expires",
                   "900s", "--replace", "--url", "http://100.64.0.2:8765"]


def test_an_older_image_is_refused(docker, capsys):
    docker.version = None
    code, _out, err = _run(capsys, "laptop")
    assert code == 4 and "update the daemon first" in err
    assert len(docker.calls) == 2


def test_revoke_is_confirmed_here_and_forwarded_with_yes(docker, env, capsys):
    env.setattr(invite_cli, "interactive", lambda: True)
    env.setattr(invite_cli, "_ask", lambda question: True)
    assert _run(capsys, "--revoke", "laptop")[0] == 0
    assert docker.calls[-1][-3:] == ["--revoke", "laptop", "--yes"]


def test_no_database_and_no_docker_is_refused(env, capsys):
    env.setattr(invite_cli, "docker_available", lambda: False)
    code, _out, err = _run(capsys, "laptop")
    assert code == 4 and "no database found" in err


def test_the_version_check_answers(capsys):
    assert invite_cli.main(["--version-check"]) == 0
    assert capsys.readouterr().out.startswith(invite_cli.VERSION_LINE)


# -- review fixes (2026-10-02): revoke and list need no healthy daemon ----------------

def test_revoke_and_list_work_with_the_daemon_down(bank, pg_url, capsys):
    _run(capsys, "laptop")
    bank["health"] = None
    assert _run(capsys, "--list")[0] == 0
    code, out, _err = _run(capsys, "--revoke", "laptop", "--yes")
    assert code == 0 and "revoked" in out


def test_bank_confirms_the_database(bank, pg_url, capsys):
    _run(capsys, "laptop")
    bank["health"] = None
    code, _out, err = _run(capsys, "--revoke", "laptop", "--yes", "--bank", "ffffffffffffffff")
    assert code == 4 and "another" in err
    assert json.loads(_run(capsys, "--list", "--json")[1])["principals"][0]["state"] == "pending"
    code, _out, _err = _run(capsys, "--revoke", "laptop", "--yes", "--bank",
                            bank_fingerprint(BANK_ID))
    assert code == 0


def test_a_stopped_daemon_container_revokes_through_postgres(docker, capsys):
    docker.running = False
    docker.psql["stdout"] = "laptop\n"
    code, out, _err = _run(capsys, "--revoke", "laptop", "--yes")
    assert code == 0 and "revoked" in out
    psql = docker.calls[-1]
    assert psql[:5] == ["docker", "exec", "-i", "pseudolife-mcp-postgres", "sh"]
    assert "UPDATE public.principals" in docker.inputs[-1] and "'laptop'" in docker.inputs[-1]
    assert not any("pseudolife-mcp-daemon" in call and "exec" in call for call in docker.calls)


def test_a_stopped_daemon_container_lists_through_postgres(docker, capsys):
    docker.running = False
    docker.psql["stdout"] = json.dumps([["laptop", "core", True, True, False, None, 1.0, 2.0, None,
                                         3.0]]) + "\n"
    code, out, _err = _run(capsys, "--list", "--json")
    assert code == 0 and json.loads(out)["principals"][0]["state"] == "paired"


def test_a_stopped_daemon_container_cannot_invite(docker, capsys):
    docker.running = False
    code, _out, err = _run(capsys, "laptop")
    assert code == 4 and "not running" in err


def test_a_failed_postgres_route_prints_the_sql_to_run(docker, capsys):
    docker.running = False
    docker.psql["code"] = 1
    code, _out, err = _run(capsys, "--revoke", "laptop", "--yes")
    assert code == 1 and "UPDATE public.principals" in err


def test_the_postgres_route_sql_runs_as_written(bank, pg_url, capsys):
    """The SQL the stopped-container route sends is plain SQL: run it here."""
    import psycopg
    _run(capsys, "laptop")
    with psycopg.connect(pg_url, autocommit=True) as conn:
        listed = conn.execute(invite_cli.LIST_SQL).fetchone()[0]
        assert [row[0] for row in listed] == ["laptop"]
        assert conn.execute(invite_cli.revoke_sql("laptop")).fetchone() == ("laptop",)
        assert conn.execute(invite_cli.BANK_SQL).fetchone() == (BANK_ID,)
    with pytest.raises(ValueError):
        invite_cli.revoke_sql("x'; DROP TABLE principals; --")


def test_re_invites_keep_the_tier_and_board_unless_given(bank, pg_url, capsys):
    import psycopg
    _run(capsys, "laptop", "--tier", "core", "--no-board")
    _run(capsys, "laptop")                       # a pending re-invite
    assert _rows(pg_url)[0][3:] == ("core", False)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("UPDATE principals SET token_hash = %s, code_hash = NULL",
                     (secret_sha256("fixture-token"),))
    _run(capsys, "laptop", "--replace")
    assert _rows(pg_url)[0][3:] == ("core", False)
    _run(capsys, "laptop", "--replace", "--tier", "full", "--board")
    assert _rows(pg_url)[0][3:] == ("full", True)
