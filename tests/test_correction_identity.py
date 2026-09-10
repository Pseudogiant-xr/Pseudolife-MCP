"""Explicit corrections resolve exact targets before changing any evidence.

Synthetic CPU vectors keep identity tests independent of model behavior.
PostgreSQL cases use the suite's per-process isolated database.
"""

from __future__ import annotations

from copy import deepcopy
import math

import pytest
import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult
from pseudolife_memory.service import MemoryService
from pseudolife_memory.storage.sync import entry_to_row, hydrate_cms
from pseudolife_memory.utils.config import MemoryConfig, MIRASBandSpec, MIRASConfig
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


NEW_TEXT = "The revised gateway deployment note."


class Embedder:
    def __init__(self, embedding_dim):
        self.calls = []
        self.vector = torch.zeros(embedding_dim)
        self.vector[0] = 1.0

    def encode_single(self, text):
        self.calls.append(("document", text))
        return self.vector.clone()

    def encode_query(self, text):
        self.calls.append(("query", text))
        return self.vector.clone()


class Storage:
    def __init__(self):
        self.rows = {}
        self.updates = []

    def insert_entry(self, row):
        entry_id = max(self.rows, default=0) + 1
        self.rows[entry_id] = deepcopy(row)
        return entry_id

    def update_entry(self, entry_id, **fields):
        self.updates.append((entry_id, fields))
        self.rows[entry_id].update(fields)

    def existing_entry_ids(self, ids):
        return set(ids) & self.rows.keys()


def _config():
    config = MemoryConfig()
    config.embedding_dim = 4
    config.surprise_threshold = 0.99
    config.bm25.enabled = False
    config.traces.enabled = False
    config.miras = MIRASConfig(preset="custom", bands=[
        MIRASBandSpec(name=name, max_entries=64, update_interval=10**9)
        for name in ("head", "tail")
    ])
    return config


def _service(tmp_path, monkeypatch, storage=None, *, embedding_dim=4):
    svc = MemoryService(data_dir=tmp_path)
    svc.config.memory = _config()
    svc.config.memory.embedding_dim = embedding_dim
    svc._embedder = Embedder(embedding_dim)
    svc._cms = ContinuumMemorySystem(svc.config.memory, storage=storage)
    svc._storage = storage
    monkeypatch.setattr(svc, "_ensure_init", lambda: None)
    return svc


@pytest.fixture
def svc(tmp_path, monkeypatch):
    return _service(tmp_path, monkeypatch, Storage())


@pytest.fixture
def file_svc(tmp_path, monkeypatch):
    return _service(tmp_path, monkeypatch)


def _seed(svc, text="Gateway deployment evidence", *, source="selected",
          episode="selected-episode", band=0, cosine=1.0):
    vector = torch.zeros(svc.config.memory.embedding_dim)
    vector[0] = cosine
    vector[1] = math.sqrt(1 - cosine**2)
    target_band = svc._cms.bands[band]
    target_band.store(text, vector, source=source, surprise=0.8)
    entry = target_band.entries[-1]
    entry.episode_id = episode
    entry.episode_title = episode
    entry.tags = [source]
    if svc._storage is not None:
        entry.db_id = svc._storage.insert_entry(entry_to_row(entry))
    return entry


def _entries(svc):
    return [e for band in svc._cms.bands for e in band.entries]


def _state(svc):
    fields = ("text", "db_id", "source", "episode_id", "episode_title", "tags",
              "superseded_at", "superseded_by_text", "surprise_score",
              "authority", "distortion_tolerance", "access_count")
    return [deepcopy({key: getattr(e, key) for key in fields}) for e in _entries(svc)]


def _call(svc, operation, *, ids=None, texts=None, **kwargs):
    if operation == "supersede":
        if ids is not None:
            kwargs["entry_id"] = ids[0]
        if texts is not None:
            kwargs["old_text"] = texts[0]
    else:
        if ids is not None:
            kwargs["entry_ids"] = ids
        if texts is not None:
            kwargs["replaces"] = texts
    return getattr(svc, operation)(new_text=NEW_TEXT, **kwargs)


