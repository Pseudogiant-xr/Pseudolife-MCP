# Cortex Freshness Inference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Infer a cortex fact's `freshness_class` from the *kind* of entity it is about, so stale transient facts age out while durable ones never decay.

**Architecture:** A new `entity_kinds` table (schema v24) stores one kind per `entity_norm` — `artifact` (frozen in time), `system` (live), `concept` (abstract). A pure `resolve_class(kind, attribute_norm)` maps that plus the attribute name to a freshness class; only `system` entities can ever yield `volatile`. The write path looks the kind up and applies the rule — no model call. A one-time offline backfill classifies the 233 decision-relevant entities via the Fable shim, gated on a hand-labelled gold set.

**Tech Stack:** Python 3.11, psycopg 3, Postgres 16 (+pgvector), pytest, the existing `evals/sonnet_shim.py` (OpenAI-compatible shim over `claude -p`).

## Global Constraints

- **Default is `evergreen` on every failure path** — unknown kind, unknown entity, shim down, malformed response, unparseable batch. Never `volatile`. The world cortex's `normalize_class` defaults unknown to `volatile`; the personal cortex must not.
- **Only `system` entities can yield `volatile`.** `artifact` and `concept` return `evergreen` unconditionally, whatever the attribute is called.
- **Nothing auto-applies.** The classifier writes a JSON artifact; a separate human-gated step commits it.
- **Schema v24 touches four places together** (repo convention): `SCHEMA_META_VERSION` in `pseudolife_memory/storage/schema.py`; docs (README capabilities table + the DSN row and version-history table in `docs/guide/configuration.md`); the version-pin tests (`test_schema_v13.py`, `test_schema_v16.py`, `test_schema_v22.py`, `test_schema_v23.py`, `test_temporal_stamp.py`, plus new `test_schema_v24.py`); and a CHANGELOG mention of `v24`.
- **TDD with a watched RED.** Every task runs the test and sees it fail before implementing.
- **Full suite before each commit:** `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/ -q` with the bench Postgres up on 127.0.0.1:5433. PG tests skip silently without it, which is not a pass.
- **No PII / machine identifiers** in tracked files — no emails, OS usernames, hostnames, LAN IPs, tokens.

---

## File Structure

| File | Responsibility |
|---|---|
| `pseudolife_memory/memory/freshness.py` (modify) | Add `VOLATILE_ATTRIBUTE_RE` + `resolve_class`. Stays pure stdlib. |
| `pseudolife_memory/storage/schema.py` (modify) | `SCHEMA_META_VERSION = 24`; `entity_kinds` DDL + additive `CREATE TABLE IF NOT EXISTS` in `ensure_schema`. |
| `pseudolife_memory/storage/postgres.py` (modify) | `load_entity_kinds()`, `upsert_entity_kinds(rows)`. |
| `pseudolife_memory/service.py` (modify) | `cortex_write` accepts `freshness_class="auto"`; resolves via the kind map; caches the map. |
| `pseudolife_memory/mcp_server.py` (modify) | Tool default becomes `"auto"`. |
| `pseudolife_memory/web/routes.py` (modify) | REST default becomes `"auto"`. |
| `evals/classify_entity_kinds.py` (create) | Offline classifier: scope → batch → shim → artifact. Never writes the DB. |
| `evals/apply_entity_kinds.py` (create) | Human-gated apply: artifact → `entity_kinds` → recompute `facts.freshness_class`. |
| `tests/fixtures/entity_kinds_gold.json` (create) | ~40 hand-labelled entities, weighted to the ambiguous class. |
| `tests/test_schema_v24.py` (create) | Table DDL, PG round-trip, storage accessors. |
| `tests/test_freshness_resolve.py` (create) | The `resolve_class` truth table. |
| `tests/test_entity_kind_inference.py` (create) | Write-path integration. |
| `tests/test_classify_entity_kinds.py` (create) | Scoping, batching, malformed-response handling. |

---

## Task 1: `resolve_class` — the whole policy in one pure function

**Files:**
- Modify: `pseudolife_memory/memory/freshness.py`
- Test: `tests/test_freshness_resolve.py` (create)

**Interfaces:**
- Consumes: `freshness.FRESHNESS_CLASSES` (existing).
- Produces: `freshness.ENTITY_KINDS: tuple[str, ...]`, `freshness.resolve_class(kind: str | None, attribute_norm: str) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_freshness_resolve.py`:

```python
"""The entity-kind -> freshness policy (schema v24).

Whether a fact rots is decided by what KIND of thing it is about, not by the
attribute name. `0-9-0-release / schema-version` is evergreen (a release is
frozen in time); `daemon / schema-version` is volatile. Same attribute,
opposite class -- that pair is the whole design in one test.
"""
from __future__ import annotations

import pytest

from pseudolife_memory.memory import freshness


def test_the_motivating_pair_same_attribute_opposite_class():
    assert freshness.resolve_class("artifact", "schema-version") == "evergreen"
    assert freshness.resolve_class("system", "schema-version") == "volatile"


@pytest.mark.parametrize("attribute", [
    "deploy-status", "current-branch", "schema-version", "running-model",
    "health-check-status", "live-url", "build-state", "deployment-status",
])
def test_system_entities_yield_volatile_for_transient_attributes(attribute):
    assert freshness.resolve_class("system", attribute) == "volatile"


@pytest.mark.parametrize("attribute", ["language", "owner", "purpose", "licence"])
def test_system_entities_stay_evergreen_for_durable_attributes(attribute):
    assert freshness.resolve_class("system", attribute) == "evergreen"


@pytest.mark.parametrize("attribute", [
    "deployment-date", "merge-date", "commit-date", "commit-hash",
    "asserted-at", "replicate-count", "cortex-score",
])
def test_event_attributes_never_decay_even_on_a_live_system(attribute):
    """An EVENT is permanently true; only STATE rots. "deployment-date" says
    when a deploy happened and stays correct forever, so it must not inherit
    the volatility of the "deployment" prefix -- the event suffix wins."""
    assert freshness.resolve_class("system", attribute) == "evergreen"


@pytest.mark.parametrize("kind", ["artifact", "concept"])
@pytest.mark.parametrize("attribute", ["deploy-status", "schema-version", "current-branch"])
def test_artifacts_and_concepts_never_decay(kind, attribute):
    """Structural guarantee: the harmful error direction -- a durable fact
    silently decaying -- cannot reach these 282 facts at all."""
    assert freshness.resolve_class(kind, attribute) == "evergreen"


@pytest.mark.parametrize("kind", [None, "", "nonsense", "SYSTEM_TYPO"])
def test_unknown_kind_defaults_evergreen_not_volatile(kind):
    """normalize_class sends unknown to volatile -- right for world facts and
    the exact inversion here. An unclassified personal fact must not decay."""
    assert freshness.resolve_class(kind, "deploy-status") == "evergreen"


def test_kind_matching_is_case_and_whitespace_insensitive():
    assert freshness.resolve_class("  System ", "deploy-status") == "volatile"


def test_entity_kinds_vocabulary_is_exported():
    assert freshness.ENTITY_KINDS == ("artifact", "system", "concept")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_freshness_resolve.py -q`
