# Session Identity Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace connection-scoped session keying (`mcp-session-id`, removed from MCP on 2026-07-28) with a five-tier identity contract — shim header → episode handle → hook-registered session → legacy transport id → writer+idle-gap floor — plus ownership guards so no session can close another's episode, and the installer flips to the shim by default.

**Architecture:** `writer_context.py` learns to report header vs transport session separately; `service._resolve_writer` (the single chokepoint all 12 call sites already use) applies the tiers with an in-memory active-session pointer persisted to `meta`. Write tools gain an optional `episode` handle. The plugin's SessionStart hook forwards Claude Code's `session_id`; a new SessionEnd hook closes deterministically. No schema change anywhere.

**Tech Stack:** Python stdlib in `pseudolife_memory/`, bash (curl/sed only — no host python) for plugin hooks, PowerShell+bash for installers, pytest with the `pg_service` fixture pattern (tests/pg_fixtures.py).

**Spec:** `docs/superpowers/specs/2026-07-18-session-identity-contract-design.md` — read it first. Verified seams: `service._resolve_writer` at service.py:384-388 wraps `resolve_writer` and feeds every `session_key` use; `episode_start_session` (service.py:2732) is idempotent per key and never closes other sessions; `episode_end_session(None)` (service.py:2750-2762) currently force-closes ANY open root — the blind pop; plugin hook is `plugin/hooks/session-start.sh` (curl-only, stdin unused today) wired via `plugin/hooks/hooks.json`; installers wire HTTP at ops/install.sh:257 and ops/install.ps1:243; the shim entry point is `pseudolife-mcp` (bare = shim mode, pyproject `[project.scripts]`); shim already sends per-process `X-PL-Session` (shim.py:94).

## Global Constraints

