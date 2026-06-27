# Memory Lifecycle Utilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the world-facts, lessons, and episodes subsystems populate and surface reliably — via session-lifecycle hooks (episodes), an extended session-start briefing (world facts + recap), and a rewritten global CLAUDE.md.

**Architecture:** Three mechanisms each own the part of the loop they can do reliably: harness-enforced **lifecycle hooks** open/close episodes through torch-free CLI verbs hitting new daemon REST endpoints; the existing **briefing** read-surface gains world-facts + "where we left off" blocks; the **CLAUDE.md** drives the judgment-call writes (`memory_outcome`, cited `memory_world_set`). Phase 1 ships session-floor episodes on proven plumbing; Phase 2 adds episode nesting.

**Tech Stack:** Python 3.11, FastMCP + raw-ASGI daemon, PostgreSQL (psycopg) for storage, `torch.save` snapshots, PowerShell 7 install scripts, pytest.

## Global Constraints

- **Never break session start/stop.** All CLI verbs invoked from hooks must swallow every exception and `exit 0` when the daemon is down (mirror `pseudolife_memory/briefing_cli.py`).
- **Schema changes are additive only.** New columns via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`; the `CREATE TABLE IF NOT EXISTS` body is also updated for fresh installs. Bump `SCHEMA_META_VERSION` in `pseudolife_memory/storage/schema.py` (currently `13`) by exactly 1 per phase that adds a column.
- **Deploy is daemon-only:** `docker compose build daemon && docker compose up -d daemon`. The postgres container and its volumes are NEVER touched. Back up first with `ops/backup.ps1`.
- **Hooks are idempotent**, keyed by Claude Code's `session_id` (passed as `session_key`), because `SessionStart` fires on `startup`/`resume`/`clear`/`compact`.
- **Torch-free CLI path:** new `episode_cli.py` imports nothing heavier than stdlib `urllib` + the existing `pseudolife_memory.shim` helpers (`_daemon_url`, `probe_health`). No torch at import time.
- **One claim per memory write; follow existing file style** (ASCII-only briefing output, existing docstring conventions).
- Spec: `docs/specs/2026-06-27-memory-lifecycle-utilization-design.md`.

---

## File Structure

| File | Phase | Responsibility |
|---|---|---|
| `pseudolife_memory/memory/episodes.py` | 1, 2 | `Episode.session_key` (1), `parent_id` + stack semantics (2) |
| `pseudolife_memory/service.py` | 1, 2 | `episode_start_session`/`episode_end_session` + fire-and-forget dream; `_episode_to_dict` passthrough; `session_briefing` world+recap; subtree episode filter (2) |
| `pseudolife_memory/storage/schema.py` | 1, 2 | `session_key` (1) / `parent_id` (2) columns; version bump |
| `pseudolife_memory/storage/postgres.py` | 1, 2 | `upsert_episode`/`load_episodes` column lists |
| `pseudolife_memory/web/routes.py` | 1 | `POST /api/episode/start`, `/api/episode/end`; `max_world` on `/api/briefing` |
| `pseudolife_memory/episode_cli.py` (new) | 1 | torch-free `episode-start`/`episode-end` verbs |
| `pseudolife_memory/cli.py` | 1 | dispatch the new modes |
| `pseudolife_memory/memory/briefing.py` | 1 | render `## Verified world facts` + `## Where we left off` |
| `pseudolife_memory/briefing_cli.py` | 1 | `--max-world` flag |
| `ops/install-hook.ps1` | 1 | register SessionStart episode-start + SessionEnd episode-end |
| `~/.claude/CLAUDE.md` (user global, out of repo) | 1, 2 | RECALL→CAPTURE→REFLECT rewrite |
| `tests/test_episodes.py` | 1, 2 | manager-level unit tests |
| `tests/test_briefing.py` (new) | 1 | `format_briefing` rendering |
| `tests/test_episode_service.py` (new) | 1, 2 | service wrappers, persistence round-trip, subtree filter |
| `tests/test_episode_cli.py` (new) | 1 | daemon-down → exit 0 |

---

# PHASE 1 — session-floor episodes + read surfaces + CLAUDE.md

## Task 1: `session_key` on the episode model

**Files:**
- Modify: `pseudolife_memory/memory/episodes.py`
- Test: `tests/test_episodes.py`

**Interfaces:**
- Produces: `Episode.session_key: str | None`; `EpisodeManager.start(title, hint=None, session_key=None) -> Episode`; `EpisodeManager.open_episode() -> Episode | None` (the current open leaf, or None).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_episodes.py`:

```python
from pseudolife_memory.memory.episodes import EpisodeManager


def test_start_records_session_key_and_open_episode_helper():
    em = EpisodeManager()
    assert em.open_episode() is None
    ep = em.start(title="S", session_key="sess-abc")
    assert ep.session_key == "sess-abc"
    assert em.open_episode() is ep
    em.end()
    assert em.open_episode() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_episodes.py::test_start_records_session_key_and_open_episode_helper -v`
Expected: FAIL — `Episode.__init__() got an unexpected keyword argument 'session_key'` (or `AttributeError: open_episode`).

- [ ] **Step 3: Write minimal implementation**

In `episodes.py`, add the field to the `Episode` dataclass (append after `closed_by_new_start`):

```python
    closed_by_new_start: bool = False
    session_key: str | None = None
```

Extend `EpisodeManager.start` to accept and stamp `session_key`:

```python
    def start(self, title: str, hint: str | None = None,
              session_key: str | None = None) -> Episode:
        if self.current_id is not None:
            prior = self.episodes.get(self.current_id)
            if prior is not None and prior.ended_at is None:
                prior.ended_at = time.time()
                prior.closed_by_new_start = True

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
```

Add the helper (place after `end`):

```python
    def open_episode(self) -> Episode | None:
        """The current open leaf episode, or None when nothing is open."""
        if self.current_id is None:
            return None
        ep = self.episodes.get(self.current_id)
        return ep if (ep is not None and ep.ended_at is None) else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_episodes.py::test_start_records_session_key_and_open_episode_helper -v`
Expected: PASS.

- [ ] **Step 5: Run the full episodes suite (no regressions)**

Run: `pytest tests/test_episodes.py -v`
Expected: all PASS (existing tests unaffected — `session_key` defaults to None; `from_dict`'s unknown-key filter already tolerates the new field).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/episodes.py tests/test_episodes.py
git commit -m "feat(episodes): session_key field + open_episode() helper"
```

