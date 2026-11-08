"""Whether a harness test may skip a database case it cannot run.

Hosted CI has no disposable-bank login, so there a database case skips. The
batch's local Rust harness run (`ops/integrate.py`'s `RUST_HARNESS`, run with
`--require-databases`) has the bench login and Docker, so a database case it
skipped would pass unseen: there it fails instead, naming the cause and the
fix. `rust/conftest.py` sets the variable from the option.
"""
from __future__ import annotations

import os

REQUIRE_DATABASES_ENV = "PL_HARNESS_REQUIRE_DATABASES"


def unavailable(cause: str, fix: str):
    """Skip a database case that cannot run here, or, when the run requires
    databases, fail it naming ``cause`` and ``fix``."""
    import pytest

    if os.environ.get(REQUIRE_DATABASES_ENV) == "1":
        pytest.fail(f"{cause}: {fix}. This run requires databases (--require-databases), "
                    "so a database case may not skip.", pytrace=False)
    pytest.skip(cause)
