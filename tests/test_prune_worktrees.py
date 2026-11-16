"""ops/prune_worktrees.py: retention for finished worktrees and stale caches.

The git side is real: a bare ``origin`` with ``master``, a clone the script
treats as the repo, and worktrees added under a temp worktree root. Nothing
outside ``tmp_path`` is ever touched: every run passes ``--config`` and
``--ledger`` inside it, ``CARGO_TARGET_DIR`` is unset, and the process
listing used by owner mode is faked.
"""

from __future__ import annotations

import importlib.util
import json
import re
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OLD = time.time() - 30 * 86400


def _load():
    spec = importlib.util.spec_from_file_location(
        "prune_worktrees_under_test", ROOT / "ops" / "prune_worktrees.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prune = _load()
REAL_ARRIVAL = getattr(prune, "_arrival", None)   # before any fixture replaces it
REAL_LIST_PROCESSES = prune.list_processes


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8")
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def _configure(repo: Path) -> None:
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"),
                       ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        _git(repo, "config", key, value)


def _is_link(path: str) -> bool:
    st = os.lstat(path)
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _backdate(path: Path, when: float = OLD) -> None:
    """Age every file under ``path`` (no link following) and, for a
    worktree, its admin dir's HEAD and index."""
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames + dirnames:
            entry = os.path.join(dirpath, name)
            if not _is_link(entry):  # never age a link's target
                os.utime(entry, (when, when))
    if not _is_link(str(path)):
        os.utime(path, (when, when))       # the root dir's own mtime too
    dotgit = path / ".git"
    if dotgit.is_file():
        admin = Path(dotgit.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip())
        for name in ("HEAD", "index"):
            if (admin / name).exists():
                os.utime(admin / name, (when, when))


def _junction(link: Path, target: Path) -> bool:
    """Create a junction (Windows) or symlink (elsewhere); False if the
    platform refuses, so the caller can skip and say so."""
    target.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True)
        return proc.returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return True


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.repo = tmp / "repo"
        self.wt_root = tmp / "worktrees"
        self.cache_root = tmp / "cache"
        self.config = tmp / "retention.toml"     # absent unless a test writes it
        self.ledger = tmp / "ledger.jsonl"
        self.wt_root.mkdir()
        self.cache_root.mkdir()
        _git(tmp, "init", "--bare", "-b", "master", str(self.origin))
        _git(tmp, "clone", str(self.origin), str(self.repo))
        _configure(self.repo)
        (self.repo / ".gitignore").write_text(
            "__pycache__/\n.pytest_cache/\nrust/target/\n*.egg-info/\nlocal/\n.venv/\n",
            encoding="utf-8")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        (self.repo / "rust").mkdir()
        (self.repo / "rust" / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")
        _git(self.repo, "push", "origin", "master")

    def add(self, name: str, *, root: Path | None = None, unmerged: bool = False,
            age: bool = True) -> Path:
        path = (root or self.wt_root) / name
        _git(self.repo, "worktree", "add", "-b", name, str(path), "origin/master")
        if unmerged:
            (path / "new.txt").write_text("work\n", encoding="utf-8")
            _git(path, "add", "new.txt")
            _git(path, "commit", "-m", "unmerged work")
        if age:
            _backdate(path)
        return path

    def cache(self, name: str, *, age: bool = True) -> Path:
        path = self.cache_root / name
        (path / "debug").mkdir(parents=True)
        (path / "debug" / "lib.rlib").write_bytes(b"x" * 1000)
        (path / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n",
                                           encoding="utf-8")
        if age:
            _backdate(path)
        return path

    def args(self, *extra: str, roots: bool = True) -> list[str]:
        base = ["--repo", str(self.repo), "--config", str(self.config),
                "--ledger", str(self.ledger)]
        if roots:
            base += ["--worktree-root", str(self.wt_root),
                     "--cache-root", str(self.cache_root)]
        return base + list(extra)

    def ledger_lines(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in
                self.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]

    def worktrees(self) -> list[str]:
        out = _git(self.repo, "worktree", "list", "--porcelain")
        return [os.path.normcase(os.path.realpath(line[len("worktree "):]))
                for line in out.splitlines() if line.startswith("worktree ")]

    def registered(self, path: Path) -> bool:
        return os.path.normcase(os.path.realpath(path)) in self.worktrees()


@pytest.fixture
def env(tmp_path, monkeypatch):
    # tmp_path lives under the system temp dir, which the script protects;
    # tests opt their own temp tree back in, and no test may see a real
    # CARGO_TARGET_DIR or the host's process table.
    monkeypatch.setattr(prune, "protected_temp_roots", lambda: [])
    monkeypatch.setattr(prune, "list_processes", lambda: [])
    # Files made here arrive now; tests age them by mtime alone, and the
    # arrival rule has its own test.
    monkeypatch.setattr(prune, "_arrival", lambda st: 0.0, raising=False)
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    # Never the developer's own cargo config or toolchain homes.
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "cargo-home"))
    monkeypatch.setenv("RUSTUP_HOME", str(tmp_path / "rustup-home"))
    monkeypatch.chdir(tmp_path)
    return Env(tmp_path)


def run(env: Env, *argv: str, roots: bool = True) -> tuple[int, str]:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = prune.main(env.args(*argv, roots=roots))
    return rc, buf.getvalue()


# --- sweep mode: worktrees -------------------------------------------------

def test_good_case_is_removed_with_plain_remove_and_branch_survives(env):
    wt = env.add("done")
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert not wt.exists(), out
    assert not env.registered(wt)
    assert _git(env.repo, "branch", "--list", "done"), "local branch must survive"


def test_dry_run_changes_nothing(env):
    wt = env.add("done")
    cache = env.cache("old-slice")
    rc, out = run(env)
    assert rc == 0, out
    assert wt.exists() and env.registered(wt)
    assert cache.exists()
    assert env.ledger_lines() == []
    assert "dry run" in out.lower()
    assert "would remove" in out


def test_refuses_head_not_on_master(env):
    wt = env.add("wip", unmerged=True)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert wt.exists() and env.registered(wt)
    assert "not merged" in out


def test_refuses_non_whitelisted_ignored_file(env):
    wt = env.add("evals")
    (wt / "local" / "data").mkdir(parents=True)
    (wt / "local" / "data" / "run.jsonl").write_text("{}\n", encoding="utf-8")
    _backdate(wt)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert wt.exists()
    assert "ignored" in out and "local/" in out


def test_whitelisted_ignored_caches_do_not_block(env):
    wt = env.add("cached")
    for rel in ("rust/target/debug/x.rlib", "pkg/__pycache__/m.pyc",
                ".pytest_cache/v/cache", "demo.egg-info/PKG-INFO"):
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text("x", encoding="utf-8")
    _backdate(wt)
    # A fresh file inside a whitelisted cache does not reset the grace clock.
    os.utime(wt / "rust/target/debug/x.rlib", None)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert not wt.exists(), out


def test_refuses_tracked_and_untracked_changes(env):
    dirty = env.add("dirty")
    (dirty / "README.md").write_text("changed\n", encoding="utf-8")
    untracked = env.add("untracked")
    (untracked / "notes.txt").write_text("mine\n", encoding="utf-8")
    _backdate(dirty)
    _backdate(untracked)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert dirty.exists() and untracked.exists()
    rows = [line for line in out.splitlines() if line.startswith("worktree ")]
    assert sum("uncommitted changes" in line for line in rows) == 2, out


def test_refuses_reparse_point(env, tmp_path):
    wt = env.add("linked")
    if not _junction(wt / "rust" / "target", tmp_path / "elsewhere"):
        pytest.skip("this platform could not create a junction or symlink")
    keep = tmp_path / "elsewhere" / "precious.txt"
    keep.write_text("data\n", encoding="utf-8")
    _backdate(wt)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert wt.exists() and keep.exists()
    assert "reparse point" in out


def test_grace_period_holds_a_fresh_worktree(env):
    wt = env.add("fresh", age=False)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert wt.exists()
    assert "grace" in out


def test_grace_counts_the_admin_dir(env):
    wt = env.add("reused")
    admin = Path((wt / ".git").read_text(encoding="utf-8").split("gitdir:", 1)[1].strip())
    os.utime(admin / "HEAD", None)  # a fresh checkout in an old tree
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert wt.exists()
    assert "grace" in out


def test_grace_days_flag(env):
    wt = env.add("recent", age=False)
    _backdate(wt, time.time() - 2 * 86400)
    rc, out = run(env, "--apply", "--worktree-grace-days", "1")
    assert rc == 0, out
    assert not wt.exists(), out


def test_keep_marker_file(env):
    wt = env.add("kept-file")
    (wt / ".pseudolife-keep").write_text("", encoding="utf-8")
    _backdate(wt)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert wt.exists()
    assert "keep marker" in out


def test_keep_marker_worktree_config(env):
    wt = env.add("kept-config")
    other = env.add("other")
    _git(env.repo, "config", "extensions.worktreeConfig", "true")
    _git(wt, "config", "--worktree", "pseudolife.keep", "true")
    _backdate(wt)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert wt.exists()
    assert not other.exists(), "a per-worktree marker must not keep its siblings"
    assert "keep marker" in out


def test_never_main_worktree_or_outside_roots(env, tmp_path):
    outside = env.add("outside", root=tmp_path)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert env.repo.exists() and outside.exists()
    assert "outside roots: 1" in out


def test_harness_roots_are_untouched_even_when_configured(env, tmp_path):
    harness = tmp_path / "home" / ".claude" / "worktrees"
    harness.mkdir(parents=True)
    wt = env.add("agent-1", root=harness)
    rc, out = run(env, "--apply", "--worktree-root", str(harness))
    assert rc == 0, out
    assert wt.exists() and env.registered(wt)
    assert "harness-managed: 1" in out


def test_temp_dirs_are_protected(env, tmp_path, monkeypatch):
    monkeypatch.setattr(prune, "protected_temp_roots", lambda: [str(tmp_path)])
    wt = env.add("in-temp")
    cache = env.cache("old")
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert wt.exists() and cache.exists()
    assert "harness-managed" in out


def test_locked_worktree_is_refused(env):
    wt = env.add("locked")
    _git(env.repo, "worktree", "lock", str(wt))
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert wt.exists()
    assert "locked" in out


def test_fetch_failure_refuses_the_whole_run(env):
    wt = env.add("done")
    _git(env.repo, "remote", "set-url", "origin", str(env.tmp / "missing.git"))
    rc, out = run(env, "--apply")
    assert rc != 0
    assert wt.exists()
    assert "fetch" in out.lower()
    assert env.ledger_lines() == []


def test_no_roots_is_a_noop_with_instructions(env):
    wt = env.add("done")
    rc, out = run(env, "--apply", roots=False)
    assert rc == 0, out
    assert wt.exists()
    assert "--worktree-root" in out and "retention.toml" in out


def test_config_file_supplies_roots(env):
    wt = env.add("done")
    env.config.write_text(
        f"repo = {json.dumps(str(env.repo))}\n"
        f"worktree_roots = [{json.dumps(str(env.wt_root))}]\n"
        f"cache_roots = [{json.dumps(str(env.cache_root))}]\n",
        encoding="utf-8")
    rc, out = run(env, "--apply", roots=False)
    assert rc == 0, out
    assert not wt.exists(), out


def test_ledger_lines_are_written(env):
    wt = env.add("done")
    env.cache("old-slice")
    rc, out = run(env, "--apply")
    assert rc == 0, out
    lines = env.ledger_lines()
    kinds = sorted(line["kind"] for line in lines)
    assert kinds == ["cache", "worktree"]
    tree = next(line for line in lines if line["kind"] == "worktree")
    assert tree["mode"] == "sweep"
    assert tree["head"] and len(tree["head"]) == 40
    assert os.path.normcase(tree["path"]) == os.path.normcase(str(wt))
    for line in lines:
        assert {"time", "kind", "path", "bytes", "newest_mtime", "checks"} <= set(line)
        assert line["bytes"] > 0
        assert line["checks"]


# --- sweep mode: caches ----------------------------------------------------

def test_cache_age_rule(env):
    old = env.cache("old")
    fresh = env.cache("fresh", age=False)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert not old.exists()
    assert fresh.exists()
    assert "grace" in out


