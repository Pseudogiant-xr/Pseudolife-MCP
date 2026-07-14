# Cortex Provenance-Aware Contenders — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a weaker-tier (e.g. agent) write from silently overwriting a stronger-tier (e.g. user-stated) canonical fact; instead park it as a visible *contender* the agent can surface and resolve.

**Architecture:** Add a provenance tier-rank guard to `CortexStore.write_fact`. A conflict that may not supersede (weaker tier, or below the confidence margin) is recorded as a `status="contested"` `CortexRecord` (at most one active per slot), never indexed as current. A new `resolve()` promotes (stamped user-confirmed) or retires it. Service + MCP surface expose contenders via `memory_fact_get`, flag them in `memory_search`, and add a `memory_fact_resolve` tool. Gated by `config.memory.cortex.protect_provenance` (default on).

**Tech Stack:** Python 3.11, PyTorch (cortex embeddings/persistence), FastMCP, pytest. Spec: `docs/specs/2026-06-10-cortex-provenance-contenders-design.md`.

**Test note:** run pytest with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` (models are cached locally; avoids flaky HF HEAD-checks). Store-level tests inject a fake embedder and don't need it, but service/MCP tests do.

---

## File Structure

- Modify `pseudolife_memory/memory/cortex.py` — tier rank, `protect_provenance`, `_contend`, `_active_contender`, `contenders_for`, `resolve`, generalised `_reindex_current`; conflict branch in `write_fact`.
- Modify `pseudolife_memory/service.py` — `cortex_write` (+`current` on contested), `cortex_contenders`, `cortex_resolve`, `cortex_search` (contested flags), thread `protect_provenance` into `CortexStore(...)`.
- Modify `pseudolife_memory/mcp_server.py` — `memory_fact_get` (+contenders), `memory_search` cortex-first (forward contested), new `memory_fact_resolve` tool, `memory_fact_set` docstring nudge.
- Modify `pseudolife_memory/utils/config.py` — `CortexConfig.protect_provenance: bool = True`.
- Tests: extend `tests/test_cortex.py`, `tests/test_cortex_service.py`, `tests/test_mcp_server.py`; new `tests/test_cortex_contenders.py` (service-level).
- Modify `README.md` — document contenders + `memory_fact_resolve`.

---

## Task 0: Feature branch

- [ ] **Step 1: Branch off master**

```bash
git checkout -b cortex-provenance-contenders
git status   # clean tree except the two new docs/specs/*.md (stage them with the first commit)
```

---

## Task 1: Tier-rank guard + contender write path (store-level)

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py`
- Test: `tests/test_cortex.py`

- [ ] **Step 1: Write failing tests** — append to `tests/test_cortex.py` (helpers `_unit`, `CortexStore`, `Slot` already imported there):

