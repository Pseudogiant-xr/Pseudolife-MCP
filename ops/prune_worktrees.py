#!/usr/bin/env python3
"""Reclaim finished git worktrees and stale build caches, safely.

    python ops/prune_worktrees.py                       # sweep, dry run
    python ops/prune_worktrees.py --apply               # sweep, remove
    python ops/prune_worktrees.py --mine [--cache-dir D] [--apply]

Two modes:

* **Sweep** (the weekly backstop) looks at every worktree of ``--repo`` under
  a configured ``--worktree-root`` and every immediate subdir of a
  configured ``--cache-root``.
* **Owner** (``--mine``, the primary path) is run by a session whose work
  has all merged: it targets only the worktree it runs from plus its own
  cache dir (``--cache-dir``, else ``$CARGO_TARGET_DIR``). The owner asserts
  it is done, so the grace period is skipped; every other check still
  applies, and any failed check refuses the whole run with nothing changed.

A worktree is removed only when every check passes, re-checked immediately
before removal: its HEAD is an ancestor of ``origin/master`` after a fresh
fetch; ``git status`` shows no changes and every ignored path is a known
cache; nothing under it is a symlink or reparse point (``git worktree remove
--force`` once followed a Windows junction and deleted real data); nothing
in it was written within the grace period; it carries no keep marker; it is
neither the main worktree nor the one this script runs from. Removal is a
plain ``git worktree remove`` (never ``--force``); local branches are kept.

A cache dir is deleted only when nothing under it was written within the
cache grace period (owner mode: no running process names it), it holds no
symlink or reparse point, no ``.cargo/config.toml`` pins it as
``target-dir``, and it can be renamed aside (a held lock is a skip, never a
retry). Harness-managed checkouts (``.claude/worktrees``, ``.t3/worktrees``,
``.codex/worktrees``, temp dirs) are never removed.

Sweep mode also judges cargo targets by the checkouts their dep-info names
(``--target-root``), standalone clones (``--clone-root``) and idle scratch
dirs (``--scratch-root``); see the section "cargo targets, standalone
clones, scratch dirs" below.

Every removal appends a JSON line to ``~/.pseudolife-mcp/retention-ledger.jsonl``.
Roots come from flags or ``~/.pseudolife-mcp/retention.toml``; nothing is
configured by default. Runbook: docs/dev/worktree-retention.md.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DAY = 86400.0
DEFAULT_WORKTREE_GRACE_DAYS = 3.0
DEFAULT_CACHE_GRACE_DAYS = 7.0
STATE_DIR = Path.home() / ".pseudolife-mcp"
DEFAULT_CONFIG = STATE_DIR / "retention.toml"
DEFAULT_LEDGER = STATE_DIR / "retention-ledger.jsonl"
KEEP_FILE = ".pseudolife-keep"
KEEP_CONFIG = "pseudolife.keep"
MASTER = "refs/remotes/origin/master"
UNREADABLE_PIN = "<unreadable pin config>"   # never a real path, so never matches one

# Ignored paths a removable worktree may hold: rebuildable caches only.
CACHE_COMPONENTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
CACHE_PREFIXES = ("rust/target",)

# Checkouts a harness owns and cleans up itself; never ours to remove.
HARNESS_MARKERS = ((".claude", "worktrees"), (".t3", "worktrees"), (".codex", "worktrees"))

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class Refusal(Exception):
    """The whole run is refused; nothing was changed."""


# --- paths -----------------------------------------------------------------

def norm(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_under(path: str, root: str) -> bool:
    """Both normalized. True when ``path`` is ``root`` or inside it."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def protected_temp_roots() -> list[str]:
    roots = {tempfile.gettempdir()}
    for var in ("TEMP", "TMP", "TMPDIR"):
        if os.environ.get(var):
            roots.add(os.environ[var])
    return [norm(r) for r in roots]


def harness_managed(path: str) -> bool:
    """Checked on the path as given and as resolved, so a harness dir that
    is itself a link to a configured root still counts as the harness's."""
    temps = protected_temp_roots()
    for form in {os.path.normcase(os.path.abspath(path)), norm(path)}:
        parts = [p.lower() for p in Path(form).parts]
        for a, b in HARNESS_MARKERS:
            if any(parts[i] == a and parts[i + 1] == b for i in range(len(parts) - 1)):
                return True
        if any(is_under(form, norm(root)) for root in temps):
            return True
    return False


def too_broad(path: str) -> bool:
    """A drive root, the home dir or one of its ancestors: never a root or
    a cache dir, whatever the configuration says."""
    path = norm(path)
    home = norm(Path.home())
    return (path == norm(os.path.splitdrive(path)[0] + os.sep) or path == os.sep
            or is_under(home, path))


def whitelisted(rel: str) -> bool:
    """``rel`` is a forward-slash path relative to the worktree root."""
    rel = rel.strip("/")
    if any(rel == p or rel.startswith(p + "/") for p in CACHE_PREFIXES):
        return True
    return any(part in CACHE_COMPONENTS or part.endswith(".egg-info")
               for part in rel.split("/"))


def _long(path: str) -> str:
    """Windows extended-length form, so deep ``rust/target`` trees walk."""
    if sys.platform != "win32" or path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & _REPARSE)


_MOUNTS: list[set[str] | None] = []


def _mount_points() -> set[str] | None:
    """Every mount point this process sees (Linux, from mountinfo), read
    once per run; None where /proc is missing, so the walk asks
    os.path.ismount instead. A bind mount on the same filesystem has the
    same st_dev, so only this list shows it."""
    if not _MOUNTS:
        found: set[str] | None = None
        try:
            with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
                found = set()
                for line in fh:
                    fields = line.split()
                    if len(fields) > 4:
                        found.add(os.path.normpath(
                            fields[4].replace("\\040", " ").replace("\\011", "\t")))
        except OSError:
            found = None
        _MOUNTS.append(found)
    return _MOUNTS[0]


def _arrival(st: os.stat_result) -> float:
    """When an entry arrived where it is: its creation time on Windows and
    its change time on POSIX (a rename or an extraction sets it), so a
    folder moved or unpacked into a root is not idle on arrival."""
    if sys.platform == "win32":
        return getattr(st, "st_birthtime", None) or st.st_ctime
    return st.st_ctime


@dataclass
class Scan:
    bytes: int = 0
    newest: float = 0.0          # newest mtime of a non-whitelisted file
    newest_dir: float = 0.0      # newest mtime of the root or any dir under it
    links: list[str] = field(default_factory=list)
    error: str | None = None
    first_file: str | None = None


def scan(root: str, *, skip_mtime=lambda rel: False, arrival: bool = False) -> Scan:
    """One lstat walk (never following a link): size, newest mtime, and
    every symlink, reparse point or mount point. Any OSError fails closed.
    With ``arrival``, an entry's arrival time counts as a write too."""
    out = Scan()
    base = _long(root)
    mounts = _mount_points() if sys.platform != "win32" else set()

    def is_mount(path: str) -> bool:
        if sys.platform == "win32":
            return False
        if mounts is None:
            return os.path.ismount(path)
        return os.path.normpath(path) in mounts
    # A POSIX mount (bind or other) is a plain dir to lstat; a different
    # device fails closed. Windows mount points are reparse points already.
    try:
        root_st = os.lstat(base)
    except OSError as exc:
        out.error = f".: {exc.strerror or exc}"
        return out
    root_dev = root_st.st_dev if sys.platform != "win32" else None
    if is_mount(base):
        out.links.append(". (mount point)")
        return out
    out.newest_dir = max(root_st.st_mtime, _arrival(root_st) if arrival else 0.0)
    stack = [("", base)]
    while stack:
        rel_dir, abs_dir = stack.pop()
        try:
            with os.scandir(abs_dir) as it:
                entries = list(it)
        except OSError as exc:
            out.error = f"{rel_dir or '.'}: {exc.strerror or exc}"
            return out
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                out.error = f"{rel}: {exc.strerror or exc}"
                return out
            if is_link(st):
                out.links.append(rel)
                continue
            if stat.S_ISDIR(st.st_mode):
                if (root_dev is not None and st.st_dev != root_dev) or is_mount(entry.path):
                    out.links.append(f"{rel} (mount point)")
                    continue
                out.newest_dir = max(out.newest_dir, st.st_mtime,
                                     _arrival(st) if arrival else 0.0)
                stack.append((rel, entry.path))
                continue
            out.bytes += st.st_size
            if out.first_file is None:
                out.first_file = rel
            if not skip_mtime(rel):
                out.newest = max(out.newest, st.st_mtime, _arrival(st) if arrival else 0.0)
    return out


# --- git -------------------------------------------------------------------

# Variables that point git at another repository, work tree, index or
# object store: inherited from a caller (a hook sets GIT_DIR), they would
# make every check here read the wrong repository.
_GIT_REDIRECTS = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                  "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                  "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
                  "GIT_DISCOVERY_ACROSS_FILESYSTEM"}
# core.fsmonitor and gpg.program (through log.showSignature) name programs
# git runs on status and on reflog or log; never one here.
GIT_SAFE = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
            "-c", "log.showSignature=false"]


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() not in _GIT_REDIRECTS}
    # A partial clone would otherwise fetch from its promisor remote.
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    return env


def git(cwd: str, *args: str, timeout: float = 120, input: str | None = None
        ) -> subprocess.CompletedProcess:
    return subprocess.run([*GIT_SAFE, "-C", cwd, *args], input=input,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=git_env(), timeout=timeout)


@dataclass
class Worktree:
    path: str                    # as git prints it
    head: str | None = None
    branch: str | None = None
    locked: bool = False
    prunable: bool = False


def list_worktrees(repo: str) -> list[Worktree]:
    proc = git(repo, "worktree", "list", "--porcelain")
    if proc.returncode:
        raise Refusal(f"`git worktree list` failed in {repo}: {proc.stderr.strip()}")
    out: list[Worktree] = []
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            out.append(Worktree(path=value))
        elif not out:
            continue
        elif key == "HEAD":
            out[-1].head = value
        elif key == "branch":
            out[-1].branch = value.removeprefix("refs/heads/")
        elif key == "locked":
            out[-1].locked = True
        elif key == "prunable":
            out[-1].prunable = True
    return out


def fetch(repo: str) -> None:
    proc = git(repo, "fetch", "--quiet", "origin", "master", timeout=600)
    if proc.returncode:
        raise Refusal("`git fetch origin master` failed, so merged status cannot be "
                      f"trusted; nothing was changed: {proc.stderr.strip()}")


def admin_dir(wt: str) -> str | None:
    dotgit = os.path.join(wt, ".git")
    try:
        text = Path(dotgit).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("gitdir:"):
        return None
    path = text.split(":", 1)[1].strip()
    return path if os.path.isabs(path) else os.path.join(wt, path)


