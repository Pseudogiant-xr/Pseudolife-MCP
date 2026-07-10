# Known-Facts Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `memory.dream.known_facts_window > 0`, the dream extractor's prompt shows the current *values* of the top-N relevance-ranked cortex slots so updates supersede in place instead of minting paraphrase-variant keys.

**Architecture:** One new ranked read on `CortexStore` (`facts_ranked`, sibling of `vocab_ranked`), one prompt block in `dream.py` behind an optional `known_facts` kwarg, one service helper (`_dream_hints`) that builds vocab + window from a single batch embedding, and a config flag that defaults to 0 (off — byte-identical behavior everywhere until enabled). Bench/ladder grow `--window` flags; an echo-check script guards the designed-against failure mode.

**Tech Stack:** Python 3.11+, torch (already a dep), pytest, stdlib urllib/http.server for extractor stubs. No new dependencies.

**Spec:** `docs/specs/2026-07-10-known-facts-window-design.md` — read it first.

## Global Constraints

- `memory.dream.known_facts_window: int = 0` — default **off**. Working value when enabled: **20**.
- The `known_facts` kwarg is passed to an extractor **only when the window is enabled and non-empty**. Existing extractors (incl. test stubs like `_StubExtractor` in `tests/test_dream.py:357`) define `extract(self, texts, vocab)` and must keep working untouched.
- Window construction must **never raise** into a dream (mirror `_dream_vocab`'s fallback discipline).
- Fact values in the window are truncated to **120 chars**.
- Prompt block wording is fixed by the spec (see Task 2) — do not rephrase.
- Run tests from repo root: `.venv/Scripts/python.exe -m pytest <file> -v` with env `HF_HUB_OFFLINE=1`. PG-backed tests (those using the `svc` fixture) skip cleanly without a test Postgres.
- Commit style: `feat(dream): ...`, `feat(evals): ...`, matching repo history. Every commit ends with the Claude co-author trailer.

---

### Task 1: `CortexStore.facts_ranked`

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py` (insert directly after `vocab_ranked`, which ends at line 508)
- Test: `tests/test_cortex.py` (append)

**Interfaces:**
- Consumes: `CortexRecord` fields (`entity`, `attribute`, `value`, `status`, `slot_embedding`, `key` property) and the ranking idiom of `vocab_ranked` (cortex.py:479-508).
- Produces: `CortexStore.facts_ranked(query_embedding: torch.Tensor | None, limit: int = 20, value_chars: int = 120) -> list[tuple[str, str, str]]` — display-form `(entity, attribute, value)` triples, most-relevant first. Task 4 calls this.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cortex.py`; add `import torch` at the top if not already imported)

```python
# ── facts_ranked (known-facts window, spec 2026-07-10) ───────────────────

def _kf_rec(entity, attribute, value, emb=None, status="current"):
    from pseudolife_memory.memory.cortex import CortexRecord
    return CortexRecord(entity=entity, attribute=attribute, value=value,
                        status=status, slot_embedding=emb)


def test_facts_ranked_orders_by_slot_embedding_cosine():
    from pseudolife_memory.memory.cortex import CortexStore
    store = CortexStore()
    store.records = [
        _kf_rec("db", "host", "h1", torch.tensor([0.0, 1.0])),
        _kf_rec("svc", "port", "8080", torch.tensor([1.0, 0.0])),
    ]
    out = store.facts_ranked(torch.tensor([0.9, 0.1]), limit=2)
    assert out == [("svc", "port", "8080"), ("db", "host", "h1")]


def test_facts_ranked_excludes_non_current_and_caps_limit():
    from pseudolife_memory.memory.cortex import CortexStore
    store = CortexStore()
    store.records = [
        _kf_rec("svc", "port", "8080", torch.tensor([1.0, 0.0])),
        _kf_rec("svc", "port", "9090", torch.tensor([1.0, 0.0]),
                status="superseded"),
        _kf_rec("db", "host", "h1", torch.tensor([0.0, 1.0])),
    ]
    out = store.facts_ranked(torch.tensor([1.0, 0.0]), limit=1)
    assert out == [("svc", "port", "8080")]
    assert store.facts_ranked(torch.tensor([1.0, 0.0]), limit=0) == []


def test_facts_ranked_falls_back_alphabetical_without_embedding():
    from pseudolife_memory.memory.cortex import CortexStore
    store = CortexStore()
    store.records = [
        _kf_rec("zeta", "attr", "z"),
        _kf_rec("alpha", "attr", "a"),
    ]
    # No query embedding AND no slot embeddings -> alphabetical by slot key.
    assert store.facts_ranked(None, limit=2) == [
        ("alpha", "attr", "a"), ("zeta", "attr", "z")]


def test_facts_ranked_appends_embeddingless_records_after_ranked():
    from pseudolife_memory.memory.cortex import CortexStore
    store = CortexStore()
    store.records = [
        _kf_rec("plain", "attr", "no-emb"),                       # no slot_embedding
        _kf_rec("svc", "port", "8080", torch.tensor([1.0, 0.0])),
    ]
    out = store.facts_ranked(torch.tensor([1.0, 0.0]), limit=5)
    assert out == [("svc", "port", "8080"), ("plain", "attr", "no-emb")]


def test_facts_ranked_truncates_long_values():
    from pseudolife_memory.memory.cortex import CortexStore
    store = CortexStore()
    store.records = [_kf_rec("svc", "notes", "x" * 300,
                             torch.tensor([1.0, 0.0]))]
    (_, _, v), = store.facts_ranked(torch.tensor([1.0, 0.0]), limit=1)
    assert len(v) == 120 and v.endswith("…")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_cortex.py -k facts_ranked -v`
