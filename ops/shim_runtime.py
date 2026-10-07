"""Manage side-by-side shim runtimes from a checkout, before any install.

    python ops/shim_runtime.py install --source <checkout>   # prints the launcher path
    python ops/shim_runtime.py migrate [--client codex]      # registrations -> launcher
    python ops/shim_runtime.py prune | list | current | launcher [--if-installed]

The logic is ``pseudolife_memory/runtimes.py``, loaded here by file path so
the installers can run it from a fresh clone with nothing installed, and so
an older installed package never answers for the checkout being installed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("checkout_runtimes", ROOT / "pseudolife_memory" / "runtimes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    sys.exit(_load().main(sys.argv[1:]))