def keep_marker(wt: str) -> str | None:
    if os.path.lexists(os.path.join(wt, KEEP_FILE)):
        return f"keep marker ({KEEP_FILE})"
    proc = git(wt, "config", "--type=bool", "--get", KEEP_CONFIG)
    if proc.returncode == 0 and proc.stdout.strip() == "true":
        return f"keep marker ({KEEP_CONFIG}=true)"
    if proc.returncode not in (0, 1):
        return f"keep marker unreadable ({proc.stderr.strip()})"
    return None


def status_problem(wt: str) -> str | None:
    # matching: an ignored dir is reported as the path its pattern matched
    # (pkg/__pycache__/), not collapsed to a parent that holds nothing else.
    proc = git(wt, "status", "--porcelain=v1", "-z", "--ignored=matching",
               "--untracked-files=all", "--ignore-submodules=none")
    if proc.returncode:
        return f"git status failed: {proc.stderr.strip()}"
    tokens = proc.stdout.split("\0")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if len(token) < 4:
            continue
        code, rel = token[:2], token[3:]
        if code[0] in "RC" or code[1] in "RC":
            i += 1                       # -z puts the rename source next
        if code != "!!":
            return f"uncommitted changes ({code.strip()} {rel})"
        if not whitelisted(rel):
            return f"ignored path not in cache whitelist: {rel}"
    return None


# --- candidates ------------------------------------------------------------

@dataclass
class Verdict:
    kind: str
    path: str
    failure: str | None = None
    passed: list[str] = field(default_factory=list)
    bytes: int = 0
    newest: float = 0.0
    head: str | None = None
    branch: str | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


def evaluate_worktree(repo: str, wt: Worktree, *, grace_days: float | None,
                      now: float) -> Verdict:
    """Checks in reporting order; the first failure wins. ``grace_days``
    None means owner mode (the owner asserts it is done)."""
    v = Verdict("worktree", wt.path, branch=wt.branch)
    path = wt.path
    if wt.prunable or not os.path.isdir(path):
        v.failure = "missing on disk (git worktree prune clears it)"
        return v
    if wt.locked:
        v.failure = "locked (git worktree unlock to release)"
        return v
    v.passed.append("not-locked")
    marker = keep_marker(path)
    if marker:
        v.failure = marker
        return v
    v.passed.append("no-keep-marker")
    # Read the admin dir's times before any git call could touch them.
    admin = admin_dir(path)
    admin_newest = 0.0
    for name in ("HEAD", "index"):
        try:
            admin_newest = max(admin_newest, os.stat(os.path.join(admin, name)).st_mtime)
        except (OSError, TypeError):
            pass
    head = git(path, "rev-parse", "HEAD")
    if head.returncode:
        v.failure = f"cannot read HEAD: {head.stderr.strip()}"
        return v
    v.head = head.stdout.strip()
    anc = git(repo, "merge-base", "--is-ancestor", v.head, MASTER)
    if anc.returncode == 1:
        v.failure = f"not merged (HEAD {v.head[:10]} is not on origin/master)"
        return v
    if anc.returncode:
        v.failure = f"merge check failed: {anc.stderr.strip()}"
        return v
    v.passed.append("merged")
    # Links before status: git also lists an untracked symlink as a change,
    # and the link is the reason that matters (a followed link loses data).
    s = scan(path, skip_mtime=whitelisted)
    v.bytes = s.bytes
    v.newest = max(s.newest, admin_newest)
    if s.error:
        v.failure = f"walk error (failing closed): {s.error}"
        return v
    if s.links:
        v.failure = f"reparse point or symlink: {s.links[0]}"
        return v
    v.passed.append("no-reparse-points")
    problem = status_problem(path)
    if problem:
        v.failure = problem
        return v
    v.passed.append("clean")
    if grace_days is None:
        v.passed.append("grace-skipped-owner")
    elif now - v.newest < grace_days * DAY:
        v.failure = (f"in grace period (last write {(now - v.newest) / DAY:.1f} days "
                     f"ago, grace {grace_days:g})")
        return v
    else:
        v.passed.append("grace")
    return v


_TARGET_DIR = re.compile(r"""^\s*(?:build\.)?target-dir\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""",
                         re.M)


def cargo_pins(places: list[str], exclude: list[str]) -> dict[str, str]:
    """``target-dir`` pins from ``.cargo/config.toml`` under each place:
    normalized pinned path -> the config file naming it."""
    pins: dict[str, str] = {}
    configs = []
    for place in places:
        # The place itself and one level down: this repo's cargo workspace
        # lives in rust/, so rust/.cargo/config.toml is a real pin site.
        dirs = [place]
        try:
            with os.scandir(place) as it:
                dirs += [e.path for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            pass
        for d in dirs:
            for name in ("config.toml", "config"):
                configs.append(os.path.join(d, ".cargo", name))
    cargo_home = os.environ.get("CARGO_HOME") or os.path.join(Path.home(), ".cargo")
    configs += [os.path.join(cargo_home, "config.toml"), os.path.join(cargo_home, "config")]
    for cfg in configs:
        if any(is_under(norm(cfg), ex) for ex in exclude):
            continue
        try:
            text = Path(cfg).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Not UTF-8 (a PowerShell 5 `>` writes UTF-16): a pin we cannot
            # read could protect any cache, so every cache is kept (a sweep
            # still removes worktrees; an owner run with a cache dir refuses).
            pins[UNREADABLE_PIN] = ", ".join(filter(None, (pins.get(UNREADABLE_PIN), cfg)))
            continue
        except OSError:
            continue
        for m in _TARGET_DIR.finditer(text):
            if m.group(1) is not None:
                try:
                    value = json.loads(f'"{m.group(1)}"')
                except ValueError:
                    value = m.group(1)
            else:
                value = m.group(2)
            if not os.path.isabs(value):
                value = os.path.join(os.path.dirname(os.path.dirname(cfg)), value)
            pins[norm(value)] = cfg
    return pins


def list_processes() -> list[tuple[int, int, str]] | None:
    """(pid, ppid, command line) for every process, or None when this host
    cannot list command lines."""
    try:
        if sys.platform == "win32":
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if not shell:
                return None
            script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                      "Get-CimInstance Win32_Process | ForEach-Object { "
                      "\"{0}`t{1}`t{2} {3}\" -f $_.ProcessId, $_.ParentProcessId, "
                      "($_.CommandLine -replace '[\\r\\n]', ' '), $_.ExecutablePath }")
            proc = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=120)
            if proc.returncode:
                return None
            out = []
            for line in proc.stdout.splitlines():
                parts = line.split("\t", 2)
                if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                    out.append((int(parts[0]), int(parts[1]), parts[2]))
            return out or None
        if os.path.isdir("/proc/self"):
            if _proc_hides_others():
                return None             # other users' processes are invisible
            out = []
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    raw = Path(f"/proc/{name}/cmdline").read_bytes()
                    statline = Path(f"/proc/{name}/stat").read_text()
                except OSError:
                    continue
                ppid = int(statline.rsplit(")", 1)[1].split()[1])
                cmd = raw.replace(b"\0", b" ").decode(errors="replace")
                try:
                    cmd += " " + os.readlink(f"/proc/{name}/exe")    # the executable too
                except OSError:
                    pass
                out.append((int(name), ppid, cmd))
            return out
        proc = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode:
            return None
        out = []
        for line in proc.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                out.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
        return out
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _proc_hides_others() -> bool:
    """True when /proc is mounted with hidepid, so a listing would show only
    this user's processes and look like nobody else uses anything."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) > 4 and fields[4] == "/proc":
                    opts = line.rsplit(" - ", 1)[-1]
                    if re.search(r"hidepid=(1|2|invisible|noaccess)", opts):
                        return True
    except OSError:
        pass
    return False


def _fold(text: str) -> str:
    text = text.replace("\\", "/")
    return text.lower() if sys.platform == "win32" else text


def process_naming(path: str, procs: list[tuple[int, int, str]]) -> int | None:
    """PID of a process (not this one or its ancestors) whose command line
    names ``path`` or anything inside it."""
    parent = {pid: ppid for pid, ppid, _ in procs}
    mine, pid = set(), os.getpid()
    while pid and pid not in mine and len(mine) < 64:
        mine.add(pid)
        pid = parent.get(pid, 0)
    needles = {_fold(os.path.abspath(path)).rstrip("/"), _fold(os.path.realpath(path)).rstrip("/")}
    patterns = [re.compile(re.escape(n) + r"(?![\w.-])") for n in needles]
    for pid, _, cmd in procs:
        if pid in mine:
            continue
        folded = _fold(cmd)
        if any(p.search(folded) for p in patterns):
            return pid
    return None


def evaluate_cache(path: str, *, grace_days: float | None, now: float,
                   pins: dict[str, str]) -> Verdict:
    v = Verdict("cache", path)
    npath = norm(path)
    try:
        st = os.lstat(path)
    except OSError as exc:
        v.failure = f"unreadable: {exc.strerror or exc}"
        return v
    if is_link(st):
        v.failure = "reparse point or symlink: the cache dir itself"
        return v
    if UNREADABLE_PIN in pins:
        v.failure = (f"pin config unreadable: not UTF-8, re-save as UTF-8: "
                     f"{pins[UNREADABLE_PIN]} (owner mode: rerun with no --cache-dir and "
                     "CARGO_TARGET_DIR unset to remove only the worktree)")
        return v
    for pin, cfg in pins.items():
        if is_under(pin, npath) or is_under(npath, pin):
            v.failure = f"pinned as target-dir by {cfg}"
            return v
    v.passed.append("not-pinned")
    s = scan(path)
    v.bytes = s.bytes
    v.newest = s.newest
    if s.error:
        v.failure = f"walk error (failing closed): {s.error}"
        return v
    if s.links:
        v.failure = f"reparse point or symlink: {s.links[0]}"
        return v
    v.passed.append("no-reparse-points")
    if grace_days is None:
        procs = list_processes()
        if procs is None:
            v.note = "process check unavailable; skipped"
            v.passed.append("process-check-unavailable")
        else:
            pid = process_naming(path, procs)
            if pid is not None:
                v.failure = f"in use by process {pid}"
                return v
            v.passed.append("no-process-names-it")
    elif now - v.newest < grace_days * DAY:
        v.failure = (f"in grace period (last write {(now - v.newest) / DAY:.1f} days "
                     f"ago, grace {grace_days:g})")
        return v
    else:
        v.passed.append("grace")
    return v


def _rmtree(path: str) -> None:
    """``shutil.rmtree`` that clears the read-only bit Windows puts on git's
    object files, then retries that one entry. The tree was scanned first
    and holds no link, and this runs only inside the renamed-aside copy."""
    def retry(func, entry, exc):
        # Only Windows marks files read-only in a way that blocks deletion,
        # and never through a link (one made after the scan).
        if sys.platform != "win32" or is_link(os.lstat(entry)):
            raise exc if isinstance(exc, BaseException) else exc[1]
        os.chmod(entry, stat.S_IWRITE | stat.S_IREAD)
        func(entry)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def delete_cache(path: str) -> str | None:
    """Rename aside (fails atomically while cargo holds a file open on
    Windows), then delete. Returns an error text, or None."""
    parent, name = os.path.split(os.path.abspath(path))
    trash = os.path.join(parent, f".{name}.retention-{os.getpid()}-{int(time.time())}")
    try:
        os.rename(path, trash)
    except OSError as exc:
        return f"in use or access denied, skipped: {exc.strerror or exc}"
    try:
        _rmtree(_long(trash))
    except OSError as exc:
        return f"deleted in part; the rest stays at {trash}: {exc.strerror or exc}"
    return None


# --- cargo targets, standalone clones, scratch dirs -------------------------
#
# Three more classes, each decided by an exact signal, never by a name: a
# name may only ever keep something.
#
# * A **target** is a cargo target dir: cargo's CACHEDIR.TAG plus its own
#   .rustc_info.json (a uv virtual environment carries the same tag, so the
#   tag alone proves nothing). Its owners are the checkouts its dep-info
#   files (``<profile>/*.d``, ``<profile>/deps/*.d``) name as sources. It is
#   removed when every owner is gone or merged and nothing in it was written
#   for the target grace period; with no owner found, only after it has been
#   idle for the unowned period. A build holding cargo's lock keeps it.
#   Entries cargo's layout does not explain stay, listed.
# * A **clone** is a standalone git checkout (its own .git dir, not a
#   worktree). It is removed when HEAD and every local branch are in the
#   main repository and on origin/master, it has no stash, is clean, holds
#   no link and has been idle for the worktree grace period.
# * A **scratch** dir is a direct child of a configured scratch root (the
#   system temp dir, or a dev machine's work dir). It is removed when it has
#   been idle for the scratch grace period and holds no checkout, virtual
#   environment, log or evidence and no link.

CARGO_TAG_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"
# Inside a verified target, the names the Cargo book's build-cache layout
# gives (target/tmp is CARGO_TARGET_TMPDIR); a profile dir proves itself by
# its own .fingerprint or .cargo-lock.
CARGO_NAMED_DIRS = {"tmp", "doc", "package"}
CARGO_FILES = {"CACHEDIR.TAG", ".rustc_info.json"}
HOUR = 3600.0
DEFAULT_TARGET_GRACE_HOURS = 24.0
DEFAULT_UNOWNED_TARGET_DAYS = 14.0
DEFAULT_SCRATCH_GRACE_DAYS = 14.0   # maintainer ruling, 16 November 2026
# Harness scratchpads hold registered worktrees and evidence; tool dirs
# hold the programs a dev machine runs. A name may only ever keep: the
# Windows dev VM's dry run (16 November 2026) would otherwise have offered
# its .rustup toolchain and its bin dir as scratch.
SCRATCH_KEEP_NAMES = {"claude", "bin", "scripts", "tools"}
# Harness dirs and their lock dirs start with these (claude-wsl-lease,
# codex-artifact-session-*); a name may only keep.
SCRATCH_KEEP_PREFIXES = ("claude", "codex", "t3")
# A scratch root is one the user named on purpose by putting this file in
# it; without it a run refuses that root (a misnamed root such as the temp
# dir's parent would otherwise put a WSL disk image in reach).
SCRATCH_ROOT_MARKER = ".pseudolife-scratch-root"
SCRATCH_LOG_SUFFIXES = (".log", ".jsonl")
# Results, databases, models and programs: never scratch, whatever the age.
SCRATCH_KEEP_SUFFIXES = (".json", ".csv", ".tsv", ".parquet", ".db", ".sqlite", ".sqlite3",
                         ".gguf", ".safetensors", ".onnx", ".pt", ".pth", ".ckpt",
                         ".exe", ".dll", ".so", ".dylib",
                         # disk images, archives and backups, bundles, patches, keys
                         ".vhdx", ".vhd", ".img", ".iso", ".qcow2", ".vmdk",
                         ".tar", ".tgz", ".gz", ".zip", ".7z", ".bak", ".dump", ".sql",
                         ".bundle", ".patch", ".diff", ".pem", ".key")
SCRATCH_KEEP_FILES = {"pg_version"}             # a PostgreSQL data dir
DEP_INFO_MAX_BYTES = 4 * 1024 * 1024


def is_cargo_target(path: str) -> bool:
    try:
        st = os.lstat(path)
        if is_link(st) or not stat.S_ISDIR(st.st_mode):
            return False
        info = os.lstat(os.path.join(path, ".rustc_info.json"))
        if is_link(info) or not stat.S_ISREG(info.st_mode):
            return False
        with open(os.path.join(path, "CACHEDIR.TAG"), encoding="utf-8",
                  errors="replace") as fh:
            head = fh.read(200)
    except OSError:
        return False
    return head.startswith(CARGO_TAG_SIGNATURE)


def _is_profile_dir(path: str) -> bool:
    return (os.path.isdir(os.path.join(path, ".fingerprint"))
            or os.path.isfile(os.path.join(path, ".cargo-lock")))


def _real_dir(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not is_link(st)


def _profile_built_here(prof: str) -> bool:
    """True when a dep-info file of this profile dir names an output inside
    it: cargo writes absolute output paths, so a copied profile (a kept
    candidate build, ``cp -r release release-pr1234``) still names the
    original and is not cargo's own."""
    forms = _both_forms(prof)
    for d in (prof, os.path.join(prof, "deps")):
        try:
            with os.scandir(d) as it:
                files = [e.path for e in it if e.name.endswith(".d")
                         and e.is_file(follow_symlinks=False)][:20]
        except OSError:
            continue
        for f in files:
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    first = fh.readline(65536)
            except OSError:
                continue
            tokens = _dep_tokens(first)
            if tokens and os.path.isabs(tokens[0]):
                out = os.path.normcase(os.path.normpath(tokens[0]))
                if any(is_under(out, form) for form in forms):
                    return True
    return False


