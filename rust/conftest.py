"""Options for the Python tests under rust/ (the full suite collects only tests/)."""
import os
import runpy
from pathlib import Path

# database_gate.REQUIRE_DATABASES_ENV; test_pgdisposable_slices pins that they match.
REQUIRE_DATABASES_ENV = "PL_HARNESS_REQUIRE_DATABASES"

# On Windows, console children (pwsh, cmd, python) of a run that has no
# console open no window on the developer's desktop. The helper is
# tests/windowless_children.py, run by its path: this conftest does not put
# the repository root on sys.path.
runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests" / "windowless_children.py"))["install"]()


def pytest_addoption(parser):
    parser.addoption(
        "--database-turn-wait-seconds", type=float, default=0,
        help="on an opted-in dev machine, wait this many seconds for its "
             "database turn instead of refusing immediately")
    parser.addoption(
        "--require-databases", action="store_true", default=False,
        help="fail, rather than skip, a harness database case that finds no "
             "disposable-bank login or Docker engine (the batch's local "
             "Rust harness run; see ops/integrate.py RUST_HARNESS)")


def pytest_configure(config):
    marker = Path.home() / ".pseudolife-mcp/locks/test-postgres.turn-enabled"
    if marker.exists():
        # Default machines stop at that one existence check. Only an opted-in
        # machine imports the engine or inspects process identities.
        import sys
        import pytest

        root = str(Path(__file__).resolve().parents[1])
        inserted = root not in sys.path
        if inserted:
            sys.path.insert(0, root)
        try:
            from rust.harness_support.database_turn import ensure_turn

            lease = ensure_turn(holder="pytest Rust harness", contain=True,
                                wait_seconds=config.getoption("--database-turn-wait-seconds"))
        except (RuntimeError, ValueError) as error:
            raise pytest.UsageError(str(error)) from None
        finally:
            if inserted:
                sys.path.remove(root)
        if lease is not None:
            config.add_cleanup(lease.close)
    if config.getoption("--require-databases"):
        os.environ[REQUIRE_DATABASES_ENV] = "1"
