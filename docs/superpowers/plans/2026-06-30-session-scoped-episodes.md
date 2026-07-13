# Session-Scoped Episodes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make session episodes correctly named, correctly attributed, and free of empty/spurious husks by keying episode lifecycle *and* per-store stamping to one stable per-session id minted by the shim.

**Architecture:** The shim is the only process that exists exactly once per Claude session and sees every MCP call. It mints a `session_uid`, injects it as an `X-PL-Session` header on every upstream call, and owns the episode lifecycle (open on startup, close on exit) via the REST `/api/episode/*` endpoints. The daemon stops using a single global "current episode" pointer: `EpisodeManager` tracks one open episode per `session_key`, stamping routes a store to *its own* session's open episode, and a session that ends with zero captured entries is deleted rather than persisted. A one-shot prune clears the existing junk.

**Tech Stack:** Python 3.11, FastMCP (`mcp` SDK), Starlette/uvicorn daemon, Postgres (psycopg3), pytest. Windows host; daemon runs in Docker.

## Global Constraints

- **Never touch the Postgres container's data volume.** Deploy is daemon-image-only (backup `.sql.gz` + rollback image tag + `/health` schema check), per the project's established procedure.
- **No schema migration needed:** `episodes.session_key` (v14) and `episodes.parent_id` (v15) already exist. This plan adds no columns.
- **Backward compatibility:** `session_key=None` must preserve today's single-`current_id` behavior (embedded/file mode, existing tests, `memory_episode_start` without a session). New session-scoping activates only when a real `session_key` is supplied.
- **Best-effort lifecycle:** every shim-side episode call is wrapped so a daemon error or cold daemon can never break or slow a Claude session (same contract as the existing hooks).
- **ASCII only** in titles/markers (Windows console safety).
- **Title format (locked):** `"{project} - {YYYY-MM-DD HH:MM}"`, project = git-repo-root basename, else cwd basename, else `session` (never the home dir).

---

### Task 1: Session-keyed `EpisodeManager`

Replace the single global `current_id` clobber model with per-session open tracking. No cross-session auto-close.

**Files:**
- Modify: `pseudolife_memory/memory/episodes.py`
- Test: `tests/test_episodes.py`

**Interfaces:**
- Produces:
  - `EpisodeManager.open_leaf_for(session_key: str | None) -> Episode | None` — deepest currently-open episode for a session.
  - `EpisodeManager.start_session(title: str, session_key: str | None, hint: str | None = None) -> Episode` — idempotent per open `session_key`; does NOT close other sessions.
  - `EpisodeManager.start_nested(title, hint=None, session_key=None) -> Episode` — nests under that session's open leaf (or the global leaf when `session_key=None`); propagates `session_key` down.
  - `EpisodeManager.end_leaf(session_key: str | None = None) -> Episode | None` — closes that session's open leaf (or the global leaf) and pops to parent.
  - `EpisodeManager.stamp(entry, session_key: str | None = None) -> None` — stamps with the session's open leaf; falls back to `current_id` only when `session_key is None`.
  - `EpisodeManager.remove(id: str) -> None` — drop an episode from the log.
  - `EpisodeManager.end()` retained as `end_leaf(None)` (legacy alias).
  - `EpisodeManager.end_session(session_key)` unchanged.

- [ ] **Step 1: Write failing tests** — append to `tests/test_episodes.py`:

```python
def test_start_session_does_not_close_other_sessions():
    em = EpisodeManager()
    a = em.start_session("proj-a", session_key="A")
    b = em.start_session("proj-b", session_key="B")
    # B opening must NOT close A (the old clobber bug).
    assert a.ended_at is None
    assert b.ended_at is None
    assert em.open_leaf_for("A").id == a.id
    assert em.open_leaf_for("B").id == b.id


def test_start_session_is_idempotent_per_key():
    em = EpisodeManager()
    a1 = em.start_session("proj-a", session_key="A")
    a2 = em.start_session("proj-a again", session_key="A")
    assert a1.id == a2.id  # re-fire returns the same open episode
    assert a2.title == "proj-a"  # unchanged


def test_stamp_routes_to_the_callers_session():
    em = EpisodeManager()
    a = em.start_session("proj-a", session_key="A")
    b = em.start_session("proj-b", session_key="B")
    ent = MemoryEntry(text="x", embedding=None, surprise_score=0.0,
                      timestamp=0.0, access_count=0, source="claude", bank="instant")
    em.stamp(ent, session_key="B")
    assert ent.episode_id == b.id
    assert ent.episode_title == "proj-b"


def test_stamp_with_no_session_falls_back_to_current_id():
    em = EpisodeManager()
    ep = em.start_session("solo", session_key=None)
    ent = MemoryEntry(text="x", embedding=None, surprise_score=0.0,
                      timestamp=0.0, access_count=0, source="claude", bank="instant")
    em.stamp(ent)  # session_key omitted -> legacy current_id path
    assert ent.episode_id == ep.id


def test_stamp_with_unknown_session_is_noop_not_crosstalk():
    em = EpisodeManager()
    em.start_session("proj-a", session_key="A")  # sets current_id
    ent = MemoryEntry(text="x", embedding=None, surprise_score=0.0,
                      timestamp=0.0, access_count=0, source="claude", bank="instant")
    em.stamp(ent, session_key="GHOST")  # no open episode for GHOST
    assert ent.episode_id is None  # must NOT fall through to A


def test_nested_belongs_to_caller_session_and_end_leaf_pops():
    em = EpisodeManager()
    root = em.start_session("proj-a", session_key="A")
    child = em.start_nested("subtask", session_key="A")
    assert child.parent_id == root.id
    assert child.session_key == "A"
    assert em.open_leaf_for("A").id == child.id
    em.end_leaf(session_key="A")  # close the child
    assert child.ended_at is not None
    assert em.open_leaf_for("A").id == root.id  # popped back to root
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_episodes.py -k "session or stamp or nested" -v`
Expected: FAIL (`start_session` / `open_leaf_for` / `remove` not defined; `stamp` signature mismatch).

- [ ] **Step 3: Implement** — in `pseudolife_memory/memory/episodes.py`, replace the `start`/`start_nested`/`end`/`stamp` block (lines ~99-219) with:

```python
    def start_session(self, title: str, session_key: str | None = None,
                      hint: str | None = None) -> Episode:
        """Open a root session episode. Idempotent per open ``session_key``
        (a resume/compact re-fire returns the existing open one) and, unlike
        the old ``start``, NEVER closes another session's open episode."""
        if session_key is not None:
            existing = self.open_leaf_for(session_key)
            if existing is not None:
                return existing
        ep = Episode(
            id=uuid.uuid4().hex,
            title=title,
            started_at=time.time(),
            hint=hint,
            session_key=session_key,
        )
        self.episodes[ep.id] = ep
        self.current_id = ep.id
        return ep

    # Legacy alias: pre-session callers (tests, embedded single-session use)
    # still get the old "one open episode, auto-close prior" semantics.
    def start(self, title: str, hint: str | None = None,
              session_key: str | None = None) -> Episode:
        if session_key is None and self.current_id is not None:
            prior = self.episodes.get(self.current_id)
            if prior is not None and prior.ended_at is None:
                prior.ended_at = time.time()
                prior.closed_by_new_start = True
        return self.start_session(title, session_key=session_key, hint=hint)

    def start_nested(self, title: str, hint: str | None = None,
                     session_key: str | None = None) -> Episode:
        """Open a sub-episode under the caller's open leaf (which stays open).
        With a ``session_key`` it nests under THAT session's leaf and inherits
        the key; without one it uses the global ``current_id`` leaf."""
        parent = (self.open_leaf_for(session_key) if session_key is not None
                  else self.open_episode())
        ep = Episode(
            id=uuid.uuid4().hex,
            title=title,
            started_at=time.time(),
            hint=hint,
            parent_id=parent.id if parent is not None else None,
            session_key=(session_key if session_key is not None
                         else (parent.session_key if parent else None)),
        )
        self.episodes[ep.id] = ep
        self.current_id = ep.id
        return ep

    def end_leaf(self, session_key: str | None = None) -> Episode | None:
        """Close the open leaf (for ``session_key`` when given, else the global
        ``current_id`` leaf) and pop to its parent if still open."""
        ep = (self.open_leaf_for(session_key) if session_key is not None
              else self.open_episode())
        if ep is None:
            if session_key is None:
                self.current_id = None
            return None
        ep.ended_at = time.time()
        parent = self.episodes.get(ep.parent_id) if ep.parent_id else None
        new_leaf = parent if (parent and parent.ended_at is None) else None
        if self.current_id == ep.id:
            self.current_id = new_leaf.id if new_leaf else None
        return ep

    def end(self) -> Episode | None:
        """Legacy: close the global current leaf. Equivalent to end_leaf()."""
        return self.end_leaf(None)

    def open_leaf_for(self, session_key: str | None) -> Episode | None:
        """The deepest currently-open episode for ``session_key`` (root or a
        nested child), or None. 'Deepest' = latest ``started_at`` among the
        session's open episodes, which is the child (children start after
        their parent)."""
        if session_key is None:
            return None
        open_eps = [e for e in self.episodes.values()
                    if e.ended_at is None and e.session_key == session_key]
        if not open_eps:
            return None
        return max(open_eps, key=lambda e: e.started_at)

    def remove(self, id: str) -> None:
        """Drop an episode from the log (used by prune-on-empty / cleanup)."""
        self.episodes.pop(id, None)
        if self.current_id == id:
            self.current_id = None
```

