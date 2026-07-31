# Set-Valued Cortex Slots (C2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cortex slots that hold sets — member-per-record with full add/remove lifecycle — written by both the dream extractor and new MCP tools, served as one entry per slot.

**Architecture:** Approach A from `docs/superpowers/specs/2026-07-30-set-valued-slots-design.md`: each member is a `CortexRecord` with `kind='member'` under a shared `(entity, attribute)` slot; the existing supersession/HLC/provenance machinery applies per member; a new `removed` status is the membership analogue of supersession. Schema v26 adds `kind` + `value_norm` columns and splits the current-uniqueness index by kind.

**Tech Stack:** Python 3.11, Postgres (psycopg), torch CPU embeddings, pytest. No new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-30-set-valued-slots-design.md` — rules there win on conflict.
- Schema bump v25 → v26 touches four places together (CLAUDE.md rule): `SCHEMA_META_VERSION`, README capabilities table + `docs/guide/configuration.md` DSN/version rows, version-pin tests (+ new `tests/test_schema_v26.py`), CHANGELOG mention of `v26`.
- TDD with a watched RED for every behavior. Full suite before commit: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/` with bench Postgres up (127.0.0.1:5433).
- Constants fixed by this plan: `MEMBER_DEDUP_COSINE = 0.9`, `MAX_CURRENT_MEMBERS = 100`.
- Kind-conflict rules (spec): member-add to scalar slot converts (audited); scalar write to set slot → tools raise `ValueError`, extractor logs and drops.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`; professional voice, no persona.
- MCP tool params are plain strings (client anyOf-stringification bug).

---

### Task 1: Schema v26 — columns, index split, version pins

**Files:**
- Modify: `pseudolife_memory/storage/schema.py` (SCHEMA_META_VERSION at :18; facts DDL at :155; migration block near :469; index block near :539)
- Create: `tests/test_schema_v26.py`
- Modify: any test pinning `SCHEMA_META_VERSION == 25` (grep `SCHEMA_META_VERSION` and `= 25` under tests/; update pins the way v25 did)

**Interfaces:**
- Produces: `facts.kind TEXT NOT NULL DEFAULT 'scalar'`, `facts.value_norm TEXT`, partial unique indexes `facts_slot_current_scalar_uq (entity_norm, attribute_norm) WHERE status='current' AND kind='scalar'` and `facts_member_current_uq (entity_norm, attribute_norm, value_norm) WHERE status='current' AND kind='member'`. Old `facts_slot_current_uq` dropped.

- [ ] **Step 1: Write the failing test** (`tests/test_schema_v26.py`, pattern from `tests/test_schema_v16.py` — connect to the bench DB, apply schema, inspect):

```python
"""Schema v26: set-valued slots — kind/value_norm columns + split uniqueness."""
import pytest

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

pg = pytest.importorskip("psycopg")


def test_meta_version_is_26():
    assert SCHEMA_META_VERSION == 26


def test_facts_has_kind_and_value_norm(bench_conn):  # reuse the suite's bench-PG fixture
    cols = {r[0] for r in bench_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'facts'")}
    assert "kind" in cols and "value_norm" in cols


def test_current_uniqueness_split_by_kind(bench_conn):
    idx = {r[0] for r in bench_conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'facts'")}
    assert "facts_slot_current_scalar_uq" in idx
    assert "facts_member_current_uq" in idx
    assert "facts_slot_current_uq" not in idx
```

(Adopt the exact fixture name the existing schema tests use — check `tests/test_schema_v16.py` before writing; if it builds its own connection, mirror that instead of `bench_conn`.)

- [ ] **Step 2: Run to verify it fails** — `pytest tests/test_schema_v26.py -q` → FAIL on `SCHEMA_META_VERSION == 26` (currently 25).

- [ ] **Step 3: Implement.** In `schema.py`: bump `SCHEMA_META_VERSION = 26`; add the two columns to the `CREATE TABLE facts` DDL; in the additive-migration block add:

```python
        # v26 additive: set-valued slots. kind partitions the
        # current-uniqueness constraint; value_norm is the member identity
        # the member index dedupes on (NULL on scalar rows).
        cur.execute(
            "ALTER TABLE facts ADD COLUMN IF NOT EXISTS kind "
            "TEXT NOT NULL DEFAULT 'scalar'")
        cur.execute(
            "ALTER TABLE facts ADD COLUMN IF NOT EXISTS value_norm TEXT")
        cur.execute("DROP INDEX IF EXISTS facts_slot_current_uq")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS facts_slot_current_scalar_uq "
            "ON facts (entity_norm, attribute_norm) "
            "WHERE status = 'current' AND kind = 'scalar'")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS facts_member_current_uq "
            "ON facts (entity_norm, attribute_norm, value_norm) "
            "WHERE status = 'current' AND kind = 'member'")
