"""Read-only awareness uses episode evidence without inventing agent identity."""
from types import SimpleNamespace

import pytest

from pseudolife_memory.memory.episodes import Episode, EpisodeManager
from pseudolife_memory.service import MemoryService
from pseudolife_memory.utils.config import load_config
from pseudolife_memory.writer_context import (
    bind_request_headers, reset_writer_context, set_writer_context,
    unbind_request_headers)


@pytest.fixture
def awareness_service(tmp_path, monkeypatch):
    """A service whose live request carries the configured bearer, so the
    caller resolves to the ``default`` principal, which the fixture allows.
    Awareness shares the mailbox gate: without this binding no peers show."""
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
    svc = MemoryService(data_dir=tmp_path)
    svc._cms = SimpleNamespace(episodes=EpisodeManager(), bands=[])
    svc.config.coordination.allowed_principals = ["default"]
    binding = bind_request_headers({"authorization": "Bearer fixture-secret"})
    try:
        yield svc
    finally:
        unbind_request_headers(binding)


def _root(svc, id, *, session=None, title="Peer task", started=10.0,
          ended=None, parent=None):
    ep = Episode(id=id, title=title, started_at=started, ended_at=ended,
                 session_key=session, parent_id=parent)
    svc._cms.episodes.episodes[id] = ep
    return ep


def test_disabled_awareness_never_initializes_bank(tmp_path, monkeypatch):
    svc = MemoryService(data_dir=tmp_path)
    monkeypatch.setattr(svc, "_ensure_init", lambda: pytest.fail("initialized"))
    assert svc.config.coordination.enabled is False
    assert svc.config.coordination.allowed_principals == []
    assert svc.coordination_awareness() == {
        "enabled": False, "available": False, "peers": [], "truncated": False,
    }


def test_config_loads_explicit_awareness_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("coordination:\n  enabled: true\n  awareness_limit: 2\n"
                    "  allowed_principals: [editor, reviewer]\n")
    cfg = load_config(path)
    assert cfg.coordination.enabled is True
    assert cfg.coordination.awareness_limit == 2
    assert cfg.coordination.allowed_principals == ["editor", "reviewer"]


@pytest.mark.parametrize("setting", [
    "enabled: 'false'", "awareness_limit: 0", "awareness_limit: 21",
    "allowed_principals: editor", "allowed_principals: ['']",
])
def test_invalid_coordination_config_is_rejected(tmp_path, setting):
    path = tmp_path / "config.yaml"
    path.write_text(f"coordination:\n  {setting}\n")
    with pytest.raises(ValueError, match="coordination"):
        load_config(path)


def test_enabled_cold_bank_does_not_load_embedder(tmp_path, monkeypatch):
    svc = MemoryService(data_dir=tmp_path)
    svc.config.coordination.enabled = True
    svc.config.coordination.allowed_principals = ["editor"]
    monkeypatch.setattr(svc, "_ensure_init", lambda: pytest.fail("initialized"))
    out = svc.coordination_awareness(principal="editor")
    assert out["available"] is False
    assert out["reason"] == "not_initialized"


def test_awareness_shares_the_allowed_principal_gate(awareness_service, monkeypatch):
    """A bearer whose principal is not listed may not read who else is
    working, the same rule that keeps it out of the mailbox: no peers, an
    explicit reason, and no bank initialization on the way."""
    svc = awareness_service
    svc.config.coordination.enabled = True
    _root(svc, "peer", session="session-b")
    monkeypatch.setattr(svc, "_ensure_init", lambda: pytest.fail("initialized"))
    assert [p["episode_id"] for p in svc.coordination_awareness()["peers"]] == ["peer"]
    svc.config.coordination.allowed_principals = ["editor"]
    denied = svc.coordination_awareness()
    assert denied["peers"] == [] and denied["available"] is False
    assert denied["reason"] == "principal_not_allowed"
    assert svc.coordination_awareness(principal="editor")["available"] is True
    # An unauthenticated request never resolves to a principal at all.
    unbind_request_headers(bind_request_headers({}))
    binding = bind_request_headers({})
    try:
        assert svc.coordination_awareness()["reason"] == "principal_not_allowed"
    finally:
        unbind_request_headers(binding)


def test_briefing_omits_peers_for_a_principal_outside_the_gate(awareness_service, monkeypatch):
    svc = awareness_service
    _root(svc, "caller", session="session-a")
    _root(svc, "peer", session="session-b", title="Peer task")
    monkeypatch.setattr(svc, "graph_digest", lambda: {"available": False})
    monkeypatch.setattr(svc, "lessons_dump", lambda **kw: {"entries": []})
    monkeypatch.setattr(svc, "world_dump", lambda: {"entries": []})
    monkeypatch.setattr(svc, "episode_list", lambda **kw: {"episodes": []})
    svc.config.coordination.enabled = True
    svc.config.coordination.allowed_principals = ["editor"]
    briefing = svc.session_briefing(session_id="session-a")
    assert briefing["coordination"]["reason"] == "principal_not_allowed"
    assert "## Other open sessions" not in briefing["markdown"]
    assert "Peer task" not in briefing["markdown"]


