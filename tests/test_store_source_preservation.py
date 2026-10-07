"""Potential conflicts admit updates without retiring their source evidence.

An entry can contain several claims, including claims the slot extractor did
not recognize. Neither a conflicting slot nor a text-pair detector verdict
authorizes replacing that entire entry. These tests use synthetic CPU vectors
and the real detector, with a fake scorer only for the optional NLI path.
"""

from __future__ import annotations

from copy import deepcopy
import math

import pytest
import torch

from pseudolife_memory.memory import contradiction
from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.slots import extract_slots
from pseudolife_memory.utils.config import MemoryConfig, MIRASBandSpec, MIRASConfig


def _config():
    cfg = MemoryConfig()
    cfg.embedding_dim = 4
    # The synthetic pairs have surprise <= 0.95. All must need the conflict
    # admission path, even the deliberately distant slot-identity example.
    cfg.surprise_threshold = 0.99
    cfg.miras = MIRASConfig(preset="custom", bands=[
        MIRASBandSpec(name=name, max_entries=64, update_interval=10**9)
        for name in ("head", "tail")
    ])
    return cfg


def _slots(text):
    return [(s.entity, s.attribute, s.value, s.polarity)
            for s in extract_slots(text)]


def _seed(text, cosine, *, source="status", band=0, nli_scorer=None, storage=None):
    cms = ContinuumMemorySystem(_config(), nli_scorer=nli_scorer, storage=storage)
    old_vector = torch.tensor([cosine, math.sqrt(1 - cosine**2), 0.0, 0.0])
    query = torch.tensor([1.0, 0.0, 0.0, 0.0])
    # Direct seeding covers entries hydrated into either band as well as fresh
    # writes. Give the source distinctive metadata to detect collateral edits.
    cms.bands[band].store(text, old_vector, source=source, surprise=0.8)
    old = cms.bands[band].entries[-1]
    old.slots = _slots(text)
    old.tags = ["evidence"]
    old.authority = "quoted"
    old.distortion_tolerance = "constraint"
    old.episode_id = "synthetic-episode"
    old.episode_title = "Synthetic source evidence"
    old.db_id = 7 if storage is not None else None
    return cms, old, query


def _evidence(entry):
    # Detector cue caches may warm. Persisted evidence and retention weight may
    # not change merely because another note was considered or accepted.
    fields = ("text", "source", "slots", "tags", "authority",
              "distortion_tolerance", "episode_id", "episode_title",
              "surprise_score", "superseded_at", "superseded_by_text",
              "timestamp", "access_count", "bank")
    return deepcopy({**{key: getattr(entry, key) for key in fields},
                     "embedding": entry.embedding.tolist()})


CASES = [
    pytest.param(
        "The gateway is ready. Keep the retry recipe.",
        "The gateway is not ready.", 0.75, id="negation-partial-note"),
    pytest.param(
        "The travel destination is Paris; train tickets are refundable.",
        "The travel destination is Berlin.", 0.9, id="affirmative-partial-note"),
    pytest.param(
        "Project Orion has a probe called Relay.",
        "Project Orion abandoned the unrelated theme experiment.",
        0.2, id="state-transition-unrelated-shared-name"),
    pytest.param(
        "the relay port is 4001. Keep the rollback bundle until tomorrow.",
        "the relay port is 4002", 0.05, id="slot-partial-note"),
]


@pytest.mark.parametrize("old_text,new_text,cosine", CASES)
@pytest.mark.parametrize("new_source", ["status", "project-notes"])
def test_real_conflict_admits_update_without_changing_source_evidence(
    old_text, new_text, cosine, new_source,
):
    cms, old, query = _seed(old_text, cosine)
    before = _evidence(old)
    assert contradiction.detect_contradictions(
        new_text, query, [old], new_slots=_slots(new_text)) == [old]

    stored, surprise = cms.store(new_text, query, source=new_source)

    assert stored and surprise < cms.config.surprise_threshold
    assert _evidence(old) == before
    assert any(e.text == new_text for b in cms.bands for e in b.entries)