```python
def test_agent_write_contends_user_fact_not_supersede():
    store = CortexStore()
    store.write_fact(Slot("box", "ip", "10.0.0.1"), _unit(20), support="user", now=1.0)
    res = store.write_fact(Slot("box", "ip", "10.0.0.2"), _unit(21), support="agent", now=2.0)
    assert res.action == "contested"          # parked, not superseded
    assert store.lookup("box", "ip").value == "10.0.0.1"   # user fact still current
    conts = store.contenders_for("box", "ip")
    assert len(conts) == 1 and conts[0].value == "10.0.0.2"
    assert conts[0].status == "contested"


def test_action_over_agent_supersedes_but_agent_over_action_contends():
    store = CortexStore()
    store.write_fact(Slot("svc", "port", "8080"), _unit(22), support="agent", now=1.0)
    r1 = store.write_fact(Slot("svc", "port", "9090"), _unit(23), support="action", now=2.0)
    assert r1.action == "superseded"          # action (2) >= agent (1)
    assert store.lookup("svc", "port").value == "9090"
    r2 = store.write_fact(Slot("svc", "port", "7070"), _unit(24), support="agent", now=3.0)
    assert r2.action == "contested"           # agent (1) < action (2)
    assert store.lookup("svc", "port").value == "9090"


def test_user_write_supersedes_lower_tier():
    store = CortexStore()
    store.write_fact(Slot("p", "lang", "go"), _unit(25), support="agent", now=1.0)
    r = store.write_fact(Slot("p", "lang", "rust"), _unit(26), support="user", now=2.0)
    assert r.action == "superseded"
    assert store.lookup("p", "lang").value == "rust"


def test_below_margin_same_tier_now_records_contender():
    store = CortexStore(supersede_confidence_margin=0.15)
    store.write_fact(Slot("box", "ip", "192.168.1.104"), _unit(27), confidence=0.9,
                     support="agent", now=1.0)
    res = store.write_fact(Slot("box", "ip", "10.0.0.5"), _unit(28), confidence=0.5,
                           support="agent", now=2.0)
    assert res.action == "contested"
    assert store.lookup("box", "ip").value == "192.168.1.104"
    assert len(store.contenders_for("box", "ip")) == 1


def test_at_most_one_active_contender_newer_value_supersedes_prior():
    store = CortexStore()
    store.write_fact(Slot("k", "v", "current"), _unit(29), support="user", now=1.0)
    store.write_fact(Slot("k", "v", "first"), _unit(30), support="agent", now=2.0)
    store.write_fact(Slot("k", "v", "second"), _unit(31), support="agent", now=3.0)
    conts = store.contenders_for("k", "v")
    assert len(conts) == 1 and conts[0].value == "second"
    # the prior contender is retained as superseded history, not current/contested
    hist = [r for r in store.records_for("k", "v") if r.value == "first"]
    assert hist and hist[0].status == "superseded"


def test_contender_confirm_reinforces_same_value():
    store = CortexStore()
    store.write_fact(Slot("k", "v", "cur"), _unit(32), support="user", now=1.0)
    store.write_fact(Slot("k", "v", "alt"), _unit(33), confidence=0.5, support="agent", now=2.0)
    c0 = store.contenders_for("k", "v")[0].confidence
    for i in range(10):
        store.write_fact(Slot("k", "v", "alt"), _unit(33), support="agent", now=3.0 + i)
    conts = store.contenders_for("k", "v")
    assert len(conts) == 1 and conts[0].confidence > c0   # reinforced, still one


def test_unknown_tier_contests_known_but_known_supersedes_legacy_unknown():
    store = CortexStore()
    # known user fact, unknown-tier write -> contends
    store.write_fact(Slot("a", "b", "x"), _unit(34), support="user", now=1.0)
    r1 = store.write_fact(Slot("a", "b", "y"), _unit(35), now=2.0)        # no support
    assert r1.action == "contested"
    # legacy unknown fact, known agent write -> supersedes (1 >= 0)
    store.write_fact(Slot("c", "d", "x"), _unit(36), now=1.0)            # no support
    r2 = store.write_fact(Slot("c", "d", "y"), _unit(37), support="agent", now=2.0)
    assert r2.action == "superseded"
    assert store.lookup("c", "d").value == "y"
```

- [ ] **Step 2: Run, verify they fail**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_cortex.py -k "contend or contest or tier or unknown_tier or below_margin or at_most_one" -q`
Expected: FAIL (`AttributeError: 'CortexStore' object has no attribute 'contenders_for'`, and action mismatches).

- [ ] **Step 3: Add tier rank + ctor flag** — in `cortex.py`, after `_norm_support` (≈line 72):

```python
# Provenance tier rank for the supersession guard. A write may only SUPERSEDE a
# slot whose current value is backed by an equal-or-weaker tier; a weaker-tier
# write is parked as a contender instead of silently overwriting. Unknown/"" = 0.
_TIER_RANK = {"user": 3, "action": 2, "agent": 1}


def _rank(origin: str | None) -> int:
    return _TIER_RANK.get((origin or "").strip().casefold(), 0)
```

In `CortexStore.__init__`, add the parameter + field:

```python
    def __init__(
        self,
        supersede_confidence_margin: float = 0.15,
        reinforce_rate: float = 0.34,
        protect_provenance: bool = True,
    ) -> None:
        self.supersede_confidence_margin = float(supersede_confidence_margin)
        self.reinforce_rate = float(reinforce_rate)
        self.protect_provenance = bool(protect_provenance)
        ...   # rest unchanged
