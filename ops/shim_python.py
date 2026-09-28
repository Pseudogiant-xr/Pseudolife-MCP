#!/usr/bin/env python3
"""Pick, and verify, the interpreter for an extractor shim's autostart unit.

``evals/claude_shim.py`` and ``evals/codex_shim.py`` import the dream system
prompt from ``pseudolife_memory``, which imports the package's dependencies
(torch among them). The shims put the checkout on ``sys.path`` themselves, so
any interpreter imports the *package*; what the unit's interpreter needs is
the dependencies. A bare ``python3`` registered a
``pseudolife-codex-shim.service`` that exited 1 in a restart loop on a
Docker-tier Debian 13 host with no checkout venv (2026-09-29): this helper
exists so ``ops/install-shim-autostart.sh``,
``ops/install-codex-shim-autostart.sh`` and their ``.ps1`` twins never write
such a unit.

Candidates, in order, each verified by importing what the shims import with
the checkout on the path (the unit's own shape):

1. ``--python``, alone: the operator named it, so it is verified and
   refused, never replaced;
2. the checkout's ``.venv``;
3. pipx's ``pseudolife-mcp`` venv (the installer's shim install);
4. a venv this helper made on an earlier run (``--venv-dir``, default
   ``~/.pseudolife-mcp/shim-venv``);
5. ``python3`` / ``python`` on PATH (``pip install --user <checkout>``, the
   installer's shim install without pipx, lands there);
6. a new venv at ``--venv-dir`` with the checkout installed into it. The
   shim never runs a model, so torch is taken from PyTorch's CPU wheel index
   first (a fraction of the default build's size); ``--no-create`` refuses
   at this point instead.

Prints the chosen interpreter on stdout; what it tried and why on stderr.
Exit 1, with the fix, when nothing qualifies. Stdlib only, like the other
``ops/*.py`` helpers.

    ops/shim_python.py --repo <checkout> [--python EXE] [--venv-dir DIR] [--no-create]
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable

# What the shims import, the way they import it (repo root first on the path).
PROBE_IMPORT = "pseudolife_memory.memory.dream"
PROBE = f"import sys; sys.path.insert(0, sys.argv[1]); import {PROBE_IMPORT}"
# torch's first import from a cold disk cache is slow; the unit waits longer.
PROBE_TIMEOUT = 300
CPU_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"

Log = Callable[[str], None]


def venv_python(root: Path) -> Path:
    """The interpreter inside a venv, in this platform's layout."""
    if os.name == "nt":
        return Path(root) / "Scripts" / "python.exe"
    return Path(root) / "bin" / "python"


def default_venv_dir() -> Path:
    return Path.home() / ".pseudolife-mcp" / "shim-venv"


