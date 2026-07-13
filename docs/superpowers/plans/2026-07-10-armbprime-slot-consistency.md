# Arm B′ Slot-Key Canonicalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the Sonnet distill labels' slot-key inconsistency (6.8% exact reuse vs Qwen's 24.1%), retrain the E4B extractor on the repaired set, and run the standard gated eval against the deployed baseline.

**Architecture:** A deterministic canonicalizer inside `distill_datagen_sonnet.py --ingest` rewrites near-miss attribute keys to the first-seen key per (question-chain, entity) *before* the vocab-chain recompute, so prompts' vocab hints match repaired labels structurally. Downstream (clean → train → GGUF → gated eval) reuses the arm-B pipeline verbatim with `sonnet2` naming.

**Tech Stack:** Python stdlib (canonicalizer), pytest, unsloth+TRL QLoRA on WSL 4090, llama.cpp GGUF, existing eval harnesses (`ladder_sweep.py`, `longmemeval_bench.py`).

**Spec:** `docs/superpowers/specs/2026-07-10-armbprime-slot-consistency-design.md`

## Global Constraints

- torch.compile must stay ON for `distill_train_e4b.py` — disabling it bypasses unsloth's fused CE and materializes the [5120, 262144] logits tensor (OOM). Manage VRAM only via `expandable_segments`, `--no-eval`, save cadence, and freeing desktop VRAM.
- Pre-flight before any full training run: `nvidia-smi` must show ≥3–4GB VRAM headroom after the expected ~19.5GB footprint (ask the user to close desktop GPU apps if not).
- Eval GPU tenancy: one model at a time. Only ever touch the `pseudolife-mcp-extractor-bench` container — never the live `pseudolife-mcp-extractor`/`daemon`/`postgres`.
- Gates (verbatim from spec): dataset exact-reuse ≥ 0.20; ladder `gold_recoverable >= 0.9` AND `stale_leak == 0.0` (hard stop); deploy iff `cortex > same-sitting baseline` AND `cortex > 0.564`.
- Eval tags: `sonnet2` / `sonnet2-baseline`. Artifacts: `~/e4b-sonnet2` (WSL), `evals/models/e4b-sonnet2-Q4_K_M.gguf`.
- The arm-B dataset files (`distill-extract-sonnet.jsonl`, `distill-extract-sonnet-clean.jsonl`) are never modified. All `evals/data/*.jsonl` are gitignored — no dataset commits.
- Never stage `verify_final.py` (pre-existing, unrelated).
- `distill_datagen_sonnet.py --ingest` WITHOUT `--canonical-keys` must remain byte-identical in behavior (arm-B reproducibility).

---

### Task 1: Canonicalizer in `distill_datagen_sonnet.py` (TDD)

**Files:**
- Modify: `evals/distill_datagen_sonnet.py`
- Test: `tests/test_distill_sonnet.py`

**Interfaces:**
- Produces: `canonicalize_claims(claims: list[dict], canon: dict) -> list[dict]` (pure, `canon` mutated across calls within one question); `_key_sig(attribute: str) -> frozenset[str]`; `ingest_question(qplan, answers, canonical: bool = False)`; CLI flags `--canonical-keys` (bool) and `--out PATH` (default: existing `MERGED`).
- Consumes: existing `_norm_key`, `validate_claims`, `_vocab_hint`, `ingest_question` row format.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_distill_sonnet.py`; note the file already inserts `evals/` on `sys.path` and imports from `distill_datagen_sonnet`)

```python
from distill_datagen_sonnet import canonicalize_claims, _key_sig  # noqa: E402


def _claim(entity, attribute, value, source=1):
    return {"entity": entity, "attribute": attribute, "value": value,
            "confidence": 0.9, "source": source}


def test_key_sig_merges_wording_variants():
    # reordering + suffix variants collapse to one signature
    assert _key_sig("pre-approved-loan-amount") == _key_sig("loan pre-approval amount")
    # generic filler tokens are ignored
    assert _key_sig("wake-time") == _key_sig("wake-up-time")


def test_key_sig_keeps_distinct_properties_apart():
    assert _key_sig("bedroom-color") != _key_sig("bedroom-size")