- **No DDL, no schema bump.** Pointer = `meta` key `active_session_pointer`, shape `{"session_id": str, "ts": float}`.
- Identity precedence (spec table, verbatim): `X-PL-Session` header → explicit `episode` tool argument (attribution; identity only when no header) → hook-registered active session → `mcp-session-id` (annotate as removed 2026-07-28) → writer+idle-gap floor.
- Fail-open everywhere: no identity-resolution or handle-validation error may fail a memory write; hook endpoints keep the "never break a session start" contract (session_hook.py docstring).
- Handle = OPEN root episode id or unambiguous prefix ≥8 chars; unknown/closed/ambiguous → warn-and-degrade (`episode_warning` in the result), never raise.
- Ownership guard: `episode_end_session`/`memory_episode_end` close only roots whose `session_key` equals the resolved identity; explicit `session_key` argument (REST/hook) remains an explicit target. No-match → `{"closed": None, "reason": "no owned open session"}`.
- Plugin hook scripts: bash + curl + sed only (no jq, no python — the script's own documented constraint).
- Tests offline except PG-backed ones (bench Postgres 127.0.0.1:5433, `pg_service`/fixtures from tests/test_outcome_inference.py + tests/pg_fixtures.py — never `pristine_service` for storage-touching tests).
- Commits conventional + trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`; pytest via `.venv\Scripts\python.exe -m pytest`.
- CHANGELOG entry lands in Task 8. Guard tests that pin hook/plugin content (tests/test_plugin_packaging.py) are updated deliberately with a watched RED, never loosened silently.

---

### Task 1: `writer_context.resolve_writer_detailed`

**Files:**
- Modify: `pseudolife_memory/writer_context.py`
- Test: `tests/test_session_identity.py` (new)

**Interfaces:**
- Produces: `resolve_writer_detailed(default_writer: str) -> tuple[str, str | None, str | None]` — `(writer_id, header_session, transport_session)`. An explicit `set_writer_context(w, s)` override maps `s` into the **header** slot (top identity tier — preserves test/direct-API semantics). `resolve_writer` becomes a compat wrapper returning `(writer, header or transport)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_identity.py`:

```python
"""Session identity contract (spec 2026-07-18): tier resolution units."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pseudolife_memory.writer_context import (
    reset_writer_context, resolve_writer, resolve_writer_detailed,
    set_writer_context,
)


def test_detailed_override_maps_to_header_slot():
    tok = set_writer_context("w1", "sessA")
    try:
        assert resolve_writer_detailed("dflt") == ("w1", "sessA", None)
        assert resolve_writer("dflt") == ("w1", "sessA")
    finally:
        reset_writer_context(tok)


def test_detailed_no_context_returns_default_and_nones():
    assert resolve_writer_detailed("dflt") == ("dflt", None, None)
    assert resolve_writer("dflt") == ("dflt", None)
```

- [ ] **Step 2: Run to verify failure** — `.venv\Scripts\python.exe -m pytest tests/test_session_identity.py -v` → ImportError (`resolve_writer_detailed`).

- [ ] **Step 3: Implement**

In `pseudolife_memory/writer_context.py`, replace `_http_writer_session` and `resolve_writer` with:

```python
def _http_writer_session_detailed() -> tuple[str | None, str | None, str | None]:
    """Best-effort ``(writer_id, header_session, transport_session)`` from the
    live MCP request. ``header_session`` is the integrator-asserted
    ``X-PL-Session`` (identity tier 1); ``transport_session`` is the
    transport's ``mcp-session-id`` — per-CONNECTION in multiplexing clients,
    and REMOVED from the MCP spec in the 2026-07-28 revision (SEP-2567), so
    it is a legacy fallback only."""
    try:
        from mcp.server.lowlevel.server import request_ctx

        req = getattr(request_ctx.get(), "request", None)
        if req is None:
            return (None, None, None)
        headers = req.headers
        return (headers.get("x-pl-writer"), headers.get("x-pl-session"),
                headers.get("mcp-session-id"))
    except Exception:  # noqa: BLE001  (LookupError when unset; ImportError; ...)
        return (None, None, None)


def resolve_writer_detailed(
        default_writer: str) -> tuple[str, str | None, str | None]:
    """``(writer_id, header_session, transport_session)`` for this request.
    An explicit override binds its session into the HEADER slot — overrides
    are the strongest assertion we have."""
    w, s = _WRITER_CTX.get()
    if w is not None:
        return (w, s, None)
    hw, hs, ts = _http_writer_session_detailed()
    return (hw or default_writer, hs, ts)


def resolve_writer(default_writer: str) -> tuple[str, str | None]:
    """Compat wrapper: ``(writer_id, session)`` with the pre-contract merge
    (header wins over transport). Prefer ``resolve_writer_detailed``."""
    w, hs, ts = resolve_writer_detailed(default_writer)
    return (w, hs or ts)
```

(Delete the old `_http_writer_session`; nothing else imports it — verify with a grep and report if something does.)

- [ ] **Step 4: Run to verify pass** — 2 passed; also `.venv\Scripts\python.exe -m pytest tests/ -k writer -q` for existing writer-context consumers.
- [ ] **Step 5: Commit** — `feat(identity): resolve_writer_detailed — header vs transport session split` (+ trailer).

---

### Task 2: service tiering + active-session pointer

**Files:**
- Modify: `pseudolife_memory/service.py` (`_resolve_writer` at :384-388, init-time pointer load near `_ensure_init`'s storage-ready section, new public pointer methods)
- Test: `tests/test_session_identity.py` (append)

**Interfaces:**
- Consumes: `resolve_writer_detailed` (Task 1); `get_meta`/`set_meta` (postgres.py:624-635).
- Produces (service):
  - `set_active_session(session_id: str | None) -> None` — sets the in-memory pointer `self._active_session: tuple[str, float] | None` and persists `{"session_id", "ts"}` to meta key `active_session_pointer` (or clears both on None). Takes the lock itself.
  - `clear_active_session(session_id: str) -> bool` — clears ONLY if the pointer currently names `session_id`; returns whether it cleared. Takes the lock.
  - `_resolve_writer()` — tier logic: header session → pointer → transport session → None. (Tier 2, the handle, is applied at the write call sites in Task 4 — identity and target episode are separable per the spec.)
- Pointer loads from meta once storage is ready (inside `_ensure_init`'s storage-initialised branch — locate the point where `self._storage` is set up and hydrated, add the load there); file-mode (no storage) keeps a process-local pointer only.

- [ ] **Step 1: Write the failing tests** (append; reuse the `pg_service` fixture by importing the module’s pattern — read tests/test_outcome_inference.py's fixture and reuse/share it via a small local copy or import):

```python
def test_resolver_prefers_header_then_pointer_then_transport(pg_service):
    svc = pg_service
    svc.set_active_session("hookSess")
    tok = set_writer_context("w", "headerSess")
    try:
        assert svc._resolve_writer() == ("w", "headerSess")
    finally:
        reset_writer_context(tok)
    # no header: pointer wins (transport can't be simulated without the MCP
    # request context — its tier is covered by resolve_writer_detailed units)
    assert svc._resolve_writer()[1] == "hookSess"
    svc.set_active_session(None)
    assert svc._resolve_writer()[1] is None


def test_pointer_persists_and_clear_only_if_owner(pg_service):
    svc = pg_service
    svc.set_active_session("s1")
    assert svc._storage.get_meta("active_session_pointer")["session_id"] == "s1"
    assert svc.clear_active_session("someone-else") is False
    assert svc._resolve_writer()[1] == "s1"
    assert svc.clear_active_session("s1") is True
    assert svc._resolve_writer()[1] is None
    assert svc._storage.get_meta("active_session_pointer") is None
```

- [ ] **Step 2: Run to verify failure** — AttributeError `set_active_session`.
- [ ] **Step 3: Implement**

```python
    _ACTIVE_SESSION_META_KEY = "active_session_pointer"

    def set_active_session(self, session_id: str | None) -> None:
        """Machine-scoped active-session pointer (identity tier 3): set by
        the SessionStart hook, cleared by SessionEnd. Last-start-wins by
        design — concurrent unheaded sessions are the shim's/handle's job."""
        import time as _t
        with self._lock:
            self._ensure_init()
            if session_id:
                self._active_session = (str(session_id), _t.time())
                if self._storage is not None:
                    self._storage.set_meta(
                        self._ACTIVE_SESSION_META_KEY,
                        {"session_id": str(session_id),
                         "ts": self._active_session[1]})
            else:
                self._active_session = None
                if self._storage is not None:
                    self._storage.set_meta(self._ACTIVE_SESSION_META_KEY, None)

    def clear_active_session(self, session_id: str) -> bool:
        with self._lock:
            self._ensure_init()
            cur = getattr(self, "_active_session", None)
            if cur is None or cur[0] != session_id:
                return False
        self.set_active_session(None)
        return True
```

Caution: `set_active_session` must not be called while the lock is held (non-reentrant) — `clear_active_session` above releases before delegating; keep that shape. `_resolve_writer` becomes:

```python
    def _resolve_writer(self) -> tuple[str, str | None]:
        """Identity tiers 1/3/4 (spec 2026-07-18): X-PL-Session header ->
        hook-registered active session -> legacy mcp-session-id (removed
        from MCP 2026-07-28). Tier 2 (episode handle) is per-call at the
        write sites; tier 5 is the reaper's idle-gap floor."""
        w, header_s, transport_s = resolve_writer_detailed(self._writer_id)
        if header_s:
            return (w, header_s)
        active = getattr(self, "_active_session", None)
        if active is not None:
            return (w, active[0])
        return (w, transport_s)
```

Update the import at the top of service.py (`resolve_writer` → `resolve_writer_detailed`; keep `resolve_writer` import only if still used elsewhere in the file — grep). Add the meta load where `_ensure_init` finishes storage setup: `raw = self._storage.get_meta(self._ACTIVE_SESSION_META_KEY)` → set `self._active_session` if `isinstance(raw, dict) and raw.get("session_id")`. Initialise `self._active_session = None` in `__init__`.

Also verify `set_meta(key, None)` round-trips as SQL-null-or-JSON-null and `get_meta` returns `None` for it — if JSON `null` comes back as Python `None` already, nothing to do; if it comes back as the string "null" or raises, normalise inside `set_active_session` by using a sentinel `{}` and treating falsy as cleared. State in the report which case applied.

- [ ] **Step 4: Run to verify pass** — new tests + `tests/ -k "writer or episode_service" -q` for regressions.
- [ ] **Step 5: Commit** — `feat(identity): tiered session resolution + persistent active-session pointer` (+ trailer).

---

### Task 3: ownership guards on both close paths

**Files:**
- Modify: `pseudolife_memory/service.py` (`episode_end_session` :2750-2769; the `memory_episode_end` service path — find `def episode_end(` and its root-close fallthrough by reading it)
- Test: `tests/test_session_identity.py` (append)

**Interfaces:**
- Consumes: `_resolve_writer` (Task 2), `_close_session_locked` (unchanged), `episode_start_session`.
- Produces: `episode_end_session(session_key=None)` → when `session_key` is None it uses the resolved identity; if that is None or no open root carries it, returns `{"closed": None, "reason": "no owned open session"}` and closes nothing. An explicit non-None `session_key` remains an explicit target (REST/hook/shim path — unchanged). The `episode_end` (sub-episode pop) root-fallthrough closes a root ONLY if its `session_key` matches the resolved identity, else the same no-op dict.

- [ ] **Step 1: Write the failing tests** (this is the observed-pop reproduction):

```python
def test_end_session_never_pops_foreign_root(pg_service):
    svc = pg_service
    svc.episode_start_session("victim-key", "victim session")
    svc.store("victim entry", source="t")          # non-empty -> survives close-prune
    tok = set_writer_context("w", None)            # resolver yields no identity
    try:
        res = svc.episode_end_session(None)
        assert res == {"closed": None, "reason": "no owned open session"}
    finally:
        reset_writer_context(tok)
    tok = set_writer_context("w", "attacker-key")  # identity that owns nothing
    try:
        res = svc.episode_end_session(None)
        assert res == {"closed": None, "reason": "no owned open session"}
    finally:
        reset_writer_context(tok)
    tok = set_writer_context("w", "victim-key")    # the owner can close it
    try:
        res = svc.episode_end_session(None)
        assert res.get("id")
    finally:
        reset_writer_context(tok)


def test_episode_end_fallthrough_guarded(pg_service):
    svc = pg_service
    svc.episode_start_session("victim-key", "victim session")
    svc.store("victim entry", source="t")
    tok = set_writer_context("w", "attacker-key")
    try:
        res = svc.episode_end()               # no open sub-episode for attacker
        assert res.get("closed") is None      # must NOT pop victim's root
    finally:
        reset_writer_context(tok)
```

(If `svc.store(...)`'s real signature differs — read service.py:611 — adapt the two store calls only; the assertions are normative. If `episode_end`'s no-op return shape differs today, preserve its existing shape for the true no-op and add the guard branch returning the reason dict; state what you found.)

- [ ] **Step 2: Run to verify failure** — the attacker-key case closes the victim root today (blind pop).
- [ ] **Step 3: Implement** — in `episode_end_session`, before taking the lock:

```python
        if session_key is None:
            _, session_key = self._resolve_writer()
        if session_key is None:
            return {"closed": None, "reason": "no owned open session"}
```

and inside `_close_session_locked` no change (it already matches by key; with a real key it can only close that key's root — verify `end_session(session_key)` in episodes.py matches by key, not newest-any; if it force-closes any root on a non-matching key, fix THERE by returning None when no root matches the key, which also fixes the reaper path uniformly — read `Episodes.end_session` and report which variant you found). In the `episode_end` fallthrough: where it currently reaches for a session root after finding no open sub-episode, compare `root.session_key` to `self._resolve_writer()[1]` and return `{"closed": None, "reason": "no owned open session"}` on mismatch.

- [ ] **Step 4: Run to verify pass** — new tests + `tests/test_episode_service.py tests/test_episodes.py -q` (reaper + shim close paths must stay green).
- [ ] **Step 5: Commit** — `fix(episodes): ownership guard — a session can only close its own root` (+ trailer).

---

### Task 4: `episode` handle on write tools (tier 2)

**Files:**
- Modify: `pseudolife_memory/service.py` (`store` :611-area, `record_outcome` :1567-area, `fact_set` — find `def fact_set(`), new `_resolve_episode_handle`
- Modify: `pseudolife_memory/mcp_server.py` (`memory_store`, `memory_outcome`, `memory_fact_set` signatures + docstring line each)
- Test: `tests/test_session_identity.py` (append)

**Interfaces:**
- Produces: `service._resolve_episode_handle(handle: str | None) -> tuple[str, str | None] | None` — caller holds the lock; `(episode_id, session_key)` for an OPEN root whose id equals the handle or starts with it (prefix ≥8 chars, exactly one match); else `None`.
- `store(..., episode: str | None = None)`, `record_outcome(..., episode: str | None = None)`, `fact_set(..., episode: str | None = None)`: valid handle → the write attributes to that episode (`episode_id` on the entry/signal; `session_key` stamp on fact writes) and, when no header session is present, the resolved identity for the call becomes that episode's `session_key`; invalid → proceed under normal tiers, add `"episode_warning": "unknown or closed episode handle"` to the result dict.
- MCP tool params: `episode: str | None = None` with docstring: "Optional episode handle (id or ≥8-char prefix) from the session briefing — attributes this write to that session's episode; useful when running concurrent sessions."

- [ ] **Step 1: Write the failing tests**

```python
def test_store_with_valid_handle_attributes_and_keys(pg_service):
    svc = pg_service
    ep = svc.episode_start_session("keyA", "session A")
    res = svc.store("handled entry", source="t", episode=ep["id"][:12])
    assert "episode_warning" not in res
    found = [e for band in svc._cms.bands for e in band.entries
             if e.text == "handled entry"]
    assert found and found[0].episode_id == ep["id"]


def test_store_with_bad_handle_warns_and_degrades(pg_service):
    svc = pg_service
    res = svc.store("degraded entry", source="t", episode="nope-not-real")
    assert res["episode_warning"] == "unknown or closed episode handle"
    assert res.get("stored") is not None      # the write itself succeeded


def test_outcome_with_handle_lands_on_episode(pg_service):
    svc = pg_service
    ep = svc.episode_start_session("keyB", "session B")
    svc.record_outcome(task="t", outcome="success", episode=ep["id"][:12])
    sigs = [s for s in svc._storage.pending_signals(limit=100)
            if s.get("episode_id") == ep["id"]]
    assert len(sigs) == 1


def test_short_prefix_rejected(pg_service):
    svc = pg_service
    ep = svc.episode_start_session("keyC", "session C")
    res = svc.store("short prefix", source="t", episode=ep["id"][:4])
    assert res["episode_warning"] == "unknown or closed episode handle"
```

- [ ] **Step 2: Run to verify failure** — TypeError: unexpected keyword `episode`.
- [ ] **Step 3: Implement**

```python
    def _resolve_episode_handle(
            self, handle: str | None) -> tuple[str, str | None] | None:
        """Caller MUST hold the lock. Match an OPEN root episode by id or
        unambiguous prefix (>=8 chars). None on any miss — callers
        warn-and-degrade, never raise (a stale handle must not lose a
        memory)."""
        if not handle or len(handle) < 8 or self._cms is None:
            return None
        matches = [e for e in self._cms.episodes.episodes.values()
                   if e.parent_id is None and e.ended_at is None
                   and e.id.startswith(handle)]
        if len(matches) != 1:
            return None
        return (matches[0].id, matches[0].session_key)
```

In `store`: accept `episode: str | None = None`; inside the locked section, resolve the handle once; on hit override the entry's `episode_id` (and the session used for the write's episode attribution/stamp when no header session exists — thread it through the same variable the lazy-episode logic uses; read the surrounding code and keep the change minimal); on miss set a local `episode_warning = True` and add the key to the result dict just before returning. Mirror the same pattern in `record_outcome` (pass `episode_id=` to `add_signal` on hit) and `fact_set` (use the handle's `session_key` for the stamp on hit). In `mcp_server.py`, add the parameter + docstring line to the three tools and pass through.

- [ ] **Step 4: Run to verify pass** — new tests + `tests/test_outcome_inference.py -q` (episode attribution consumers) + `tests/test_mcp_server.py -q`.
- [ ] **Step 5: Commit** — `feat(identity): episode handle on write tools — spec-blessed tier-2 attribution` (+ trailer).

---

### Task 5: hook endpoints — register on start, close on end, advertise the handle

**Files:**
- Modify: `pseudolife_memory/web/session_hook.py` (accept `session_id`/`source`, mint + advertise, register pointer)
- Modify: `pseudolife_memory/web/routes.py` (:131-area — pass query params through; add `POST /api/hook/session-end`)
- Test: `tests/test_session_identity.py` (append; read how existing session-hook endpoint tests drive the handler — grep `session-start` in tests/ — and use the same harness)

**Interfaces:**
- `GET /api/hook/session-start?session_id=<id>&source=<startup|resume|clear|compact>`: with a `session_id`, the daemon calls `svc.episode_start_session(session_id, "session")` (idempotent for resume/compact re-fires), `svc.set_active_session(session_id)`, and prepends ONE line to the briefing payload: `Session episode: <id-first-12> — pass episode="<id-first-12>" on memory writes when running concurrent sessions.` Without `session_id`: exactly today's behavior. Never raises; always 200.
- `POST /api/hook/session-end` with body `{"session_id": "..."}`: `svc.episode_end_session(session_id)` + `svc.clear_active_session(session_id)`; returns `{"ok": true}` always (errors logged).

- [ ] **Step 1: Write the failing tests** (shape per the existing hook-endpoint test harness; assertions normative):

```python
def test_hook_start_registers_and_advertises(pg_service):
    from pseudolife_memory.web.session_hook import hook_session_start
    text = hook_session_start(pg_service, session_id="claudeSess1",
                              source="startup")
    assert "Session episode:" in text
    assert pg_service._resolve_writer()[1] == "claudeSess1"
    # idempotent on resume
    text2 = hook_session_start(pg_service, session_id="claudeSess1",
                               source="resume")
    assert text.splitlines()[0] == text2.splitlines()[0]


def test_hook_end_closes_and_clears_only_owner(pg_service):
    from pseudolife_memory.web.session_hook import (
        hook_session_end, hook_session_start)
    hook_session_start(pg_service, session_id="claudeSess2", source="startup")
    pg_service.store("an entry", source="t")
    assert hook_session_end(pg_service, session_id="other") == {"ok": True}
    assert pg_service._resolve_writer()[1] == "claudeSess2"   # not cleared
    assert hook_session_end(pg_service, session_id="claudeSess2") == {"ok": True}
    assert pg_service._resolve_writer()[1] is None
```

(Adapt the exact entry-point names to session_hook.py's real structure — it may expose one build function the route lambda calls; keep the public names `hook_session_start(svc, session_id, source)` / `hook_session_end(svc, session_id)` as the new seam and have the routes call them.)

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** — in session_hook.py, wrap the existing briefing-building path: when `session_id` is truthy, inside a broad try/except (log + fall through to plain briefing): `ep = svc.episode_start_session(session_id, "session")`, `svc.set_active_session(session_id)`, prepend the advertisement line using `ep["id"][:12]`. `hook_session_end`: try/except around `svc.episode_end_session(session_id)` then `svc.clear_active_session(session_id)`, return `{"ok": True}` unconditionally. In routes.py, thread `_s(q, "session_id")`/`_s(q, "source")` into the existing GET lambda and add `p("/api/hook/session-end", lambda q, b: hook_session_end(svc, b.get("session_id")))` next to it.
- [ ] **Step 4: Run to verify pass** — new tests + whatever existing test pins the session-start payload (grep and run it; the advertisement line is additive — if a pin breaks, update it deliberately and say so).
- [ ] **Step 5: Commit** — `feat(hooks): session-start registers identity + advertises episode handle; session-end closes` (+ trailer).

---

### Task 6: plugin hook scripts

**Files:**
- Modify: `plugin/hooks/session-start.sh` (read stdin, forward `session_id`/`source`)
- Create: `plugin/hooks/session-end.sh`
- Modify: `plugin/hooks/hooks.json` (add SessionEnd)
- Test: `tests/test_plugin_packaging.py` (update pins with watched RED)

- [ ] **Step 1: RED first** — run `tests/test_plugin_packaging.py -q`, note green; make the script edits; run again and watch the pin fail (this is the deliberate guard-update discipline); then update the pinned expectations to the new content.
- [ ] **Step 2: session-start.sh** — after the existing `URL`/`AUTH` setup, replace the curl line with:

```bash
# Claude Code delivers hook input as JSON on stdin (session_id is a
# documented common field). curl+sed only — no jq/python on the host.
INPUT=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
SRC=$(printf '%s' "$INPUT" | sed -n 's/.*"source"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
QS=""
[ -n "$SID" ] && QS="?session_id=${SID}&source=${SRC}"
curl -sf --max-time 5 "${AUTH[@]}" "${URL}/api/hook/session-start${QS}" || \
    echo "Pseudolife-MCP: the memory daemon is not reachable at ${URL} — ..."
```

(keep the existing unreachable-message text verbatim; only the URL line changes). Session ids are UUID-shaped — no URL-encoding needed; do not add an encoder.

- [ ] **Step 3: session-end.sh** (new, same header conventions as session-start.sh):

```bash
#!/usr/bin/env bash
# Pseudolife-MCP SessionEnd hook — closes this session's episode promptly
# (the idle reaper remains the backstop). Must never block session end.
URL="${PSEUDOLIFE_MCP_DAEMON_URL:-http://127.0.0.1:8765}"
AUTH=()
if [ -n "${PSEUDOLIFE_MCP_TOKEN:-}" ]; then
    AUTH=(-H "Authorization: Bearer ${PSEUDOLIFE_MCP_TOKEN}")
fi
INPUT=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$INPUT" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
if [ -n "$SID" ]; then
    curl -sf --max-time 5 "${AUTH[@]}" -X POST \
        -H "content-type: application/json" \
        -d "{\"session_id\":\"${SID}\"}" \
        "${URL}/api/hook/session-end" >/dev/null 2>&1 || true
fi
exit 0
```

- [ ] **Step 4: hooks.json** — add alongside SessionStart (same structure):

```json
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/session-end.sh\"",
            "timeout": 10
          }
        ]
      }
    ]