def probe(python: Path, repo: Path) -> tuple[bool, str]:
    """Import what the shims import, from the checkout: ``(ok, reason)``."""
    try:
        proc = subprocess.run([str(python), "-c", PROBE, str(repo)], cwd=str(repo),
                              capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except OSError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, f"the import did not finish within {PROBE_TIMEOUT}s"
    if proc.returncode == 0:
        return True, ""
    lines = [line.strip() for line in (proc.stderr or proc.stdout).splitlines() if line.strip()]
    return False, lines[-1] if lines else f"exit {proc.returncode}"


def pipx_home() -> Path | None:
    """pipx's home, from ``pipx environment`` when pipx answers, else its
    environment variable; None when neither says."""
    pipx = shutil.which("pipx")
    if pipx:
        try:
            proc = subprocess.run([pipx, "environment", "--value", "PIPX_HOME"],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip().splitlines()[-1])
    value = os.environ.get("PIPX_HOME")
    return Path(value) if value else None


def candidates(repo: Path, venv_dir: Path) -> list[tuple[Path, str]]:
    """Existing interpreters worth verifying, in order, with what each is."""
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def add(path: Path | None, label: str) -> None:
        if path is None or not path.is_file() or not os.access(path, os.X_OK):
            return
        # Absolute, but never resolved: a POSIX venv's bin/python is a
        # symlink to the base interpreter, and only its own path makes Python
        # find the venv (pyvenv.cfg beside it). Following the link probed
        # pipx's venv as the bare system python (Debian 13, 2026-09-29), and
        # every venv built from one Python shares that target, so it is no
        # key for de-duplication either.
        spelled = Path(os.path.abspath(path))
        if spelled in seen:
            return
        seen.add(spelled)
        found.append((spelled, label))

    add(venv_python(repo / ".venv"), "the checkout's .venv")
    home = pipx_home()
    if home is not None:
        add(venv_python(home / "venvs" / "pseudolife-mcp"), "pipx's pseudolife-mcp venv")
    add(venv_python(venv_dir), f"the shim venv made earlier at {venv_dir}")
    for name in ("python3", "python"):
        exe = shutil.which(name)
        if exe:
            add(Path(exe), f"{name} on PATH")
    return found


def _run(cmd: list[str], log: Log) -> int:
    """Run a build step with its output on stderr (stdout carries the result)."""
    try:
        sys.stderr.flush()
        if hasattr(sys.stderr, "fileno") and callable(sys.stderr.fileno):
            try:
                proc = subprocess.run(cmd, stdout=sys.stderr, stderr=subprocess.STDOUT, check=False)
                return proc.returncode
            except (ValueError, OSError):
                pass  # a redirected stderr without a file descriptor: capture instead
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        for line in (proc.stdout + proc.stderr).splitlines():
            log(line)
        return proc.returncode
    except OSError as exc:
        log(f"{cmd[0]}: {exc}")
        return 1


def create_venv(venv_dir: Path, repo: Path, log: Log) -> Path | None:
    """Make a venv at ``venv_dir`` with the checkout installed; its python,
    or None with the failure already logged."""
    base = Path(sys.executable)
    if sys.version_info < (3, 10):
        log(f"cannot create a venv for the shim: this helper runs under Python "
            f"{sys.version.split()[0]} and the package needs 3.10 or newer")
        return None
    log(f"creating a venv for the shim at {venv_dir} from {base} and installing the "
        f"checkout into it. Its dependencies include torch, taken from PyTorch's CPU "
        f"wheel index (the shim never runs a model); this takes a few minutes the "
        f"first time...")
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    if _run([str(base), "-m", "venv", str(venv_dir)], log) != 0:
        log(f"python -m venv {venv_dir} failed")
        return None
    python = venv_python(venv_dir)
    if _run([str(python), "-m", "pip", "install", "--index-url", CPU_TORCH_INDEX, "torch"], log) != 0:
        log("the CPU torch wheel index did not answer; taking the default torch build instead")
    if _run([str(python), "-m", "pip", "install", str(repo)], log) != 0:
        log(f"pip install {repo} into {venv_dir} failed")
        return None
    return python


def _fix(repo: Path, venv_dir: Path) -> str:
    return ("Name an interpreter that imports pseudolife_memory with --python (the "
            "checkout's venv, pipx's pseudolife-mcp venv, or one you make):\n"
            f"  {sys.executable} -m venv {venv_dir}\n"
            f"  {venv_python(venv_dir)} -m pip install {repo}")


def choose(repo: Path, explicit: Path | None, venv_dir: Path, allow_create: bool,
           log: Log) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            log(f"{explicit}: not found (named with --python)")
            log(_fix(repo, venv_dir))
            return None
        ok, why = probe(explicit, repo)
        if ok:
            log(f"shim interpreter: {explicit} (named with --python; imports {PROBE_IMPORT})")
            return explicit
        log(f"{explicit}, named with --python, cannot import {PROBE_IMPORT}: {why}")
        log(_fix(repo, venv_dir))
        return None
    for path, label in candidates(repo, venv_dir):
        ok, why = probe(path, repo)
        if ok:
            log(f"shim interpreter: {path} ({label}; imports {PROBE_IMPORT})")
            return path
        log(f"skipped {path}: {why} ({label})")
    if not allow_create:
        log(f"no interpreter on this machine imports {PROBE_IMPORT}, and --no-create "
            f"was given, so none was made.")
        log(_fix(repo, venv_dir))
        return None
    python = create_venv(venv_dir, repo, log)
    if python is None:
        log(_fix(repo, venv_dir))
        return None
    ok, why = probe(python, repo)
    if not ok:
        log(f"the venv created at {venv_dir} still cannot import {PROBE_IMPORT}: {why}")
        log(_fix(repo, venv_dir))
        return None
    log(f"shim interpreter: {python} (created at {venv_dir} from the checkout; "
        f"imports {PROBE_IMPORT})")
    return python


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pick and verify the interpreter for an extractor shim's autostart unit.")
    parser.add_argument("--repo", required=True, type=Path, help="the checkout the shim runs from")
    parser.add_argument("--python", type=Path, default=None,
                        help="verify this interpreter only; refused rather than replaced")
    parser.add_argument("--venv-dir", type=Path, default=None,
                        help=f"where a venv of this helper's own lives (default {default_venv_dir()})")
    parser.add_argument("--no-create", action="store_true",
                        help="refuse instead of creating a venv when nothing else qualifies")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    venv_dir = (args.venv_dir or default_venv_dir()).resolve()
    # Absolute against the caller's directory, never resolved (a venv's
    # interpreter is a symlink): the probe runs from the checkout and the
    # unit from its own directory, so a relative path would name a
    # different file in each.
    explicit = Path(os.path.abspath(args.python)) if args.python is not None else None

    def log(message: str) -> None:
        print(message, file=sys.stderr)

    chosen = choose(repo, explicit, venv_dir, not args.no_create, log)
    if chosen is None:
        return 1
    print(chosen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