Expected: FAIL — `AttributeError: module 'pseudolife_memory.memory.freshness' has no attribute 'resolve_class'`

- [ ] **Step 3: Write minimal implementation**

In `pseudolife_memory/memory/freshness.py`, after the `_FLOOR` definition, add:

```python
# ── entity-kind -> freshness policy (schema v24) ──────────────────────────
# Whether a fact rots is decided by what KIND of thing it is about. A release
# is frozen in time, so "0-9-0-release / schema-version" is permanently true;
# the daemon is live, so "daemon / schema-version" rots. Same attribute name,
# opposite class -- the name cannot decide it, the entity's kind can.
ENTITY_KINDS = ("artifact", "system", "concept")

# EVENTS and measurements are permanently true -- only STATE rots. Checked
# FIRST so "deployment-date" (when a deploy happened) does not inherit the
# volatility of the "deployment" prefix. Without this exclusion the pattern
# marks historical events as decaying, which is the harmful direction.
_EVENT_ATTRIBUTE_RE = _re.compile(
    r"(^|[-_])(date|at|hash|commit|count|score|id|size|duration)$")

# Only consulted for `system` entities, and only after the event check.
# Deliberately dumb and auditable: the entity kind already carries the load,
# so this stays a short explicit list rather than anything clever.
_VOLATILE_ATTRIBUTE_RE = _re.compile(
    r"(^|[-_])(status|state|health|live|running|current|deployment|deployed)([-_]|$)"
    r"|(^|[-_])version([-_]|$)"
    r"|[-_](status|state|url)$"
)


def resolve_class(kind: str | None, attribute_norm: str) -> str:
    """Freshness class for a fact, from its entity's kind and attribute name.

    Unknown kinds return ``evergreen`` -- NOT ``volatile`` like
    :func:`normalize_class`. That fallback is right for world facts, which rot
    by default, and is the exact inversion of the intent here: an
    unclassified personal fact must not quietly start decaying.
    """
    if (kind or "").strip().casefold() != "system":
        return "evergreen"
    attr = (attribute_norm or "").strip().casefold()
    if _EVENT_ATTRIBUTE_RE.search(attr):        # events never rot
        return "evergreen"
    return "volatile" if _VOLATILE_ATTRIBUTE_RE.search(attr) else "evergreen"
```

At the top of the file, add `import re as _re` beside `import time as _time`.

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_freshness_resolve.py -q`
Expected: PASS (26 passed)

- [ ] **Step 5: Run the full suite**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all pass, no skips (bench PG up)

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/freshness.py tests/test_freshness_resolve.py
git commit -m "feat(freshness): entity-kind -> freshness policy

resolve_class(kind, attribute) is the whole policy. Only `system` entities
can yield volatile, so artifact/concept facts are structurally protected
from decaying. Unknown kind returns evergreen, NOT normalize_class's
volatile -- that fallback is right for world facts and inverts the intent
here.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: Schema v24 — the `entity_kinds` table

**Files:**
- Modify: `pseudolife_memory/storage/schema.py`
- Modify: `pseudolife_memory/storage/postgres.py`
- Modify: `tests/test_schema_v13.py`, `tests/test_schema_v16.py`, `tests/test_schema_v22.py`, `tests/test_schema_v23.py`, `tests/test_temporal_stamp.py` (version pins 23 -> 24)
- Test: `tests/test_schema_v24.py` (create)

**Interfaces:**
- Produces: `PostgresStorage.load_entity_kinds() -> dict[str, str]` (entity_norm -> kind);
  `PostgresStorage.upsert_entity_kinds(rows: list[dict]) -> int` where each row is
  `{"entity_norm": str, "kind": str, "origin": str, "confidence": float | None, "decided_at": float}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schema_v24.py`:

```python
"""Schema v24 -- per-entity kind, the input to the freshness policy.

Keyed on entity_norm, NOT entity_id: that is what cortex slots key on, a third
of cortex entities have no graph node at all, and a graph merge would
otherwise silently retarget the kind.
"""
from __future__ import annotations

import time

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage import schema


def test_schema_version_is_24():
    assert schema.SCHEMA_META_VERSION == 24


def test_entity_kinds_table_present(pg_conn):
    assert pg_conn.execute(
        "SELECT to_regclass('public.entity_kinds')").fetchone()[0]
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='entity_kinds'").fetchall()}
    assert {"entity_norm", "kind", "origin", "confidence", "decided_at"} <= cols


def test_entity_norm_is_the_primary_key(pg_conn):
    """One kind per entity -- a second write to the same entity updates it."""
    rows = pg_conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
        "WHERE i.indrelid='entity_kinds'::regclass AND i.indisprimary").fetchall()
    assert [r[0] for r in rows] == ["entity_norm"]