```

- [ ] **Step 5: GREEN** — `tests/test_plugin_packaging.py -q` all pass with updated pins; `bash -n plugin/hooks/session-start.sh plugin/hooks/session-end.sh` parses clean.
- [ ] **Step 6: Commit** — `feat(plugin): hooks forward session_id; SessionEnd closes the episode` (+ trailer).

---

### Task 7: installer default flips to the shim

**Files:**
- Modify: `ops/install.sh` (:250-260 area) and `ops/install.ps1` (:236-246 area)

**Behavior (both installers, mirrored):** new `--transport shim|http` flag (default `shim`), parsed like the existing `--extractor` flag (read the file's arg parsing and mirror it). Shim path: if `pipx` exists → `pipx install pseudolife-mcp` (idempotent: `pipx upgrade` on already-installed); else if `python3`/`py -3` ≥3.10 exists → `python3 -m pip install --user pseudolife-mcp`; then `claude mcp remove pseudolife-memory 2>/dev/null; claude mcp add --scope user pseudolife-memory -- pseudolife-mcp`. If neither pipx nor python is found, print a WARNING naming the concurrent-session limitation and fall back to the existing HTTP `claude mcp add` line (which stays verbatim as the `--transport http` path). The success message states which transport was wired and why the shim matters ("per-session identity — required for correct episodes with concurrent sessions").

- [ ] **Step 1:** Implement in `ops/install.sh` (bash), mirroring existing style/flag parsing.
- [ ] **Step 2:** Mirror in `ops/install.ps1` (pwsh 7; `Get-Command pipx/python`), same messages.
- [ ] **Step 3:** Syntax checks: `bash -n ops/install.sh`; PS parser on install.ps1 (0 errors). Run `ops/preflight`-adjacent tests if any pin installer text (`grep -rn "claude mcp add" tests/` — update pins deliberately if hit).
- [ ] **Step 4: Commit** — `feat(install): shim is the default transport (per-session identity); http opt-out` (+ trailer).

---

### Task 8: docs + CHANGELOG

**Files:**
- Modify: `docs/guide/configuration.md` (shim section: reframe from "host-process installs only" to the identity story; new "Session identity" subsection with the five-tier table), `docs/guide/episodes.md` (keying paragraph: hook-registered identity, handle advertisement, ownership guard, sessionless-MCP note), `README.md` (transport bullets only: shim-first for session identity; HTTP for single-session setups — minimal edit, full narrative pass deferred to the release cut), `CHANGELOG.md`.

- [ ] **Step 1:** episodes.md + configuration.md edits per the spec's terms (five tiers, precedence rationale, last-start-wins limitation, `mcp-session-id` removal note with the SEP-2567 citation).
- [ ] **Step 2:** README minimal transport edit. Note in the commit body: README narrative changed → bump `docs/i18n/README.source.md` version marker **at the next release cut** per CLAUDE.md release step 0 (do NOT re-run translations now).
- [ ] **Step 3:** CHANGELOG under `[Unreleased]`, house style:

```markdown
### Changed (2026-07-18 — session identity contract)
- **Episodes no longer key on the transport connection.** Five-tier
  identity: shim `X-PL-Session` → explicit `episode` handle on write tools
  (advertised in the session briefing) → hook-registered session (SessionStart
  now forwards Claude Code's `session_id`; new SessionEnd hook closes the
  episode promptly) → legacy `mcp-session-id` (removed from MCP 2026-07-28,
  SEP-2567) → writer+idle-gap floor. A session can no longer close another
  session's episode (`episode_end` ownership guard). The installer now wires
  the stdio shim by default (`--transport http` to opt out) — per-session
  identity for concurrent sessions.
