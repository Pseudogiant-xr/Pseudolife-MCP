"""The daemon's storage log line names the PostgreSQL endpoint, never the
credential. A keyword-form DSN (``host=... password=... dbname=...``) has no
``@`` to split on, so the previous ``rsplit("@", 1)`` sanitizer logged the
whole string, password included (2026-10-08 incident: a reviewer printed it
from a private daemon log). ``evals/quarantine_replay.py`` wrote the same
split into its result file; both now use ``dsn_endpoint``."""

from pathlib import Path

import pytest

from pseudolife_memory import service as service_module
from pseudolife_memory.storage.schema import dsn_endpoint

SECRET = "hunter2-not-a-real-credential"
KEYWORD_DSN = f"host=127.0.0.1 port=5434 user=postgres password={SECRET} dbname=bench"
URL_DSN = f"postgresql://postgres:{SECRET}@127.0.0.1:5434/bench"


@pytest.mark.parametrize("dsn", [KEYWORD_DSN, URL_DSN])
def test_dsn_endpoint_names_endpoint_without_credential(dsn):
    summary = dsn_endpoint(dsn)
    assert summary == "127.0.0.1:5434/bench"
    assert SECRET not in summary


def test_dsn_endpoint_omits_absent_parts_and_never_echoes_input():
    assert dsn_endpoint("dbname=bench") == "<default host>/bench"
    assert dsn_endpoint("host=db.internal user=x") == "db.internal/<default dbname>"
    unparseable = f"=junk password={SECRET}"
    summary = dsn_endpoint(unparseable)
    assert SECRET not in summary and "junk" not in summary


class _FakeStorage:
    def __init__(self, dsn):
        self.dsn = dsn

    def close(self):
        pass


def test_storage_log_line_never_carries_keyword_dsn_password(monkeypatch, caplog):
    """Drive the real connect path with a fake store: the one INFO line it
    writes names the endpoint and nothing else."""
    import pseudolife_memory.storage.postgres as postgres_module

    monkeypatch.setattr(postgres_module, "PostgresStorage", _FakeStorage)
    svc = object.__new__(service_module.MemoryService)
    svc._storage = None
    svc._db_url = KEYWORD_DSN
    svc._init_refusal = svc._not_ready = "stale"
    svc._refuse_while_backing_off = lambda: None
    svc._assert_public_search_path = lambda: None
    with caplog.at_level("INFO", logger=service_module.logger.name):
        assert svc._ensure_postgres_storage() is svc._storage
    lines = [r.getMessage() for r in caplog.records if "storage: postgres" in r.getMessage()]
    assert lines == ["storage: postgres (127.0.0.1:5434/bench)"]
    assert SECRET not in caplog.text
    assert svc._init_refusal is None and svc._not_ready is None


def test_quarantine_replay_result_payload_uses_endpoint_summary():
    """The eval's result file carries the endpoint, not the DSN it was run
    with (a keyword DSN would otherwise land in a committed artifact)."""
    source = Path(__file__).resolve().parents[1] / "evals" / "quarantine_replay.py"
    text = source.read_text(encoding="utf-8")
    assert '"dsn_host": dsn_endpoint(args.dsn)' in text
    assert 'rsplit("@"' not in text
