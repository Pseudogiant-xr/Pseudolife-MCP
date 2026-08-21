"""Static guard: storage calls in service.py happen under the service lock.

Every psycopg transaction block runs on ONE shared connection, and psycopg
transaction blocks must never interleave across threads: when two threads
each enter ``with conn.transaction()`` concurrently, the exits mismatch,
psycopg raises "transaction commit at the wrong nesting level", and the
connection is left permanently in-transaction. The 2026-08-21 daemon
incident was exactly this — the sweep thread's unlocked
``prune_retrieval_log`` racing a lock-holding writer wedged the connection
for hours (the episode write-through then spun INSERTs inside the dead
transaction, pinning a Postgres core).

The invariant: every ``self._storage.*`` / ``self._graph.*`` call site in
``service.py`` is either lexically inside ``with self._lock`` or lives in a
private helper whose callers were all verified to hold the lock (the
2026-08-21 audit walked each chain). This test re-checks the lexical half
mechanically and pins the helper set, so the next unlocked addition fails
here instead of in production.

If this test fails on a NEW method: either wrap the storage call in
``with self._lock:``, or — only if every caller provably holds the lock —
add the helper to the allowlist with a comment naming the callers.
"""

from __future__ import annotations

import ast
from pathlib import Path

SERVICE_PY = (Path(__file__).resolve().parent.parent
              / "pseudolife_memory" / "service.py")

# Private helpers verified caller-by-caller in the 2026-08-21 audit: every
# call site of each is inside ``with self._lock`` (transitively, where the
# caller is itself an allowlisted helper). New entries need the same
# verification, not just a green run.
CALLER_HOLDS_LOCK = {
    "_assert_public_search_path",   # only _ensure_init
    "_ensure_init",                 # all call sites locked
    "_ensure_subject_entity",       # _promote_slots/cortex_write/set_add
    "_persist_all",                 # save/autosave_if_changed/flush
    "_entity_kind_map",             # cortex_write
    "_emit_correction_signal",      # cortex_write
    "_link_lesson_graph",           # lesson_write
    "_link_dream_relations",        # _dream_extract_relations
    "_annotate_lesson_staleness",   # lesson_search/lessons_dump
    "_log_retrieval_event",         # search
    "_record_retrieval_use",        # get_entry/reinforce
    "_persist_episodes",            # all episode mutators, locked
    "_load_infer_cursor",           # infer_outcomes_stage/dream_status
    "_save_infer_cursor",           # infer_outcomes_stage
    "_pending_inference_candidates",  # infer_outcomes_stage/dream_status
    "_delete_episode_row",          # _close_session_locked/episode_merge/...
    "_retitle_locked",              # set_session_title/episode_rename/...
    "_auto_title_locked",           # _close_session_locked
    "_resolve_or_create_entity",    # locked callers + allowlisted helpers
    "_propose_write_dedup",         # via _resolve_or_create_entity
}

_STORAGE_ROOTS = {"_storage", "_graph"}


def _is_lock_item(item: ast.withitem) -> bool:
    """True for ``with self._lock`` (bare or aliased)."""
    ctx = item.context_expr
    return (isinstance(ctx, ast.Attribute) and ctx.attr == "_lock"
            and isinstance(ctx.value, ast.Name) and ctx.value.id == "self")


def _storage_root(node: ast.expr, aliases: set[str]) -> bool:
    """True if the expression is self._storage/self._graph or an alias."""
    if (isinstance(node, ast.Attribute) and node.attr in _STORAGE_ROOTS
            and isinstance(node.value, ast.Name) and node.value.id == "self"):
        return True
    return isinstance(node, ast.Name) and node.id in aliases


def _walk(node: ast.AST, *, under_lock: bool, func: str | None,
          aliases: set[str], out: list[tuple[str, int]]) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # New scope: fresh alias set; the lock does not leak across defs.
        scoped: set[str] = set()
        for child in node.body:
            _walk(child, under_lock=False, func=node.name,
                  aliases=scoped, out=out)
        return
    if isinstance(node, ast.With):
        locked = under_lock or any(_is_lock_item(i) for i in node.items)
        for i in node.items:
            _walk(i.context_expr, under_lock=under_lock, func=func,
                  aliases=aliases, out=out)
        for child in node.body:
            _walk(child, under_lock=locked, func=func, aliases=aliases,
                  out=out)
        return
    if isinstance(node, ast.Assign):
        # Track ``st = self._storage`` style aliases within the function.
        if _storage_root(node.value, aliases):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    aliases.add(t.id)
        _walk(node.value, under_lock=under_lock, func=func, aliases=aliases,
              out=out)
        return
    if (isinstance(node, ast.Attribute)
            and _storage_root(node.value, aliases)
            and not under_lock and func is not None
            and func not in CALLER_HOLDS_LOCK):
        out.append((func, node.lineno))
    for child in ast.iter_child_nodes(node):
        _walk(child, under_lock=under_lock, func=func, aliases=aliases,
              out=out)


def find_unlocked_storage_sites(source: str) -> list[tuple[str, int]]:
    tree = ast.parse(source)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                _walk(child, under_lock=False, func=None, aliases=set(),
                      out=out)
    return out


def test_storage_calls_hold_service_lock():
    sites = find_unlocked_storage_sites(SERVICE_PY.read_text(
        encoding="utf-8"))
    assert not sites, (
        "storage/graph accessed outside self._lock (add the lock, or "
        "verify every caller holds it and extend CALLER_HOLDS_LOCK): "
        + ", ".join(f"{fn}:{ln}" for fn, ln in sorted(set(sites))))