```

Replace the old `facts_slot_current_uq` CREATE at :539 with the scalar-scoped one (idempotent re-runs must not recreate the dropped index).

- [ ] **Step 4: Run** `pytest tests/test_schema_v26.py tests/test_schema_v13.py tests/test_schema_v16.py tests/test_temporal_stamp.py -q` → PASS (update any `== 25` pins found in Step 0 grep).

- [ ] **Step 5: Commit** — `git add -A pseudolife_memory/storage/schema.py tests/ && git commit -m "feat(schema): v26 — kind/value_norm columns, current-uniqueness split by kind"`

---

### Task 2: CortexStore member model — add, remove, dedup, convert, cap

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py` (`CortexRecord` at :106, `write_fact` at :227, `_insert` at :314, `current_records` at :513)
- Test: `tests/test_cortex_sets.py` (new)

**Interfaces:**
- Consumes: `Slot(entity, attribute, value)`, `WriteResult(action, record)`, `_norm_key`, `_norm_value` (all existing in cortex.py).
- Produces (later tasks rely on these exact names):
  - `CortexRecord.kind: str = "scalar"` (`"scalar" | "member"`)
  - `MEMBER_DEDUP_COSINE = 0.9`, `MAX_CURRENT_MEMBERS = 100` (module constants)
  - `CortexStore.add_member(slot, embedding, *, confidence=0.7, provenance=(), support=None, now=None, hlc=None, writer_id=None, session_id=None) -> WriteResult` with actions `"member_added" | "member_confirmed" | "member_capped"` (a conversion emits `"member_added"` and the superseded scalar is audit-visible)
  - `CortexStore.remove_member(entity, attribute, member, *, now=None) -> WriteResult` with actions `"member_removed" | "member_not_found"`
  - `CortexStore.members(entity, attribute, include_removed=False) -> list[CortexRecord]`
  - `CortexStore.slot_kind(entity, attribute) -> str | None` (`"scalar" | "set" | None`)
  - New record status literal `"removed"` (timestamp reuses `superseded_at`)
- Internal: `self._members: dict[tuple[str, str], list[int]]` index of current member row positions, maintained by insert/remove/load.

- [ ] **Step 1: Write the failing tests** (unit-level, no Postgres — construct `CortexStore` directly the way `tests/test_cortex.py` does; copy its embedder/store setup helper):

```python
def test_add_member_creates_set_slot(store, emb):
    r = store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    assert r.action == "member_added" and r.record.kind == "member"
    assert store.slot_kind("user", "bikes owned") == "set"
    assert [m.value for m in store.members("user", "bikes owned")] == ["road bike"]


def test_add_member_dedup_confirms_not_duplicates(store, emb):
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    r = store.add_member(Slot("user", "bikes owned", "Road Bike"), emb("Road Bike"))
    assert r.action == "member_confirmed"
    assert len(store.members("user", "bikes owned")) == 1


def test_remove_member_keeps_audit_row(store, emb):
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    r = store.remove_member("user", "bikes owned", "road bike")
    assert r.action == "member_removed" and r.record.status == "removed"
    assert store.members("user", "bikes owned") == []
    removed = store.members("user", "bikes owned", include_removed=True)
    assert [m.value for m in removed] == ["road bike"]
    assert removed[0].superseded_at is not None


def test_member_add_converts_scalar_slot_with_audit(store, emb):
    store.write_fact(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    r = store.add_member(Slot("user", "bikes owned", "gravel bike"), emb("gravel bike"))
    assert r.action == "member_added"
    assert store.slot_kind("user", "bikes owned") == "set"
    vals = sorted(m.value for m in store.members("user", "bikes owned"))
    assert vals == ["gravel bike", "road bike"]      # scalar re-minted as member
    audit = [x for x in store.records
             if x.status == "superseded" and x.superseded_by_value == "(converted to set)"]
    assert len(audit) == 1


def test_member_cap_drops_beyond_100(store, emb):
    for i in range(100):
        store.add_member(Slot("user", "tags", f"tag-{i:03d}"), emb(f"tag-{i:03d}"))
    r = store.add_member(Slot("user", "tags", "tag-overflow"), emb("tag-overflow"))
    assert r.action == "member_capped"
    assert len(store.members("user", "tags")) == 100


def test_removed_member_can_rejoin(store, emb):
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    store.remove_member("user", "bikes owned", "road bike")
    r = store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    assert r.action == "member_added"
    assert len(store.members("user", "bikes owned")) == 1
    assert len(store.members("user", "bikes owned", include_removed=True)) == 2
```

