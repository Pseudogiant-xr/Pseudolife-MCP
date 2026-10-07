"""Generate lease help from the pinned Python parser, without a bank or daemon."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ORACLE_HEAD = "3c01bb31abd60178e15dea99adda369b4bbf92fc"
ASSET_DIRECTORY = "rust/shim/src/cli/lease/assets"
ASSETS = tuple(f"{action}_{kind}.txt"
               for action in ("top", "run", "hold", "check", "list", "break", "delegate")
               for kind in ("help", "usage"))
ORACLE_SOURCES = ("__init__.py", "lease_cli.py", "os_lock.py", "daemon_exec.py")


def committed_bytes(root: Path, revision: str, relative: str) -> bytes:
    return subprocess.check_output(
        ["git", "show", f"{revision}:{relative}"], cwd=root)


def pinned_help(root: Path, scratch: Path) -> dict[str, str]:
    """Execute the genuine parser from Git's pin using only disposable source."""
    package = scratch / "pseudolife_memory"
    package.mkdir(parents=True)
    for name in ORACLE_SOURCES:
        (package / name).write_bytes(committed_bytes(
            root, ORACLE_HEAD, f"pseudolife_memory/{name}"))
    script = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from pseudolife_memory.lease_cli import _parsers
top, run, others = _parsers()
parsers = {"top": top, "run": run, **others}
print(json.dumps({f"{action}_{kind}.txt": getattr(parser, f"format_{kind}")()
                  for action, parser in parsers.items() for kind in ("help", "usage")}))
"""
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR")
                   if key in os.environ}
    environment["COLUMNS"] = "80"
    output = subprocess.check_output(
        [sys.executable, "-I", "-c", script, str(scratch.resolve())],
        cwd=scratch, env=environment)
    return json.loads(output)
