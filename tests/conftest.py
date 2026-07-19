"""Shared fixtures for the MCP test suite.

A single ``MemoryService`` instance per session is too coarse: tests
that mutate memory state would pollute each other. A fresh
``tmp_path`` per test is too fine: loading the embedder takes ~1.5s
on CPU. The compromise is a module-scoped service in
:func:`pristine_service` that ``clear()``-s the bank between tests —
the embedder and torch graphs stay warm, but the bank is empty for
each test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Silence torch.dynamo before any import. Mirrors the production server.
os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")

# Allow `from pseudolife_memory...` from the test files without an editable
# install. Keeps CI/setup minimal.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Bench-DB isolation: evals' reset_bench() reaps every backend on its
# database before truncating, so concurrent suite runs must not share one
# bench DB (same crossfire as pg_fixtures' per-run test DB — see its module
# docstring). Pin a per-run name before any test imports ladder_sweep, and
# drop the database at exit. Eval CLI runs are unaffected (env unset there).
if "PSEUDOLIFE_BENCH_DB" not in os.environ:
    os.environ["PSEUDOLIFE_BENCH_DB"] = f"pseudolife_memory_bench_{os.getpid()}"

    def _drop_run_bench_db() -> None:
        try:
            import psycopg

            admin = os.environ.get(
                "PSEUDOLIFE_BENCH_ADMIN_URL",
                "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres",
            )
            admin = admin.rsplit("/", 1)[0] + "/postgres"
            db = os.environ["PSEUDOLIFE_BENCH_DB"]
            with psycopg.connect(admin, connect_timeout=3, autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        except Exception:  # noqa: BLE001 — best-effort; pg_fixtures prunes leftovers
            pass

    import atexit

    atexit.register(_drop_run_bench_db)

import pytest

if TYPE_CHECKING:
    from pseudolife_memory.service import MemoryService


@pytest.fixture(scope="module")
def warm_service(tmp_path_factory: pytest.TempPathFactory) -> MemoryService:
    """One service per test module — embedder stays warm, data dir
    survives for the module. Tests that need a pristine bank should use
    :func:`pristine_service` (function-scoped) instead.
    """
    from pseudolife_memory.service import MemoryService
    data_dir = tmp_path_factory.mktemp("warm-service")
    return MemoryService(data_dir=data_dir)


@pytest.fixture
def pristine_service(warm_service: MemoryService) -> MemoryService:
    """Function-scoped wrapper that clears the warm service's banks.

    Re-uses the loaded embedder + torch graphs but guarantees each test
    starts with an empty bank.
    """
    warm_service._ensure_init()  # noqa: SLF001 — fixture wiring.
    assert warm_service._cms is not None
    warm_service._cms.clear()
    if warm_service._reference is not None:
        try:
            warm_service._reference.clear()
        except Exception:  # noqa: BLE001 — chromadb may complain on empty.
            pass
    warm_service._last_user_query = None
    return warm_service