```

- [ ] **Step 4: Replace the conflict branch in `write_fact`** — swap the final two statements (the `if self._should_supersede(...)` block + the trailing `contested` return) for:

```python
        # Genuine conflict at the same slot. Provenance guard: only a write whose
        # tier is >= the current value's tier may supersede; a weaker-tier write
        # (or one below the confidence margin) is parked as a contender instead of
        # silently overwriting. (Guard off -> tier ignored, pure newer-wins.)
        tier_ok = (not self.protect_provenance) or _rank(sup) >= _rank(cur.origin)
        if tier_ok and self._should_supersede(cur, confidence, t):
            cur.status = "superseded"
            cur.superseded_at = t
            cur.superseded_by_value = slot.value
            self._log(cur, slot.value, confidence, t, "supersede", "newer_wins")
            new = self._insert(slot, emb, confidence, prov, t, supersedes=cur.value, support=sup)
            return WriteResult("superseded", new)

        reason = "tier_downgrade" if not tier_ok else "below_confidence_margin"
        if not self.protect_provenance:
            # Legacy behavior: drop the conflicting value, keep current.
            self._log(cur, slot.value, confidence, t, "contested", reason)
            return WriteResult("contested", cur)
        return self._contend(cur, slot, emb, confidence, prov, t, sup, reason)
```

- [ ] **Step 5: Add `_active_contender`, `contenders_for`, `_contend`** — after `_should_supersede`/`_log`:

```python
    def _active_contender(self, key: tuple[str, str]) -> "CortexRecord | None":
        """The one active (status='contested') contender at a slot, or None."""
        for r in self.records:
            if r.key == key and r.status == "contested":
                return r
        return None

    def contenders_for(self, entity: str, attribute: str) -> list["CortexRecord"]:
        """Active contenders at a slot (0 or 1 under the at-most-one invariant)."""
        key = (_norm_key(entity), _norm_key(attribute))
        return [r for r in self.records if r.key == key and r.status == "contested"]

    def _contend(self, cur, slot, emb, confidence, prov, t, sup, reason):
        """Park a conflicting value as a contender at ``cur``'s slot rather than
        superseding. Keeps the current value canonical. At most one active
        contender per slot: a matching value confirms (reinforces) the existing
        contender; a different value supersedes the prior contender."""
        existing = self._active_contender(cur.key)
        if existing is not None and _norm_value(existing.value) == _norm_value(slot.value):
            existing.last_confirmed = t
            existing.provenance |= prov
            if sup:
                existing.support.add(sup)
            existing.confidence = min(
                1.0, max(self._reinforce(existing.confidence), float(confidence)),
            )
            self._log(cur, slot.value, confidence, t, "contested", "contender_confirmed")
            return WriteResult("contested", existing)
        supersedes_val = None
        if existing is not None:
            existing.status = "superseded"
            existing.superseded_at = t
            existing.superseded_by_value = slot.value
            supersedes_val = existing.value
        rec = CortexRecord(
            entity=slot.entity,
            attribute=slot.attribute,
            value=slot.value,
            polarity=getattr(slot, "polarity", "+"),
            confidence=float(confidence),
            status="contested",
            provenance=set(prov),
            asserted_at=t,
            last_confirmed=t,
            supersedes_value=supersedes_val,
            embedding=emb,
            support={sup} if sup else set(),
        )
        self.records.append(rec)   # deliberately NOT registered in self._current
        self._log(cur, slot.value, confidence, t, "contested", reason)
        return WriteResult("contested", rec)
```

- [ ] **Step 6: Run tests, verify pass**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_cortex.py -q`
Expected: PASS (new tests + all pre-existing, incl. `test_lower_confidence_candidate_does_not_supersede` unchanged).

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/memory/cortex.py tests/test_cortex.py docs/specs/2026-06-10-cortex-provenance-contenders-*.md
git commit -m "feat(cortex): provenance tier-rank guard + contender write path"
```

---

## Task 2: `resolve()` — promote / retire a contender

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py`
- Test: `tests/test_cortex.py`