def test_cache_reparse_point_is_refused(env, tmp_path):
    cache = env.cache("linked")
    if not _junction(cache / "shared", tmp_path / "shared-target"):
        pytest.skip("this platform could not create a junction or symlink")
    keep = tmp_path / "shared-target" / "precious.txt"
    keep.write_text("data\n", encoding="utf-8")
    _backdate(cache)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert cache.exists() and keep.exists()
    assert "reparse point" in out


def test_cache_pinned_by_a_worktree_config_is_kept(env):
    cache = env.cache("pinned")
    holder = env.wt_root / "holder"
    (holder / ".cargo").mkdir(parents=True)
    (holder / ".cargo" / "config.toml").write_text(
        f"[build]\ntarget-dir = {json.dumps(str(cache))}\n", encoding="utf-8")
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert cache.exists()
    assert "pinned" in out


def test_cache_delete_error_is_a_skip(env, monkeypatch):
    cache = env.cache("held")

    def held(src, dst):
        raise PermissionError(32, "The process cannot access the file", str(src))

    monkeypatch.setattr(prune.os, "rename", held)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert cache.exists()
    assert "in use" in out
    assert env.ledger_lines() == []


# --- owner mode ------------------------------------------------------------

def test_owner_mode_skips_grace_and_removes_own_worktree(env, monkeypatch):
    wt = env.add("mine", age=False)
    cache = env.cache("mine-target", age=False)
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc == 0, out
    assert not env.registered(wt)
    assert not cache.exists()
    assert _git(env.repo, "branch", "--list", "mine")
    assert "branch mine kept" in out
    modes = {line["mode"] for line in env.ledger_lines()}
    assert modes == {"owner"}


def test_owner_mode_uses_cargo_target_dir(env, monkeypatch):
    wt = env.add("mine", age=False)
    cache = env.cache("env-target", age=False)
    monkeypatch.setenv("CARGO_TARGET_DIR", str(cache))
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--apply", "--cache-root", str(env.cache_root),
                  roots=False)
    assert rc == 0, out
    assert not cache.exists()


def test_owner_mode_leaves_a_possibly_shared_cargo_target_dir(env, tmp_path, monkeypatch):
    """CARGO_TARGET_DIR is often one dir every session on a machine shares;
    outside a configured cache root it is not known to be the owner's."""
    wt = env.add("mine", age=False)
    shared = tmp_path / "shared-target"
    (shared / "debug").mkdir(parents=True)
    (shared / "debug" / "lib.rlib").write_bytes(b"x")
    monkeypatch.setenv("CARGO_TARGET_DIR", str(shared))
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--apply", roots=False)
    assert rc == 0, out
    assert (shared / "debug" / "lib.rlib").exists()
    assert "may be shared" in out
    assert not env.registered(wt), "the worktree itself is still the owner's to remove"


def test_owner_mode_refuses_unmerged_head_and_changes_nothing(env, monkeypatch):
    wt = env.add("mine", unmerged=True, age=False)
    cache = env.cache("mine-target", age=False)
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc != 0
    assert "not merged" in out
    assert wt.exists() and env.registered(wt) and cache.exists()
    assert env.ledger_lines() == []


def test_owner_mode_refusal_on_dirty_tree_changes_nothing(env, monkeypatch):
    wt = env.add("mine", age=False)
    (wt / "notes.txt").write_text("unsaved\n", encoding="utf-8")
    cache = env.cache("mine-target", age=False)
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc != 0
    assert wt.exists() and (wt / "notes.txt").exists() and cache.exists()
    assert env.ledger_lines() == []


def test_owner_mode_refuses_keep_marker(env, monkeypatch):
    wt = env.add("mine", age=False)
    (wt / ".pseudolife-keep").write_text("", encoding="utf-8")
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--apply", roots=False)
    assert rc != 0
    assert wt.exists()
    assert "keep marker" in out


def test_owner_mode_harness_checkout_kept_cache_cleaned(env, tmp_path, monkeypatch):
    harness = tmp_path / "home" / ".t3" / "worktrees" / "proj"
    harness.mkdir(parents=True)
    wt = env.add("t3-abc", root=harness, age=False)
    cache = env.cache("t3-target", age=False)
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc == 0, out
    assert wt.exists() and env.registered(wt)
    assert not cache.exists()
    assert "belongs to its harness" in out


def test_owner_mode_refuses_cache_named_by_a_running_process(env, monkeypatch):
    wt = env.add("mine", age=False)
    cache = env.cache("busy", age=False)
    monkeypatch.setattr(prune, "list_processes", lambda: [
        (999999, 1, f"rustc --out-dir {cache}{os.sep}debug{os.sep}deps")])
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc != 0
    assert "in use by process 999999" in out
    assert wt.exists() and env.registered(wt) and cache.exists()


def test_owner_mode_process_check_unavailable_is_skipped(env, monkeypatch):
    wt = env.add("mine", age=False)
    cache = env.cache("mine-target", age=False)
    monkeypatch.setattr(prune, "list_processes", lambda: None)
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc == 0, out
    assert not cache.exists()
    assert "process check unavailable" in out


def test_owner_mode_refuses_a_shared_cache_root(env, monkeypatch):
    wt = env.add("mine", age=False)
    env.cache("someone-else")
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(env.cache_root),
                  "--cache-root", str(env.cache_root), "--apply", roots=False)
    assert rc != 0
    assert "cache root" in out
    assert wt.exists() and (env.cache_root / "someone-else").exists()


def test_owner_mode_dry_run_changes_nothing(env, monkeypatch):
    wt = env.add("mine", age=False)
    cache = env.cache("mine-target", age=False)
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), roots=False)
    assert rc == 0, out
    assert wt.exists() and env.registered(wt) and cache.exists()
    assert env.ledger_lines() == []
    assert "would remove" in out


def test_owner_mode_refuses_main_worktree(env, monkeypatch):
    monkeypatch.chdir(env.repo)
    rc, out = run(env, "--mine", "--apply", roots=False)
    assert rc != 0
    assert "main worktree" in out
    assert (env.repo / "README.md").exists()


# --- review fixes ----------------------------------------------------------

def test_owner_mode_refuses_a_cache_dir_inside_another_worktree(env, monkeypatch):
    wt = env.add("mine", age=False)
    sibling = env.add("sibling", age=False)
    (sibling / "notes.txt").write_text("uncommitted\n", encoding="utf-8")
    monkeypatch.chdir(wt)
    for target in (sibling, sibling / "rust", env.repo / "rust"):
        rc, out = run(env, "--mine", "--cache-dir", str(target), "--apply", roots=False)
        assert rc != 0, out
        assert "worktree" in out
    assert (sibling / "notes.txt").exists() and (sibling / "rust" / "Cargo.toml").exists()
    assert (env.repo / "rust" / "Cargo.toml").exists()
    assert env.registered(wt) and env.ledger_lines() == []


def test_owner_mode_accepts_its_own_rust_target(env, tmp_path, monkeypatch):
    harness = tmp_path / "home" / ".t3" / "worktrees" / "proj"
    harness.mkdir(parents=True)
    wt = env.add("t3-own", root=harness, age=False)
    target = wt / "rust" / "target"
    (target / "debug").mkdir(parents=True)
    (target / "debug" / "x.rlib").write_bytes(b"x")
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(wt / "rust"), "--apply", roots=False)
    assert rc != 0 and "not a cache path" in out
    rc, out = run(env, "--mine", "--cache-dir", str(target), "--apply", roots=False)
    assert rc == 0, out
    assert not target.exists() and (wt / "rust" / "Cargo.toml").exists()


def test_owner_mode_failed_remove_is_a_refusal(env, monkeypatch):
    wt = env.add("mine", age=False)
    cache = env.cache("mine-target", age=False)
    real_git = prune.git

    def failing(cwd, *args, **kw):
        if args[:2] == ("worktree", "remove"):
            return subprocess.CompletedProcess(args, 1, "", "fatal: simulated failure")
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(prune, "git", failing)
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc == 1, out
    assert "simulated failure" in out
    assert env.registered(wt) and cache.exists()
    assert env.ledger_lines() == []


def test_sweep_failed_remove_exits_nonzero(env, monkeypatch):
    wt = env.add("done")
    real_git = prune.git

    def failing(cwd, *args, **kw):
        if args[:2] == ("worktree", "remove"):
            return subprocess.CompletedProcess(args, 1, "", "fatal: simulated failure")
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(prune, "git", failing)
    rc, out = run(env, "--apply")
    assert rc == 1, out
    assert env.registered(wt) and env.ledger_lines() == []


def test_sweep_recheck_sees_a_lock_taken_after_the_scan(env, monkeypatch):
    wt = env.add("done")
    real = prune.evaluate_worktree
    calls = []

    def lock_after_first(repo, w, **kw):
        verdict = real(repo, w, **kw)
        if not calls:
            _git(env.repo, "worktree", "lock", str(wt))
        calls.append(1)
        return verdict

    monkeypatch.setattr(prune, "evaluate_worktree", lock_after_first)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert wt.exists() and "locked" in out


def test_sweep_recheck_sees_a_pin_added_after_the_scan(env, monkeypatch):
    cache = env.cache("soon-pinned")
    holder = env.add("holder", unmerged=True)
    real = prune.evaluate_cache
    calls = []

    def pin_after_first(path, **kw):
        verdict = real(path, **kw)
        if not calls:
            (holder / "rust" / ".cargo").mkdir(parents=True)
            (holder / "rust" / ".cargo" / "config.toml").write_text(
                f"[build]\ntarget-dir = {json.dumps(str(cache))}\n", encoding="utf-8")
        calls.append(1)
        return verdict

    monkeypatch.setattr(prune, "evaluate_cache", pin_after_first)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert cache.exists() and "pinned" in out


def test_pin_in_a_workspace_subdir_counts(env):
    cache = env.cache("rust-pinned")
    holder = env.add("holder", unmerged=True)
    (holder / "rust" / ".cargo").mkdir(parents=True)
    (holder / "rust" / ".cargo" / "config.toml").write_text(
        f"[build]\ntarget-dir = {json.dumps(str(cache))}\n", encoding="utf-8")
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert cache.exists() and "pinned" in out


def test_config_roots_must_be_lists(env):
    env.add("done")
    env.config.write_text(f"cache_roots = {json.dumps(str(env.cache_root))}\n",
                          encoding="utf-8")
    rc, out = run(env, "--apply", roots=False)
    assert rc == 2
    assert "must be a list" in out


def test_drive_root_and_home_ancestors_are_never_roots(env, tmp_path, monkeypatch):
    home = tmp_path / "home" / "user"
    home.mkdir(parents=True)
    monkeypatch.setattr(prune.Path, "home", classmethod(lambda cls: home))
    stray = tmp_path / "home" / "old-tool"
    stray.mkdir()
    (stray / "f").write_text("x", encoding="utf-8")
    _backdate(stray)
    rc, out = run(env, "--apply", "--cache-root", str(tmp_path / "home"), roots=False)
    assert rc == 0, out
    assert stray.exists()
    assert "never used as a root" in out


def test_toml_fallback_keeps_hash_inside_quotes():
    assert prune._strip_comment('repo = "C:/a#b"  # note').strip() == 'repo = "C:/a#b"'
    assert prune._strip_comment("x = 'a#b' # c").strip() == "x = 'a#b'"
    assert prune._strip_comment('x = "a\\"#b" # c').strip() == 'x = "a\\"#b"'


def test_process_listing_sees_this_process():
    """Unmocked: the Windows listing once printed a literal backtick-t, so
    every row failed to parse and the owner-mode process check never ran."""
    procs = prune.list_processes()
    assert procs is not None, "this platform's process listing failed"
    assert os.getpid() in {pid for pid, _, _ in procs}


