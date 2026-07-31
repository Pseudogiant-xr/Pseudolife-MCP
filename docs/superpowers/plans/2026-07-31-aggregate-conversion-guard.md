# Aggregate Conversion Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `add_member` on a slot whose current scalar is number-led parks the incoming member as a contender instead of converting the slot to a set, protecting stated-total scalars from destruction by spurious membership ops.

**Architecture:** One new module-level predicate (`_is_aggregate_value`) and one guarded branch at the conversion point in `CortexStore.add_member`, reusing the existing `_contend` contender machinery wholesale. Plus a committed, test-pinned eval prompt artifact for the later gate run. No schema change.

**Tech Stack:** Python, pytest, existing cortex/PG fixtures in `tests/test_cortex_sets.py`.

**Spec:** `docs/superpowers/specs/2026-07-31-aggregate-conversion-guard-design.md`

## Global Constraints

- No schema bump: contested records persist through existing columns; do not touch `SCHEMA_META_VERSION` or version-pin tests.
- TDD with a watched RED for every new behavior; run the named test and see it fail before implementing.
- Public repo: no PII, no machine identifiers, professional comments only.
- Full suite before the final commit: `HF_HUB_OFFLINE=1 python -m pytest tests/` with bench Postgres up at 127.0.0.1:5433 (PG tests skip silently without it — that is not a pass).
- Never overwrite canonical files in `evals/results/`; new run artifacts use `--tag c2op-guard`.
- Commit messages professional, no Culture voice.

---

### Task 1: `_is_aggregate_value` predicate

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py` (module level, near `_norm_value`, ~line 72)
- Test: `tests/test_cortex_sets.py`

**Interfaces:**
- Produces: `_is_aggregate_value(value: str) -> bool`, importable from `pseudolife_memory.memory.cortex`. Task 2 calls it inside `add_member`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cortex_sets.py` (extend the existing `from pseudolife_memory.memory.cortex import ...` block with `_is_aggregate_value`):

```python
def test_is_aggregate_value_detection():
    hits = ["32", "27 species", "$1,500", "+3", "-5", "3.5 kg", "3rd place",
            "  42  ", "€200", "£15"]
    misses = ["gravel bike", "Rosa's Diner", "prod-eu", "", "   ",
              "thirty-two", "iPhone 15", None]
    for v in hits:
        assert _is_aggregate_value(v), f"should match: {v!r}"
    for v in misses:
        assert not _is_aggregate_value(v), f"should not match: {v!r}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cortex_sets.py::test_is_aggregate_value_detection -v`
Expected: FAIL with ImportError (`_is_aggregate_value` not defined)

- [ ] **Step 3: Write minimal implementation**

In `pseudolife_memory/memory/cortex.py`, after `_norm_value`:

```python
# Number-led values ("32", "27 species", "$1,500") — the class the C2-op gate
# measured being destroyed by scalar->set conversion (evals/results/
# c2op-gate-verdict.json). Currency sign then optional sign then a digit.
_AGGREGATE_VALUE_RE = re.compile(r"^[$€£]?[+-]?\d")


def _is_aggregate_value(value: str) -> bool:
    """True when a scalar value reads as a number-led quantity."""
    return bool(_AGGREGATE_VALUE_RE.match((value or "").strip()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cortex_sets.py::test_is_aggregate_value_detection -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/cortex.py tests/test_cortex_sets.py
git commit -m "feat(cortex): number-led aggregate-value predicate"
```

---

### Task 2: guard in `add_member`, docs

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py` (`add_member`, conversion branch ~line 534)
- Modify: `CHANGELOG.md` (`[Unreleased]`, dated subsection style)
- Modify: `docs/guide/memory-model.md` (sets section, one paragraph)
- Modify: `docs/superpowers/specs/2026-07-31-aggregate-conversion-guard-design.md` (fix the one `CortexBank` reference to `CortexStore`)
- Test: `tests/test_cortex_sets.py`

**Interfaces:**
- Consumes: `_is_aggregate_value` (Task 1); existing `_contend`, `_norm_support`, `contenders_for`, `resolve`.
- Produces: guarded `add_member` returning `WriteResult("contested", <contender>)` with audit reason `"member_add_blocked_aggregate"`. Task 3 persists this record; callers (`memory_set_add`, dream apply loop) need no changes.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cortex_sets.py`:

