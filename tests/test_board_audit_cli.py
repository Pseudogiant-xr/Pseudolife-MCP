"""``pseudolife-mcp board-audit``: the operator's read-only view of the log.

Export is JSON lines on stdout (or a new file); verify prints one report and
exits 0 intact, 1 on tamper evidence, 2 when it could not check at all.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_coordination_storage import creds, pair, store  # noqa: F401


@pytest.fixture
def cli(pg_url, monkeypatch, capsys):
    from pseudolife_memory.board_audit_cli import main
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)

    def run(*args):
        code = main(list(args))
        return code, capsys.readouterr()
    return run


def lines(output):
    return [json.loads(line) for line in output.out.splitlines()]


def test_export_writes_json_lines_filtered_by_task_agent_and_time(store, cli):
    a = store.register("alice", project="p", task="t1")
    b = store.register("alice", project="p", task="t2")
    store.test_time[0] = 2000.0
    store.send(*creds(a), to=b["agent_id"], text="from t1 ✓", request_id="1")
    store.send(*creds(b), to=a["agent_id"], text="from t2", request_id="2")

    def export(*args):
        code, output = cli("export", *args)
        assert code == 0, output.err
        return lines(output)

    by_task = export("--task", "t1")
    assert [(e["event"], e["task"]) for e in by_task] == [("register", "t1"), ("send", "t1")]
    assert by_task[1]["payload"]["text"] == "from t1 ✓"
    assert set(by_task[1]) == {"seq", "event", "actor", "principal", "agent_id",
                               "recipient_agent_id", "project", "task", "message_id",
                               "payload", "created_at", "hlc", "prev_hash", "hash"}
    assert [e["event"] for e in export("--agent", b["agent_id"])] == ["register", "send", "send"]
    assert [e["seq"] for e in export("--project", "p", "--since", "2000")] == [3, 4]
    assert [e["seq"] for e in export("--until", "2000")] == [1, 2]
    iso = datetime.fromtimestamp(2000, timezone.utc).isoformat()
    assert [e["seq"] for e in export("--since", iso)] == [3, 4]


def test_verify_prints_the_head_and_fails_on_tampering_but_an_archive_still_verifies(
        store, cli, tmp_path):
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="naïve ✓", request_id="r")
    code, output = cli("verify")
    report = json.loads(output.out)
    assert code == 0 and report["ok"] and report["head_seq"] == 3
    archive = tmp_path / "board-audit.jsonl"
    assert cli("export", "--out", str(archive))[0] == 0
    head = f"{report['head_seq']}:{report['head_hash']}"
    assert cli("verify", "--input", str(archive), "--expect-head", head)[0] == 0

    code, output = cli("verify", "--expect-head", "3:" + "0" * 64)
    assert (code, json.loads(output.out)["reason"]) == (1, "head_mismatch")
    code, output = cli("verify", "--expect-head", "9:" + report["head_hash"])
    assert (code, json.loads(output.out)["reason"]) == (1, "head_missing")

    store.storage.conn.execute("UPDATE coordination_events SET task='elsewhere' WHERE seq=2")
    code, output = cli("verify")
    assert (code, json.loads(output.out)) == (1, {"ok": False, "seq": 2, "reason": "hash_mismatch"})
    # Exported before the damage, the archive is still intact on its own.
    assert cli("verify", "--input", str(archive), "--expect-head", head)[0] == 0


def test_export_never_replaces_an_existing_file(store, cli, tmp_path):
    pair(store)
    target = tmp_path / "board-audit.jsonl"
    target.write_text("keep", encoding="utf-8")
    code, output = cli("export", "--out", str(target))
    assert code == 2 and "exists" in output.err
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("args", [("verify", "--expect-head", "three:abc"),
                                  ("verify", "--expect-head", "0:" + "0" * 64),
                                  ("export", "--since", "yesterday"),
                                  ("export", "--until", "nan")])
def test_malformed_arguments_are_refused_before_connecting(cli, args):
    code, output = cli(*args)
    assert code == 2 and output.out == ""


def test_an_archive_line_that_is_not_an_exported_event_cannot_be_checked(store, cli, tmp_path):
    pair(store)
    archive = tmp_path / "board-audit.jsonl"
    assert cli("export", "--out", str(archive))[0] == 0
    first, rest = archive.read_text(encoding="utf-8").split("\n", 1)
    row = json.loads(first)
    row["seq"] = "1"
    archive.write_text(json.dumps(row) + "\n" + rest, encoding="utf-8")
    code, output = cli("verify", "--input", str(archive))
    assert code == 2 and output.out == ""
    assert "line 1 is not an exported audit event" in output.err


@pytest.mark.parametrize("dsn", [
    "postgresql://auditor:hunter2@127.0.0.1:1/nowhere?connect_timeout=2",
    # psycopg echoes an unparseable DSN in its own error text.
    "hunter2",
])
def test_failures_never_print_the_database_url(monkeypatch, capsys, dsn):
    from pseudolife_memory.board_audit_cli import main
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", dsn)
    assert main(["verify"]) == 2
    output = capsys.readouterr()
    assert "hunter2" not in output.out + output.err


def test_without_a_database_url_or_a_lite_bank_it_says_what_is_missing(
        monkeypatch, capsys, tmp_path):
    from pseudolife_memory.board_audit_cli import main
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    assert main(["export"]) == 2
    assert "PSEUDOLIFE_MCP_DATABASE_URL" in capsys.readouterr().err


def test_a_bank_without_the_audit_log_cannot_be_checked(store, cli):
    store.storage.conn.execute("DROP TABLE coordination_events")
    code, output = cli("verify")
    assert code == 2 and output.out == "" and "no audit log" in output.err


def test_a_failed_export_removes_the_file_it_created(store, cli, tmp_path, monkeypatch):
    from pseudolife_memory import board_audit_cli
    pair(store)
    real = board_audit_cli.audit_events

    def breaks_after_one(conn, **filters):
        rows = real(conn, **filters)
        yield next(rows)
        rows.close()
        raise RuntimeError("connection lost mid-export")

    monkeypatch.setattr(board_audit_cli, "audit_events", breaks_after_one)
    target = tmp_path / "board-audit.jsonl"
    code, output = cli("export", "--out", str(target))
    assert code == 2 and not target.exists()


def test_a_write_that_fails_midway_removes_the_file_and_says_so(store, cli, tmp_path, monkeypatch):
    """A full disk or a dropped share: the first close re-raises the failed
    flush, which must not skip the cleanup or be blamed on the bank."""
    import errno
    from pathlib import Path
    pair(store)
    real_open = Path.open

    class Failing:
        def __init__(self, real):
            self.real = real

        def write(self, text):
            raise OSError(errno.ENOSPC, "No space left on device")

        flush = write

        def close(self):
            if not self.real.closed:
                self.real.close()
                raise OSError(errno.ENOSPC, "No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def opening(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        return Failing(handle) if mode == "x" else handle

    monkeypatch.setattr(Path, "open", opening)
    target = tmp_path / "board-audit.jsonl"
    code, output = cli("export", "--out", str(target))
    assert code == 2 and not target.exists()
    assert "cannot write" in output.err and "database" not in output.err


def test_the_lite_bank_is_used_and_its_instance_stopped_on_every_path(store, cli, pg_url,
                                                                      monkeypatch):
    from pseudolife_memory import transfer_cli
    stopped = []

    class Instance:
        def stop(self):
            stopped.append(1)

    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL")
    monkeypatch.setattr(transfer_cli, "_resolve_dsn", lambda data_dir: (pg_url, Instance()))
    pair(store)
    assert cli("verify")[0] == 0
    store.storage.conn.execute("DROP TABLE coordination_events")
    code, output = cli("verify")
    assert code == 2 and "no audit log" in output.err
    assert stopped == [1, 1]


def test_an_archive_that_is_not_utf8_is_named(cli, tmp_path):
    archive = tmp_path / "board-audit.jsonl"
    archive.write_text('{"seq": 1}\n', encoding="utf-16")
    code, output = cli("verify", "--input", str(archive))
    assert code == 2 and "not UTF-8" in output.err


def test_an_output_directory_that_does_not_exist_is_named_not_blamed_on_the_bank(
        store, cli, tmp_path):
    pair(store)
    target = tmp_path / "missing" / "board-audit.jsonl"
    code, output = cli("export", "--out", str(target))
    assert code == 2 and "cannot create" in output.err and "database" not in output.err


def test_a_reader_that_closes_early_is_not_reported_as_a_database_failure(
        store, cli, monkeypatch):
    """``export | head`` on Windows fails the write with EINVAL, not EPIPE."""
    import errno
    import io
    pair(store)

    class Closed(io.StringIO):
        def write(self, text):
            raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr("sys.stdout", Closed())
    code, output = cli("export")
    assert code == 2 and "database" not in output.err


def test_the_console_script_routes_board_audit(monkeypatch):
    from pseudolife_memory import board_audit_cli, cli as console
    seen = []
    monkeypatch.setattr(board_audit_cli, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", "board-audit", "verify"])
    with pytest.raises(SystemExit) as exit_:
        console.main()
    assert (exit_.value.code, seen) == (0, [["verify"]])
    assert "board-audit" in console._USAGE