- [ ] **Step 1: Write failing tests**

```python
def test_resolve_accept_promotes_contender_and_marks_user_confirmed():
    store = CortexStore()
    store.write_fact(Slot("box", "ip", "10.0.0.1"), _unit(40), support="user", now=1.0)
    store.write_fact(Slot("box", "ip", "10.0.0.2"), _unit(41), support="agent", now=2.0)
    res = store.resolve("box", "ip", accept=True, now=3.0)
    assert res is not None and res.action == "superseded"
    cur = store.lookup("box", "ip")
    assert cur.value == "10.0.0.2" and cur.status == "current"
    assert "user" in cur.support               # human confirmed -> user-tier
    assert store.contenders_for("box", "ip") == []
    old = [r for r in store.records_for("box", "ip") if r.value == "10.0.0.1"]
    assert old and old[0].status == "superseded"


def test_resolve_reject_retires_contender_current_unchanged():
    store = CortexStore()
    store.write_fact(Slot("box", "ip", "10.0.0.1"), _unit(42), support="user", now=1.0)
    store.write_fact(Slot("box", "ip", "10.0.0.2"), _unit(43), support="agent", now=2.0)
    res = store.resolve("box", "ip", accept=False, now=3.0)
    assert res is not None
    assert store.lookup("box", "ip").value == "10.0.0.1"
    assert store.contenders_for("box", "ip") == []
    retired = [r for r in store.records_for("box", "ip") if r.value == "10.0.0.2"]
    assert retired and retired[0].status == "retired"


def test_resolve_no_contender_returns_none():
    store = CortexStore()
    store.write_fact(Slot("box", "ip", "10.0.0.1"), _unit(44), support="user", now=1.0)
    assert store.resolve("box", "ip", accept=True) is None
```

- [ ] **Step 2: Run, verify fail**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_cortex.py -k resolve -q`
Expected: FAIL (`AttributeError: ... 'resolve'`).

- [ ] **Step 3: Implement `resolve`** — add after `_contend`:

```python
    def resolve(self, entity, attribute, accept: bool, now: float | None = None):
        """Resolve the active contender at a slot. ``accept=True`` promotes it to
        current (old current -> superseded; contender stamped user-confirmed);
        ``accept=False`` retires it (current untouched). Returns a ``WriteResult``
        or ``None`` when there is no active contender."""
        key = (_norm_key(entity), _norm_key(attribute))
        t = time.time() if now is None else float(now)
        c_idx = next(
            (i for i, r in enumerate(self.records)
             if r.key == key and r.status == "contested"),
            None,
        )
        if c_idx is None:
            return None
        contender = self.records[c_idx]
        cur_idx = self._current.get(key)
        cur = self.records[cur_idx] if cur_idx is not None else None
        if accept:
            if cur is not None:
                cur.status = "superseded"
                cur.superseded_at = t
                cur.superseded_by_value = contender.value
            contender.status = "current"
            contender.support.add("user")
            contender.last_confirmed = t
            contender.supersedes_value = cur.value if cur is not None else contender.supersedes_value
            self._current[key] = c_idx
            self._log(cur or contender, contender.value, contender.confidence, t,
                      "resolved", "accepted")
            return WriteResult("superseded", contender)
        contender.status = "retired"
        contender.superseded_at = t
        self._log(cur or contender, contender.value, contender.confidence, t,
                  "resolved", "rejected")
        return WriteResult("contested", cur or contender)
```

- [ ] **Step 4: Run, verify pass**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_cortex.py -k resolve -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/cortex.py tests/test_cortex.py
git commit -m "feat(cortex): resolve() to promote or retire a contender"
```

---

## Task 3: Persistence + load reconciliation for contenders

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py` (`_reindex_current`)
- Test: `tests/test_cortex.py`

- [ ] **Step 1: Write failing tests**

```python
def test_contested_and_retired_survive_persistence_roundtrip():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        store = CortexStore()
        store.write_fact(Slot("box", "ip", "10.0.0.1"), _unit(50), support="user", now=1.0)
        store.write_fact(Slot("box", "ip", "10.0.0.2"), _unit(51), support="agent", now=2.0)
        p = Path(d) / "cortex_state.pt"
        store.save(p)
        loaded = CortexStore()
        loaded.load(p)
        assert loaded.lookup("box", "ip").value == "10.0.0.1"
        conts = loaded.contenders_for("box", "ip")
        assert len(conts) == 1 and conts[0].value == "10.0.0.2"


