import pseudolife_memory.episode_cli as ec


def test_daemon_down_is_silent_exit_zero(monkeypatch, capsys):
    # probe_health returning None == daemon down -> do nothing, never raise.
    monkeypatch.setattr(ec, "_daemon_url", lambda: "http://127.0.0.1:9", raising=False)
    monkeypatch.setattr(ec, "probe_health", lambda url: None, raising=False)
    ec.run_episode("episode-start", stdin_text='{"session_id":"abc","cwd":"/x"}')
    assert capsys.readouterr().out == ""


def test_parses_session_key_from_stdin(monkeypatch):
    captured = {}

    def fake_post(url, token, path, payload):
        captured["path"] = path
        captured["payload"] = payload

    monkeypatch.setattr(ec, "_daemon_url", lambda: "http://x", raising=False)
    monkeypatch.setattr(ec, "probe_health", lambda url: {"ok": True}, raising=False)
    monkeypatch.setattr(ec, "_post", fake_post, raising=False)
    ec.run_episode("episode-start",
                   stdin_text='{"session_id":"abc","cwd":"/home/u/Proj"}')
    assert captured["path"] == "/api/episode/start"
    assert captured["payload"]["session_key"] == "abc"
    assert "Proj" in captured["payload"]["title"]