Expected: 5 FAILED with `AttributeError: 'CortexStore' object has no attribute 'facts_ranked'`

- [ ] **Step 3: Implement** (insert in `pseudolife_memory/memory/cortex.py` immediately after `vocab_ranked`, line 508)

```python
    def facts_ranked(self, query_embedding: torch.Tensor | None,
                     limit: int = 20,
                     value_chars: int = 120) -> list[tuple[str, str, str]]:
        """Current ``(entity, attribute, value)`` triples for the top-``limit``
        slots, ranked like :meth:`vocab_ranked` — the dream extractor's
        known-facts window (docs/specs/2026-07-10-known-facts-window-design.md).
        Values are truncated to ``value_chars`` to bound prompt size. Display
        forms (not normalised keys) so the prompt reads naturally. Records
        without a slot embedding follow alphabetically; no embedding at all
        falls back to alphabetical-by-key."""
        if limit <= 0:
            return []

        def _triple(r: CortexRecord) -> tuple[str, str, str]:
            v = r.value if len(r.value) <= value_chars else \
                r.value[:value_chars - 1] + "…"
            return (r.entity, r.attribute, v)

        cur = [r for r in self.records if r.status == "current"]
        with_emb = [r for r in cur if r.slot_embedding is not None]
        if query_embedding is None or not with_emb:
            ranked = sorted(cur, key=lambda r: "%s.%s" % r.key)
            return [_triple(r) for r in ranked[: int(limit)]]
        q = query_embedding.detach().to("cpu", torch.float32).reshape(-1)
        q = q / (q.norm() + 1e-12)
        mat = torch.stack([r.slot_embedding.reshape(-1) for r in with_emb])
        mat = mat / (mat.norm(dim=1, keepdim=True) + 1e-12)
        sims = (mat @ q).tolist()
        out = [_triple(with_emb[i])
               for i in sorted(range(len(with_emb)), key=lambda i: -sims[i])]
        tail = sorted((r for r in cur if r.slot_embedding is None),
                      key=lambda r: "%s.%s" % r.key)
        out.extend(_triple(r) for r in tail)
        return out[: int(limit)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_cortex.py -k facts_ranked -v`
Expected: 5 PASSED

