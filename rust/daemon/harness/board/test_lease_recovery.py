"""The lease-recovery checks read the scenario's own lease, not the only one."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board import lease_recovery  # noqa: E402


def _ctx():
    return SimpleNamespace(id=lambda who: f"id-{who}")


def _lease(name, holder, queue=()):
    return {"name": name, "holder": {"agent_id": holder}, "queue": [{"agent_id": a} for a in queue]}


def test_the_holder_check_finds_the_scenario_lease_among_a_real_boards_own():
    """On a copy of a real board the listing also holds its own leases: the
    single-row unpack raised (lease-* scenarios, forced run, 2026-11-07)."""
    step = lease_recovery.holder(SimpleNamespace(), "A", "BC")
    response = {"status": 200, "json": {"leases": [
        _lease("full-suite", "real-agent"), _lease("resource", "id-A", ("id-B", "id-C")),
        _lease("conductor:other", "real-agent-2")]}}
    step.check(None, _ctx(), response, None)


def test_the_holder_check_still_refuses_a_wrong_holder_or_a_missing_lease():
    step = lease_recovery.holder(SimpleNamespace(), "A", "")
    with pytest.raises(AssertionError):
        step.check(None, _ctx(), {"status": 200, "json": {"leases": [_lease("resource", "id-B")]}}, None)
    with pytest.raises(AssertionError):
        step.check(None, _ctx(), {"status": 200, "json": {"leases": [_lease("full-suite", "id-A")]}}, None)
