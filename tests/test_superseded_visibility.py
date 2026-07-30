"""The superseded-entry retrieval gate, end to end from the operator knob.

Since v0.7.3 superseded band entries are *included* in retrieval, merely
downranked (``SUPERSEDED_SCORE_MULT``), so the agent can narrate "you used
to have X, then you said Y". The escape hatch that restores the v0.7.2
hard filter is ``memory.hide_superseded``.

Before 2026-07-30 that hatch did not exist as a config field: ``cms``
read it via ``getattr`` (so only a hand-assigned attribute reached the
gate) while the console exposed an opposite-named ``memory.show_superseded``
knob that ``cms`` deliberately ignores. Flipping the console switch changed
nothing. These tests pin the three links of the chain — the declared field,
the YAML parse, and the console write — plus the *default*, so a future
change cannot quietly re-hide superseded entries.

Note ``show_superseded`` semantics are NOT revived: hard-filtering on
supersession caused the cat-category retrieval failure (cms.py) and
superseded rows are retrieval-load-bearing for LongMemEval knowledge-update
(2026-07-14 compaction design).
"""

from __future__ import annotations

import dataclasses
import time

import torch
import yaml

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.config import AppConfig, MemoryConfig, load_config
from pseudolife_memory.web import config_io

CURRENT = "the deploy script backs up the bank first"
OLD = "the deploy script skips the backup step"


def _seed(cms: ContinuumMemorySystem) -> torch.Tensor:
    """Store one superseded entry and one current entry, near-identical
    embeddings so the test isolates the gate from similarity chance."""
    torch.manual_seed(7)
    dim = cms.config.embedding_dim
    base = torch.randn(dim)
    base = base / base.norm()

    def _near() -> torch.Tensor:
        v = base + 0.01 * torch.randn(dim)
        return v / v.norm()

    cms.store(OLD, _near(), source="test")
    cms.store(CURRENT, _near(), source="test")
    for band in cms.bands:
        for entry in band.entries:
            if entry.text == OLD:
                entry.superseded_at = time.time()
                entry.superseded_by_text = CURRENT
    return base


def _cms(cfg: MemoryConfig) -> ContinuumMemorySystem:
    cfg.surprise_threshold = -1.0  # disable the store gate
    return ContinuumMemorySystem(cfg)


def _texts(result) -> set[str]:
    return {e.text for e in result.entries}


# ── The default: superseded entries stay visible (v0.7.3) ────────────────


def test_superseded_entry_surfaces_by_default() -> None:
    cms = _cms(MemoryConfig())
    q = _seed(cms)
    surfaced = _texts(cms.retrieve(q, top_k=10, min_score=0.0))
    assert CURRENT in surfaced
    assert OLD in surfaced, "v0.7.3 default must keep superseded entries retrievable"


def test_default_config_does_not_hide_superseded() -> None:
    assert MemoryConfig().hide_superseded is False


# ── The gate itself, both retrieval pools ────────────────────────────────


def test_hide_superseded_filters_the_dense_pool() -> None:
    cfg = MemoryConfig()
    cfg.hide_superseded = True
    cms = _cms(cfg)
    q = _seed(cms)
    surfaced = _texts(cms.retrieve(q, top_k=10, min_score=0.0))
    assert CURRENT in surfaced
    assert OLD not in surfaced


def test_hide_superseded_filters_the_bm25_pool() -> None:
    """BM25-only injections bypass the dense pool's filters, so the gate
    has to be applied in the sparse candidate build too."""
    cfg = MemoryConfig()
    cfg.hide_superseded = True
    cms = _cms(cfg)
    q = _seed(cms)
    surfaced = _texts(cms.retrieve(
        q, top_k=10, min_score=0.0, query_text="deploy script backup", bm25=True,
    ))
    assert OLD not in surfaced


# ── Link 1: the declared config field ────────────────────────────────────


def test_hide_superseded_is_a_declared_field() -> None:
    """A ``getattr``-only gate is unreachable from a config file — the
    field has to exist for YAML and the console to be able to set it."""
    names = {f.name for f in dataclasses.fields(MemoryConfig)}
    assert "hide_superseded" in names


def test_show_superseded_field_is_gone() -> None:
    names = {f.name for f in dataclasses.fields(MemoryConfig)}
    assert "show_superseded" not in names, (
        "the no-op field was removed in favour of memory.hide_superseded")


# ── Link 2: the YAML parse ───────────────────────────────────────────────


def test_hide_superseded_loads_from_yaml(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"memory": {"hide_superseded": True}}),
                    encoding="utf-8")
    assert load_config(path).memory.hide_superseded is True


def test_legacy_show_superseded_key_loads_without_error(tmp_path) -> None:
    """Config files written by the old console still parse; the retired
    key is simply ignored (it was already a no-op)."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"memory": {"show_superseded": True, "top_k": 5}}),
                    encoding="utf-8")
    cfg = load_config(path).memory
    assert cfg.top_k == 5
    assert cfg.hide_superseded is False
    assert not hasattr(cfg, "show_superseded")


# ── Link 3: the console knob reaches the gate ────────────────────────────


class _Stub:
    """Minimal stand-in for the service the console writes through."""

    def __init__(self, config: AppConfig, data_dir) -> None:
        self.config = config
        self.data_dir = data_dir


def test_console_knob_hides_superseded_end_to_end(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
    app_cfg = AppConfig()
    cms = _cms(app_cfg.memory)
    q = _seed(cms)
    assert OLD in _texts(cms.retrieve(q, top_k=10, min_score=0.0))

    res = config_io.write_config(_Stub(app_cfg, tmp_path),
                                 {"memory.hide_superseded": "true"})
    assert "memory.hide_superseded" in res["applied"]
    assert app_cfg.memory.hide_superseded is True
    assert OLD not in _texts(cms.retrieve(q, top_k=10, min_score=0.0)), (
        "flipping the console knob must reach the retrieval gate")


def test_retired_show_superseded_knob_is_unregistered() -> None:
    assert "memory.show_superseded" not in {k["path"] for k in config_io.KNOBS}