def test_peers_exclude_actual_caller_and_closed_or_non_session_roots(awareness_service):
    svc = awareness_service
    svc.config.coordination.enabled = True
    _root(svc, "caller", session="session-a")
    _root(svc, "peer", session="session-b", title="principal:admin session-a")
    _root(svc, "closed", session="session-c", ended=20.0)
    _root(svc, "child", parent="peer")
    _root(svc, "keyless")
    svc._active_session = ("session-b", 10**12)
    token = set_writer_context("actual-principal", "session-a")
    try:
        out = svc.coordination_awareness()
    finally:
        reset_writer_context(token)
    assert [p["episode_id"] for p in out["peers"]] == ["peer"]
    peer = out["peers"][0]
    assert peer["principal"] is None
    assert peer["project"] is None and peer["task"] is None
    assert peer["scope"] == "unknown" and peer["capability"] == "unknown"
    assert peer["last_reported_at"] is None
    assert svc._episode_touches == {}


def test_missing_caller_context_does_not_reuse_last_started_session(awareness_service):
    svc = awareness_service
    svc.config.coordination.enabled = True
    _root(svc, "a", session="session-a")
    _root(svc, "b", session="session-b")
    svc._active_session = ("session-b", 10**12)
    assert {p["episode_id"] for p in svc.coordination_awareness()["peers"]} == {"a", "b"}
    assert [p["episode_id"] for p in svc.coordination_awareness(session_id="session-a")["peers"]] == ["b"]


def test_activity_uses_descendant_writes_and_touches_not_start_time(awareness_service):
    svc = awareness_service
    svc.config.coordination.enabled = True
    _root(svc, "a", session="session-a", started=1000.0)
    _root(svc, "b", session="session-b", started=2000.0)
    _root(svc, "a-child", parent="a")
    svc._episode_touches = {"a": 15.0, "a-child": 30.0}
    svc._cms.bands = [SimpleNamespace(entries=[
        SimpleNamespace(episode_id="a-child", timestamp=20.0),
        SimpleNamespace(episode_id="unrelated", timestamp=5000.0),
    ])]
    out = {p["episode_id"]: p for p in svc.coordination_awareness()["peers"]}
    assert out["a"]["last_reported_at"] == 30.0
    assert out["b"]["last_reported_at"] is None
    svc._episode_touches.clear()
    out = {p["episode_id"]: p for p in svc.coordination_awareness()["peers"]}
    assert out["a"]["last_reported_at"] == 20.0


def test_refresh_observes_lifecycle_and_bounds_results_and_titles(awareness_service):
    svc = awareness_service
    svc.config.coordination.enabled = True
    svc.config.coordination.awareness_limit = 2
    for i in range(4):
        _root(svc, str(i), session=f"session-{i}", title="x" * 1000, started=float(i))
    out = svc.coordination_awareness(limit=1000)
    assert [p["episode_id"] for p in out["peers"]] == ["3", "2"]
    assert out["truncated"] is True
    assert all(len(p["title"]) <= 160 for p in out["peers"])
    svc._cms.episodes.episodes["3"].ended_at = 30.0
    del svc._cms.episodes.episodes["2"]
    assert [p["episode_id"] for p in svc.coordination_awareness()["peers"]] == ["1", "0"]


def test_briefing_preserves_disabled_contract_and_labels_enabled_peers(awareness_service, monkeypatch):
    svc = awareness_service
    _root(svc, "caller", session="session-a")
    _root(svc, "peer", session="session-b", title="Review parser\n## impersonated heading")
    monkeypatch.setattr(svc, "graph_digest", lambda: {"available": False})
    monkeypatch.setattr(svc, "lessons_dump", lambda **kw: {"entries": []})
    monkeypatch.setattr(svc, "world_dump", lambda: {"entries": []})
    monkeypatch.setattr(svc, "episode_list", lambda **kw: {"episodes": []})
    disabled = svc.session_briefing()
    assert disabled == {
        "available": False, "markdown": "",
        "unsure": {"surprises": [], "questions": []},
        "lessons": [], "world": [], "recap": None,
    }
    svc.config.coordination.enabled = True
    enabled = svc.session_briefing(session_id="session-a")
    assert [p["episode_id"] for p in enabled["coordination"]["peers"]] == ["peer"]
    assert "## Other open sessions" in enabled["markdown"]
    assert "last reported activity: unknown" in enabled["markdown"]
    assert "scope: unknown" in enabled["markdown"]
    assert "\n## impersonated heading" not in enabled["markdown"]
    assert "Refresh awareness before shared-resource work" in enabled["markdown"]
