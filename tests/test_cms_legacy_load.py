"""v0.5 back-compat: loading a pre-v0.5 state that carries MLP weight blocks
must restore entries and ignore the weights (bands are cosine stores now)."""

from __future__ import annotations

import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.miras.band import MIRASBand
from pseudolife_memory.memory.miras.retention import build_policy
from pseudolife_memory.utils.config import MemoryConfig


def _emb(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(384, generator=g)
    return v / v.norm()


def test_band_load_ignores_legacy_weight_block():
    b = MIRASBand(
        name="t", embedding_dim=384, retention=build_policy("balanced"),
        max_entries=100, update_interval=1, promotion_access_count=2,
        promotion_surprise=0.5, device="cpu",
    )
    legacy = {  # pre-v0.5 layout: MLP weights + optimizer + counters + entries
        "memory_state": {"net.0.weight": torch.zeros(4, 4)},
        "optimizer_state": {"name": "sgd_momentum", "opt": {}},
        "surprise_ema": 0.3,
        "update_count": 42,
        "axes": {"objective": "l2", "update_rule": "sgd_momentum"},
        "entries": [{
            "text": "kept", "embedding": torch.ones(384), "surprise_score": 0.5,
            "timestamp": 1.0, "access_count": 0, "source": "t",
        }],
    }
    b.load_state_dict(legacy)  # must not raise
    assert [e.text for e in b.entries] == ["kept"]
    assert not hasattr(b, "memory")  # no MLP resurrected


def test_cms_load_tolerates_legacy_state(tmp_path):
    cms = ContinuumMemorySystem(MemoryConfig())
    cms.bands[0].store("kept entry", _emb(7), source="t")
    cms.save(tmp_path)

    # Inject a legacy top-level key + per-band MLP weight blocks into the saved state.
    p = tmp_path / "cms_state.pt"
    st = torch.load(p, weights_only=False)
    st["chain_residual"] = True
    for bstate in st["bands"].values():
        bstate["memory_state"] = {"net.0.weight": torch.zeros(2, 2)}
        bstate["optimizer_state"] = {"name": "sgd_momentum"}
        bstate["update_count"] = 99
    torch.save(st, p)

    cms2 = ContinuumMemorySystem(MemoryConfig())
    cms2.load(tmp_path)  # must not raise on the legacy keys
    assert any(e.text == "kept entry" for b in cms2.bands for e in b.entries)
