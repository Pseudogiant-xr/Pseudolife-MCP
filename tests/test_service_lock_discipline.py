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

The invariant: every use of ``self._storage`` / ``self._graph`` in the
MemoryService sources (``service.py`` and the ``DreamOps`` mixin in
``service_dream.py``) — attribute access, mutation, aliasing, or passing the
object to a callee — happens while ``self._lock`` is held. Two tests
enforce it:

* ``test_storage_calls_hold_service_lock`` — every storage use is either
  lexically inside ``with self._lock`` or inside a helper listed in
  ``CALLER_HOLDS_LOCK``.
* ``test_allowlisted_helpers_only_called_under_lock`` — the allowlist is
  not taken on faith: a fixpoint over ``self.<helper>()`` call sites
  verifies each listed helper is only ever reached with the lock held
  (directly, or via callers that are themselves always lock-held).

Known, accepted blind spots (each latent — no such code in service.py
today; noted so nobody mistakes a green run for proof against them):
lambdas inherit the lock state of their definition site even if invoked
later; a nested ``def`` resets to unlocked and flags under the inner
name; allowlist matching is by bare function name; manual
``self._lock.acquire()``/``release()`` is not recognized as locking; the
scan covers ``service.py`` plus the ``DreamOps`` mixin in
``service_dream.py`` (their call graphs are merged before the fixpoint,
since the mixin's methods run on the same instance) — any further split
of the class must be added to ``SERVICE_FILES``.

If ``test_storage_calls_hold_service_lock`` fails on a NEW method: either
wrap the storage use in ``with self._lock:``, or add the helper to
``CALLER_HOLDS_LOCK`` — the fixpoint test then verifies the callers for
you.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "pseudolife_memory"
# Every file that contributes methods to the MemoryService instance. The
# lock-discipline contract spans the whole class, so all parts are scanned
# and their call graphs merged before the fixpoint.
SERVICE_FILES = (_PKG / "service.py", _PKG / "service_dream.py")
SERVICE_PY = SERVICE_FILES[0]

# Private helpers whose callers hold the lock (verified mechanically by
# test_allowlisted_helpers_only_called_under_lock, seeded by the
# 2026-08-21 caller-by-caller audit).
CALLER_HOLDS_LOCK = {
    "_assert_public_search_path",
    "_ensure_init",
    "_ensure_postgres_storage",
    "_ensure_subject_entity",
    "_persist_all",
    "_entity_kind_map",
    "_emit_correction_signal",
    "_link_lesson_graph",
    "_link_dream_relations",
    "_annotate_lesson_staleness",
    "_log_retrieval_event",
    "_record_retrieval_use",
    "_track_slot_reads",
    "_persist_episodes",
    "_load_infer_cursor",
    "_save_infer_cursor",
    "_pending_inference_candidates",
    # Session-digest stage (spec 2026-08-24) — same locked-pull /
    # locked-commit shape as the outcome-inference trio above.
    "_load_digest_cursor",
    "_save_digest_cursor",
    "_pending_digest_candidates",
    "_store_digest",
    "_delete_episode_row",
    "_retitle_locked",
    "_auto_title_locked",
    "_resolve_or_create_entity",
    "_propose_write_dedup",
    # These three pass self._storage as an argument to the sync helpers
    # rather than calling methods on it — invisible to the first walker,
    # surfaced by the argument-passing check.
    "_save_cortex",
    "_save_world",
    "_save_lessons",
}

_STORAGE_ROOTS = {"_storage", "_graph"}


class _Scan:
    """One pass over a single source file collecting both unlocked storage
    sites and every ``self.<name>(...)`` call site with its lock state."""

    def __init__(self) -> None:
        # (enclosing_func, lineno) for storage uses outside the lock.
        self.unlocked_sites: list[tuple[str, int]] = []
        # (callee_name, under_lock, enclosing_func) for self-method calls.
        self.call_sites: list[tuple[str, bool, str | None]] = []


def _is_lock_item(item: ast.withitem) -> bool:
    """True for ``with self._lock`` (bare or aliased)."""
    ctx = item.context_expr
    return (isinstance(ctx, ast.Attribute) and ctx.attr == "_lock"
            and isinstance(ctx.value, ast.Name) and ctx.value.id == "self")


def _is_storage_expr(node: ast.expr, aliases: set[str]) -> bool:
    """True if the expression IS the storage/graph object (or an alias)."""
    if (isinstance(node, ast.Attribute) and node.attr in _STORAGE_ROOTS
            and isinstance(node.value, ast.Name) and node.value.id == "self"):
        return True
    return isinstance(node, ast.Name) and node.id in aliases


def _bind_alias_targets(target: ast.expr, aliases: set[str]) -> None:
    """Record every plain name in an assignment target as a storage alias.
    Tuple/list unpacking over-approximates (every name in the target
    becomes an alias), which can only add strictness, never lose a real
    site."""
    if isinstance(target, ast.Name):
        aliases.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _bind_alias_targets(elt, aliases)


def _assignment_binds_storage(value: ast.expr, aliases: set[str]) -> bool:
    if _is_storage_expr(value, aliases):
        return True
    if isinstance(value, (ast.Tuple, ast.List)):
        return any(_is_storage_expr(e, aliases) for e in value.elts)
    return False


def _flag_if_storage_use(node: ast.AST, *, under_lock: bool,
                         func: str | None, aliases: set[str],
                         scan: _Scan) -> None:
    """Flag attribute access ON the storage object, and the storage object
    passed as a call argument (handing the live connection to a callee is
    a storage use — the incident shape, one hop removed)."""
    if under_lock or func is None or func in CALLER_HOLDS_LOCK:
        return
    if (isinstance(node, ast.Attribute)
            and _is_storage_expr(node.value, aliases)):
        scan.unlocked_sites.append((func, node.lineno))
    elif isinstance(node, ast.Call):
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if _is_storage_expr(arg, aliases):
                scan.unlocked_sites.append((func, arg.lineno))


def _walk(node: ast.AST, *, under_lock: bool, func: str | None,
          aliases: set[str], scan: _Scan) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # New scope: fresh alias set; the lock does not leak across defs.
        scoped: set[str] = set()
        for child in node.body:
            _walk(child, under_lock=False, func=node.name,
                  aliases=scoped, scan=scan)
        return
    if isinstance(node, (ast.With, ast.AsyncWith)):
        # Items acquire left-to-right: an item AFTER `self._lock` in the
        # same statement already runs locked.
        locked = under_lock
        for item in node.items:
            _walk(item.context_expr, under_lock=locked, func=func,
                  aliases=aliases, scan=scan)
            if _is_lock_item(item):
                locked = True
            if (item.optional_vars is not None
                    and _is_storage_expr(item.context_expr, aliases)):
                _bind_alias_targets(item.optional_vars, aliases)
        for child in node.body:
            _walk(child, under_lock=locked, func=func, aliases=aliases,
                  scan=scan)
        return
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target])
        if node.value is not None:
            if _assignment_binds_storage(node.value, aliases):
                for t in targets:
                    _bind_alias_targets(t, aliases)
            _walk(node.value, under_lock=under_lock, func=func,
                  aliases=aliases, scan=scan)
        # Mutation THROUGH the object (`self._storage.cursor = v`) lives
        # in the target expression — walk targets too.
        for t in targets:
            _walk(t, under_lock=under_lock, func=func, aliases=aliases,
                  scan=scan)
        return
    if isinstance(node, ast.NamedExpr):  # (st := self._storage)
        if _assignment_binds_storage(node.value, aliases):
            _bind_alias_targets(node.target, aliases)
    if isinstance(node, ast.Call):
        if (isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"):
            scan.call_sites.append((node.func.attr, under_lock, func))
        _flag_if_storage_use(node, under_lock=under_lock, func=func,
                             aliases=aliases, scan=scan)
    elif isinstance(node, ast.Attribute):
        _flag_if_storage_use(node, under_lock=under_lock, func=func,
                             aliases=aliases, scan=scan)
    for child in ast.iter_child_nodes(node):
        _walk(child, under_lock=under_lock, func=func, aliases=aliases,
              scan=scan)


def scan_source(source: str) -> _Scan:
    tree = ast.parse(source)
    scan = _Scan()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                _walk(child, under_lock=False, func=None, aliases=set(),
                      scan=scan)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _walk(node, under_lock=False, func=None, aliases=set(),
                  scan=scan)
    return scan


def find_unlocked_storage_sites(source: str) -> list[tuple[str, int]]:
    return scan_source(source).unlocked_sites


def find_unlocked_helper_paths(*sources: str) -> list[str]:
    """Fixpoint over call sites: a function is lock-held when every
    ``self.<name>()`` call of it is either lexically under the lock or
    made from a function that is itself lock-held. Returns the
    allowlisted helpers for which that fails, with the offending callers.
    Functions never called via ``self.`` (public API entry points) are
    treated as NOT lock-held, which is the safe direction. Multiple
    sources (the mixin split) are scanned separately and their call
    sites merged, since all methods share one instance and one lock."""
    scan = _Scan()
    for source in sources:
        part = scan_source(source)
        scan.unlocked_sites.extend(part.unlocked_sites)
        scan.call_sites.extend(part.call_sites)
    callers: dict[str, list[tuple[bool, str | None]]] = {}
    for callee, under_lock, func in scan.call_sites:
        callers.setdefault(callee, []).append((under_lock, func))

    # Anchoring (least fixpoint): a function is reachable-from-a-locked-
    # region if some call site is lexically locked, or comes from an
    # anchored function. Without this, a purely self-recursive helper
    # would count as held on the strength of its own cycle.
    anchored = {name: False for name in callers}
    changed = True
    while changed:
        changed = False
        for name, sites in callers.items():
            if not anchored[name] and any(
                    under or (via is not None and anchored.get(via, False))
                    for under, via in sites):
                anchored[name] = True
                changed = True

    # Greatest fixpoint: start by assuming every anchored function is
    # lock-held, then refute until stable. A cycle whose every ENTRY is
    # locked stays held (recursive helpers like _resolve_or_create_entity
    # execute under the lock their outermost caller took); a function with
    # no self-call sites is an external entry point and never held.
    held = dict(anchored)
    changed = True
    while changed:
        changed = False
        for name, sites in callers.items():
            if not held[name]:
                continue
            ok = all(under or (via is not None and held.get(via, False))
                     for under, via in sites)
            if not ok:
                held[name] = False
                changed = True

    bad: list[str] = []
    for helper in sorted(CALLER_HOLDS_LOCK):
        sites = callers.get(helper, [])
        naked = [(via or "<module>") for under, via in sites
                 if not under
                 and not (via is not None and held.get(via, False))]
        if naked:
            bad.append(f"{helper} (called without lock from: "
                       + ", ".join(sorted(set(naked))) + ")")
    return bad


def test_storage_calls_hold_service_lock():
    sites = [site
             for path in SERVICE_FILES
             for site in find_unlocked_storage_sites(
                 path.read_text(encoding="utf-8"))]
    assert not sites, (
        "storage/graph used outside self._lock (add the lock, or add the "
        "helper to CALLER_HOLDS_LOCK — the fixpoint test will then verify "
        "its callers): "
        + ", ".join(f"{fn}:{ln}" for fn, ln in sorted(set(sites))))


def test_allowlisted_helpers_only_called_under_lock():
    bad = find_unlocked_helper_paths(
        *(path.read_text(encoding="utf-8") for path in SERVICE_FILES))
    assert not bad, (
        "CALLER_HOLDS_LOCK entries reachable without the lock: "
        + "; ".join(bad))


# ── walker self-tests on synthetic sources ──────────────────────────────
# The 2026-08-21 review pass found the first walker passed several
# incident-shaped constructs; these pin the fixed behavior.

def _sites(src: str) -> list[tuple[str, int]]:
    return find_unlocked_storage_sites(textwrap.dedent(src))


def test_walker_flags_unlocked_call():
    assert _sites("""
        class S:
            def bad(self):
                self._storage.prune(1)
    """) == [("bad", 4)]


def test_walker_accepts_locked_call_and_none_check():
    assert _sites("""
        class S:
            def good(self):
                if self._storage is None:
                    return
                with self._lock:
                    self._storage.prune(1)
    """) == []


def test_walker_flags_tuple_unpacked_alias():
    assert _sites("""
        class S:
            def bad(self):
                st, g = self._storage, self._graph
                st.prune(1)
    """) == [("bad", 5)]


def test_walker_flags_walrus_alias():
    assert _sites("""
        class S:
            def bad(self):
                if (st := self._storage) is not None:
                    st.prune(1)
    """) == [("bad", 5)]


def test_walker_flags_attribute_mutation_target():
    assert _sites("""
        class S:
            def bad(self):
                self._storage.cursor = 5
    """) == [("bad", 4)]


def test_walker_flags_storage_passed_as_argument():
    assert _sites("""
        class S:
            def bad(self):
                helper(self._storage)
    """) == [("bad", 4)]


def test_walker_accepts_sibling_with_item_after_lock():
    assert _sites("""
        class S:
            def good(self):
                with self._lock, self._storage.txn():
                    pass
    """) == []


def test_walker_flags_sibling_with_item_before_lock():
    assert _sites("""
        class S:
            def bad(self):
                with self._storage.txn(), self._lock:
                    pass
    """) == [("bad", 4)]


def test_fixpoint_accepts_transitively_locked_chain():
    src = """
        class S:
            def api(self):
                with self._lock:
                    self._chain_root()
            def _chain_root(self):
                self._ensure_init()
            def _ensure_init(self):
                self._storage.q()
    """
    assert find_unlocked_helper_paths(textwrap.dedent(src)) == []


def test_fixpoint_rejects_unlocked_helper_call():
    src = """
        class S:
            def sweep(self):
                self._ensure_init()
            def _ensure_init(self):
                self._storage.q()
    """
    assert any(b.startswith("_ensure_init ") and "sweep" in b
               for b in find_unlocked_helper_paths(textwrap.dedent(src)))


def test_fixpoint_rejects_self_supporting_recursion():
    # A helper whose only call site is itself must not vouch for itself:
    # something external invokes it, and that entry is unlocked-unknown.
    src = """
        class S:
            def _ensure_init(self):
                self._ensure_init()
    """
    assert any(b.startswith("_ensure_init ")
               for b in find_unlocked_helper_paths(textwrap.dedent(src)))
