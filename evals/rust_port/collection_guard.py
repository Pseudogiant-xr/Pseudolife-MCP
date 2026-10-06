"""Require every eval test file to contribute an item to the Parity collection."""
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
EVAL_DIRECTORIES = ("evals/rust_port", "evals/rust_baseline")


def uncollected_test_files(root, items):
    expected = {path.resolve() for directory in EVAL_DIRECTORIES
                for path in (root / directory).rglob("test_*.py")}
    collected = {Path(item.path).resolve() for item in items}
    return sorted(path.relative_to(root).as_posix() for path in expected - collected)


def pytest_addoption(parser):
    parser.addoption("--eval-collection-guard", action="store_true",
                     help="require all rust_port and rust_baseline test files to be collected")


def pytest_collection_finish(session):
    if session.config.getoption("--eval-collection-guard"):
        missing = uncollected_test_files(ROOT, session.items)
        if missing:
            raise pytest.UsageError("uncollected eval test files: " + ", ".join(missing))