- [ ] **Step 5: Run the whole cortex test file (no regressions)**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_cortex.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/cortex.py tests/test_cortex.py
git commit -m "feat(cortex): facts_ranked — ranked (entity, attribute, value) triples for the known-facts window"
```

---

### Task 2: Prompt block + `known_facts` kwarg in `dream.py`

**Files:**
- Modify: `pseudolife_memory/memory/dream.py` (`DreamExtractor` protocol :53-58, `RegexExtractor.extract` :65, `NoOpExtractor.extract` :85, `_vocab_hint` area :101-104, `OpenAICompatExtractor.extract` :187-259)
- Test: `tests/test_dream.py` (append; reuse `_StubHandler`, `_stub_server`, `_chat_payload` helpers at :24-55)

**Interfaces:**
- Consumes: nothing from other tasks (the kwarg is self-contained; Task 4 wires it).
- Produces: `extract(self, texts: list[str], vocab: list[str], known_facts: list[tuple[str, str, str]] | None = None)` on the protocol and all three extractors; module function `_facts_hint(known_facts) -> str`. **Calling `extract(texts, vocab)` with no third argument must produce a byte-identical HTTP request to today's.**

- [ ] **Step 1: Write the failing tests** (append to `tests/test_dream.py`)

```python
# ── known-facts window prompt block (spec 2026-07-10) ────────────────────

def test_facts_hint_formats_block_and_empty_is_empty():
    from pseudolife_memory.memory.dream import _facts_hint

    assert _facts_hint(None) == ""
    assert _facts_hint([]) == ""
    block = _facts_hint([("svc", "port", "8080"), ("db", "host", "h1")])
    assert "Current known facts" in block
    assert "never emit a claim the notes do not state" in block
    assert "- svc — port: 8080" in block
    assert "- db — host: h1" in block


def _capture_extract_body(known_facts):
    """Run one extract() against a capturing stub server; return the request
    body the extractor sent (messages etc.)."""
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    seen_bodies = []

    class _CapturingHandler(_StubHandler):
        @staticmethod
        def responder():
            return (200, _chat_payload([]))

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", 0))
            seen_bodies.append(json.loads(self.rfile.read(length).decode()))
            status, body = self.responder()
            data = body.encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base_url = f"http://127.0.0.1:{srv.server_address[1]}"
        ext = OpenAICompatExtractor(base_url, "m")
        if known_facts is None:
            ext.extract(["a note"], vocab=["svc.port"])
        else:
            ext.extract(["a note"], vocab=["svc.port"], known_facts=known_facts)
    finally:
        srv.shutdown()
    return seen_bodies[0]


def test_openai_extractor_renders_known_facts_block():
    body = _capture_extract_body([("svc", "port", "8080")])
    system = body["messages"][0]["content"]
    assert "Current known facts" in system
    assert "- svc — port: 8080" in system


def test_openai_extractor_omits_block_without_known_facts():
    system = _capture_extract_body(None)["messages"][0]["content"]
    assert "Current known facts" not in system
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py -k "facts_hint or known_facts" -v`
Expected: FAIL — `ImportError: cannot import name '_facts_hint'`, and the render test fails with `TypeError: extract() got an unexpected keyword argument 'known_facts'`

- [ ] **Step 3: Implement in `pseudolife_memory/memory/dream.py`**

3a. Add after `_vocab_hint` (line 104):

```python
_FACTS_HINT_HEAD = (
    "\n\nCurrent known facts (for key reuse — if a note updates one of "
    "these, emit the claim under the SAME entity and attribute with the new "
    "current value; never emit a claim the notes do not state):\n"
)


def _facts_hint(known_facts: list[tuple[str, str, str]] | None) -> str:
    if not known_facts:
        return ""
    return _FACTS_HINT_HEAD + "\n".join(
        f"- {e} — {a}: {v}" for e, a, v in known_facts)
```

3b. Update the protocol (replace `DreamExtractor.extract` signature, line 54):

```python
class DreamExtractor(Protocol):
    def extract(self, texts: list[str], vocab: list[str],
                known_facts: list[tuple[str, str, str]] | None = None,
                ) -> list[Claim]:
        """Return canonical claims for ``texts``. ``vocab`` is the existing
        ``entity.attribute`` slot keys, so an extractor can REUSE them instead of
        reinventing variants. ``known_facts`` (when the known-facts window is
        enabled) is ``(entity, attribute, current value)`` triples the batch
        plausibly updates — shown so updates land on the SAME slot. The caller
        only passes it when non-empty, so extractors without the parameter
        keep working on window-off deployments. Must never raise — return
        ``[]`` on any failure."""
        ...
```

3c. `RegexExtractor.extract` (line 65) and `NoOpExtractor.extract` (line 85): add the parameter, ignore it:

```python
    def extract(self, texts: list[str], vocab: list[str],
                known_facts: list[tuple[str, str, str]] | None = None,
                ) -> list[Claim]:
```

3d. `OpenAICompatExtractor.extract` (line 187): same signature change, and the system message (line 201-202) becomes:

```python
                    {"role": "system",
                     "content": _SYSTEM_PROMPT + _vocab_hint(vocab)
                                + _facts_hint(known_facts)},
```

(`_facts_hint(None)` is `""`, so the window-off request is byte-identical.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py -k "facts_hint or known_facts" -v`
Expected: 3 PASSED

- [ ] **Step 5: Run the whole dream test file (no regressions — the existing byte-identical tests are the real check)**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/dream.py tests/test_dream.py
git commit -m "feat(dream): known_facts kwarg + Current-known-facts prompt block (off by default)"
```

---

### Task 3: `DreamConfig.known_facts_window`

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (append field to `DreamConfig`, after `alias_candidate_min_cosine`, line 324)
- Test: `tests/test_dream.py::test_dream_config_defaults` (line 88 — extend)

**Interfaces:**
- Produces: `config.memory.dream.known_facts_window: int` (default `0`). Task 4 reads it.

- [ ] **Step 1: Extend the existing defaults test** (add one line inside `test_dream_config_defaults`)

```python
    assert c.known_facts_window == 0            # known-facts window off by default
```

- [ ] **Step 2: Run to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py::test_dream_config_defaults -v`
Expected: FAIL with `AttributeError: 'DreamConfig' object has no attribute 'known_facts_window'`

- [ ] **Step 3: Add the field** (end of `DreamConfig`, after line 324)

```python
    # TiMem-inspired known-facts window
    # (docs/specs/2026-07-10-known-facts-window-design.md): when > 0, the dream
    # prompt also shows the CURRENT VALUES of the top-N relevance-ranked slots
    # so updates supersede in place instead of minting paraphrase-variant keys.
    # 0 (default) = off — the extractor request is byte-identical to before.
    # Working value when enabled: 20.
    known_facts_window: int = 0
```

- [ ] **Step 4: Run to verify it passes**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py::test_dream_config_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/utils/config.py tests/test_dream.py
git commit -m "feat(config): memory.dream.known_facts_window flag (default 0 = off)"
```

---

### Task 4: Service wiring — `_dream_hints` + `dream_run`

**Files:**
- Modify: `pseudolife_memory/service.py` (`_dream_vocab` :2123-2140 → refactor; `dream_run` vocab line :2233 and isolation path :2284-2285)
- Test: `tests/test_dream.py` (append; uses the PG-backed `svc` fixture like `test_dream_run_promotes_and_advances_cursor` :332)

**Interfaces:**
- Consumes: `CortexStore.facts_ranked` (Task 1), `known_facts` kwarg (Task 2), `known_facts_window` config (Task 3).
- Produces: `MemoryService._dream_hints(texts: list[str], vocab_limit: int = 120, facts_limit: int = 0) -> tuple[list[str], list[tuple[str, str, str]]]` — one batch encode feeding both `vocab_ranked` and `facts_ranked`; never raises. `_dream_vocab` becomes a thin wrapper (kept — its docstring documents the ranking rationale and it preserves any external callers).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_dream.py`)

