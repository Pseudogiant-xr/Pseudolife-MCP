"""Run an operator command inside the bundled daemon container.

``lease break|delegate``, ``maintainer`` and ``board-audit`` open the bank
directly, with the database owner's credentials: ``PSEUDOLIFE_MCP_DATABASE_URL``
or the lite tier's embedded instance. On a Docker-tier host neither is in the
operator's shell: the URL lives in the ``pseudolife-mcp-daemon`` container's
environment. When a command finds no bank and that container is running
here, it re-runs itself there (``docker exec``, as ``invite`` and
``maintainer setup`` already do) with the same arguments, a terminal passed
through when it has one, and the container's exit code.

The re-run sets ``PSEUDOLIFE_DAEMON_EXEC`` in the container, and a command
that sees it never re-runs again; inside the container the URL is set
anyway, so this is a second guard against a loop, not the first.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Sequence

CONTAINER = "pseudolife-mcp-daemon"
NESTED_ENV = "PSEUDOLIFE_DAEMON_EXEC"
INSPECT_TIMEOUT_S = 30

# The seam the tests replace.
run = subprocess.run


class NoBank(Exception):
    """No database URL and no lite bank in this environment."""


def docker_cmd() -> str:
    return os.environ.get("PSEUDOLIFE_DOCKER") or "docker"


def no_bank_message(mode: str) -> str:
    return ("no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL, or run "
            "where the lite tier's data dir holds one (Docker tier: start the daemon, or run "
            f"`docker exec -it {CONTAINER} pseudolife-mcp {mode} ...` on its host)")


def _running(docker: str) -> bool:
    if docker == "docker" and not shutil.which("docker"):
        return False
    try:
        proc = run([docker, "inspect", "-f", "{{.State.Running}}", CONTAINER],
                   capture_output=True, text=True, errors="replace",
                   stdin=subprocess.DEVNULL, timeout=INSPECT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == "true"


def _terminal() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):   # closed or replaced streams
        return False


def run_in_daemon(mode: str, argv: Sequence[str], *, stdin=None,
                  stdout=None) -> subprocess.CompletedProcess | None:
    """``pseudolife-mcp <mode> <argv>`` inside the daemon container, or
    ``None`` when it cannot run there (already inside, no Docker, no running
    container). ``stdin`` / ``stdout`` default to this process's own; with
    both left alone and both terminals, the container gets a terminal too
    (``-it``), so its questions still reach the operator."""
    if os.environ.get(NESTED_ENV):
        return None
    docker = docker_cmd()
    if not _running(docker):
        return None
    tty = stdin is None and stdout is None and _terminal()
    command = [docker, "exec", "-it" if tty else "-i", "-e", f"{NESTED_ENV}=1", CONTAINER,
               "python", "-m", "pseudolife_memory.cli", mode, *argv]
    print(f"pseudolife-mcp {mode}: no bank in this shell; running it inside the {CONTAINER} "
          "container", file=sys.stderr, flush=True)
    sys.stdout.flush()
    try:
        return run(command, stdin=stdin, stdout=stdout, check=False)
    except OSError:
        return None