```python
def test_member_add_on_aggregate_scalar_parks_contender(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    store.dirty_slots.clear()
    r = store.add_member(Slot("user", "birds", "Northern Flicker"),
                         emb("Northern Flicker"))
    assert r.action == "contested"
    assert r.record.status == "contested"
    assert r.record.value == "Northern Flicker"
    # Scalar survives, canonical; no set forms.
    assert store.slot_kind("user", "birds") == "scalar"
    assert store.members("user", "birds") == []
    cur = store.records[store._current[("user", "birds")]]
    assert cur.value == "27" and cur.status == "current"
    # Contender must persist: the guard schedules the slot rewrite itself
    # (_contend relies on its caller for dirty_slots, as write_fact does).
    assert ("user", "birds") in store.dirty_slots
    # Audit reason is the guard's own, not a tier reason.
    assert store.supersession_log[-1]["reason"] == "member_add_blocked_aggregate"


@pytest.mark.parametrize("total", ["27 species", "$1,500", "3.5 kg"])
def test_guard_covers_unit_and_currency_totals(store, emb, total):
    store.write_fact(Slot("user", "stat", total), emb(total))
    r = store.add_member(Slot("user", "stat", "Blue Jay"), emb("Blue Jay"))
    assert r.action == "contested"
    assert store.slot_kind("user", "stat") == "scalar"


def test_second_blocked_add_supersedes_prior_contender(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    store.add_member(Slot("user", "birds", "Northern Flicker"),
                     emb("Northern Flicker"))
    r = store.add_member(Slot("user", "birds", "Blue Jay"), emb("Blue Jay"))
    assert r.action == "contested"
    assert [c.value for c in store.contenders_for("user", "birds")] == ["Blue Jay"]
    # The displaced contender stays in the audit trail as superseded.
    gone = [x for x in store.records
            if x.value == "Northern Flicker" and x.status == "superseded"]
    assert len(gone) == 1


def test_repeated_blocked_add_confirms_contender(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    first = store.add_member(Slot("user", "birds", "Northern Flicker"),
                             emb("Northern Flicker")).record
    before = first.confidence
    r = store.add_member(Slot("user", "birds", "Northern Flicker"),
                         emb("Northern Flicker"))
    assert r.action == "contested" and r.record is first
    assert r.record.confidence >= before
    assert len(store.contenders_for("user", "birds")) == 1


def test_resolve_accept_promotes_blocked_member_to_scalar(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    store.add_member(Slot("user", "birds", "Northern Flicker"),
                     emb("Northern Flicker"))
    r = store.resolve("user", "birds", accept=True)
    assert r.action == "superseded"
    assert store.slot_kind("user", "birds") == "scalar"
    cur = store.records[store._current[("user", "birds")]]
    assert cur.value == "Northern Flicker"


def test_resolve_reject_keeps_aggregate_scalar(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    store.add_member(Slot("user", "birds", "Northern Flicker"),
                     emb("Northern Flicker"))
    r = store.resolve("user", "birds", accept=False)
    assert r.action == "contested"
    cur = store.records[store._current[("user", "birds")]]
    assert cur.value == "27" and cur.status == "current"
    assert store.contenders_for("user", "birds") == []


def test_empty_member_value_on_aggregate_slot_still_invalid(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    r = store.add_member(Slot("user", "birds", "   "), emb("blank"))
    assert r.action == "member_invalid"
    assert store.contenders_for("user", "birds") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cortex_sets.py -k "aggregate or blocked or resolve_accept_promotes or resolve_reject_keeps" -v`
Expected: the new behavior tests FAIL (guarded slots currently convert: `r.action == "member_added"`, `slot_kind == "set"`). Two matched tests are allowed to pass at RED: `test_is_aggregate_value_detection` (Task 1, already green) and `test_empty_member_value_...` (rejection precedes conversion today — it pins ordering against regression, note that in a comment).

- [ ] **Step 3: Implement the guard**

In `add_member`, replace the conversion branch head (currently `if idx is not None: self.dirty_slots.add(key); cur = self.records[idx]; ...`):

