"""CPU-only tests for deterministic disposable-bank dumps."""

import json

from rust.cli_harness.rows import _bank


class _FakeConnection:
    def __init__(self, reverse: bool) -> None:
        self.reverse = reverse

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def _server_order(self, rows):
        return list(reversed(rows)) if self.reverse else list(rows)

    def execute(self, query: str):
        if "FROM pg_tables" in query:
            rows = [("coordination_lease_waiters",), ("coordination_leases",)]
        elif "AS j FROM public." in query:
            table = query.split('public."', 1)[1].split('"', 1)[0]
            rows = [(value,) for value in {
                "coordination_lease_waiters": ['{"id":2}', '{"id":1}'],
                "coordination_leases": ['{"id":4}', '{"id":3}'],
            }[table]]
        elif "FROM pg_sequences" in query:
            rows = [("coordination_lease_waiters_id_seq", 2),
                    ("coordination_leases_id_seq", 4)]
        elif "FROM information_schema.columns" in query:
            rows = [
                ("coordination_lease_waiters", "id", "bigint", "int8", "NO", None),
                ("coordination_leases", "id", "bigint", "int8", "NO", None),
            ]
        elif "FROM pg_indexes" in query:
            rows = [
                ("coordination_lease_waiters", "coordination_lease_waiters_pkey", "CREATE UNIQUE INDEX ..."),
                ("coordination_leases", "coordination_leases_pkey", "CREATE UNIQUE INDEX ..."),
            ]
        elif "FROM pg_constraint" in query:
            rows = [
                ("coordination_lease_waiters", "coordination_lease_waiters_pkey", "PRIMARY KEY (id)"),
                ("coordination_leases", "coordination_leases_pkey", "PRIMARY KEY (id)"),
            ]
        else:
            raise AssertionError(f"unexpected query: {query}")
        return self._server_order(rows)


def test_dump_is_independent_of_server_collation_order(monkeypatch):
    def serialized_dump(reverse: bool) -> str:
        monkeypatch.setattr(_bank, "_connect", lambda _name: _FakeConnection(reverse))
        return json.dumps(_bank.dump("pl_cf_fake"))

    assert serialized_dump(False) == serialized_dump(True)
def test_fixture_diagnostic_redacts_connection_credentials():
    from .lease_ci import diagnostic
    error = RuntimeError("initdb failed: postgresql://fixture:synthetic-password@127.0.0.1/db "
                         "password='synthetic-password' token=synthetic-token")
    detail = diagnostic(error)
    assert "initdb failed" in detail
    assert "synthetic-password" not in detail
    assert "synthetic-token" not in detail