@pytest.mark.parametrize("source", ["digest", "project-notes"])
@pytest.mark.parametrize("band", [0, 1])
def test_source_evidence_is_preserved_in_every_band(source, band):
    cms, old, query = _seed("The gateway is ready.", 0.75,
                            source=source, band=band)
    before = _evidence(old)
    assert cms.store("The gateway is not ready.", query, source="correction")[0]
    assert _evidence(old) == before


def test_nli_conflict_is_admission_only():
    class Scorer:
        pairs = []

        def is_available(self):
            return True

        def flagged_indices(self, pairs):
            self.pairs.extend(pairs)
            return [0]

    scorer = Scorer()
    old_text, new_text = "alpha sample", "beta sample"
    cms, old, query = _seed(old_text, 0.4, nli_scorer=scorer)
    before = _evidence(old)
    assert contradiction.detect_contradictions(new_text, query, [old]) == []

    stored, surprise = cms.store(new_text, query)

    assert stored and surprise < cms.config.surprise_threshold
    assert scorer.pairs == [(old_text, new_text)]
    assert _evidence(old) == before


def test_duplicate_gate_preserves_evidence_and_source_name_does_not_bypass_it():
    cms, old, query = _seed("steady gateway settings", 1.0)
    before = _evidence(old)
    stored, surprise = cms.store(old.text, query, source="correction")
    assert not stored and surprise == 0.0
    assert cms.total_memories == 1
    assert _evidence(old) == before


def test_explicit_bypass_admits_low_surprise_text_without_retiring_evidence():
    cms, old, query = _seed("steady gateway settings", 1.0)
    before = _evidence(old)
    assert cms.store(old.text, query, bypass_surprise_gate=True) == (True, 0.0)
    assert cms.total_memories == 2
    assert _evidence(old) == before


@pytest.mark.parametrize("bypass", [False, True])
def test_meta_filter_still_rejects_without_changing_existing_evidence(bypass):
    cms, old, query = _seed("The gateway is ready.", 0.8)
    before = _evidence(old)
    result = cms.store("I don't have any gateway information stored.", query,
                       source="agent", bypass_surprise_gate=bypass)
    assert result == (False, 0.0)
    assert cms.total_memories == 1
    assert _evidence(old) == before


def test_failed_band_write_does_not_retire_or_decay_existing_evidence(monkeypatch):
    cms, old, query = _seed("The gateway is ready.", 0.8)
    before = _evidence(old)

    def fail_store(*args, **kwargs):
        raise RuntimeError("synthetic allocation failure")

    monkeypatch.setattr(cms.bands[0], "store", fail_store)
    with pytest.raises(RuntimeError, match="synthetic allocation failure"):
        cms.store("The gateway is not ready.", query)
    assert _evidence(old) == before
    assert cms.total_memories == 1


def test_failed_persistence_does_not_write_supersession_to_existing_rows():
    class Storage:
        updates = []

        def update_entry(self, entry_id, **fields):
            self.updates.append((entry_id, fields))

        def insert_entry(self, row):
            raise RuntimeError("synthetic insertion failure")

    storage = Storage()
    cms, old, query = _seed("The gateway is ready.", 0.8, storage=storage)
    before = _evidence(old)
    with pytest.raises(RuntimeError, match="synthetic insertion failure"):
        cms.store("The gateway is not ready.", query)
    assert storage.updates == []
    assert _evidence(old) == before


def test_file_roundtrip_preserves_sources_and_existing_explicit_history(tmp_path):
    cms, old, query = _seed("The gateway is ready.", 0.8)
    before = _evidence(old)
    cms.bands[1].store("Earlier explicit correction target", query, source="notes")
    explicit = cms.bands[1].entries[-1]
    explicit.superseded_at = 123.0
    explicit.superseded_by_text = "An explicit replacement"
    history = _evidence(explicit)

    assert cms.store("The gateway is not ready.", query)[0]
    cms.save(tmp_path)
    restored = ContinuumMemorySystem(_config())
    restored.load(tmp_path)
    entries = {e.text: e for b in restored.bands for e in b.entries}

    assert _evidence(entries[old.text]) == before
    assert _evidence(entries[explicit.text]) == history
    assert "The gateway is not ready." in entries