def _assert_refused(svc, before, result, reason):
    assert result["superseded_count"] == 0
    assert result["superseded_texts"] == []
    assert result["superseded_ids"] == []
    assert result["new_memory_stored"] is False
    assert result["reason"] == reason
    assert result["error"]
    assert _state(svc) == before
    assert svc._embedder.calls == []
    if isinstance(svc._storage, Storage):
        assert svc._storage.updates == []


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
def test_legacy_unique_exact_text_control(file_svc, operation):
    old = _seed(file_svc)
    result = _call(file_svc, operation, texts=[old.text])
    assert result["superseded_count"] == 1
    assert result["new_memory_stored"] is True
    assert old.superseded_by_text == NEW_TEXT


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
def test_id_selects_only_one_duplicate_across_source_episode_and_band(svc, operation):
    old = _seed(svc)
    sibling = _seed(svc, source="other", episode="other-episode", band=1)
    old.authority = "quoted"
    sibling.authority = "directive"
    sibling.distortion_tolerance = "constraint"
    sibling_before = deepcopy(_state(svc)[1])
    result = _call(svc, operation, ids=[old.db_id])
    assert result["superseded_ids"] == [old.db_id]
    assert result["superseded_count"] == 1
    assert old.superseded_by_text == NEW_TEXT
    assert _state(svc)[-1] == sibling_before  # new entry is appended in head
    replacement = next(e for e in _entries(svc) if e.text == NEW_TEXT)
    assert replacement.authority == "quoted"
    assert replacement.distortion_tolerance is None
    assert svc._storage.updates == [(old.db_id, {
        "superseded_at": old.superseded_at, "superseded_by_text": NEW_TEXT,
    })]


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
@pytest.mark.parametrize("retired_twin", [False, True])
def test_legacy_duplicate_text_is_ambiguous_including_retired_twins(
        svc, operation, retired_twin):
    old = _seed(svc)
    sibling = _seed(svc, source="other", episode="other-episode", band=1)
    if retired_twin:
        sibling.superseded_at = 123.0
        sibling.superseded_by_text = "Earlier replacement"
    before = _state(svc)
    result = _call(svc, operation, texts=[old.text])
    _assert_refused(svc, before, result, "ambiguous_target")
    candidates = result["target_errors"][0]["candidates"]
    assert {c["id"] for c in candidates} == {old.db_id, sibling.db_id}
    assert {c["episode_id"] for c in candidates} == {"selected-episode", "other-episode"}


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
def test_missing_exact_text_never_reselects_similar_entry(file_svc, operation):
    _seed(file_svc, "Gateway deployment discussion", cosine=0.4)
    before = _state(file_svc)
    result = _call(file_svc, operation, texts=["Gateway deployment target absent"])
    _assert_refused(file_svc, before, result, "target_not_found")


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
def test_missing_id_never_reselects_surviving_text_twin(svc, operation):
    _seed(svc)
    before = _state(svc)
    result = _call(svc, operation, ids=[999])
    _assert_refused(svc, before, result, "target_not_found")


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
@pytest.mark.parametrize("mode", ["ids", "texts"])
def test_already_superseded_target_is_not_redirected(svc, operation, mode):
    old = _seed(svc)
    old.superseded_at = 123.0
    old.superseded_by_text = "Earlier correction"
    _seed(svc, "Gateway deployment alternative", band=1, cosine=0.4)
    before = _state(svc)
    selectors = {mode: [old.db_id if mode == "ids" else old.text]}
    result = _call(svc, operation, **selectors)
    _assert_refused(svc, before, result, "target_superseded")


@pytest.mark.parametrize("mode", ["ids", "texts"])
def test_consolidate_resolves_all_targets_before_any_mutation(svc, mode):
    first = _seed(svc, "First deployment evidence")
    second = _seed(svc, "Second deployment evidence", band=1)
    before = _state(svc)
    selectors = {mode: [first.db_id, second.db_id, 999] if mode == "ids"
                 else [first.text, second.text, "Absent deployment evidence"]}
    result = _call(svc, "consolidate", **selectors)
    _assert_refused(svc, before, result, "target_not_found")


