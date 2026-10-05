"""Eligibility must precede the dense candidate cap, even without lexical hits."""
import math

import pytest
import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.titans_memory import MemoryEntry
from pseudolife_memory.utils.config import MemoryConfig, MIRASConfig


def _vector(cosine):
    return torch.tensor([cosine, math.sqrt(1 - cosine * cosine), 0.0, 0.0])


def _bank(preset='flat'):
    cfg = MemoryConfig(embedding_dim=4, miras=MIRASConfig(preset=preset))
    cfg.bm25.enabled = True
    cfg.recency_boost_enabled = False
    cms = ContinuumMemorySystem(cfg)
    band = cms.bands[0]
    # Direct fixture insertion isolates retrieval from automatic contradiction
    # detection. This is the same mutation shape as hydration/import.
    band.entries = [
        MemoryEntry(text=f'unrelated record {i}', embedding=_vector(0.95 - i * 0.04),
                    source='outside', episode_id='outside', tags=['outside'],
                    timestamp=100.0, bank=band.name)
        for i in range(11)
    ]
    target = MemoryEntry(text='eligible evidence', embedding=_vector(0.4),
                         source='status', episode_id='episode-a', tags=['reliability'],
                         timestamp=100.0, bank=band.name, last_logical_turn=10)
    band.entries.append(target)
    band._dirty = True
    return cms, target


def _search(cms, **filters):
    return cms.retrieve(torch.tensor([1.0, 0.0, 0.0, 0.0]), top_k=4,
                        query_text='unmatchedquerytoken', **filters)


@pytest.mark.parametrize('filters', [
    {'sources': ['status']},
    {'episodes': ['episode-a']},
    {'tags': [' Reliability ']},
    {'sources': ['status'], 'episodes': ['episode-a'], 'tags': ['reliability']},
    {'min_logical_turn': 10},
])
def test_eligible_evidence_beyond_global_top_k_is_retrieved(filters):
    cms, target = _bank()
    result = _search(cms, **filters)
    assert result.entries == [target]
    assert result.scores == pytest.approx([0.4])
    assert target.access_count == 1
    assert all(e.access_count == 0 for e in cms.bands[0].entries[:-1])


def test_scope_filters_and_together_before_candidate_cut():
    cms, target = _bank()
    for i, entry in enumerate(cms.bands[0].entries[:-1]):
        entry.source = 'status'
        entry.episode_id = 'episode-a' if i % 2 else 'outside'
        entry.tags = ['reliability'] if i % 2 == 0 else ['outside']
    result = _search(cms, sources=['status'], episodes=['episode-a'], tags=['reliability'])
    assert result.entries == [target]


def test_empty_eligible_pool_is_empty():
    cms, _ = _bank()
    assert _search(cms, sources=['missing']).entries == []


def test_filtered_selection_keeps_the_best_k_eligible_entries():
    cms, _ = _bank()
    entries = cms.bands[0].entries
    for entry in entries[1:8]:
        entry.source = 'status'
    result = _search(cms, sources=['status'])
    assert result.entries == entries[1:5]


def test_eligibility_reads_live_metadata_after_the_matrix_is_warm():
    cms, target = _bank()
    _search(cms)
    target.source = 'changed'
    first = _search(cms, sources=['changed'])
    assert first.entries == [target]
    target.source = 'outside'
    assert _search(cms, sources=['changed']).entries == []


def test_hidden_superseded_candidates_do_not_consume_the_dense_cap():
    cms, target = _bank()
    cms.config.hide_superseded = True
    for entry in cms.bands[0].entries[:-1]:
        entry.superseded_at = 200.0
    assert _search(cms).entries == [target]


def test_empty_filter_lists_preserve_unfiltered_ranking():
    cms, _ = _bank()
    plain = _search(cms)
    empty = _search(cms, sources=[], episodes=[], tags=[])
    assert plain.entries == empty.entries
    assert plain.scores == empty.scores


@pytest.mark.parametrize('filters', [{'sources': ['missing']}, {'tags': ['   ']}])
def test_scoped_trace_distinguishes_no_eligible_entries_from_an_empty_band(filters):
    cms, _ = _bank()
    result, trace = cms.retrieve_with_trace(
        torch.tensor([1.0, 0.0, 0.0, 0.0]), top_k=4,
        query_text='unmatchedquerytoken', **filters,
    )
    assert result.entries == []
    tier = trace['tiers'][0]
    assert tier['entry_count'] == 12
    assert tier['eligible_count'] == 0
    assert tier['excluded_count'] == 12
    assert tier['candidates'] == []


def test_continuum_scopes_use_band_containment_and_or_within_each_filter():
    cms, first = _bank('continuum')
    second_band = cms.bands[1]
    second = MemoryEntry(text='second eligible evidence', embedding=_vector(0.35),
                         source='history', episode_id='episode-a', tags=['audit'],
                         timestamp=100.0, bank=cms.bands[0].name)
    partial = MemoryEntry(text='wrong episode', embedding=_vector(0.9),
                          source='history', episode_id='outside', tags=['audit'],
                          timestamp=100.0, bank=second_band.name)
    second_band.entries.extend([second, partial])
    second_band._dirty = True
    filters = dict(sources=['status', 'history'], episodes=['episode-a'],
                   tags=['reliability', 'audit'])
    assert _search(cms, **filters).entries == [first, second]
    assert _search(cms, bands=[second_band.name], **filters).entries == [second]