---

## Task 2: episode persistence carries `session_key`

**Files:**
- Modify: `pseudolife_memory/storage/schema.py` (DDL + ALTER + version bump)
- Modify: `pseudolife_memory/storage/postgres.py` (`upsert_episode`, `load_episodes`)
- Test: `tests/test_episode_service.py` (new) — round-trip via the storage layer

**Interfaces:**
- Consumes: `Episode.session_key` (Task 1).
- Produces: episodes table column `session_key TEXT`; `upsert_episode`/`load_episodes` persist and return it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_episode_service.py`. Postgres-backed tests use the `pg_conn`/`pg_url` fixtures from `tests/pg_fixtures.py` (same pattern as `tests/test_migration.py`): `pg_conn` ensures the schema and truncates tables; build the storage with `PostgresStorage(pg_url)`. These skip cleanly when no test Postgres is reachable.

```python
from dataclasses import asdict

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.memory.episodes import Episode
from pseudolife_memory.storage.postgres import PostgresStorage


def test_episode_session_key_round_trips(pg_conn, pg_url):
    storage = PostgresStorage(pg_url)
    ep = Episode(id="e1", title="S", started_at=1.0, session_key="sess-xyz")
    storage.upsert_episode(asdict(ep))           # episode_row(ep) == asdict(ep)
    rows = {r["id"]: r for r in storage.load_episodes()}
    assert rows["e1"]["session_key"] == "sess-xyz"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_episode_service.py::test_episode_session_key_round_trips -v`
Expected: FAIL — `psycopg` error `column "session_key" of relation "episodes" does not exist` (or KeyError on the loaded row).

- [ ] **Step 3: Write minimal implementation**

In `schema.py`, add the column to the `episodes` `CREATE TABLE` body (after `closed_by_new_start`):

```sql
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  hint TEXT,
  started_at DOUBLE PRECISION NOT NULL,
  ended_at DOUBLE PRECISION,
  closed_by_new_start BOOLEAN NOT NULL DEFAULT FALSE,
  session_key TEXT
);
```

In `schema.py` `ensure_schema`, add an additive ALTER beside the existing v13 `entries` ALTER (just after `cur.execute(SCHEMA_SQL)`):

```python
        # v14 additive: per-session idempotency key for hook-driven episodes.
        cur.execute(
            "ALTER TABLE episodes ADD COLUMN IF NOT EXISTS session_key TEXT"
        )
