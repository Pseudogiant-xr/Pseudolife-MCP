# Cortex for PseudoLife-MCP — implementation spec (core-only)

Status: proposed. Target repo: **PseudoLife-MCP** (`origin`), core-only, branched
off `origin/master` (`adc39a8`). Deliberately excludes the redacted gateway "dream".

## 1. Why (model-agnostic motivation)

The continuum (MIRAS bands, embedding/similarity) inherently spawns competing
near-duplicate copies, and a short "names the fact" entry can out-rank the actual
current value (the `41x41 → 40x40` / "didn't persist" class). That's a property of
the *substrate*, not the model, so every consumer of the MCP hits it.

The cortex is a sibling **slot-keyed canonical-fact** store: identity-not-
similarity, **supersession-not-decay**, **currency-not-frequency**. One current
value per `(entity, attribute)` slot, retrievable out of the context window.

It matters most for the MCP's *weakest* and *longest-context* consumers:
- **Proactive-curation gap** — Haiku/Sonnet don't reliably *decide* to write/update
  a canonical note. (Opus mostly does; the MCP serves everyone.)
- **Lost-in-the-middle** — a fact stated early in a long chat becomes unretrievable;
  the cortex externalizes it so retrieval no longer depends on transcript position.
- **Current-vs-stale reconciliation** — handed both an abandoned plan and the live
  decision, a weak model grabs whichever is *salient*, not *current*. The cortex's
  currency-not-frequency picks for it.

**Not portable / out of scope:** the LLM "dream" and conversation-sourced
multi-source tiering. MCP is request/response with no lifecycle hooks and no
automatic view of the conversation, so there is nothing for an async dream to ride
on. Population is instead **deterministic + tool-driven** (below).

## 2. What already exists in core (landed on the redacted branch `d65c540`)

These are in `pseudolife_memory/` (shared) and port to the MCP branch verbatim by
`git checkout d65c540 -- <path>`:

- `pseudolife_memory/memory/cortex.py` — `CortexStore` / `CortexRecord`: insert /
  confirm / supersede / contest, confidence margin + reinforce, **key
  normalization** (case + separator collapse) + load reconciliation,
  `support`/`origin` (user > action > agent) with corroboration promotion,
  `forget` / `dump` / `vocab`, torch save/load. **No redacted/MCP deps.**
- `pseudolife_memory/service.py` — `cortex_write[+support]` / `cortex_lookup` /
  `cortex_search` / `cortex_stats` / `cortex_vocab` / `cortex_dump` /
  `cortex_forget`, `extract_slots_regex`, **cortex co-persistence already folded
  into `save()` / `flush()` / `autosave_if_changed()` / `_entry_fingerprint()`**.
- `tests/test_cortex.py` (15) + `tests/test_cortex_service.py` (3) — core, run under
  the repo's existing pytest.

So persistence + the whole store + the service API are **done**. The MCP port is
only the three pieces below.

## 3. NEW piece #1 — deterministic slot-promotion on `store()` (the crux)

This is what makes the cortex help a model that won't curate. Run the existing
deterministic `slots.extract_slots()` **synchronously inside `service.store()`** and
promote any slot-shaped fact into the cortex. No LLM, no lifecycle hooks, no model
cooperation.

```python
# pseudolife_memory/service.py  — inside store(), after the CMS store succeeds
def store(self, text, source="claude", tags=None, origin=None):
    ...                                   # existing CMS store + surprise gate
    if stored and self.config.cortex.auto_promote:
        sup = origin or _origin_from_source(source)     # see §4
        for s in extract_slots(text):
            value = s.value if s.polarity != "-" else ("NOT " + s.value)
            claim = f"{s.entity} {s.attribute} {value}".strip()
            self._cortex.write_fact(
                Slot(s.entity, s.attribute, value),
                self._embedder.encode_single(claim),
                confidence=self.config.cortex.promote_confidence,   # 0.5 floor
                provenance=[source],
                support=sup,
            )
    return {...}                          # unchanged return shape
```

Notes:
- **Floor, not ceiling.** Regex only catches slot-shaped facts ("X is Y", "named X",
  "my X", loss/negation phrases). Rich/relational facts still need an explicit
  `fact_set` (§5) — same boundary as redacted.
- **Confidence 0.5** so an explicit `fact_set` (≥0.7) or a user-origin assertion
  cleanly out-ranks an auto-promoted guess via the existing supersede margin (0.15).
- Reuses one embedder call per slot (cheap; same path as `cortex_write`).
- Idempotent by construction: re-storing the same text re-confirms the slot
  (reinforce), never duplicates.

## 4. NEW piece #2 — `origin` (provenance-of-kind)

`CortexRecord.support`/`origin` already exist. In MCP, origin can't be inferred from
the conversation (the server never sees it), so it is **set by the caller or
defaulted from the `source` tag**:

```python
def _origin_from_source(source: str) -> str | None:
    return {
        "conversation": "user", "user": "user",
        "claude": "agent", "assistant": "agent", "agent": "agent",
        "tool": "action", "action": "action",
    }.get((source or "").lower())            # None when unknown -> origin ""
```

- `memory_store` and `memory_fact_set` gain an optional `origin: "user"|"action"|
  "agent"` param; when omitted it defaults from `source`.
- **Honest limitation:** in MCP most stores are model-initiated (`source="claude"` →
  `agent`). User-origin facts only land if the model passes `origin="user"` (capable
  models will; weak ones default to `agent`). The server cannot do better without a
  conversation feed, which MCP does not provide. Corroboration promotion still works:
  a later `origin="user"` write to the same slot promotes `origin` and lifts
  confidence.