def test_load_reconciles_duplicate_contested_to_one_active():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        store = CortexStore()
        store.write_fact(Slot("box", "ip", "10.0.0.1"), _unit(52), support="user", now=1.0)
        # hand-craft a second contested record at the same slot (legacy/dup)
        from pseudolife_memory.memory.cortex import CortexRecord
        store.records.append(CortexRecord(entity="box", attribute="ip", value="A",
                                          status="contested", last_confirmed=2.0))
        store.records.append(CortexRecord(entity="box", attribute="ip", value="B",
                                          status="contested", last_confirmed=3.0))
        p = Path(d) / "c.pt"
        store.save(p)
        loaded = CortexStore()
        loaded.load(p)
        conts = loaded.contenders_for("box", "ip")
        assert len(conts) == 1 and conts[0].value == "B"   # newest kept
```

- [ ] **Step 2: Run, verify fail**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_cortex.py -k "persistence_roundtrip or reconciles_duplicate_contested" -q`
Expected: the reconcile test FAILS (two active contenders).

- [ ] **Step 3: Generalise `_reindex_current`** — replace the method body so it reconciles `current` AND `contested` per slot:

```python
    def _reindex_current(self) -> None:
        """Rebuild the slot -> current index and self-heal the one-record-per-status
        invariants. If two records share a normalised slot at the same LIVE status
        (``current`` or ``contested``), keep the most-recently-confirmed and demote
        the rest to ``superseded`` (so legacy / pre-normalisation dups self-heal)."""
        self._current = {}
        seen_contested: dict[tuple[str, str], int] = {}

        def _demote(keep: int, drop: int) -> None:
            loser = self.records[drop]
            loser.status = "superseded"
            if loser.superseded_at is None:
                loser.superseded_at = self.records[keep].last_confirmed
            loser.superseded_by_value = self.records[keep].value

        for i, rec in enumerate(self.records):
            if rec.status == "current":
                prev = self._current.get(rec.key)
                if prev is None:
                    self._current[rec.key] = i
                else:
                    keep, drop = ((i, prev) if rec.last_confirmed >= self.records[prev].last_confirmed
                                 else (prev, i))
                    _demote(keep, drop)
                    self._current[rec.key] = keep
            elif rec.status == "contested":
                prev = seen_contested.get(rec.key)
                if prev is None:
                    seen_contested[rec.key] = i
                else:
                    keep, drop = ((i, prev) if rec.last_confirmed >= self.records[prev].last_confirmed
                                 else (prev, i))
                    _demote(keep, drop)
                    seen_contested[rec.key] = keep
```

- [ ] **Step 4: Run, verify pass**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_cortex.py -q`
Expected: PASS (whole file).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/cortex.py tests/test_cortex.py
git commit -m "feat(cortex): persist contenders + self-heal one-active-contender on load"
```

---

## Task 4: `protect_provenance` config flag + wiring

**Files:**
- Modify: `pseudolife_memory/utils/config.py`
- Modify: `pseudolife_memory/service.py` (`_ensure_init` CortexStore construction)
- Test: `tests/test_cortex.py`

- [ ] **Step 1: Write failing test**

```python
def test_protect_provenance_false_restores_pure_newer_wins():
    store = CortexStore(protect_provenance=False)
    store.write_fact(Slot("box", "ip", "10.0.0.1"), _unit(60), support="user", now=1.0)
    r = store.write_fact(Slot("box", "ip", "10.0.0.2"), _unit(61), support="agent", now=2.0)
    assert r.action == "superseded"            # tier ignored
    assert store.lookup("box", "ip").value == "10.0.0.2"
    assert store.contenders_for("box", "ip") == []
```