def test_unregistered_with_leftover_files_is_partial_not_removed(env, monkeypatch):
    """git can unregister a worktree yet leave files behind (a file held
    open on Windows); no later sweep can see them, so it is reported."""
    wt = env.add("done")
    real_git = prune.git

    def leaves_a_file(cwd, *args, **kw):
        proc = real_git(cwd, *args, **kw)
        if args[:2] == ("worktree", "remove") and proc.returncode == 0:
            wt.mkdir(exist_ok=True)
            (wt / "README.md").write_text("held\n", encoding="utf-8")
            return subprocess.CompletedProcess(args, 255, "", "error: failed to delete")
        return proc

    monkeypatch.setattr(prune, "git", leaves_a_file)
    rc, out = run(env, "--apply")
    assert rc == 1, out
    assert "files remain" in out and "partial" in out
    assert [line["complete"] for line in env.ledger_lines()] == [False]


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to delete an open file")
def test_windows_open_file_leaves_a_partial_owner_removal(env, monkeypatch):
    wt = env.add("mine", age=False)
    cache = env.cache("mine-target", age=False)
    monkeypatch.chdir(wt)
    with open(wt / "README.md", encoding="utf-8"):
        rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc == 1, out
    assert "partial:" in out and "files remain" in out
    assert cache.exists(), "a partial removal must not go on to the cache"
    assert [(l["mode"], l["kind"], l["complete"]) for l in env.ledger_lines()] == [
        ("owner", "worktree", False)]


def _remove_then(leave):
    """A git wrapper whose `worktree remove` really runs, then leaves
    something behind and reports git's error exit, as a held cwd or file
    does on Windows."""
    real_git = prune.git

    def wrapped(cwd, *args, **kw):
        proc = real_git(cwd, *args, **kw)
        if args[:2] == ("worktree", "remove") and proc.returncode == 0:
            leave(Path(args[2]))
            return subprocess.CompletedProcess(args, 255, "", "error: failed to delete")
        return proc
    return wrapped


def test_only_empty_folders_left_counts_as_removed(env, monkeypatch):
    """A shell sitting in <wt>/rust leaves empty folders, not files."""
    wt = env.add("done")
    monkeypatch.setattr(prune, "git", _remove_then(
        lambda path: (path / "rust" / "deeper").mkdir(parents=True)))
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert "files remain" not in out
    assert [line["complete"] for line in env.ledger_lines()] == [True]


def test_owner_mode_partial_is_reported_as_partial_not_refused(env, monkeypatch):
    wt = env.add("mine", age=False)
    cache = env.cache("mine-target", age=False)

    def leave(path):
        path.mkdir(exist_ok=True)
        (path / "held.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(prune, "git", _remove_then(leave))
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc == 1, out
    assert out.startswith("partial:") and "refused" not in out
    assert cache.exists()
    assert [(l["mode"], l["complete"]) for l in env.ledger_lines()] == [("owner", False)]


def test_listing_failure_after_remove_is_unknown_and_recorded(env, monkeypatch):
    """git's list failing after a removal leaves the outcome unknown: it is
    reported and recorded as such, not called failed or removed, and the
    rest of the run still reports."""
    env.add("done")
    env.cache("old-slice")
    real = prune.registered_entry
    calls = []

    def fails_after_the_recheck(repo, path):
        calls.append(path)
        if len(calls) == 1:            # the sweep's recheck before removal
            return real(repo, path)
        raise prune.Refusal("`git worktree list` failed: simulated")

    monkeypatch.setattr(prune, "registered_entry", fails_after_the_recheck)
    rc, out = run(env, "--apply")
    assert rc == 1, out
    assert "could not be read" in out
    assert "caches: 1 candidates, 1 removed" in out
    tree = next(line for line in env.ledger_lines() if line["kind"] == "worktree")
    assert tree["complete"] is False and tree["state"] == "unknown"


def test_persistent_listing_failure_fails_rows_not_the_run(env, monkeypatch):
    """Once `git worktree list` keeps failing, later rows fail one by one;
    the run still prints every row, including the removal already done."""
    wt = env.add("done")
    env.cache("old-slice")
    real = prune.list_worktrees
    removed = []

    def breaks_after_removal(repo):
        if removed:
            raise prune.Refusal("`git worktree list` failed: simulated")
        return real(repo)

    real_git = prune.git

    def note_removal(cwd, *args, **kw):
        proc = real_git(cwd, *args, **kw)
        if args[:2] == ("worktree", "remove"):
            removed.append(args[2])
        return proc

    monkeypatch.setattr(prune, "list_worktrees", breaks_after_removal)
    monkeypatch.setattr(prune, "git", note_removal)
    rc, out = run(env, "--apply")
    assert rc == 1, out
    assert not wt.exists()
    assert "APPLY" in out and "could not finish" in out
    rows = [line for line in out.splitlines() if line.startswith(("worktree ", "cache "))]
    assert rows[0].split()[1] == "unknown", "the removal already done is still shown"
    assert rows[1].split()[1] == "failed"
    assert "1 unknown (see rows)" in out and "1 failed (see rows)" in out
    assert [(l["kind"], l["state"]) for l in env.ledger_lines()] == [("worktree", "unknown")]


def test_leftover_file_under_empty_folders_is_partial(env, monkeypatch):
    env.add("done")

    def leave(path):
        (path / "a" / "b").mkdir(parents=True)
        (path / "a" / "b" / "held.bin").write_bytes(b"x")

    monkeypatch.setattr(prune, "git", _remove_then(leave))
    rc, out = run(env, "--apply")
    assert rc == 1, out
    assert "held.bin" in out and "partial" in out


def test_leftover_link_is_partial(env, tmp_path, monkeypatch):
    env.add("done")
    made = []

    def leave(path):
        path.mkdir(exist_ok=True)
        made.append(_junction(path / "link", tmp_path / "link-target"))

    monkeypatch.setattr(prune, "git", _remove_then(leave))
    rc, out = run(env, "--apply")
    if not made[0]:
        pytest.skip("this platform could not create a junction or symlink")
    assert rc == 1, out
    assert "first: link" in out


def test_owner_mode_unknown_state_leaves_the_cache(env, monkeypatch):
    wt = env.add("mine", age=False)
    cache = env.cache("mine-target", age=False)
    real = prune.registered_entry
    calls = []

    def fails_after_the_recheck(repo, path):
        calls.append(path)
        if len(calls) == 1:
            return real(repo, path)
        raise prune.Refusal("`git worktree list` failed: simulated")

    monkeypatch.setattr(prune, "registered_entry", fails_after_the_recheck)
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc == 1, out
    assert out.startswith("unknown:") and cache.exists()
    assert [(l["mode"], l["complete"], l["state"]) for l in env.ledger_lines()] == [
        ("owner", False, "unknown")]


def _utf16_pin(holder: Path, cache: Path) -> None:
    (holder / ".cargo").mkdir()
    (holder / ".cargo" / "config.toml").write_text(
        f"[build]\ntarget-dir = {json.dumps(str(cache))}\n", encoding="utf-16")


def test_cargo_config_that_is_not_utf8_keeps_caches_not_worktrees(env):
    """A pin that cannot be read could protect any cache, so every cache is
    kept; it says nothing about worktrees, which still go."""
    cache = env.cache("old-slice")
    done = env.add("done")
    _utf16_pin(env.add("holder", unmerged=True), cache)
    _utf16_pin(env.add("holder2", unmerged=True), cache)
    rc, out = run(env, "--apply")
    assert rc == 0, out
    assert "pin config unreadable" in out and cache.exists()
    for name in ("holder", "holder2"):
        assert f"{name}{os.sep}.cargo" in out, f"every unreadable file is named: {name}"
    assert not done.exists()


def test_owner_mode_with_an_unreadable_pin_config_changes_nothing(env, monkeypatch):
    wt = env.add("mine", age=False)
    cache = env.cache("mine-target", age=False)
    _utf16_pin(env.add("holder", unmerged=True), cache)
    monkeypatch.chdir(wt)
    rc, out = run(env, "--mine", "--cache-dir", str(cache), "--apply", roots=False)
    assert rc == 1, out
    assert "pin config unreadable" in out
    assert "no --cache-dir and CARGO_TARGET_DIR unset" in out, "the way out is named"
    assert env.registered(wt) and cache.exists() and env.ledger_lines() == []


def test_far_future_mtime_does_not_crash_the_ledger(env, monkeypatch):
    wt = env.add("mine", age=False)
    monkeypatch.chdir(wt)
    real_localtime = prune.time.localtime

    def refuses_far_future(secs=None):
        # Windows refuses times past year 3000; reproduce that refusal
        # everywhere, for anything past 2100 (4102444800).
        if secs is not None and secs > 4102444800:
            raise OSError(22, "Invalid argument")
        return real_localtime() if secs is None else real_localtime(secs)

    monkeypatch.setattr(prune.time, "localtime", refuses_far_future)
    # Year 2255: every filesystem stores it intact. ext4 and APFS clamp a
    # larger value such as 4e10 to 2262, so the file's real mtime would
    # depend on the CI runner's filesystem.
    future = 9e9
    os.utime(wt / "README.md", (future, future))
    rc, out = run(env, "--mine", "--apply", roots=False)
    assert rc == 0, out
    assert not env.registered(wt)
    assert [line["newest_mtime"] for line in env.ledger_lines()] == [None], (
        "the removal must be recorded, not crash")


def test_failed_prune_is_reported(env, monkeypatch):
    env.add("done")
    real_git = prune.git

    def prune_fails(cwd, *args, **kw):
        if args[:2] == ("worktree", "prune"):
            return subprocess.CompletedProcess(args, 1, "", "fatal: simulated prune failure")
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(prune, "git", prune_fails)
    rc, out = run(env, "--apply")
    assert rc == 1, out
    assert "simulated prune failure" in out and "ran git worktree prune" not in out


def test_partial_cache_delete_is_recorded_as_partial(env, monkeypatch):
    env.cache("old-slice")

    def stops_midway(path, *a, **kw):
        raise PermissionError(5, "Access is denied", str(path))

    monkeypatch.setattr(prune.shutil, "rmtree", stops_midway)
    rc, out = run(env, "--apply")
    assert rc == 1, out
    assert "deleted in part" in out and "1 partial (see rows)" in out
    assert [(l["kind"], l["complete"], l["state"]) for l in env.ledger_lines()] == [
        ("cache", False, "partial")]


# --- sweep mode: cargo targets judged by their owners -----------------------
#
# Disk retention design of 16 November 2026 (maintainer-approved): a cargo
# target is removed when every checkout its dep-info names is gone or
# merged, after a grace period; with no owner found, only after 14 days
# idle. Everything is decided by an exact marker, never by a name.

SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55\n"


def _target(env: Env, name: str, *sources: Path, age: float | None = OLD,
            root: Path | None = None) -> Path:
    """A cargo target dir as cargo leaves it: the tag, .rustc_info.json, a
    debug profile with its lock and fingerprints, and a dep-info file
    naming ``sources``."""
    path = (root or env.tmp / "targets") / name
    deps = path / "debug" / "deps"
    deps.mkdir(parents=True)
    (path / "debug" / ".fingerprint").mkdir()
    (path / "debug" / ".cargo-lock").write_bytes(b"")
    (path / "CACHEDIR.TAG").write_text(SIGNATURE, encoding="utf-8")
    (path / ".rustc_info.json").write_text("{}", encoding="utf-8")
    (deps / "libthing.rlib").write_bytes(b"x" * 1000)
    # Cargo names its outputs inside the profile, which is how a profile is
    # known to be cargo's own and not a copy.
    escaped = " ".join(str(s).replace(" ", "\\ ") for s in sources)
    (deps / "thing-1234.d").write_text(
        f"{deps / 'libthing.rlib'}: {escaped}\n\n"
        + "".join(f"{str(s).replace(' ', chr(92) + ' ')}:\n" for s in sources),
        encoding="utf-8")
    if age is not None:
        _backdate(path, age)
    return path


def _src(checkout: Path) -> Path:
    src = checkout / "rust" / "src" / "lib.rs"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("// source\n", encoding="utf-8")
    return src


def run_targets(env: Env, *argv: str) -> tuple[int, str]:
    return run(env, "--repo", str(env.repo), "--config", str(env.config),
               "--ledger", str(env.ledger), "--target-root", str(env.tmp / "targets"),
               *argv, roots=False)


@pytest.fixture
def targets_root(env):
    (env.tmp / "targets").mkdir()
    return env.tmp / "targets"


def test_target_whose_owner_checkout_is_gone_is_removed(env, targets_root):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "w5", gone)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert not target.exists(), out
    [line] = env.ledger_lines()
    assert line["kind"] == "target" and line["complete"] is True
    assert "deleted-worktree" in line["note"] and "gone" in line["note"]