def target_layout(path: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """(entries cargo's layout explains, its profile dirs, foreign entries,
    every dir that carries a build lock) at the top of a verified target.
    A profile dir counts only when its own dep-info names outputs inside
    it; a target-triple dir counts when every entry in it is such a
    profile. Anything else is foreign and stays."""
    cargo, profiles, foreign, lock_dirs = [], [], [], []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if _real_dir(full):
            if _is_profile_dir(full):
                lock_dirs.append(full)
                if _profile_built_here(full):
                    cargo.append(name)
                    profiles.append(full)
                else:
                    foreign.append(name)
                continue
            if name in CARGO_NAMED_DIRS:
                cargo.append(name)
                continue
            inner = [os.path.join(full, n) for n in os.listdir(full)]
            if inner and all(_real_dir(p) and _is_profile_dir(p) for p in inner):
                lock_dirs.extend(inner)
                if all(_profile_built_here(p) for p in inner):
                    cargo.append(name)
                    profiles.extend(inner)
                    continue
        elif name in CARGO_FILES and os.path.isfile(full) and not os.path.islink(full):
            cargo.append(name)
            continue
        foreign.append(name)
    return cargo, profiles, foreign, lock_dirs


_ENV_DEP_MANIFEST = "# env-dep:CARGO_MANIFEST_DIR="


def _dep_tokens(text: str) -> list[str]:
    """Paths in a Makefile-style dep-info file (``\\ `` escapes a space).

    rustc names workspace sources relative to the workspace root, so those
    say nothing; a crate that reads CARGO_MANIFEST_DIR also gets a
    ``# env-dep:CARGO_MANIFEST_DIR=<dir>`` line (backslashes doubled), the
    crate's absolute dir, which names its checkout exactly."""
    out = []
    for line in text.splitlines():
        if line.startswith(_ENV_DEP_MANIFEST):
            value = line[len(_ENV_DEP_MANIFEST):].strip().replace("\\\\", "\\")
            if value:
                out.append(os.path.join(value, "Cargo.toml"))
            continue
        if line.startswith("#"):
            continue
        for token in re.findall(r"(?:\\ |\S)+", line):
            token = token.replace("\\ ", " ")
            if token.endswith(":"):
                token = token[:-1]
            if token:
                out.append(token)
    return out


def _both_forms(path: str) -> list[str]:
    """A path as written and as resolved: dep-info paths are not resolved,
    so a junctioned or symlinked home must match either way."""
    return sorted({os.path.normcase(os.path.abspath(path)), norm(path)})


def _tool_homes() -> list[str]:
    home = Path.home()
    homes = [os.environ.get("CARGO_HOME") or str(home / ".cargo"),
             os.environ.get("RUSTUP_HOME") or str(home / ".rustup")]
    return [form for h in homes for form in _both_forms(h)]


def source_owner(src: str, target: str, tool_homes: list[str]) -> tuple[str, str] | None:
    """What a dep-info source path says about the checkout that built it:
    ("checkout", root) when an existing ancestor holds a .git, ("gone",
    the first missing dir) when the path no longer exists and no existing
    ancestor is a checkout, or None when it says nothing (a relative path,
    another machine's drive, the target itself, or a cargo or rustup home)."""
    if not os.path.isabs(src):
        return None
    drive = os.path.splitdrive(src)[0]
    # A WSL path read on Windows (/home/..., /mnt/c/...) has no drive; it
    # would resolve against this drive's root and look gone.
    if sys.platform == "win32" and not drive:
        return None
    if not os.path.isdir((drive + os.sep) if drive else os.sep):
        return None
    path = os.path.normpath(src)
    npath = os.path.normcase(path)
    if any(is_under(npath, t) for t in _both_forms(target)) or any(
            is_under(npath, h) for h in tool_homes):
        return None
    missing, cur = None, path
    while True:
        try:
            os.lstat(cur)
            break
        except (FileNotFoundError, NotADirectoryError):
            pass                        # truly absent; any other error is raised
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        missing, cur = cur, parent
    if missing is not None and sys.platform != "win32" and (
            os.path.ismount(cur) or (_real_dir(cur) and not os.listdir(cur))):
        # Gone below a mount point, or below an empty dir that may be an
        # unmounted one: the drive may simply be absent, so it says nothing.
        return None
    probe = cur if _real_dir(cur) else os.path.dirname(cur)
    while True:
        if os.path.lexists(os.path.join(probe, ".git")):
            return "checkout", probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return ("gone", missing) if missing is not None else None


def target_owners(target: str, profiles: list[str]) -> tuple[dict[str, str], str | None]:
    """(owner path -> "checkout" or "gone", why discovery is incomplete),
    read from every dep-info file of each profile dir and its deps/. Every
    owner must be finished, so a file that cannot be read whole makes the
    result incomplete and the target is kept."""
    homes = _tool_homes()
    owners: dict[str, str] = {}
    incomplete = None
    for prof in profiles:
        for d in (prof, os.path.join(prof, "deps")):
            try:
                with os.scandir(d) as it:
                    files = [e for e in it if e.name.endswith(".d")
                             and e.is_file(follow_symlinks=False)]
            except OSError as exc:
                if os.path.isdir(d):
                    incomplete = f"cannot list {d}: {exc.strerror or exc}"
                continue
            for e in files:
                try:
                    with open(e.path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read(DEP_INFO_MAX_BYTES + 1)
                except OSError as exc:
                    incomplete = f"cannot read {e.path}: {exc.strerror or exc}"
                    continue
                if len(text) > DEP_INFO_MAX_BYTES:
                    incomplete = f"{e.path} is too large to read whole"
                    continue
                for token in _dep_tokens(text):
                    try:
                        found = source_owner(token, target, homes)
                    except OSError as exc:
                        # A dir that cannot be read may hold a live owner.
                        incomplete = f"cannot look at {token}: {exc.strerror or exc}"
                        continue
                    if found:
                        kind, where = found
                        owners.setdefault(where, kind)
    return owners, incomplete


def _lock_held(lock_path: str) -> bool | None:
    """True when another process holds cargo's build lock on ``lock_path``,
    False when this process could take it (it is released at once), None
    when there is no lock file. Any other failure to open it (another
    user's build, a read-only file, a sharing violation) counts as held."""
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError as exc:
        return None if exc.errno == errno.ENOENT else True
    try:
        if sys.platform == "win32":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def build_lock_holder(profiles: list[str]) -> str | None:
    for prof in profiles:
        if _lock_held(os.path.join(prof, ".cargo-lock")):
            return prof
    return None


def merged_in_main(repo: str, sha: str) -> str | None:
    """None when ``sha`` is a commit of the main repository on
    origin/master; otherwise the reason it is not."""
    if git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
        return f"commit {sha[:10]} exists only in that checkout"
    anc = git(repo, "merge-base", "--is-ancestor", sha, MASTER)
    if anc.returncode == 1:
        return f"commit {sha[:10]} is not on origin/master"
    if anc.returncode:
        return f"merge check failed: {anc.stderr.strip()}"
    return None


def checkout_unfinished(repo: str, root: str, main: str) -> str | None:
    """None when the checkout at ``root`` (a worktree or a clone) holds no
    commit that is not on origin/master; otherwise why it may still be in
    use."""
    if norm(root) == norm(main):
        return "the main checkout"
    head = git(root, "rev-parse", "HEAD")
    if head.returncode:
        return f"cannot read HEAD: {head.stderr.strip()}"
    reason = merged_in_main(repo, head.stdout.strip())
    if reason:
        return reason
    if os.path.isdir(os.path.join(root, ".git")):
        # A clone: every ref (tags, remote-tracking refs, notes; the stash is
        # checked on its own) and every reflog entry, since a commit left on
        # a detached HEAD lives only in the reflog.
        refs = git(root, "for-each-ref", "--format=%(objectname) %(refname)")
        logs = git(root, "reflog", "show", "--all", "--format=%H")
        # Every commit object too: one no ref and no reflog holds (logging
        # off, or an expired reflog) is still unique work.
        objects = git(root, "cat-file", "--batch-all-objects",
                      "--batch-check=%(objecttype) %(objectname)", timeout=600)
        if refs.returncode or logs.returncode or objects.returncode:
            return ("cannot list its refs and commits: "
                    f"{(refs.stderr or logs.stderr or objects.stderr).strip()}")
        shas = {line.split()[0] for line in refs.stdout.splitlines()
                if len(line.split()) == 2 and not line.split()[1].startswith("refs/stash")}
        shas |= set(logs.stdout.split())
        shas |= {line.split()[1] for line in objects.stdout.splitlines()
                 if line.startswith("commit ")}
        reason = all_on_master(repo, sorted(shas))
        if reason:
            return f"a ref or reflog entry: {reason}"
    return None


def all_on_master(repo: str, shas: list[str]) -> str | None:
    """None when every commit in ``shas`` is in the main repository and on
    origin/master (two git calls, however many); otherwise the reason."""
    if not shas:
        return None
    check = git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                input="".join(f"{sha}^{{commit}}\n" for sha in shas), timeout=600)
    if check.returncode:
        return f"cannot check commits: {check.stderr.strip()}"
    for sha, line in zip(shas, check.stdout.splitlines()):
        if not line.endswith(" commit"):
            return f"commit {sha[:10]} exists only in that checkout"
    extra = git(repo, "rev-list", "--max-count=1", "--stdin",
                input="".join(f"{sha}\n" for sha in shas) + f"^{MASTER}\n", timeout=600)
    if extra.returncode:
        return f"merge check failed: {extra.stderr.strip()}"
    if extra.stdout.strip():
        return f"commit {extra.stdout.strip()[:10]} is not on origin/master"
    return None


def cwd_inside(path: str) -> int | None:
    """PID of a process (not this one) whose working dir is ``path`` or
    inside it, where /proc shows it (Linux); a shell left in a dir names it
    on no command line. Other users' processes cannot be read and are not
    seen."""
    if not os.path.isdir("/proc/self"):
        return None
    root = os.path.realpath(path).rstrip("/")
    me = os.getpid()
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            cwd = os.readlink(f"/proc/{name}/cwd")
        except OSError:
            continue
        if cwd == root or cwd.startswith(root + "/"):
            return int(name)
    return None


def process_in(path: str, procs) -> int | None:
    pid = process_naming(path, procs)
    return pid if pid is not None else cwd_inside(path)


def evaluate_target(path: str, *, repo: str, main: str, grace_hours: float,
                    unowned_days: float, now: float, pins: dict[str, str],
                    procs=None) -> Verdict:
    v = Verdict("target", path)
    try:
        st = os.lstat(path)
    except OSError as exc:
        v.failure = f"unreadable: {exc.strerror or exc}"
        return v
    if is_link(st):
        v.failure = "reparse point or symlink: the target dir itself"
        return v
    if not is_cargo_target(path):
        v.failure = "not a cargo target (no cargo CACHEDIR.TAG with .rustc_info.json)"
        return v
    v.passed.append("cargo-target")
    if UNREADABLE_PIN in pins:
        v.failure = f"pin config unreadable: not UTF-8, re-save as UTF-8: {pins[UNREADABLE_PIN]}"
        return v
    npath = norm(path)
    for pin, cfg in pins.items():
        if is_under(pin, npath) or is_under(npath, pin):
            v.failure = f"pinned as target-dir by {cfg}"
            return v
    v.passed.append("not-pinned")
    s = scan(path)
    # A dir created or emptied counts as a write, so a tree of empty dirs
    # is never idle for ever.
    v.bytes, v.newest = s.bytes, max(s.newest, s.newest_dir)
    if s.error:
        v.failure = f"walk error (failing closed): {s.error}"
        return v
    if s.links:
        v.failure = f"reparse point or symlink: {s.links[0]}"
        return v
    v.passed.append("no-reparse-points")
    try:
        cargo, profiles, foreign, lock_dirs = target_layout(path)
    except OSError as exc:
        v.failure = f"walk error (failing closed): {exc.strerror or exc}"
        return v
    if not [n for n in cargo if n not in CARGO_FILES]:
        v.failure = "nothing of cargo's own to delete (every profile here was built elsewhere); kept"
        return v
    if not any(os.path.isfile(os.path.join(p, ".cargo-lock")) for p in profiles):
        # Cargo keeps one in every profile it builds; none found means the
        # probe below cannot speak for a build.
        v.failure = "no cargo build lock found in any profile; kept"
        return v
    holder = build_lock_holder(lock_dirs)
    if holder:
        v.failure = f"a build holds cargo's lock ({os.path.basename(holder)})"
        return v
    v.passed.append("no-build-lock")
    if procs is None:
        v.failure = "process check unavailable; kept"
        return v
    pid = process_in(path, procs)
    if pid is not None:
        v.failure = f"in use by process {pid}"
        return v
    v.passed.append("no-process-uses-it")
    idle = now - v.newest
    owners, incomplete = target_owners(path, lock_dirs)
    if incomplete:
        v.failure = f"its owners cannot all be read ({incomplete}); kept"
        return v
    # A finished owner's own .cargo/config.toml may still pin this target.
    owner_pins = cargo_pins(sorted(o for o, k in owners.items() if k == "checkout"),
                            exclude=[])
    for pin, cfg in owner_pins.items():
        if pin == UNREADABLE_PIN or is_under(pin, npath) or is_under(npath, pin):
            v.failure = f"pinned as target-dir by {cfg}"
            return v
    if owners:
        for owner, kind in sorted(owners.items()):
            if kind == "checkout":
                reason = checkout_unfinished(repo, owner, main)
                if reason:
                    v.failure = f"owner in use: {owner} ({reason})"
                    return v
        v.note = "owners finished: " + ", ".join(
            f"{o} ({'gone' if k == 'gone' else 'merged'})" for o, k in sorted(owners.items()))
        v.passed.append("owners-finished")
        if idle < grace_hours * HOUR:
            v.failure = (f"in grace period (last write {idle / HOUR:.1f} hours ago, "
                         f"grace {grace_hours:g})")
            return v
    else:
        v.note = "no owner found in its dep-info"
        if idle < unowned_days * DAY:
            v.failure = (f"no owner found; idle {idle / DAY:.1f} days, "
                         f"removed after {unowned_days:g}")
            return v
        v.passed.append("unowned-idle")
    v.passed.append("grace")
    if foreign:
        # Only what is deleted counts: the size and the ledger leave out
        # what stays.
        try:
            stays = foreign + [n for n in cargo if n in CARGO_FILES]   # markers stay too
            kept = sum(scan(os.path.join(path, n)).bytes if _real_dir(os.path.join(path, n))
                       else os.lstat(os.path.join(path, n)).st_size for n in stays)
        except OSError as exc:
            v.failure = f"walk error (failing closed): {exc.strerror or exc}"
            return v
        v.bytes = max(v.bytes - kept, 0)
        v.note = (v.note or "") + f"; kept, not cargo's ({human(kept)}): " + ", ".join(foreign)
    return v


def delete_target(path: str) -> str | None:
    """Delete the entries cargo's layout explains; anything else stays.
    Whole when nothing else is there (renamed aside first)."""
    try:
        cargo, _profiles, foreign, _locks = target_layout(path)
    except OSError as exc:
        return f"in use or access denied, skipped: {exc.strerror or exc}"
    if not foreign:
        return delete_cache(path)
    # With foreign entries, cargo's markers stay: the dir remains a target
    # (with nothing of cargo's own left, a later sweep keeps it and names
    # the rest), and a delete that stopped part-way is finished later.
    dirs = [n for n in cargo if n not in CARGO_FILES]
    errors, deleted = [], 0
    for name in dirs:
        err = delete_cache(os.path.join(path, name))
        if err:
            errors.append(f"{name}: {err}")
        else:
            deleted += 1
    if errors:
        if deleted == 0 and all(": in use" in e for e in errors):
            return "in use or access denied, skipped: " + "; ".join(errors)
        return "deleted in part (cargo's markers kept, so a later sweep finishes it): " + "; ".join(errors)
    return None


class _BuildLocks:
    """Cargo's build locks of a target, held across its rename and delete
    on POSIX, where a rename succeeds under a running build; a build that
    starts meanwhile waits on the lock. On Windows the rename itself fails
    while any file in the tree is open, and an open lock file would make
    it fail always, so nothing is held there."""

    def __init__(self, lock_dirs: list[str]) -> None:
        self.paths = [os.path.join(d, ".cargo-lock") for d in lock_dirs]
        self.fds: list[int] = []

    def __enter__(self) -> bool:
        if sys.platform == "win32":
            return True
        import fcntl
        for path in self.paths:
            try:
                fd = os.open(path, os.O_RDWR)
            except FileNotFoundError:
                continue
            except OSError:
                return False
            self.fds.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
        return True

    def __exit__(self, *exc) -> None:
        for fd in self.fds:
            os.close(fd)


def _clone_newest(path: str, s: Scan) -> float:
    newest = s.newest
    for name in ("HEAD", "index"):
        try:
            newest = max(newest, os.stat(os.path.join(path, ".git", name)).st_mtime)
        except OSError:
            pass
    return newest


# Clone config keys under which git runs a program or reads another file;
# a clone carrying any of them is kept, never inspected further.
_RUNS_PROGRAMS = re.compile(
    r"^(core\.fsmonitor|core\.hookspath|core\.sshcommand|core\.pager|core\.editor"
    r"|filter\..*|diff\..*\.(textconv|command)|merge\..*\.driver|include\.path"
    r"|includeif\..*|credential\..*|core\.worktree|extensions\.worktreeconfig"
    r"|gpg\..*|log\.showsignature|diff\.external|core\.alternaterefscommand)$")


def clone_config_problem(path: str) -> str | None:
    """Read (never run git in) the clone's own config files. A key under
    which git runs a program, reads another file or looks at another dir
    (core.worktree) keeps the clone; so does a worktree-scoped config file,
    which git would also load."""
    gitdir = os.path.join(path, ".git")
    if os.path.lexists(os.path.join(gitdir, "commondir")):
        return "its .git names another dir's config and objects (commondir); kept"
    if os.path.lexists(os.path.join(gitdir, "config.worktree")):
        return "it has a worktree-scoped git config (config.worktree); kept"
    cfg = os.path.join(gitdir, "config")
    proc = subprocess.run([*GIT_SAFE, "config", "--file", cfg, "--null", "--list"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=git_env(), timeout=60)
    if proc.returncode:
        return f"its git config cannot be read: {proc.stderr.strip()}"
    for entry in proc.stdout.split("\0"):
        key, _, value = entry.partition("\n")
        key = key.strip().lower()
        if not key:
            continue
        if _RUNS_PROGRAMS.match(key) or (key == "core.bare" and value.strip().lower()
                                          in ("true", "yes", "on", "1")):
            return f"its git config sets {key}, which can run a program or move git elsewhere; kept"
    return None


def evaluate_clone(path: str, *, repo: str, main: str, grace_days: float,
                   now: float, procs) -> Verdict:
    v = Verdict("clone", path)
    if not _real_dir(path) or not _real_dir(os.path.join(path, ".git")):
        v.failure = "not a standalone git clone"
        return v
    if norm(path) == norm(main):
        v.failure = "the main checkout"
        return v
    problem = clone_config_problem(path)
    if problem:
        v.failure = problem
        return v
    v.passed.append("config-runs-nothing")
    top = git(path, "rev-parse", "--show-toplevel")
    if top.returncode or norm(top.stdout.strip()) != norm(path):
        v.failure = f"git does not resolve its work tree to this dir ({top.stdout.strip() or top.stderr.strip()})"
        return v
    if os.path.lexists(os.path.join(path, ".git", "modules")):
        v.failure = "it holds submodules, whose work git status may not show"
        return v
    flagged = git(path, "ls-files", "-v")
    if flagged.returncode:
        v.failure = f"git ls-files failed: {flagged.stderr.strip()}"
        return v
    hidden = [line[2:] for line in flagged.stdout.splitlines()
              if line[:1].islower() or line[:1] == "S"]
    if hidden:
        v.failure = f"a file is marked assume-unchanged or skip-worktree ({hidden[0]}), so status may hide its changes"
        return v
    linked = git(path, "worktree", "list", "--porcelain")
    if linked.returncode:
        v.failure = f"cannot list its worktrees: {linked.stderr.strip()}"
        return v
    if sum(line.startswith("worktree ") for line in linked.stdout.splitlines()) > 1:
        v.failure = "it owns linked worktrees, whose work lives in its .git"
        return v
    v.passed.append("no-linked-worktrees")
    marker = keep_marker(path)
    if marker:
        v.failure = marker
        return v
    v.passed.append("no-keep-marker")
    head = git(path, "rev-parse", "HEAD")
    if head.returncode:
        v.failure = f"cannot read HEAD: {head.stderr.strip()}"
        return v
    v.head = head.stdout.strip()
    branch = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    v.branch = branch.stdout.strip() or None
    reason = checkout_unfinished(repo, path, main)
    if reason:
        v.failure = f"not merged ({reason})"
        return v
    v.passed.append("merged")
    stash = git(path, "stash", "list")
    if stash.returncode or stash.stdout.strip():
        v.failure = "has a stash" if not stash.returncode else f"git stash failed: {stash.stderr.strip()}"
        return v
    v.passed.append("no-stash")
    s = scan(path, skip_mtime=lambda rel: whitelisted(rel) or rel.startswith(".git/"))
    v.bytes = s.bytes
    v.newest = max(_clone_newest(path, s), s.newest_dir)
    if s.error:
        v.failure = f"walk error (failing closed): {s.error}"
        return v
    if s.links:
        v.failure = f"reparse point or symlink: {s.links[0]}"
        return v
    v.passed.append("no-reparse-points")
    problem = status_problem(path)
    if problem:
        v.failure = problem
        return v
    v.passed.append("clean")
    if now - v.newest < grace_days * DAY:
        v.failure = (f"in grace period (last write {(now - v.newest) / DAY:.1f} days "
                     f"ago, grace {grace_days:g})")
        return v
    v.passed.append("grace")
    if procs is None:
        v.failure = "process check unavailable; kept"
        return v
    pid = process_in(path, procs)
    if pid is not None:
        v.failure = f"in use by process {pid}"
        return v
    v.passed.append("no-process-uses-it")
    return v


def scratch_reason(path: str) -> str | None:
    """Why a scratch dir must stay, from what it holds (one lstat walk):
    a checkout, a virtual environment, a log or evidence."""
    stack = [_long(path)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError as exc:
            return f"walk error (failing closed): {exc.strerror or exc}"
        names = {e.name.lower() for e in entries}
        if {"head", "objects", "refs"} <= names:
            return "holds a git repository (bare or a git dir)"
        for e in entries:
            low = e.name.lower()
            if low == KEEP_FILE:
                return f"keep marker ({KEEP_FILE})"
            if low == ".git":
                return "holds a git checkout"
            if low == "pyvenv.cfg":
                return "holds a virtual environment"
            if "evidence" in low or low == "logs" or low.endswith("-logs"):
                return f"holds evidence or logs ({e.name})"
            try:
                st = e.stat(follow_symlinks=False)
            except OSError as exc:
                return f"walk error (failing closed): {exc.strerror or exc}"
            if is_link(st):
                continue                      # scan() reports links
            if stat.S_ISDIR(st.st_mode):
                stack.append(e.path)
                continue
            if not stat.S_ISREG(st.st_mode):
                return f"holds a socket, pipe or device ({e.name})"
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                return f"holds another user's file ({e.name})"
            if low.endswith(SCRATCH_LOG_SUFFIXES):
                return f"holds a log ({e.name})"
            if low.endswith(SCRATCH_KEEP_SUFFIXES) or low in SCRATCH_KEEP_FILES:
                return f"holds results, a database, a model or a program ({e.name})"
            if sys.platform != "win32" and st.st_mode & 0o111:
                return f"holds a program ({e.name})"
    return None


def evaluate_scratch(path: str, *, grace_days: float, now: float, procs,
                     registered: list[str]) -> Verdict:
    v = Verdict("scratch", path)
    base = os.path.basename(path)
    name = base.lower()
    if sys.platform == "win32" and base != base.rstrip(" ."):
        # Win32 drops a trailing dot or space, so later calls would reach
        # another dir than the one judged here by name.
        v.failure = f"a name ending in a dot or space ({base!r}); kept"
        return v
    if name.startswith(SCRATCH_KEEP_PREFIXES):
        v.failure = f"a harness's scratchpad or a tool dir ({base})"
        return v
    if name in SCRATCH_KEEP_NAMES:
        v.failure = f"a harness's scratchpad or a tool dir ({os.path.basename(path)})"
        return v
    if name.startswith("."):
        v.failure = f"a hidden settings or tool dir ({os.path.basename(path)})"
        return v
    if (os.path.isdir(os.path.join(path, "toolchains"))
            and os.path.isfile(os.path.join(path, "settings.toml"))) or (
            os.path.isdir(os.path.join(path, "registry"))
            and os.path.isdir(os.path.join(path, "bin"))):
        v.failure = "a rustup or cargo home"
        return v
    if "evidence" in name or name == "logs" or name.endswith("-logs"):
        v.failure = f"holds evidence or logs ({os.path.basename(path)})"
        return v
    if not _real_dir(path):
        v.failure = "not a plain directory"
        return v
    npath = norm(path)
    if any(is_under(r, npath) or is_under(npath, r) for r in registered):
        v.failure = "holds or sits inside a registered worktree"
        return v
    s = scan(path, arrival=True)
    v.bytes, v.newest = s.bytes, max(s.newest, s.newest_dir)
    if s.error:
        v.failure = f"walk error (failing closed): {s.error}"
        return v
    if s.links:
        v.failure = f"reparse point or symlink: {s.links[0]}"
        return v
    v.passed.append("no-reparse-points")
    reason = scratch_reason(path)
    if reason:
        v.failure = reason
        return v
    v.passed.append("nothing-to-keep")
    if now - v.newest < grace_days * DAY:
        v.failure = (f"in grace period (last write {(now - v.newest) / DAY:.1f} days "
                     f"ago, grace {grace_days:g})")
        return v
    v.passed.append("grace")
    if s.bytes == 0:
        # Frees nothing, and may be a lock dir some process made with mkdir.
        v.failure = "holds no bytes (it may be a lock dir); kept"
        return v
    if procs is None:
        v.failure = "process check unavailable; kept"
        return v
    pid = process_in(path, procs)
    if pid is not None:
        v.failure = f"in use by process {pid}"
        return v
    v.passed.append("no-process-uses-it")
    return v


# --- scratch sightings -------------------------------------------------------

class Sightings:
    """When a run first saw each scratch dir, kept beside the ledger. Times
    on disk cannot show every arrival (a move within one Windows volume
    keeps the creation time), so a scratch dir goes only a grace period
    after a run first saw it. A dry run records sightings too, so a week of
    dry runs counts. A file that cannot be read keeps every scratch dir."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seen: dict[str, float] = {}
        self.error: str | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not all(
                    isinstance(k, str) and isinstance(v, (int, float)) and math.isfinite(v)
                    for k, v in data.items()):
                raise ValueError("not a map of paths to times")
            self.seen = {k: float(v) for k, v in data.items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.error = f"{path} cannot be read ({exc}); no scratch dir is removed"

    def first_seen(self, path: str, now: float) -> float:
        return self.seen.setdefault(path, now)

    def save(self, present: set[str]) -> str | None:
        # Only dirs still there are remembered.
        kept = {k: v for k, v in self.seen.items() if k in present}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(kept, indent=0, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            return f"could not write {self.path}: {exc.strerror or exc}"
        return None


# --- one run at a time -------------------------------------------------------

class RunLock:
    """One retention run at a time per state dir: a second run (a scheduled
    sweep beside a hand run, or a WSL leg seeing the same drive) refuses."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise Refusal(f"another retention run holds {self.path}; nothing was changed")
        return self

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            os.close(self.fd)


# --- config, ledger, output ------------------------------------------------

def _strip_comment(line: str) -> str:
    """Cut a ``#`` comment, but not a ``#`` inside a quoted string."""
    quote, escaped = None, False
    for i, ch in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def _parse_toml(text: str) -> dict:
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        pass
    # Python 3.10: the flat subset this file uses (key = string | number | [strings]).
    out: dict = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError(f"line {n}: expected key = value")
        key, value = key.strip(), value.strip()
        if value.startswith("["):
            out[key] = [json.loads(s) if s.startswith('"') else s.strip("'")
                        for s in re.findall(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'', value)]
        elif value.startswith('"'):
            out[key] = json.loads(value)
        elif value.startswith("'"):
            out[key] = value.strip("'")
        else:
            out[key] = float(value)
    return out


ROOT_KEYS = ("worktree_roots", "cache_roots", "target_roots", "clone_roots",
             "scratch_roots")
GRACE_KEYS = ("worktree_grace_days", "cache_grace_days", "target_grace_hours",
              "unowned_target_days", "scratch_grace_days")
KNOWN_KEYS = {"repo", *ROOT_KEYS, *GRACE_KEYS}


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = _parse_toml(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:     # UnicodeDecodeError is a ValueError
        raise Refusal(f"cannot read {path}: {exc}") from exc
    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise Refusal(f"{path}: unknown keys {sorted(unknown)}; known: {sorted(KNOWN_KEYS)}")
    for key in ROOT_KEYS:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise Refusal(f'{path}: {key} must be a list of paths, e.g. {key} = ["<dir>"]')
    if not isinstance(data.get("repo", ""), str):
        raise Refusal(f'{path}: repo must be a path string, e.g. repo = "<checkout>"')
    for key in GRACE_KEYS:
        value = data.get(key, 0)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            unit = "hours" if key.endswith("_hours") else "days"
            raise Refusal(f"{path}: {key} must be a number of {unit}, 0 or more")
    return data


def ledger_write(ledger: Path, v: Verdict, mode: str, extra: dict | None = None) -> bool:
    """Append one line; False (after saying so) if the ledger is unwritable,
    since the removal itself has already happened."""
    try:
        # A file dated past what the OS can format (year 3000+ on Windows)
        # must not crash the run after the removal already happened.
        newest = (time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(v.newest))
                  if v.newest else None)
    except (OSError, OverflowError, ValueError):
        newest = None
    line = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mode": mode, "kind": v.kind,
            "path": v.path, "bytes": v.bytes, "newest_mtime": newest,
            "head": v.head, "branch": v.branch, "checks": v.passed}
    line.update(extra or {})
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError as exc:
        what = "removed" if line.get("complete", True) else f"acted on ({line.get('state')})"
        print(f"warning: {what} {v.path} but could not write the ledger {ledger}: "
              f"{exc.strerror or exc}; record it by hand: {json.dumps(line)}")
        return False
    return True


def human(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e3:.1f} kB"


NO_ROOTS = f"""\
No worktree, cache, target, clone or scratch roots are configured, so there is
nothing to do. Configure them with flags (each repeatable):
  python ops/prune_worktrees.py --repo <checkout> --worktree-root <dir> --cache-root <dir>
      --target-root <dir> --clone-root <dir> --scratch-root <dir>
or in {DEFAULT_CONFIG} (flags win):
  repo = "<checkout>"
  worktree_roots = ["<dir>"]
  cache_roots = ["<dir>"]
  target_roots = ["<dir>"]
  clone_roots = ["<dir>"]
  scratch_roots = ["<dir>"]
Runbook: docs/dev/worktree-retention.md"""


def print_table(rows: list[tuple[Verdict, str]]) -> None:
    if not rows:
        print("no candidates")
        return
    print(f"{'KIND':<9} {'VERDICT':<13} {'SIZE':>10}  {'REASON':<48} PATH")
    for v, verdict in rows:
        reason = v.failure or v.note or "all checks passed"
        print(f"{v.kind:<9} {verdict:<13} {human(v.bytes):>10}  {reason:<48} {v.path}")


# --- modes -----------------------------------------------------------------

def registered_entry(repo: str, path: str) -> Worktree | None:
    n = norm(path)
    return next((w for w in list_worktrees(repo) if norm(w.path) == n), None)


def remove_worktree(main: str, path: str) -> tuple[str, str | None]:
    """Plain ``git worktree remove``. Returns (state, error), state one of
    removed, partial, failed or unknown.

    git can unregister a worktree yet fail to delete it: a process holding
    a folder in it as its cwd leaves empty folders, which is removed; a
    file held open leaves files behind, which no later sweep can see, so
    that is partial. When git's list cannot be read afterwards the state
    is unknown, never assumed either way."""
    proc = None
    try:
        # Deleting a large rust/target on Windows can outlast the default
        # timeout, and a kill mid-delete leaves a half-deleted worktree.
        proc = git(main, "worktree", "remove", path, timeout=3600)
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        detail = "timed out after an hour; some files may already be gone"
    try:
        entry = registered_entry(main, path)
    except (Refusal, subprocess.TimeoutExpired) as exc:
        return "unknown", (f"git worktree remove ran ({detail}) but git's worktree list "
                           f"could not be read ({exc}); check `git worktree list` and {path}")
    if entry is not None:
        return "failed", f"git worktree remove failed: {detail}"
    left = leftover_file(path)
    if left:
        return "partial", (f"unregistered, but files remain in {path} (first: {left}; "
                           f"{detail if proc is None or proc.returncode else 'held open'}); "
                           "delete them by hand")
    return "removed", None


def leftover_file(path: str) -> str | None:
    """The first file, link or unreadable entry left under ``path`` (one
    lstat walk, never following a link), or None when only empty folders
    or nothing remain."""
    if not os.path.lexists(path):
        return None
    s = scan(path)
    return s.error or (s.links[0] if s.links else None) or s.first_file


def own_worktree(start: str) -> str | None:
    proc = git(start, "rev-parse", "--show-toplevel")
    return proc.stdout.strip() if proc.returncode == 0 else None


def run_sweep(args, cfg: dict) -> int:
    wt_roots = args.worktree_root or cfg.get("worktree_roots") or []
    cache_roots = args.cache_root or cfg.get("cache_roots") or []
    target_roots = args.target_root or cfg.get("target_roots") or []
    clone_roots = args.clone_root or cfg.get("clone_roots") or []
    scratch_roots = args.scratch_root or cfg.get("scratch_roots") or []
    if not (wt_roots or cache_roots or target_roots or clone_roots or scratch_roots):
        print(NO_ROOTS)
        return 0
    for root in [*wt_roots, *cache_roots, *target_roots, *clone_roots, *scratch_roots]:
        if not root or not os.path.isabs(root):
            raise Refusal(f"root {root!r} is empty or relative; give each root as an "
                          "absolute path; nothing was changed")
    wgrace = args.worktree_grace_days
    if wgrace is None:
        wgrace = float(cfg.get("worktree_grace_days", DEFAULT_WORKTREE_GRACE_DAYS))
    cgrace = args.cache_grace_days
    if cgrace is None:
        cgrace = float(cfg.get("cache_grace_days", DEFAULT_CACHE_GRACE_DAYS))

    def setting(flag, key, default):
        return float(flag if flag is not None else cfg.get(key, default))

    tgrace = setting(args.target_grace_hours, "target_grace_hours", DEFAULT_TARGET_GRACE_HOURS)
    unowned = setting(args.unowned_target_days, "unowned_target_days",
                      DEFAULT_UNOWNED_TARGET_DAYS)
    sgrace = setting(args.scratch_grace_days, "scratch_grace_days", DEFAULT_SCRATCH_GRACE_DAYS)
    repo = args.repo or cfg.get("repo") or own_worktree(str(Path(__file__).resolve().parent))
    if not repo or own_worktree(repo) is None:
        raise Refusal(f"--repo {repo!r} is not a git checkout")
    now = time.time()
    left = {"main": 0, "runs from here": 0, "harness-managed": 0, "outside roots": 0}

    def usable(roots: list[str]) -> list[str]:
        out = []
        for root in roots:
            if harness_managed(root):
                print(f"note: {root} is harness-managed; its contents are left alone")
                continue
            if too_broad(root):
                print(f"note: {root} is a drive root or the home dir (or above it); "
                      "never used as a root")
                continue
            out.append(norm(root))
        return out

    def usable_marked(roots: list[str]) -> list[str]:
        # Only children carrying an exact marker are ever candidates here,
        # so the home dir may be a root (WSL workers leave targets and
        # clones directly in it); a drive root or above the home never is.
        out = []
        home = norm(Path.home())
        for root in roots:
            n = norm(root)
            if harness_managed(root):
                print(f"note: {root} is harness-managed; its contents are left alone")
            elif n != home and too_broad(root):
                print(f"note: {root} is a drive root or above the home dir; never used as a root")
            else:
                out.append(n)
        return out

    def usable_scratch(roots: list[str]) -> list[tuple[str, bool]]:
        """(root, list only) per scratch root. A root must carry the marker
        file its user put there on purpose, the temp dir included; without
        it the whole run refuses and says how to opt in. The temp dir is
        list only: it holds too many things whose names say nothing."""
        out = []
        temps = protected_temp_roots()
        for root in roots:
            n = norm(root)
            if too_broad(root):
                print(f"note: {root} is a drive root or the home dir (or above it); "
                      "never used as a root")
                continue
            if n not in temps and harness_managed(root):
                print(f"note: {root} is harness-managed; its contents are left alone")
                continue
            if not os.path.isfile(os.path.join(n, SCRATCH_ROOT_MARKER)):
                raise Refusal(f"scratch root {root} has no {SCRATCH_ROOT_MARKER} file, so it is "
                              "not known to hold only scratch; if every dir in it may go after "
                              f"its idle period, create {os.path.join(root, SCRATCH_ROOT_MARKER)} "
                              "and run again; nothing was changed")
            out.append((n, n in temps))
        return out

    wt_norm, cache_norm = usable(wt_roots), usable(cache_roots)
    target_norm, clone_norm = usable_marked(target_roots), usable_marked(clone_roots)
    scratch_norm = usable_scratch(scratch_roots)
    worktrees = list_worktrees(repo)
    registered = [norm(w.path) for w in worktrees]
    main = worktrees[0].path
    if wt_roots or target_norm or clone_norm:
        fetch(repo)
    here = {own_worktree(os.getcwd()), own_worktree(str(Path(__file__).resolve().parent))}
    here = {norm(h) for h in here if h}

    verdicts: list[Verdict] = []
    targets: dict[int, Worktree] = {}
    if wt_roots:
        for i, wt in enumerate(worktrees):
            p = norm(wt.path)
            if i == 0:
                left["main"] += 1
            elif p in here:
                left["runs from here"] += 1
            elif harness_managed(wt.path):
                left["harness-managed"] += 1
            elif not any(is_under(p, r) and p != r for r in wt_norm):
                left["outside roots"] += 1
            else:
                v = evaluate_worktree(repo, wt, grace_days=wgrace, now=now)
                v.passed.insert(0, "not-main-or-self")
                targets[len(verdicts)] = wt
                verdicts.append(v)
    else:
        left["outside roots"] = max(len(worktrees) - 1, 0)

    def current_pins() -> dict[str, str]:
        now_registered = [norm(w.path) for w in list_worktrees(repo)]
        places = sorted(set(now_registered + [norm(c) for r in wt_norm if os.path.isdir(r)
                                              for c in _children(r)]
                            + [norm(c) for r in clone_norm for c in _children(r)
                               if _real_dir(os.path.join(c, ".git"))]))
        return cargo_pins(places, exclude=[])

    pins = current_pins() if cache_norm else {}
    for root in cache_norm:
        if not os.path.isdir(root):
            print(f"note: cache root {root} does not exist")
            continue
        if any(is_under(r, root) or is_under(root, r) for r in registered):
            print(f"note: cache root {root} contains or sits inside a git worktree; "
                  "skipped entirely")
            continue
        if harness_managed(root):
            continue
        for child in _children(root):
            if not os.path.isdir(child) and not os.path.islink(child):
                continue
            verdicts.append(evaluate_cache(child, grace_days=cgrace, now=now, pins=pins))

    procs = list_processes() if (target_norm or clone_norm or scratch_norm) else None
    if (target_norm or clone_norm or scratch_norm) and procs is None:
        print("note: the process list cannot be read here, so no target, clone or "
              "scratch dir is removed")
    if target_norm and not pins:
        pins = current_pins()
    # A dir is judged once: one listed under a cache root keeps the cache rule.
    seen: set[str] = {norm(v.path) for v in verdicts if v.kind == "cache"}

    def candidate(child: str) -> bool:
        n = norm(child)
        if any(is_under(h, n) for h in here):
            left["runs from here"] += 1
            return False
        if n in seen or n in registered or any(is_under(n, r) or is_under(r, n)
                                               for r in registered):
            return False
        seen.add(n)
        return True

    for root in target_norm:
        for child in _children(root):
            if is_cargo_target(child) and candidate(child):
                verdicts.append(evaluate_target(
                    child, repo=repo, main=main, grace_hours=tgrace, unowned_days=unowned,
                    now=now, pins=pins, procs=procs))
            elif _real_dir(child) and norm(child) not in seen and not is_cargo_target(child):
                left["not a cargo target"] = left.get("not a cargo target", 0) + 1
    for root in clone_norm:
        for child in _children(root):
            if _real_dir(os.path.join(child, ".git")) and candidate(child):
                verdicts.append(evaluate_clone(child, repo=repo, main=main, grace_days=wgrace,
                                               now=now, procs=procs))
            elif _real_dir(child) and norm(child) not in seen:
                left["not a clone"] = left.get("not a clone", 0) + 1
    list_only: set[str] = set()
    sightings = Sightings(args.ledger.parent / "scratch-sightings.json") if scratch_norm else None
    present: set[str] = set()
    for root, only_list in scratch_norm:
        for child in _children(root):
            if _real_dir(child) and candidate(child):
                v = evaluate_scratch(child, grace_days=sgrace, now=now, procs=procs,
                                     registered=registered)
                key = norm(child)
                present.add(key)
                seen_at = sightings.first_seen(key, now)
                if v.ok and sightings.error:
                    v.failure = sightings.error
                elif v.ok and now - seen_at < sgrace * DAY:
                    v.failure = (f"first seen {(now - seen_at) / DAY:.1f} days ago; a scratch dir "
                                 f"goes only {sgrace:g} days after a run first saw it")
                if only_list:
                    list_only.add(key)
                verdicts.append(v)
    if sightings is not None and not sightings.error:
        note = sightings.save(present)
        if note:
            print(f"warning: {note}")

    rows: list[tuple[Verdict, str]] = []
    ledger_ok = [True]
    pruned_note = None

    def record(v: Verdict, extra: dict) -> None:
        if not ledger_write(args.ledger, v, "sweep", extra):
            ledger_ok[0] = False

    def apply_worktree(wt: Worktree, v: Verdict) -> tuple[Verdict, str]:
        fresh = registered_entry(repo, wt.path)          # catches a lock taken since
        if fresh is None:
            v.failure = "no longer registered"
            return v, "keep"
        again = evaluate_worktree(repo, fresh, grace_days=wgrace, now=time.time())
        again.passed.insert(0, "not-main-or-self")
        if not again.ok:
            again.failure = f"changed since scan: {again.failure}"
            return again, "keep"
        state, err = remove_worktree(registered[0], again.path)
        if state != "failed":
            record(again, {"complete": state == "removed"}
                   | ({} if state == "removed" else {"state": state}))
        again.failure = err
        return again, state

    def apply_marked(v: Verdict) -> tuple[Verdict, str]:
        if v.kind == "target":
            again = evaluate_target(v.path, repo=repo, main=main, grace_hours=tgrace,
                                    unowned_days=unowned, now=time.time(), pins=current_pins(),
                                    procs=list_processes())
        elif v.kind == "clone":
            again = evaluate_clone(v.path, repo=repo, main=main, grace_days=wgrace,
                                   now=time.time(), procs=list_processes())
        else:
            again = evaluate_scratch(v.path, grace_days=sgrace, now=time.time(),
                                     procs=list_processes(),
                                     registered=[norm(w.path) for w in list_worktrees(repo)])
        if not again.ok:
            again.failure = f"changed since scan: {again.failure}"
            return again, "keep"
        if v.kind == "target":
            _c, _p, _f, lock_dirs = target_layout(v.path)
            with _BuildLocks(lock_dirs) as free:
                err = (delete_target(v.path) if free else
                       "in use or access denied, skipped: a build took cargo's lock")
        else:
            err = delete_cache(v.path)
        if err and err.startswith("in use"):
            again.failure = err
            return again, "keep"
        extra = {"note": again.note} if again.note else {}
        record(again, extra | ({"complete": True} if err is None
                               else {"complete": False, "state": "partial"}))
        again.failure = err
        return again, "partial" if err else "removed"

    def apply_cache(v: Verdict) -> tuple[Verdict, str]:
        again = evaluate_cache(v.path, grace_days=cgrace, now=time.time(), pins=current_pins())
        if not again.ok:
            again.failure = f"changed since scan: {again.failure}"
            return again, "keep"
        err = delete_cache(v.path)
        if err and err.startswith("in use"):
            again.failure = err
            return again, "keep"
        record(again, {"complete": True} if err is None else {"complete": False, "state": "partial"})
        again.failure = err
        return again, "partial" if err else "removed"

    for i, v in enumerate(verdicts):
        if not v.ok:
            rows.append((v, "keep"))
        elif v.kind == "scratch" and norm(v.path) in list_only:
            v.note = "the temp dir is list only"
            rows.append((v, "list only"))
        elif not args.apply:
            rows.append((v, "would remove"))
        else:
            try:
                rows.append(apply_worktree(targets[i], v) if v.kind == "worktree"
                            else apply_cache(v) if v.kind == "cache" else apply_marked(v))
            except (Refusal, subprocess.TimeoutExpired, OSError) as exc:
                # One row's git failure must not hide the rows already done.
                v.failure = f"could not finish: {exc}"
                rows.append((v, "failed"))
    rc = 0 if ledger_ok[0] and not any(
        verdict in ("failed", "partial", "unknown") for _, verdict in rows) else 1
    if args.apply and wt_roots:
        try:
            pruned = git(registered[0], "worktree", "prune")
            pruned_note = (None if pruned.returncode == 0 else
                           f"git worktree prune failed: {pruned.stderr.strip()}")
        except subprocess.TimeoutExpired:
            pruned_note = "git worktree prune timed out"
        if pruned_note:
            print(f"warning: {pruned_note}")
            rc = 1

    print("APPLY" if args.apply else "DRY RUN (pass --apply to remove)")
    print_table(rows)
    _totals(rows, left, args.apply, bool(wt_roots) and pruned_note is None)
    return rc


def _children(root: str) -> list[str]:
    try:
        return sorted(os.path.join(root, n) for n in os.listdir(root))
    except OSError:
        return []


def _totals(rows, left, applied: bool, swept_worktrees) -> None:
    done = "removed" if applied else "would remove"
    for kind in ("worktree", "cache", "target", "clone", "scratch"):
        mine = [(v, verdict) for v, verdict in rows if v.kind == kind]
        if kind not in ("worktree", "cache") and not mine:
            continue
        go = [v for v, verdict in mine if verdict in ("removed", "would remove")]
        counts = {state: sum(verdict == state for _, verdict in mine)
                  for state in ("partial", "unknown", "failed")}
        odd = "".join(f"{n} {state} (see rows), " for state, n in counts.items() if n)
        kept = len(mine) - len(go) - sum(counts.values())
        print(f"{kind}s: {len(mine)} candidates, {len(go)} {done}, {odd}"
              f"{kept} kept, {human(sum(v.bytes for v in go))}")
    reasons: dict[str, int] = {}
    for v, verdict in rows:
        if verdict in ("keep", "failed", "partial", "unknown"):
            key = re.sub(r"[:(].*", "", v.failure or "").strip()
            reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print("not removed because: " + ", ".join(f"{k}: {n}" for k, n in
                                           sorted(reasons.items(), key=lambda kv: -kv[1])))
    print("left alone: " + ", ".join(f"{k}: {n}" for k, n in left.items()))
    if applied and swept_worktrees:
        print("ran git worktree prune")
    elif swept_worktrees:
        print("dry run: git worktree prune not run")


def run_owner(args, cfg: dict) -> int:
    start = os.getcwd()
    own = own_worktree(start)
    if own is None:
        raise Refusal(f"{start} is not inside a git worktree")
    worktrees = list_worktrees(own)
    main = worktrees[0].path
    nown = norm(own)
    if norm(main) == nown:
        raise Refusal(f"{own} is the main worktree; --mine removes only a linked worktree")
    entry = next((w for w in worktrees if norm(w.path) == nown), None)
    if entry is None:
        raise Refusal(f"{own} is not a registered worktree of {main}")
    common = git(own, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if args.repo:
        theirs = git(args.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if theirs.returncode or norm(theirs.stdout.strip()) != norm(common.stdout.strip()):
            raise Refusal(f"--repo {args.repo} is not the repository of {own}")
    harness = harness_managed(own)
    fetch(main)
    now = time.time()
    tree = evaluate_worktree(main, entry, grace_days=None, now=now)
    tree.passed.insert(0, "not-main")

    cache_dir = args.cache_dir or os.environ.get("CARGO_TARGET_DIR")
    cache: Verdict | None = None
    cache_roots = [norm(r) for r in (args.cache_root or cfg.get("cache_roots") or [])]
    if cache_dir and not args.cache_dir:
        # CARGO_TARGET_DIR is often one dir shared by every session on a
        # machine; only a slice directly under a configured cache root is
        # known to be this session's own. --cache-dir is the owner's word.
        ncache = norm(cache_dir)
        if not any(os.path.dirname(ncache) == r for r in cache_roots):
            print(f"note: CARGO_TARGET_DIR {cache_dir} is not directly under a configured "
                  "cache root, so it may be shared; left alone (name your own slice "
                  "with --cache-dir)")
            cache_dir = None
    if cache_dir:
        cache_dir = os.path.abspath(cache_dir)
        ncache = norm(cache_dir)
        registered = [norm(w.path) for w in worktrees]
        holders = sorted((r for r in registered if is_under(ncache, r)), key=len)
        holder = holders[-1] if holders else None     # the innermost worktree
        if not os.path.isdir(cache_dir):
            print(f"note: cache dir {cache_dir} does not exist; nothing to clean there")
            cache_dir = None
        elif too_broad(cache_dir):
            raise Refusal(f"{cache_dir} is a drive root or the home dir (or above it); "
                          "pass your own cache slice with --cache-dir")
        elif any(is_under(r, ncache) for r in cache_roots):
            raise Refusal(f"{cache_dir} is or contains a configured cache root, shared "
                          "with other work; pass your own slice with --cache-dir")
        elif any(is_under(r, ncache) for r in registered):
            raise Refusal(f"{cache_dir} contains a git worktree; "
                          "pass your own cache slice with --cache-dir")
        elif holder is not None and holder != nown:
            raise Refusal(f"{cache_dir} is inside another worktree ({holder}); "
                          "a cache dir must be your own")
        elif holder == nown and not whitelisted(
                os.path.relpath(ncache, nown).replace(os.sep, "/")):
            raise Refusal(f"{cache_dir} is inside your worktree but is not a cache path "
                          "(rust/target, __pycache__, ...)")
        else:
            places = [r for r in registered if r != nown]
            pins = cargo_pins(places, exclude=[nown])
            cache = evaluate_cache(cache_dir, grace_days=None, now=now, pins=pins)

    failures = [v for v in (tree, cache) if v is not None and not v.ok]
    if failures:
        print_table([(v, "keep" if not v.ok else "ok") for v in (tree, cache) if v])
        reason = "; ".join(f"{v.kind}: {v.failure}" for v in failures)
        print(f"refused: {reason}. Nothing was changed.")
        return 1
    plan = []
    if harness:
        plan.append(f"checkout {own} belongs to its harness and stays")
    if not args.apply:
        rows = [] if harness else [(tree, "would remove")]
        if cache:
            rows.append((cache, "would remove"))
        print("DRY RUN (pass --apply to remove)")
        print_table(rows)
        if plan:
            print(plan[0])
        return 0

    # Re-check immediately before acting, from a fresh worktree listing.
    entry = registered_entry(main, own)
    if entry is None:
        print(f"refused: {own} is no longer a registered worktree. Nothing was changed.")
        return 1
    tree = evaluate_worktree(main, entry, grace_days=None, now=time.time())
    tree.passed.insert(0, "not-main")
    if cache is not None:
        cache = evaluate_cache(cache.path, grace_days=None, now=time.time(),
                               pins=cargo_pins([norm(w.path) for w in worktrees
                                                if norm(w.path) != nown], exclude=[nown]))
    failures = [v for v in (tree, cache) if v is not None and not v.ok]
    if failures:
        reason = "; ".join(f"{v.kind}: changed since check: {v.failure}" for v in failures)
        print(f"refused: {reason}. Nothing was changed.")
        return 1

    said, rc = [], 0
    if harness:
        said.append(f"checkout {own} belongs to its harness and was left in place")
    else:
        os.chdir(main)               # a process cannot remove the dir it sits in
        state, err = remove_worktree(main, own)
        if state != "failed" and not ledger_write(
                args.ledger, tree, "owner",
                {"complete": state == "removed"} | ({} if state == "removed" else {"state": state})):
            rc = 1
        if state == "partial":
            print(f"partial: {err}. git no longer lists the worktree; the cache was "
                  "not touched.")
            return 1
        if state == "unknown":
            print(f"unknown: {err}. The cache was not touched.")
            return 1
        if state == "failed":
            print(f"failed: {err}. git still lists the worktree; the cache was not "
                  "touched.")
            return 1
        line = f"removed worktree {own}"
        if tree.branch:
            line += f" (branch {tree.branch} kept)"
        if os.path.exists(own):
            line += "; its folder remains while a process holds it as its cwd"
        said.append(line)
    if cache is not None and not os.path.exists(cache.path):
        said.append(f"cache {cache.path} went with the worktree")
    elif cache is not None:
        err = delete_cache(cache.path)
        if err and err.startswith("in use"):
            said.append(f"cache {cache.path} {err}")
            rc = 1
        else:
            if not ledger_write(args.ledger, cache, "owner", {"complete": True} if err is None
                                else {"complete": False, "state": "partial"}):
                rc = 1
            if err:
                rc = 1
            said.append(f"removed cache {cache.path} ({human(cache.bytes)})"
                        + (f"; {err}" if err else "")
                        + (f"; {cache.note}" if cache.note else ""))
    else:
        said.append("no cache dir given (--cache-dir or CARGO_TARGET_DIR)")
    print("; ".join(said))
    return rc


def _period(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of hours or days, 0 or more")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Reclaim finished git worktrees and stale build caches. "
                    "Dry run unless --apply.")
    p.add_argument("--apply", action="store_true", help="remove; default is a dry run")
    p.add_argument("--mine", action="store_true",
                   help="owner mode: the worktree this runs from, plus its cache dir")
    p.add_argument("--cache-dir", help="owner mode: this session's cache dir "
                   "(default $CARGO_TARGET_DIR)")
    p.add_argument("--repo", help="the main checkout (default: config, else this checkout)")
    p.add_argument("--worktree-root", action="append", default=[],
                   help="a dir whose worktrees may be removed (repeatable)")
    p.add_argument("--cache-root", action="append", default=[],
                   help="a dir whose immediate subdirs are caches (repeatable)")
    p.add_argument("--worktree-grace-days", type=_period, default=None,
                   help=f"default {DEFAULT_WORKTREE_GRACE_DAYS:g}")
    p.add_argument("--cache-grace-days", type=_period, default=None,
                   help=f"default {DEFAULT_CACHE_GRACE_DAYS:g}")
    p.add_argument("--target-root", action="append", default=[],
                   help="a dir whose cargo target subdirs are judged by their owners (repeatable)")
    p.add_argument("--clone-root", action="append", default=[],
                   help="a dir whose standalone git clones may be removed (repeatable)")
    p.add_argument("--scratch-root", action="append", default=[],
                   help="a dir whose idle scratch subdirs may be removed (repeatable)")
    p.add_argument("--target-grace-hours", type=_period, default=None,
                   help=f"default {DEFAULT_TARGET_GRACE_HOURS:g}")
    p.add_argument("--unowned-target-days", type=_period, default=None,
                   help=f"default {DEFAULT_UNOWNED_TARGET_DAYS:g}")
    p.add_argument("--scratch-grace-days", type=_period, default=None,
                   help=f"default {DEFAULT_SCRATCH_GRACE_DAYS:g}")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=argparse.SUPPRESS)
    p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
        with RunLock(args.ledger.parent / "locks" / "retention.lock"):
            return run_owner(args, cfg) if args.mine else run_sweep(args, cfg)
    except Refusal as exc:
        print(f"refused: {exc}")
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"refused: {exc.cmd[:4]} timed out; nothing further was changed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