def _storage(pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage
    return PostgresStorage(pg_url)


def test_upsert_then_load_round_trip(pg_conn, pg_url):
    st = _storage(pg_url)
    try:
        n = st.upsert_entity_kinds([
            {"entity_norm": "daemon", "kind": "system",
             "origin": "model", "confidence": 0.9, "decided_at": time.time()},
            {"entity_norm": "0-9-0-release", "kind": "artifact",
             "origin": "rule", "confidence": 1.0, "decided_at": time.time()},
        ])
        assert n == 2
        assert st.load_entity_kinds() == {
            "daemon": "system", "0-9-0-release": "artifact"}
    finally:
        st.close()


def test_upsert_is_idempotent_and_updates_in_place(pg_conn, pg_url):
    st = _storage(pg_url)
    try:
        row = {"entity_norm": "daemon", "kind": "system",
               "origin": "model", "confidence": 0.9, "decided_at": time.time()}
        st.upsert_entity_kinds([row])
        st.upsert_entity_kinds([{**row, "kind": "concept", "origin": "user"}])
        assert st.load_entity_kinds() == {"daemon": "concept"}
        assert pg_conn.execute(
            "SELECT count(*) FROM entity_kinds").fetchone()[0] == 1
    finally:
        st.close()


def test_load_on_empty_table_returns_empty_dict(pg_conn, pg_url):
    st = _storage(pg_url)
    try:
        assert st.load_entity_kinds() == {}
    finally:
        st.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_schema_v24.py -q`
Expected: FAIL — `assert 23 == 24` and `to_regclass` returns None

- [ ] **Step 3: Write minimal implementation**

In `pseudolife_memory/storage/schema.py`:

1. Change `SCHEMA_META_VERSION = 23` to `SCHEMA_META_VERSION = 24`.
2. In `SCHEMA_SQL`, after the `entity_proposals` block, add:

```sql
CREATE TABLE IF NOT EXISTS entity_kinds (
  entity_norm TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  origin      TEXT NOT NULL,
  confidence  REAL,
  decided_at  DOUBLE PRECISION NOT NULL
);
```

3. In `ensure_schema`, beside the other additive DDL, add:

```python
        # v24 additive: per-entity kind, the input to the freshness policy.
        # Keyed on entity_norm (what cortex slots key on), not entity_id --
        # a third of cortex entities have no graph node, and a graph merge
        # would otherwise silently retarget the kind.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS entity_kinds ("
            "entity_norm TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "origin TEXT NOT NULL, confidence REAL, "
            "decided_at DOUBLE PRECISION NOT NULL)"
        )
```

In `pseudolife_memory/storage/postgres.py`, beside the other cortex accessors, add:

```python
    # ── entity kinds (schema v24) ───────────────────────────────────────

    def load_entity_kinds(self) -> dict[str, str]:
        """entity_norm -> kind. Small (order 1k rows); loaded once and cached
        by the service, so a plain full read is right here."""
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT entity_norm, kind FROM entity_kinds").fetchall()}

    def upsert_entity_kinds(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self._txn(), self.conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    "INSERT INTO entity_kinds "
                    "(entity_norm, kind, origin, confidence, decided_at) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (entity_norm) DO UPDATE SET "
                    "kind=EXCLUDED.kind, origin=EXCLUDED.origin, "
                    "confidence=EXCLUDED.confidence, "
                    "decided_at=EXCLUDED.decided_at",
                    (r["entity_norm"], r["kind"], r["origin"],
                     r.get("confidence"), r["decided_at"]),
                )
        return len(rows)
```

Then update the five version pins from `== 23` to `== 24` in
`tests/test_schema_v13.py`, `tests/test_schema_v16.py`, `tests/test_schema_v22.py`,
`tests/test_schema_v23.py`, `tests/test_temporal_stamp.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_schema_v24.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Run the full suite**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all pass. If `test_release_ux.py` fails on the CHANGELOG `v24` mention, that is fixed in Task 6 — note it and continue.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/storage/schema.py pseudolife_memory/storage/postgres.py tests/test_schema_v24.py tests/test_schema_v13.py tests/test_schema_v16.py tests/test_schema_v22.py tests/test_schema_v23.py tests/test_temporal_stamp.py
git commit -m "feat(schema): v24 entity_kinds table

One kind per entity_norm -- artifact | system | concept. Keyed on
entity_norm, not entity_id: that is what cortex slots key on, a third of
cortex entities have no graph node, and a graph merge would otherwise
silently retarget the kind.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: Write path — infer the class when the caller says `auto`

**Files:**
- Modify: `pseudolife_memory/service.py`
- Modify: `pseudolife_memory/mcp_server.py`
- Modify: `pseudolife_memory/web/routes.py`
- Modify: `pseudolife_memory/web/fixtures.py`
- Test: `tests/test_entity_kind_inference.py` (create)

**Interfaces:**
- Consumes: `freshness.resolve_class` (Task 1), `PostgresStorage.load_entity_kinds` (Task 2).
- Produces: `MemoryService.cortex_write(..., freshness_class: str = "auto")`; `MemoryService._entity_kind_map() -> dict[str, str]` (cached, invalidated by `upsert_entity_kinds`).

**Why a sentinel:** today the service cannot tell "caller omitted the class" from "caller explicitly said evergreen". `"auto"` is that sentinel. Behaviour is unchanged until `entity_kinds` has rows, because an empty map resolves everything to `evergreen`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_entity_kind_inference.py`:

```python
"""Write-path inference: freshness_class comes from the entity's kind.

No model call on this path -- it is a dictionary lookup plus a pure function,
because it runs on every dream forever.
"""
from __future__ import annotations

import tempfile
import time

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url):  # noqa: F811
    from pseudolife_memory.service import MemoryService
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def _kinds(svc, **pairs):
    svc._storage.upsert_entity_kinds([
        {"entity_norm": k, "kind": v, "origin": "model",
         "confidence": 0.9, "decided_at": time.time()}
        for k, v in pairs.items()])
    svc._entity_kind_cache = None          # force reload