```python
        # Scalar at this slot -> one-way conversion (spec rule 1), UNLESS the
        # scalar is a number-led aggregate ("total species: 32"): converting
        # destroys a stated total that no enumeration of members recovers
        # (measured: evals/results/c2op-gate-verdict.json). Park the incoming
        # member as a contender instead — auditable, and resolve(accept=True)
        # remains the explicit human path to overwrite the total.
        idx = self._current.get(key)
        if idx is not None:
            cur = self.records[idx]
            if _is_aggregate_value(cur.value):
                self.dirty_slots.add(key)
                return self._contend(cur, slot, emb, confidence,
                                     {p for p in provenance if p}, t,
                                     _norm_support(support),
                                     "member_add_blocked_aggregate",
                                     cur.slot_embedding,
                                     writer_id=writer_id,
                                     session_id=session_id)
            self.dirty_slots.add(key)
            cur.status = "superseded"
            # ... rest of the existing conversion path unchanged ...
```

Update the `add_member` docstring: after the sentence describing one-way conversion, add — "Exception: when the current scalar is a number-led aggregate value (`_is_aggregate_value`), the slot is NOT converted; the incoming member is parked as a contender (reason `member_add_blocked_aggregate`) and the total stays canonical. `resolve(accept=True)` remains the explicit path to overwrite it."

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cortex_sets.py -v`
Expected: all PASS, including the pre-existing conversion tests (`test_member_add_converts_scalar_slot_with_audit` uses the non-numeric scalar "road bike" and must stay green).

- [ ] **Step 5: Docs**

- `CHANGELOG.md` under `[Unreleased]` in the existing dated-subsection style: `add_member` on a slot whose current scalar is number-led ("32", "27 species", "$1,500") now parks the member as a contender (audit reason `member_add_blocked_aggregate`) instead of converting the slot — stated totals survive spurious membership ops; `resolve(accept=True)` remains the explicit overwrite path. Cite the gate verdict artifact.
- `docs/guide/memory-model.md`: one paragraph in the set-valued-slots section stating the guard, the detection rule (number-led value), the contender disposition, and the enumerating-content limitation from the spec.
- Spec file: change the single `CortexBank.add_member` reference to `CortexStore.add_member`.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/cortex.py tests/test_cortex_sets.py CHANGELOG.md docs/guide/memory-model.md docs/superpowers/specs/2026-07-31-aggregate-conversion-guard-design.md
git commit -m "feat(cortex): aggregate conversion guard — number-led scalars park member-adds as contenders"
```

---

### Task 3: PG round-trip for the blocked-aggregate contender

**Files:**
- Test: `tests/test_cortex_sets.py` (PG-backed section; reuse the persistence fixture idiom at ~line 414, gated on the bench Postgres like its neighbors)

**Interfaces:**
- Consumes: guarded `add_member` (Task 2); existing PG save/load fixtures (`pg_conn`, the hydrate idiom used by the ~line 414 fixture).
- Produces: nothing new — a pin only.

- [ ] **Step 1: Write the failing test**

Uses the existing `store_with_pg` fixture (~line 414: bare `CortexStore` + `reload_store()` closure that write-throughs dirty slots to Postgres and hydrates a fresh store). PG-path embeddings in this file use `dim=1024`.

```python
def test_blocked_aggregate_contender_survives_pg_roundtrip(store_with_pg, emb):
    """Regression pin: the guard's contender must survive persistence with
    status and kind intact — a hydration that dropped either would resurrect
    the destructive conversion on the next daemon restart."""
    store, reload_store = store_with_pg
    store.write_fact(Slot("user", "birds", "27"), emb("27", dim=1024))
    r = store.add_member(Slot("user", "birds", "Northern Flicker"),
                         emb("Northern Flicker", dim=1024))
    assert r.action == "contested"
    fresh = reload_store()
    assert fresh.slot_kind("user", "birds") == "scalar"
    assert fresh.members("user", "birds") == []
    cur = fresh.records[fresh._current[("user", "birds")]]
    assert cur.value == "27" and cur.status == "current"
    conts = fresh.contenders_for("user", "birds")
    assert [c.value for c in conts] == ["Northern Flicker"]
    assert conts[0].kind == "scalar"
```

- [ ] **Step 2: Watch the RED**

This is a regression pin over behavior Task 2 already implements, so the watched RED is an assertion-bites check: run once with one expected value deliberately wrong (e.g. expect `slot_kind == "set"`), see exactly that assertion fail, restore the correct value. Confirm the failure is the assertion, not a fixture skip.

