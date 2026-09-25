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

# Pin the CPU embedder to fp32 for the whole suite. EmbeddingConfig.cpu_dtype
# defaults to "auto", which picks bf16 on a CPU with native bf16, so without
# this the suite's numerics would follow the host. Set outright (an exported
# developer value must not flip it), and as an env var rather than a fixture
# because the daemons the suite spawns inherit os.environ too.
os.environ["PSEUDOLIFE_EMBEDDING_CPU_DTYPE"] = "fp32"

# Allow `from pseudolife_memory...` from the test files without an editable
# install. Keeps CI/setup minimal.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# CPU-only unless PSEUDOLIFE_TEST_CUDA=1, and one full suite per machine
# unless its lock allows more slots — tests/suite_lock.py carries the
# measurements. The GPU is hidden here,
# before anything can import torch; the lock is taken in pytest_configure.
from tests import suite_lock  # noqa: E402

suite_lock.hide_cuda(os.environ)

# The eval-backed suites (test_recall, test_memcot_bench,
# test_constraint_pinning) and evals/ladder_sweep.py read the bench admin
# URL from PSEUDOLIFE_BENCH_ADMIN_URL. Seed it once, here, from the same
# resolver pg_fixtures uses (explicit test DSN, then password from ops/.env),
# so a rotated dev password or alternate test server cannot turn those files
# into silent skips. An operator's own bench value is left alone.
from tests.pg_defaults import bench_admin_url, conninfo_with_dbname  # noqa: E402

if "PSEUDOLIFE_BENCH_ADMIN_URL" not in os.environ:
    os.environ["PSEUDOLIFE_BENCH_ADMIN_URL"] = bench_admin_url()
    os.environ["_PSEUDOLIFE_BENCH_ADMIN_URL_SEEDED"] = "1"

# Isolate client configuration before test-module imports can snapshot it.
# Model caches and ordinary home-directory lookup stay intact; only the Codex
# connection and its credentials/state are redirected to this owned temp home.
import tempfile
from tests.client_environment import isolate_client_environment

_client_test_home = tempfile.TemporaryDirectory(prefix="pseudolife-test-codex-")
isolate_client_environment(os.environ, _client_test_home.name)

# The suite never talks to a real dream extractor. The endpoint selection
# reads PSEUDOLIFE_DREAM_* from the ambient environment (memory/dream.py,
# resolve_endpoints), so a shell with the ops values exported would send
# every end-of-session dream a test fires to a live model — and pg_conn now
# waits for those dream threads before it reaps (tests/pg_fixtures.py), so a
# real call could hold the next PG test for the extractor timeout (240 s
# default) instead of the no-op extractor's milliseconds. Tests that need an
# endpoint set it with monkeypatch; none read the ambient value.
EXTRACTOR_ENDPOINT_ENV = (
    "PSEUDOLIFE_DREAM_BASE_URL", "PSEUDOLIFE_DREAM_MODEL",
    "PSEUDOLIFE_DREAM_FALLBACK_BASE_URL", "PSEUDOLIFE_DREAM_FALLBACK_MODEL",
    "PSEUDOLIFE_DREAM_EXTRACTOR_MODE", "PSEUDOLIFE_DREAM_API_KEY",
)


def scrub_extractor_endpoint_env(environ) -> list[str]:
    """Drop the extractor endpoint selection from ``environ``; returns the
    names that were set. Only the selection variables — timeout and token
    budgets are harmless without an endpoint and stay as the operator left
    them."""
    return [name for name in EXTRACTOR_ENDPOINT_ENV
            if environ.pop(name, None) is not None]


scrub_extractor_endpoint_env(os.environ)