```

Bump the version constant:

```python
SCHEMA_META_VERSION = 14
```

In `postgres.py`, update `upsert_episode` to insert + update the new column:

```python
    def upsert_episode(self, ep: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO episodes (id, title, hint, started_at, ended_at,
                                  closed_by_new_start, session_key)
            VALUES (%(id)s, %(title)s, %(hint)s, %(started_at)s,
                    %(ended_at)s, %(closed_by_new_start)s, %(session_key)s)
            ON CONFLICT (id) DO UPDATE SET
              title = EXCLUDED.title,
              hint = EXCLUDED.hint,
              started_at = EXCLUDED.started_at,
              ended_at = EXCLUDED.ended_at,
              closed_by_new_start = EXCLUDED.closed_by_new_start,
              session_key = EXCLUDED.session_key
            """,
            ep,
        )
        self.conn.commit()
```

Note: `ep` is `episode_row(ep) == asdict(ep)`, which now contains `session_key`. Update `load_episodes` column list to include it:

```python
    def load_episodes(self) -> list[dict]:
        cols = ("id", "title", "hint", "started_at", "ended_at",
                "closed_by_new_start", "session_key")
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM episodes ORDER BY started_at",
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]
```

`sync.hydrate_cms` does `Episode(**ep)`; since `load_episodes` now returns exactly the dataclass fields (including `session_key`), the round-trip stays aligned — no change needed there.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_episode_service.py::test_episode_session_key_round_trips -v`
Expected: PASS.

- [ ] **Step 5: Run the migration suite (no regressions)**

Run: `pytest tests/test_migration.py -v`
Expected: all PASS (version bump + additive ALTER are backward-compatible).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/storage/schema.py pseudolife_memory/storage/postgres.py tests/test_episode_service.py
git commit -m "feat(episodes): persist session_key (schema v14, additive)"
```

---

## Task 3: service session wrappers + fire-and-forget dream

**Files:**
- Modify: `pseudolife_memory/service.py` (`_episode_to_dict`, new `episode_start_session`, `episode_end_session`, `_fire_and_forget_dream`)
- Test: `tests/test_episode_service.py`

**Interfaces:**
- Consumes: `EpisodeManager.start(session_key=...)`, `EpisodeManager.open_episode()` (Task 1); `self.dream_run`, `build_extractor` (existing).
- Produces:
  - `MemoryService.episode_start_session(session_key: str | None, title: str, hint: str | None = None) -> dict` — idempotent open.
  - `MemoryService.episode_end_session(session_key: str | None, run_dream: bool = True) -> dict` — guarded close + background dream.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_episode_service.py`. Service-level tests use the **file-mode** `pristine_service` fixture from `tests/conftest.py` (a cleared, embedder-warm `MemoryService`; `_storage is None`, so `_persist_episodes` is a no-op and these exercise the in-memory `EpisodeManager`). Alias it locally for brevity:

```python
def test_session_start_is_idempotent_per_key(pristine_service):
    service = pristine_service
    a = service.episode_start_session("sess-1", "Session A")
    b = service.episode_start_session("sess-1", "Session A")   # re-fire
    assert a["id"] == b["id"]                                   # no second episode


def test_session_end_matches_key_only(pristine_service):
    service = pristine_service
    service.episode_start_session("sess-1", "Session A")
    assert service.episode_end_session("other", run_dream=False) == {}   # no-op
    closed = service.episode_end_session("sess-1", run_dream=False)
    assert closed and closed["ended_at"] is not None
    assert service.episode_end_session("sess-1", run_dream=False) == {}   # nothing open
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_episode_service.py -k session -v`
Expected: FAIL — `AttributeError: 'MemoryService' object has no attribute 'episode_start_session'`.

- [ ] **Step 3: Write minimal implementation**

In `service.py`, extend `_episode_to_dict` to pass through `session_key` (so callers/tests can see it):

```python
    @staticmethod
    def _episode_to_dict(ep) -> dict[str, Any]:
        """Serialise an :class:`Episode` for MCP transport."""
        return {
            "id": ep.id,
            "title": ep.title,
            "started_at": ep.started_at,
            "ended_at": ep.ended_at,
            "hint": ep.hint,
            "closed_by_new_start": ep.closed_by_new_start,
            "session_key": getattr(ep, "session_key", None),
        }
```

Add the two wrappers next to `episode_start`/`episode_end`:

```python
    def episode_start_session(
        self, session_key: str | None, title: str, hint: str | None = None,
    ) -> dict[str, Any]:
        """Idempotent open for a hook-driven session episode.

        If an episode is already open with the same ``session_key`` (a
        resume/compact re-fire of SessionStart), return it unchanged.
        Otherwise open a new one stamped with the key (auto-closing any
        stale prior open episode, as ``start`` always has).
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            em = self._cms.episodes
            cur = em.open_episode()
            if (session_key is not None and cur is not None
                    and cur.session_key == session_key):
                return self._episode_to_dict(cur)
            ep = em.start(title=title, hint=hint, session_key=session_key)
            self._persist_episodes()
            return self._episode_to_dict(ep)

    def episode_end_session(
        self, session_key: str | None, run_dream: bool = True,
    ) -> dict[str, Any]:
        """Close the open episode only if its ``session_key`` matches; then
        (optionally) fire a background dream so the session's outcome signals
        become lessons by the next session start. Returns the closed episode
        dict, or ``{}`` when nothing matching was open."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            em = self._cms.episodes
            cur = em.open_episode()
            if cur is None or (session_key is not None
                               and cur.session_key != session_key):
                return {}
            closed = em.end()
            self._persist_episodes()
            result = self._episode_to_dict(closed) if closed is not None else {}
        if run_dream and result:
            self._fire_and_forget_dream()
        return result

    def _fire_and_forget_dream(self) -> None:
        """Run one dream cycle in a daemon thread so SessionEnd never blocks on
        the extractor. Errors are logged, never raised."""
        import threading

        def _run() -> None:
            try:
                from pseudolife_memory.memory.dream import build_extractor
                self.dream_run(build_extractor(self.config.memory.dream))
            except Exception:  # noqa: BLE001 — background best-effort
                logger.warning("session-end dream failed", exc_info=True)

        threading.Thread(target=_run, name="session-end-dream",
                         daemon=True).start()
```

`dream_run` takes `self._lock` itself; `_fire_and_forget_dream` is invoked **after** the `with self._lock` block, so there is no re-entrant deadlock.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_episode_service.py -k session -v`
Expected: PASS (both tests; `run_dream=False` avoids spawning threads in unit tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_episode_service.py
git commit -m "feat(episodes): idempotent session open/close + fire-and-forget dream"
```

---

## Task 4: REST endpoints for episode start/end

**Files:**
- Modify: `pseudolife_memory/web/routes.py` (`_register`)
- Test: `tests/test_episode_service.py` (dispatch through `ConsoleRoutes`)

**Interfaces:**
- Consumes: `service.episode_start_session`, `service.episode_end_session` (Task 3).
- Produces: `POST /api/episode/start {session_key,title,hint}`; `POST /api/episode/end {session_key,run_dream?}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_episode_service.py`:

```python
from pseudolife_memory.web.routes import ConsoleRoutes


def test_episode_rest_start_and_end(pristine_service):
    service = pristine_service
    routes = ConsoleRoutes(service)
    started = routes.dispatch("POST", "/api/episode/start", {},
                              {"session_key": "s1", "title": "Sess"})
    assert started["session_key"] == "s1"
    ended = routes.dispatch("POST", "/api/episode/end", {},
                            {"session_key": "s1", "run_dream": False})
    assert ended["ended_at"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_episode_service.py::test_episode_rest_start_and_end -v`
Expected: FAIL — `KeyError: '/api/episode/start'` from `dispatch`.

- [ ] **Step 3: Write minimal implementation**

In `routes.py` `_register`, add under the `# ---- episodes ----` section (after the existing GET routes):

```python
        p("/api/episode/start", lambda q, b: svc.episode_start_session(
            b.get("session_key"), b.get("title") or "session",
            b.get("hint")))
        p("/api/episode/end", lambda q, b: svc.episode_end_session(
            b.get("session_key"), run_dream=bool(b.get("run_dream", True))))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_episode_service.py::test_episode_rest_start_and_end -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/web/routes.py tests/test_episode_service.py
git commit -m "feat(api): POST /api/episode/start and /api/episode/end"
```

---

## Task 5: torch-free `episode-start` / `episode-end` CLI

**Files:**
- Create: `pseudolife_memory/episode_cli.py`
- Modify: `pseudolife_memory/cli.py` (dispatch)
- Test: `tests/test_episode_cli.py` (new)

**Interfaces:**
- Consumes: `pseudolife_memory.shim._daemon_url`, `probe_health` (existing torch-free helpers); the REST endpoints from Task 4.
- Produces: `pseudolife-mcp episode-start` / `episode-end` console modes. Reads hook stdin JSON `{session_id, cwd}`; posts `{session_key, title, hint}` / `{session_key}`. On any error or daemon-down, prints nothing and returns (exit 0).

- [ ] **Step 1: Write the failing test**

Create `tests/test_episode_cli.py`:

```python
import pseudolife_memory.episode_cli as ec


def test_daemon_down_is_silent_exit_zero(monkeypatch, capsys):
    # probe_health returning None == daemon down -> do nothing, never raise.
    monkeypatch.setattr(ec, "_daemon_url", lambda: "http://127.0.0.1:9", raising=False)
    monkeypatch.setattr(ec, "probe_health", lambda url: None, raising=False)
    ec.run_episode("episode-start", stdin_text='{"session_id":"abc","cwd":"/x"}')
    assert capsys.readouterr().out == ""


def test_parses_session_key_from_stdin(monkeypatch):
    captured = {}

    def fake_post(url, token, path, payload):
        captured["path"] = path
        captured["payload"] = payload

    monkeypatch.setattr(ec, "_daemon_url", lambda: "http://x", raising=False)
    monkeypatch.setattr(ec, "probe_health", lambda url: {"ok": True}, raising=False)
    monkeypatch.setattr(ec, "_post", fake_post, raising=False)
    ec.run_episode("episode-start",
                   stdin_text='{"session_id":"abc","cwd":"/home/u/Proj"}')
    assert captured["path"] == "/api/episode/start"
    assert captured["payload"]["session_key"] == "abc"
    assert "Proj" in captured["payload"]["title"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_episode_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pseudolife_memory.episode_cli'`.

- [ ] **Step 3: Write minimal implementation**

Create `pseudolife_memory/episode_cli.py`:

```python
"""``pseudolife-mcp episode-start`` / ``episode-end`` — hook-driven episode
lifecycle. Torch-free (stdlib ``urllib`` only); hits the already-running daemon
and NEVER auto-starts one. Prints nothing and returns on any error / cold daemon
so a SessionStart / SessionEnd hook can never break or slow a session.

Reads Claude Code's hook stdin JSON for ``session_id`` (→ ``session_key``, the
idempotency key) and ``cwd`` (→ a human episode title).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