def test_target_whose_owner_worktree_is_merged_is_removed(env, targets_root):
    wt = env.add("merged-owner")
    target = _target(env, "w6", _src(wt))
    _backdate(wt)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert not target.exists(), out
    assert env.registered(wt), "a target sweep never removes the owner itself"


def test_target_whose_owner_has_unmerged_work_is_kept(env, targets_root):
    wt = env.add("busy-owner", unmerged=True)
    target = _target(env, "w7", _src(wt))
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "owner in use" in out and "not on origin/master" in out


def test_target_built_from_the_main_checkout_is_kept(env, targets_root):
    target = _target(env, "main-build", _src(env.repo))
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "the main checkout" in out


def test_every_owner_must_be_finished(env, targets_root):
    busy = env.add("second-owner", unmerged=True)
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "shared", gone, _src(busy))
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "owner in use" in out


def test_a_venv_with_the_same_tag_is_not_a_target(env, targets_root):
    """uv writes cargo's exact CACHEDIR.TAG signature into a virtual
    environment (measured 16 November 2026), so the tag alone proves
    nothing: a target also needs cargo's own .rustc_info.json."""
    venv = targets_root / "ci-venv"
    (venv / "lib").mkdir(parents=True)
    (venv / "CACHEDIR.TAG").write_text(SIGNATURE, encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    _backdate(venv)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert venv.exists()
    assert "not a cargo target: 1" in out


def test_a_build_holding_cargos_lock_keeps_the_target(env, targets_root):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "building", gone)
    fd = os.open(target / "debug" / ".cargo-lock", os.O_RDWR)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, out = run_targets(env, "--apply")
    finally:
        os.close(fd)
    assert rc == 0, out
    assert target.exists()
    assert "a build holds cargo's lock" in out


def test_entries_cargo_does_not_explain_are_kept(env, targets_root):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "with-extras", gone, age=None)
    (target / "mutant-src").mkdir()
    (target / "mutant-src" / "lib.rs").write_text("copy\n", encoding="utf-8")
    (target / "cand-final.exe").write_bytes(b"binary")
    (target / "tmp").mkdir()
    _backdate(target)
    total = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    # Cargo's markers stay beside foreign entries, so the dir stays a target.
    assert sorted(p.name for p in target.iterdir()) == [
        ".rustc_info.json", "CACHEDIR.TAG", "cand-final.exe", "mutant-src", "tmp"]
    assert "kept, not cargo's (" in out and "): cand-final.exe, mutant-src, tmp" in out
    [line] = env.ledger_lines()
    kept = (target / "cand-final.exe").stat().st_size + sum(
        f.stat().st_size for f in (target / "mutant-src").rglob("*") if f.is_file()) + sum(
        (target / n).stat().st_size for n in ("CACHEDIR.TAG", ".rustc_info.json"))
    assert line["bytes"] == total - kept, (
        "only what was deleted is counted")


def test_a_target_with_no_owner_waits_for_the_unowned_period(env, targets_root):
    idle_5_days = _target(env, "unowned-recent", age=time.time() - 5 * 86400)
    idle_30_days = _target(env, "unowned-old")
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert idle_5_days.exists() and not idle_30_days.exists()
    assert "no owner found; idle 5.0 days, removed after 14" in out


def test_target_grace_holds_a_recent_build(env, targets_root):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "fresh", gone, age=None)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "in grace period" in out
    rc, out = run_targets(env, "--apply", "--target-grace-hours", "0")
    assert rc == 0, out
    assert not target.exists()


def test_a_link_inside_a_target_keeps_it(env, targets_root, tmp_path):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "linked", gone)
    if not _junction(target / "debug" / "shared", tmp_path / "elsewhere"):
        pytest.skip("this platform could not create a junction or symlink")
    keep = tmp_path / "elsewhere" / "precious.txt"
    keep.write_text("data\n", encoding="utf-8")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists() and keep.exists()
    assert "reparse point" in out


def test_dry_run_reports_a_target_and_changes_nothing(env, targets_root):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "w9", gone)
    rc, out = run_targets(env)
    assert rc == 0, out
    assert target.exists() and env.ledger_lines() == []
    assert "would remove" in out and "targets: 1 candidates, 1 would remove" in out


def test_cargo_and_rustup_homes_name_no_owner(env, monkeypatch):
    """A registry source cargo's own cleanup removed is not a gone owner."""
    home = env.tmp / "cargo-home"
    home.mkdir()
    monkeypatch.setenv("CARGO_HOME", str(home))
    missing = home / "registry" / "src" / "crate-1.0" / "src" / "lib.rs"
    target = env.tmp / "t"
    assert prune.source_owner(str(missing), str(target), prune._tool_homes()) is None


def test_dep_info_paths_keep_escaped_spaces():
    text = "out.rlib: /a\\ b/src/lib.rs /c/d.rs\n\n/a\\ b/src/lib.rs:\n"
    assert prune._dep_tokens(text) == ["out.rlib", "/a b/src/lib.rs", "/c/d.rs",
                                       "/a b/src/lib.rs"]


def test_the_home_dir_may_be_a_target_root(env, monkeypatch):
    """WSL workers leave targets directly in the home dir; only children
    with cargo's markers are ever candidates, so the home may be a root."""
    home = env.tmp / "home"
    home.mkdir()
    monkeypatch.setattr(prune.Path, "home", classmethod(lambda cls: home))
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "w18-target", gone, root=home)
    (home / "notes").mkdir()
    rc, out = run(env, "--repo", str(env.repo), "--config", str(env.config),
                  "--ledger", str(env.ledger), "--target-root", str(home), "--apply",
                  roots=False)
    assert rc == 0, out
    assert not target.exists() and (home / "notes").exists()


# --- sweep mode: standalone clones -----------------------------------------

def _clone(env: Env, name: str) -> Path:
    root = env.tmp / "clones"
    root.mkdir(exist_ok=True)
    path = root / name
    _git(env.tmp, "clone", "-q", str(env.origin), str(path))
    _configure(path)
    return path


def run_clones(env: Env, *argv: str) -> tuple[int, str]:
    return run(env, "--repo", str(env.repo), "--config", str(env.config),
               "--ledger", str(env.ledger), "--clone-root", str(env.tmp / "clones"),
               *argv, roots=False)


def test_a_merged_clean_idle_clone_is_listed_with_nothing_keeping_it(env):
    clone = _clone(env, "pr-check")
    _backdate(clone)
    rc, out = run_clones(env)
    assert rc == 0, out
    assert clone.exists(), out
    row = next(line for line in out.splitlines() if "pr-check" in line)
    assert "list only" in row and "nothing keeps it" in row, row


def test_a_clone_whose_commit_exists_only_there_is_kept(env):
    """A WSL clone's remote is a bundle or a Windows path, so "pushed"
    means nothing; the test is whether the main repository has the commit."""
    clone = _clone(env, "local-work")
    (clone / "work.txt").write_text("only here\n", encoding="utf-8")
    _git(clone, "add", "work.txt")
    _git(clone, "commit", "-qm", "local only")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "exists only in that checkout" in out


def test_a_clone_with_an_unmerged_branch_is_kept(env):
    clone = _clone(env, "side-branch")
    _git(clone, "checkout", "-qb", "side")
    (clone / "side.txt").write_text("side\n", encoding="utf-8")
    _git(clone, "add", "side.txt")
    _git(clone, "commit", "-qm", "side work")
    _git(clone, "checkout", "-q", "master")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "a ref or reflog entry" in out


def test_a_clone_with_a_stash_is_kept(env):
    clone = _clone(env, "stashed")
    (clone / "README.md").write_text("edited\n", encoding="utf-8")
    _git(clone, "stash", "-q")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    # The stash's commit is itself off master, so the ref check keeps it
    # before the stash check is reached; either reason keeps the clone.
    assert "has a stash" in out or "a ref or reflog entry" in out


def test_a_dirty_or_fresh_clone_is_kept(env):
    dirty = _clone(env, "dirty")
    (dirty / "untracked.log").write_text("evidence\n", encoding="utf-8")
    _backdate(dirty)
    fresh = _clone(env, "fresh")
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert dirty.exists() and fresh.exists()
    assert "uncommitted changes" in out and "in grace period" in out


def test_a_registered_worktree_is_not_a_clone(env):
    root = env.tmp / "clones"
    root.mkdir()
    wt = env.add("registered", root=root)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert env.registered(wt)


# --- sweep mode: scratch dirs ----------------------------------------------

def run_scratch(env: Env, *argv: str) -> tuple[int, str]:
    root = env.tmp / "scratch"
    root.mkdir(exist_ok=True)
    (root / getattr(prune, "SCRATCH_ROOT_MARKER", ".pseudolife-scratch-root")).write_text(
        "", encoding="utf-8")
    return run(env, "--repo", str(env.repo), "--config", str(env.config),
               "--ledger", str(env.ledger), "--scratch-root", str(env.tmp / "scratch"),
               *argv, roots=False)


def _scratch(env: Env, name: str, *files: str, age: float | None = OLD) -> Path:
    path = env.tmp / "scratch" / name
    path.mkdir(parents=True)
    for rel in files or ("data.bin",):
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(b"x")
    if age is not None:
        _backdate(path, age)
    return path


def test_an_idle_scratch_dir_is_listed_not_removed(env):
    """Conductor's ruling of 16 November 2026: the scratch class is list
    only, with or without --apply; a row says that nothing keeps the dir."""
    old = _scratch(env, "w3runnertemp", "a/b.bin")
    fresh = _scratch(env, "today", age=None)
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert old.exists() and fresh.exists()
    assert "list only; nothing keeps it; last write 30.0 days ago" in out
    assert "in grace period" in out
    assert env.ledger_lines() == []


def test_scratch_holding_logs_evidence_checkouts_or_venvs_is_kept(env):
    """Maintainer ruling of 16 November 2026: logs and evidence are kept
    for ever; a checkout or virtual environment is never scratch."""
    kept = [_scratch(env, "runlogs", "out/run.log"),
            _scratch(env, "r4-evidence"),
            _scratch(env, "has-checkout", ".git/HEAD"),
            _scratch(env, "has-venv", "venv/pyvenv.cfg"),
            _scratch(env, "claude")]
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert all(p.exists() for p in kept), out
    for reason in ("holds a log", "holds evidence or logs", "holds a git checkout",
                   "holds a virtual environment", "a harness's scratchpad or a tool dir"):
        assert reason in out, out


def test_scratch_is_kept_when_the_process_list_cannot_be_read(env, monkeypatch):
    monkeypatch.setattr(prune, "list_processes", lambda: None)
    old = _scratch(env, "old")
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert old.exists()
    assert "process check unavailable" in out


def test_scratch_named_by_a_running_process_is_kept(env, monkeypatch):
    old = _scratch(env, "busy")
    monkeypatch.setattr(prune, "list_processes",
                        lambda: [(999999, 1, f"cargo test --target-dir {old}")])
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert old.exists()
    assert "in use by process 999999" in out


def test_new_root_keys_are_read_from_the_config(env, targets_root):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "from-config", gone)
    env.config.write_text(
        f"repo = {json.dumps(str(env.repo))}\n"
        f"target_roots = [{json.dumps(str(targets_root))}]\n"
        "target_grace_hours = 6\nunowned_target_days = 14\nscratch_grace_days = 14\n",
        encoding="utf-8")
    rc, out = run(env, "--config", str(env.config), "--ledger", str(env.ledger),
                  "--apply", roots=False)
    assert rc == 0, out
    assert not target.exists(), out


def test_a_clone_whose_config_can_run_a_program_is_kept(env):
    """git status in a clone runs whatever core.fsmonitor (or a filter)
    names in that clone's own config, so such a clone is never inspected
    further (security review of this change)."""
    clone = _clone(env, "fsmonitor")
    marker = env.tmp / "ran.txt"
    _git(clone, "config", "core.fsmonitor", f"touch {marker.as_posix()}")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists() and not marker.exists()
    assert "core.fsmonitor, which can run a program" in out


def test_a_scratch_tree_of_new_empty_dirs_is_not_idle(env):
    """A dir with no files has no file mtime; its dirs' own mtimes count, so
    it is not idle for ever (security review of this change)."""
    path = env.tmp / "scratch" / "dirs-only"
    (path / "a" / "b").mkdir(parents=True)
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert path.exists()
    assert "in grace period" in out