```python
class _RecordingExtractor:
    """Records what dream_run passes; returns one fixed claim per call."""

    def __init__(self):
        self.calls = []

    def extract(self, texts, vocab, known_facts=None):
        self.calls.append({"texts": list(texts), "vocab": list(vocab),
                           "known_facts": known_facts})
        return [{"entity": "gadget", "attribute": "version", "value": "3.3",
                 "confidence": 0.8, "origin": "agent"}]


def test_dream_run_window_off_by_default_passes_no_known_facts(svc):
    svc.store("the widget port is 9090", source="notes")
    ext = _RecordingExtractor()
    svc.dream_run(ext)
    assert ext.calls and ext.calls[0]["known_facts"] is None


def test_dream_run_passes_known_facts_window_when_enabled(svc):
    svc.config.memory.dream.known_facts_window = 20
    # Seed a current fact through the normal dream path (no LLM needed).
    svc.store("gadget version is 3.2", source="notes")
    svc.dream_run(_StubExtractor([{
        "entity": "gadget", "attribute": "version", "value": "3.2",
        "confidence": 0.8, "origin": "agent"}]))
    # Second cycle: the extractor must now SEE the seeded fact's value.
    svc.store("the gadget version is now 3.3", source="notes")
    ext = _RecordingExtractor()
    out = svc.dream_run(ext)
    kf = ext.calls[0]["known_facts"]
    assert kf, "window enabled + non-empty cortex must pass known_facts"
    assert ("gadget", "version", "3.2") in kf
    # And the claim written under the same slot supersedes as usual.
    assert out["superseded"] >= 1
    fact = svc.cortex_lookup("gadget", "version")
    assert fact is not None and "3.3" in fact["value"]


def test_dream_run_window_on_empty_cortex_omits_kwarg(svc):
    # First-ever dream on an empty bank: facts_ranked returns [] and the
    # kwarg must NOT be passed (extractors without it must keep working).
    svc.config.memory.dream.known_facts_window = 20
    svc.store("brand new note about a fresh topic", source="notes")
    out = svc.dream_run(_StubExtractor([{
        "entity": "fresh", "attribute": "topic", "value": "noted",
        "confidence": 0.8, "origin": "agent"}]))     # has no known_facts param
    assert out["inserted"] + out["confirmed"] >= 1   # did not blow up
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py -k dream_run_window or dream_run_passes -v`