def test_system_entity_transient_attribute_infers_volatile(svc):
    _kinds(svc, daemon="system")
    svc.cortex_write("daemon", "schema-version", "24", support="action")
    assert svc.cortex_lookup("daemon", "schema-version")["freshness_class"] == "volatile"


def test_artifact_entity_same_attribute_stays_evergreen(svc):
    """The motivating pair, end to end through the real write path."""
    _kinds(svc, **{"0-9-0-release": "artifact"})
    svc.cortex_write("0-9-0-release", "schema-version", "v22", support="action")
    assert svc.cortex_lookup("0-9-0-release", "schema-version")[
        "freshness_class"] == "evergreen"


def test_explicit_class_beats_inference(svc):
    _kinds(svc, daemon="system")
    svc.cortex_write("daemon", "schema-version", "24",
                     support="action", freshness_class="evergreen")
    assert svc.cortex_lookup("daemon", "schema-version")["freshness_class"] == "evergreen"


def test_unknown_entity_defaults_evergreen(svc):
    """An unclassified entity must never start a fact decaying."""
    svc.cortex_write("never-seen", "deploy-status", "green", support="action")
    assert svc.cortex_lookup("never-seen", "deploy-status")["freshness_class"] == "evergreen"


def test_empty_kind_map_preserves_v23_behaviour(svc):
    """Until entity_kinds is populated, nothing changes."""
    svc.cortex_write("daemon", "deploy-status", "green", support="action")
    assert svc.cortex_lookup("daemon", "deploy-status")["freshness_class"] == "evergreen"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_entity_kind_inference.py -q`
Expected: FAIL — `AttributeError: 'MemoryService' object has no attribute '_entity_kind_cache'`

- [ ] **Step 3: Write minimal implementation**

In `pseudolife_memory/service.py`:

1. In `MemoryService.__init__`, add `self._entity_kind_cache: dict[str, str] | None = None`.

2. Add the cached accessor beside the other cortex helpers:

```python
    def _entity_kind_map(self) -> dict[str, str]:
        """entity_norm -> kind, cached. Order 1k rows and read on every fact
        write, so it is loaded once; the apply step clears the cache."""
        if self._entity_kind_cache is None:
            self._entity_kind_cache = (
                self._storage.load_entity_kinds() if self._storage else {})
        return self._entity_kind_cache
```

3. Change the `cortex_write` signature default from `freshness_class: str = "evergreen"` to `freshness_class: str = "auto"`, and resolve before the write (inside the lock, after `_ensure_init()`):

```python
            if freshness_class == "auto":
                from pseudolife_memory.memory.cortex import _norm_key
                freshness_class = freshness.resolve_class(
                    self._entity_kind_map().get(_norm_key(entity)), _norm_key(attribute))
```

Add `from pseudolife_memory.memory import freshness` to the module imports if absent.

In `pseudolife_memory/mcp_server.py`, change the tool parameter to:

```python
    freshness_class: Literal["auto", "evergreen", "slow", "volatile"] = "auto",
```

In `pseudolife_memory/web/routes.py`, change the REST default:

```python
            freshness_class=(b.get("freshness_class") or "auto")))
```

In `pseudolife_memory/web/fixtures.py`, change `FixtureService.cortex_write`'s default to `freshness_class="auto"` and have it echo `freshness_class if freshness_class != "auto" else "evergreen"`.

Update `tests/test_web.py::test_facts_set_defaults_freshness_class_to_evergreen` — the assertion stays `evergreen`; only the fixture's internal default changed.

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_entity_kind_inference.py tests/test_web.py -q`
Expected: PASS

- [ ] **Step 5: RED-check the inference hook is load-bearing**

Temporarily change the resolve line to `freshness_class = "evergreen"`, run
`tests/test_entity_kind_inference.py`, confirm
`test_system_entity_transient_attribute_infers_volatile` FAILS, then restore.
A hook that never goes red is decoration.

- [ ] **Step 6: Run the full suite and commit**

```bash
HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/ -q
git add pseudolife_memory/service.py pseudolife_memory/mcp_server.py pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_entity_kind_inference.py tests/test_web.py
git commit -m "feat(cortex): infer freshness_class from the entity's kind

freshness_class defaults to the sentinel \"auto\": the service could not
previously tell an omitted class from an explicit evergreen. Resolution is
a cached dict lookup plus a pure function -- no model call, because this
runs on every dream forever. Behaviour is unchanged until entity_kinds has
rows, since an empty map resolves everything to evergreen.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: Gold set + offline classifier

**Files:**
- Create: `evals/classify_entity_kinds.py`
- Create: `tests/fixtures/entity_kinds_gold.json`
- Test: `tests/test_classify_entity_kinds.py` (create)

**Interfaces:**
- Produces: `classify_entity_kinds.scope_entities(rows) -> list[str]`;
  `classify_entity_kinds.rule_kind(entity_norm) -> str | None`;
  `classify_entity_kinds.parse_batch(text, batch) -> dict[str, str]`;
  `classify_entity_kinds.batched(items, size) -> Iterator[list[str]]`.
- `rows` is `list[tuple[str, str]]` of `(entity_norm, attribute_norm)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_classify_entity_kinds.py`:

```python
"""Offline entity-kind classifier -- scoping, batching, robustness.

Scoping is the dominant token lever: an entity only matters if it carries a
transient-looking attribute, because otherwise every one of its facts resolves
evergreen whatever its kind. Measured on the live bank that is 2415 facts ->
1005 entities -> 264 decision-relevant -> 233 needing model judgement.
"""
from __future__ import annotations

import json
from pathlib import Path

from evals import classify_entity_kinds as C

GOLD = Path(__file__).parent / "fixtures" / "entity_kinds_gold.json"