```

- [ ] **Step 4:** `tests/test_release_ux.py -q` green.
- [ ] **Step 5: Commit** — `docs: session identity contract — guide, README transport note, changelog` (+ trailer).

---

### Task 9: full-suite verification

- [ ] `$env:HF_HUB_OFFLINE = "1"; .venv\Scripts\python.exe -m pytest tests/` with bench Postgres up — all green (state that PG tests ran). Fix anything red; commit fixes.

### Task 10: deploy + live verify (MAIN SESSION — not subagent work)

- [ ] **Step 1:** Deploy via `ops/update.ps1` (backup → rollback tag → daemon rebuild → health).
- [ ] **Step 2:** Switch the maintainer's own client to the shim: `pipx install pseudolife-mcp` (or pip --user), `claude mcp remove pseudolife-memory`, `claude mcp add --scope user pseudolife-memory -- pseudolife-mcp` — coordinate with the user (their live sessions restart on reconnect).
- [ ] **Step 3:** Live verify: start two concurrent sessions (main + a chip/worktree session); confirm via Console/psql that each gets a distinct root episode (distinct `session_key`s), that the briefing shows the `Session episode:` line, and that `memory_episode_end` in one session cannot close the other's root.
- [ ] **Step 4:** `memory_store` the deploy record + `memory_outcome` for the feature; ledger close.

---

## Self-review notes (done at plan-writing time)

- Spec coverage: tiers 1/3/4 (Tasks 1-2), tier 2 (Task 4), tier 5 unchanged-reaper (Task 3 verifies), ownership guards both paths (Task 3), hook registration + advertisement + SessionEnd (Tasks 5-6), shim default + fallback (Task 7), docs/i18n-deferral/CHANGELOG (Task 8), fail-open contract woven into Tasks 2/4/5 code, live two-session verify (Task 10). Out-of-scope items untouched.
- The two places implementers must adapt to code they read (store-signature details in Task 3/4 tests; session_hook's internal structure in Task 5) are bounded: assertions and public seam names are fixed, discovery is named.
- Type consistency: `hook_session_start(svc, session_id, source) -> str`, `hook_session_end(svc, session_id) -> dict`, `set_active_session/clear_active_session`, `_resolve_episode_handle -> tuple[str, str | None] | None` used consistently across Tasks 2/4/5.