- [ ] **Step 2: Run to verify RED** — `pytest tests/test_cortex_sets.py -q` → FAIL: `CortexStore has no attribute 'add_member'`.

- [ ] **Step 3: Implement in cortex.py.** Add `kind: str = "scalar"` to `CortexRecord`. Module constants. `add_member`:

```python
    def add_member(self, slot, embedding, *, confidence=0.7, provenance=(),
                   support=None, now=None, hlc=None, writer_id=None,
                   session_id=None) -> WriteResult:
        t = time.time() if now is None else float(now)
        key = (_norm_key(slot.entity), _norm_key(slot.attribute))
        emb = embedding.detach().to("cpu", torch.float32).clone()
        self.dirty_slots.add(key)
        # Scalar at this slot -> one-way conversion (spec rule 1).
        idx = self._current.get(key)
        if idx is not None:
            cur = self.records[idx]
            cur.status = "superseded"
            cur.superseded_at = t
            cur.superseded_by_value = "(converted to set)"
            del self._current[key]
            self._log(cur, slot.value, confidence, t, "convert_to_set",
                      "member_add_to_scalar", writer_id=writer_id,
                      session_id=session_id)
            self._insert_member(Slot(cur.entity, cur.attribute, cur.value),
                                cur.embedding, cur.confidence,
                                set(cur.provenance), cur.asserted_at,
                                hlc=hlc, writer_id=cur.writer_id,
                                session_id=cur.session_id)
        # Dedup against current members: exact norm OR cosine >= threshold.
        members = self.members(slot.entity, slot.attribute)
        for m in members:
            same_norm = _norm_value(m.value) == _norm_value(slot.value)
            cos = float((m.embedding.reshape(-1) @ emb.reshape(-1))
                        / ((m.embedding.norm() * emb.norm()) + 1e-12)) \
                if m.embedding is not None else 0.0
            if same_norm or cos >= MEMBER_DEDUP_COSINE:
                m.last_confirmed = t
                m.provenance |= {p for p in provenance if p}
                m.confidence = min(1.0, max(m.confidence, float(confidence)))
                return WriteResult("member_confirmed", m)
        if len(members) >= MAX_CURRENT_MEMBERS:
            self._log(members[-1], slot.value, confidence, t, "member_capped",
                      "max_current_members", writer_id=writer_id,
                      session_id=session_id)
            return WriteResult("member_capped", members[-1])
        rec = self._insert_member(slot, emb, confidence,
                                  {p for p in provenance if p}, t, hlc=hlc,
                                  writer_id=writer_id, session_id=session_id,
                                  support=support)
        return WriteResult("member_added", rec)
```

`_insert_member` mirrors `_insert` but sets `kind="member"` and registers in `self._members[key]` instead of `self._current`. `remove_member` finds the current member by `_norm_value` equality (then cosine fallback), flips `status="removed"`, sets `superseded_at`, drops it from `_members`, logs `"member_removed"`; returns `member_not_found` when no match. `members()` reads `_members` (or scans records when `include_removed=True`). `slot_kind()`: `"set"` if `_members.get(key)` or any member row exists at the key, `"scalar"` if `key in self._current`, else `None`. Keep `current_records()` returning scalars AND members (search must see members); verify no existing test assumes scalars-only — if one does, it names the behavior change and gets updated consciously, in this commit, with a comment.

