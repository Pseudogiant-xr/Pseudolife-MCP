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

    store.storage.conn.execute("UPDATE coordination_events SET task='elsewhere' WHERE seq=2")
    code, output = cli("verify")
    assert (code, json.loads(output.out)) == (1, {"ok": False, "seq": 2, "reason": "hash_mismatch"})
    # Exported before the damage, the archive is still intact on its own.
    assert cli("verify", "--input", str(archive), "--expect-head", head)[0] == 0
    code, output = cli("verify", "--expect-head", "3:" + "0" * 64)
    assert (code, json.loads(output.out)["reason"]) == (1, "hash_mismatch")


def test_export_never_replaces_an_existing_file(store, cli, tmp_path):
    pair(store)
    target = tmp_path / "board-audit.jsonl"
    target.write_text("keep", encoding="utf-8")
    code, output = cli("export", "--out", str(target))
    assert code == 2 and "exists" in output.err
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("args", [("verify", "--expect-head", "three:abc"),
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


def test_failures_never_print_the_database_url(monkeypatch, capsys):
    from pseudolife_memory.board_audit_cli import main
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL",
                       "postgresql://auditor:hunter2@127.0.0.1:1/nowhere?connect_timeout=2")
    assert main(["verify"]) == 2
    output = capsys.readouterr()
    assert "hunter2" not in output.out + output.err
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL")
    assert main(["export"]) == 2
    assert "PSEUDOLIFE_MCP_DATABASE_URL" in capsys.readouterr().err


def test_the_console_script_routes_board_audit(monkeypatch):
    from pseudolife_memory import board_audit_cli, cli as console
    seen = []
    monkeypatch.setattr(board_audit_cli, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", "board-audit", "verify"])
    with pytest.raises(SystemExit) as exit_:
        console.main()
    assert (exit_.value.code, seen) == (0, [["verify"]])
    assert "board-audit" in console._USAGE