@pytest.mark.parametrize("mode", ["ids", "texts"])
def test_consolidate_repeated_selector_changes_target_once(svc, mode):
    old = _seed(svc)
    sibling = _seed(svc, "A related gateway deployment", band=1, cosine=0.4)
    value = old.db_id if mode == "ids" else old.text
    result = _call(svc, "consolidate", **{mode: [value, value]})
    assert result["superseded_ids"] == [old.db_id]
    assert result["superseded_count"] == 1
    assert sibling.superseded_at is None
    assert len(svc._storage.updates) == 1


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
@pytest.mark.parametrize("entry_id", [True, False, 0, -1, "1", 1.0])
def test_ids_are_strict_positive_integers(svc, operation, entry_id):
    _seed(svc)
    before = _state(svc)
    result = _call(svc, operation, ids=[entry_id])
    _assert_refused(svc, before, result, "invalid_selector")


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
def test_mixed_selector_modes_are_rejected(svc, operation):
    old = _seed(svc)
    before = _state(svc)
    result = _call(svc, operation, ids=[old.db_id], texts=[old.text])
    _assert_refused(svc, before, result, "invalid_selector")


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
def test_file_mode_never_accepts_transient_sequence_as_id(file_svc, operation):
    old = _seed(file_svc)
    before = _state(file_svc)
    result = _call(file_svc, operation, ids=[old.seq])
    _assert_refused(file_svc, before, result, "id_unavailable")


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
@pytest.mark.parametrize("mode", ["ids", "texts"])
def test_phantom_row_is_not_treated_as_a_durable_target(svc, operation, mode):
    old = _seed(svc)
    del svc._storage.rows[old.db_id]
    before = _state(svc)
    result = _call(svc, operation, **{mode: [old.db_id if mode == "ids" else old.text]})
    _assert_refused(svc, before, result, "target_unavailable")


def test_duplicate_resident_id_mapping_is_rejected(svc):
    old = _seed(svc)
    sibling = _seed(svc, "Other target", band=1)
    sibling.db_id = old.db_id
    before = _state(svc)
    result = _call(svc, "supersede", ids=[old.db_id])
    _assert_refused(svc, before, result, "ambiguous_target")


def test_row_validation_failure_does_not_mutate_or_embed(svc, monkeypatch):
    old = _seed(svc)
    before = _state(svc)

    def fail_read(ids):
        raise RuntimeError("synthetic unavailable database")

    monkeypatch.setattr(svc._storage, "existing_entry_ids", fail_read)
    result = _call(svc, "supersede", ids=[old.db_id])
    _assert_refused(svc, before, result, "target_unavailable")


def test_supersede_reports_derivations_only_from_selected_ids(svc, monkeypatch):
    old = _seed(svc)
    _seed(svc, source="other", episode="other", band=1)
    requested = []

    def derived(ids):
        requested.extend(ids)
        return [{"entity": "gateway", "entry_ids": ids}]

    monkeypatch.setattr(svc, "_derived_from_entries_locked", derived)
    result = _call(svc, "supersede", ids=[old.db_id])
    assert requested == [old.db_id]
    assert result["derived_flagged"] == [{"entity": "gateway", "entry_ids": [old.db_id]}]


