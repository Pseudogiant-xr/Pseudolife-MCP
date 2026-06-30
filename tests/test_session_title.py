"""Shared session-title helper: project detection + date/time disambiguator."""
from __future__ import annotations

from pseudolife_memory.session_title import git_project_name, title_from_cwd


def test_title_includes_date_and_time():
    t = title_from_cwd(None, now=1782780930.0)  # fixed epoch
    # Format: "<name> - YYYY-MM-DD HH:MM"; name falls back to 'session'.
    assert t.startswith("session - ")
    head, stamp = t.split(" - ", 1)
    assert len(stamp) == len("2026-06-30 14:32")
    assert stamp[4] == "-" and stamp[10] == " " and stamp[13] == ":"


def test_git_project_name_finds_repo_root(tmp_path):
    repo = tmp_path / "MyProj"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert git_project_name(str(sub)) == "MyProj"


def test_git_project_name_none_outside_repo(tmp_path):
    assert git_project_name(str(tmp_path)) is None