def test_scope_drops_entities_whose_kind_cannot_matter():
    rows = [("daemon", "deploy-status"),      # transient -> in scope
            ("readme", "purpose"),            # durable only -> out of scope
            ("readme", "author")]
    assert C.scope_entities(rows) == ["daemon"]


def test_scope_keeps_an_entity_with_any_transient_attribute():
    rows = [("proj", "language"), ("proj", "current-branch")]
    assert C.scope_entities(rows) == ["proj"]


def test_rule_classifies_confident_artifacts_without_a_model():
    for name in ("0-9-0-release", "2026-07-15-atlas-deploy", "pr-42-review",
                 "commit-eec67b1"):
        assert C.rule_kind(name) == "artifact"


def test_rule_abstains_when_unsure():
    for name in ("daemon", "pseudolife-mcp", "cortex-console"):
        assert C.rule_kind(name) is None


def test_batched_splits_evenly_and_keeps_every_item():
    items = [f"e{i}" for i in range(233)]
    batches = list(C.batched(items, 50))
    assert [len(b) for b in batches] == [50, 50, 50, 50, 33]
    assert [x for b in batches for x in b] == items


def test_parse_batch_reads_a_well_formed_response():
    text = '{"daemon": "system", "0-9-0-release": "artifact"}'
    assert C.parse_batch(text, ["daemon", "0-9-0-release"]) == {
        "daemon": "system", "0-9-0-release": "artifact"}


def test_parse_batch_tolerates_fenced_json():
    text = '```json\n{"daemon": "system"}\n```'
    assert C.parse_batch(text, ["daemon"]) == {"daemon": "system"}


def test_malformed_response_yields_no_labels_rather_than_guesses():
    """Every failure path defaults to evergreen -- which means emitting NO
    kind, so resolve_class falls back. Never invent a label."""
    assert C.parse_batch("I could not classify these.", ["daemon"]) == {}


def test_parse_batch_drops_unknown_kinds_and_unrequested_entities():
    text = '{"daemon": "banana", "not-asked": "system", "ok-one": "concept"}'
    assert C.parse_batch(text, ["daemon", "ok-one"]) == {"ok-one": "concept"}


def test_harness_loads_the_one_canonical_policy_without_torch():
    """The harness must use the SAME resolve_class as the write path, loaded
    by file path because the package __init__ pulls torch. If this regresses
    to a private copy, the backfill can write classes new writes would never
    reproduce -- and the drift would be invisible."""
    import sys
    from pseudolife_memory.memory import freshness
    assert C._freshness.resolve_class is not None
    for attr in ("deploy-status", "deployment-date", "schema-version", "owner"):
        assert (C._is_transient(attr)
                is (freshness.resolve_class("system", attr) == "volatile"))
    # Loaded standalone: the module object is NOT the package-imported one.
    assert C._freshness is not sys.modules.get("pseudolife_memory.memory.freshness")


def test_gold_set_is_well_formed_and_covers_the_ambiguous_class():
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    assert len(gold) >= 40
    assert {g["kind"] for g in gold} <= set(C.KINDS)
    # The whole point: the same attribute on both an artifact and a system.
    kinds_for_version = {g["kind"] for g in gold
                         if "version" in g.get("example_attribute", "")}
    assert {"artifact", "system"} <= kinds_for_version
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_classify_entity_kinds.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'evals.classify_entity_kinds'`

- [ ] **Step 3: Write minimal implementation**

Create `evals/classify_entity_kinds.py`:

```python
"""Classify cortex entities as artifact | system | concept (schema v24).

Offline and one-time. Writes a JSON artifact and NEVER touches the database;
`evals/apply_entity_kinds.py` is the human-gated step that commits it.

Scoping is the dominant token lever, not batch size. An entity only matters if
it carries at least one transient-looking attribute -- otherwise every one of
its facts resolves evergreen whatever its kind. On the live bank that is
2415 facts -> 1005 entities -> 264 decision-relevant -> 233 needing judgement,
a 10.4x reduction before a single model call.

Batch 50. Larger batches degrade through lost-in-the-middle attention, label
streaking (the model pattern-matches its own recent outputs), correlated
failure (one malformed response loses the whole batch) and no retry
granularity. Batching also helps, because this is a comparative judgement --
seeing `0-9-0-release` beside `daemon` makes the distinction salient. Fifty
keeps every item in the high-attention zone while preserving that.

Usage:
    python evals/classify_entity_kinds.py --out evals/results/entity-kinds-<tag>.json
    python evals/classify_entity_kinds.py --gold tests/fixtures/entity_kinds_gold.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Iterator

import psycopg
import urllib.request

KINDS = ("artifact", "system", "concept")
DEFAULT_DSN = os.environ.get(
    "PL_DSN", "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory")
SHIM_URL = os.environ.get("PL_SHIM_URL", "http://127.0.0.1:8082/v1/chat/completions")

# The policy lives in exactly ONE place. freshness.py is pure stdlib by
# design ("can be loaded by file path from the gateway venv"), so load it by
# path rather than through the package: `pseudolife_memory.memory.__init__`
# pulls torch, which this offline harness must not require. One copy means no
# drift, and no parity test to keep two copies honest.
def _load_freshness():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pl_freshness",
        Path(__file__).resolve().parents[1]
        / "pseudolife_memory" / "memory" / "freshness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_freshness = _load_freshness()


def _is_transient(attribute_norm: str) -> bool:
    """True when a `system` entity's fact at this attribute would be volatile."""
    return _freshness.resolve_class("system", attribute_norm) == "volatile"

# Names that are frozen in time by construction -- no model needed.
_RULE_ARTIFACT = re.compile(
    r"(^|-)(20\d{2}-\d{2}|release|commit-|pr-\d+|programme)|(^|-)v?\d+-\d+-\d+(-|$)")