def test_tool_dirs_under_a_scratch_root_are_kept(env):
    """The Windows dev VM's dry run (16 November 2026) offered its .rustup
    toolchain as scratch when the VM's dev dir was named a scratch root. Hidden dirs,
    tool dir names and rustup or cargo homes are kept; a name may only keep."""
    kept = [_scratch(env, ".rustup"),
            _scratch(env, "bin", "uv.exe"),
            _scratch(env, "rustup-home", "toolchains/stable/x", "settings.toml"),
            _scratch(env, "cargo-home", "registry/index/x", "bin/cargo.exe")]
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert all(p.exists() for p in kept), out
    for reason in ("a hidden settings or tool dir", "a harness's scratchpad or a tool dir",
                   "a rustup or cargo home"):
        assert reason in out, out


def test_a_manifest_dir_env_dep_line_names_the_owner(env, targets_root):
    """rustc names workspace sources relative to the workspace root; the
    absolute owner is in ``# env-dep:CARGO_MANIFEST_DIR=`` lines, with
    backslashes doubled (read from a real target, 16 November 2026)."""
    gone_crate = env.tmp / "deleted-worktree" / "rust" / "shim"
    target = _target(env, "env-dep", age=None)
    escaped = str(gone_crate).replace("\\", "\\\\")
    (target / "debug" / "deps" / "x-1.d").write_text(
        "out.rlib: shim/src/lib.rs\n\nshim/src/lib.rs:\n\n"
        f"# env-dep:CARGO_MANIFEST_DIR={escaped}\n", encoding="utf-8")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert not target.exists(), out
    assert "deleted-worktree" in env.ledger_lines()[0]["note"]


# --- Codex review of the target, clone and scratch classes -----------------

def test_a_clone_that_owns_linked_worktrees_is_kept(env):
    """A linked worktree's detached commits and index live in the clone's
    .git; deleting the clone would destroy them."""
    clone = _clone(env, "has-linked")
    linked = env.tmp / "linked-elsewhere"
    _git(clone, "worktree", "add", "-q", "--detach", str(linked))
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists() and linked.exists()
    assert "it owns linked worktrees" in out


def test_a_clone_with_worktree_scoped_config_is_kept(env):
    """git also loads .git/config.worktree under extensions.worktreeConfig;
    a filter defined there would run during status."""
    clone = _clone(env, "wt-config")
    marker = env.tmp / "ran.txt"
    _git(clone, "config", "extensions.worktreeConfig", "true")
    _git(clone, "config", "--worktree", "filter.x.clean", f"touch {marker.as_posix()}")
    # Map every path to the filter, so a status run would fire it.
    (clone / ".git" / "info").mkdir(exist_ok=True)
    (clone / ".git" / "info" / "attributes").write_text("* filter=x\n", encoding="utf-8")
    (clone / "README.md").write_text("touched\n", encoding="utf-8")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists() and not marker.exists()
    assert "extensions.worktreeconfig" in out or "config.worktree" in out


def test_a_clone_whose_work_tree_points_elsewhere_is_kept(env):
    """core.worktree would make status inspect another dir, hiding an
    untracked file that exists only in the candidate."""
    clone = _clone(env, "moved-worktree")
    elsewhere = _clone(env, "clean-elsewhere")
    (clone / "only-copy.txt").write_text("unique\n", encoding="utf-8")
    _git(clone, "config", "core.worktree", str(elsewhere))
    _backdate(clone)
    _backdate(elsewhere)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert (clone / "only-copy.txt").exists()
    assert "core.worktree" in out


def test_a_target_whose_dep_info_cannot_be_read_whole_is_kept(env, targets_root, monkeypatch):
    """Every owner must be finished, so a dep-info file read only in part
    keeps the target rather than judging it on the owners it saw."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "huge-dep-info", gone)
    monkeypatch.setattr(prune, "DEP_INFO_MAX_BYTES", 10)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "its owners cannot all be read" in out


def test_a_target_pinned_by_its_finished_owner_is_kept(env, targets_root):
    clone = _clone(env, "pinning-owner")
    target = _target(env, "pinned-by-owner", _src(clone))
    (clone / "rust" / ".cargo").mkdir(parents=True)
    (clone / "rust" / ".cargo" / "config.toml").write_text(
        f"[build]\ntarget-dir = {json.dumps(str(target))}\n", encoding="utf-8")
    _backdate(clone)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "pinned as target-dir" in out


def test_scratch_waits_fourteen_days_by_default(env):
    """Maintainer ruling of 16 November 2026: unmarked scratch goes after
    14 days idle."""
    ten_days = _scratch(env, "ten-days", age=time.time() - 10 * 86400)
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert ten_days.exists()
    assert "grace 14" in out


# --- Opus review of the target, clone and scratch classes ------------------

def test_a_commit_held_only_by_a_tag_keeps_the_clone(env):
    clone = _clone(env, "tagged")
    (clone / "t.txt").write_text("only on a tag\n", encoding="utf-8")
    _git(clone, "add", "t.txt")
    _git(clone, "commit", "-qm", "tagged work")
    _git(clone, "tag", "before-rebase")
    _git(clone, "reset", "-q", "--hard", "origin/master")
    _git(clone, "reflog", "expire", "--expire=now", "--all")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "a ref or reflog entry" in out


def test_a_commit_left_on_a_detached_head_keeps_the_clone(env):
    clone = _clone(env, "detached-work")
    _git(clone, "checkout", "-q", "--detach")
    (clone / "d.txt").write_text("only in the reflog\n", encoding="utf-8")
    _git(clone, "add", "d.txt")
    _git(clone, "commit", "-qm", "detached work")
    _git(clone, "checkout", "-q", "master")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "a ref or reflog entry" in out


def test_an_assume_unchanged_file_keeps_the_clone(env):
    clone = _clone(env, "hidden-change")
    _git(clone, "update-index", "--assume-unchanged", "README.md")
    (clone / "README.md").write_text("changed but hidden\n", encoding="utf-8")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "assume-unchanged or skip-worktree" in out


def test_a_clone_with_submodules_is_kept(env):
    clone = _clone(env, "with-modules")
    (clone / ".git" / "modules" / "sub").mkdir(parents=True)
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "it holds submodules" in out


def test_the_clone_this_run_works_in_is_kept(env, monkeypatch):
    clone = _clone(env, "runs-here")
    _backdate(clone)
    monkeypatch.chdir(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "runs from here: 1" in out


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")
def test_a_shell_left_in_a_clone_keeps_it(env):
    clone = _clone(env, "shell-inside")
    _backdate(clone)
    child = subprocess.Popen(["sleep", "60"], cwd=clone)
    try:
        rc, out = run_clones(env, "--apply")
    finally:
        child.kill()
        child.wait()
    assert rc == 0, out
    assert clone.exists()
    assert f"in use by process {child.pid}" in out


def test_a_target_named_by_a_running_process_is_kept(env, targets_root, monkeypatch):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "served", gone)
    monkeypatch.setattr(prune, "list_processes",
                        lambda: [(999998, 1, f"{target / 'release' / 'server'} --port 1")])
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "in use by process 999998" in out


def test_a_lock_file_that_cannot_be_opened_counts_as_held(env, targets_root, monkeypatch):
    """Another user's build, a read-only file or a sharing violation must
    not read as "no build"."""
    import errno
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "unopenable", gone)
    real_open = prune.os.open

    def refuses(path, *a, **kw):
        if str(path).endswith(".cargo-lock"):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(prune.os, "open", refuses)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "a build holds cargo's lock" in out


def test_a_target_with_no_lock_file_is_kept(env, targets_root):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "no-lock", gone, age=None)
    (target / "debug" / ".cargo-lock").unlink()
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "no cargo build lock found" in out


def test_a_failed_partial_target_delete_keeps_cargos_markers(env, targets_root, monkeypatch):
    """If a profile dir cannot go, the markers stay, so a later sweep still
    sees a target and finishes it."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "half", gone, age=None)
    _profile(target, "release", gone)
    (target / "mutant-src").mkdir()
    _backdate(target)
    real = prune.delete_cache

    def debug_in_use(path, **kw):
        if os.path.basename(path) == "debug":
            return "in use or access denied, skipped: held open"
        return real(path, **kw)

    monkeypatch.setattr(prune, "delete_cache", debug_in_use)
    rc, out = run_targets(env, "--apply")
    assert rc == 1, out
    assert (target / "CACHEDIR.TAG").exists() and (target / ".rustc_info.json").exists()
    assert (target / "debug").exists() and not (target / "release").exists()
    assert "cargo's markers kept" in out


@pytest.mark.skipif(sys.platform != "win32", reason="a driveless path is absolute on POSIX")
def test_a_wsl_path_read_on_windows_names_no_owner(tmp_path):
    assert prune.source_owner("/home/u/repo/rust/src/lib.rs", str(tmp_path / "t"),
                              prune._tool_homes()) is None


def test_scratch_holding_results_models_databases_or_programs_is_kept(env):
    kept = [_scratch(env, "results", "out/summary.json"),
            _scratch(env, "model", "weights.gguf"),
            _scratch(env, "pgdata", "PG_VERSION"),
            _scratch(env, "tool-install", "python.exe")]
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert all(p.exists() for p in kept), out
    assert "holds results, a database, a model or a program" in out


def test_periods_must_be_finite_numbers(env):
    env.config.write_text(f"repo = {json.dumps(str(env.repo))}\nscratch_grace_days = nan\n",
                          encoding="utf-8")
    rc, out = run(env, "--config", str(env.config), roots=False)
    assert rc == 2 and "scratch_grace_days must be a number of days" in out
    with pytest.raises(SystemExit):
        prune.parse_args(["--scratch-grace-days", "nan"])
    with pytest.raises(SystemExit):
        prune.parse_args(["--target-grace-hours", "-1"])


def test_every_git_call_turns_fsmonitor_off(env):
    """core.fsmonitor names a program git runs on status; the main repo's
    own setting must not run either."""
    marker = env.tmp / "fsmonitor-ran.txt"
    env.add("watched")
    # Set after the test's own git calls, so only the script's can run it.
    _git(env.repo, "config", "core.fsmonitor", f"touch {marker.as_posix()}")
    rc, out = run(env)
    assert rc == 0, out
    assert not marker.exists()


# --- final Codex review ------------------------------------------------------

def test_a_clone_whose_config_names_a_signature_program_is_kept(env):
    """log.showSignature makes git log and reflog run gpg.program."""
    clone = _clone(env, "signing")
    marker = env.tmp / "gpg-ran.txt"
    _git(clone, "config", "log.showSignature", "true")
    _git(clone, "config", "gpg.program", f"touch {marker.as_posix()}")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists() and not marker.exists()
    assert "which can run a program" in out


def test_a_bare_repository_in_a_scratch_dir_is_kept(env):
    bare = env.tmp / "scratch" / "mirror.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(env.tmp, "init", "-q", "--bare", str(bare))
    _backdate(bare)
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert bare.exists()
    assert "holds a git repository" in out


def test_many_commits_are_checked_without_a_long_command_line(env):
    """Windows caps a command line near 32,000 characters; 900 commits in
    argv overflowed it and aborted the sweep."""
    sha = _git(env.repo, "rev-parse", "origin/master")
    assert prune.all_on_master(str(env.repo), [sha] * 900) is None


# --- conductor's review of #1654 (16 November 2026) -------------------------

def test_an_unmarked_scratch_root_refuses_the_run(env):
    """B1: a root becomes a scratch root only by a marker its user put there,
    so a misnamed root (the temp dir's parent, which holds a WSL disk image)
    refuses the run and says how to opt in."""
    root = env.tmp / "unmarked"
    old = root / "wsl"
    old.mkdir(parents=True)
    (old / "ext4.vhdx").write_bytes(b"disk")
    _backdate(old)
    rc, out = run(env, "--scratch-root", str(root), "--apply", roots=False)
    assert rc == 2, out
    assert old.exists()
    assert prune.SCRATCH_ROOT_MARKER in out and "create" in out


def test_a_marked_scratch_root_is_listed(env):
    old = _scratch(env, "w3runnertemp", "a/b.bin")
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert old.exists()
    assert "list only" in out


