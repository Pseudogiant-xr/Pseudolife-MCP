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


def test_title_uses_git_repo_root_from_subdir(tmp_path):
    # cwd is a nested subdir of a git repo -> title is the REPO ROOT name.
    repo = tmp_path / "MyProject"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    assert ec._title_from_cwd(str(sub)).startswith("MyProject - ")


def test_title_ignores_home_dir(tmp_path, monkeypatch):
    # SessionStart sometimes fires with cwd=home; don't title after the
    # home-dir basename (the noisy "<user> - <date>" case).
    home = tmp_path / "home" / "someuser"
    home.mkdir(parents=True)
    monkeypatch.setattr(ec.os.path, "expanduser",
                        lambda p: str(home) if p == "~" else p)
    title = ec._title_from_cwd(str(home))
    assert title.startswith("session - ")
    assert "someuser" not in title
