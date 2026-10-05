"""Service policy for durable source-retraction warnings."""

from types import SimpleNamespace

from pseudolife_memory.service import MemoryService


class _InvalidationStorage:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def trace_invalidations_for_slots(self, slot_keys):
        self.calls.append(list(slot_keys))
        return self.events


def _service(events, *, enabled=True):
    service = MemoryService.__new__(MemoryService)
    service.config = SimpleNamespace(memory=SimpleNamespace(
        traces=SimpleNamespace(enabled=enabled)))
    service._storage = _InvalidationStorage(events)
    return service


def test_durable_annotation_batches_slots_and_counts_distinct_newer_sources():
    alpha = ("payments db", "host")
    beta = ("stack", "languages")
    service = _service({
        alpha: [
            {"source_entry_id": 7, "invalidated_at": 30.0,
             "cause": "source_superseded"},
            {"source_entry_id": 7, "invalidated_at": 31.0,
             "cause": "source_superseded"},
            {"source_entry_id": 8, "invalidated_at": 9.0,
             "cause": "source_superseded"},
            {"source_entry_id": 9, "invalidated_at": 40.0,
             "cause": "source_deleted"},
        ],
        beta: [
            {"source_entry_id": 10, "invalidated_at": 50.0,
             "cause": "source_superseded"},
        ],
    })
    first, second = {}, {}

    service._annotate_trace_invalidations([
        (first, alpha, 10.0),
        (second, beta, 60.0),
    ])

    assert service._storage.calls == [[alpha, beta]]
    assert first["re_verify"] is True
    assert "1 source memory corrected" in first["re_verify_reason"]
    assert "re_verify" not in second


def test_disabled_serving_keeps_events_known_without_querying_them():
    slot = ("payments db", "host")
    service = _service({
        slot: [{"source_entry_id": 7, "invalidated_at": 30.0,
                "cause": "source_superseded"}],
    }, enabled=False)
    row = {}

    service._annotate_trace_invalidations([(row, slot, 10.0)])

    assert row == {}
    assert service._storage.events[slot]
    assert service._storage.calls == []


def test_missing_confirmation_clock_does_not_open_a_permanent_warning():
    slot = ("legacy", "slot")
    service = _service({
        slot: [{"source_entry_id": 7, "invalidated_at": 30.0,
                "cause": "source_superseded"}],
    })
    row = {}

    service._annotate_trace_invalidations([(row, slot, None)])

    assert row == {}