# The production daemon's DSN never reaches the suite. MemoryService falls
# back to PSEUDOLIFE_MCP_DATABASE_URL when no database_url is passed, so an
# exported value binds every file-mode fixture to that bank — and
# pristine_service.save() then snapshots a just-cleared cortex over it
# (replace_facts([]) -> DELETE FROM facts), a path no reset-site guard sees
# (2026-09-23 review). Tests that need a PG-bound service set the variable
# themselves (monkeypatch in pg_service, a child env in the daemon fixtures);
# none read the ambient value. Its database NAME is kept — never the DSN,
# which carries a credential — so the reset guard keeps refusing that bank;
# and because a record always exists, the guard ignores the live variable
# those tests point at their own per-run databases.
def scrub_live_bank_dsn(environ) -> str:
    """Drop PSEUDOLIFE_MCP_DATABASE_URL from ``environ`` and record the bank
    it named for the reset guard: the default bank's name when none was
    exported, and an inherited record (an xdist worker's, from the
    controller) is kept. A DSN that leaves its database implicit (libpq's
    user-name default, or a service file) is recorded as unresolved, which
    stops the run in pytest_configure — the DSN is about to be gone, and
    with it the only clue to the bank's name. Returns the recorded name."""
    from pseudolife_memory.storage.schema import (
        DEFAULT_PRODUCTION_DATABASE, PRODUCTION_DATABASE_ENV,
        UNRESOLVED_PRODUCTION_DATABASE, dsn_database_name,
    )

    dsn = environ.pop("PSEUDOLIFE_MCP_DATABASE_URL", None)
    if dsn:
        environ[PRODUCTION_DATABASE_ENV] = (
            dsn_database_name(dsn) or UNRESOLVED_PRODUCTION_DATABASE)
    else:
        # Never the empty string: Windows deletes a variable assigned one,
        # and xdist workers would then inherit no record at all.
        environ.setdefault(PRODUCTION_DATABASE_ENV, DEFAULT_PRODUCTION_DATABASE)
    return environ[PRODUCTION_DATABASE_ENV]


scrub_live_bank_dsn(os.environ)

# Bench-DB isolation: evals' reset_bench() reaps every backend on its
# database before truncating, so concurrent suite runs must not share one
# bench DB (same crossfire as pg_fixtures' per-run test DB — see its module
# docstring). Pin a per-run name before any test imports ladder_sweep, and
# drop the database at exit. Eval CLI runs are unaffected (env unset there).
def bench_db_autopin(environ) -> str | None:
    """The per-process bench-DB name to pin, or None to keep a user value.

    Pins when the variable is unset, and RE-pins when the inherited value
    is a parent process's own autopin (the ``_PSEUDOLIFE_BENCH_DB_AUTOPIN``
    sentinel matches) — an xdist worker inherits the controller's pin, and
    sharing it would put every worker's reset_bench() reaper on one
    database. A value set without the sentinel is a deliberate operator
    override and wins verbatim.
    """
    current = environ.get("PSEUDOLIFE_BENCH_DB")
    if current is not None and current != environ.get(
            "_PSEUDOLIFE_BENCH_DB_AUTOPIN"):
        return None
    return f"pseudolife_memory_bench_{os.getpid()}"


_bench_pin = bench_db_autopin(os.environ)
if _bench_pin is not None:
    os.environ["PSEUDOLIFE_BENCH_DB"] = _bench_pin
    os.environ["_PSEUDOLIFE_BENCH_DB_AUTOPIN"] = _bench_pin

    def _drop_run_bench_db() -> None:
        try:
            import psycopg

            admin = os.environ.get("PSEUDOLIFE_BENCH_ADMIN_URL") or bench_admin_url()
            admin = conninfo_with_dbname(admin, "postgres")
            # The name pinned above, not whatever PSEUDOLIFE_BENCH_DB holds
            # by exit: a DROP ... WITH (FORCE) must never follow a later
            # (or mistyped) value onto a database this run did not create.
            db = _bench_pin
            with psycopg.connect(admin, connect_timeout=3, autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        except Exception:  # noqa: BLE001 — best-effort; pg_fixtures prunes leftovers
            pass

    import atexit

    atexit.register(_drop_run_bench_db)

# mcp.client.stdio.stdio_client binds ``errlog=sys.stderr`` as a default at
# first import (``import mcp`` imports it eagerly). A first import inside a
# capsys test binds capsys's CaptureIO, which has no fileno, and every later
# stdio_client(params) without errlog= in the process fails with
# io.UnsupportedOperation — six tests/test_shim.py failures when PR #352's
# test_codex_doorbell.py ran first (2026-09-23). Importing here, before any
# test runs, binds pytest's session-long fd-capture file instead (the
# terminal under -s; --capture=sys/tee-sys offer no fileno at all). The
# filter keeps opentelemetry's import-time DeprecationWarning as unrecorded
# as it was when torch first imported it inside pytest_configure. Pinned by
# tests/test_mcp_stdio_errlog.py.
import warnings  # noqa: E402

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="SelectableGroups dict interface is deprecated",
        category=DeprecationWarning)
    import mcp.client.stdio  # noqa: E402, F401