SYSTEM_PROMPT = """You classify entities from a personal knowledge base.

For each entity name, answer with exactly one kind:

- "artifact": frozen in time. A specific release, commit, dated run, PR, or a
  completed programme. Facts about it stay true forever, because the thing
  itself never changes again.
- "system": live and mutable. A daemon, server, repository, deployed model,
  or running service. Facts about its current state go out of date.
- "concept": abstract or definitional. A design pattern, policy, lesson, or
  idea. Facts about it are durable.

The distinction that matters: "0-9-0-release" is an artifact (version 0.9.0
shipped with whatever it shipped with, forever), while "daemon" is a system
(its version changes under you).

Reply with ONLY a JSON object mapping each name to its kind. No prose."""


def batched(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def rule_kind(entity_norm: str) -> str | None:
    """Confident lexical classification, or None to defer to the model."""
    return "artifact" if _RULE_ARTIFACT.search(entity_norm or "") else None


def scope_entities(rows: list[tuple[str, str]]) -> list[str]:
    """Entities whose kind can actually change an outcome, in stable order."""
    keep, seen = [], set()
    relevant = {e for e, a in rows if _is_transient(a)}
    for e, _a in rows:
        if e in relevant and e not in seen:
            seen.add(e)
            keep.append(e)
    return keep


def parse_batch(text: str, batch: list[str]) -> dict[str, str]:
    """Labels for the requested entities. Unparseable input yields {} -- the
    caller then writes no kind, and resolve_class falls back to evergreen.
    Never guess a label."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\n|\n```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        raw = json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    want = set(batch)
    return {k: v for k, v in raw.items()
            if k in want and isinstance(v, str) and v in KINDS}


def _ask(batch: list[str], model: str, timeout: float) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps(batch)}],
    }).encode()
    req = urllib.request.Request(
        SHIM_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def _fetch_rows(dsn: str) -> list[tuple[str, str]]:
    with psycopg.connect(dsn) as conn:
        return [(r[0], r[1]) for r in conn.execute(
            "SELECT entity_norm, attribute_norm FROM facts "
            "WHERE status='current' ORDER BY entity_norm, attribute_norm").fetchall()]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dsn", default=DEFAULT_DSN)
    p.add_argument("--model", default="claude-fable-5")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--gold", type=Path, default=None,
                   help="Score against a gold set instead of classifying the bank.")
    a = p.parse_args()

    if a.gold:
        gold = json.loads(a.gold.read_text(encoding="utf-8"))
        names = [g["entity_norm"] for g in gold]
        truth = {g["entity_norm"]: g["kind"] for g in gold}
    else:
        rows = _fetch_rows(a.dsn)
        names, truth = scope_entities(rows), {}

    ruled = {n: k for n in names if (k := rule_kind(n))}
    ask = [n for n in names if n not in ruled]
    print(f"scoped={len(names)} rule={len(ruled)} model={len(ask)} "
          f"batch={a.batch_size}")

    labels, failures = dict(ruled), []
    for i, batch in enumerate(batched(ask, a.batch_size), 1):
        try:
            got = parse_batch(_ask(batch, a.model, a.timeout), batch)
        except Exception as exc:                      # noqa: BLE001
            got, _ = {}, failures.append(f"batch {i}: {type(exc).__name__}")
        missing = [n for n in batch if n not in got]
        if missing:
            failures.append(f"batch {i}: {len(missing)} unlabelled")
        labels.update(got)
        print(f"  batch {i}: {len(got)}/{len(batch)} labelled")

    out = {"model": a.model, "batch_size": a.batch_size,
           "scoped": len(names), "rule_labelled": len(ruled),
           "model_labelled": len(labels) - len(ruled),
           "unlabelled": len(names) - len(labels),
           "failures": failures, "labels": labels,
           "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    if truth:
        scored = [(n, truth[n], labels.get(n, "(none)")) for n in names]
        correct = sum(1 for _n, t, g in scored if t == g)
        out["accuracy"] = round(correct / len(scored), 4)
        out["errors"] = [{"entity": n, "gold": t, "got": g}
                         for n, t, g in scored if t != g]
        print(f"accuracy {correct}/{len(scored)} = {out['accuracy']:.1%}")

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
```

`tests/fixtures/entity_kinds_gold.json` is **already written and committed** —
40 entries drawn from real entity names on the live bank
(20 `artifact` / 14 `system` / 6 `concept`), deliberately weighted to the
ambiguous class. It contains the motivating pair (`0-9-0-release` and `daemon`,
both carrying `schema-version`) and several genuinely hard cases:
`atlas-stage-1` is an `artifact` (a completed stage — its deployment status
never changes again) while `atlas-review-queue` is a `system` (a live queue).
Verify it with:

```bash
.venv/Scripts/python.exe -c "import json,collections; g=json.load(open('tests/fixtures/entity_kinds_gold.json',encoding='utf-8')); print(len(g), collections.Counter(x['kind'] for x in g))"
```

Expected: `40 Counter({'artifact': 20, 'system': 14, 'concept': 6})`

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_classify_entity_kinds.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add evals/classify_entity_kinds.py tests/test_classify_entity_kinds.py tests/fixtures/entity_kinds_gold.json
git commit -m "feat(evals): offline entity-kind classifier + gold set

Scoping is the dominant token lever, not batch size: only entities carrying
a transient attribute can change an outcome, so 2415 facts -> 1005 entities
-> 264 relevant -> 233 needing judgement. Batch 50 in five calls; a
malformed response emits NO label rather than a guess, so resolve_class
falls back to evergreen.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: Gated apply + fact recompute

**Files:**
- Create: `evals/apply_entity_kinds.py`
- Test: `tests/test_apply_entity_kinds.py` (create)

**Interfaces:**
- Consumes: the artifact from Task 4; `PostgresStorage.upsert_entity_kinds` (Task 2); `freshness.resolve_class` (Task 1).
- Produces: `apply_entity_kinds.plan_updates(labels, rows) -> list[tuple[str, str, str]]` of `(entity_norm, attribute_norm, new_class)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_apply_entity_kinds.py`:

```python
"""Applying an entity-kind artifact -- planning is pure and reviewable."""
from __future__ import annotations

import pytest

from evals import apply_entity_kinds as A


@pytest.mark.parametrize("attribute", [
    "deploy-status", "deployment-status", "schema-version", "current-branch",
    "live-url", "build-state", "deployment-date", "commit-hash", "language",
])
@pytest.mark.parametrize("kind", ["system", "artifact", "concept", None])
def test_apply_uses_the_same_policy_as_the_write_path(kind, attribute):
    """The recompute delegates to the one canonical resolve_class. If this
    ever forks into a private copy, the backfill writes classes new writes
    would never reproduce, and nothing would surface the divergence."""
    from pseudolife_memory.memory import freshness
    assert A._resolve(kind, attribute) == freshness.resolve_class(kind, attribute)


def test_plan_marks_only_system_transient_pairs_volatile():
    labels = {"daemon": "system", "0-9-0-release": "artifact"}
    rows = [("daemon", "schema-version"), ("daemon", "language"),
            ("0-9-0-release", "schema-version")]
    assert A.plan_updates(labels, rows) == [("daemon", "schema-version", "volatile")]


def test_plan_is_empty_without_labels():
    rows = [("daemon", "schema-version")]
    assert A.plan_updates({}, rows) == []


def test_plan_skips_pairs_already_at_the_target_class():
    labels = {"daemon": "system"}
    rows = [("daemon", "schema-version", "volatile")]
    assert A.plan_updates(labels, [(e, a) for e, a, _ in rows],
                          current={("daemon", "schema-version"): "volatile"}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_apply_entity_kinds.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'evals.apply_entity_kinds'`

- [ ] **Step 3: Write minimal implementation**

Create `evals/apply_entity_kinds.py`:

```python
"""Apply an entity-kind artifact to the bank (human-gated).

Two writes, both reversible: entity_kinds rows, and a recompute of
facts.freshness_class through the SAME resolve_class the write path uses --
one policy, not two implementations that drift.

Reverting: `UPDATE facts SET freshness_class='evergreen'` restores the
pre-run state wholesale, and dropping entity_kinds reverts the write path.
BACK UP FIRST -- ops/backup.ps1.

Usage:
    python evals/apply_entity_kinds.py --artifact <path>            # dry run
    python evals/apply_entity_kinds.py --artifact <path> --apply
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import psycopg

DEFAULT_DSN = os.environ.get(
    "PL_DSN", "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory")

# Same single-copy rule as the classifier: load freshness.py by path so the
# recompute uses the EXACT policy the write path uses. Two implementations
# would drift, and the drift would be invisible -- the backfill would write
# classes new writes never reproduce.
def _load_freshness():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pl_freshness",
        Path(__file__).resolve().parents[1]
        / "pseudolife_memory" / "memory" / "freshness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_freshness = _load_freshness()


def _resolve(kind: str | None, attribute_norm: str) -> str:
    return _freshness.resolve_class(kind, attribute_norm)


def plan_updates(labels: dict[str, str], rows: list[tuple[str, str]],
                 current: dict[tuple[str, str], str] | None = None
                 ) -> list[tuple[str, str, str]]:
    """(entity, attribute, new_class) for every pair whose class changes."""
    cur = current or {}
    out = []
    for e, a in rows:
        want = _resolve(labels.get(e), a)
        if want != cur.get((e, a), "evergreen"):
            out.append((e, a, want))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--artifact", type=Path, required=True)
    p.add_argument("--dsn", default=DEFAULT_DSN)
    p.add_argument("--apply", action="store_true",
                   help="Without this the run is a dry run and writes nothing.")
    a = p.parse_args()

    art = json.loads(a.artifact.read_text(encoding="utf-8"))
    labels = art["labels"]

    with psycopg.connect(a.dsn) as conn:
        rows = [(r[0], r[1]) for r in conn.execute(
            "SELECT entity_norm, attribute_norm FROM facts "
            "WHERE status='current'").fetchall()]
        current = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT entity_norm, attribute_norm, freshness_class FROM facts "
            "WHERE status='current'").fetchall()}

        updates = plan_updates(labels, rows, current)
        print(f"kinds={len(labels)} fact_updates={len(updates)}")
        for e, at, c in updates[:15]:
            print(f"  {e} / {at} -> {c}")
        if not a.apply:
            print("dry run -- nothing written. Re-run with --apply.")
            return

        now = time.time()
        with conn.transaction():
            for e, k in labels.items():
                conn.execute(
                    "INSERT INTO entity_kinds "
                    "(entity_norm, kind, origin, confidence, decided_at) "
                    "VALUES (%s,%s,'model',NULL,%s) "
                    "ON CONFLICT (entity_norm) DO UPDATE SET "
                    "kind=EXCLUDED.kind, origin=EXCLUDED.origin, "
                    "decided_at=EXCLUDED.decided_at", (e, k, now))
            for e, at, c in updates:
                conn.execute(
                    "UPDATE facts SET freshness_class=%s "
                    "WHERE entity_norm=%s AND attribute_norm=%s AND status='current'",
                    (c, e, at))
    print(f"applied {len(labels)} kinds, {len(updates)} fact updates")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_apply_entity_kinds.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add evals/apply_entity_kinds.py tests/test_apply_entity_kinds.py
