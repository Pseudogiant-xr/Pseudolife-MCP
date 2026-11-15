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
        "--require-databases", action="store_true", default=False,
        help="fail, rather than skip, a harness database case that finds no "
             "disposable-bank login or Docker engine (the batch's local "
             "Rust harness run; see ops/integrate.py RUST_HARNESS)")


def pytest_configure(config):
    if config.getoption("--require-databases"):
        os.environ[REQUIRE_DATABASES_ENV] = "1"