import pytest

from tests.fake_embedder import FakeSentenceTransformer, is_known

if TYPE_CHECKING:
    from pseudolife_memory.service import MemoryService

_SUITE_LOCK = pytest.StashKey[suite_lock.HeldLock]()

# Fixture HTTP servers (stub extractors, fake daemons, shim upstreams) run
# serve_forever() on a thread, and shutdown() waits for its next poll: the
# stdlib default of 0.5 s cost nearly that much per server, since fixtures
# stop right after their last request. Measured 2026-09-23 UTC on six of the
# server-heavy files (listed in each artifact's command; 187 tests, 16-CPU
# Windows host): 65.2 s at 0.5 s, 42.5-47.5 s at 0.05 s, two runs each, a gain
# that includes one 2 s retry sleep dropped from test_extractor_fallback
# (evals/results/suite-cost-fixture-server-shutdown-slice-*.json). Only
# servers inside this pytest process are affected; the eval shims' own
# serve_forever() runs in their separate processes.
import socketserver  # noqa: E402

# Fail at import, not as a hung shutdown(), if the stdlib signature changes.
assert socketserver.BaseServer.serve_forever.__defaults__ == (0.5,)
socketserver.BaseServer.serve_forever.__defaults__ = (0.05,)

# Which embedder a test gets. By default every test that is not marked
# ``real_model`` runs on tests/fake_embedder.py's deterministic hashing model;
# PSEUDOLIFE_TEST_EMBEDDER=real restores the real weights for the whole run.
EMBEDDER_ENV = "PSEUDOLIFE_TEST_EMBEDDER"
_real_model_test = False


def embedder_mode(environ) -> str:
    """"fake" (the default) or "real". Anything else is refused: a typo in
    CI's all-real lane must not quietly run it on the fake and still pass."""
    value = environ.get(EMBEDDER_ENV)
    if value is None:
        return "fake"
    if value not in ("fake", "real"):
        raise pytest.UsageError(
            f"{EMBEDDER_ENV} must be 'fake' or 'real' (or unset), got {value!r}")
    return value


def _use_fake_embedder(args: tuple, kwargs: dict) -> bool:
    if embedder_mode(os.environ) == "real" or _real_model_test:
        return False
    if kwargs.get("backend", "torch") != "torch":
        return False
    return is_known(args[0] if args else kwargs.get("model_name_or_path"))


def _refuse_fake_in_real_model_test() -> None:
    # A module-scoped service built by an earlier unmarked test would hand a
    # real_model test the fake; fail rather than pass on the wrong model.
    if _real_model_test:
        raise RuntimeError(
            "a real_model test is embedding through the fake model built by "
            "an earlier unmarked test in this module; mark the module "
            "(pytestmark = pytest.mark.real_model) so the shared service "
            "loads the real weights")


