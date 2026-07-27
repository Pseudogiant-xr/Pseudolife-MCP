"""embedding-backbone-v25 Task 3 — call-site rule pinned end to end.

Design: docs/superpowers/specs/2026-07-28-embedding-backbone-v25-design.md,
section "The one design wrinkle: asymmetry". The call-site rule: a text is
QUERY-side iff it is a retrieval probe that never gets stored; everything
persisted, or compared stored-to-stored (dedup, slot-to-slot, entity-name
comparisons, supersession probes over already-stored corpora used as
internal maintenance rather than a user-facing retrieval probe), stays
document-side.

These tests inject a recording fake ``EmbeddingPipeline`` (no real model
load — fast) via the same monkeypatch seam ``test_dim_mismatch_health.py``
uses (``pseudolife_memory.service.EmbeddingPipeline``), then drive the
public ``MemoryService`` surface and assert which encode method fired for
which text. This is the ONLY thing under test — retrieval quality,
similarity scores, etc. are irrelevant and untouched (the fake returns
correctly-dimensioned zero vectors for every call).
"""
from __future__ import annotations

import pytest
import torch


class RecordingEmbedder:
    """Stands in for ``EmbeddingPipeline``. Records every ``(method, text)``
    call and returns correctly-dimensioned zero vectors — enough for the
    CMS / cortex / world / lesson stores' cosine math to run without
    crashing, without paying for a real model load."""

    embedding_dim = 8

    def __init__(self, config=None) -> None:
        self.config = config
        self.calls: list[tuple[str, str]] = []

    def encode(self, texts, normalize: bool = True):
        if isinstance(texts, str):
            texts = [texts]
        for t in texts:
            self.calls.append(("encode", t))
        return torch.zeros((len(texts), self.embedding_dim))

    def encode_single(self, text: str, normalize: bool = True):
        self.calls.append(("encode_single", text))
        return torch.zeros(self.embedding_dim)

    def encode_query(self, text: str, normalize: bool = True):
        self.calls.append(("encode_query", text))
        return torch.zeros(self.embedding_dim)

    # -- helpers for assertions -------------------------------------------------
    def methods_for(self, text: str) -> list[str]:
        return [m for m, t in self.calls if t == text]

    def any_query_calls(self) -> bool:
        return any(m == "encode_query" for m, _ in self.calls)


@pytest.fixture
def recording_service(tmp_path, monkeypatch):
    """A file-mode MemoryService (no Postgres needed) wired to a
    RecordingEmbedder instead of the real EmbeddingPipeline."""
    from pseudolife_memory.service import MemoryService

    holder: dict[str, RecordingEmbedder] = {}

    def _make(config=None):
        e = RecordingEmbedder(config)
        holder["embedder"] = e
        return e

    monkeypatch.setattr("pseudolife_memory.service.EmbeddingPipeline", _make)
    svc = MemoryService(data_dir=tmp_path)
    svc._ensure_init()  # noqa: SLF001 — construct once; builds the fake embedder.
    return svc, holder["embedder"]


# ---------------------------------------------------------------------------
# Query-side: every retrieval probe must use encode_query.
# ---------------------------------------------------------------------------

def test_search_query_uses_encode_query(recording_service):
    svc, embedder = recording_service
    svc.store("the sky is blue", source="agent")
    embedder.calls.clear()

    svc.search("what color is the sky")

    assert embedder.methods_for("what color is the sky") == ["encode_query"]


def test_trace_query_uses_encode_query(recording_service):
    svc, embedder = recording_service
    embedder.calls.clear()

    svc.trace("what color is the sky")

    assert embedder.methods_for("what color is the sky") == ["encode_query"]


def test_search_documents_query_uses_encode_query(recording_service):
    svc, embedder = recording_service
    embedder.calls.clear()

    svc.search_documents("what color is the sky")

    assert embedder.methods_for("what color is the sky") == ["encode_query"]


def test_cortex_search_query_uses_encode_query(recording_service):
    svc, embedder = recording_service
    svc.cortex_write("Widget", "color", "blue")
    embedder.calls.clear()

    svc.cortex_search("widget color")

    assert embedder.methods_for("widget color") == ["encode_query"]


def test_world_search_query_uses_encode_query(recording_service):
    svc, embedder = recording_service
    svc.world_write("Widget", "color", "blue")
    embedder.calls.clear()

    svc.world_search("widget color")

    assert embedder.methods_for("widget color") == ["encode_query"]