- [ ] **Step 4: Run** `pytest tests/test_cortex_sets.py tests/test_cortex.py -q` → PASS.

- [ ] **Step 5: Commit** — `feat(cortex): member-per-record set slots with add/remove lifecycle`

---

### Task 3: Persistence roundtrip

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (`_FACT_COLS` near :47, the None-guards near :353/:386), `pseudolife_memory/storage/sync.py` (hydrate path), `pseudolife_memory/service.py` (fact save/load mapping if service owns it — follow where `freshness_class` was threaded in v23 and do exactly that for `kind` and `value_norm`)
- Test: `tests/test_cortex_sets.py` (extend)

**Interfaces:**
- Consumes: Task 2's record fields. `value_norm` on save = `_norm_value(record.value)` for members, NULL for scalars.
- Produces: members survive a service restart with kind/status intact; `_members` index rebuilt on load.

- [ ] **Step 1: Failing test** (service-level, temp data dir, same pattern as `tests/test_cortex_service.py::test_cortex_copersists_across_service_restart`):

```python
def test_members_survive_restart(tmp_service_dir):
    svc = MemoryService(data_dir=tmp_service_dir)
    svc.set_add("user", "bikes owned", "road bike")
    svc.set_add("user", "bikes owned", "gravel bike")
    svc.set_remove("user", "bikes owned", "road bike")
    svc.save()
    svc2 = MemoryService(data_dir=tmp_service_dir)
    got = svc2.cortex_lookup("user", "bikes owned")
    assert got["kind"] == "set"
    assert [m["value"] for m in got["members"]] == ["gravel bike"]
    assert [m["value"] for m in got["removed"]] == ["road bike"]
```

(This test needs Task 4's `svc.set_add` — write it now, mark it `pytest.mark.skip` until Task 4 lands, and unskip in Task 4. The store-level roundtrip assertion below is this task's RED.)

```python
def test_store_roundtrip_preserves_kind(store_with_pg):
    store, reload_store = store_with_pg
    store.add_member(Slot("user", "tags", "alpha"), emb("alpha"))
    fresh = reload_store()
    assert fresh.slot_kind("user", "tags") == "set"
    assert [m.value for m in fresh.members("user", "tags")] == ["alpha"]
```

(Adopt the exact persistence-fixture idiom the existing storage tests use — find it in `tests/` by grepping `load_facts` before writing.)

- [ ] **Step 2: RED** — kind column not saved/loaded → fresh store sees no members.
- [ ] **Step 3: Implement** — add `"kind", "value_norm"` to `_FACT_COLS`; map on save (`value_norm = _norm_value(value) if kind == "member" else None`) and load (`kind = row.get("kind") or "scalar"`); rebuild `_members` wherever `_current` is rebuilt (grep `_current[` in cortex.py — every rebuild site gains the member branch; the derived-state rule: enumerate ALL mutation paths, including `hydrate_cms`/`load()`).
- [ ] **Step 4: GREEN** including the full storage test files.
- [ ] **Step 5: Commit** — `feat(storage): persist member kind/value_norm; rebuild member index on load`

---

### Task 4: Service surface — set_add / set_remove / lookup / history / rejection

**Files:**
- Modify: `pseudolife_memory/service.py` (`cortex_write` at :1455, `cortex_lookup` at :1538, `history` at :2401)
- Test: `tests/test_cortex_sets.py` (extend; unskip the restart test)

**Interfaces:**
- Consumes: Task 2 store methods.
- Produces:
  - `MemoryService.set_add(entity: str, attribute: str, member: str, provenance=None, origin=None) -> dict` → `{"action", "entity", "attribute", "member", "members_count"}`
  - `MemoryService.set_remove(entity: str, attribute: str, member: str) -> dict` (same shape)
  - `cortex_lookup` on a set slot → `{"kind": "set", "entity", "attribute", "members": [record dicts], "removed": [record dicts]}`
  - `history(entity, attribute)` on a set slot → `{"kind": "set", "versions": [{"value", "event": "added"|"removed", "at": ts}, ...]}` HLC/time-ordered
  - `cortex_write` (and therefore `memory_fact_set`) on a set slot → raises `ValueError("slot holds a set; use memory_set_add / memory_set_remove")`

