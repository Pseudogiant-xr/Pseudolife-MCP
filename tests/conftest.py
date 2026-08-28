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


def pytest_configure(config: pytest.Config) -> None:
    """Load each distinct embedding model once per session, not per module.

    ``warm_service`` is module-scoped, and every ``MemoryService`` builds its
    own ``EmbeddingPipeline`` -> its own ``SentenceTransformer``. Under the
    schema-v25 default (Qwen3-Embedding-0.6B, 2.52 GB resident apiece) the
    full suite peaked at ~49.5 GB private commit and killed a run on a 64 GB
    host (2026-07-29) — dropped services sit in reference cycles, and even
    collected ones don't return their arenas to the OS.

    Memoize the model LOAD only, never the ``EmbeddingPipeline``: each
    pipeline keeps its own encode LRU and dim state, so nothing crosses a
    test boundary. The weights are read-only at inference — with one
    exception: ``EmbeddingPipeline.__init__`` caps ``model.max_seq_length``
    in place, reading the *current* value as the floor, so two configs with
    different caps on one shared model would ratchet it down permanently.
    The memoization key is the full construction signature, so a different
    cap (or model, device, backend...) loads its own instance. Pinned by
    ``tests/test_shared_embedding_weights.py``.

    Measured effect on the full suite (1792 tests, 2026-07-29): peak
    49.5 GB -> 5.39 GB, runtime 13:23 -> 6:44, with four distinct loads —
    Qwen3, MiniLM torch, MiniLM ONNX, plus the guard test's
    deliberately-capped ~90 MB MiniLM.
    """
    from pseudolife_memory.memory import embedding as embedding_module
    from pseudolife_memory.utils.config import EmbeddingConfig

    real_load = embedding_module.SentenceTransformer
    loaded: dict[tuple, object] = {}
    # __init__ mutates max_seq_length post-construction (the ratchet above),
    # but it is not a constructor argument — fold the *config* cap into the
    # key via a contextvar-free side channel: EmbeddingPipeline sets the cap
    # to min(model default, config.max_seq_length), so keying on the config
    # value that will be applied keeps differently-capped pipelines apart.
    default_cap = EmbeddingConfig.max_seq_length

    def _shared_load(*args, **kwargs):  # noqa: ANN002, ANN003 — passthrough
        cap = _shared_load.next_cap if _shared_load.next_cap is not None \
            else default_cap
        key = (
            args,
            tuple(sorted((k, repr(v)) for k, v in kwargs.items())),
            cap,
        )
        if key not in loaded:
            loaded[key] = real_load(*args, **kwargs)
        return loaded[key]

    _shared_load.next_cap = None

    real_pipeline_init = embedding_module.EmbeddingPipeline.__init__

    def _capturing_init(self, config):  # noqa: ANN001 — mirrors the real sig
        _shared_load.next_cap = getattr(
            config, "max_seq_length", default_cap,
        )
        try:
            real_pipeline_init(self, config)
        finally:
            _shared_load.next_cap = None

    embedding_module.SentenceTransformer = _shared_load
    embedding_module.EmbeddingPipeline.__init__ = _capturing_init


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
    starts with an empty bank: the CMS bands (and their episode log), the
    reference bank, and the cortex.

    NOT cleared, deliberately: the world cortex and the lesson store have no
    equivalent reset — ``WorldCortexStore``/``LessonStore`` expose only
    per-entity ``forget()`` — and no test in the tree reads world or lesson
    state it did not itself write (surveyed 2026-08-28 across all thirteen
    fixture-consuming files). Also not reset: ``svc.config``, which outlives
    the bank clear, so a test that flips a config knob must restore it.
    """
    warm_service._ensure_init()  # noqa: SLF001 — fixture wiring.
    assert warm_service._cms is not None
    warm_service._cms.clear()
    # Slot-keyed facts survive a CMS clear — without this, cortex writes leak
    # between tests sharing the module-scoped service, and files compensated
    # by hand-rolling ``cortex_forget`` cleanup in finally blocks.
    if warm_service._cortex is not None:
        warm_service._cortex.clear()
    if warm_service._reference is not None:
        try:
            warm_service._reference.clear()
        except Exception:  # noqa: BLE001 — chromadb may complain on empty.
            pass
    return warm_service