def test_the_temp_dir_as_a_scratch_root_is_list_only(env, monkeypatch):
    """B1 ruling 2: the temp dir needs the marker too, and even then nothing
    there is removed in this change."""
    old = _scratch(env, "w3runnertemp", "a/b.bin")
    temp = prune.norm(env.tmp / "scratch")
    monkeypatch.setattr(prune, "protected_temp_roots", lambda: [temp])
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert old.exists()
    assert "list only" in out


def test_scratch_holding_images_archives_bundles_patches_or_keys_is_kept(env):
    kept = [_scratch(env, n, f) for n, f in (
        ("disk", "ext4.vhdx"), ("backup", "bank.dump"), ("archive", "old.zip"),
        ("bundle", "w17.bundle"), ("patch", "fix.patch"), ("key", "id.pem"))]
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert all(p.exists() for p in kept), out


def test_an_empty_scratch_dir_is_kept_as_a_possible_lock_dir(env):
    empty = env.tmp / "scratch" / "some-lease"
    empty.mkdir(parents=True)
    _backdate(empty)
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert empty.exists()
    assert "holds no bytes" in out


def test_harness_name_prefixes_are_kept_and_the_near_miss_is_not(env):
    kept = [_scratch(env, n) for n in ("claude-wsl-lease", "codex-artifact-1", "t3-tmp")]
    near = _scratch(env, "notclaude")
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert all(p.exists() for p in kept), out
    assert near.exists(), out
    assert re.search(r"list only; nothing keeps it.*notclaude", out), out


def test_an_inherited_git_dir_cannot_make_a_clone_read_as_merged(env, monkeypatch):
    """B2: GIT_DIR from a caller (a hook) would point every check at the main
    repository, where the clone's unmerged branch does not exist."""
    clone = _clone(env, "side-under-git-dir")
    _git(clone, "checkout", "-qb", "side")
    (clone / "side.txt").write_text("side\n", encoding="utf-8")
    _git(clone, "add", "side.txt")
    _git(clone, "commit", "-qm", "side work")
    _git(clone, "checkout", "-q", "master")
    _backdate(clone)
    monkeypatch.setenv("GIT_DIR", str(env.repo / ".git"))
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "a ref or reflog entry" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mount points")
def test_a_bind_mount_inside_a_scratch_dir_keeps_it(env, monkeypatch):
    """S1: a bind mount on the same filesystem has the same st_dev; only the
    mount list shows it."""
    old = _scratch(env, "has-mount", "data/x.bin")
    mounted = os.path.normpath(str(old / "data"))
    monkeypatch.setattr(prune, "_mount_points", lambda: {mounted})
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert old.exists()
    assert "mount point" in out


def test_a_copied_profile_dir_in_a_target_is_foreign(env, targets_root):
    """S2: a copy of a profile (a kept candidate build) still names the
    original's outputs, so it is not cargo's own and stays."""
    import shutil
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "with-copy", gone, age=None)
    shutil.copytree(target / "debug", target / "debug-pr1234")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert (target / "debug-pr1234").exists() and not (target / "debug").exists()
    assert "debug-pr1234" in out


def test_a_second_run_at_once_is_refused(env):
    """S3: one retention run at a time."""
    lock = prune.RunLock(env.ledger.parent / "locks" / "retention.lock")
    with lock:
        rc, out = run(env)
    assert rc == 2, out
    assert "another retention run" in out


@pytest.mark.parametrize("root", ["", "relative/dir"])
def test_an_empty_or_relative_root_is_refused(env, root):
    """S4: an unset variable would otherwise sweep the current dir."""
    rc, out = run(env, "--scratch-root", root, roots=False)
    assert rc == 2, out
    assert "empty or relative" in out


def test_a_scratch_dir_that_just_arrived_is_not_idle(env, monkeypatch):
    """S5: a folder moved or extracted into a root keeps its old mtimes; its
    arrival (creation on Windows, change time on POSIX) counts."""
    monkeypatch.setattr(prune, "_arrival", REAL_ARRIVAL)
    extracted = _scratch(env, "extracted", "a/b.bin")      # mtimes 30 days old
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert extracted.exists()
    assert "in grace period" in out


def test_an_owner_path_that_cannot_be_read_keeps_the_target(env, targets_root, monkeypatch):
    """S7: a dir the sweep cannot read may hold a live owner."""
    gone = env.tmp / "unreadable-owner" / "rust" / "src" / "lib.rs"
    target = _target(env, "unreadable", gone)
    real = prune.os.lstat

    def denies(path, *a, **kw):
        if "unreadable-owner" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(prune.os, "lstat", denies)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "its owners cannot all be read" in out


def _pause_forever_elf() -> bytes:
    """A static x86-64 Linux program that blocks in pause() until killed.

    It needs no libraries, no interpreter and no particular file name or
    argv[0], so a copy of it runs anywhere. A copied system binary does not:
    a multi-call coreutils (uutils, as on the hosted ubuntu-26.04 runner)
    picks its program from its own name and exits at once under any other.
    """
    import struct
    base = 0x400000
    code_at = 64 + 56  # right after the ELF header and one program header
    # loop: mov eax, 34 (pause); syscall; jmp loop
    code = bytes.fromhex("b822000000" "0f05" "ebf7")
    size = code_at + len(code)
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        b"\x7fELF\x02\x01\x01" + bytes(9),  # 64-bit, little endian, SysV
        2, 0x3E, 1,                          # ET_EXEC, x86-64, version 1
        base + code_at, 64, 0,               # entry, phoff, no sections
        0, 64, 56, 1, 0, 0, 0,               # flags, sizes, one phdr
    )
    load = struct.pack("<IIQQQQQQ", 1, 5, 0, base, base, size, size, 0x1000)
    return header + load + code


@pytest.mark.skipif(not os.path.isdir("/proc/self") or os.uname().machine != "x86_64",
                    reason="reads /proc and runs an x86-64 Linux program")
def test_a_running_program_from_a_target_keeps_it(env, targets_root, monkeypatch):
    """S9: the executable path counts, not only the command line."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "server", gone, age=None)
    app = target / "debug" / "app"
    app.write_bytes(_pause_forever_elf())
    os.chmod(app, 0o755)
    # An output cargo names in the profile's own dep-info, so the profile
    # stays cargo's own and only the process keeps it.
    (target / "debug" / "app.d").write_text(f"{app}: {gone}\n")
    _backdate(target)
    monkeypatch.setattr(prune, "list_processes", REAL_LIST_PROCESSES)
    # argv[0] names nothing in the target: only the executable path can.
    child = subprocess.Popen(["renamed-argv0"], executable=str(app))
    try:
        # The precondition, so a program that exits early fails here and
        # not as a missing target: it runs, from inside the target.
        deadline = time.monotonic() + 10
        while True:
            assert child.poll() is None, f"the test program exited with {child.returncode}"
            try:
                if os.readlink(f"/proc/{child.pid}/exe") == os.path.realpath(app):
                    break
            except OSError:
                pass
            assert time.monotonic() < deadline, "the test program never ran from the target"
            time.sleep(0.02)
        rc, out = run_targets(env, "--apply")
        assert child.poll() is None, "the test program exited during the sweep"
    finally:
        child.kill()
        child.wait()
    assert rc == 0, out
    assert target.exists()
    assert f"in use by process {child.pid}" in out


def test_a_commit_no_ref_or_reflog_holds_keeps_the_clone(env):
    """S11: with reflogs off, a commit left on a detached HEAD is held by
    nothing but its object."""
    clone = _clone(env, "no-reflog")
    _git(clone, "config", "core.logAllRefUpdates", "false")
    _git(clone, "reflog", "expire", "--expire=now", "--all")
    _git(clone, "checkout", "-q", "--detach")
    (clone / "d.txt").write_text("object only\n", encoding="utf-8")
    _git(clone, "add", "d.txt")
    _git(clone, "commit", "-qm", "object only")
    _git(clone, "checkout", "-q", "master")
    _git(clone, "reflog", "expire", "--expire=now", "--all")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "a ref or reflog entry" in out


def test_untracked_files_show_whatever_the_clone_config_says(env):
    """N2: status.showUntrackedFiles=no in a clone must not hide work."""
    clone = _clone(env, "hides-untracked")
    _git(clone, "config", "status.showUntrackedFiles", "no")
    (clone / "only-here.txt").write_text("unique\n", encoding="utf-8")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "uncommitted changes" in out


def test_a_dotted_target_dir_pin_keeps_the_target(env, targets_root):
    """N6: ``build.target-dir = ...`` pins as the table form does."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "dotted-pin", gone)
    holder = env.add("dotted-holder")
    (holder / ".cargo").mkdir()
    (holder / ".cargo" / "config.toml").write_text(
        f"build.target-dir = {json.dumps(str(target))}\n", encoding="utf-8")
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "pinned as target-dir" in out


def test_a_clone_whose_git_dir_names_a_commondir_is_kept(env):
    """N12: commondir makes git read another dir's config and objects."""
    clone = _clone(env, "commondir")
    (clone / ".git" / "commondir").write_text(str(env.repo / ".git") + "\n", encoding="utf-8")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "commondir" in out


# --- fresh Codex pass on the review round ------------------------------------

def test_a_keep_marker_keeps_a_scratch_dir(env):
    kept = _scratch(env, "kept-on-purpose", "data.bin", ".pseudolife-keep")
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert kept.exists()
    assert "keep marker" in out


# --- conductor's round 2 on #1654 --------------------------------------------

def test_scratch_is_list_only_with_apply_under_any_root_spelling(env):
    """V1 and V2 are moot: no scratch row is ever removed, whatever the
    root is called, and a run writes no sightings and no ledger line."""
    old = _scratch(env, "anything", "a/b.bin")
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert old.exists()
    assert env.ledger_lines() == []
    assert not (env.tmp / "scratch-sightings.json").exists()


def test_a_marker_that_is_a_dir_does_not_mark_a_root(env):
    root = env.tmp / "dir-marker"
    (root / ".pseudolife-scratch-root").mkdir(parents=True)
    rc, out = run(env, "--scratch-root", str(root), roots=False)
    assert rc == 2 and "has no .pseudolife-scratch-root file" in out, out


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_marker_that_is_a_link_does_not_mark_a_root(env):
    root = env.tmp / "link-marker"
    root.mkdir()
    real = env.tmp / "elsewhere-marker"
    real.write_text("", encoding="utf-8")
    os.symlink(real, root / ".pseudolife-scratch-root")
    rc, out = run(env, "--scratch-root", str(root), roots=False)
    assert rc == 2 and "not a link" in out, out


def test_an_inherited_git_config_cannot_hide_a_keep_marker(env, monkeypatch):
    """S-b: GIT_CONFIG pointing at an empty file made git read no other
    config, so `pseudolife.keep true` read as unset (verified on review)."""
    clone = _clone(env, "kept-by-config")
    _git(clone, "config", "pseudolife.keep", "true")
    _backdate(clone)
    empty = env.tmp / "empty.gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG", str(empty))
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "keep marker" in out


def test_a_users_file_in_a_profile_keeps_that_profile(env, targets_root):
    """S-c: a copied server-good.exe or notes beside a build, newer than the
    profile's fingerprints, make the profile foreign; it stays."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "with-user-file", gone, age=None)
    (target / "debug" / ".fingerprint" / "thing-1234").mkdir()
    (target / "debug" / ".fingerprint" / "thing-1234" / "lib-thing").write_text("x")
    _backdate(target)
    user = target / "debug" / "server-good.exe"
    user.write_bytes(b"kept build")
    os.utime(user, (OLD + 86400, OLD + 86400))      # after the last build, still old
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert user.exists()
    assert "nothing of cargo's own" in out


def test_cargos_own_outputs_in_a_profile_still_go(env, targets_root):
    """The near miss: an output cargo names in the profile's own dep-info,
    with its debug info, is cargo's and goes with the profile."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "with-outputs", gone, age=None)
    prof = target / "debug"
    (prof / "app.exe").write_bytes(b"binary")
    (prof / "app.pdb").write_bytes(b"debug info")
    (prof / "app.d").write_text(f"{prof / 'app.exe'}: {gone}\n", encoding="utf-8")
    (prof / ".fingerprint" / "app-1").mkdir()
    (prof / ".fingerprint" / "app-1" / "bin-app").write_text("x")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert not target.exists(), out


