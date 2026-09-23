"""``ops/dedup_cortex.py`` never rewrites the bank on a dry run.

Its docstring always promised that a dry run "writes nothing", but the
script ended every run with ``svc.flush()``, a full snapshot: ``DELETE
FROM facts`` / ``world_facts`` / ``lessons`` and re-insert from the
script's own resident copy (fresh-eyes review 2026-09-23). Against a bank
a daemon was still serving, that silently reverted every write the daemon
made after the script hydrated. Now a dry run persists nothing, ``--apply``
saves only the slots it changed, and the script takes the bank writer
lease, so it refuses while a daemon holds the bank.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from pseudolife_memory.storage.postgres import PostgresStorage
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "dedup_cortex.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("dedup_cortex_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubService:
    instances: list["_StubService"] = []

    def __init__(self, **_kwargs) -> None:
        self.calls: list[str] = []
        _StubService.instances.append(self)

    def cortex_dedup(self, threshold: float, dry_run: bool) -> dict:
        self.calls.append("dedup")
        return {"dry_run": dry_run, "threshold": threshold,
                "clusters": [], "merged": 0}

    def flush(self):
        self.calls.append("flush")

    def autosave_if_changed(self):
        self.calls.append("autosave")


@pytest.mark.parametrize("argv, persisted", [
    ([], []),                      # dry run: nothing at all
    (["--apply"], ["autosave"]),   # apply: per-slot save, never a full rewrite
])
def test_only_apply_persists_and_never_by_full_rewrite(monkeypatch, argv, persisted):
    script = _load_script()
    monkeypatch.setattr(script, "MemoryService", _StubService)
    monkeypatch.setattr(sys, "argv", ["dedup_cortex.py", *argv])
    script.main()
    svc = _StubService.instances[-1]
    assert [c for c in svc.calls if c != "dedup"] == persisted


def _facts(conn) -> list[tuple]:
    rows = conn.execute(
        "SELECT id, entity, attribute, value, status FROM facts ORDER BY id"
    ).fetchall()
    conn.commit()  # release the read's lock before the script's schema DDL
    return rows


def _seed(pg_url, tmp_path) -> None:
    from pseudolife_memory.service import MemoryService

    seed = MemoryService(data_dir=tmp_path / "seed", database_url=pg_url)
    seed.cortex_write("payments-db", "host", "db1.example.com")
    seed.cortex_write("svc", "port", "8080")
    seed._storage.close()  # noqa: SLF001 — the writer lease is free again


def test_dry_run_leaves_every_fact_row_untouched(pg_conn, pg_url, tmp_path,
                                                  monkeypatch):
    _seed(pg_url, tmp_path)
    before = _facts(pg_conn)
    assert before

    script = _load_script()
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path / "script"))
    monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
    monkeypatch.setattr(sys, "argv", ["dedup_cortex.py"])
    script.main()

    # Same rows, same ids: a full snapshot would have deleted and
    # re-inserted them under fresh ids.
    assert _facts(pg_conn) == before


def test_refuses_while_another_writer_holds_the_bank(pg_conn, pg_url, tmp_path,
                                                     monkeypatch, capsys):
    _seed(pg_url, tmp_path)
    before = _facts(pg_conn)
    daemon = PostgresStorage(pg_url)  # the running daemon's storage
    try:
        script = _load_script()
        monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
        monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path / "script"))
        monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
        monkeypatch.setattr(sys, "argv", ["dedup_cortex.py", "--apply"])
        with pytest.raises(SystemExit) as exited:
            script.main()
        assert exited.value.code == 2
        assert "writer lease" in capsys.readouterr().err
        assert _facts(pg_conn) == before
    finally:
        daemon.close()
