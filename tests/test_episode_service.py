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
    # Give the session an entry so prune-on-empty-close doesn't delete it; this
    # test is about key matching, not the empty-prune path.
    service.store("a durable fact so sess-1 survives close")
    assert service.episode_end_session("other", run_dream=False) == {}   # no-op
    closed = service.episode_end_session("sess-1", run_dream=False)
    assert closed and closed["ended_at"] is not None
    assert service.episode_end_session("sess-1", run_dream=False) == {}   # nothing open


# ── Prune-on-empty-close + no-clobber (v2) ────────────────────────────────────


def test_empty_session_is_pruned_on_close(pristine_service):
    service = pristine_service
    service.episode_start_session("S1", "empty-proj")
    service.episode_end_session("S1", run_dream=False)   # nothing stored
    titles = [e["title"] for e in service.episode_list(include_open=True)["episodes"]]
    assert "empty-proj" not in titles                    # empty husk deleted


def test_nonempty_session_survives_close(pristine_service):
    service = pristine_service
    service.episode_start_session("S2", "real-proj")
    service.store("durable work in S2")                  # stamps the open leaf
    service.episode_end_session("S2", run_dream=False)
    titles = [e["title"] for e in service.episode_list(include_open=True)["episodes"]]
    assert "real-proj" in titles


def test_two_sessions_start_without_clobber(pristine_service):
    service = pristine_service
    a = service.episode_start_session("A", "proj-a")
    b = service.episode_start_session("B", "proj-b")
    assert a["ended_at"] is None and b["ended_at"] is None


def test_prune_empty_deletes_only_entryless_closed(pristine_service):
    service = pristine_service
    service.episode_start_session("KEEP", "has-entry")
    service.store("durable")                       # stamps the open leaf (KEEP)
    service.episode_end_session("KEEP", run_dream=False)   # survives (has entry)
    # A closed, entry-less husk (closed directly so prune-on-close doesn't run):
    service.episode_start_session("DROP", "empty")
    service._cms.episodes.end_session("DROP")
    out = service.episode_prune_empty(include_open=False)
    titles = [e["title"] for e in service.episode_list(include_open=True)["episodes"]]
    assert "has-entry" in titles
    assert "empty" not in titles
    assert out["deleted"] >= 1


def test_prune_empty_keeps_open_session_by_default(pristine_service):
    service = pristine_service
    service.episode_start_session("OPEN", "live-open")    # open, 0 entries
    service.episode_prune_empty(include_open=False)
    titles = [e["title"] for e in service.episode_list(include_open=True)["episodes"]]
    assert "live-open" in titles                          # not deleted while open


def test_episode_rest_start_and_end(pristine_service):
    from pseudolife_memory.web.routes import ConsoleRoutes
    service = pristine_service
    routes = ConsoleRoutes(service)
    started = routes.dispatch("POST", "/api/episode/start", {},
                              {"session_key": "s1", "title": "Sess"})
    assert started["session_key"] == "s1"
    service.store("a durable fact so the session is not pruned on close")
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
