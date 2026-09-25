"""``pseudolife-mcp board-audit``: the operator's view of the log.

Export is JSON lines on stdout (or a new file); verify prints one report and
exits 0 intact, 1 on tamper evidence, 2 when it could not check at all;
redact (v46) removes one body, printing one result: 0 done, 1 refused, 2 could
not run.
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
    assert by_task[1]["body"] == "from t1 ✓"
    assert "text" not in by_task[1]["payload"] and by_task[1]["payload"]["text_bytes"] == 11
    assert by_task[0]["body"] is None
    assert set(by_task[1]) == {"seq", "event", "actor", "principal", "agent_id",
                               "recipient_agent_id", "project", "task", "message_id",
                               "payload", "created_at", "hlc", "prev_hash", "hash", "body"}
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
    # argparse's own refusal, not a later failure that also exits 2.
    assert code == 2 and output.out == "" and "usage:" in output.err


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


# ── redaction (schema v46) ────────────────────────────────────────────────


def _send(store, text="pasted by mistake"):
    a, b = pair(store)
    return store.send(*creds(a), to=b["agent_id"], text=text, request_id="r")["message_id"]


def test_redact_removes_a_body_prints_json_and_the_chain_still_verifies(store, cli, tmp_path):
    message_id = _send(store)
    code, output = cli("redact", "--message-id", message_id, "--reason", "wrong paste")
    assert code == 0, output.err
    result = json.loads(output.out)
    head = f"4:{result['redact_hash']}"
    assert result == {"ok": True, "message_id": message_id, "seq": 3, "redact_seq": 4,
                      "redact_hash": result["redact_hash"], "expect_head": head,
                      "live_body_cleared": True, "audit_copy": "removed", "vacuumed": True}
    # The operator is told to keep the new head, which catches a later
    # removal of the redact row itself.
    assert f"--expect-head {head}" in output.err
    assert cli("verify", "--expect-head", head)[0] == 0
    archive = tmp_path / "board-audit.jsonl"
    assert cli("export", "--out", str(archive))[0] == 0
    events = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
    assert [(e["event"], e["body"]) for e in events[2:]] == [("send", None), ("redact", None)]
    assert events[3]["payload"] == {"message_id": message_id, "seq": 3, "reason": "wrong paste",
                                    "audit_copy": "removed"}
    assert "pasted by mistake" not in archive.read_text(encoding="utf-8")
    code, output = cli("verify")
    assert code == 0 and json.loads(output.out)["ok"]
    assert cli("verify", "--input", str(archive))[0] == 0

    code, output = cli("redact", "--message-id", message_id, "--reason", "again")
    assert code == 1
    assert json.loads(output.out) == {"ok": False, "message_id": message_id,
                                      "reason": "already_redacted"}
    assert "already" in output.err


def test_redact_refuses_a_body_written_before_v46_and_says_why(store, cli):
    from tests.test_coordination_audit import _legacy_send
    a, b = pair(store)
    message_id = _legacy_send(store, a, b)
    code, output = cli("redact", "--message-id", message_id, "--reason", "too old")
    assert code == 1
    assert json.loads(output.out)["reason"] == "body_in_hashed_payload"
    assert "before schema v46" in output.err and "an old body" not in output.out + output.err
    code, output = cli("redact", "--message-id", "0" * 32, "--reason", "unknown")
    assert (code, json.loads(output.out)["reason"]) == (1, "message_not_found")


def test_redacting_a_live_pre_v46_message_says_the_audit_copy_stays(store, cli, monkeypatch):
    from tests.test_coordination_audit import _sent_by_v45
    a, b = pair(store)
    message_id = _sent_by_v45(monkeypatch, store, a, b, "sent by a v45 daemon")["message_id"]
    code, output = cli("redact", "--message-id", message_id, "--reason", "pasted by mistake")
    assert code == 0, output.err
    result = json.loads(output.out)
    assert (result["audit_copy"], result["live_body_cleared"]) == ("kept", True)
    assert "audit log keeps" in output.err and "sent by a v45 daemon" not in output.err


def test_redact_vacuums_after_its_commit_and_a_failed_vacuum_does_not_undo_it(
        store, cli, pg_url, monkeypatch):
    """Redaction leaves the old row versions in the table files until a
    vacuum frees them, so the CLI vacuums both tables once the redaction has
    committed; the vacuum failing is reported, never taken as a failed
    redaction."""
    import psycopg
    from pseudolife_memory import board_audit_cli
    seen = []

    def spy(conn):
        with psycopg.connect(pg_url, autocommit=True) as other:
            other.execute("SET search_path TO public")
            seen.append(other.execute("SELECT count(*) FROM coordination_events "
                                      "WHERE event='redact'").fetchone()[0])
        raise psycopg.errors.LockNotAvailable("simulated")

    monkeypatch.setattr(board_audit_cli, "_vacuum", spy)
    message_id = _send(store)
    code, output = cli("redact", "--message-id", message_id, "--reason", "wrong paste")
    assert code == 0 and seen == [1]                  # committed before the vacuum ran
    assert json.loads(output.out)["vacuumed"] is False
    assert "vacuum" in output.err.lower()


def test_a_busy_board_is_reported_as_busy_not_as_a_database_failure(store, cli, pg_url):
    import psycopg
    message_id = _send(store)
    with psycopg.connect(pg_url) as holder:
        holder.execute("SET search_path TO public")
        holder.execute("SELECT 1 FROM coordination_messages WHERE message_id=%s FOR UPDATE",
                       (message_id,))
        code, output = cli("redact", "--message-id", message_id, "--reason", "wrong paste")
        holder.rollback()
    assert code == 2 and output.out == ""
    assert "busy" in output.err and "PSEUDOLIFE_MCP_DATABASE_URL" not in output.err
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_events WHERE event='redact'").fetchone() == (0,)


def test_an_open_export_snapshot_does_not_block_the_daemons_schema_pass(store, pg_url):
    """export and verify hold a read lock on the log for their whole
    snapshot, and every daemon start runs the schema pass under a 5 s lock
    timeout, so nothing in that pass may need a lock that conflicts with it
    once the log exists: an ``export | less`` left open kept a v46 daemon
    from starting (review, 2026-09-26)."""
    import psycopg
    from pseudolife_memory.storage.coordination import audit_events
    from pseudolife_memory.storage.schema import ensure_schema
    pair(store)
    reader = psycopg.connect(pg_url)
    try:
        reader.read_only = True
        reader.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        reader.execute("SET search_path TO public")
        reader.commit()
        rows = audit_events(reader)
        next(rows)                                      # the snapshot is open
        with psycopg.connect(pg_url, autocommit=True) as daemon:
            daemon.execute("SET search_path TO public")
            ensure_schema(daemon)
        rows.close()
    finally:
        reader.close()


def test_an_archive_with_a_duplicate_key_cannot_be_checked(store, cli, tmp_path):
    """A JSON object may repeat a key and a reader keeps the last one, so a
    line could show one body to a person and verify another."""
    _send(store, "the body")
    archive = tmp_path / "board-audit.jsonl"
    assert cli("export", "--out", str(archive))[0] == 0
    lines = archive.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace('"body":"the body"', '"body":"EVIL","body":"the body"', 1)
    assert '"body":"EVIL"' in lines[2]
    archive.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, output = cli("verify", "--input", str(archive))
    assert code == 2 and "line 3" in output.err and "duplicate" in output.err


@pytest.mark.parametrize("args", [
    ("redact", "--reason", "no id"),
    ("redact", "--message-id", "0" * 32),
])
def test_redact_needs_both_a_message_id_and_a_reason(cli, args):
    code, output = cli(*args)
    assert code == 2 and output.out == "" and "usage:" in output.err


def test_a_redaction_reason_is_checked_before_anything_is_written(store, cli):
    message_id = _send(store)
    for reason, code in (("bell\x07", "invalid_reason"), ("x" * 241, "invalid_reason"),
                         ("token=" + "q7Hd2kLm9Pz4" + "Rt6Wv8Xy1Bc3", "secret_like_body")):
        exit_code, output = cli("redact", "--message-id", message_id, "--reason", reason)
        assert (exit_code, json.loads(output.out)["reason"]) == (1, code)
        assert reason not in output.out + output.err
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_events WHERE event='redact'").fetchone() == (0,)


def test_verify_input_names_a_tampered_or_stripped_body_in_an_export(store, cli, tmp_path):
    _send(store, "the body")
    archive = tmp_path / "board-audit.jsonl"
    assert cli("export", "--out", str(archive))[0] == 0
    rows = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]

    def verify_rows(rows, name):
        path = tmp_path / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        code, output = cli("verify", "--input", str(path))
        return code, json.loads(output.out) if output.out else output.err

    edited = [dict(row) for row in rows]
    edited[2]["body"] = "another body"
    assert verify_rows(edited, "edited.jsonl") == (
        1, {"ok": False, "seq": 3, "reason": "body_mismatch"})
    stripped = [{k: v for k, v in row.items() if k != "body"} for row in rows]
    assert verify_rows(stripped, "stripped.jsonl") == (
        1, {"ok": False, "seq": 3, "reason": "body_not_exported"})
    blanked = [dict(row) for row in rows]
    blanked[2]["body"] = None
    assert verify_rows(blanked, "blanked.jsonl") == (
        1, {"ok": False, "seq": 3, "reason": "body_missing"})
    wrong_type = [dict(row) for row in rows]
    wrong_type[2]["body"] = 7
    code, err = verify_rows(wrong_type, "wrong-type.jsonl")
    assert code == 2 and "line 3 is not an exported audit event" in err


def test_an_export_written_before_v46_still_verifies(store, cli, tmp_path):
    """Exports from v42-v45 have no body field; their sends carry the body
    inside the hashed payload, and they must keep verifying."""
    from tests.test_coordination_audit import _legacy_send
    a, b = pair(store)
    _legacy_send(store, a, b)
    store.update(*creds(a), status="later")
    archive = tmp_path / "board-audit.jsonl"
    assert cli("export", "--out", str(archive))[0] == 0
    rows = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
    old = tmp_path / "v45.jsonl"
    old.write_text("".join(json.dumps({k: v for k, v in row.items() if k != "body"}) + "\n"
                           for row in rows), encoding="utf-8")
    code, output = cli("verify", "--input", str(old))
    assert code == 0 and json.loads(output.out)["events"] == 4


def test_a_bank_before_v46_exports_and_verifies_but_cannot_redact(store, cli):
    """A restored v42-v45 bank, read before any v46 daemon has started."""
    from pseudolife_memory.storage.schema import COORDINATION_SCHEMA_SQL
    from tests.test_coordination_audit import _legacy_send
    a, b = pair(store)
    message_id = _legacy_send(store, a, b)
    store.storage.conn.execute("ALTER TABLE coordination_events DROP COLUMN body")
    try:
        code, output = cli("export")
        assert code == 0 and [e["body"] for e in lines(output)] == [None, None, None]
        code, output = cli("verify")
        assert code == 0 and json.loads(output.out)["events"] == 3
        code, output = cli("redact", "--message-id", message_id, "--reason", "why")
        assert code == 2 and output.out == "" and "v46" in output.err
    finally:
        store.storage.conn.execute(COORDINATION_SCHEMA_SQL)


def test_the_usage_names_redaction():
    from pseudolife_memory import cli as console
    line = next(line for line in console._USAGE.splitlines() if "board-audit" in line)
    assert "redact" in line
