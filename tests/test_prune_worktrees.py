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
    assert "files remain" in out
    assert cache.exists(), "a partial removal must not go on to the cache"