FakeSentenceTransformer.guard = staticmethod(_refuse_fake_in_real_model_test)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Record whether the test about to set up needs the real weights —
    before its fixtures run, so module-scoped services see it too."""
    global _real_model_test
    _real_model_test = item.get_closest_marker("real_model") is not None


def pytest_sessionstart(session: pytest.Session) -> None:
    """Required CI database coverage must fail before collection can skip it."""
    if os.environ.get("PSEUDOLIFE_REQUIRE_TEST_POSTGRES") != "1":
        return
    try:
        import psycopg  # noqa: F401 — cannot use importorskip in a required lane
    except ImportError:
        raise pytest.UsageError("Required test PostgreSQL needs psycopg") from None

    from pseudolife_memory.storage.schema import ProductionDatabaseError
    from tests.helpers import pg_reachable
    from tests.pg_defaults import (
        PostgresAuthError, PostgresSetupError, PostgresUnavailableError,
    )
    from tests.pg_fixtures import ensure_test_db, resolve_test_db_url

    try:
        ensure_test_db()
        pg_reachable(resolve_test_db_url())
    except (PostgresAuthError, PostgresSetupError, PostgresUnavailableError,
            ProductionDatabaseError) as exc:
        raise pytest.UsageError(str(exc)) from None


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
    # Before anything else: refuse to run when the exported daemon DSN hid
    # its database (scrub_live_bank_dsn above). Every PG reset would refuse
    # anyway; one message here beats a thousand errors.
    from pseudolife_memory.storage.schema import (
        PRODUCTION_DATABASE_ENV, UNRESOLVED_PRODUCTION_DATABASE,
    )

    if os.environ.get(PRODUCTION_DATABASE_ENV) == UNRESOLVED_PRODUCTION_DATABASE:
        raise pytest.UsageError(
            "PSEUDOLIFE_MCP_DATABASE_URL is exported without an explicit "
            "database name (libpq would use the user name or a service file), "
            "so the test/bench reset guard cannot identify the production "
            "bank. Unset it — the suite never needs it — or add dbname=.")

    # A full run queues for the suite lock first, while it holds ~50 MB:
    # the embedding import below commits ~1.3 GB (measured 2026-09-23).
    # What it imported from the checkout by now (this file and its imports)
    # is fingerprinted before the wait and re-checked after: a run whose
    # copies changed on disk while it queued stops instead of running them.
    held = suite_lock.take_for_session(config, os.environ, ROOT / "tests")
    if held is not None:
        config.stash[_SUITE_LOCK] = held


    embedder_mode(os.environ)  # a bad PSEUDOLIFE_TEST_EMBEDDER fails here
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
        fake = _use_fake_embedder(args, kwargs)
        key = (
            args,
            tuple(sorted((k, repr(v)) for k, v in kwargs.items())),
            cap,
            fake,
        )
        if key not in loaded:
            loaded[key] = (FakeSentenceTransformer(*args, **kwargs) if fake
                           else real_load(*args, **kwargs))
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

    # The guard sits on the pipeline as well as the fake: the pipeline's LRU
    # answers repeated texts without calling the model at all.
    real_encode = embedding_module.EmbeddingPipeline.encode

    def _guarded_encode(self, texts, normalize=True):  # noqa: ANN001
        if getattr(self.model, "is_fake", False):
            _refuse_fake_in_real_model_test()
        return real_encode(self, texts, normalize)

    embedding_module.EmbeddingPipeline.encode = _guarded_encode


def _match_embedder_to_test(svc: MemoryService) -> None:
    """Rebuild ``svc``'s pipeline when the running test wants the other kind
    of embedder (fake vs real weights) than the one it holds."""
    from pseudolife_memory.memory.embedding import EmbeddingPipeline

    config = svc.config.embedding
    model = getattr(svc._embedder, "model", None)  # noqa: SLF001
    # The backend in use, not the configured one: an "onnx" config that fell
    # back to torch holds a fake or real torch model like any other; a real
    # ONNX model is never faked, so rebuilding it would change nothing.
    if model is None or getattr(svc._embedder, "backend", "torch") != "torch":  # noqa: SLF001
        return
    if getattr(model, "is_fake", False) != _use_fake_embedder(
            (config.model_name,), {}):
        svc._embedder = EmbeddingPipeline(config)  # noqa: SLF001


def pytest_unconfigure(config: pytest.Config) -> None:
    held = config.stash.get(_SUITE_LOCK, None)
    if held is not None:
        suite_lock.release(held)


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

    The embedder follows the test: the bank is emptied here, so a
    ``real_model`` test in a module of fake-embedder tests (or the reverse)
    gets a pipeline of its own kind without leaving vectors of the other
    kind in the bank. (The uncleared world and lesson stores can keep them;
    per the survey above, no test reads state there it did not write.)
    Model loads are memoized, so the swap costs no reload.
    """
    warm_service._ensure_init()  # noqa: SLF001 — fixture wiring.
    _match_embedder_to_test(warm_service)
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