from pseudolife_memory.shim import _daemon_url, probe_health  # torch-free


def _read_stdin() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _title_from_cwd(cwd: str | None) -> str:
    base = os.path.basename(os.path.normpath(cwd)) if cwd else "session"
    return f"{base or 'session'} - {time.strftime('%Y-%m-%d')}"


def _post(url: str, token: str | None, path: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{url}{path}", data=data, method="POST")
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


def run_episode(mode: str, stdin_text: str | None = None) -> None:
    """``mode`` is ``episode-start`` or ``episode-end``. ``stdin_text`` is for
    tests; production reads real stdin."""
    if stdin_text is not None:
        try:
            payload_in = json.loads(stdin_text)
        except Exception:
            payload_in = {}
    else:
        payload_in = _read_stdin()

    session_key = str(payload_in.get("session_id") or "") or None
    if session_key is None:
        return  # no key -> nothing safe to do; stay silent

    url = _daemon_url()
    if probe_health(url) is None:
        return  # daemon down -> inject nothing
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None

    try:
        if mode == "episode-start":
            _post(url, token, "/api/episode/start", {
                "session_key": session_key,
                "title": _title_from_cwd(payload_in.get("cwd")),
            })
        elif mode == "episode-end":
            _post(url, token, "/api/episode/end", {"session_key": session_key})
    except Exception:
        return  # never break session start/stop
```

In `cli.py`, add the dispatch branches (before the `else:`):

```python
    elif mode in ("episode-start", "episode-end"):
        from pseudolife_memory.episode_cli import run_episode
        run_episode(mode)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_episode_cli.py -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/episode_cli.py pseudolife_memory/cli.py tests/test_episode_cli.py
git commit -m "feat(cli): torch-free episode-start/episode-end hook verbs"
```

---

## Task 6: briefing surfaces — world facts + "where we left off"

**Files:**
- Modify: `pseudolife_memory/memory/briefing.py` (`format_briefing` + two formatters)
- Modify: `pseudolife_memory/service.py` (`session_briefing` gathers world + recap)
- Modify: `pseudolife_memory/web/routes.py` (`max_world` on `/api/briefing`)
- Modify: `pseudolife_memory/briefing_cli.py` (`--max-world`)
- Test: `tests/test_briefing.py` (new)

**Interfaces:**
- Consumes: `service.world_dump()` (entries with `entity, attribute, value, effective_confidence, stale, source_url`); `service.episode_list(include_open=False)` (episodes with `title, ended_at, entry_count`).
- Produces: `format_briefing(surprises, questions, lessons, world=None, recap=None) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_briefing.py`:

```python
from pseudolife_memory.memory.briefing import format_briefing


def test_world_block_renders_fresh_facts():
    world = [{"entity": "anthropic", "attribute": "latest-model",
              "value": "opus-4.8", "source_url": "https://docs.anthropic.com/x"}]
    md = format_briefing([], [], [], world=world, recap=None)
    assert "## Verified world facts" in md
    assert "anthropic" in md and "opus-4.8" in md


def test_recap_block_renders_last_session():
    recap = {"title": "Auth refactor", "entry_count": 7}
    md = format_briefing([], [], [], world=None, recap=recap)
    assert "## Where we left off" in md
    assert "Auth refactor" in md


def test_empty_inputs_render_nothing():
    assert format_briefing([], [], [], world=[], recap=None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_briefing.py -v`
Expected: FAIL — `TypeError: format_briefing() got an unexpected keyword argument 'world'`.

- [ ] **Step 3: Write minimal implementation**

In `briefing.py`, add two formatters and extend `format_briefing`:

```python
def _fmt_world(w: dict) -> str:
    ent = (w.get("entity") or "").strip()
    attr = (w.get("attribute") or "").strip()
    val = (w.get("value") or "").strip()
    if not (ent and val):
        return ""
    url = (w.get("source_url") or "").strip()
    src = ""
    if url:
        host = url.split("://", 1)[-1].split("/", 1)[0]
        src = f" ({host})" if host else ""
    head = f"{ent} {attr}".strip()
    return f"- `{head}`: {val}{src}"


def _fmt_recap(r: dict) -> str:
    title = (r.get("title") or "").strip()
    if not title:
        return ""
    n = r.get("entry_count") or 0
    return f"- {title} ({n} memories)"


def format_briefing(surprises: list[dict], questions: list[dict],
                    lessons: list[dict], world: list[dict] | None = None,
                    recap: dict | None = None) -> str:
    """Render the markdown block; empty string when there is nothing to say."""
    parts: list[str] = []
    unsure = [_fmt_surprise(s) for s in surprises]
    unsure += [_fmt_question(q) for q in questions]
    unsure = [ln for ln in unsure if ln]
    if unsure:
        parts.append("## What your memory is unsure about\n" + "\n".join(unsure))
    lesson_lines = [ln for ln in (_fmt_lesson(e) for e in lessons) if ln]
    if lesson_lines:
        parts.append("## Lessons from past work\n" + "\n".join(lesson_lines))
    world_lines = [ln for ln in (_fmt_world(w) for w in (world or [])) if ln]
    if world_lines:
        parts.append("## Verified world facts\n" + "\n".join(world_lines))
    recap_line = _fmt_recap(recap) if recap else ""
    if recap_line:
        parts.append("## Where we left off\n" + recap_line)
    return "\n\n".join(parts)
```

In `service.py` `session_briefing`, gather the two new inputs and pass them through. Replace the body that computes `markdown`:

```python
    def session_briefing(self, max_unsure: int = 3, max_lessons: int = 3,
                         max_world: int = 3) -> dict[str, Any]:
        """Assemble the session-start briefing: graph 'unsure-about' + avoid-first
        lessons + fresh world facts + a one-line recap of the last closed session.
        Read-only; no LLM. Each sub-call takes the lock itself, so this
        orchestrator must not hold it."""
        from pseudolife_memory.memory.briefing import format_briefing, select_lessons
        dg = self.graph_digest()
        surprises: list[dict] = []
        questions: list[dict] = []
        if dg.get("available"):
            d = dg.get("digest") or {}
            surprises = (d.get("surprises") or [])[:max_unsure]
            questions = (d.get("questions") or [])[:max_unsure]
        lessons_all = (self.lessons_dump(limit=120) or {}).get("entries", [])
        lessons = select_lessons(lessons_all, max_lessons)

        # Fresh, high-confidence world facts (drop stale; best-confidence first).
        world_all = (self.world_dump() or {}).get("entries", [])
        world = sorted(
            (w for w in world_all if not w.get("stale")),
            key=lambda w: w.get("effective_confidence", 0.0), reverse=True,
        )[:max_world]

        # Recap: newest CLOSED episode that actually captured memories.
        recap = None
        eps = (self.episode_list(limit=20, include_open=False)
               or {}).get("episodes", [])
        for e in eps:  # episode_list is newest-first
            if (e.get("entry_count") or 0) > 0:
                recap = {"title": e.get("title"), "entry_count": e.get("entry_count")}
                break

        markdown = format_briefing(surprises, questions, lessons,
                                   world=world, recap=recap)
        return {
            "available": bool(markdown),
            "markdown": markdown,
            "unsure": {"surprises": surprises, "questions": questions},
            "lessons": lessons,
            "world": world,
            "recap": recap,
        }
```

In `routes.py`, thread `max_world` into the briefing route:

```python
        g("/api/briefing", lambda q, b: svc.session_briefing(
            max_unsure=_i(q, "max_unsure", 3), max_lessons=_i(q, "max_lessons", 3),
            max_world=_i(q, "max_world", 3)))
```

In `briefing_cli.py`, add the flag and pass it through `_fetch_markdown`. Add the arg:

```python
    ap.add_argument("--max-world", type=int, default=3)
```

Update the `urlencode` call inside `_fetch_markdown` to include it (change its signature to accept `max_world` and pass `args.max_world` from `run_briefing`):

```python
def _fetch_markdown(url, token, max_unsure, max_lessons, max_world):
    qs = urllib.parse.urlencode({"max_unsure": max_unsure,
                                 "max_lessons": max_lessons,
                                 "max_world": max_world})
    ...

    # in run_briefing:
    md = _fetch_markdown(url, token, args.max_unsure, args.max_lessons, args.max_world)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_briefing.py -v`
Expected: PASS (all three).

- [ ] **Step 5: Run the service + briefing suites (no regressions)**

Run: `pytest tests/test_briefing.py tests/test_episode_service.py -v`
Expected: all PASS. The extra `session_briefing` keys are additive; existing callers ignore them.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/briefing.py pseudolife_memory/service.py pseudolife_memory/web/routes.py pseudolife_memory/briefing_cli.py tests/test_briefing.py
git commit -m "feat(briefing): surface verified world facts + last-session recap"
```

---

## Task 7: register the SessionStart/SessionEnd episode hooks

**Files:**
- Modify: `ops/install-hook.ps1`
- Test: manual (PowerShell against a temp settings file) — no pytest harness for PS scripts.

**Interfaces:**
- Consumes: the `pseudolife-mcp episode-start` / `episode-end` console modes (Task 5).
- Produces: an idempotent installer that adds a SessionStart `episode-start` group and a SessionEnd `episode-end` group, never replacing existing hooks (including the existing briefing hook).

- [ ] **Step 1: Add episode-hook installation (idempotent)**

In `ops/install-hook.ps1`, after the existing SessionStart briefing block writes (just before the final `Write-Host`), add:

```powershell
# ---- episode lifecycle hooks (idempotent, alongside the briefing hook) ----
if (-not ($obj.hooks.PSObject.Properties.Name -contains 'SessionEnd')) {
    $obj.hooks | Add-Member -NotePropertyName SessionEnd -NotePropertyValue @()
}

$hasStart = $false
foreach ($group in @($obj.hooks.SessionStart)) {
    foreach ($h in @($group.hooks)) {
        if ($h.command -like "*pseudolife-mcp episode-start*") { $hasStart = $true }
    }
}
if (-not $hasStart) {
    $startGroup = [pscustomobject]@{
        hooks = @([pscustomobject]@{ type = 'command';
            command = 'pseudolife-mcp episode-start'; shell = 'bash' })
    }
    $obj.hooks.SessionStart = @($obj.hooks.SessionStart) + $startGroup
    Write-Host "Installed SessionStart episode-start hook."
}

$hasEnd = $false
foreach ($group in @($obj.hooks.SessionEnd)) {
    foreach ($h in @($group.hooks)) {
        if ($h.command -like "*pseudolife-mcp episode-end*") { $hasEnd = $true }
    }
}
if (-not $hasEnd) {
    $endGroup = [pscustomobject]@{
        hooks = @([pscustomobject]@{ type = 'command';
            command = 'pseudolife-mcp episode-end'; shell = 'bash' })
    }
    $obj.hooks.SessionEnd = @($obj.hooks.SessionEnd) + $endGroup
    Write-Host "Installed SessionEnd episode-end hook."
}

$obj | ConvertTo-Json -Depth 30 | Set-Content -Path $SettingsPath -Encoding utf8
```

Note: the existing script `return`s early if the briefing hook is already present — move that early-return guard so it only skips the *briefing* append, not the whole script, OR (simpler) drop the early `return` and make the briefing append conditional on its own `$hasBriefing` flag mirroring the pattern above. Implement the `$hasBriefing` flag so all three hooks are independently idempotent and a single re-run installs whichever are missing.

- [ ] **Step 2: Verify idempotency against a temp settings file**

Run:
```bash
cp "$HOME/.claude/settings.json" /tmp/settings.test.json 2>/dev/null || echo '{}' > /tmp/settings.test.json
pwsh ops/install-hook.ps1 -SettingsPath /tmp/settings.test.json
pwsh ops/install-hook.ps1 -SettingsPath /tmp/settings.test.json   # second run = no-op
```
Expected: first run installs briefing + episode-start + episode-end; second run prints "already present"-style messages and adds nothing. Confirm:
```bash
python -c "import json;h=json.load(open('/tmp/settings.test.json'))['hooks'];import sys;cmds=[x['command'] for g in h.get('SessionStart',[])+h.get('SessionEnd',[]) for x in g['hooks']];print(cmds);assert cmds.count('pseudolife-mcp episode-start')==1 and cmds.count('pseudolife-mcp episode-end')==1"
```
Expected: each command appears exactly once.

- [ ] **Step 3: Commit**

```bash
git add ops/install-hook.ps1
git commit -m "feat(ops): register SessionStart/SessionEnd episode hooks (idempotent)"
```

---

## Task 8: rewrite the global CLAUDE.md memory section

**Files:**
- Modify: `~/.claude/CLAUDE.md` (user global; **out of repo** — do not `git add`).
- Test: manual — start a new Claude Code session and confirm the briefing + behavior.

**Interfaces:** none (documentation/behavioral).

- [ ] **Step 1: Replace the PseudoLife-MCP memory section**

Replace the existing `## PseudoLife-MCP memory — use it every session` section in `~/.claude/CLAUDE.md` with the RECALL→CAPTURE→REFLECT block verbatim from the spec (`docs/specs/2026-06-27-memory-lifecycle-utilization-design.md`, section 1c). Keep the surrounding "Coding discipline (Karpathy principles)" section untouched.

- [ ] **Step 2: Verify in a fresh session**

Open a new Claude Code session in this project. Confirm:
- The SessionStart briefing injects (and, once data exists, shows the world-facts / recap blocks).
- The assistant, per the new instructions, calls `memory_lesson_search` / `memory_world_search` at task start and `memory_outcome` at task end.

(No commit — this file lives outside the repo.)

---

### Phase 1 verification gate

- [ ] Run the full suite: `pytest -q`. Expected: all green.
- [ ] Deploy daemon-only: `pwsh ops/backup.ps1` then `docker compose build daemon && docker compose up -d daemon`.
- [ ] Re-run `pwsh ops/install-hook.ps1` to register the new hooks.
- [ ] Live smoke: start a session → `memory_store` a note → end the session → `memory_episode_list` shows a closed episode with `entry_count >= 1`; next session's briefing shows `## Where we left off`.

---

# PHASE 2 — episode nesting (task sub-episodes)

## Task 9: nesting model — `parent_id` + stack semantics

**Files:**
- Modify: `pseudolife_memory/memory/episodes.py`
- Test: `tests/test_episodes.py`

**Interfaces:**
- Produces: `Episode.parent_id: str | None`; `EpisodeManager.start_nested(title, hint=None) -> Episode` (nests under the open leaf, parent stays open); `EpisodeManager.end()` now pops to the parent; `EpisodeManager.end_session(session_key) -> Episode | None` (close the matching root + cascade-close descendants).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_episodes.py`:

```python
def test_nested_episode_keeps_parent_open_and_pops():
    em = EpisodeManager()
    root = em.start(title="Session", session_key="s1")
    child = em.start_nested(title="Task")
    assert child.parent_id == root.id
    assert em.episodes[root.id].ended_at is None      # parent stays open
    assert em.open_episode() is child                  # leaf is the child
    em.end()                                            # pop child
    assert em.open_episode() is em.episodes[root.id]    # back to parent


def test_end_session_cascade_closes_orphan_children():
    em = EpisodeManager()
    root = em.start(title="Session", session_key="s1")
    em.start_nested(title="Task")                       # forgot to end()
    em.end_session("s1")
    assert all(e.ended_at is not None for e in em.episodes.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_episodes.py -k "nested or cascade" -v`
Expected: FAIL — `AttributeError: 'EpisodeManager' object has no attribute 'start_nested'`.

- [ ] **Step 3: Write minimal implementation**

Add the field (after `session_key`):

```python
    session_key: str | None = None
    parent_id: str | None = None
```

Add nesting methods and change `end` to pop. Replace `end` and add `start_nested` / `end_session`:

```python
    def start_nested(self, title: str, hint: str | None = None) -> Episode:
        """Open a sub-episode under the current open leaf (which stays open).
        Falls back to a root episode when nothing is open."""
        parent = self.open_episode()
        ep = Episode(
            id=uuid.uuid4().hex,
            title=title,
            started_at=time.time(),
            hint=hint,
            parent_id=parent.id if parent is not None else None,
        )
        self.episodes[ep.id] = ep
        self.current_id = ep.id
        return ep

    def end(self) -> Episode | None:
        """Close the current open leaf and pop to its parent (if still open)."""
        if self.current_id is None:
            return None
        ep = self.episodes.get(self.current_id)
        if ep is None:
            self.current_id = None
            return None
        ep.ended_at = time.time()
        parent = self.episodes.get(ep.parent_id) if ep.parent_id else None
        self.current_id = parent.id if (parent and parent.ended_at is None) else None
        return ep

    def end_session(self, session_key: str | None) -> Episode | None:
        """Close the open ROOT episode matching ``session_key`` and cascade-close
        any still-open descendants. Returns the closed root, or None."""
        root = None
        for e in self.episodes.values():
            if (e.ended_at is None and e.parent_id is None
                    and (session_key is None or e.session_key == session_key)):
                root = e
                break
        if root is None:
            return None
        now = time.time()
        # close root + every still-open episode reachable from it
        open_ids = {e.id for e in self.episodes.values() if e.ended_at is None}
        for e in self.episodes.values():
            if e.ended_at is None and self._descends_from(e, root.id):
                e.ended_at = now
        self.current_id = None
        return root

    def _descends_from(self, ep: Episode, root_id: str) -> bool:
        seen: set[str] = set()
        cur: Episode | None = ep
        while cur is not None and cur.id not in seen:
            if cur.id == root_id:
                return True
            seen.add(cur.id)
            cur = self.episodes.get(cur.parent_id) if cur.parent_id else None
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_episodes.py -v`
Expected: all PASS, including the existing `session_key` test (the single-level case still pops to `None`).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/episodes.py tests/test_episodes.py
git commit -m "feat(episodes): parent_id nesting — start_nested/end pop/end_session cascade"
```

---

## Task 10: persist `parent_id` + wire service end-session to cascade

**Files:**
- Modify: `pseudolife_memory/storage/schema.py` (column + ALTER + version → 15)
- Modify: `pseudolife_memory/storage/postgres.py` (`upsert_episode`, `load_episodes`)
- Modify: `pseudolife_memory/service.py` (`episode_end_session` uses `em.end_session`; new `episode_start` nests; `_episode_to_dict` passthrough)
- Test: `tests/test_episode_service.py`

**Interfaces:**
- Consumes: `EpisodeManager.start_nested`, `end_session` (Task 9).
- Produces: episodes column `parent_id TEXT`; `service.episode_start` opens a NESTED episode (agent sub-episode); `service.episode_end_session` cascade-closes via `end_session`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_episode_service.py`:

```python
def test_agent_episode_nests_under_session(pristine_service):
    service = pristine_service
    service.episode_start_session("s1", "Session")
    sub = service.episode_start("Big task")           # agent sub-episode
    assert sub["parent_id"] is not None
    # storing now stamps the sub-episode (the leaf)
    eid = service.store("did a thing")  # returns entry/ack; episode stamped internally
    closed = service.episode_end_session("s1", run_dream=False)
    assert closed and closed["parent_id"] is None      # the root was closed
```

(If `service.store` returns a different shape, keep only the `parent_id` assertions; the stamping is covered by Task 11.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_episode_service.py::test_agent_episode_nests_under_session -v`
Expected: FAIL — `sub["parent_id"]` is `None` (today `episode_start` opens a root and auto-closes the session) or `KeyError: 'parent_id'`.

- [ ] **Step 3: Write minimal implementation**

In `schema.py`: add `parent_id TEXT` to the `episodes` `CREATE TABLE` body; add `ALTER TABLE episodes ADD COLUMN IF NOT EXISTS parent_id TEXT` in `ensure_schema`; bump `SCHEMA_META_VERSION = 15`.

In `postgres.py`: add `parent_id` to `upsert_episode` INSERT columns / `VALUES` / `ON CONFLICT DO UPDATE`, and to the `load_episodes` `cols` tuple (same pattern as Task 2's `session_key`).

In `service.py`:
- `_episode_to_dict`: add `"parent_id": getattr(ep, "parent_id", None)`.
- Change `episode_start` to nest (this is what the `memory_episode_start` MCP tool calls):

```python
    def episode_start(self, title: str, hint: str | None = None) -> dict[str, Any]:
        """Open a NESTED sub-episode under the current open (session) episode;
        the parent stays open. Falls back to a root when nothing is open."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            ep = self._cms.episodes.start_nested(title=title, hint=hint)
            self._persist_episodes()
            return self._episode_to_dict(ep)
```

- Change `episode_end_session` to cascade via `end_session` (replace `closed = em.end()` line):

```python
            closed = em.end_session(session_key)
            self._persist_episodes()
            result = self._episode_to_dict(closed) if closed is not None else {}
```

Note `_persist_episodes` already upserts every episode in the manager, so cascade-closed children are written too.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_episode_service.py -v` and `pytest tests/test_migration.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/schema.py pseudolife_memory/storage/postgres.py pseudolife_memory/service.py tests/test_episode_service.py
git commit -m "feat(episodes): persist parent_id; agent episode_start nests; end_session cascades (schema v15)"
```

---

## Task 11: recall expands an episode filter to its subtree

**Files:**
- Modify: `pseudolife_memory/service.py` (episode-filter expansion helper used by `search`)
- Test: `tests/test_episode_service.py`

**Interfaces:**
- Consumes: `Episode.parent_id` (Task 9/10).
- Produces: `MemoryService._episode_subtree(ids: list[str]) -> list[str]` — expands each episode id to itself + all descendants; `search(..., episodes=[root_id])` returns child-episode entries too.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_episode_service.py`:

```python
def test_search_episode_filter_includes_child_episodes(pristine_service):
    service = pristine_service
    root = service.episode_start_session("s1", "Session")
    sub = service.episode_start("Sub")                       # nests under root
    service.store("alpha beta gamma", source="pseudolife")   # stamped to sub
    hits = service.search("alpha beta gamma", episodes=[root["id"]])
    texts = [e["text"] for e in hits.get("entries", [])]
    assert any("alpha beta gamma" in t for t in texts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_episode_service.py::test_search_episode_filter_includes_child_episodes -v`
Expected: FAIL — the entry is stamped to `sub`, but the filter only matches `root`, so it's excluded.

- [ ] **Step 3: Write minimal implementation**

Add the helper to `MemoryService` (near the episode methods):

```python
    def _episode_subtree(self, ids: list[str] | None) -> list[str] | None:
        """Expand each episode id to itself + all descendant episode ids, so a
        session-scoped query also returns entries from its sub-episodes."""
        if not ids:
            return ids
        assert self._cms is not None
        all_eps = self._cms.episodes.episodes
        want = set(ids)
        # walk parent chains; an episode is in-scope if any ancestor is requested
        out = set(ids)
        for ep in all_eps.values():
            cur = ep
            seen: set[str] = set()
            while cur is not None and cur.id not in seen:
                if cur.id in want:
                    out.add(ep.id)
                    break
                seen.add(cur.id)
                cur = all_eps.get(cur.parent_id) if cur.parent_id else None
        return list(out)
```

In `search`, expand the `episodes` argument before it reaches the band filter. Locate the line in `search` that resolves the episodes filter and wrap it:

```python
            episodes = self._episode_subtree(episodes)
```

Place this immediately after `self._ensure_init()` inside `search`, before the retrieval call that consumes `episodes`. (The expansion holds the service lock, same as the rest of `search`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_episode_service.py::test_search_episode_filter_includes_child_episodes -v`
Expected: PASS.

- [ ] **Step 5: Run the retrieval suites (no regressions)**

Run: `pytest tests/test_episodes.py tests/test_episode_service.py tests/test_service.py -v`
Expected: all PASS — single-episode queries are unchanged (subtree of a leaf is itself).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/service.py tests/test_episode_service.py
git commit -m "feat(recall): episode filter expands to sub-episode subtree"
```

---

## Task 12: CLAUDE.md sub-episode guidance + MCP tool docs

**Files:**
- Modify: `~/.claude/CLAUDE.md` (user global, out of repo)
- Modify: `pseudolife_memory/mcp_server.py` (docstring of `memory_episode_start` — now nests)
- Test: manual

**Interfaces:** none (documentation/behavioral).

- [ ] **Step 1: Update the MCP tool docstring**

In `mcp_server.py`, update `memory_episode_start`'s docstring to state it now **nests** under the open session episode (no longer auto-closes the parent), and `memory_episode_end` pops back to the parent. Match the existing docstring style.

- [ ] **Step 2: Add the sub-episode line to CLAUDE.md**

Add to the CAPTURE section of the memory block in `~/.claude/CLAUDE.md`:

```markdown
- For a substantial multi-step task, open a named sub-episode with
  `memory_episode_start(title)` — it nests under the auto-managed session
  episode; `memory_episode_end` pops back. Keeps a big task's memories grouped
  for later `memory_episode_summary` / episode-scoped search.
```

- [ ] **Step 3: Commit the in-repo doc change**

```bash
git add pseudolife_memory/mcp_server.py
git commit -m "docs(mcp): episode_start now nests under the session episode"
```

- [ ] **Step 4: Manual verification**

In a fresh session: `memory_episode_start("demo task")` → `memory_episode_list` shows the new episode with a `parent_id` equal to the session episode; `memory_store` then `memory_episode_summary(<sub id>)` shows the stored entry; `memory_episode_end` pops back to the session.

---

### Phase 2 verification gate

- [ ] `pytest -q` → all green.
- [ ] Deploy daemon-only (`ops/backup.ps1`, then `docker compose build daemon && docker compose up -d daemon`); the `ADD COLUMN parent_id` runs automatically on daemon start.
- [ ] Live smoke: session open → `memory_episode_start("X")` nests → store → `memory_episode_end` pops → end session cascade-closes; `memory_search(episodes=[session_id])` returns the sub-episode's entries.

---

## Self-Review (completed during planning)

- **Spec coverage:** every spec section maps to a task — episodes lifecycle (Tasks 1–5, 7), briefing surfaces (Task 6), CLAUDE.md (Tasks 8, 12), nesting (Tasks 9–11), schema/sync (Tasks 2, 10), testing (each task), deploy (phase gates). No gaps.
- **Placeholders:** none — every code/test step shows complete content.
- **Type consistency:** `episode_start_session(session_key, title, hint)`, `episode_end_session(session_key, run_dream)`, `start_nested`, `end_session`, `open_episode`, `_episode_subtree`, and `format_briefing(..., world, recap)` are used identically across the tasks that define and consume them.

## Execution note

Tasks are ordered so each leaves the suite green. Phase 1 (Tasks 1–8) is independently shippable; Phase 2 (Tasks 9–12) builds on it.

**Fixtures (verified against this repo):** service-level tests use `pristine_service` (file-mode `MemoryService`, `tests/conftest.py`); Postgres persistence tests use `pg_conn` + `pg_url` from `tests/pg_fixtures.py` with `PostgresStorage(pg_url)`, and skip cleanly when no test Postgres is reachable. Confirmed signatures: `service.store(text, source=…, tags=…, origin=…)` and `service.search(query, top_k=…, episodes=[…], …)`. Note: `pristine_service` clears the CMS banks between tests; if an open episode leaks across tests, close it at test top via `episode_end_session(<key>, run_dream=False)`.
