import math
import time

import torch

from pseudolife_memory.memory.miras.protocols import RetentionPolicy
from pseudolife_memory.memory.titans_memory import MemoryEntry


def _entry(reinforcements=0, access_count=0, source="user", ts=None):
    return MemoryEntry(
        text="x", embedding=torch.zeros(4), source=source,
        access_count=access_count, reinforcements=reinforcements,
        timestamp=ts if ts is not None else time.time())


def _policy(retention_boost=0.0):
    # constant base score isolates the boost term
    return RetentionPolicy(
        weight_decay=0.0, decay_factor_on_contradiction=0.3,
        eviction_score=lambda e, now: 0.0, name="t",
        retention_boost=retention_boost)


def test_memory_entry_reinforcements_default():
    assert _entry().reinforcements == 0


def test_boost_zero_ignores_reinforcements():
    now = time.time()
    p = _policy(0.0)
    assert p.source_weighted_score(_entry(reinforcements=0), now) == \
           p.source_weighted_score(_entry(reinforcements=100), now)


def test_boost_positive_protects_reinforced():
    now = time.time()
    p = _policy(2.0)
    lo = p.source_weighted_score(_entry(reinforcements=0), now)
    hi = p.source_weighted_score(_entry(reinforcements=10), now)
    assert hi > lo
    # additive AFTER the source-weight multiply: difference is exactly the term
    assert math.isclose(hi - lo, 2.0 * math.log1p(10))


def test_traces_config_retention_boost_default():
    from pseudolife_memory.utils.config import TracesConfig, MemoryConfig
    assert TracesConfig().retention_boost == 0.0
    assert MemoryConfig().traces.retention_boost == 0.0


def test_build_policy_threads_retention_boost():
    from pseudolife_memory.memory.miras.retention import build_policy
    assert build_policy("balanced", retention_boost=1.5).retention_boost == 1.5
    assert build_policy("balanced").retention_boost == 0.0


def test_build_band_threads_retention_boost():
    from pseudolife_memory.memory.miras.band import build_band
    from pseudolife_memory.utils.config import MIRASBandSpec
    spec = MIRASBandSpec(name="b", max_entries=10, retention_policy="balanced")
    band = build_band(spec, embedding_dim=8, device="cpu", retention_boost=2.0)
    assert band.retention.retention_boost == 2.0


def test_cms_bands_get_configured_retention_boost():
    from pseudolife_memory.utils.config import MemoryConfig
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    cfg = MemoryConfig()
    cfg.embedding_dim = 8
    cfg.traces.retention_boost = 3.0
    cms = ContinuumMemorySystem(cfg)
    assert cms.bands
    assert all(b.retention.retention_boost == 3.0 for b in cms.bands)


def test_evict_one_protects_reinforced_and_still_evicts():
    # Exercises the real _evict_one: with access_count=0 the balanced base score
    # is 0 for every entry (0/age + 0), so the ONLY differentiator is the
    # retention_boost term -> the unreinforced entry is the victim, the reinforced
    # one survives, and the band still drops exactly one (relative, no deadlock).
    from pseudolife_memory.memory.miras.band import build_band
    from pseudolife_memory.utils.config import MIRASBandSpec
    spec = MIRASBandSpec(name="b", max_entries=3, retention_policy="balanced")
    band = build_band(spec, embedding_dim=4, device="cpu", retention_boost=5.0)
    for i, reinf in enumerate([0, 5, 0]):
        band.store(text=f"e{i}", embedding=torch.zeros(4), source="agent_action")
        band.entries[-1].db_id = i
        band.entries[-1].reinforcements = reinf
    band.store(text="e3", embedding=torch.zeros(4), source="agent_action")  # triggers eviction
    band.entries[-1].db_id = 3
    ids = {e.db_id for e in band.entries}
    assert 1 in ids                 # the reinforced entry survived eviction
    assert len(band.entries) == 3   # still evicts its weakest (no deadlock)