git commit -m "feat(evals): human-gated entity-kind apply

Dry run by default. Recomputes facts.freshness_class through the same
resolve_class the write path uses, so there is one policy rather than two
that drift. Fully reversible: reset the column, drop the table.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: Docs, CHANGELOG, and the v24 four-places

**Files:**
- Modify: `CHANGELOG.md`, `README.md`, `docs/guide/configuration.md`, `docs/guide/memory-model.md`

- [ ] **Step 1: Add the CHANGELOG entry under `[Unreleased]`**

```markdown
### Added (2026-07-27 — freshness is inferred from the entity's kind; schema **v24**)
- **`entity_kinds` (schema v24) stores one kind per entity** — `artifact`
  (frozen in time), `system` (live), `concept` (abstract) — and
  `freshness.resolve_class(kind, attribute)` turns that into a fact's
  `freshness_class`. Only `system` entities can yield `volatile`, so the 282
  facts about frozen artifacts are structurally protected from decaying.
- **Why kind and not attribute name.** `0-9-0-release / schema-version` is
  permanently true; `daemon / schema-version` rots. Same attribute, opposite
  class — the name cannot decide it, the entity's kind can.
- **No model call on the write path.** A new fact looks its entity's kind up in
  a cached map and applies a pure function, because this runs on every dream.
  `freshness_class` now defaults to the sentinel `"auto"`; explicit values are
  still honoured, and an empty kind map reproduces v23 behaviour exactly.
- **Scoping, not batch size, is the token lever.** An entity only matters if it
  carries a transient attribute: 2,415 facts → 1,005 entities → 264
  decision-relevant → 233 needing model judgement, a 10.4× reduction before a
  single call. Backfill runs at batch 50 over five calls.
- Measured first: entity *aliasing* was the presumed root cause and is
  explicitly out of scope — on the live bank only ~4–6 of 74 lexical clusters
  are genuine aliases, reproducing the Stage 1.5 finding on real data.
```