def test_canonicalize_rewrites_to_first_seen_key():
    canon = {}
    first = canonicalize_claims(
        [_claim("user", "pre-approved-loan-amount", "$400,000")], canon)
    later = canonicalize_claims(
        [_claim("user", "loan-pre-approval-amount", "$350,000")], canon)
    assert first[0]["attribute"] == "pre-approved-loan-amount"
    assert later[0]["attribute"] == "pre-approved-loan-amount"  # rewritten


def test_canonicalize_scopes_by_entity():
    canon = {}
    canonicalize_claims([_claim("alice", "team-size", "5")], canon)
    other = canonicalize_claims([_claim("bob", "team-size", "8")], canon)
    assert other[0]["attribute"] == "team-size"      # no cross-entity rewrite


def test_ingest_question_canonical_flag():
    qplan = {"question_id": "q_c", "sessions": [
        {"session_id": "sA", "date": "2023/03/02",
         "notes": ["[2023/03/02] user: pre-approved for $400k"]},
        {"session_id": "sB", "date": "2023/03/03",
         "notes": ["[2023/03/03] user: actually the pre-approval is $350k"]},
    ]}
    answers = {
        "sA": [_claim("user", "pre-approved-loan-amount", "$400,000")],
        "sB": [_claim("user", "loan-pre-approval-amount", "$350,000")],
    }
    rows = ingest_question(qplan, answers, canonical=True)
    target_b = json.loads(rows[1]["messages"][-1]["content"])["claims"][0]
    assert target_b["attribute"] == "pre-approved-loan-amount"
    # session B's vocab hint carries only the canonical key
    sys_b = rows[1]["messages"][0]["content"]
    assert "pre-approved-loan-amount" in sys_b
    assert "loan-pre-approval-amount" not in sys_b
    # flag OFF: variant key survives untouched (arm-B parity)
    rows_off = ingest_question(qplan, answers)
    target_off = json.loads(rows_off[1]["messages"][-1]["content"])["claims"][0]
    assert target_off["attribute"] == "loan-pre-approval-amount"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_distill_sonnet.py -v -k "key_sig or canonicalize or canonical_flag"`
Expected: FAIL/ERROR with `ImportError: cannot import name 'canonicalize_claims'`

- [ ] **Step 3: Implement** (in `evals/distill_datagen_sonnet.py`, after the `_notes` helper)

```python
# Slot-key canonicalization (arm B'): the Sonnet labels re-word the same
# property's key instead of reusing it (6.8% exact reuse vs qwen 24.1%),
# which suppresses supersession downstream. Merging is per (entity,
# signature) within one question chain; the first-seen attribute wins so
# the recomputed vocab hint and the labels stay coherent.
_GENERIC_TOKENS = {"a", "an", "and", "at", "for", "in", "of", "on", "or",
                   "the", "to", "up"}
_STEM_SUFFIXES = ("ation", "ment", "ness", "ing", "ed", "es", "al", "s")


def _stem(tok: str) -> str:
    for suf in _STEM_SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def _key_sig(attribute: str) -> frozenset[str]:
    toks = {_stem(t) for t in _norm_key(attribute).split("-") if t}
    return frozenset(toks - _GENERIC_TOKENS) or frozenset(toks)


def canonicalize_claims(claims: list[dict], canon: dict) -> list[dict]:
    out = []
    for c in claims:
        key = (_norm_key(str(c.get("entity", ""))),
               _key_sig(str(c.get("attribute", ""))))
        first = canon.setdefault(key, c["attribute"])
        if first != c["attribute"]:
            c = {**c, "attribute": first}
        out.append(c)
    return out
```

Wire into `ingest_question` — change the signature and add two lines after `validate_claims` (the `canon` dict is created next to `vocab` so it spans the question's sessions):

```python
def ingest_question(qplan: dict, answers: dict[str, list],
                    canonical: bool = False) -> list[dict] | None:
    ...
    rows = []
    vocab: set[str] = set()
    canon: dict = {}
    for s in qplan["sessions"]:
        ...
        claims = validate_claims(content, len(s["notes"]))
        if claims is None:
            return None                                # schema violation
        if canonical:
            claims = canonicalize_claims(claims, canon)
        ...
