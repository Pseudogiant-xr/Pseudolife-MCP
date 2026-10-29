"""The Rust daemon embeds Python's Console knob table and its vectors
(``goldens/config-knobs.json``); a change to ``web/config_io.py`` that the
file does not carry fails here instead of serving a stale table."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gen_config_knobs  # noqa: E402


def test_config_knob_golden_is_current():
    current = gen_config_knobs.TARGET.read_text(encoding="utf-8")
    assert current == gen_config_knobs.render(), (
        "goldens/config-knobs.json is stale: run python rust/daemon/harness/gen_config_knobs.py")