def test_lesson_search_query_uses_encode_query(recording_service):
    svc, embedder = recording_service
    svc.lesson_write("deploy", "rollback", "always tag before deploying")
    embedder.calls.clear()

    svc.lesson_search("deploy rollback")

    assert embedder.methods_for("deploy rollback") == ["encode_query"]


def test_consolidation_candidates_seed_uses_encode_query(recording_service):
    svc, embedder = recording_service
    svc.store("note one about widgets", source="agent")
    svc.store("note two about widgets", source="agent")
    embedder.calls.clear()

    svc.consolidation_candidates(query="widgets")

    assert embedder.methods_for("widgets") == ["encode_query"]


def test_recall_seed_uses_encode_query(recording_service):
    """recall() composes search() internally — the seed query must still
    resolve to encode_query, not a second, separately-classified path."""
    svc, embedder = recording_service
    svc.store("the sky is blue", source="agent")
    embedder.calls.clear()

    svc.recall("sky color")

    assert "encode_query" in embedder.methods_for("sky color")


def test_supersede_fallback_probe_uses_encode_query(recording_service):
    """supersede()'s embedding-fallback retrieval over old_text is a probe
    that is never itself stored — only new_text gets stored."""
    svc, embedder = recording_service
    svc.store("the sky is blue", source="agent")
    embedder.calls.clear()

    svc.supersede("the sky is a shade of blue", "the sky is grey today")

    assert "encode_query" in embedder.methods_for("the sky is a shade of blue")
    # new_text is the thing that gets stored — document-side.
    assert "encode_query" not in embedder.methods_for("the sky is grey today")
    assert "encode_single" in embedder.methods_for("the sky is grey today")


def test_consolidate_fallback_probe_uses_encode_query(recording_service):
    svc, embedder = recording_service
    svc.store("the sky is blue", source="agent")
    embedder.calls.clear()

    svc.consolidate(["the sky is a shade of blue"], "the sky is grey today")

    assert "encode_query" in embedder.methods_for("the sky is a shade of blue")
    assert "encode_query" not in embedder.methods_for("the sky is grey today")
    assert "encode_single" in embedder.methods_for("the sky is grey today")


# ---------------------------------------------------------------------------
# Document-side: everything persisted, or compared stored-to-stored, must
# NEVER use encode_query.
# ---------------------------------------------------------------------------

def test_memory_store_never_uses_encode_query(recording_service):
    svc, embedder = recording_service
    embedder.calls.clear()

    svc.store("the sky is blue", source="agent")

    assert not embedder.any_query_calls()
    assert "encode_single" in embedder.methods_for("the sky is blue")


def test_cortex_write_claim_and_slot_never_use_encode_query(recording_service):
    svc, embedder = recording_service
    embedder.calls.clear()

    svc.cortex_write("Widget", "color", "blue")

    assert not embedder.any_query_calls()
    assert "encode_single" in embedder.methods_for("Widget color blue")
    assert "encode_single" in embedder.methods_for("Widget color")


def test_world_write_claim_never_uses_encode_query(recording_service):
    svc, embedder = recording_service
    embedder.calls.clear()

    svc.world_write("Widget", "color", "blue")

    assert not embedder.any_query_calls()


def test_lesson_write_claim_never_uses_encode_query(recording_service):
    svc, embedder = recording_service
    embedder.calls.clear()

    svc.lesson_write("deploy", "rollback", "always tag before deploying")

    assert not embedder.any_query_calls()


def test_dream_and_dedup_paths_never_use_encode_query(recording_service):
    """Internal maintenance over the already-stored corpus (slot-to-slot
    dedup backfill, dream slot resolution, dream vocab/facts hints, entity
    alias-candidate name comparisons) stays document-side on both ends —
    per the spec's explicit slot-embedding carve-out, and per the fail-safe
    default for the genuinely ambiguous dream-hint batch-text probe (see
    task report)."""
    svc, embedder = recording_service
    svc.cortex_write("Widget", "color", "blue")
    svc.store("the widget is blue and shiny", source="agent")
    embedder.calls.clear()

    svc.cortex_dedup(dry_run=True)
    svc._resolve_dream_slot("Widget", "color")  # noqa: SLF001
    svc._dream_hints(["the widget is blue and shiny"])  # noqa: SLF001
    svc._propose_dream_alias_candidates(  # noqa: SLF001
        {"Gadget": "Gadget"}, {"widget"},
    )

    assert not embedder.any_query_calls()
