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
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
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
    if sources:
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
    rc, out = run_targets(env, "--apply")
    assert rc == 0, out
    assert sorted(p.name for p in target.iterdir()) == ["cand-final.exe", "mutant-src"]
    assert "kept, not cargo's: cand-final.exe, mutant-src" in out


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


def test_a_merged_clean_idle_clone_is_removed(env):
    clone = _clone(env, "pr-check")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert not clone.exists(), out
    [line] = env.ledger_lines()
    assert line["kind"] == "clone" and line["complete"] is True


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
    assert "a local branch" in out


def test_a_clone_with_a_stash_is_kept(env):
    clone = _clone(env, "stashed")
    (clone / "README.md").write_text("edited\n", encoding="utf-8")
    _git(clone, "stash", "-q")
    _backdate(clone)
    rc, out = run_clones(env, "--apply")
    assert rc == 0, out
    assert clone.exists()
    assert "has a stash" in out


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


def test_an_idle_scratch_dir_is_removed(env):
    old = _scratch(env, "w3runnertemp", "a/b.bin")
    fresh = _scratch(env, "today", age=None)
    rc, out = run_scratch(env, "--apply")
    assert rc == 0, out
    assert not old.exists() and fresh.exists()
    assert [l["kind"] for l in env.ledger_lines()] == ["scratch"]


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
                   "holds a virtual environment", "a harness's scratch root"):
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
