"""The marker for harness tests that exercise only the frozen Python package.

While the package is still published, the release cut runs these tests on
purpose: they are what can see a dependency regression in it. They are
deleted with the package (``rust/ci/package_consumers.toml`` lists their
files as retiring with it), never moved.

The skip decides on one signal: whether the import system can find the
package (``importlib.util.find_spec``). A package that is present but cannot
be imported (a broken dependency inside it) is not skipped; its tests fail.
"""
from __future__ import annotations

import importlib.util

import pytest

PACKAGE = "pseudolife_memory"
REASON = "frozen Python only: deleted with the package"


def package_present() -> bool:
    """Whether the frozen package can be found on the import path."""
    return importlib.util.find_spec(PACKAGE) is not None


only_frozen_python = pytest.mark.skipif(not package_present(), reason=REASON)