- [ ] **Step 2: Run, verify pass-or-fail**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_cortex.py -k protect_provenance_false -q`
Expected: PASS already (ctor flag landed in Task 1). If it fails, fix Task 1 wiring before continuing.

- [ ] **Step 3: Add config field** — in `config.py` `CortexConfig`, add after `search_first`:

```python
    protect_provenance: bool = True
```

- [ ] **Step 4: Thread into CortexStore** — in `service.py` `_ensure_init`, where `CortexStore(...)` is built:

```python
        self._cortex = CortexStore(
            supersede_confidence_margin=cc.supersede_confidence_margin,
            reinforce_rate=cc.reinforce_rate,
            protect_provenance=cc.protect_provenance,
        )
```

- [ ] **Step 5: Run config + cortex tests**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_cortex.py tests/test_config.py -q` (drop `test_config.py` from the command if it doesn't exist)
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/service.py tests/test_cortex.py
git commit -m "feat(cortex): protect_provenance config flag (default on)"
```

---

## Task 5: Service layer — contenders in write / lookup / search / resolve

**Files:**
- Modify: `pseudolife_memory/service.py`
- Test: `tests/test_cortex_contenders.py` (new)

- [ ] **Step 1: Write failing tests** — new file `tests/test_cortex_contenders.py`:

```python
"""Service-level contenders: provenance guard surfaces a conflicting agent value
as a contender against a user fact, and resolve() promotes/retires it."""
from __future__ import annotations

import tempfile

from pseudolife_memory.service import MemoryService


def test_store_agent_fact_parks_contender_against_user_fact():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        out = svc.cortex_write("project", "language", "rust", support="agent")
        assert out["action"] == "contested"
        assert out["current"]["value"] == "go"      # user fact still current
        assert out["value"] == "rust"               # the contender (flat record)
        conts = svc.cortex_contenders("project", "language")["contenders"]
        assert len(conts) == 1 and conts[0]["value"] == "rust"


def test_cortex_resolve_accept_then_lookup_returns_new_value_and_persists():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        svc.cortex_write("project", "language", "rust", support="agent")
        res = svc.cortex_resolve("project", "language", accept=True)
        assert res["resolved"] is True and res["accepted"] is True
        assert svc.cortex_lookup("project", "language")["value"] == "rust"
        # persisted: a fresh service reads the resolved value
        svc2 = MemoryService(data_dir=d)
        assert svc2.cortex_lookup("project", "language")["value"] == "rust"


def test_cortex_resolve_reject_keeps_current():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        svc.cortex_write("project", "language", "rust", support="agent")
        res = svc.cortex_resolve("project", "language", accept=False)
        assert res["resolved"] is True and res["accepted"] is False
        assert svc.cortex_lookup("project", "language")["value"] == "go"
        assert svc.cortex_contenders("project", "language")["contenders"] == []


def test_cortex_resolve_no_contender():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        res = svc.cortex_resolve("project", "language", accept=True)
        assert res["resolved"] is False and res["reason"] == "no_contender"


def test_cortex_search_flags_contested_entries():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        svc.cortex_write("project", "language", "rust", support="agent")
        entries = svc.cortex_search("project language", top_k=5)["entries"]
        assert entries and entries[0]["contested"] is True
        assert entries[0]["contender_value"] == "rust"
```

- [ ] **Step 2: Run, verify fail**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest tests/test_cortex_contenders.py -q`
Expected: FAIL (`cortex_contenders` / `cortex_resolve` missing; no `contested`/`current` keys).

- [ ] **Step 3: Update `cortex_write`** — add the `current` key on a contested write (after building the flat dict, before `return`):

```python
            out = {"action": res.action, **_cortex_record_to_dict(res.record)}
            if res.action == "contested":
                cur = self._cortex.lookup(entity, attribute)
                out["current"] = _cortex_record_to_dict(cur) if cur is not None else None
            return out
```

- [ ] **Step 4: Add `cortex_contenders` + `cortex_resolve`** — near the other `cortex_*` methods:

```python
    def cortex_contenders(self, entity: str, attribute: str) -> dict[str, Any]:
        """Active contenders parked at a slot (a conflicting lower-tier / below-
        margin value that did NOT supersede the current fact)."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            recs = self._cortex.contenders_for(entity, attribute)
            return {
                "entity": entity, "attribute": attribute,
                "contenders": [_cortex_record_to_dict(r) for r in recs],
            }

    def cortex_resolve(self, entity: str, attribute: str, accept: bool) -> dict[str, Any]:
        """Promote (accept) or retire (reject) the active contender at a slot.
        Persists. Returns ``{"resolved": False, "reason": "no_contender"}`` when
        there is nothing to resolve."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            res = self._cortex.resolve(entity, attribute, accept)
            if res is None:
                return {"resolved": False, "reason": "no_contender",
                        "entity": entity, "attribute": attribute}
            self._save_cortex()
            cur = self._cortex.lookup(entity, attribute)
            return {
                "resolved": True,
                "accepted": bool(accept),
                "action": res.action,
                "current": _cortex_record_to_dict(cur) if cur is not None else None,
                "record": _cortex_record_to_dict(res.record),
            }
```

- [ ] **Step 5: Flag contested entries in `cortex_search`** — replace the `entries` list-comp in `cortex_search` with:

```python
            entries = []
            for r, s in hits:
                d = {**_cortex_record_to_dict(r), "score": round(float(s), 4)}
                conts = self._cortex.contenders_for(r.entity, r.attribute)
                if conts:
                    d["contested"] = True
                    d["contender_value"] = conts[0].value
                    d["contender_origin"] = conts[0].origin
                else:
                    d["contested"] = False
                entries.append(d)
            return {"count": len(entries), "entries": entries}
```

- [ ] **Step 6: Run, verify pass**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest tests/test_cortex_contenders.py tests/test_cortex_service.py -q`
Expected: PASS (new file + existing service tests).

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/service.py tests/test_cortex_contenders.py
git commit -m "feat(cortex): service surface for contenders (write/lookup/search/resolve)"
```

---

## Task 6: MCP surface — fact_get contenders, search flag, resolve tool

**Files:**
- Modify: `pseudolife_memory/mcp_server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write failing tests** — append to `tests/test_mcp_server.py`; also add `"memory_fact_resolve"` to the expected list in `test_all_tools_registered`:

```python
def test_memory_fact_get_returns_contenders_via_mcp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    _invoke("memory_fact_set", {"entity": "project", "attribute": "language",
                                "value": "go", "origin": "user"})
    _invoke("memory_fact_set", {"entity": "project", "attribute": "language",
                                "value": "rust", "origin": "agent"})
    got = _invoke("memory_fact_get", {"entity": "project", "attribute": "language"})
    assert got["record"]["value"] == "go"                 # user fact current
    assert any(c["value"] == "rust" for c in got["contenders"])


def test_memory_fact_resolve_accept_and_reject_via_mcp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    _invoke("memory_fact_set", {"entity": "svc", "attribute": "port",
                                "value": "8080", "origin": "user"})
    _invoke("memory_fact_set", {"entity": "svc", "attribute": "port",
                                "value": "9090", "origin": "agent"})
    acc = _invoke("memory_fact_resolve", {"entity": "svc", "attribute": "port",
                                          "accept": True})
    assert acc["resolved"] is True
    assert _invoke("memory_fact_get", {"entity": "svc", "attribute": "port"})["record"]["value"] == "9090"
    # nothing left to resolve
    none = _invoke("memory_fact_resolve", {"entity": "svc", "attribute": "port",
                                           "accept": False})
    assert none["resolved"] is False
```

Note: `memory_fact_set` defaults `confidence=0.8`; both writes here are 0.8, so the agent write is diverted purely by the tier guard (agent < user) — a clean contender.

- [ ] **Step 2: Run, verify fail**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest tests/test_mcp_server.py -k "fact_get_returns_contenders or fact_resolve or all_tools_registered" -q`
Expected: FAIL (tool not registered; no `contenders` key).

- [ ] **Step 3: Update `memory_fact_get`** — return contenders too:

```python
    return {
        "record": service.cortex_lookup(entity, attribute),
        "contenders": service.cortex_contenders(entity, attribute)["contenders"],
    }
```

(Also extend the docstring's Returns line to mention `contenders`.)

- [ ] **Step 4: Forward `contested` in the `memory_search` cortex-first block** — replace the `result["cortex"] = [...]` list-comp with:

```python
            result["cortex"] = [
                {
                    "entity": f["entity"], "attribute": f["attribute"],
                    "value": f["value"], "origin": f.get("origin", ""),
                    "confidence": f["confidence"], "score": f.get("score"),
                    "contested": f.get("contested", False),
                    **(
                        {"contender_value": f.get("contender_value"),
                         "contender_origin": f.get("contender_origin", "")}
                        if f.get("contested") else {}
                    ),
                }
                for f in facts
            ]
```

- [ ] **Step 5: Add the `memory_fact_resolve` tool** — after `memory_fact_forget`:

```python
@mcp.tool()
def memory_fact_resolve(entity: str, attribute: str, accept: bool) -> dict[str, Any]:
    """Resolve a CONTESTED canonical fact after checking in with the human.

    When your write conflicts with a higher-tier fact (e.g. a user-stated value),
    the cortex KEEPS the current value and parks yours as a *contender* — you'll
    see ``action="contested"`` on the write, ``contested: true`` in ``memory_search``,
    and the contender under ``memory_fact_get``. That's your cue to ask the human,
    then call this to settle it:

    - ``accept=true``  -> adopt the contender as the new current value (recorded as
      user-confirmed; the old value is kept as superseded history).
    - ``accept=false`` -> discard the contender (retired); the current value stays.

    Args:
        entity: The slot entity (case/separator-insensitive).
        attribute: The slot attribute.
        accept: REQUIRED. ``true`` adopts the contender, ``false`` discards it.

    Returns:
        ``{"resolved": bool, "accepted": bool, "action": str, "current": {...},
        "record": {...}}``, or ``{"resolved": false, "reason": "no_contender"}``
        when nothing is parked at the slot.
    """
    return service.cortex_resolve(entity, attribute, accept)
```

- [ ] **Step 6: Nudge `memory_fact_set` docstring** — add one line to its body docstring:

```
    If your value conflicts with a higher-tier fact it is parked as a contender
    (not applied) — see ``memory_fact_resolve`` to settle it with the human.
```

- [ ] **Step 7: Run, verify pass**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest tests/test_mcp_server.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): contenders in fact_get/search + memory_fact_resolve tool"
```

---

## Task 7: Docs + full suite + finish

**Files:**
- Modify: `README.md`
- Test: whole suite

- [ ] **Step 1: README** — in the cortex/tools section, add `memory_fact_resolve` to the tool table and a short "Provenance contenders" subsection: a weaker-tier write against a stronger-tier fact is parked (not applied); see it via `memory_fact_get` / `contested:true` in search; settle with `memory_fact_resolve(accept=…)`. Note the `protect_provenance` flag. Bump the stated test count (185 → 185 + new tests; compute from the run below).

- [ ] **Step 2: Run the full suite (offline)**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m pytest -q`
Expected: PASS, 0 failures. Record the total for the README.

- [ ] **Step 3: Sanity-check the online path once** (optional, slower)

Run: `python -m pytest tests/test_cortex.py tests/test_cortex_contenders.py -q`
Expected: PASS (confirms no offline-only assumptions baked into the new tests).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: provenance contenders + memory_fact_resolve; update test count"
```

- [ ] **Step 5: Offer next step to the user** — push + PR/merge per repo norms (do NOT push unless the user asks). Summarise what landed and the new test total.

---

## Review reminders
- DRY: contender confirm reuses `_reinforce` + the same bounded-`max` rule as the current-record confirm; load reconciliation shares one `_demote` helper across current/contested.
- YAGNI: no new schema version; `status` already round-trips. No multi-contender support (at most one active per slot, by design).
- TDD: every task is RED → GREEN → commit.
- Backward compat: `test_lower_confidence_candidate_does_not_supersede` is expected to pass UNCHANGED; if a task breaks it, the conflict-branch edit is wrong.
