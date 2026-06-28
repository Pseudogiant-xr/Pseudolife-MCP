import time
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401 (fixtures)
from pseudolife_memory.storage.postgres import PostgresStorage


@pytest.fixture()
def storage(pg_conn, pg_url):
    s = PostgresStorage(pg_url)
    yield s
    s.close()


def _two_entities(st):
    a = st.ensure_entity("alpha", display="alpha")
    b = st.ensure_entity("beta", display="beta")
    return a, b


def test_insert_then_pending_then_accept(storage):
    a, b = _two_entities(storage)
    pid = storage.insert_proposal(a, "related-to", b, 0.45, 0.91, "why", "deep-dream", time.time())
    assert pid is not None
    pend = storage.pending_proposals()
    assert len(pend) == 1 and pend[0]["src"] == "alpha" and pend[0]["dst"] == "beta"
    assert storage.set_proposal_status(pid, "accepted") is True
    assert storage.pending_proposals() == []


def test_insert_is_idempotent_on_triple(storage):
    a, b = _two_entities(storage)
    first = storage.insert_proposal(a, "related-to", b, 0.45, 0.9, "x", "deep-dream", time.time())
    dup = storage.insert_proposal(a, "related-to", b, 0.45, 0.9, "x", "deep-dream", time.time())
    assert first is not None and dup is None


def test_traces_by_entity_norm_returns_dict(storage):
    assert isinstance(storage.traces_by_entity_norm(), dict)
