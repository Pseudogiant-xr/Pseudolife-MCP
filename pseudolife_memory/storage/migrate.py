"""Legacy v≤7 .pt bank → schema v8 Postgres migration (P1.6).

Runs when a legacy ``memory_state/cms_state.pt`` (or ``cortex_state.pt``)
exists under the data dir AND either the bank is empty or a previous,
interrupted import of *these same sources* is on record. Sources are
renamed ``*.pre-v8.bak`` afterwards — never deleted.

The import is NOT atomic and cannot be: ``insert_entry`` commits per row
and the renames are filesystem operations. So progress is *recorded*
rather than inferred. A ``legacy_migration`` meta row carries the source
fingerprint and a status; a partial import leaves ``status=in_progress``
and the next boot resumes it, skipping rows already present. This
replaces the old "the entries table is non-empty ⇒ nothing to do" guard,
under which any mid-import failure (malformed row, embedder failure,
dropped connection, full disk, killed machine) durably committed some
rows and then permanently blocked every retry: the remaining entries and
ALL cortex facts were lost silently, and the sources never got their
``.pre-v8.bak`` rename (#187).

The resume is a MERGE, not a replay: the daemon deliberately keeps serving
between the failed boot and the resume (the partial state is non-fatal), so
entries already committed are skipped and cortex facts only fill slots
nobody has written in the meantime.

Both the entries branch AND the cortex-facts branch re-embed any row whose
stored embedding dim doesn't match the live schema's before inserting it
(2026-07-28, embedding-backbone-v25 review escalation) — a real legacy
bank always predates the current model/dimension for BOTH tables. That
fix removed the commonest *cause* of a mid-import failure; the progress
record above is what makes the failure *class* recoverable.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

#: Meta key holding the migration progress record (see module docstring).
#: Deliberately a meta row, not a schema column — the migration is a
#: one-shot boot concern and meta is already the home of the other
#: migration leftovers (``migrated_interaction_count``).
MIGRATION_META_KEY = "legacy_migration"

#: ``reason`` values that mean "a partial import is on record and this boot
#: did NOT finish it" — the bank is short and needs an operator. The caller
#: (``MemoryService._ensure_init``) turns these into an ERROR log and a
#: degraded ``/health``. Every other no-op reason is benign.
PARTIAL_REASONS = frozenset({
    "partial_migration_source_mismatch", "legacy_source_missing",
})

# Checkpoint cadence for ``entries_done``. Purely observational: the
# resume SKIP is identity-based (see ``_already_imported``), so a
# checkpoint that lags the truly-committed count can never cause
# duplicates — it only makes the logged progress coarser.
_CHECKPOINT_EVERY = 200


def _fingerprint(paths: dict[str, Path]) -> dict:
    """Identity of the legacy sources, so a *different* .pt bank dropped in
    after an interrupted import is never folded into its leftovers.

    Size + SHA-256 of each source present. Hashing costs one full read of
    a bank that is about to be fully deserialized anyway, and only on the
    boots where legacy sources actually exist.
    """
    out = {}
    for name, p in paths.items():
        if not p.exists():
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        out[name] = {"size": p.stat().st_size, "sha256": h.hexdigest()}
    return out


def _already_imported(rows: list[dict]) -> Counter:
    """Multiset of the stable identity legacy entry rows carry.

    Legacy rows have no id that survives the import (``entries.id`` is a
    fresh serial), so the identity is ``(text, ts)`` — the two fields no
    write-through path mutates.

    ``band`` is deliberately NOT part of the key. The boot sequence runs
    ``sync.hydrate_cms`` immediately after this import, and hydrate
    REWRITES the band column of every row whose band is absent from the
    live preset (its stale-band-stamp reconcile). A legacy v<=7 bank
    carries continuum band names while the live default preset is the
    single ``flat`` band, so the rows this function has to recognise on
    the next boot come back under a different band than they were
    inserted with — keying on it would match nothing and re-insert the
    entire imported prefix.

    A Counter rather than a set so a legacy bank that genuinely holds the
    same text twice at the same instant still gets both copies on resume.
    """
    return Counter(
        (r.get("text"), float(r.get("ts") or 0.0)) for r in rows
    )


def migrate_legacy(data_dir: str | Path, storage, embedder) -> dict:
    """Import a legacy .pt bank into storage. Idempotent, and resumable.

    ``embedder`` is the service's own, already-constructed
    :class:`~pseudolife_memory.memory.embedding.EmbeddingPipeline` (threaded
    in, not re-instantiated — single-copy principle). Every real legacy .pt
    bank in the wild predates the current build's embedder, so its stored
    entry AND fact embeddings can be dimensioned for an older model (e.g.
    384-d MiniLM) than the live schema's vector columns (1024-d as of
    schema v25). Inserting them verbatim raises a pgvector dimension error,
    so any row whose stored embedding dim doesn't match
    ``embedder.embedding_dim`` is re-embedded from its own stored text
    (entries) or its own (entity, attribute, value) claim text (facts)
    before insertion.

    Returns ``{"migrated": bool, ...counts}``; ``reason`` on a no-op.
    """
    data_dir = Path(data_dir)
    cms_path = data_dir / "memory_state" / "cms_state.pt"
    cortex_path = data_dir / "cortex_state.pt"
    record = storage.meta_get(MIGRATION_META_KEY) or {}

    if not cms_path.exists() and not cortex_path.exists():
        # No sources ⇒ nothing to import, and this is also what a bank
        # migrated BEFORE #187 looks like: it has no meta record, but its
        # sources were renamed ``.pre-v8.bak`` on the way out, so it never
        # reaches the resume logic at all. The one case that needs
        # reconciling is a crash inside the single-``meta_set`` window
        # between the renames and ``status=done``.
        if record.get("status") == "in_progress":
            renamed = [p.with_name(p.name + ".pre-v8.bak")
                       for p in (cms_path, cortex_path)]
            if any(p.exists() for p in renamed):
                storage.meta_set(MIGRATION_META_KEY,
                                 {**record, "status": "done",
                                  "finished_at": time.time()})
                logger.warning(
                    "legacy migration record reconciled: sources were "
                    "already renamed, marking done")
                return {"migrated": False, "reason": "completed_at_rename"}
            logger.error(
                "legacy migration is recorded as in_progress but its sources "
                "are gone from %s — restore cms_state.pt / cortex_state.pt to "
                "resume, or use ops/restore_from_pt.py", data_dir)
            return {"migrated": False, "reason": "legacy_source_missing"}
        return {"migrated": False, "reason": "no_legacy_state"}

    sources = {"cms": cms_path, "cortex": cortex_path}
    fingerprint = _fingerprint(sources)
    resuming = False
    if record.get("status") == "in_progress":
        # Two halves, because the renames at the bottom are per-file and a
        # crash between them legitimately leaves one source renamed:
        #   (a) every source still on disk must match what we recorded, and
        #   (b) every source we recorded that is NOT on disk must have been
        #       renamed to .pre-v8.bak, i.e. this import is the reason it is
        #       gone. Without (b) a recorded source that simply vanished
        #       would pass (a) vacuously and the resume would mark a
        #       permanently short bank done.
        recorded = record.get("source") or {}
        on_disk_matches = all(
            recorded.get(name) == fp for name, fp in fingerprint.items())
        vanished_were_renamed = all(
            sources[name].with_name(sources[name].name + ".pre-v8.bak").exists()
            for name in recorded if name not in fingerprint and name in sources
        )
        if on_disk_matches and vanished_were_renamed:
            resuming = True
        else:
            # Refuse rather than merge: the bank holds the leftovers of a
            # DIFFERENT legacy import, and the identity-based skip below
            # would not recognise these rows as already-present.
            logger.error(
                "legacy migration is recorded as in_progress for other "
                "sources; refusing to import %s on top of a partial bank. "
                "Restore the original .pt files to resume, or clear the "
                "'%s' meta row deliberately.", data_dir, MIGRATION_META_KEY)
            return {"migrated": False,
                    "reason": "partial_migration_source_mismatch"}

    existing = storage.load_entries()
    if not resuming and (existing or storage.load_facts()):
        # A real bank with no migration ever started — unchanged behavior.
        return {"migrated": False, "reason": "storage_not_empty"}

    import torch

    # Same meta keys the live cortex persistence writes — imported rather
    # than re-spelled so a rename cannot silently orphan the resume path.
    from pseudolife_memory.storage.sync import (
        _CORTEX_CURSOR_KEY, _CORTEX_LOG_KEY, _record_to_row,
    )

    started_at = record.get("started_at") if resuming else time.time()
    storage.meta_set(MIGRATION_META_KEY, {
        "status": "in_progress",
        "source": fingerprint,
        "entries_done": int(record.get("entries_done") or 0) if resuming else 0,
        "started_at": started_at,
        "updated_at": time.time(),
    })

    # Only a resume can meet pre-existing rows; on a fresh import the
    # guard above already proved the bank is empty, so skip the scan.
    already = _already_imported(existing) if resuming else Counter()
    target_dim = embedder.embedding_dim
    entries = episodes = facts = reembedded = skipped = 0

    def _checkpoint() -> None:
        storage.meta_set(MIGRATION_META_KEY, {
            "status": "in_progress", "source": fingerprint,
            "entries_done": entries + skipped,
            "started_at": started_at, "updated_at": time.time(),
        })

    if cms_path.exists():
        # weights_only=True: legacy CMS snapshot is tensors + plain containers;
        # avoid unpickling arbitrary objects from an imported .pt bank (CWE-502).
        state = torch.load(str(cms_path), map_location="cpu", weights_only=True)
        # Episodes first (entries carry episode_id FKs). upsert_episode is
        # keyed on the legacy id, so a resume re-applies these harmlessly.
        ep_payload = (state.get("episodes") or {}).get("episodes") or {}
        for _eid, ep in ep_payload.items():
            storage.upsert_episode({
                "id": ep["id"], "title": ep["title"], "hint": ep.get("hint"),
                "started_at": ep["started_at"], "ended_at": ep.get("ended_at"),
                "closed_by_new_start": bool(ep.get("closed_by_new_start")),
                "session_key": ep.get("session_key"),
                "parent_id": ep.get("parent_id"),
            })
            episodes += 1
        for band_name, band_state in (state.get("bands") or {}).items():
            for e in band_state.get("entries", []):
                ts = float(e.get("timestamp", 0.0))
                key = (e["text"], ts)
                if already[key]:
                    # Committed by an earlier, interrupted pass.
                    already[key] -= 1
                    skipped += 1
                    continue
                embedding = e["embedding"]
                if len(embedding) != target_dim:
                    # Legacy dim doesn't fit this build's vector column —
                    # re-embed through the real pipeline rather than
                    # inserting a wrong-shaped vector (which either raises
                    # at the DB or, worse, would silently never match
                    # anything at search time if the column allowed it).
                    embedding = embedder.encode_single(e["text"])
                    reembedded += 1
                storage.insert_entry({
                    "band": band_name,
                    "text": e["text"],
                    "embedding": embedding,
                    "surprise": float(e.get("surprise_score", 0.0)),
                    "ts": ts,
                    "access_count": int(e.get("access_count", 0)),
                    "source": e.get("source", ""),
                    "superseded_at": e.get("superseded_at"),
                    "superseded_by_text": e.get("superseded_by_text"),
                    "last_logical_turn": e.get("last_logical_turn"),
                    "episode_id": e.get("episode_id"),
                    "episode_title": e.get("episode_title"),
                    "tags": list(e.get("tags") or []),
                    "slots": [list(s) for s in (e.get("slots") or [])],
                })
                entries += 1
                if (entries + skipped) % _CHECKPOINT_EVERY == 0:
                    _checkpoint()
        storage.meta_set("migrated_interaction_count",
                         int(state.get("interaction_count", 0)))
        _checkpoint()

    reembedded_facts = facts_skipped = 0
    if cortex_path.exists():
        from pseudolife_memory.memory.cortex import CortexStore
        cortex = CortexStore()
        cortex.load(cortex_path)
        for r in cortex.records:
            if r.embedding is not None and len(r.embedding) != target_dim:
                # Same treatment as the entries branch above, and for the
                # same reason: a legacy fact's stored claim embedding
                # predates this build's model/dimension. Re-embed from the
                # record's own (entity, attribute, value) using the EXACT
                # claim-text shape ``MemoryService.cortex_write`` commits
                # (service.py ~line 1509:
                # ``f"{entity} {attribute} {value}".strip()``) so the row
                # fits the live vector column before it is ever inserted.
                claim = f"{r.entity} {r.attribute} {r.value}".strip()
                r.embedding = embedder.encode_single(claim)
                reembedded_facts += 1
        rows = [_record_to_row(r) for r in cortex.records]
        if not resuming:
            # Fresh import: the guard above proved the bank was empty, so
            # the snapshot rewrite has nothing to destroy.
            storage.replace_facts(rows)
            storage.meta_set(_CORTEX_LOG_KEY, cortex.supersession_log[-200:])
            storage.meta_set(_CORTEX_CURSOR_KEY, cortex.dream_cursor)
            facts = len(rows)
        else:
            # Resume: the daemon has been SERVING since the failed boot (the
            # partial state is deliberately non-fatal), so dream and
            # fact_set writes may have landed in the meantime. A snapshot
            # rewrite would DELETE them. Merge instead — fill only the slots
            # nobody has written yet, and leave every occupied slot alone.
            occupied = {(f["entity_norm"], f["attribute_norm"])
                        for f in storage.load_facts()}
            keep = [r for r in rows
                    if (r["entity_norm"], r["attribute_norm"]) not in occupied]
            slots = {(r["entity_norm"], r["attribute_norm"]) for r in keep}
            if slots:
                # Per-slot replace touches only these (empty) slots — the
                # same primitive the live per-write path uses.
                storage.replace_slot_facts(slots, keep)
            facts = len(keep)
            facts_skipped = len(rows) - len(keep)
            # The dream cursor is monotonic by contract; a legacy value is
            # necessarily older than anything the degraded window advanced
            # it to, and moving it backwards would force re-extraction of
            # every memory since.
            current_cursor = float(storage.meta_get(_CORTEX_CURSOR_KEY, 0.0) or 0.0)
            storage.meta_set(_CORTEX_CURSOR_KEY,
                             max(current_cursor, float(cortex.dream_cursor or 0.0)))
            # Same reasoning for the supersession log: only seed it if the
            # live one is still empty, never overwrite real audit history.
            if not (storage.meta_get(_CORTEX_LOG_KEY) or []):
                storage.meta_set(_CORTEX_LOG_KEY,
                                 cortex.supersession_log[-200:])

    # Rename sources — migration is read-only on content, rename-only on
    # the filesystem, and never deletes.
    for p in (cms_path, cortex_path):
        if p.exists():
            p.rename(p.with_name(p.name + ".pre-v8.bak"))

    # status=done only AFTER the renames: a crash before this point leaves
    # in_progress and is either resumed (sources still there) or
    # reconciled at the top of this function (sources already renamed).
    storage.meta_set(MIGRATION_META_KEY, {
        "status": "done", "source": fingerprint,
        "entries_done": entries + skipped,
        "started_at": started_at, "finished_at": time.time(),
    })

    summary = {"migrated": True, "resumed": resuming, "entries": entries,
               "entries_skipped": skipped, "episodes": episodes,
               "facts": facts, "facts_skipped": facts_skipped,
               "reembedded": reembedded,
               "reembedded_facts": reembedded_facts}
    logger.warning("legacy bank migrated to schema v8: %s", summary)
    return summary
