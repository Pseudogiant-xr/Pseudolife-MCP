"""Controls for the lease row's bounded clock comparison."""

import base64

import pytest

from . import compare
from .rows.lease import T0  # registers the row's named rules


def observation(hour, field, age):
    text = f'queued behind codex for {age}, purpose "peer work"\n'
    out = {"exit": 0, "stdout": "", "stderr": "", "files": {},
           "window": [T0 + hour * 3600, T0 + hour * 3600 + 1], "utc_offset": 0}
    out[field] = base64.b64encode(text.encode()).decode()
    return out


@pytest.mark.parametrize("field", ["stdout", "stderr"])
def test_valid_holder_age_compares_in_each_arms_own_window(field):
    before = observation(1, field, "1h00m")
    later = observation(2, field, "2h00m")
    assert not compare.diff(before, later, ("lease-seeded-clock",))


@pytest.mark.parametrize("field", ["stdout", "stderr"])
def test_holder_age_outside_its_own_window_remains_a_difference(field):
    correct = observation(2, field, "2h00m")
    stale = observation(2, field, "1h00m")
    assert compare.diff(correct, stale, ("lease-seeded-clock",))