(quote the `-k` expression: `-k "dream_run_window or dream_run_passes"`)
Expected: the window-on test FAILS (`known_facts` is `None` — nothing passes it yet); the off/empty tests may already pass (they assert today's behavior).

- [ ] **Step 3: Implement in `pseudolife_memory/service.py`**

3a. Replace `_dream_vocab` (lines 2123-2140) with the pair:

```python
    def _dream_hints(self, texts: list[str], vocab_limit: int = 120,
                     facts_limit: int = 0,
                     ) -> tuple[list[str], list[tuple[str, str, str]]]:
        """Relevance-ranked slot keys plus (when ``facts_limit > 0``) the
        known-facts window — current values of the top slots — from ONE
        batch-text embedding (docs/specs/2026-07-10-known-facts-window-design.md).
        Never raises — falls back to the alphabetical vocab and no window."""
        try:
            with self._lock:
                self._ensure_init()
                assert self._embedder is not None and self._cortex is not None
                emb = self._embedder.encode_single(" ".join(texts)[:4000])
                vocab = self._cortex.vocab_ranked(emb, vocab_limit)
                facts = (self._cortex.facts_ranked(emb, facts_limit)
                         if facts_limit > 0 else [])
                return vocab, facts
        except Exception as exc:  # noqa: BLE001 — hint quality must never break a dream
            logger.warning("dream hint build failed (%s); using alphabetical "
                           "vocab, no facts window", exc)
            return self.cortex_vocab(vocab_limit).get("slots", []), []

    def _dream_vocab(self, texts: list[str], limit: int = 120) -> list[str]:
        """Relevance-ranked slot keys for the dream vocab hint: embed the
        batch text and rank current slots by value-free slot-embedding cosine
        (see ``CortexStore.vocab_ranked``). The prompt hint shows ~60 keys;
        on a large bank the alphabetical head rarely contains the keys the
        batch actually updates, so the extractor mints paraphrase variants
        instead of superseding. Never raises — falls back to the alphabetical
        list on any failure. (Vocab half of :meth:`_dream_hints`.)"""
        return self._dream_hints(texts, vocab_limit=limit)[0]
```

3b. In `dream_run`, replace line 2233 (`vocab = self._dream_vocab(...)`):

```python
        kf_n = int(self.config.memory.dream.known_facts_window or 0)
        vocab, known_facts = self._dream_hints(
            [e["text"] for e in entries], facts_limit=kf_n)
```

3c. Replace the batch extract call (line 2257, `for c in extractor.extract(texts, vocab):`):

```python
            extracted = (extractor.extract(texts, vocab,
                                           known_facts=known_facts)
                         if known_facts else extractor.extract(texts, vocab))
            for c in extracted:
```

3d. Replace the isolation-path call (lines 2284-2285):

```python
                    e_vocab, e_kf = self._dream_hints([e["text"]],
                                                      facts_limit=kf_n)
                    e_claims = list(
                        extractor.extract([e["text"]], e_vocab,
                                          known_facts=e_kf)
                        if e_kf else extractor.extract([e["text"]], e_vocab))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py -k "dream_run_window or dream_run_passes" -v`
Expected: 3 PASSED

- [ ] **Step 5: Run dream + cortex-service suites (no regressions)**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py tests/test_cortex_service.py tests/test_connection_loss_recovery.py -v`
Expected: all PASS (every pre-existing stub extractor still works — the kwarg is only passed when the window yields facts)

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/service.py tests/test_dream.py
git commit -m "feat(dream): wire known-facts window through dream_run (_dream_hints, one shared encode)"
```

---

### Task 5: Bench + ladder `--window` flags, CHANGELOG

**Files:**
- Modify: `evals/longmemeval_bench.py` (argparse :421-434, `run_extract` :322-375, row dict :353)
- Modify: `evals/ladder_sweep.py` (`build_service` :294-305, `main` argparse :623-632)
- Modify: `CHANGELOG.md` (Unreleased → Added)

**Interfaces:**
- Consumes: `known_facts_window` config (Task 3).
- Produces: `--window N` on both tools. Ladder applies it via module global `WINDOW` inside `build_service` (three call sites, one wiring point); the bench sets it directly on its service after `build_service`. Bench rows gain a `"window": N` field.

- [ ] **Step 1: `ladder_sweep.py`** — add below the imports (module level):

```python
WINDOW = 0   # --window: known-facts window size applied to every bench service
```

In `build_service` (after line 305, `protect_provenance = False`):

```python
    svc.config.memory.dream.known_facts_window = WINDOW
```

In `main()` argparse (with the other `add_argument` calls, :624-631):

```python
    ap.add_argument("--window", type=int, default=0,
                    help="known-facts window size for every service built "
                         "by this run (0 = off; spec 2026-07-10)")
```

And directly after `args = ap.parse_args()` (line 632):

```python
    global WINDOW
    WINDOW = args.window
```

(`main` needs no other change; note `global` requires `main`'s body to declare it before first use.)

- [ ] **Step 2: `longmemeval_bench.py`** — thread a `window` parameter:

`run_extract` signature (line 322) becomes:

```python
def run_extract(dataset: str, limit: int | None, extractor_name: str,
                do_answer: bool, tag: str = "", window: int = 0) -> None:
```

After `svc.config.memory.dream.extract_relations = False` (line 345):

```python
        svc.config.memory.dream.known_facts_window = window
```

In the row dict (after `"extractor": extractor_name,` line 360):

```python
            "window": window,
```

In `main()` argparse:

```python
    ap.add_argument("--window", type=int, default=0,
                    help="known-facts window size for the dream pass "
                         "(0 = off; use 20 for the window arm — spec 2026-07-10)")
```

And the `run_extract` call (line 441) becomes:

```python
        run_extract(args.dataset, args.limit, args.extractor,
                    do_answer=(args.phase == "full"), tag=args.tag,
                    window=args.window)
```

- [ ] **Step 3: Smoke both CLIs (no live endpoints needed)**

Run: `.venv/Scripts/python.exe evals/ladder_sweep.py --list`
Expected: rung table prints, exit 0.
Run: `.venv/Scripts/python.exe evals/longmemeval_bench.py --help`
Expected: `--window` appears in help, exit 0.

- [ ] **Step 4: CHANGELOG** — under `## [Unreleased]` → `### Added` (create the subsection if absent):

```markdown
- Known-facts window for the dream pass (`memory.dream.known_facts_window`,
  default 0 = off): the extractor prompt shows current values of the top-N
  relevance-ranked slots so updates supersede in place instead of minting
  paraphrase keys. `--window` flags on `evals/longmemeval_bench.py` and
  `evals/ladder_sweep.py`; echo guard in `evals/window_echo_check.py`.
  (docs/specs/2026-07-10-known-facts-window-design.md)
```

- [ ] **Step 5: Commit**

```bash
git add evals/longmemeval_bench.py evals/ladder_sweep.py CHANGELOG.md
git commit -m "feat(evals): --window flag for known-facts-window arms (bench + ladder)"
```

---

### Task 6: Echo-check script

**Files:**
- Create: `evals/window_echo_check.py`

**Interfaces:**
- Consumes: `build_service`, `probe` from `evals/ladder_sweep.py`; `EXTRACTORS` from `evals/longmemeval_bench.py`; `OpenAICompatExtractor`; `known_facts_window` config.
- Produces: operator CLI, exit 0 = no echo, exit 1 = echo detected. Requires a live extractor endpoint (same GGUF-swap convention as the ladder).

- [ ] **Step 1: Write the script** (complete file)

```python
"""Echo check for the known-facts window (spec 2026-07-10).

Seeds a bench bank with distinctive facts, then dreams notes that say NOTHING
related to them, with the window ON. Any claim landing on a seeded slot or
containing a seeded value is an ECHO — the window leaked into extraction,
which is the stale-leak vector the spec designs against. Requires a live
extractor endpoint (swap the served GGUF, as with the ladder).

Usage (repo root):

  PYTHONPATH=. python evals/window_echo_check.py --extractor e4b-ft
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from ladder_sweep import build_service, probe            # noqa: E402
from longmemeval_bench import EXTRACTORS                  # noqa: E402

# Distinctive seeds: values that could not plausibly be extracted from the
# unrelated notes below. Any reappearance is an echo by construction.
SEEDS = [
    ("aquarium-heater", "wattage", "150W"),
    ("greenhouse-sensor", "battery type", "CR2477"),
    ("sourdough-starter", "feeding ratio", "1:5:5"),
    ("telescope-mount", "payload limit", "13.6 kg"),
    ("beehive-7", "queen marking color", "blue"),
    ("kiln", "cone rating", "cone 10"),
]
NOTES = [
    "user: I switched the team to trunk-based development this sprint.",
    "assistant: Noted — trunk-based development is now the team's workflow.",
    "user: Our CI provider is CircleCI and the pipeline takes 12 minutes.",
    "user: The release cadence is every second Thursday.",
    "assistant: Confirmed: releases go out every second Thursday.",
    "user: Code review SLA is 24 hours for all pull requests.",
]


class _SeedStub:
    """Writes the seed claims through the normal dream path (no LLM)."""

    def extract(self, texts, vocab, known_facts=None):
        return [{"entity": e, "attribute": a, "value": v,
                 "confidence": 0.9, "origin": "agent"} for e, a, v in SEEDS]


class _Recording:
    """Wraps the real extractor; keeps every claim it returned."""

    def __init__(self, inner):
        self.inner = inner
        self.claims = []

    def extract(self, texts, vocab, known_facts=None):
        out = (self.inner.extract(texts, vocab, known_facts=known_facts)
               if known_facts else self.inner.extract(texts, vocab))
        self.claims.extend(out)
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extractor", choices=list(EXTRACTORS), default="e4b-ft")
    ap.add_argument("--window", type=int, default=20)
    args = ap.parse_args()
    ex_url = EXTRACTORS[args.extractor]
    if not probe(ex_url):
        sys.exit(f"no extractor server at {ex_url} — start it first")
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    with tempfile.TemporaryDirectory(prefix="plecho_",
                                     ignore_cleanup_errors=True) as td:
        svc = build_service(Path(td))
        svc.config.memory.dream.extract_relations = False
        # Seed facts first (window irrelevant on an empty bank), then arm it.
        for e, a, v in SEEDS:
            svc.store(f"{e} {a} is {v}", source="bench")
        svc.dream_run(_SeedStub(), limit=100)
        svc.config.memory.dream.known_facts_window = args.window

        for note in NOTES:
            svc.store(note, source="bench")
        rec = _Recording(OpenAICompatExtractor(ex_url, "bench",
                                               max_tokens=4096,
                                               timeout_seconds=600.0))
        while True:
            res = svc.dream_run(rec, limit=100)
            if res.get("extractor_failed"):
                sys.exit("extractor endpoint failing — restart it and rerun")
            if not res.get("pulled"):
                break

    seeded_slots = {(e.lower(), a.lower()) for e, a, _ in SEEDS}
    seeded_values = {v.lower() for _, _, v in SEEDS}
    echoes = [c for c in rec.claims
              if (c["entity"].lower(), c["attribute"].lower()) in seeded_slots
              or any(v in c["value"].lower() for v in seeded_values)]
    print(f"extractor={args.extractor} window={args.window} "
          f"claims={len(rec.claims)} echoes={len(echoes)}")
    for c in echoes:
        print(f"  ECHO: {c['entity']} — {c['attribute']}: {c['value']}")
    if echoes:
        print("FAIL — window facts leaked into extraction")
        return 1
    print("PASS — no window echo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Static check (no live endpoint in CI)**

Run: `.venv/Scripts/python.exe -c "import ast; ast.parse(open('evals/window_echo_check.py').read())" && .venv/Scripts/python.exe evals/window_echo_check.py --help`
Expected: help text prints (imports succeed), exit 0. (A live run happens in Task 7.)

- [ ] **Step 3: Commit**

```bash
git add evals/window_echo_check.py
git commit -m "feat(evals): window echo check — guard against known-facts leaking into claims"
```

---

### Task 7: Full-suite verification + live validation runbook

No new code. This task verifies the branch and documents the operator runs that decide the gate. The live runs need the GPU box with the served GGUFs (`e4b-ft` and `qwen-27b` endpoints per `EXTRACTORS` in `evals/longmemeval_bench.py:70`), so they are operator steps, not CI.

- [ ] **Step 1: Full test suite**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest`
Expected: everything passes (724+ tests as of 2026-07-01; window default-off means zero behavioral drift).

- [ ] **Step 2: Live echo check (extractor endpoint required)**

```bash
PYTHONPATH=. python evals/window_echo_check.py --extractor e4b-ft
```
Expected: `PASS — no window echo`, exit 0. **FAIL here blocks the bench runs** — fix the prompt block before burning GPU time.

- [ ] **Step 3: Ladder with window on (standing rule: re-run after any dream-write-path change)**

```bash
PYTHONPATH=. python evals/ladder_sweep.py --rung e4b-ft --window 20
PYTHONPATH=. python evals/ladder_sweep.py --report
```
Expected gate: stale_leak **0.0**, gold_recoverable **≥ 0.9**.

- [ ] **Step 4: The 2×2 same-sitting KU-oracle bench** (four runs; swap served GGUFs between extractor pairs as usual)

```bash
# e4b-ft pair (the gated arm) — same sitting:
PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag w0
PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag w20 --window 20
# qwen-27b pair (the mechanism control) — same sitting:
PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle --extractor qwen-27b --tag w0
PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle --extractor qwen-27b --tag w20 --window 20
```

- [ ] **Step 5: Evaluate the gate** (from the four `.summary.json` files in `evals/results/`)

- **PASS** = e4b-ft w20 cortex ≥ e4b-ft w0 cortex + **0.05**, AND e4b-ft w20 hybrid ≥ w0 hybrid, AND Steps 2-3 clean.
- Secondary diagnostics (explain, don't gate): `answer_in_current_fact` up, `answer_in_history_only` down, supersessions up (per-row fields in the JSONLs).
- qwen-27b pair interpretation: 27B lifts but e4b-ft doesn't → mechanism validated, follow-up is window-formatted datagen + retrain; neither lifts → close the experiment, config stays 0.

- [ ] **Step 6: Record the outcome** — log `memory_outcome` (success or failure) with the four cortex/hybrid numbers, and archive the summaries per the Stage-1.5 convention (`chore(evals): archive ...`).

- [ ] **Step 7: On PASS only — deploy**

`ops/backup.ps1` → tag rollback image → set `known_facts_window: 20` in the live config → `docker compose -f ops/docker-compose.yml up -d --no-deps pseudolife-daemon` (never `down -v`) → `/health` → watch the next organic dream cycles.

---

## Self-review notes

- Spec coverage: mechanism (Tasks 1-4), config default-off (3), bench/ladder flags (5), echo guard (2 prompt wording + 6 + 7.2), ladder gate (7.3), 2×2 + gate (7.4-7.5), rollout (7.7), out-of-scope items untouched. Spec's "window state folded into the run tag" is realized as explicit `--tag w0/w20` + a `"window"` row field — recorded in both filename and data.
- Backward compat is load-bearing: the kwarg is only passed when non-empty (Task 4 3c/3d), covered by `test_dream_run_window_on_empty_cortex_omits_kwarg` and the untouched existing stubs.
- Type consistency: `known_facts: list[tuple[str, str, str]] | None` everywhere; `facts_ranked` returns display-form triples; `_dream_hints` returns `(vocab, facts)`.