And update `stamp` (was lines ~209-219) to:

```python
    def stamp(self, entry: MemoryEntry, session_key: str | None = None) -> None:
        """Fill ``entry.episode_id`` / ``entry.episode_title`` from the open
        episode. With a ``session_key`` it stamps THAT session's open leaf (and
        never another session's — no crosstalk). Without one it uses the global
        ``current_id`` leaf (legacy single-session path). No-op when nothing
        applies."""
        if session_key is not None:
            ep = self.open_leaf_for(session_key)
        elif self.current_id is not None:
            ep = self.episodes.get(self.current_id)
            if ep is not None and ep.ended_at is not None:
                ep = None
        else:
            ep = None
        if ep is None:
            return
        entry.episode_id = ep.id
        entry.episode_title = ep.title
```

Keep `end_session`, `_descends_from`, `open_episode`, `get`, `list`, `to_dict`, `from_dict` unchanged.

- [ ] **Step 4: Run to verify pass + no regressions**

Run: `pytest tests/test_episodes.py -v`
Expected: PASS (including the pre-existing tests; if a legacy test asserted that `start()` clobbers a prior open episode, it still does because that path passes `session_key=None`).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/episodes.py tests/test_episodes.py
git commit -m "feat(episodes): session-keyed lifecycle (no cross-session clobber)"
```

---

### Task 2: Thread `session_key` into stamping (CMS + service.store)

Make a `memory_store` stamp its own session's episode by passing the resolved session id down to `episodes.stamp`.

**Files:**
- Modify: `pseudolife_memory/memory/cms.py` (the `store` signature + the `episodes.stamp(entry)` call ~line 307)
- Modify: `pseudolife_memory/service.py` (`store` ~463; the other two `self._cms.store(...)` call sites at ~838 and ~2256)
- Test: `tests/test_service.py`

**Interfaces:**
- Consumes: `EpisodeManager.stamp(entry, session_key)` (Task 1), `MemoryService._resolve_writer() -> (writer_id, session_id)` (existing).
- Produces: `CMS.store(text, embedding, *, source=..., tags=..., session_key=None)`.

- [ ] **Step 1: Write failing test** — append to `tests/test_service.py` (use the existing service fixture pattern in that file; this asserts a store made "inside" session B is stamped with B's episode):

```python
def test_store_stamps_callers_session_episode(service):
    # Two concurrent sessions open; a store resolved to session B must carry
    # B's episode, regardless of which session opened last.
    service.episode_start_session("A", "proj-a")
    service.episode_start_session("B", "proj-b")
    from pseudolife_memory.writer_context import set_writer_context, reset_writer_context
    tok = set_writer_context("writer-x", "B")
    try:
        service.store("a durable fact about session B work", source="claude")
    finally:
        reset_writer_context(tok)
    eps = {e["title"]: e for e in service.episode_list()["episodes"]}
    assert eps["proj-b"]["entry_count"] == 1
    assert eps["proj-a"]["entry_count"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_service.py::test_store_stamps_callers_session_episode -v`
Expected: FAIL (store currently stamps via global `current_id` → counts land on the last-opened session, not B).

- [ ] **Step 3: Implement.**

In `pseudolife_memory/memory/cms.py`, change the `store` signature to accept `session_key` and pass it to stamp. Find `def store(` and add the kwarg; at the stamp call (~307) change `self.episodes.stamp(entry)` to `self.episodes.stamp(entry, session_key)`:

```python
    def store(self, text, embedding, *, source="claude", tags=None,
              session_key=None):
        ...
            self.episodes.stamp(entry, session_key)
```

(Preserve every other line of `store` exactly; only the signature and that one call change.)

In `pseudolife_memory/service.py` `store` (~494), resolve the session and pass it:

```python
            _, session_id = self._resolve_writer()
            stored, surprise = self._cms.store(
                text, embedding, source=source, tags=tags,
                session_key=session_id,
            )
```

For the other two `self._cms.store(...)` call sites (~838, ~2256): add `session_key=session_id` where a writer is already resolved in that method, else `session_key=self._resolve_writer()[1]`. (Verify the surrounding lines when editing; both must keep their existing args.)

- [ ] **Step 4: Run to verify pass + suite**

Run: `pytest tests/test_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/cms.py pseudolife_memory/service.py tests/test_service.py
git commit -m "feat(store): stamp the caller's own session episode"
```

---

### Task 3: Session-scoped lifecycle service methods + prune-on-empty-close

Rewire the REST/MCP episode methods to the new manager API and delete an episode that ends empty.

**Files:**
- Modify: `pseudolife_memory/service.py` (`episode_start_session` ~1904, `episode_start` ~1883, `episode_end` ~1895, `episode_end_session` ~1926)
- Test: `tests/test_episode_service.py`

**Interfaces:**
- Consumes: Task 1 manager API; `storage.delete_episode(id)` (Task 4 — land Task 4 first or stub `_delete_episode` to only touch memory when storage is None).
- Produces: behavior — `episode_end_session` returns `{}` and persists nothing when the closed session captured 0 entries.

- [ ] **Step 1: Write failing tests** — append to `tests/test_episode_service.py`:

```python
def test_empty_session_is_pruned_on_close(service):
    service.episode_start_session("S1", "empty-proj")
    closed = service.episode_end_session("S1", run_dream=False)
    # Nothing was stored -> the husk is deleted, not returned/persisted.
    ids = [e["id"] for e in service.episode_list(include_open=True)["episodes"]]
    assert all(e.get("title") != "empty-proj" for e in
               service.episode_list(include_open=True)["episodes"])


def test_nonempty_session_survives_close(service):
    service.episode_start_session("S2", "real-proj")
    from pseudolife_memory.writer_context import set_writer_context, reset_writer_context
    tok = set_writer_context("w", "S2")
    try:
        service.store("durable work in S2", source="claude")
    finally:
        reset_writer_context(tok)
    service.episode_end_session("S2", run_dream=False)
    titles = [e["title"] for e in service.episode_list(include_open=True)["episodes"]]
    assert "real-proj" in titles


def test_two_sessions_start_without_clobber(service):
    a = service.episode_start_session("A", "proj-a")
    b = service.episode_start_session("B", "proj-b")
    assert a["ended_at"] is None and b["ended_at"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_episode_service.py -k "prune or survives or clobber" -v`
Expected: FAIL.

- [ ] **Step 3: Implement** in `pseudolife_memory/service.py`:

`episode_start_session`:

```python
    def episode_start_session(self, session_key, title, hint=None):
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            ep = self._cms.episodes.start_session(
                title=title, session_key=session_key, hint=hint)
            self._persist_episodes()
            return self._episode_to_dict(ep)
```

`episode_start` (nested) and `episode_end` (nested) become session-aware via the resolved request session:

```python
    def episode_start(self, title, hint=None):
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            _, session_id = self._resolve_writer()
            ep = self._cms.episodes.start_nested(
                title=title, hint=hint, session_key=session_id)
            self._persist_episodes()
            return self._episode_to_dict(ep)

    def episode_end(self):
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            _, session_id = self._resolve_writer()
            closed = self._cms.episodes.end_leaf(session_key=session_id)
            self._persist_episodes()
            return self._episode_to_dict(closed) if closed is not None else {}
```

`episode_end_session` — close, then prune-on-empty:

```python
    def episode_end_session(self, session_key, run_dream=True):
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            em = self._cms.episodes
            closed = em.end_session(session_key)
            result = self._episode_to_dict(closed) if closed is not None else {}
            pruned = False
            if closed is not None:
                # Subtree entry count: the root + any descendants.
                subtree = {closed.id} | {
                    e.id for e in em.episodes.values()
                    if em._descends_from(e, closed.id)
                }
                counts = self._episode_entry_counts()
                if sum(counts.get(i, 0) for i in subtree) == 0:
                    for i in subtree:
                        em.remove(i)
                        self._delete_episode_row(i)
                    pruned = True
            if not pruned:
                self._persist_episodes()
        if run_dream and result and not pruned:
            self._fire_and_forget_dream()
        return {} if pruned else result
```

Add two small helpers near `_persist_episodes`:

```python
    def _episode_entry_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for band in self._cms.bands:
            for entry in band.entries:
                if entry.episode_id:
                    counts[entry.episode_id] = counts.get(entry.episode_id, 0) + 1
        return counts

    def _delete_episode_row(self, episode_id: str) -> None:
        if self._storage is None:
            return
        try:
            self._storage.delete_episode(episode_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("episode delete failed: %s", exc)
```

Refactor `episode_list` to reuse `_episode_entry_counts()` (replace its inline count loop with `counts = self._episode_entry_counts()`).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_episode_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_episode_service.py
git commit -m "feat(episodes): session-scoped lifecycle + prune-on-empty-close"
```

---

### Task 4: `storage.delete_episode` + bulk `episode_prune_empty` + REST route

The low-level delete (needed by Task 3) and a one-shot bulk prune for the existing junk, exposed over REST so it updates both the live daemon's memory and Postgres atomically.

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (after `load_episodes` ~238)
- Modify: `pseudolife_memory/service.py` (new `episode_prune_empty`)
- Modify: `pseudolife_memory/web/routes.py` (register `POST /api/episodes/prune`)
- Test: `tests/test_pg_storage.py`, `tests/test_episode_service.py`

**Interfaces:**
- Produces:
  - `PostgresStorage.delete_episode(episode_id: str) -> None`
  - `MemoryService.episode_prune_empty(include_open: bool = False) -> {"deleted": int, "ids": list[str]}`
  - REST `POST /api/episodes/prune` body `{"include_open": false}`.

- [ ] **Step 1: Write failing tests.**

`tests/test_pg_storage.py` (follow the file's existing PG fixture):

```python
def test_delete_episode_removes_row(pg_storage):
    pg_storage.upsert_episode({
        "id": "ep-del", "title": "t", "hint": None, "started_at": 1.0,
        "ended_at": 2.0, "closed_by_new_start": False,
        "session_key": "k", "parent_id": None})
    assert any(e["id"] == "ep-del" for e in pg_storage.load_episodes())
    pg_storage.delete_episode("ep-del")
    assert not any(e["id"] == "ep-del" for e in pg_storage.load_episodes())
```

`tests/test_episode_service.py`:

```python
def test_prune_empty_deletes_only_entryless_closed(service):
    service.episode_start_session("KEEP", "has-entry")
    from pseudolife_memory.writer_context import set_writer_context, reset_writer_context
    tok = set_writer_context("w", "KEEP")
    try:
        service.store("durable", source="claude")
    finally:
        reset_writer_context(tok)
    service.episode_end_session("KEEP", run_dream=False)
    # An empty, CLOSED husk:
    service.episode_start_session("DROP", "empty")
    service._cms.episodes.end_session("DROP")  # close without prune-on-close
    out = service.episode_prune_empty(include_open=False)
    titles = [e["title"] for e in service.episode_list(include_open=True)["episodes"]]
    assert "has-entry" in titles
    assert "empty" not in titles
    assert out["deleted"] >= 1


def test_prune_empty_keeps_open_session_by_default(service):
    service.episode_start_session("OPEN", "live-open")  # open, 0 entries
    out = service.episode_prune_empty(include_open=False)
    titles = [e["title"] for e in service.episode_list(include_open=True)["episodes"]]
    assert "live-open" in titles  # not deleted while open
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_pg_storage.py -k delete_episode tests/test_episode_service.py -k prune_empty -v`
Expected: FAIL (`delete_episode` / `episode_prune_empty` missing).

- [ ] **Step 3: Implement.**

`pseudolife_memory/storage/postgres.py`, after `load_episodes`:

```python
    def delete_episode(self, episode_id: str) -> None:
        self.conn.execute("DELETE FROM episodes WHERE id = %s", (episode_id,))
        self.conn.commit()
```

`pseudolife_memory/service.py`:

```python
    def episode_prune_empty(self, include_open: bool = False) -> dict[str, Any]:
        """Delete episodes that have zero attached entries. By default only
        CLOSED ones (the currently-open session episodes are live and kept).
        Returns the count + ids deleted."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            counts = self._episode_entry_counts()
            em = self._cms.episodes
            victims = [
                e.id for e in list(em.episodes.values())
                if counts.get(e.id, 0) == 0
                and (include_open or e.ended_at is not None)
            ]
            for i in victims:
                em.remove(i)
                self._delete_episode_row(i)
            return {"deleted": len(victims), "ids": victims}
```

`pseudolife_memory/web/routes.py`, in the `# ---- episodes ----` block after the `/api/episode/end` registration:

```python
        p("/api/episodes/prune", lambda q, b: svc.episode_prune_empty(
            include_open=bool(b.get("include_open", False))))
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_pg_storage.py -k delete_episode tests/test_episode_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/postgres.py pseudolife_memory/service.py pseudolife_memory/web/routes.py tests/test_pg_storage.py tests/test_episode_service.py
git commit -m "feat(episodes): delete_episode + bulk prune-empty REST route"
```

---

### Task 5: `writer_context` prefers the stable `X-PL-Session` header

Make the schema-v11 `session_id` (and thus the stamping key) stable per session instead of per-call.

**Files:**
- Modify: `pseudolife_memory/writer_context.py` (`_http_writer_session` ~45)
- Test: `tests/` — add `tests/test_writer_context.py` (new) or extend an existing writer test.

**Interfaces:**
- Produces: `_http_writer_session()` returns `(x-pl-writer, x-pl-session or mcp-session-id)`.

- [ ] **Step 1: Write failing test** — `tests/test_writer_context.py`:

```python
from pseudolife_memory import writer_context as wc


class _Req:
    def __init__(self, headers):
        self.headers = headers


class _Ctx:
    def __init__(self, req):
        self.request = req


def test_prefers_x_pl_session_over_mcp_session_id(monkeypatch):
    import mcp.server.lowlevel.server as srv
    req = _Req({"x-pl-writer": "w1", "x-pl-session": "stable-1",
                "mcp-session-id": "per-call-9"})
    monkeypatch.setattr(srv.request_ctx, "get", lambda: _Ctx(req))
    assert wc._http_writer_session() == ("w1", "stable-1")


def test_falls_back_to_mcp_session_id(monkeypatch):
    import mcp.server.lowlevel.server as srv
    req = _Req({"x-pl-writer": "w1", "mcp-session-id": "per-call-9"})
    monkeypatch.setattr(srv.request_ctx, "get", lambda: _Ctx(req))
    assert wc._http_writer_session() == ("w1", "per-call-9")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_writer_context.py -v`
Expected: FAIL on the first test (currently returns `mcp-session-id`).

- [ ] **Step 3: Implement** — in `pseudolife_memory/writer_context.py`, change the return line of `_http_writer_session`:

```python
        return (
            headers.get("x-pl-writer"),
            headers.get("x-pl-session") or headers.get("mcp-session-id"),
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_writer_context.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/writer_context.py tests/test_writer_context.py
git commit -m "feat(writer): stable X-PL-Session header preferred over per-call mcp-session-id"
```

---

### Task 6: Shared title helper with time + `session` title util

Centralize title derivation (used by both the shim and the legacy CLI) and add the `HH:MM` disambiguator.

**Files:**
- Create: `pseudolife_memory/session_title.py`
- Modify: `pseudolife_memory/episode_cli.py` (import the shared helpers; keep CLI working)
- Test: `tests/test_session_title.py` (new)

**Interfaces:**
- Produces:
  - `session_title.git_project_name(cwd: str | None) -> str | None`
  - `session_title.title_from_cwd(cwd: str | None, now: float | None = None) -> str` → `"{project} - {YYYY-MM-DD HH:MM}"`.

- [ ] **Step 1: Write failing test** — `tests/test_session_title.py`:

```python
import os
from pseudolife_memory.session_title import title_from_cwd, git_project_name


def test_title_includes_date_and_time():
    t = title_from_cwd(None, now=1782780930.0)  # fixed epoch
    # Format: "<name> - YYYY-MM-DD HH:MM"; name falls back to 'session'.
    assert t.startswith("session - ")
    head, stamp = t.split(" - ", 1)
    assert len(stamp) == len("2026-06-30 14:32")
    assert stamp[4] == "-" and stamp[10] == " " and stamp[13] == ":"


def test_git_project_name_finds_repo_root(tmp_path):
    repo = tmp_path / "MyProj"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert git_project_name(str(sub)) == "MyProj"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_session_title.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement** — `pseudolife_memory/session_title.py` (move the logic out of `episode_cli.py`, add time):

```python
"""Shared session-episode title derivation. Torch-free (stdlib only) so both
the stdio shim and the episode CLI can import it without pulling in the heavy
service stack."""
from __future__ import annotations

import os
import time


def git_project_name(cwd: str | None) -> str | None:
    """Nearest git-repo-root basename walking up from ``cwd``; None if not in a repo."""
    if not cwd:
        return None
    try:
        path = os.path.abspath(cwd)
    except Exception:  # noqa: BLE001
        return None
    prev = ""
    while path and path != prev:
        if os.path.isdir(os.path.join(path, ".git")):
            return os.path.basename(path) or None
        prev, path = path, os.path.dirname(path)
    return None


def title_from_cwd(cwd: str | None, now: float | None = None) -> str:
    """``"{project} - {YYYY-MM-DD HH:MM}"``. Project = git-root name, else cwd
    basename, else ``session``. Never titles after the home directory."""
    name = git_project_name(cwd)
    if not name and cwd:
        norm = os.path.normpath(cwd)
        try:
            is_home = os.path.abspath(norm) == os.path.abspath(os.path.expanduser("~"))
        except Exception:  # noqa: BLE001
            is_home = False
        if not is_home:
            name = os.path.basename(norm) or None
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    return f"{name or 'session'} - {stamp}"
```

In `pseudolife_memory/episode_cli.py`, delete the local `_git_project_name`/`_title_from_cwd` and import the shared ones:

```python
from pseudolife_memory.session_title import title_from_cwd as _title_from_cwd  # noqa: F401
```

(Update `run_episode` to call `_title_from_cwd(payload_in.get("cwd"))` — signature unchanged.)

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_session_title.py tests/test_episode_cli.py -v`
Expected: PASS (existing `test_episode_cli.py` still green; if it asserted the old date-only title, update that assertion to the new format).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/session_title.py pseudolife_memory/episode_cli.py tests/test_session_title.py
git commit -m "feat(episodes): shared title helper with HH:MM disambiguator"
```

---

### Task 7: Shim owns session identity + lifecycle

Mint a per-session id, inject it on every call, and open/close the episode from the shim.

**Files:**
- Modify: `pseudolife_memory/shim.py`
- Test: `tests/test_shim.py` (new or existing)

**Interfaces:**
- Consumes: `session_title.title_from_cwd` (Task 6); REST `/api/episode/{start,end}` (existing); `X-PL-Session` read by `writer_context` (Task 5).
- Produces: every upstream MCP call carries `X-PL-Session: <uid>`; a `/api/episode/start` fires once at startup and `/api/episode/end` once at shutdown.

- [ ] **Step 1: Write failing test** — `tests/test_shim.py` (unit-test the header builder + a `_post_episode` best-effort wrapper; do not spin a real daemon):

```python
from pseudolife_memory import shim


def test_session_headers_include_writer_and_session(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "writer-7")
    headers = shim._session_headers(token="tok", session_uid="uid-123")
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-PL-Writer"] == "writer-7"
    assert headers["X-PL-Session"] == "uid-123"


def test_post_episode_is_best_effort(monkeypatch):
    # A connection error must be swallowed (never break the session).
    def boom(*a, **k):
        raise OSError("daemon down")
    monkeypatch.setattr(shim.urllib.request, "urlopen", boom)
    # Should not raise:
    shim._post_episode("http://127.0.0.1:8765", None, "/api/episode/start",
                       {"session_key": "x", "title": "t"})
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_shim.py -v`
Expected: FAIL (`_session_headers` / `_post_episode` missing).

- [ ] **Step 3: Implement** — in `pseudolife_memory/shim.py`:

Add near the top (after imports):

```python
import json as _json
import uuid as _uuid
from pseudolife_memory.session_title import title_from_cwd


def _session_headers(token: str | None, session_uid: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    writer_id = os.environ.get("PSEUDOLIFE_WRITER_ID")
    if writer_id:
        headers["X-PL-Writer"] = writer_id
    headers["X-PL-Session"] = session_uid
    return headers


def _post_episode(url: str, token: str | None, path: str, payload: dict) -> None:
    """Best-effort REST call to open/close the session episode. Swallows all
    errors so episode bookkeeping can never break or slow a session."""
    try:
        data = _json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url + path, data=data, method="POST")
        req.add_header("content-type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except Exception:  # noqa: BLE001
        pass
```

Refactor `_proxy` to take `session_uid` and build headers via `_session_headers` (replace the inline `headers`/`writer_id` block at lines ~89-96):

```python
async def _proxy(url: str, token: str | None, session_uid: str) -> None:
    ...
    headers = _session_headers(token, session_uid)
    ...
```

Rewrite `run_shim`:

```python
def run_shim() -> None:
    import asyncio

    url = _daemon_url()
    ensure_daemon(url)
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None
    session_uid = _uuid.uuid4().hex
    # Open the session episode (best-effort). The shim is the single per-session
    # process, so this uid keys BOTH lifecycle and per-store stamping.
    _post_episode(url, token, "/api/episode/start", {
        "session_key": session_uid,
        "title": title_from_cwd(os.getcwd()),
    })
    try:
        asyncio.run(_proxy(url, token, session_uid))
    except KeyboardInterrupt:  # session closed
        pass
    finally:
        _post_episode(url, token, "/api/episode/end",
                      {"session_key": session_uid})
```

- [ ] **Step 4: Run to verify pass + full suite**

Run: `pytest tests/test_shim.py -v && pytest -q`
Expected: PASS (whole suite green).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/shim.py tests/test_shim.py
git commit -m "feat(shim): own per-session id + episode lifecycle (X-PL-Session)"
```

---

### Task 8: Remove the now-redundant episode hooks

The shim owns lifecycle; the SessionStart `episode-start` and SessionEnd `episode-end` hooks would now double-open/close with a *different* (Claude session_id) key. Remove them; keep the briefing + context hooks.

**Files:**
- Modify: `C:\Users\<user>\.claude\settings.json`
- Modify: `README.md` (drop the episode-hook snippet; note the shim now owns lifecycle)

- [ ] **Step 1:** Remove the third `SessionStart` hook object (`pseudolife-mcp episode-start`) and the entire `SessionEnd` block from `settings.json`. Leave the static-context echo and `pseudolife-mcp briefing --hook-json` hooks intact.

- [ ] **Step 2:** Update `README.md`'s hook section: keep the briefing hook; replace the episode-hook instructions with one line — "Episode lifecycle is owned by the shim automatically; no episode hooks required."

- [ ] **Step 3: Commit** (repo file only; `settings.json` is outside the repo):

```bash
git add README.md
git commit -m "docs: shim owns episode lifecycle; drop episode hooks"
```

---

### Task 9: Deploy daemon + one-shot prune + verify

**Files:** none (operational). Follows the project's daemon-only deploy procedure.

- [ ] **Step 1: Full suite green**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 2: Back up the live bank** (the DB has a prior wipe incident — back up first):

```bash
pwsh -File ops/backup.ps1   # or the documented pg_dump -> pseudolife_memory-YYYYMMDD-HHMMSS.sql.gz
```

- [ ] **Step 3: Tag a rollback image, then build + deploy daemon-only** (do not touch the pg/extractor services or volumes):

```bash
docker tag pseudolife-daemon:latest pseudolife-daemon:pre-session-episodes
docker compose -f ops/docker-compose.yml build daemon
docker compose -f ops/docker-compose.yml up -d --no-deps daemon
```

- [ ] **Step 4: Health check** — confirm the daemon is up and schema is intact:

```bash
curl -s http://127.0.0.1:8765/health
```
Expected: healthy; schema version unchanged (no migration in this change).

- [ ] **Step 5: One-shot prune of existing junk** (aggressive: every CLOSED entry-less episode; the live open session is kept):

```bash
curl -s -X POST http://127.0.0.1:8765/api/episodes/prune \
  -H 'content-type: application/json' \
  -H "Authorization: Bearer $PSEUDOLIFE_MCP_TOKEN" \
  -d '{"include_open": false}'
```
Expected: `{"deleted": N, "ids": [...]}` with N ≈ the empty-husk count.

- [ ] **Step 6: Verify** — list episodes; confirm only entry-bearing + the current open session remain, titles carry `HH:MM`, no `closed_by_new_start` clobber chain across keys going forward:

```bash
curl -s "http://127.0.0.1:8765/api/episodes?limit=100" -H "Authorization: Bearer $PSEUDOLIFE_MCP_TOKEN"
```

- [ ] **Step 7: Commit CHANGELOG**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for session-scoped episodes + prune"
```

---

## Self-Review

- **Spec coverage:** misnaming → Task 6 (title+time) & Task 7 (shim title); missing entries / mis-attribution → Tasks 1, 2, 5 (session-keyed stamping); irrelevant/empty husks → Task 3 (prune-on-close) & Task 4/9 (bulk prune); cross-session clobber → Task 1 (`start_session` no clobber) & Task 7 (shim lifecycle); cleanup of existing junk → Task 4 + Task 9. Hooks removal → Task 8. ✔
- **Type consistency:** `start_session(title, session_key, hint)`, `stamp(entry, session_key)`, `start_nested(title, hint, session_key)`, `end_leaf(session_key)`, `open_leaf_for(session_key)`, `remove(id)`, `episode_prune_empty(include_open)`, `delete_episode(id)`, `_session_headers(token, session_uid)`, `_post_episode(url, token, path, payload)`, `title_from_cwd(cwd, now)` — used consistently across tasks. ✔
- **Placeholder scan:** every code step shows real code; commands have expected output. ✔
- **Open risk to watch during execution:** confirm the exact arg lists at the two secondary `self._cms.store(...)` call sites (service.py ~838, ~2256) before editing; confirm `tests/test_episode_cli.py` / `tests/test_episodes.py` legacy assertions and update any that encoded the old date-only title or the old clobber-on-`start` behavior.