def test_criterion_measurements_in_a_target_are_kept(env, targets_root):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "benches", gone, age=None)
    (target / "criterion" / "parse" / "base").mkdir(parents=True)
    (target / "criterion" / "parse" / "base" / "estimates.json").write_text("{}")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert (target / "criterion" / "parse" / "base" / "estimates.json").exists()
    assert not (target / "debug").exists()


def test_criterion_measurements_in_a_clone_keep_it(env):
    clone = _clone(env, "benched")
    (clone / "rust" / "target" / "criterion" / "x").mkdir(parents=True)
    (clone / "rust" / "target" / "criterion" / "x" / "estimates.json").write_text("{}")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "criterion measurements" in out


def test_a_write_during_the_checks_renames_the_dir_back(env, targets_root, monkeypatch):
    """S-d: a file written after the checks began, found in the aside copy,
    puts the dir back and keeps it."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "written-late", gone)
    real_rename = prune.os.rename

    def rename_then_write(src, dst, *a, **kw):
        real_rename(src, dst, *a, **kw)
        if ".retention-" in os.path.basename(str(dst)):
            (Path(dst) / "late.txt").write_text("written during the checks")

    monkeypatch.setattr(prune.os, "rename", rename_then_write)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert (target / "late.txt").exists() and (target / "debug").exists()
    assert "written during the checks; renamed back" in out
    assert env.ledger_lines() == []


def test_a_process_that_appears_before_the_delete_keeps_it(env, targets_root, monkeypatch):
    """S-d: the process list is taken again just before each delete."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "started-late", gone)
    calls = []

    def listing():
        calls.append(1)
        # Empty for the scan and the re-check, then a process using it.
        return [] if len(calls) < 3 else [(999997, 1, f"{target}/debug/app --serve")]

    monkeypatch.setattr(prune, "list_processes", listing)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "process 999997 uses it" in out


def test_a_clone_with_an_operation_in_progress_is_kept(env):
    """T4: an interrupted merge, rebase or am keeps work only in .git."""
    clone = _clone(env, "mid-merge")
    (clone / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "MERGE_HEAD" in out


def test_a_clone_with_a_hook_of_its_own_is_kept(env):
    clone = _clone(env, "hooked")
    (clone / ".git" / "hooks").mkdir(exist_ok=True)
    (clone / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "hook script of its own" in out


def test_a_target_whose_profiles_hold_no_dep_info_is_kept(env, targets_root):
    """T7: with no dep-info nothing shows a profile is cargo's own."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "no-dep-info", gone, age=None)
    for d in (target / "debug" / "deps").glob("*.d"):
        d.unlink()
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "nothing of cargo's own" in out


# --- read-only Codex pass on round 2 -----------------------------------------

def test_an_old_user_file_in_a_profile_still_keeps_it(env, targets_root):
    """Provenance, not age: a saved server-good.exe older than a later
    build's fingerprints is still not cargo's, so its profile stays."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "old-user-file", gone, age=None)
    (target / "debug" / ".fingerprint" / "thing-1").mkdir()
    (target / "debug" / ".fingerprint" / "thing-1" / "lib-thing").write_text("x")
    (target / "debug" / "server-good.exe").write_bytes(b"kept build")
    _backdate(target)                       # as old as everything else
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert (target / "debug" / "server-good.exe").exists()
    assert "nothing of cargo's own" in out


def test_an_unknown_dir_in_a_profile_keeps_it(env, targets_root):
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "unknown-dir", gone, age=None)
    (target / "debug" / "saved-results").mkdir()
    (target / "debug" / "saved-results" / "run.txt").write_text("numbers")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert (target / "debug" / "saved-results" / "run.txt").exists()


def test_debug_info_with_underscores_is_cargos_own(env, targets_root):
    """cargo writes pseudolife_stdio.pdb beside pseudolife-stdio.exe."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "underscores", gone, age=None)
    prof = target / "debug"
    (prof / "my-tool.exe").write_bytes(b"binary")
    (prof / "my_tool.pdb").write_bytes(b"debug info")
    (prof / "my-tool.d").write_text(f"{prof / 'my-tool.exe'}: {gone}\n", encoding="utf-8")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert not target.exists(), out


def test_a_clone_another_clone_borrows_objects_from_is_kept(env):
    """``git clone --shared A B`` leaves no trace in A; B's alternates file
    is the only sign, and deleting A would break B."""
    lender = _clone(env, "lender")
    borrower = env.tmp / "clones" / "borrower"
    _git(env.tmp, "clone", "-q", "--shared", str(lender), str(borrower))
    _configure(borrower)
    (borrower / "work.txt").write_text("only here\n", encoding="utf-8")
    _git(borrower, "add", "work.txt")
    _git(borrower, "commit", "-qm", "borrower work")
    _backdate(lender)
    _backdate(borrower)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert lender.exists() and borrower.exists()
    assert "another checkout borrows its objects" in out


def test_a_root_that_is_also_a_scratch_root_deletes_nothing(env):
    """A scratch root is list only, and so is a clone root reaching it: the
    clone is listed once, by the clone rule, and stays."""
    clone = _clone(env, "under-scratch")
    _backdate(clone)
    root = env.tmp / "clones"
    (root / ".pseudolife-scratch-root").write_text("", encoding="utf-8")
    rc, out = run(env, "--clone-root", str(root), "--scratch-root", str(root), "--apply",
                  roots=False)
    assert rc == 0, out
    assert clone.exists() and (clone / ".git").is_dir()
    assert sum("under-scratch" in line for line in out.splitlines()) == 1, out
    assert env.ledger_lines() == []


def test_a_timestamp_preserving_copy_after_the_checks_renames_back(env, targets_root, monkeypatch):
    """A file copied in with its old mtime (cp -p) still changes its dir."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "copied-late", gone)
    real_rename = prune.os.rename

    def rename_then_copy(src, dst, *a, **kw):
        real_rename(src, dst, *a, **kw)
        if ".retention-" in os.path.basename(str(dst)):
            copied = Path(dst) / "debug" / "deps" / "kept.bin"
            copied.write_bytes(b"old data")
            os.utime(copied, (OLD, OLD))           # the copy keeps its old time

    monkeypatch.setattr(prune.os, "rename", rename_then_copy)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert (target / "debug" / "deps" / "kept.bin").exists()
    assert "written during the checks; renamed back" in out


def test_named_scratch_rows_still_show_size_and_last_write(env):
    big = _scratch(env, "claude-session")
    (big / "data.bin").write_bytes(b"x" * 5000)
    _backdate(big)
    rc, out = run_scratch(env)
    assert rc == 0, out
    row = next(line for line in out.splitlines() if "claude-session" in line)
    assert "5.0 kB" in row and "last write 30.0 days ago" in row, row


# --- final read-only Codex pass ----------------------------------------------

def test_a_worktree_root_that_is_also_a_scratch_root_removes_no_worktree(env):
    """A scratch root is list only, also when a worktree root reaches it."""
    wt = env.add("under-scratch-wt")
    (env.wt_root / ".pseudolife-scratch-root").write_text("", encoding="utf-8")
    rc, out = run(env, "--worktree-root", str(env.wt_root), "--scratch-root", str(env.wt_root),
                  "--apply", roots=False)
    assert rc == 0, out
    assert env.registered(wt) and wt.exists()
    assert "at or around a scratch root: 1" in out


def test_a_scratch_listing_shows_a_registered_worktree_and_what_keeps_it(env):
    wt = env.add("listed-wt")
    (env.wt_root / ".pseudolife-scratch-root").write_text("", encoding="utf-8")
    rc, out = run(env, "--scratch-root", str(env.wt_root), roots=False)
    assert rc == 0, out
    row = next(line for line in out.splitlines() if "listed-wt" in line)
    assert "registered worktree" in row, row


def test_a_lent_clone_is_kept_when_only_a_linked_worktree_of_the_borrower_is_seen(env):
    """The borrower clone is outside every root, and only its linked
    worktree sits under one; its alternates live in the borrower's common
    git dir, which the worktree's .git file points to."""
    lender = _clone(env, "lender-2")
    outside = env.tmp / "outside" / "borrower"
    outside.parent.mkdir()
    _git(env.tmp, "clone", "-q", "--shared", str(lender), str(outside))
    _git(outside, "worktree", "add", "-q", "--detach", str(env.tmp / "clones" / "borrower-wt"))
    _backdate(lender)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert lender.exists()
    assert "another checkout borrows its objects" in out


# --- conductor's round 3 -------------------------------------------------------

def _profile(target: Path, name: str, *sources: Path) -> Path:
    """Another profile dir as cargo writes it, naming its outputs inside it."""
    prof = target / name
    deps = prof / "deps"
    deps.mkdir(parents=True)
    (prof / ".fingerprint").mkdir()
    (prof / ".cargo-lock").write_bytes(b"")
    (deps / "libthing.rlib").write_bytes(b"x" * 1000)
    (deps / "thing-1234.d").write_text(
        f"{deps / 'libthing.rlib'}: " + " ".join(str(s) for s in sources) + "\n",
        encoding="utf-8")
    return prof


def test_clones_are_list_only_even_with_apply(env):
    """Conductor's ruling (round 3): the clone class never deletes."""
    clone = _clone(env, "done-clone")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists() and (clone / ".git").is_dir()
    row = next(line for line in out.splitlines() if "done-clone" in line)
    assert "list only" in row and "nothing keeps it" in row, row
    assert "clones: 1 listed, never removed; 1 with nothing keeping them" in out
    assert env.ledger_lines() == []


def test_a_clone_row_never_reaches_the_delete_path(env, monkeypatch):
    clone = _clone(env, "no-delete")
    _backdate(clone)
    calls = []
    monkeypatch.setattr(prune, "delete_cache", lambda *a, **kw: calls.append(a) or None)
    monkeypatch.setattr(prune, "delete_target", lambda *a, **kw: calls.append(a) or None)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert calls == [] and clone.exists()


def test_a_clone_whose_rust_target_holds_a_user_file_says_so(env):
    clone = _clone(env, "kept-binary")
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    _target(env, "target", gone, root=clone / "rust")
    (clone / "rust" / "target" / "debug" / "server-good.exe").write_bytes(b"keep me")
    (clone / ".git" / "info").mkdir(exist_ok=True)
    (clone / ".git" / "info" / "exclude").write_text("rust/target/\n", encoding="utf-8")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    row = next(line for line in out.splitlines() if "kept-binary" in line)
    assert "its rust/target holds entries cargo does not write there" in row, row
    assert "debug" in row, row


def test_a_clone_whose_hooks_cannot_be_listed_is_kept_with_the_reason(env):
    clone = _clone(env, "odd-hooks")
    hooks = clone / ".git" / "hooks"
    for f in hooks.iterdir():
        f.unlink()
    hooks.rmdir()
    hooks.write_text("not a dir", encoding="utf-8")
    _backdate(clone)
    rc, out = run_clones(env)
    assert rc == 0, out
    row = next(line for line in out.splitlines() if "odd-hooks" in line)
    assert "cannot list its hooks" in row, row


def test_tmp_doc_and_package_stay_without_cargo_provenance(env, targets_root):
    """S6: cargo writes these too, but nothing in them says cargo wrote
    them, so they are kept like any foreign entry."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "named-dirs", gone, age=None)
    for name in ("tmp", "doc", "package"):
        (target / name).mkdir()
        (target / name / "notes.txt").write_text("mine\n", encoding="utf-8")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert not (target / "debug").exists()
    for name in ("tmp", "doc", "package"):
        assert (target / name / "notes.txt").exists(), name
    assert "kept, not cargo's (" in out and "doc, package, tmp" in out


def test_a_binary_copied_in_with_its_dep_info_keeps_the_profile(env, targets_root):
    """S2: a .d file copied in beside its binary names the profile it was
    built in, so the binary is not cargo's output here."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "copied-pair", gone, age=None)
    elsewhere = env.tmp / "other-target" / "release" / "server-good.exe"
    (target / "debug" / "server-good.exe").write_bytes(b"a kept candidate")
    (target / "debug" / "server-good.d").write_text(f"{elsewhere}: {gone}\n", encoding="utf-8")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert (target / "debug" / "server-good.exe").exists()
    assert (target / "debug" / "deps" / "libthing.rlib").exists()


def test_an_output_named_inside_the_profile_is_still_cargos(env, targets_root):
    """The near miss of the copied pair: cargo's own top-level output and
    its .d file, naming this profile, are deleted with it."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "own-output", gone, age=None)
    exe = target / "debug" / "app.exe"
    exe.write_bytes(b"built here")
    (target / "debug" / "app.d").write_text(f"{exe}: {gone}\n", encoding="utf-8")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert not target.exists(), out


def _fail_rename_back(monkeypatch):
    """Rename aside works; renaming back fails, after a second look that
    always finds a late write."""
    real_rename = prune.os.rename

    def rename(src, dst, *a, **kw):
        if ".retention-" in os.path.basename(str(src)):
            raise PermissionError(13, "Access is denied")
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(prune.os, "rename", rename)
    monkeypatch.setattr(prune, "_newest_dir_fresh", lambda path: float("inf"))


def test_a_failed_rename_back_of_a_whole_target_says_where_it_is(env, targets_root, monkeypatch):
    """S3: nothing was deleted, so no ledger line, and the row says how to
    put the folder back."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "stuck-aside", gone)
    _fail_rename_back(monkeypatch)
    rc, out = run_targets(env, "--apply")
    assert rc == 1, out
    [aside] = [p for p in targets_root.iterdir() if ".retention-" in p.name]
    assert (aside / "debug" / "deps" / "libthing.rlib").exists()
    assert not target.exists()
    assert "kept aside at" in out and "nothing deleted" in out
    assert "to put it back, rename" in out
    assert "deleted in part" not in out
    assert env.ledger_lines() == []


def test_a_failed_rename_back_of_one_profile_is_not_a_partial_delete(env, targets_root,
                                                                     monkeypatch):
    """S3, per-profile path (a foreign entry keeps the dir): a profile kept
    aside is reported as nothing deleted, with no ledger line."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "stuck-profile", gone, age=None)
    (target / "notes.txt").write_text("mine\n", encoding="utf-8")
    _backdate(target)
    _fail_rename_back(monkeypatch)
    rc, out = run_targets(env, "--apply")
    assert rc == 1, out
    [aside] = [p for p in target.iterdir() if ".retention-" in p.name]
    assert (aside / "deps" / "libthing.rlib").exists()
    assert "kept aside, nothing deleted" in out and "to put it back, rename" in out
    assert "deleted in part" not in out
    assert env.ledger_lines() == []


def test_a_kept_aside_profile_beside_a_deleted_one_is_partial(env, targets_root, monkeypatch):
    """When one profile went and another is stuck aside, something was
    deleted: a partial ledger line, and the row names the aside folder."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "half-aside", gone, age=None)
    _profile(target, "release", gone)
    (target / "notes.txt").write_text("mine\n", encoding="utf-8")
    _backdate(target)
    real = prune.delete_cache

    def debug_stuck(path, **kw):
        if os.path.basename(path) == "debug":
            return f"kept aside at {path}.x, nothing deleted: late write, and renaming it back failed"
        return real(path, **kw)

    monkeypatch.setattr(prune, "delete_cache", debug_stuck)
    rc, out = run_targets(env, "--apply")
    assert rc == 1, out
    assert not (target / "release").exists()
    assert "deleted in part" in out and "kept aside at" in out
    [line] = env.ledger_lines()
    assert line["complete"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="mountinfo is Linux's")
def test_a_bind_mount_made_during_the_run_is_seen_by_the_second_look(env, targets_root,
                                                                     monkeypatch):
    """S8: the mount list is read again after the rename aside."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "mounted-late", gone)
    info = env.tmp / "mountinfo"
    info.write_text("22 1 0:1 / / rw - ext4 /dev/sda1 rw\n", encoding="utf-8")
    monkeypatch.setattr(prune, "MOUNTINFO", str(info))
    monkeypatch.setattr(prune, "_MOUNTS", [])
    real_rename = prune.os.rename

    def rename_then_mount(src, dst, *a, **kw):
        real_rename(src, dst, *a, **kw)
        if ".retention-" in os.path.basename(str(dst)):
            inner = os.path.join(str(dst), "debug", "deps")
            with open(info, "a", encoding="utf-8") as fh:
                fh.write(f"99 22 0:1 /x {inner} rw - ext4 /dev/sda1 rw\n")

    monkeypatch.setattr(prune.os, "rename", rename_then_mount)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert (target / "debug" / "deps" / "libthing.rlib").exists()
    assert "a link appeared" in out and "renamed back" in out


@pytest.mark.parametrize("names", [
    ("GIT_DIR",), ("GIT_WORK_TREE",), ("GIT_INDEX_FILE",), ("GIT_COMMON_DIR",),
    ("GIT_OBJECT_DIRECTORY",), ("GIT_ALTERNATE_OBJECT_DIRECTORIES",), ("GIT_NAMESPACE",),
    ("GIT_CEILING_DIRECTORIES",), ("GIT_DISCOVERY_ACROSS_FILESYSTEM",), ("GIT_CONFIG",),
    ("GIT_CONFIG_PARAMETERS",), ("GIT_CONFIG_GLOBAL",), ("GIT_CONFIG_SYSTEM",),
    ("GIT_CONFIG_NOSYSTEM",), ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"),
])
def test_every_git_redirect_variable_is_scrubbed(monkeypatch, names):
    for name in names:
        monkeypatch.setenv(name, "x")
    env = prune.git_env()
    assert not any(k.upper() in names for k in env), names
    assert env["GIT_NO_LAZY_FETCH"] == "1" and env["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_a_config_count_pair_cannot_redirect_a_clones_work_tree(env, monkeypatch):
    """Behaviour, not just the env: an inherited GIT_CONFIG_COUNT that would
    move the clone's work tree elsewhere is not seen by git."""
    clone = _clone(env, "count-pair")
    _backdate(clone)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(env.tmp))
    rc, out = run_clones(env)
    assert rc == 0, out
    row = next(line for line in out.splitlines() if "count-pair" in line)
    assert "nothing keeps it" in row, row