- [ ] **Step 1: Failing tests** — service-level: add/remove roundtrip shape, scalar-write rejection (`pytest.raises(ValueError, match="memory_set_add")`), history timeline event order, and unskip `test_members_survive_restart`.
- [ ] **Step 2: RED** — `MemoryService has no attribute 'set_add'`.
- [ ] **Step 3: Implement** — `set_add` embeds the member text with the store's embedder (same `encode_single` composition scalar writes use: `f"{entity} {attribute} {member}"`), calls `store.add_member` under `self._lock`, persists via the same txn path `cortex_write` uses (follow it exactly — `_txn` with COMMITTED check). `set_remove` analogous. `cortex_lookup`/`history` branch on `store.slot_kind(...) == "set"`.
- [ ] **Step 4: GREEN** — `pytest tests/test_cortex_sets.py tests/test_cortex_service.py -q`.
- [ ] **Step 5: Commit** — `feat(service): set_add/set_remove, set-aware lookup/history, scalar-write rejection`

---

### Task 5: MCP tools

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` (register beside `memory_fact_set`; same tier), `pseudolife_memory/web/fixtures.py` if the console fixture mirrors tool lists
- Test: `tests/test_tool_consolidation.py` idiom — find the existing test that asserts `memory_fact_set` is registered and add the two set tools to the same assertion; plus one call-through test

**Interfaces:**
- Produces: MCP tools `memory_set_add(entity, attribute, member)` and `memory_set_remove(entity, attribute, member)` — string params only — returning the Task 4 dicts; docstrings state the conversion rule and name `memory_fact_get` for reads.

- [ ] **Step 1: Failing test** — registration + a call-through that stores and reads back via the MCP layer (mirror how existing fact tools are tested; grep `fact_set` in tests/).
- [ ] **Step 2: RED.**
- [ ] **Step 3: Implement** — thin wrappers over `svc.set_add`/`svc.set_remove`; `ValueError` from the rejection path maps to the MCP error string convention used by `memory_fact_set` (grep how it reports validation errors and match it).
- [ ] **Step 4: GREEN.**
- [ ] **Step 5: Commit** — `feat(mcp): memory_set_add / memory_set_remove tools`

---

### Task 6: Serving — one entry per set, bench + rebuild lockstep

**Files:**
- Modify: `pseudolife_memory/service.py` (`cortex_search` — the entry-building loop after the Task-2-aware `hits`), `evals/rebuild_contexts.py` (`rebuild_fact_lines`), `evals/longmemeval_bench.py` (`build_contexts` fact-line loop at :270-ish — only if the service entry shape doesn't already flow through)
- Test: `tests/test_cortex_sets.py` + extend `tests/test_cortex_bm25.py::test_rebuild_fact_ranking_matches_service_fusion` with a set-slot case

**Interfaces:**
- Consumes: member records in the search pool (Task 2 kept them in `current_records()`).
- Produces: `cortex_search` returns for a set slot ONE entry: `{"kind": "set", "entity", "attribute", "value": "m1; m2; m3 (3 members)", "members": [...], "score": <max member score>, "contested": False}`. `rebuild_fact_lines` composes the identical line; removed members render as the existing "earlier values" garnish via `history()`.

- [ ] **Step 1: Failing tests** — (a) service: two members + one query naming one member → exactly one entry for the slot, `score == max`, value string exact; (b) lockstep: seed a set slot in both the service and a bank dict (`facts` entries carrying `"kind": "member"`), assert identical ordered lines.
- [ ] **Step 2: RED** — today each member surfaces as its own entry.
- [ ] **Step 3: Implement** — group AFTER `_cortex_bm25_fuse` (fusion stays per-record): collapse member hits by slot key, keep max score, compose the value string; mirror in `rebuild_fact_lines` (bank facts gain optional `"kind"`; treat absent as scalar so all existing banks rebuild byte-identically — assert that with a no-set-slots regression check on one committed bank file).
- [ ] **Step 4: GREEN** — includes `pytest tests/test_cortex_bm25.py -q`.
- [ ] **Step 5: Commit** — `feat(serving): set slots surface as one entry; rebuild lockstep extended`

---

### Task 7: Dream extractor op

**Files:**
- Modify: `pseudolife_memory/memory/dream.py` (claim parsing + apply loop — grep `entity` in the claim-apply function), the extraction prompt (grep the prompt constant in dream.py; also `evals/prompts/` copies used by benches)
- Test: `tests/test_dream*.py` idiom — new cases in whichever file tests claim application (grep `claims` under tests/)

**Interfaces:**
- Consumes: `svc.set_add` / `svc.set_remove` (Task 4); `store.slot_kind` (Task 2).
- Produces: claims may carry `"op": "add" | "remove"`; absent op = scalar supersede (bit-identical to today). Extractor scalar claim to a set slot → logged (`logger.info("dropped scalar claim for set slot %s.%s", ...)`) and dropped, per spec rule 2.

- [ ] **Step 1: Failing tests** — (a) claim with `op: "add"` lands a member; (b) claim with `op: "remove"` removes; (c) claim WITHOUT op on a set slot is dropped, store unchanged; (d) malformed op value falls back to scalar path with a log (no crash mid-dream).
- [ ] **Step 2: RED.**
- [ ] **Step 3: Implement** — parse `op = claim.get("op")` in the apply loop; route add/remove; add the drop-guard before the scalar write. Prompt: add a short "collection membership" instruction block with one add and one remove example, phrased to discourage op on value-updates (the risk named in the spec). Update the `evals/prompts/` extractor copies identically.
- [ ] **Step 4: GREEN** — plus `evals/window_echo_check.py` run unchanged (dream-write-path guard).
- [ ] **Step 5: Commit** — `feat(dream): claim-level op for set membership; scalar-claim drop guard`

---

### Task 8: Four-place checklist, docs, and the pre-registered gates

**Files:**
- Modify: `README.md` (capabilities table row for schema), `docs/guide/configuration.md` (DSN row + version-history table), `docs/guide/memory-model.md` (set-slots section), `CHANGELOG.md` (v26 + feature entry)
- Test: `tests/test_release_ux.py` (pins the doc mentions — run it, fix what it names)
- Run: the gates below, in order, before the PR

**Interfaces:** consumes everything above; produces the evidence for the PR.

- [ ] **Step 1:** Docs + CHANGELOG edits; `pytest tests/test_release_ux.py tests/test_eval_evidence.py -q` → PASS.
- [ ] **Step 2: Pre-register the gate wording via `memory_store` BEFORE any GPU run** (exact criteria, filled at run time with tags): (1) ladder re-run — stale-leak 0.0 required, gold within historical band; (2) KU-oracle fresh e2e, set-capable prompt vs current prompt, paired permutation on the count/aggregate class + no significant overall regression, cascade reported; (3) regression gate PASS. The deterministic membership probe is Tasks 2–7's unit tests — no GPU.
- [ ] **Step 3:** Run the ladder (`evals/ladder_sweep.py`, extractor = shipped default + qwen-27b ceiling). Stale-leak must be 0.0.
- [ ] **Step 4:** Run the KU e2e pair (extract + answer phases; `Start-Qwen`, server-identity check by process inspection, ledger + heartbeats per the unattended rules).
- [ ] **Step 5:** `evals/regression_gate.ps1` → PASS required.
- [ ] **Step 6:** Full suite; commit docs + any promoted artifacts with evidence rows if numbers are published; push branch; PR with honest gate outcomes.

---

### Post-merge (not part of this plan's tasks; user-gated)

Deploy via `ops/update.ps1` (backup → rollback tag → daemon-only rebuild →
health). Live verify per the spec: psql inspection of
`facts_slot_current_scalar_uq` / `facts_member_current_uq`, then an MCP
`memory_set_add` → `memory_fact_get` roundtrip through the daemon.

## Execution notes

- Tasks 1–3 are sequential (schema → model → persistence). Task 4 depends on 2–3; Tasks 5–7 depend on 4 and are mutually independent; Task 8 last.
- Any gate FAIL in Task 8 stops the PR and gets reported plainly with its artifact — the B1 precedent (ship decision goes to the user).
- If an existing test constrains `current_records()` to scalars (Task 2 risk), the change is named in the commit message, not silently absorbed.
