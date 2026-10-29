"""Container bootstrap migrates old state, then permanently drops root."""

import os
from pathlib import Path

import pytest


def test_bootstrap_drops_groups_gid_uid_before_exec(monkeypatch):
    from pseudolife_memory import container_entrypoint as entry

    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(entry, "migrate_state", lambda: calls.append("migrate"))
    monkeypatch.setattr(os, "setgroups", lambda v: calls.append(("groups", v)), raising=False)
    monkeypatch.setattr(os, "setgid", lambda v: calls.append(("gid", v)), raising=False)
    monkeypatch.setattr(os, "setuid", lambda v: calls.append(("uid", v)), raising=False)
    monkeypatch.setattr(os, "execvp", lambda *v: calls.append(("exec", v)))
    entry.main(["python", "-m", "pseudolife_memory.cli", "serve"])
    assert calls == ["migrate", ("groups", []), ("gid", 10001), ("uid", 10001),
                     ("exec", ("python", ["python", "-m", "pseudolife_memory.cli", "serve"]))]


def test_explicit_nonroot_user_does_not_change_ownership(monkeypatch):
    from pseudolife_memory import container_entrypoint as entry

    monkeypatch.setattr(os, "geteuid", lambda: 1234, raising=False)
    monkeypatch.setattr(entry, "migrate_state", lambda: pytest.fail("ownership changed"))
    seen = []
    monkeypatch.setattr(os, "execvp", lambda *v: seen.append(v))
    entry.main(["python", "--version"])
    assert seen == [("python", ["python", "--version"])]


def test_failed_migration_never_executes_the_daemon(monkeypatch):
    from pseudolife_memory import container_entrypoint as entry

    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    def fail():
        raise PermissionError("state is read-only")
    monkeypatch.setattr(entry, "migrate_state", fail)
    monkeypatch.setattr(os, "execvp", lambda *v: pytest.fail("executed"))
    with pytest.raises(SystemExit, match="state is read-only"):
        entry.main(["python", "--version"])


def test_failed_privilege_drop_never_executes_the_daemon(monkeypatch):
    from pseudolife_memory import container_entrypoint as entry

    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(entry, "migrate_state", lambda: None)
    monkeypatch.setattr(os, "setgroups", lambda v: None, raising=False)
    monkeypatch.setattr(os, "setgid", lambda v: None, raising=False)
    def fail(uid):
        raise PermissionError("cannot drop root")
    monkeypatch.setattr(os, "setuid", fail, raising=False)
    monkeypatch.setattr(os, "execvp", lambda *v: pytest.fail("executed as root"))
    with pytest.raises(SystemExit, match="cannot drop root"):
        entry.main(["python", "--version"])


def test_only_root_owned_entries_are_transferred_without_dereferencing(monkeypatch):
    from types import SimpleNamespace
    from pseudolife_memory import container_entrypoint as entry

    calls = []
    monkeypatch.setattr(os, "stat", lambda *a, **kw: SimpleNamespace(st_uid=1234))
    monkeypatch.setattr(os, "chown", lambda *a, **kw: calls.append((a, kw)), raising=False)
    entry._change_owner("keep", 7)
    assert calls == []
    monkeypatch.setattr(os, "stat", lambda *a, **kw: SimpleNamespace(st_uid=0))
    entry._change_owner("link", 7)
    assert calls == [(("link", 10001, 10001), {"dir_fd": 7, "follow_symlinks": False})]


@pytest.mark.skipif(not hasattr(os, "fwalk"), reason="Linux container filesystem API")
def test_ownership_migration_does_not_follow_symlinks(tmp_path, monkeypatch):
    from pseudolife_memory import container_entrypoint as entry

    state = tmp_path / "data"
    state.mkdir()
    (state / "nested").mkdir()
    (state / "nested" / "bank").write_text("keep", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text("untouched", encoding="utf-8")
    (state / "link").symlink_to(outside, target_is_directory=True)
    seen = []
    monkeypatch.setattr(entry, "STATE_DIR", state)
    monkeypatch.setattr(entry, "_change_owner", lambda name, fd: seen.append(name))
    entry.migrate_state()
    assert "bank" in seen and "link" in seen
    assert "private" not in seen
    assert (state / "nested" / "bank").read_text(encoding="utf-8") == "keep"


def test_image_uses_the_privilege_dropping_entrypoint():
    root = Path(__file__).resolve().parents[1]
    image = (root / "ops/Dockerfile.daemon").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["python", "-m", "pseudolife_memory.container_entrypoint"]' in image
    assert "HOME=/home/pseudolife" in image
    for path in ["ops/docker-compose.yml", "pseudolife_memory/compose/docker-compose.yml"]:
        compose = (root / path).read_text(encoding="utf-8")
        assert "no-new-privileges:true" in compose


@pytest.mark.skipif(not hasattr(os, "fwalk"), reason="Linux container filesystem API")
def test_walk_errors_are_not_silently_ignored(tmp_path, monkeypatch):
    from pseudolife_memory import container_entrypoint as entry

    monkeypatch.setattr(entry, "STATE_DIR", tmp_path)
    def inaccessible(*args, **kwargs):
        kwargs["onerror"](PermissionError("unreadable state subtree"))
    monkeypatch.setattr(os, "fwalk", inaccessible)
    with pytest.raises(PermissionError, match="unreadable state subtree"):
        entry.migrate_state()