- [ ] **Step 3: Run to green**

Run: `python -m pytest tests/test_cortex_sets.py -k pg -v` (bench Postgres up at 127.0.0.1:5433)
Expected: PASS, not SKIP — a skip means the DB is down, which does not count.

- [ ] **Step 4: Commit**

```bash
git add tests/test_cortex_sets.py
git commit -m "test(cortex): pin PG round-trip of blocked-aggregate contender"
```

---

### Task 4: committed op prompt artifact

**Files:**
- Create: `evals/prompts/ku_op_prompt_v0.txt`
- Test: `tests/test_op_prompt_artifact.py` (new, small)

**Interfaces:**
- Consumes: `evals/op_probe.py`'s `VARIANTS["v0-appended-block"]` (the programmatic reconstruction of the definitive-gate prompt).
- Produces: a committed prompt file the gate run passes via `--system-prompt-file`; the pin test keeps file and construction byte-identical forever.

- [ ] **Step 1: Write the failing test**

```python
"""The committed op-prompt artifact must stay byte-identical to the
programmatic construction the definitive C2-op gate ran (op_probe's
v0-appended-block: shipped _SYSTEM_PROMPT with the op block inserted
before the Return-empty line). Drift here would silently change what
`--system-prompt-file evals/prompts/ku_op_prompt_v0.txt` measures."""
from pathlib import Path


def test_op_prompt_file_matches_probe_construction():
    from evals.op_probe import VARIANTS
    path = Path(__file__).resolve().parents[1] / "evals" / "prompts" / "ku_op_prompt_v0.txt"
    assert path.read_text(encoding="utf-8") == VARIANTS["v0-appended-block"]
```

(If `evals` is not importable as a package from tests, use the same `sys.path` insertion idiom the other eval-adjacent tests in `tests/` use — check `tests/test_eval_replicate.py`'s import style and mirror it.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_op_prompt_artifact.py -v`
Expected: FAIL with FileNotFoundError (artifact not yet written)

- [ ] **Step 3: Generate the artifact**

```bash
PYTHONPATH=. python -c "from evals.op_probe import VARIANTS; from pathlib import Path; Path('evals/prompts/ku_op_prompt_v0.txt').write_text(VARIANTS['v0-appended-block'], encoding='utf-8')"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_op_prompt_artifact.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add evals/prompts/ku_op_prompt_v0.txt tests/test_op_prompt_artifact.py
git commit -m "evals: commit the op prompt artifact, pinned to the probe construction"
```

---

### Task 5: full suite, gate campaign, verdict, PR (controller-executed)

This task is run by the session controller, not an implementer subagent — it needs the GPU, the bench Postgres, and maintainer-gated judgment calls.

- [ ] **Step 1: Full suite**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/` (bench Postgres up at 127.0.0.1:5433)
Expected: all pass, PG tests ran (not skipped).

- [ ] **Step 2: Pre-register the gate**

`memory_store` the exact pass/fail criteria from the spec's Measurement section (readouts, decision rules, artifact names) BEFORE starting the server.

- [ ] **Step 3: Run the campaign**

Reproducible server via `Start-Qwen` (dot-source `evals/qwen_server.ps1`), verify the listener's command line carries `cache-type-k q8_0`, then:

```
python evals/longmemeval_bench.py --dataset oracle --extractor qwen-27b --tag c2op-guard --system-prompt-file evals/prompts/ku_op_prompt_v0.txt
```

Affirmative launch verification within 2 minutes (first JSONL row written); 15-minute heartbeats to the SDD ledger for any unattended stretch. No ladder/echo re-runs (prompt and extractor byte-identical to the already-passed pair).

- [ ] **Step 4: Analyze against pre-registration**

Paired permutation (10k draws, seed 0) vs the ceiling-e2e control and vs c2op-e2e; count-class split; member_facts/banks_with_sets. Write `evals/results/c2op-guard-verdict.json` (never overwrite prior verdicts). Add evidence rows in `tests/test_eval_evidence.py` for any number that lands in docs/CHANGELOG, same change.

- [ ] **Step 5: PR**

Push branch, `gh pr create` against master with the verdict-backed story; the maintainer merges. If the outcome hits the "significantly above control" rule, the PR presents the block-shipping proposal as a separate decision, not a fait accompli.
