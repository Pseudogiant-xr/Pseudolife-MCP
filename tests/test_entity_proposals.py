import time
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401 (fixtures)
from pseudolife_memory.storage.postgres import PostgresStorage


@pytest.fixture()
def storage(pg_conn, pg_url):
    s = PostgresStorage(pg_url)
    yield s
    s.close()


def _ents(st):
    return (st.ensure_entity("daemon", display="daemon"),
            st.ensure_entity("live daemon", display="live daemon"),
            st.ensure_entity("2", display="2"))


def test_merge_proposal_insert_pending_accept(storage):
    a, b, _ = _ents(storage)
    pid = storage.insert_entity_proposal("merge", b, a, 0.99, "token-subset", time.time())
    assert pid is not None
    pend = storage.pending_entity_proposals()
    assert len(pend) == 1
    row = pend[0]
    assert row["kind"] == "merge" and row["entity"] == "live daemon" and row["into"] == "daemon"
    assert storage.set_entity_proposal_status(pid, "accepted") is True
    assert storage.pending_entity_proposals() == []


def test_junk_proposal_insert_and_get(storage):
    _, _, n = _ents(storage)
    pid = storage.insert_entity_proposal("junk", n, None, None, "bare-number", time.time())
    assert pid is not None
    got = storage.get_entity_proposal(pid)
    assert got["kind"] == "junk" and got["entity_id"] == n and got["into_id"] is None


def test_partial_unique_dedupe(storage):
    a, b, n = _ents(storage)
    first = storage.insert_entity_proposal("merge", b, a, 0.99, "x", time.time())
    rev = storage.insert_entity_proposal("merge", a, b, 0.99, "x", time.time())   # order-free dup
    assert first is not None and rev is None
    j1 = storage.insert_entity_proposal("junk", n, None, None, "x", time.time())
    j2 = storage.insert_entity_proposal("junk", n, None, None, "x", time.time())
    assert j1 is not None and j2 is None


def test_insert_entity_proposal_skips_dangling_fk(storage):
    # entity_id referencing a non-existent entity -> FK violation -> None, no raise,
    # and the connection stays usable (rollback recovered the transaction).
    a = storage.ensure_entity("real", display="real")
    assert storage.insert_entity_proposal("merge", 999999, a, 0.9, "x", time.time()) is None
    pid = storage.insert_entity_proposal("junk", a, None, None, "x", time.time())
    assert pid is not None