- [ ] **Step 2: Update the version-history surfaces**

- `README.md`: bump the schema line from v23 to v24.
- `docs/guide/configuration.md`: update the DSN row, the "current Postgres meta
  version" line, and add a v24 row to the version-history table.
- `docs/guide/memory-model.md`: in the "How current is this fact?" section, add
  that `freshness_class` is now inferred from the entity's kind unless the
  caller passes one explicitly, and state the artifact/system distinction.

- [ ] **Step 3: Run the full suite**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all pass, including `test_release_ux.py` (CHANGELOG mentions `v24`) and `test_eval_evidence.py`.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md README.md docs/guide/configuration.md docs/guide/memory-model.md
git commit -m "docs: schema v24 entity-kind freshness inference

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: Gold-set gate, then the live backfill (GATED — stop for the user)

**This task writes to the live bank. It does not proceed without an explicit go-ahead.**

- [ ] **Step 1: Start the Fable shim**

```bash
.venv/Scripts/python.exe evals/sonnet_shim.py --port 8082 --model claude-fable-5
```

Verify: `curl -s http://127.0.0.1:8082/health`

- [ ] **Step 2: Score the gold set at batch 40 and batch 10**

```bash
.venv/Scripts/python.exe evals/classify_entity_kinds.py --gold tests/fixtures/entity_kinds_gold.json --batch-size 40 --out evals/results/entity-kinds-gold-b40.json
.venv/Scripts/python.exe evals/classify_entity_kinds.py --gold tests/fixtures/entity_kinds_gold.json --batch-size 10 --out evals/results/entity-kinds-gold-b10.json
```

This is the batch-size claim measured rather than asserted; both artifacts are committed.

- [ ] **Step 3: GATE — report and stop**

Report both accuracies and every error row. **Do not proceed to Step 4** without
the user's go-ahead. If accuracy on the ambiguous subset is poor, say so plainly
and stop — an unusable label set is a result, not a failure to work around.

- [ ] **Step 4: Back up, then classify the live bank**

```bash
pwsh -File ops/backup.ps1
.venv/Scripts/python.exe evals/classify_entity_kinds.py --batch-size 50 --out evals/results/entity-kinds-live-20260727.json
```

- [ ] **Step 5: Dry-run the apply, review, then apply**

```bash
.venv/Scripts/python.exe evals/apply_entity_kinds.py --artifact evals/results/entity-kinds-live-20260727.json
.venv/Scripts/python.exe evals/apply_entity_kinds.py --artifact evals/results/entity-kinds-live-20260727.json --apply
```

- [ ] **Step 5b: Restart the daemon — REQUIRED, not optional**

```bash
docker restart pseudolife-mcp-daemon
curl -s http://127.0.0.1:8765/health
```

The service caches the entity-kind map for the life of the process, and the
apply above runs in a *separate* process. Without this restart the running
daemon keeps its pre-backfill map — almost always empty — and every new fact
resolves `evergreen` forever, so the feature is live in the database and inert
in the daemon. Step 6's SQL would still pass, because the apply rewrote
`facts.freshness_class` directly; only a write through the daemon exposes it.

- [ ] **Step 6: Verify live**

```bash
docker exec pseudolife-mcp-postgres psql -U pseudolife -d pseudolife_memory -c "SELECT kind, count(*) FROM entity_kinds GROUP BY 1;"
docker exec pseudolife-mcp-postgres psql -U pseudolife -d pseudolife_memory -c "SELECT freshness_class, count(*) FROM facts WHERE status='current' GROUP BY 1;"
docker exec pseudolife-mcp-postgres psql -U pseudolife -d pseudolife_memory -c "SELECT entity_norm, attribute_norm, value, freshness_class FROM facts WHERE attribute_norm ~ 'schema.?version' AND status='current' ORDER BY entity_norm;"
```

The last query is the acceptance check on stored data: `daemon / schema-version`
must read `volatile` while `0-9-0-release / schema-version` reads `evergreen`.

Then prove the **write path** is live too — the SQL above passes even when the
daemon is ignoring the backfill, so this is the check that actually matters:

```bash
.venv/Scripts/python.exe - <<'PY'
import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main():
    async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("memory_fact_set", {
                "entity": "pseudolife-mcp", "attribute": "deploy-status",
                "value": "green", "origin": "action"})   # note: no freshness_class
            print(json.loads(res.content[0].text)["freshness_class"])
asyncio.run(main())
PY
```

Expected `volatile` — inferred, with no `freshness_class` passed. `evergreen`
here means the daemon did not pick up the backfill: re-check Step 5b.

- [ ] **Step 7: Commit the run artifacts**

```bash
git add evals/results/entity-kinds-*.json
git commit -m "test(evals): entity-kind gold-set + live backfill artifacts

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
