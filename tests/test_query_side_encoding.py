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

import ast
from pathlib import Path

import pytest
import torch

_SERVICE_PY = Path(__file__).resolve().parent.parent / "pseudolife_memory" / "service.py"

# Every (enclosing function, embedder method) pair behind a ``self._embedder.<method>(...)``
# call in pseudolife_memory/service.py, per the call-site rule in this file's module
# docstring. Built from the AST walk below (see task-3-report.md for the raw dump) and
# verified against the spec review's counts: 23 raw call sites (9 encode_query, 14
# encode_single/encode) collapse to 21 unique pairs here because cortex_write and
# _propose_dream_alias_candidates each call the same method twice in the same function.
#
# Task 4 (set-valued slots) added ("set_add", "encode_single") -- set_add embeds the
# member text through the same document-side composition cortex_write uses, so it
# joins cortex_write's classification rather than getting its own rule.
#
# A future call site -- e.g. a new graph_search(query) calling encode_single -- is a
# pair not in this set, so test_embedder_call_site_inventory_pinned fails RED naming it,
# until its author classifies it under the query/document rule and adds a row here.
EMBEDDER_CALL_SITE_INVENTORY = frozenset({
    ("_dream_hints", "encode_single"),
    ("_promote_slots", "encode_single"),
    ("_propose_dream_alias_candidates", "encode"),
    ("_resolve_dream_slot", "encode_single"),
    ("consolidate", "encode_query"),
    ("consolidate", "encode_single"),
    ("consolidation_candidates", "encode_query"),
    ("cortex_candidates", "encode_single"),
    ("cortex_dedup", "encode_single"),
    ("cortex_search", "encode_query"),
    ("cortex_write", "encode_single"),
    ("lesson_search", "encode_query"),
    ("lesson_write", "encode_single"),
    # Synthesis-time dedup gate (2026-08-12): document-side on purpose — it
    # re-composes the exact string lesson_write embeds, so the comparison
    # against stored lesson embeddings is symmetric.
    ("_synthesized_lesson_duplicate", "encode_single"),
    ("search", "encode_query"),
    ("search_documents", "encode_query"),
    ("set_add", "encode_single"),
    ("store", "encode_single"),
    # Session digest (spec 2026-08-24): document-side on purpose — the
    # digest is stored content embedded for later retrieval, the same
    # classification as store()'s entry embedding.
    ("_store_digest", "encode_single"),
    ("supersede", "encode_query"),
    ("supersede", "encode_single"),
    ("trace", "encode_query"),
    ("world_search", "encode_query"),
    ("world_write", "encode_single"),
})


def _collect_embedder_call_sites(source_path: Path) -> set[tuple[str, str]]:
    """Parse ``source_path`` and collect every ``self._embedder.<method>(...)`` call
    site as ``(enclosing function name, method name)``. Direct ``def``/``async def``
    nesting only; a call outside any function reports ``"<module>"``."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    sites: set[tuple[str, str]] = set()
    func_stack: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            func_stack.append(node.name)
            self.generic_visit(node)
            func_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Attribute)
                and isinstance(f.value.value, ast.Name)
                and f.value.value.id == "self"
                and f.value.attr == "_embedder"
            ):
                sites.add((func_stack[-1] if func_stack else "<module>", f.attr))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sites


def test_embedder_call_site_inventory_pinned():
    """AST guard scoped to pseudolife_memory/service.py ONLY -- it does not walk
    reference_bank.py:124 or storage/migrate.py:73; extend ``_collect_embedder_call_sites``
    callers to those files if this test should also cover them. A new
    ``self._embedder.<method>()`` call in service.py is a (function, method) pair not
    already in EMBEDDER_CALL_SITE_INVENTORY, so this fails RED naming the unclassified
    site until its author adds a row there under the module docstring's query/document
    rule -- the same forcing function the original 23-site enumeration applied."""
    assert _collect_embedder_call_sites(_SERVICE_PY) == EMBEDDER_CALL_SITE_INVENTORY


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


def test_cortex_candidates_never_uses_encode_query(recording_service):
    """cortex_candidates() ranks nearby already-stored slots for an empty-slot
    lookup -- a stored-to-stored comparison, not a user-facing retrieval probe,
    so it stays document-side. The second of the two genuine judgment calls the
    spec review flagged (the other is covered by
    test_dream_and_dedup_paths_never_use_encode_query below)."""
    svc, embedder = recording_service
    svc.cortex_write("Widget", "color", "blue")
    embedder.calls.clear()

    svc.cortex_candidates("Widget", "color")

    assert not embedder.any_query_calls()
    assert "encode_single" in embedder.methods_for("Widget color")


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
