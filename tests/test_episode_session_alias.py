"""The board's host session id is a usable episode anchor without hooks."""
import pytest

from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from pseudolife_memory.writer_context import reset_writer_context, set_writer_context


SESSION = "01234567-89ab-4cde-8fab-0123456789ab"


@pytest.mark.parametrize("operation", ["store", "outcome", "title", "subepisode"])
def test_board_session_id_resolves_open_root(pg_service, operation):
    svc = pg_service
    root = svc.episode_start_session(SESSION, "A named session")
    svc.episode_start_session("another-session", "Another session")
    if operation == "store":
        result = svc.store("The backup is scheduled nightly.", episode=SESSION)
        entry = next(e for band in svc._cms.bands for e in band.entries
                     if e.text == "The backup is scheduled nightly.")
        assert entry.episode_id == root["id"]
    elif operation == "outcome":
        result = svc.record_outcome(task="session alias", outcome="success", episode=SESSION)
        signal = next(s for s in svc._storage.pending_signals(limit=100)
                      if s["task"] == "session alias")
        assert signal["episode_id"] == root["id"]
    elif operation == "title":
        result = svc.set_session_title("Renamed through the board id", episode=SESSION)
        assert result["ok"] and result["id"] == root["id"]
    else:
        result = svc.episode_start("A task", episode=SESSION)
        assert result["parent_id"] == root["id"]
        assert svc.episode_end(episode=SESSION)["id"] == result["id"]
    assert "episode_warning" not in result


def test_pruned_closed_board_alias_is_refused_even_with_own_header(pg_service):
    root = pg_service.episode_start_session(SESSION, "Empty session")
    pg_service.episode_end_session(SESSION, run_dream=False)
    assert pg_service._cms.episodes.get(root["id"]) is None
    token = set_writer_context("codex", SESSION)
    try:
        result = pg_service.set_session_title("Must stay closed", episode=SESSION)
    finally:
        reset_writer_context(token)
    assert result == {"ok": False, "reason": "No open episode matches this session id; call memory_session_title without episode, then retry."}
    assert not any(e.session_key == SESSION
                   for e in pg_service._cms.episodes.episodes.values())


def test_closed_board_alias_is_refused_without_reopening(pg_service):
    root = pg_service.episode_start_session(SESSION, "Closed session")
    pg_service.store("Keep this closed session in history.", episode=root["id"])
    pg_service.episode_end_session(SESSION, run_dream=False)
    assert pg_service.set_session_title("Must stay closed", episode=SESSION) == {
        "ok": False, "reason": "This session's episode is closed; call memory_session_title without episode to start a new episode."}
    assert pg_service._cms.episodes.get(root["id"]).ended_at is not None


def test_session_id_prefix_is_not_an_alias(pg_service):
    pg_service.episode_start_session(SESSION, "A named session")
    assert pg_service.set_session_title("Wrong prefix", episode=SESSION[:12])["ok"] is False


def test_unknown_alias_cannot_open_someone_elses_session(pg_service):
    token = set_writer_context("codex", "different-session")
    try:
        result = pg_service.set_session_title("Foreign", episode=SESSION)
    finally:
        reset_writer_context(token)
    assert result["ok"] is False
    assert not any(e.session_key == SESSION
                   for e in pg_service._cms.episodes.episodes.values())


@pytest.mark.parametrize("operation", ["store", "outcome"])
def test_first_hookless_write_names_the_bootstrap(pg_service, operation):
    token = set_writer_context("codex", SESSION)
    try:
        if operation == "store":
            result = pg_service.store("Nightly backups use the archive disk.", episode=SESSION)
        else:
            result = pg_service.record_outcome(task="first hookless outcome", outcome="success",
                                               episode=SESSION)
        named = pg_service.set_session_title("A hookless session")
        retry = pg_service.record_outcome(task="after bootstrap", outcome="success", episode=SESSION)
    finally:
        reset_writer_context(token)
    assert result["episode_warning"] == (
        "No open episode matches this session id; call memory_session_title without episode, then retry.")
    assert named["ok"] and "episode_warning" not in retry
    signal = next(s for s in pg_service._storage.pending_signals(limit=100)
                  if s["task"] == "after bootstrap")
    assert signal["episode_id"] == named["id"]
