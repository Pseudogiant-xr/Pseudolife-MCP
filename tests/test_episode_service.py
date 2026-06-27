from dataclasses import asdict

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.memory.episodes import Episode
from pseudolife_memory.storage.postgres import PostgresStorage


def test_episode_session_key_round_trips(pg_conn, pg_url):
    storage = PostgresStorage(pg_url)
    ep = Episode(id="e1", title="S", started_at=1.0, session_key="sess-xyz")
    storage.upsert_episode(asdict(ep))           # episode_row(ep) == asdict(ep)
    rows = {r["id"]: r for r in storage.load_episodes()}
    assert rows["e1"]["session_key"] == "sess-xyz"