def test_file_mode_unique_text_and_history_survive_reload(file_svc, tmp_path):
    old = _seed(file_svc)
    file_svc._cms.save(tmp_path / "saved")
    restored = ContinuumMemorySystem(_config())
    restored.load(tmp_path / "saved")
    file_svc._cms = restored
    result = _call(file_svc, "supersede", texts=[old.text])
    assert result["superseded_ids"] == []
    assert result["new_memory_stored"] is True
    restored.save(tmp_path / "corrected")
    reloaded = ContinuumMemorySystem(_config())
    reloaded.load(tmp_path / "corrected")
    entry = next(e for b in reloaded.bands for e in b.entries if e.text == old.text)
    assert entry.db_id is None
    assert entry.superseded_by_text == NEW_TEXT


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
def test_pg_selected_id_survives_hydration_and_correction_restart(
        pg_conn, pg_url, tmp_path, monkeypatch, operation):
    from pseudolife_memory.storage.postgres import PostgresStorage

    with_storage = PostgresStorage(pg_url)
    try:
        # The existing test schema uses 1024-dimensional stored vectors.
        svc = _service(tmp_path, monkeypatch, with_storage, embedding_dim=1024)
        selected = _seed(svc)
        sibling = _seed(svc, source="other", episode="other-episode", band=1)
        svc._cms = ContinuumMemorySystem(svc.config.memory, storage=with_storage)
        assert hydrate_cms(svc._cms, with_storage) == 2
        result = _call(svc, operation, ids=[selected.db_id])
        assert result["superseded_ids"] == [selected.db_id]
        restored = ContinuumMemorySystem(svc.config.memory, storage=with_storage)
        hydrate_cms(restored, with_storage)
        rows = {e.db_id: e for b in restored.bands for e in b.entries}
        assert rows[selected.db_id].superseded_by_text == NEW_TEXT
        assert rows[sibling.db_id].superseded_at is None
    finally:
        with_storage.close()


@pytest.mark.parametrize("mode", ["query", "episode"])
def test_candidates_exclude_retired_entries_before_caps_and_text_dedup(svc, mode):
    retired = [_seed(svc, "Gateway deployment evidence")]
    retired.extend(_seed(svc, f"Retired gateway evidence {i}") for i in range(24))
    for entry in retired:
        entry.superseded_at = 123.0
        entry.superseded_by_text = "Earlier replacement"
    # The retired text twin precedes the active twin. High-cosine retired
    # entries also fill the dense candidate cap unless eligibility runs first.
    first = _seed(svc, "Gateway deployment evidence", cosine=0.4)
    second = _seed(svc, "Related gateway evidence", cosine=0.4)
    assert svc.config.memory.hide_superseded is False

    result = svc.consolidation_candidates(
        query="gateway deployment" if mode == "query" else None,
        episode="selected-episode", sources=["selected"], top_k=2,
    )

    members = [m for cluster in result["clusters"] for m in cluster["members"]]
    assert {m["id"] for m in members} == {first.db_id, second.db_id}
    assert all(not m["superseded"] for m in members)
    assert svc.config.memory.hide_superseded is False
    corrected = svc.consolidate(
        entry_ids=[m["id"] for m in members], new_text=NEW_TEXT,
    )
    assert corrected["superseded_count"] == 2
    assert corrected["new_memory_stored"] is True
    assert all(e.superseded_at == 123.0 for e in retired)


def test_retrieval_hide_override_preserves_default_config_behavior(file_svc):
    retired = _seed(file_svc, "Older gateway evidence")
    retired.superseded_at = 123.0
    retired.superseded_by_text = "Earlier replacement"
    active = _seed(file_svc, "Current gateway evidence", cosine=0.4)
    vector = file_svc._embedder.encode_query("gateway")

    def retrieve(**kwargs):
        return file_svc._cms.retrieve(vector, top_k=4, min_score=0.0, **kwargs).entries

    assert any(e is retired for e in retrieve())
    assert retrieve(hide_superseded=True) == [active]
    assert file_svc.config.memory.hide_superseded is False
    file_svc.config.memory.hide_superseded = True
    assert retrieve() == [active]
    assert any(e is retired for e in retrieve(hide_superseded=False))
    assert file_svc.config.memory.hide_superseded is True