def _marked_scratch_root(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".pseudolife-scratch-root").write_text("", encoding="utf-8")
    return path


def test_a_target_inside_a_scratch_root_is_not_deleted(env, targets_root):
    """S7: a target root that is also a scratch root deletes nothing there."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "in-scratch", gone)
    _marked_scratch_root(targets_root)
    rc, out = run_targets(env, "--scratch-root", str(targets_root), "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "at or around a scratch root: 1" in out


def test_a_target_around_a_scratch_root_is_not_deleted(env, targets_root):
    """S7, the other direction: a target that contains a scratch root."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "around-scratch", gone, age=None)
    _marked_scratch_root(target / "debug" / "scratch")
    _backdate(target)
    rc, out = run_targets(env, "--scratch-root", str(target / "debug" / "scratch"), "--apply")
    assert rc == 0, out
    assert target.exists()
    assert "at or around a scratch root: 1" in out


def test_a_sibling_named_like_a_scratch_root_is_still_judged(env, targets_root):
    """S7 near miss: a prefix of the name is not inside the root."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    _marked_scratch_root(targets_root / "scratch")
    target = _target(env, "scratch-old", gone)
    rc, out = run_targets(env, "--scratch-root", str(targets_root / "scratch"), "--apply")
    assert rc == 0, out
    assert not target.exists(), out


def test_a_cache_dir_inside_a_scratch_root_is_not_deleted(env):
    root = _marked_scratch_root(env.tmp / "shared")
    stale = root / "old-slice"
    stale.mkdir()
    (stale / "x.bin").write_bytes(b"x")
    _backdate(stale)
    rc, out = run(env, "--cache-root", str(root), "--scratch-root", str(root), "--apply",
                  roots=False)
    assert rc == 0, out
    assert stale.exists()
    assert "at or around a scratch root: 1" in out


def test_a_cache_root_that_is_also_a_clone_root_deletes_no_clone(env):
    """S7: clones are list only, also when a cache root reaches them; the
    plain cache dir beside them is still judged."""
    clone = _clone(env, "in-cache-root")
    root = env.tmp / "clones"
    stale = root / "old-slice"
    stale.mkdir()
    (stale / "x.bin").write_bytes(b"x")
    _backdate(clone)
    _backdate(stale)
    rc, out = run(env, "--cache-root", str(root), "--clone-root", str(root), "--apply",
                  roots=False)
    assert rc == 0, out
    assert clone.exists() and (clone / ".git").is_dir()
    assert not stale.exists(), out
    assert "at or around a clone: 1" in out


def test_a_target_inside_a_clone_is_not_deleted(env):
    """S7: a target root inside a clone reaches a list-only dir."""
    clone = _clone(env, "with-target")
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "target", gone, root=clone / "rust")
    _backdate(clone)
    rc, out = run(env, "--target-root", str(clone / "rust"), "--clone-root",
                  str(env.tmp / "clones"), "--apply", roots=False)
    assert rc == 0, out
    assert target.exists()
    assert "at or around a clone: 1" in out


def test_a_target_beside_clones_in_one_root_is_still_judged(env):
    """S7 near miss: one root holding both clones and targets (the WSL
    home) still deletes the target, never the clone."""
    clone = _clone(env, "beside")
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "beside-target", gone, root=env.tmp / "clones")
    _backdate(clone)
    root = str(env.tmp / "clones")
    rc, out = run(env, "--target-root", root, "--clone-root", root, "--apply", roots=False)
    assert rc == 0, out
    assert not target.exists(), out
    assert clone.exists()


def test_a_skipped_scratch_root_overlapping_another_root_refuses(env, tmp_path, monkeypatch):
    """N2: a scratch root left out (here the home dir, too broad) must not
    leave what is under it to a deleting class."""
    home = tmp_path / "home" / "user"
    _marked_scratch_root(home)
    monkeypatch.setattr(prune.Path, "home", classmethod(lambda cls: home))
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "under-home", gone, root=home / "targets")
    rc, out = run(env, "--target-root", str(home / "targets"), "--scratch-root", str(home),
                  "--apply", roots=False)
    assert rc != 0, out
    assert target.exists()
    assert "is skipped but overlaps the root" in out


# --- read-only Codex pass at 56f9fdc90 -----------------------------------------

def test_a_binary_whose_name_differs_only_by_underscores_stays(env, targets_root):
    """Cargo's own my-tool.exe makes my_tool.pdb its debug info, but a
    my_tool.exe beside it is someone's copy, not cargo's output."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "underscore-copy", gone, age=None)
    exe = target / "debug" / "my-tool.exe"
    exe.write_bytes(b"cargo's")
    (target / "debug" / "my-tool.d").write_text(f"{exe}: {gone}\n", encoding="utf-8")
    (target / "debug" / "my_tool.exe").write_bytes(b"a kept candidate")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert (target / "debug" / "my_tool.exe").exists()
    assert (target / "debug" / "deps" / "libthing.rlib").exists()


def test_an_underscored_pdb_beside_a_hyphenated_exe_is_still_cargos(env, targets_root):
    """The near miss: the debug info cargo names with underscores goes."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "underscore-pdb", gone, age=None)
    exe = target / "debug" / "my-tool.exe"
    exe.write_bytes(b"cargo's")
    (target / "debug" / "my-tool.d").write_text(f"{exe}: {gone}\n", encoding="utf-8")
    (target / "debug" / "my_tool.pdb").write_bytes(b"debug info")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert not target.exists(), out


def test_a_deps_binary_copied_to_the_profile_top_with_its_d_stays(env, targets_root):
    """A copy of deps/app-123.exe and its .d at the profile's top names an
    output inside deps, not at the top, so the copy is not cargo's."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "deps-copy", gone, age=None)
    inner = target / "debug" / "deps" / "app-123.exe"
    inner.write_bytes(b"cargo's")
    (target / "debug" / "app-123.exe").write_bytes(b"a kept candidate")
    (target / "debug" / "app-123.d").write_text(f"{inner}: {gone}\n", encoding="utf-8")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert (target / "debug" / "app-123.exe").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_a_fifo_at_a_profile_top_keeps_the_profile(env, targets_root):
    """A FIFO or socket at the top is not a file cargo writes there."""
    gone = env.tmp / "deleted-worktree" / "rust" / "src" / "lib.rs"
    target = _target(env, "with-fifo", gone, age=None)
    os.mkfifo(target / "debug" / "ipc")
    _backdate(target)
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert os.path.lexists(target / "debug" / "ipc")
