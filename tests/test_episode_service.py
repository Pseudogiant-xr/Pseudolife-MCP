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


def test_session_start_is_idempotent_per_key(pristine_service):
    service = pristine_service
    a = service.episode_start_session("sess-1", "Session A")
    b = service.episode_start_session("sess-1", "Session A")   # re-fire
    assert a["id"] == b["id"]                                   # no second episode


def test_session_end_matches_key_only(pristine_service):
    service = pristine_service
    service.episode_start_session("sess-1", "Session A")
    assert service.episode_end_session("other", run_dream=False) == {}   # no-op
    closed = service.episode_end_session("sess-1", run_dream=False)
    assert closed and closed["ended_at"] is not None
    assert service.episode_end_session("sess-1", run_dream=False) == {}   # nothing open


def test_episode_rest_start_and_end(pristine_service):
    from pseudolife_memory.web.routes import ConsoleRoutes
    service = pristine_service
    routes = ConsoleRoutes(service)
    started = routes.dispatch("POST", "/api/episode/start", {},
                              {"session_key": "s1", "title": "Sess"})
    assert started["session_key"] == "s1"
    ended = routes.dispatch("POST", "/api/episode/end", {},
                            {"session_key": "s1", "run_dream": False})
    assert ended["ended_at"] is not None


def test_agent_episode_nests_under_session(pristine_service):
    service = pristine_service
    service.episode_start_session("s1", "Session")
    sub = service.episode_start("Big task")           # agent sub-episode
    assert sub["parent_id"] is not None
    # storing now stamps the sub-episode (the leaf)
    service.store("did a thing")  # returns entry/ack; episode stamped internally
    closed = service.episode_end_session("s1", run_dream=False)
    assert closed and closed["parent_id"] is None      # the root was closed


def test_search_episode_filter_includes_child_episodes(pristine_service):
    service = pristine_service
    root = service.episode_start_session("s1", "Session")
    sub = service.episode_start("Sub")                       # nests under root
    service.store("alpha beta gamma", source="pseudolife")   # stamped to sub
    hits = service.search("alpha beta gamma", episodes=[root["id"]])
    texts = [e["text"] for e in hits.get("entries", [])]
    assert any("alpha beta gamma" in t for t in texts)