def test_retrieval_hide_override_filters_before_bm25_candidate_cap(file_svc, monkeypatch):
    from pseudolife_memory.memory import cms as cms_module

    retired = _seed(file_svc, "Gatekeeper retired deployment token")
    retired.superseded_at = 123.0
    active = _seed(file_svc, "Gatekeeper current deployment token", cosine=0.4)
    file_svc.config.memory.bm25.top_n = 1
    seen = []
    original_index = cms_module.BM25Index

    def recording_index(entries, **kwargs):
        seen.extend(entries)
        return original_index(entries, **kwargs)

    monkeypatch.setattr(cms_module, "BM25Index", recording_index)
    result = file_svc._cms.retrieve(
        file_svc._embedder.encode_query("Gatekeeper"), query_text="Gatekeeper",
        top_k=1, min_score=0.0, bm25=True, hide_superseded=True,
    )
    assert seen == [active]
    assert result.entries == [active]


@pytest.mark.parametrize("mode", ["query", "episode"])
@pytest.mark.parametrize("retired_twin", [False, True])
def test_file_candidates_omit_text_ambiguous_outside_selected_scope(
        file_svc, mode, retired_twin):
    twin = _seed(file_svc, "Duplicate gateway evidence", episode="other-episode")
    if retired_twin:
        twin.superseded_at = 123.0
        twin.superseded_by_text = "Earlier replacement"
    _seed(file_svc, "Duplicate gateway evidence")
    first = _seed(file_svc, "First unique gateway evidence")
    second = _seed(file_svc, "Second unique gateway evidence")

    result = file_svc.consolidation_candidates(
        query="gateway" if mode == "query" else None,
        episode="selected-episode", top_k=10 if mode == "query" else 2,
    )
    members = [m for cluster in result["clusters"] for m in cluster["members"]]
    assert {m["text"] for m in members} == {first.text, second.text}
    corrected = file_svc.consolidate(replaces=[m["text"] for m in members], new_text=NEW_TEXT)
    assert corrected["superseded_count"] == 2
    assert corrected["new_memory_stored"] is True


@pytest.mark.parametrize("with_residents", [False, True])
def test_query_candidates_never_offer_reference_documents(file_svc, with_residents):
    references = [MemoryEntry(
        text=f"Reference gateway document {i}",
        embedding=torch.tensor([1.0, 0.0, 0.0, 0.0]),
        surprise_score=0.8, source="reference",
    ) for i in range(2)]

    class Reference:
        def retrieve(self, embedding, top_k):
            return RetrievalResult(entries=references, scores=[1.0, 1.0], surprises=[0.8, 0.8])

    file_svc._cms.reference = Reference()
    residents = ([_seed(file_svc, f"Resident gateway evidence {i}") for i in range(2)]
                 if with_residents else [])
    result = file_svc.consolidation_candidates(query="gateway", top_k=4)
    members = [m for cluster in result["clusters"] for m in cluster["members"]]
    assert {m["text"] for m in members} == {e.text for e in residents}
    if residents:
        corrected = file_svc.consolidate(replaces=[m["text"] for m in members], new_text=NEW_TEXT)
        assert corrected["superseded_count"] == 2


@pytest.mark.parametrize("mode", ["query", "episode"])
def test_pg_candidates_omit_phantom_and_duplicate_ids_without_losing_valid_targets(svc, mode):
    phantom = _seed(svc, "Phantom gateway evidence")
    del svc._storage.rows[phantom.db_id]
    # Ensure the fake allocator does not reuse the phantom's ID.
    svc._storage.rows[10] = {}
    ambiguous = _seed(svc, "Ambiguous gateway evidence")
    twin = _seed(svc, "Other ambiguous evidence", episode="other-episode")
    twin.db_id = ambiguous.db_id
    first = _seed(svc, "First durable gateway evidence")
    second = _seed(svc, "Second durable gateway evidence")

    result = svc.consolidation_candidates(
        query="gateway" if mode == "query" else None,
        episode="selected-episode", top_k=10 if mode == "query" else 2,
    )
    members = [m for cluster in result["clusters"] for m in cluster["members"]]
    assert {m["id"] for m in members} == {first.db_id, second.db_id}
    corrected = svc.consolidate(entry_ids=[m["id"] for m in members], new_text=NEW_TEXT)
    assert corrected["superseded_count"] == 2
    assert corrected["new_memory_stored"] is True
