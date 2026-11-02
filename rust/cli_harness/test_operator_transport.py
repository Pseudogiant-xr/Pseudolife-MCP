"""Exercise the oracle side of native transport rows with real child processes."""
import sys
from pathlib import Path

import pytest

from . import core
from .rows import invite, lease_transport, pair


@pytest.mark.parametrize("case", invite._transport_cases(), ids=lambda case: case.id)
def test_invite_transport_oracle_arm(case, tmp_path):
    target = core.python_target(sys.executable, Path(__file__).resolve().parents[2])
    core.run_arm(case, target, tmp_path / "arm")


@pytest.mark.parametrize("case", [case for case in lease_transport.cases()
                                 if case.id.startswith("operator-container")], ids=lambda case: case.id)
def test_lease_transport_oracle_arm(case, tmp_path):
    target = core.python_target(sys.executable, Path(__file__).resolve().parents[2])
    core.run_arm(case, target, tmp_path / "arm")


@pytest.mark.parametrize("case", [case for case in pair.cases()
                                 if case.id in ("https-verified", "https-untrusted")], ids=lambda case: case.id)
def test_pair_https_oracle_arm(case, tmp_path):
    target = core.python_target(sys.executable, Path(__file__).resolve().parents[2])
    obs = core.run_arm(case, target, tmp_path / "arm")
    assert obs["exit"] == (0 if case.id == "https-verified" else 4)
    assert bool(obs.get("requests")) == (case.id == "https-verified")