## 5. NEW piece #3 — MCP tool surface (FastMCP `@mcp.tool()`)

The MCP exposes individual `memory_*` tools (not an action enum). Minimal additions:

Modify:
- **`memory_store`** — add optional `origin` param. Auto-promotion (§3) means weak
  models get cortex population **with zero new tool surface** — `store` still works,
  the cortex fills itself, search surfaces it.
- **`memory_search`** — cortex-aware: prepend current cortex facts (origin-annotated)
  above the associative hits, with the same dedup guard as the provider (drop an
  associative hit that merely restates a surfaced cortex value, len ≥ 5). Gated by
  `config.cortex.search_first` (default true). This is the read path; whether the
  host auto-injects search results each turn is out of MCP scope.

Add (thin wrappers over existing `service.cortex_*`):
- **`memory_fact_get(entity, attribute)`** → the one current value, or null.
- **`memory_fact_set(entity, attribute, value, origin=None, confidence=0.8)`** →
  explicit canonical write, for capable models that want to assert deliberately
  (beats auto-promotion via confidence). Wraps `cortex_write`.
- **`memory_fact_forget(entity, attribute=None)`** → purge a slot/entity (wraps
  `cortex_forget`); for cleanup, distinct from text-keyed `memory_delete`.
- **`memory_facts(limit=120)`** → dump current canonical facts (wraps `cortex_dump`),
  for introspection.

Tool descriptions must steer weak models: "Canonical facts are auto-captured from
your stores; use `memory_fact_set` only to assert a fact deliberately or correct
one." Keep `vocab` internal (it only fed the LLM dream's prompt; no dream here).

## 6. Persistence — already wired

`service.save()/flush()/autosave_if_changed()` co-persist `cortex_state.pt` beside
`cms_state.pt`, and `_entry_fingerprint()` folds the cortex so autosave wakes on
cortex mutations. `mcp_server.py` already runs the autosave loop + `flush` on exit
(durability patch `adc39a8`). **No new persistence work.** Confirm the stdio MCP
process is long-lived per session (it is — warm service, autosave thread).

## 7. Config (`config.cortex`, all defaulted)

| key | default | meaning |
|---|---|---|
| `enabled` | true | build/load the cortex at all |
| `auto_promote` | true | run §3 slot-promotion on every store |
| `promote_confidence` | 0.5 | confidence of auto-promoted facts |
| `search_first` | true | surface cortex facts in `memory_search` |
| `supersede_confidence_margin` | 0.15 | (already in CortexStore) |
| `reinforce_rate` | 0.34 | (already in CortexStore) |

## 8. Tests (TDD, pytest — repo has pytest + pytest-asyncio)

Port (run as-is): `tests/test_cortex.py` (15), `tests/test_cortex_service.py` (3).

New:
- `test_store_auto_promotes_slot_to_cortex` — `store("I have a Ragdoll cat named
  Jacque")` → `cortex_lookup("Jacque","type")=="cat"` etc., origin from source.
- `test_origin_defaults_from_source` — source `conversation`→user, `claude`→agent,
  unknown→"".
- `test_explicit_fact_set_outranks_auto_promotion` — auto-promoted 0.5 fact is
  superseded by a `fact_set` at 0.8; reverse order is contested.
- `test_auto_promote_disabled_no_cortex_writes` — `config.cortex.auto_promote=false`.
- `test_search_returns_cortex_first` (service-level) — canonical facts ahead of
  associative hits, dedup drops the restated value.
- `tests/test_mcp_server.py` additions — `memory_fact_get/set/forget` round-trip,
  `memory_store(origin=...)`.

## 9. Rollout

1. `git fetch origin && git checkout -b cortex-mcp origin/master`  (off `adc39a8`,
   core-only — no `redacted/` dir).
2. Bring the landed core files:
   `git checkout d65c540 -- pseudolife_memory/memory/cortex.py
   pseudolife_memory/service.py tests/test_cortex.py tests/test_cortex_service.py`.
3. Implement §3 (promotion + `_origin_from_source`), §4 (origin params), §5
   (`mcp_server.py` tools + cortex-aware search), §7 (config).
4. `pytest -q`. Then commit + push to `origin` (PseudoLife-MCP). PR/merge per repo
   norms.
- No data migration: a fresh `cortex_state.pt` starts empty and auto-promotion fills
  it. `store()` stays backward-compatible (new `origin` is optional).

## 10. Honest limitations (don't oversell)

1. Regex promotion catches only slot-shaped facts; rich facts need `fact_set` or a
   host-side LLM pass.
2. The cortex fixes **storage/currency, not generation** — it makes the right fact
   retrievable and unambiguous; it can't stop a weak model from mis-copying it once
   in context (drift happens *after* faithful recall).
3. **No auto-injection** — MCP can surface the cortex in `memory_search` but cannot
   force the host to recall each turn. Full benefit for weak models depends on the
   harness doing auto-recall (the redacted provider does; a bare MCP host may not).
4. origin fidelity depends on the caller (no conversation feed) — most MCP stores
   default to `agent`.

## 11. Effort

~1/3 of the redacted build. Store + service API + persistence + key-normalization +
origin machinery are **already in core**. New code is one promotion hook, an origin
defaulter, ~4 thin FastMCP tool wrappers + cortex-aware search, config, and ~6 tests.
No daemon, no provider, no buffer, no dream, no vocab-feed.