```

Wire the CLI: in `main()` add

```python
    ap.add_argument("--canonical-keys", action="store_true",
                    help="merge near-miss slot keys to the first-seen key "
                         "per question chain (arm B')")
    ap.add_argument("--out", type=Path, default=None,
                    help="ingest output file (default: distill-extract-sonnet.jsonl)")
```

and in `_cmd_ingest` replace the two `MERGED` uses with a local
`out_path = args.out or MERGED` (both the resume-read and the append-open),
and pass the flag: `rows = ingest_question(plans[qid], answers, canonical=args.canonical_keys)`.

- [ ] **Step 4: Run the new tests — expect PASS; then the full file**

Run: `.venv\Scripts\python.exe -m pytest tests/test_distill_sonnet.py -v`
Expected: all tests PASS (pre-existing ones prove `canonical=False` parity paths still work)

- [ ] **Step 5: Full test suite (no regressions)**

Run: `.venv\Scripts\python.exe -m pytest tests/ -q`
Expected: PASS (same count as before this task, plus the 5 new)

- [ ] **Step 6: Commit**

```bash
git add evals/distill_datagen_sonnet.py tests/test_distill_sonnet.py
git commit -m "feat(evals): --canonical-keys slot merging in sonnet ingest (arm B')"
```

---

### Task 2: Regenerate + verify the repaired dataset (inline, main session)

**Files:**
- Create: `evals/data/distill-extract-sonnet2.jsonl` (gitignored)

**Interfaces:**
- Consumes: Task 1's `--canonical-keys` / `--out` flags; `evals/data/sonnet_out/*.jsonl` (50 files, intact).
- Produces: the repaired raw dataset for Task 3.

- [ ] **Step 1: Ingest with canonicalization**

```powershell
$env:PYTHONPATH="."; .venv\Scripts\python.exe evals\distill_datagen_sonnet.py --ingest --canonical-keys --out evals\data\distill-extract-sonnet2.jsonl
```

Expected: `kept ~1727 rows (~340 empty); rejected questions ... : []` (same
question set as arm B; canonicalization changes attribute strings only, never
row counts).

- [ ] **Step 2: Reuse-metric gate** (same metric that scored qwen 0.241 / arm-B sonnet 0.068)

```powershell
.venv\Scripts\python.exe - << 'EOF'
import json, re
from pathlib import Path

def norm(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

rows = [json.loads(l) for l in Path("evals/data/distill-extract-sonnet2.jsonl")
        .read_text(encoding="utf-8").splitlines() if l.strip()]
chains = {}
for r in rows:
    chains.setdefault(r["id"].split(":")[0], []).append(r)
reuse = total = 0
for rs in chains.values():
    seen = set()
    for r in rs:
        for c in json.loads(next(m["content"] for m in r["messages"]
                                 if m["role"] == "assistant"))["claims"]:
            k = (norm(str(c["entity"])), norm(str(c["attribute"])))
            total += 1
            reuse += k in seen
            seen.add(k)
print(f"exact reuse {reuse}/{total} = {reuse/total:.3f}  (gate: >= 0.20)")
EOF
```

Expected: `exact reuse ... >= 0.20` (predict 0.25–0.30). **Below 0.20 = stop**;
inspect which near-misses the conservative matcher declined and report to the
user before touching the matcher.

- [ ] **Step 3: Audit 30 sampled merges** (rewrites are exactly the rows whose
attributes differ between the arm-B and B′ files)

```powershell
.venv\Scripts\python.exe - << 'EOF'
import json, random
from pathlib import Path

def claims(path):
    out = {}
    for l in Path(path).read_text(encoding="utf-8").splitlines():
        if l.strip():
            r = json.loads(l)
            out[r["id"]] = json.loads(next(m["content"] for m in r["messages"]
                                           if m["role"] == "assistant"))["claims"]
    return out

old, new = (claims(f"evals/data/distill-extract-{n}.jsonl")
            for n in ("sonnet", "sonnet2"))
merges = []
for rid in old.keys() & new.keys():
    for a, b in zip(old[rid], new[rid]):
        if a["attribute"] != b["attribute"]:
            merges.append((rid, a["entity"], a["attribute"],
                           b["attribute"], str(a["value"])[:60]))
print(f"{len(merges)} rewritten claims total")
random.seed(42)
for m in random.sample(merges, min(30, len(merges))):
    print(" | ".join(m))
EOF
```

Review each line: the rewrite is correct iff both keys describe the SAME
property of that entity (`loan-pre-approval-amount` → `pre-approved-loan-amount`:
good; anything conflating two different properties, e.g. a `-color` merged
into a `-size`: bad). >2 bad merges of 30 = stop and report to the user with
examples.

- [ ] **Step 4: Record numbers in the SDD ledger** (`.superpowers/sdd/progress.md`): reuse rate, merge count, audit result.

---

### Task 3: Clean pass

**Files:**
- Create: `evals/data/distill-extract-sonnet2-clean.jsonl` (gitignored)

**Interfaces:**
- Consumes: Task 2's raw file; `evals/distill_clean.py --src/--dst` (existing, unchanged).
- Produces: the training file consumed by Task 4.

- [ ] **Step 1: Run the cleaner**

```powershell
$env:PYTHONPATH="."; .venv\Scripts\python.exe evals\distill_clean.py --src evals\data\distill-extract-sonnet2.jsonl --dst evals\data\distill-extract-sonnet2-clean.jsonl
```

- [ ] **Step 2: Row-count sanity**

```powershell
.venv\Scripts\python.exe -c "print(sum(1 for l in open('evals/data/distill-extract-sonnet2-clean.jsonl', encoding='utf-8') if l.strip()))"
```

Expected: ≈1727 (within a few rows of the arm-B clean count; a large drop
means the canonicalizer corrupted rows — stop).

---

### Task 4: Train + merge + GGUF (inline, main session — WSL GPU)

**Files:**
- Create: `~/e4b-sonnet2/` (WSL: checkpoints + merged), `evals/models/e4b-sonnet2-Q4_K_M.gguf` (gitignored)

**Interfaces:**
- Consumes: Task 3's clean file; `evals/distill_train_e4b.py` flags (`--data`, `--out-dir`, `--save-steps`, `--no-eval`, `--logging-steps`, `--smoke`) — all committed, no code changes.
- Produces: the candidate GGUF for Task 5.

- [ ] **Step 1: Pre-flight VRAM headroom** — `nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader`; need desktop usage ≤ ~1.5GB (ask the user to close Discord/Spotify/Steam/Signal/browsers if higher).

- [ ] **Step 2: Smoke** (WSL, `~/e4b-train` venv, repo at `/mnt/c/Users/<user>/ClaudeCode/PseudoLife-MCP`)

```bash
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python evals/distill_train_e4b.py \
  --smoke --data evals/data/distill-extract-sonnet2-clean.jsonl --out-dir ~/e4b-sonnet2 \
  > ~/e4b-sonnet2-smoke.log 2>&1
```

Expected: `SMOKETEST OK` in the log, loss in the 0.1–0.4 band, GPU ~250W+/95% after compile. (A first-run compile hang after a fresh WSL boot: kill by explicit PID and retry once — known cold-boot artifact.)

- [ ] **Step 3: Full run + watchdog**

```bash
rm -rf ~/e4b-sonnet2 && PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  nohup python evals/distill_train_e4b.py --data evals/data/distill-extract-sonnet2-clean.jsonl \
  --out-dir ~/e4b-sonnet2 --save-steps 25 --no-eval --logging-steps 10 \
  > ~/e4b-sonnet2-run.log 2>&1 &
```

Watchdog: reuse `watch_overnight2.sh` pattern (frozen-step detection: step
counter unchanged across 3+ 60s polls while proc alive = STALL). Health check
at step ~25: expect ~19.5GB VRAM, ~350–420W, ~23s/it, ~2.5h ETA. NOTE: this
script logs train dicts with QUOTED values (`'loss': '0.13'`) — greps must
allow quotes.

Expected completion: `train_runtime` line (~8600s), merged
`~/e4b-sonnet2/merged/model.safetensors` ≈ 15,992,595,884 bytes.

- [ ] **Step 4: GGUF export** (WSL, CPU)

```bash
source ~/e4b-train/bin/activate && \
python ~/llama.cpp/convert_hf_to_gguf.py ~/e4b-sonnet2/merged --outfile ~/e4b-sonnet2-bf16.gguf && \
~/llama.cpp/build/bin/llama-quantize ~/e4b-sonnet2-bf16.gguf ~/e4b-sonnet2-Q4_K_M.gguf Q4_K_M && \
cp ~/e4b-sonnet2-Q4_K_M.gguf /mnt/c/Users/<user>/ClaudeCode/PseudoLife-MCP/evals/models/e4b-sonnet2-Q4_K_M.gguf
```

Expected: final GGUF = 5,335,290,144 bytes (same as every e4b-line Q4_K_M).

---

### Task 5: Gated eval + decision (inline, main session — GPU tenancy)

**Files:**
- Create: `evals/results/e4b-sonnet2-ladder.json`, `evals/results/longmemeval-ku-oracle-e4b-ft-sonnet2{,-baseline}.{jsonl,summary.json}`

**Interfaces:**
- Consumes: Task 4's GGUF; `ladder_sweep.py --rung e4b-ft` (flag, not positional); `longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag <tag> --phase extract|answer [--report]`.
- Produces: the deploy/no-deploy decision.

- [ ] **Step 1: Serve candidate + ladder gate**

```powershell
docker run -d --name pseudolife-mcp-extractor-bench --gpus all --no-healthcheck -p 127.0.0.1:8081:8081 -v "C:\Users\<user>\ClaudeCode\PseudoLife-MCP\evals\models\e4b-sonnet2-Q4_K_M.gguf:/models/extractor.gguf:ro" ghcr.io/ggml-org/llama.cpp:server-cuda --model /models/extractor.gguf --host 0.0.0.0 --port 8081 --ctx-size 8192 --jinja --parallel 1 -ngl 999
# wait for /health, then:
$env:PYTHONPATH="."; .venv\Scripts\python.exe evals\ladder_sweep.py --rung e4b-ft
Move-Item evals\results\e4b-ft.json evals\results\e4b-sonnet2-ladder.json -Force
```

Gate: `gold_recoverable >= 0.9` AND `stale_leak == 0.0`. stale_leak > 0 =
**hard stop** (implicates false merges) — remove the container, report.

- [ ] **Step 2: KU-oracle pair, tenancy phase split**

```powershell
$env:PYTHONPATH="."; .venv\Scripts\python.exe evals\longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag sonnet2 --phase extract
docker rm -f pseudolife-mcp-extractor-bench
# relaunch the same docker run with e4b-extractor-Q4_K_M.gguf mounted, wait /health
$env:PYTHONPATH="."; .venv\Scripts\python.exe evals\longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag sonnet2-baseline --phase extract
docker rm -f pseudolife-mcp-extractor-bench
Start-Process -FilePath "C:\Users\<user>\ClaudeCode\llama.ccp\run-server-turboq.bat"   # Qwen :1234 (NOT cmd /c)
# wait for :1234/health, then answer+judge both tags SAME SITTING:
$env:PYTHONPATH="."; .venv\Scripts\python.exe evals\longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag sonnet2 --phase answer
$env:PYTHONPATH="."; .venv\Scripts\python.exe evals\longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag sonnet2-baseline --phase answer
.venv\Scripts\python.exe evals\longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag sonnet2 --report
.venv\Scripts\python.exe evals\longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag sonnet2-baseline --report
```

Then stop Qwen (`Get-Process llama-server | Stop-Process -Confirm:$false`).

- [ ] **Step 3: Diagnostics beyond accuracy** (predictions from the spec)

```powershell
.venv\Scripts\python.exe - << 'EOF'
import json
from pathlib import Path
R = Path("evals/results")
for tag in ("sonnet2", "sonnet2-baseline", "sonnet"):
    rows = [json.loads(l) for l in
            (R / f"longmemeval-ku-oracle-e4b-ft-{tag}.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]
    n = len(rows)
    aic = sum(bool(r["answer_in_current_fact"]) for r in rows) / n
    sup = sum(r["consolidation"]["superseded"] for r in rows)
    cc = sum(bool(r.get("cortex_correct")) for r in rows) / n
    print(f"{tag:18s} cortex {cc:.3f}  answer_in_current_fact {aic:.3f}  "
          f"supersessions {sup}  bank_facts {sum(r['bank_facts'] for r in rows)/n:.1f}")
EOF
```

Predictions: sonnet2 supersessions ≈ 2× arm B's; `answer_in_current_fact`
≥ 0.53 (capture preserved — a DROP vs arm B implicates the merges → the
full-relabel fallback per spec).

- [ ] **Step 4: Decision + record (main session)**

- Deploy iff `cortex(sonnet2) > cortex(sonnet2-baseline)` AND `> 0.564`.
- Within noise (±0.03 per the rag-arm drift) → queue B″ (union-dedup set) per spec.
- Either way: `memory_outcome(task="slot-key canonicalization retrain (arm B')", outcome=..., detail=<all numbers incl. diagnostics>)` + SDD ledger entry.

---

### Task 6: Deploy the winner (ONLY if Task 5 clears every gate)

**Files:**
- Modify: `ops/docker-compose.yml` (the extractor GGUF mount line, currently `../evals/models/e4b-extractor-Q4_K_M.gguf:/models/extractor.gguf:ro`)

- [ ] **Step 1: Backup + rollback tag**

```powershell
pwsh ops/backup.ps1
docker tag pseudolife-extractor:gemma4-e4b pseudolife-extractor:pre-armbprime-$(Get-Date -Format yyyyMMdd)
```

- [ ] **Step 2: Swap the mount** — change the volume line to `../evals/models/e4b-sonnet2-Q4_K_M.gguf:/models/extractor.gguf:ro` (keep the container-side path verbatim).

- [ ] **Step 3: Restart + verify**

```powershell
docker compose -f ops/docker-compose.yml up -d pseudolife-extractor
docker exec pseudolife-mcp-extractor curl -sf http://localhost:8081/health
```

Then one ladder spot-check against a bench serve of the same GGUF and a live-smoke dream cycle.

- [ ] **Step 4: Commit + record**

```bash
git add ops/docker-compose.yml
git commit -m "feat(ops): deploy arm-B' extractor (e4b-sonnet2) as sidecar GGUF"
```

Plus `memory_store` (tags `["deploy", "milestone"]`) with the rollback tag and gate numbers.

### Task 2R: LLM-assisted key-merge maps (revision, 2026-07-10)

**Why:** Task 2's reuse gate failed at 0.069 (gate ≥ 0.20; arm B 0.068). The
T1 sig-equality matcher made only 3 true rewrites in 3,494 claims — the
dominant Sonnet key drift is granularity/hierarchy drift (`office-location`
vs `location`, `business` vs `business-products`), which no token-set rule
separates safely from genuinely distinct properties (`bedroom-color` vs
`bedroom-size`). User chose LLM-assisted merge maps over subset matcher
(0.111, under gate), loose matcher (0.381, unsafe), or full re-label.

**Approach:**
1. Controller builds per-chain inventories (entity → keys in first-seen
   order + 2 example values) from `evals/data/sonnet_out/*.jsonl` — 5 batch
   files of 10 questions (scratchpad `keymap_batches/batch-N.md`).
2. 5 parallel Sonnet subagents each return GROUPS of same-underlying-property
   keys per (question, entity). Criterion: merge iff the keys track the same
   evolving fact — a "what is the current X?" query should return only the
   newest value. Unsure = don't merge. Direction is NOT delegated.
3. Controller resolves groups deterministically: canonical = first-seen key
   of each group; builds `evals/data/sonnet2_keymap.json`:
   `{question_id: {norm_entity: {norm_variant_attr: canonical_attr_verbatim}}}`.
4. New `--key-map PATH` flag in `distill_datagen_sonnet.py --ingest`
   (implementer subagent, TDD): applied per claim after `validate_claims`,
   BEFORE `canonicalize_claims`/vocab recompute (same placement as T1).
   Lookup by `_norm_key(entity)` / `_norm_key(attribute)`. Without the flag,
   behavior stays byte-identical (arm-B reproducibility).
5. Gate check runs OFFLINE from map + sonnet_out before regenerating:
   exact reuse ≥ 0.20 unchanged. ~30-merge hand audit unchanged. Then
   regenerate `distill-extract-sonnet2.jsonl` with
   `--ingest --canonical-keys --key-map ... --out ...` and proceed to Task 3.

Gates, tags, and Tasks 3–6 are unchanged. Task 4 remains HELD for explicit
user go.
