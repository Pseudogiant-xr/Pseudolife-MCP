"""The startup warmup search is not a read.

``MemoryService.warmup`` runs ``search("warmup probe", top_k=1)`` on every
daemon start so the first real call is warm, and ``cms.retrieve`` bumps
``access_count`` on every entry it serves. So the same entry, whichever
one best matches "warmup probe", gained a spurious access on each restart
(2026-09-23 fresh-eyes review). ``access_count`` is a serve counter that
feeds band promotion and the Console's read audit, so a synthetic probe
must not move it. Real searches still do.
"""
from __future__ import annotations

from pseudolife_memory.service import MemoryService


def _only_entry(svc: MemoryService):
    entries = [e for band in svc._cms.bands for e in band.entries]  # noqa: SLF001
    assert len(entries) == 1
    return entries[0]


def test_warmup_does_not_count_as_an_access(
        pristine_service: MemoryService) -> None:
    svc = pristine_service
    svc.store("warmup probe", source="notes")
    entry = _only_entry(svc)
    before = entry.access_count

    svc.warmup()
    assert entry.access_count == before

    # The same query from a caller still counts, so the probe above really
    # was served the entry and only its accrual was skipped.
    out = svc.search("warmup probe", top_k=1)
    assert [e["text"] for e in out["entries"]] == ["warmup probe"]
    assert entry.access_count == before + 1


def test_retrieve_can_serve_without_accruing(
        pristine_service: MemoryService) -> None:
    svc = pristine_service
    svc.store("warmup probe", source="notes")
    entry = _only_entry(svc)
    before = entry.access_count
    q = svc._embedder.encode_query("warmup probe")  # noqa: SLF001
    res = svc._cms.retrieve(q, top_k=1, query_text="warmup probe",  # noqa: SLF001
                            count_access=False)
    assert [e.text for e in res.entries] == ["warmup probe"]
    assert entry.access_count == before
