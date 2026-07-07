# Extractor Stage 1.5 (JEPA ablation + Sonnet-5 recall labels) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push the deployed e4b-ft extractor past KU-oracle cortex 0.564 via two gated experiments: (A) an LLM-JEPA auxiliary loss on the existing 1,756-row dataset, then (B) Sonnet-5 recall-tuned label regeneration via Max-plan subagents.

**Architecture:** Arm A extends `evals/distill_train_e4b.py` with a flag-gated JEPA loss (`--jepa-lambda 0` = byte-identical current behavior), preserving the fixed-shape/compile-once/fused-CE discipline. Arm B adds `evals/distill_datagen_sonnet.py` (emit-briefs → subagent dispatch → strict ingest) with an asymmetric prompt split: Sonnet labels under a private recall-boosted prompt, stored rows keep the unchanged production prompt.

**Tech Stack:** unsloth + TRL QLoRA on WSL (4090), llama.cpp GGUF export, existing eval harnesses (`ladder_sweep.py`, `longmemeval_bench.py`), pytest for the pure-Python datagen logic.

**Spec:** `docs/superpowers/specs/2026-07-07-extractor-stage15-jepa-sonnet-design.md`

## Global Constraints

- Training runs in the WSL uv venv (`source ~/e4b-train/bin/activate`), NOT the repo `.venv`. Repo tests run in the repo venv with `PYTHONPATH=.`.
- Fixed shapes only: SFT rows 5120 (`MAX_SEQ`), JEPA notes view 4096, claims view 1024. No dynamic shapes — variable shapes recompile every step (29s → 1205s/it).
- View forwards produce hidden states only — never logits (the 262k-vocab logit tensor overflowed 24GB).
- Rows longer than a view are truncated **only for the auxiliary embedding**; the CE target keeps the drop-don't-truncate rule at `MAX_SEQ`.
- Gates for any deploy candidate: KU-oracle cortex > 0.564 (same-session baseline rerun), ladder gold ≥ 0.9, stale_leak = 0.0.
- LongMemEval-KU stays held out: reuse `distill_datagen.py`'s forbidden-set logic verbatim.
- Data outputs under `evals/data/` are gitignored; scripts and prompts are committed.
- Spill signature on the 4090: "100% util at ~120W + glacial steps" = WSL sysmem spill, not progress. Kill and diagnose.
- Commit after every green task; commit messages follow the repo's `feat(scope):` / `test(scope):` style.

---

## File structure

| file | action | responsibility |
|---|---|---|
| `evals/distill_train_e4b.py` | modify | JEPA flags, view tokenization, `JEPATrainer`, `--out-dir` |
| `evals/prompts/sonnet_recall_system.md` | create | private recall-boosted teacher prompt (arm B) |
| `evals/distill_datagen_sonnet.py` | create | `--emit-briefs` / `--ingest` / `--compare` |
| `evals/distill_clean.py` | modify | `--src` / `--dst` args (defaults unchanged) |
| `tests/test_distill_sonnet.py` | create | unit tests for the pure datagen-sonnet logic |
| `ops/docker-compose.yml` | modify (deploy only) | swap the extractor GGUF mount to the winner |

---

### Task 1: JEPA loss in `distill_train_e4b.py` (flag-gated)

**Files:**
- Modify: `evals/distill_train_e4b.py`

**Interfaces:**
- Consumes: existing `tokenize_fixed(tok, rows)`, `MAX_SEQ`, `HOLDOUT`, `OUT`.
- Produces: CLI flags `--jepa-lambda FLOAT` (default 0.0), `--jepa-pred-tokens INT` (default 2), `--jepa-stopgrad` (flag), `--out-dir PATH` (default `~/e4b-extractor`); dataset columns `notes_ids/notes_mask/notes_pred_pos/claims_ids/claims_mask/claims_last_pos` (only when λ>0); class `JEPATrainer(SFTTrainer)`.

There is no pytest cycle for this task (unsloth/CUDA only exists in the WSL venv); Task 2's parity gate + smoke are its test. Steps here are code-only.

- [ ] **Step 1: Add imports and view constants**

After the existing imports (below `from trl import SFTConfig, SFTTrainer`):

```python
import torch
import torch.nn.functional as F
```

Below `MAX_SEQ = 5120`:

```python
# JEPA view shapes (aux loss only — CE rows keep MAX_SEQ + drop-don't-truncate).
# Fixed so the run compiles exactly three graphs: 5120 SFT + 4096 + 1024.
NOTES_VIEW_SEQ = 4096
CLAIMS_VIEW_SEQ = 1024
```

- [ ] **Step 2: Extend `tokenize_fixed` to optionally emit view features**

Replace the whole `tokenize_fixed` function with (one pass, so row acceptance
can never diverge between the CE features and the view features; `jepa_k=0`
produces byte-identical output to today's function):

```python
def tokenize_fixed(tok, rows: list[dict], jepa_k: int = 0) -> Dataset:
    feats = {"input_ids": [], "labels": [], "attention_mask": []}
    if jepa_k:
        # Predictor tokens = Gemma's reserved <unusedN> vocab. Their embedding
        # rows stay frozen under QLoRA; the LoRA-adapted layers learn to emit
        # the prediction at those positions (deviation from the paper's newly
        # learned tokens — embedding surgery on a 4-bit base is not worth it).
        pred_ids = [tok.convert_tokens_to_ids(f"<unused{i}>")
                    for i in range(jepa_k)]
        assert all(isinstance(i, int) and i >= 0 for i in pred_ids), pred_ids
        feats |= {"notes_ids": [], "notes_mask": [], "notes_pred_pos": [],
                  "claims_ids": [], "claims_mask": [], "claims_last_pos": []}
    pad = tok.pad_token_id
    for r in rows:
        msgs = r["messages"]
        prompt = tok.apply_chat_template(
            msgs[:-1], tokenize=False, add_generation_prompt=True)
        completion = msgs[-1]["content"] + tok.eos_token + "\n"
        # E4B ships a multimodal Processor: text must be passed by keyword
        # (a positional arg lands in its `images` slot), and it returns a
        # BATCHED [[ids]] even for a single string.
        p_ids = tok(text=prompt, add_special_tokens=False).input_ids
        c_ids = tok(text=completion, add_special_tokens=False).input_ids
        if p_ids and isinstance(p_ids[0], list):
            p_ids, c_ids = p_ids[0], c_ids[0]
        if len(p_ids) + len(c_ids) > MAX_SEQ:
            continue                      # drop, never truncate the completion
        ids = p_ids + c_ids
        labels = [-100] * len(p_ids) + c_ids
        mask = [1] * len(ids)
        n_pad = MAX_SEQ - len(ids)
        feats["input_ids"].append(ids + [pad] * n_pad)
        feats["labels"].append(labels + [-100] * n_pad)
        feats["attention_mask"].append(mask + [0] * n_pad)
        if jepa_k:
            # view1 = raw numbered-notes user text, view2 = claims JSON.
            n_ids = tok(text=msgs[1]["content"],
                        add_special_tokens=False).input_ids
            v_ids = tok(text=msgs[2]["content"],
                        add_special_tokens=False).input_ids
            if n_ids and isinstance(n_ids[0], list):
                n_ids, v_ids = n_ids[0], v_ids[0]
            n_ids = n_ids[:NOTES_VIEW_SEQ - jepa_k] + pred_ids
            v_ids = v_ids[:CLAIMS_VIEW_SEQ]
            feats["notes_ids"].append(
                n_ids + [pad] * (NOTES_VIEW_SEQ - len(n_ids)))
            feats["notes_mask"].append(
                [1] * len(n_ids) + [0] * (NOTES_VIEW_SEQ - len(n_ids)))
            feats["notes_pred_pos"].append(len(n_ids) - 1)
            feats["claims_ids"].append(
                v_ids + [pad] * (CLAIMS_VIEW_SEQ - len(v_ids)))
            feats["claims_mask"].append(
                [1] * len(v_ids) + [0] * (CLAIMS_VIEW_SEQ - len(v_ids)))
            feats["claims_last_pos"].append(len(v_ids) - 1)
    return Dataset.from_dict(feats)
```

- [ ] **Step 3: Add `JEPATrainer`**

Insert above `main()`:

```python
_VIEW_KEYS = ("notes_ids", "notes_mask", "notes_pred_pos",
              "claims_ids", "claims_mask", "claims_last_pos")


class JEPATrainer(SFTTrainer):
    """SFTTrainer + LLM-JEPA auxiliary loss (arXiv 2509.14252).

    L = L_SFT + lambda * (1 - cos(pred(E(notes)), E(claims))), last-token
    hidden states as view embeddings, predictor = k reserved tokens appended
    to the notes view. The SFT term goes through unsloth's fused CE
    untouched; view forwards return hidden states only (no logits tensor).
    JEPA is TRAIN-only: eval_loss stays CE-only, comparable to baseline.
    """

    def __init__(self, *args, jepa_lambda: float = 0.0,
                 jepa_stopgrad: bool = False, **kw):
        super().__init__(*args, **kw)
        self.jepa_lambda = jepa_lambda
        self.jepa_stopgrad = jepa_stopgrad

    def compute_loss(self, model, inputs, return_outputs=False,
                     num_items_in_batch=None):
        view = {k: inputs.pop(k) for k in _VIEW_KEYS if k in inputs}
        out = super().compute_loss(model, inputs, return_outputs=return_outputs,
                                   num_items_in_batch=num_items_in_batch)
        if not (self.jepa_lambda and view and model.training):
            return out
        loss = out[0] if return_outputs else out
        h_n = model(input_ids=view["notes_ids"],
                    attention_mask=view["notes_mask"],
                    output_hidden_states=True).hidden_states[-1]
        pred = h_n[torch.arange(h_n.size(0)), view["notes_pred_pos"]]
        h_c = model(input_ids=view["claims_ids"],
                    attention_mask=view["claims_mask"],
                    output_hidden_states=True).hidden_states[-1]
        tgt = h_c[torch.arange(h_c.size(0)), view["claims_last_pos"]]
        if self.jepa_stopgrad:
            tgt = tgt.detach()
        jepa = 1.0 - F.cosine_similarity(pred.float(), tgt.float(),
                                         dim=-1).mean()
        loss = loss + self.jepa_lambda * jepa
        return (loss, out[1]) if return_outputs else loss
```

- [ ] **Step 4: Wire flags into `main()`**

Replace the argparse block and the affected lines in `main()`:

```python
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true",
                    help="5 steps on 40 rows — shape/memory/throughput check")
    ap.add_argument("--jepa-lambda", type=float, default=0.0,
                    help="JEPA aux-loss weight; 0 = exact baseline path")
    ap.add_argument("--jepa-pred-tokens", type=int, default=2)
    ap.add_argument("--jepa-stopgrad", action="store_true",
                    help="detach the claims-view target embedding")
    ap.add_argument("--out-dir", type=Path, default=OUT,
                    help="run dir (checkpoints + merged); default unchanged")
    args = ap.parse_args()
    jepa_k = args.jepa_pred_tokens if args.jepa_lambda > 0 else 0
```

Then: `ds = tokenize_fixed(tokenizer, rows, jepa_k=jepa_k)`; every `OUT`
reference in `main()` becomes `args.out_dir`; the trainer construction
becomes `JEPATrainer(..., jepa_lambda=args.jepa_lambda,
jepa_stopgrad=args.jepa_stopgrad, ...)` with one extra `SFTConfig` kwarg —
add exactly:

```python
            remove_unused_columns=(False if jepa_k else True),
```

(`True` is the transformers default, so the λ=0 config is unchanged; with
λ>0 the DataLoader must not drop the view columns. `default_data_collator`
tensorizes the extra integer columns as-is.)

- [ ] **Step 5: Update the module docstring**

Append to the header docstring:

```
Stage-1.5 (docs/superpowers/specs/2026-07-07): --jepa-lambda adds the
LLM-JEPA auxiliary loss (arXiv 2509.14252) — last-token view embeddings,
<unusedN> predictor tokens, cosine loss, fixed view shapes 4096/1024.
--jepa-lambda 0 (default) is the exact Stage-1 path; parity-gate it against
an unmodified checkout before any ablation run.
```

- [ ] **Step 6: Commit**

```bash
git add evals/distill_train_e4b.py
git commit -m "feat(evals): flag-gated LLM-JEPA auxiliary loss in E4B distill trainer"
```

---

### Task 2: Parity gate + JEPA smoke (WSL, GPU)

**Files:** none (verification only). Run from the repo root in WSL.

- [ ] **Step 1: Baseline smoke on the unmodified script**

```bash
git stash   # only if Task 1 is uncommitted; otherwise:
git show HEAD~1:evals/distill_train_e4b.py > /tmp/train_baseline.py
source ~/e4b-train/bin/activate
cd /mnt/c/Users/HAMO9/ClaudeCode/PseudoLife-MCP
python /tmp/train_baseline.py --smoke 2>&1 | tee /tmp/smoke_baseline.log
```

Expected: `SMOKETEST OK`, 5 logged step losses.

- [ ] **Step 2: Parity smoke on the modified script**

```bash
python evals/distill_train_e4b.py --smoke 2>&1 | tee /tmp/smoke_parity.log
grep -o "'loss': [0-9.]*" /tmp/smoke_baseline.log /tmp/smoke_parity.log
```

Expected: the five per-step loss values match between the two logs (same
seed, same data, same config). **If they differ, stop — the λ=0 path is not
the baseline; fix before proceeding.**

- [ ] **Step 3: JEPA smoke**

```bash
python evals/distill_train_e4b.py --smoke --jepa-lambda 1.0 \
    --out-dir ~/e4b-jepa 2>&1 | tee /tmp/smoke_jepa.log
```

Expected: `SMOKETEST OK`; step time roughly 2–3× the ~24.5s baseline
(fourth/fifth step, after compile warmup); loss strictly greater than the
parity-smoke loss at step 1 (CE + positive aux term); no recompile spam
after the first three graph compilations; `nvidia-smi` wattage well above
the ~120W spill signature during steps.

- [ ] **Step 4: Record the smoke numbers**

Append the three step-time/loss summaries as a comment block at the bottom of
the run log notes in the PR/commit message for Task 3 (no repo file change).

---

### Task 3: Arm A full run → GGUF → gated eval → decision

**Files:**
- Create (gitignored): `~/e4b-jepa/*` (WSL), `evals/models/e4b-jepa-Q4_K_M.gguf`
- Create: `evals/results/lme-ku-oracle-e4b-ft-jepa*.json` (whatever `--tag` emits)

**Interfaces:**
- Consumes: Task 1 flags; `evals/distill_merge_e4b.py` pattern for OOM-safe merge; `EXTRACTORS["e4b-ft"] = "http://127.0.0.1:8081/v1"` + `--tag` namespacing in `longmemeval_bench.py`.
- Produces: the arm-A decision (JEPA adopted or not) for Task 10.

- [ ] **Step 1: Full training run (overnight, ~7h)**

```bash
source ~/e4b-train/bin/activate
cd /mnt/c/Users/HAMO9/ClaudeCode/PseudoLife-MCP
nohup python evals/distill_train_e4b.py --jepa-lambda 1.0 \
    --out-dir ~/e4b-jepa > ~/e4b-jepa-run.log 2>&1 &
```

Expected: 410 steps at ~55–65s/step; checkpoints every 100 steps under
`~/e4b-jepa/checkpoints`. Monitor the first 10 minutes for the spill
signature, then leave it.

- [ ] **Step 2: Merge (OOM lesson applies)**

The in-process `save_pretrained_merged` was OOM-killed once before (needs
~40GB WSL RAM for the bf16 base staging — `.wslconfig` was already raised).
If the run's own merge dies, adapt `evals/distill_merge_e4b.py`: change
`OUT = Path.home() / "e4b-jepa"` and `CKPT = OUT / "checkpoints" /
"checkpoint-410"`, rerun. Expected: `merged model -> ~/e4b-jepa/merged`.

- [ ] **Step 3: GGUF convert + quantize (same recipe as e4b-ft)**

```bash
python ~/llama.cpp/convert_hf_to_gguf.py ~/e4b-jepa/merged \
    --outfile ~/e4b-jepa/e4b-jepa-bf16.gguf
~/llama.cpp/build/bin/llama-quantize ~/e4b-jepa/e4b-jepa-bf16.gguf \
    ~/e4b-jepa/e4b-jepa-Q4_K_M.gguf Q4_K_M
cp ~/e4b-jepa/e4b-jepa-Q4_K_M.gguf \
    /mnt/c/Users/HAMO9/ClaudeCode/PseudoLife-MCP/evals/models/
```

(If the llama.cpp checkout lives elsewhere, use wherever
`evals/models/e4b-extractor-Q4_K_M.gguf` was produced on 2026-07-06.)

- [ ] **Step 4: Serve the candidate and run the ladder**

Serve `e4b-jepa-Q4_K_M.gguf` on :8081 exactly as the current sidecar bench
setup does (llama-server, same flags as the e4b-ft bench runs). Then, repo
venv on Windows:

```powershell
rg '"e4b-ft"' evals/ladder_sweep.py   # confirm the rung name (expected: e4b-ft)
$env:PYTHONPATH="."; python evals/ladder_sweep.py e4b-ft
Move-Item evals/results/e4b-ft.json evals/results/e4b-jepa-ladder.json -Force
```

Gate: `gold_recoverable >= 0.9` and `stale_leak == 0.0`. Fail → stop arm A
here, record the negative result (Step 7), proceed to Task 4.

- [ ] **Step 5: KU-oracle, candidate then same-session baseline**

With the candidate still served on :8081:

```powershell
$env:PYTHONPATH="."; python evals/longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag jepa
```

Then swap the served GGUF back to `evals/models/e4b-extractor-Q4_K_M.gguf`
(deployed e4b-ft) and rerun in the same sitting:

```powershell
$env:PYTHONPATH="."; python evals/longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag jepa-baseline
python evals/longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag jepa --report
python evals/longmemeval_bench.py --dataset oracle --extractor e4b-ft --tag jepa-baseline --report
```

Only this pair is comparable (run-to-run drift: the 27B teacher scored 0.564
vs 0.397 across sessions).

- [ ] **Step 6: Decision rule**

- candidate cortex ≥ baseline cortex + 0.02, guards clean → **JEPA adopted**
  (Task 10 trains with `--jepa-lambda 1.0`); if it also beats 0.564
  absolute, it is itself a deploy candidate for Task 11.
- within ±0.02 → optionally one λ ∈ {0.5, 2.0} rerun of Steps 1–5; else
  treat as neutral.
- neutral/negative → Task 10 trains plain SFT.

- [ ] **Step 7: Record the outcome (main session, not subagent)**

Store `memory_outcome(task="JEPA aux-loss ablation on E4B extractor
distill", outcome=success|failure, about="LLM-JEPA arXiv 2509.14252",
detail=<numbers>)` and a `memory_store` with the same-session pair. Commit
the results JSONs if the repo tracks `evals/results/` (check `git
check-ignore evals/results/e4b-jepa-ladder.json`; skip if ignored).

---

### Task 4: Sonnet recall-boosted teacher prompt

**Files:**
- Create: `evals/prompts/sonnet_recall_system.md`

**Interfaces:**
- Produces: the file read verbatim by `render_brief()` (Task 5).

- [ ] **Step 1: Write the prompt file**

```markdown
# Sonnet recall-boosted extraction prompt (teacher-side ONLY)

This prompt is PRIVATE to datagen: Sonnet labels sessions with it, but the
stored training rows carry the unchanged production `_SYSTEM_PROMPT`
(`pseudolife_memory/memory/dream.py`). Never ship this prompt.

---

You consolidate numbered notes into canonical facts. Extract durable,
current-state facts as JSON:
{"claims":[{"entity":..,"attribute":..,"value":..,"confidence":0..1,
"source":<number of the note the fact came from>}]}.

Recall matters most: extract ALL durable facts, not just the most salient
ones. Err toward inclusion — a fact about the user's life, preferences,
possessions, plans, relationships, health, work, or history qualifies even
if it seems minor. One claim per atomic fact; split compound statements.

Precision rules (unchanged from production):
- One slot per real fact; skip narrative, opinions, and obsolete states.
- When several notes state or update the SAME fact, use one consistent
  entity and attribute and emit only the CURRENT value (source = the note
  stating it).
- Reuse existing slot keys when they fit.
- Return {"claims":[]} ONLY when a session truly contains no durable
  content (pure smalltalk). Do not force claims out of nothing.
```

- [ ] **Step 2: Commit**

```bash
git add evals/prompts/sonnet_recall_system.md
git commit -m "feat(evals): private recall-boosted teacher prompt for Sonnet datagen"
```

---

### Task 5: `distill_datagen_sonnet.py` — plan + emit-briefs (TDD)

**Files:**
- Create: `evals/distill_datagen_sonnet.py`
- Create: `tests/test_distill_sonnet.py`

**Interfaces:**
- Consumes: `evals/distill_datagen.py` (`validate_claims`, `_parse_date`, `VOCAB_MAX`), `pseudolife_memory.memory.cortex._norm_key`, `pseudolife_memory.memory.dream._SYSTEM_PROMPT/_vocab_hint`, `evals/prompts/sonnet_recall_system.md`.
- Produces: `plan_questions(data) -> list[dict]` where each dict is `{"question_id": str, "sessions": [{"session_id": str, "date": str, "notes": [str]}]}` (chrono order, KU-forbidden and cross-question-duplicate sessions removed); `render_brief(qplan, recall_prompt) -> str`; CLI `--emit-briefs [--questions N]` writing `evals/data/sonnet_briefs/<question_id>.md`. Subagents write answers to `evals/data/sonnet_out/<question_id>.jsonl`, one line per session: `{"session_id": .., "claims": [..]}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_distill_sonnet.py`:

```python
"""Unit tests for the pure logic in evals/distill_datagen_sonnet.py."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from distill_datagen_sonnet import (  # noqa: E402
    ingest_question, plan_questions, render_brief,
)

DATASET = [
    {   # KU question: ALL its sessions are forbidden everywhere
        "question_id": "ku_1", "question_type": "knowledge-update",
        "haystack_session_ids": ["s_shared"], "haystack_dates": ["2023/01/01"],
        "haystack_sessions": [[{"role": "user", "content": "irrelevant"}]],
    },
    {
        "question_id": "q_a", "question_type": "single-session-user",
        "haystack_session_ids": ["s_1", "s_shared", "s_2"],
        "haystack_dates": ["2023/03/02", "2023/03/01", "2023/03/03"],
        "haystack_sessions": [
            [{"role": "user", "content": "I adopted a cat named Miso."}],
            [{"role": "user", "content": "forbidden content"}],
            [{"role": "user", "content": "Actually Miso is a dog."}],
        ],
    },
    {
        "question_id": "q_b", "question_type": "single-session-user",
        "haystack_session_ids": ["s_1", "s_3"],
        "haystack_dates": ["2023/03/02", "2023/04/01"],
        "haystack_sessions": [
            [{"role": "user", "content": "I adopted a cat named Miso."}],
            [{"role": "user", "content": "I work at Acme."}],
        ],
    },
]


def test_plan_excludes_forbidden_and_dedups_across_questions():
    plans = plan_questions(DATASET)
    ids = {p["question_id"]: [s["session_id"] for s in p["sessions"]]
           for p in plans}
    assert "ku_1" not in ids                      # KU questions never labeled
    assert ids["q_a"] == ["s_1", "s_2"]           # forbidden s_shared dropped
    assert ids["q_b"] == ["s_3"]                  # s_1 claimed by q_a (sorted order)


def test_plan_orders_sessions_chronologically():
    plans = plan_questions(DATASET)
    qa = next(p for p in plans if p["question_id"] == "q_a")
    # s_1 dated 03/02 precedes s_2 dated 03/03 (input order had s_2 last too,
    # but s_shared 03/01 sat between them)
    assert [s["date"] for s in qa["sessions"]] == ["2023/03/02", "2023/03/03"]


def test_render_brief_contains_sessions_and_contract():
    plans = plan_questions(DATASET)
    qa = next(p for p in plans if p["question_id"] == "q_a")
    brief = render_brief(qa, recall_prompt="RECALL-PROMPT-SENTINEL")
    assert "RECALL-PROMPT-SENTINEL" in brief
    assert brief.index("s_1") < brief.index("s_2")        # chrono order
    assert "sonnet_out/q_a.jsonl" in brief                # output contract
    assert '{"session_id"' in brief                       # row schema shown


def test_ingest_rewrites_prompt_and_recomputes_vocab():
    plans = plan_questions(DATASET)
    qa = next(p for p in plans if p["question_id"] == "q_a")
    answers = {
        "s_1": [{"entity": "Miso", "attribute": "species", "value": "cat",
                 "confidence": 0.9, "source": 1}],
        "s_2": [{"entity": "Miso", "attribute": "species", "value": "dog",
                 "confidence": 0.9, "source": 1}],
    }
    rows = ingest_question(qa, answers)
    assert [r["id"] for r in rows] == ["q_a:s_1", "q_a:s_2"]
    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT
    assert rows[0]["messages"][0]["content"] == _SYSTEM_PROMPT  # no hint yet
    # second session's hint is recomputed from claim 1, not subagent-supplied
    assert "miso.species" in rows[1]["messages"][0]["content"]
    assert rows[1]["messages"][1]["content"].startswith("[1] ")


def test_ingest_rejects_question_on_bad_claim():
    plans = plan_questions(DATASET)
    qa = next(p for p in plans if p["question_id"] == "q_a")
    answers = {
        "s_1": [{"entity": "Miso", "attribute": "species", "value": "cat",
                 "confidence": 0.9, "source": 7}],   # citation out of range
        "s_2": [],
    }
    assert ingest_question(qa, answers) is None


def test_ingest_rejects_question_on_missing_session():
    plans = plan_questions(DATASET)
    qa = next(p for p in plans if p["question_id"] == "q_a")
    assert ingest_question(qa, {"s_1": []}) is None   # s_2 unanswered
```

- [ ] **Step 2: Run tests to verify they fail**

```powershell
$env:PYTHONPATH="."; python -m pytest tests/test_distill_sonnet.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'distill_datagen_sonnet'`.

- [ ] **Step 3: Write the implementation**

`evals/distill_datagen_sonnet.py`:

```python
"""Sonnet-5 recall-tuned teacher labeling via Max-plan subagents (Stage 1.5).

Asymmetric prompt split (docs/superpowers/specs/2026-07-07): Sonnet labels
sessions under evals/prompts/sonnet_recall_system.md, but the stored
training rows keep the UNCHANGED production `_SYSTEM_PROMPT` + `_vocab_hint`
— the student learns "production prompt -> high-recall claims".

Three modes:
  --emit-briefs   write one self-contained brief per source question to
                  evals/data/sonnet_briefs/<qid>.md; a subagent answers each
                  brief by writing evals/data/sonnet_out/<qid>.jsonl
                  (one line per session: {"session_id", "claims": [...]}).
  --ingest        strictly validate sonnet_out/, recompute the vocab chain
                  deterministically, rewrite prompts to production form, and
                  append rows to evals/data/distill-extract-sonnet.jsonl.
                  A question is all-or-nothing: any bad row rejects it.
  --compare       recall proxies vs the Qwen labels on shared sessions.

Vocab evolution is sequential WITHIN a question and independent ACROSS
questions (matching distill_datagen.py), so dispatch is one subagent per
question, fully parallel. KU contamination guard reused verbatim.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/

from pseudolife_memory.memory.cortex import _norm_key             # noqa: E402
from pseudolife_memory.memory.dream import (                      # noqa: E402
    _SYSTEM_PROMPT, _vocab_hint,
)
from distill_datagen import (                                     # noqa: E402
    VOCAB_MAX, _parse_date, validate_claims,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
DATASET = DATA_DIR / "longmemeval_s_cleaned.json"
BRIEFS_DIR = DATA_DIR / "sonnet_briefs"
OUT_DIR = DATA_DIR / "sonnet_out"
MERGED = DATA_DIR / "distill-extract-sonnet.jsonl"
QWEN_SET = DATA_DIR / "distill-extract.jsonl"
RECALL_PROMPT = (Path(__file__).resolve().parent / "prompts"
                 / "sonnet_recall_system.md")


def _notes(session: list[dict], date: str) -> list[str]:
    return [f"[{date}] {t['role']}: {t['content'].strip()}"
            for t in session if (t.get("content") or "").strip()]


def plan_questions(data: list[dict]) -> list[dict]:
    """Chrono-ordered per-question session plans; KU-forbidden sessions and
    cross-question duplicates removed (first question in sorted order wins,
    matching distill_datagen's global dedup)."""
    ku = [q for q in data if q["question_type"] == "knowledge-update"]
    forbidden = {sid for q in ku for sid in q["haystack_session_ids"]}
    sources = sorted((q for q in data
                      if q["question_type"] != "knowledge-update"),
                     key=lambda q: q["question_id"])
    claimed: set[str] = set()
    plans = []
    for q in sources:
        sessions = []
        ordered = sorted(zip(q["haystack_dates"], q["haystack_session_ids"],
                             q["haystack_sessions"]),
                         key=lambda tpl: _parse_date(tpl[0]))
        for date, sid, session in ordered:
            if sid in forbidden or sid in claimed:
                continue
            notes = _notes(session, date)
            if not notes:
                continue
            claimed.add(sid)
            sessions.append({"session_id": sid, "date": date, "notes": notes})
        if sessions:
            plans.append({"question_id": q["question_id"],
                          "sessions": sessions})
    return plans


def render_brief(qplan: dict, recall_prompt: str) -> str:
    qid = qplan["question_id"]
    parts = [
        f"# Extraction brief — question {qid}",
        "",
        "You are labeling chat sessions for extractor training. Apply the",
        "extraction prompt below to EACH session independently, in order.",
        "Maintain a growing slot-key list: after each session, add each",
        "claim's key as `entity.attribute` normalized (lowercase, every run",
        "of non-alphanumeric characters collapsed to a single hyphen). When",
        "a later session updates a fact you already keyed, REUSE that key.",
        "",
        "## Extraction prompt",
        "", recall_prompt, "",
        "## Output contract",
        "",
        f"Write EXACTLY one file: evals/data/sonnet_out/{qid}.jsonl — one",
        "line per session, in the order given, each line:",
        '{"session_id": "<id>", "claims": [{"entity":..,"attribute":..,'
        '"value":..,"confidence":0..1,"source":<note number>}]}',
        'Every session MUST appear, with "claims": [] when nothing',
        "qualifies. No prose, no markdown fences, JSONL only.",
        "",
        "## Sessions (chronological)",
    ]
    for s in qplan["sessions"]:
        parts += ["", f"### session_id: {s['session_id']}   date: {s['date']}",
                  ""]
        parts += [f"[{i + 1}] {n}" for i, n in enumerate(s["notes"])]
    return "\n".join(parts) + "\n"


def ingest_question(qplan: dict, answers: dict[str, list]) -> list[dict] | None:
    """Rebuild production-shaped training rows from a subagent's answers.

    all-or-nothing: every session must be answered and every claim must pass
    validate_claims; the vocab hint is recomputed here from the accepted
    claims in chrono order — the subagent's own bookkeeping is never trusted.
    """
    rows = []
    vocab: set[str] = set()
    for s in qplan["sessions"]:
        claims_in = answers.get(s["session_id"])
        if claims_in is None:
            return None                                # unanswered session
        content = json.dumps({"claims": claims_in}, ensure_ascii=False)
        claims = validate_claims(content, len(s["notes"]))
        if claims is None:
            return None                                # schema violation
        vocab_list = sorted(vocab)[:VOCAB_MAX]
        target = json.dumps({"claims": claims}, ensure_ascii=False)
        rows.append({
            "id": f"{qplan['question_id']}:{s['session_id']}",
            "messages": [
                {"role": "system",
                 "content": _SYSTEM_PROMPT + _vocab_hint(vocab_list)},
                {"role": "user", "content": "\n\n".join(
                    f"[{i + 1}] {n}" for i, n in enumerate(s["notes"]))},
                {"role": "assistant", "content": target},
            ]})
        for c in claims:
            vocab.add(f"{_norm_key(c['entity'])}.{_norm_key(c['attribute'])}")
    return rows


def _cmd_emit(args) -> int:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    plans = plan_questions(data)
    if args.questions:
        plans = plans[:args.questions]
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    recall = RECALL_PROMPT.read_text(encoding="utf-8")
    done = {p.stem for p in OUT_DIR.glob("*.jsonl")}
    n = 0
    for p in plans:
        if p["question_id"] in done:
            continue
        (BRIEFS_DIR / f"{p['question_id']}.md").write_text(
            render_brief(p, recall), encoding="utf-8")
        n += 1
    print(f"{n} briefs -> {BRIEFS_DIR} ({len(done)} questions already "
          f"answered in {OUT_DIR})")
    return 0


def _cmd_ingest(args) -> int:
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    plans = {p["question_id"]: p for p in plan_questions(data)}
    done_ids = set()
    kept = empty_kept = 0
    if MERGED.exists():
        for line in MERGED.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            done_ids.add(row["id"].split(":")[0])
            kept += 1
            if not json.loads(row["messages"][-1]["content"])["claims"]:
                empty_kept += 1
    rejected = []
    with MERGED.open("a", encoding="utf-8") as out:
        for f in sorted(OUT_DIR.glob("*.jsonl")):
            qid = f.stem
            if qid in done_ids or qid not in plans:
                continue
            try:
                answers = {}
                for line in f.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        d = json.loads(line)
                        answers[d["session_id"]] = d["claims"]
            except (ValueError, KeyError):
                rejected.append(qid)
                continue
            rows = ingest_question(plans[qid], answers)
            if rows is None:
                rejected.append(qid)
                continue
            for r in rows:
                is_empty = not json.loads(
                    r["messages"][-1]["content"])["claims"]
                if is_empty and empty_kept >= args.max_empty_share * max(kept, 20):
                    continue
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
                kept += 1
                empty_kept += is_empty
    print(f"kept {kept} rows ({empty_kept} empty); rejected questions "
          f"(delete their sonnet_out file and re-dispatch): {rejected}")
    return 0


def _cmd_compare(args) -> int:
    qwen = {}
    for line in QWEN_SET.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        qwen[row["id"]] = json.loads(row["messages"][-1]["content"])["claims"]
    per_id, keys = {}, defaultdict(lambda: [set(), set()])
    for line in MERGED.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        rid = row["id"]
        if rid not in qwen:
            continue
        claims = json.loads(row["messages"][-1]["content"])["claims"]
        per_id[rid] = (len(claims), len(qwen[rid]))
        for c in claims:
            keys[rid][0].add(f"{_norm_key(c['entity'])}."
                             f"{_norm_key(c['attribute'])}")
        for c in qwen[rid]:
            keys[rid][1].add(f"{_norm_key(c['entity'])}."
                             f"{_norm_key(c['attribute'])}")
    if not per_id:
        print("no shared sessions between the two sets yet")
        return 1
    s_mean = sum(a for a, _ in per_id.values()) / len(per_id)
    q_mean = sum(b for _, b in per_id.values()) / len(per_id)
    jac = [len(a & b) / len(a | b) for a, b in keys.values() if a | b]
    print(f"shared sessions: {len(per_id)}")
    print(f"claims/session — sonnet {s_mean:.2f} vs qwen {q_mean:.2f} "
          f"(ratio {s_mean / max(q_mean, 1e-9):.2f})")
    print(f"sonnet>qwen on {sum(1 for a, b in per_id.values() if a > b)} "
          f"sessions; slot-key jaccard mean "
          f"{sum(jac) / max(len(jac), 1):.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-briefs", action="store_true")
    mode.add_argument("--ingest", action="store_true")
    mode.add_argument("--compare", action="store_true")
    ap.add_argument("--questions", type=int, default=0,
                    help="emit only the first N question briefs (pilot)")
    ap.add_argument("--max-empty-share", type=float, default=0.2)
    args = ap.parse_args()
    if args.emit_briefs:
        return _cmd_emit(args)
    if args.ingest:
        return _cmd_ingest(args)
    return _cmd_compare(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

```powershell
$env:PYTHONPATH="."; python -m pytest tests/test_distill_sonnet.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Full suite (no regressions)**

```powershell
$env:PYTHONPATH="."; python -m pytest tests/ -q
```

Expected: everything green (known PG-lock flake `test_retire_by_writer`
passes in isolation if it trips).

- [ ] **Step 6: Commit**

```bash
git add evals/distill_datagen_sonnet.py tests/test_distill_sonnet.py
git commit -m "feat(evals): Sonnet-5 recall datagen — briefs, strict ingest, compare"
```

---

### Task 6: `distill_clean.py` path arguments

**Files:**
- Modify: `evals/distill_clean.py:32-34,61-62`

**Interfaces:**
- Produces: `python evals/distill_clean.py [--src PATH] [--dst PATH]`, defaults exactly the current constants.

- [ ] **Step 1: Add argparse**

Replace `main()`'s first line and the `SRC`/`DST` uses:

```python
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--dst", type=Path, default=DST)
    args = ap.parse_args()
    rows = [json.loads(l) for l in args.src.open(encoding="utf-8")]
```

and `with args.dst.open("w", encoding="utf-8") as f:`. Add
`import argparse` to the imports.

- [ ] **Step 2: Verify defaults unchanged**

```powershell
python evals/distill_clean.py
```

Expected: identical summary line to the last recorded run (same
counts — it re-cleans the existing Qwen set deterministically).

- [ ] **Step 3: Commit**

```bash
git add evals/distill_clean.py
git commit -m "feat(evals): --src/--dst for distill_clean (sonnet set reuse)"
```

---

### Task 7: Pilot — 10 questions through subagents + audit

**Files:** data only (gitignored). Main-session work — subagent dispatch and
memory tools live here, not in a worker.

- [ ] **Step 1: Emit pilot briefs**

```powershell
$env:PYTHONPATH="."; python evals/distill_datagen_sonnet.py --emit-briefs --questions 10
```

Expected: `10 briefs -> evals/data/sonnet_briefs`.

- [ ] **Step 2: Dispatch one subagent per brief (parallel)**

Per question: a general-purpose subagent whose prompt is: read
`evals/data/sonnet_briefs/<qid>.md`, follow it exactly, write
`evals/data/sonnet_out/<qid>.jsonl`, reply with only the row count.
Dispatch all 10 in parallel; they share no state.

- [ ] **Step 3: Ingest + re-dispatch rejects**

```powershell
$env:PYTHONPATH="."; python evals/distill_datagen_sonnet.py --ingest
```

Expected: `rejected questions: []` (else delete the listed
`sonnet_out/<qid>.jsonl` files and re-dispatch those questions once; a
second rejection means the brief contract needs fixing — stop and review).

- [ ] **Step 4: Recall proxies vs Qwen**

```powershell
$env:PYTHONPATH="."; python evals/distill_datagen_sonnet.py --compare
```

Record: claims/session ratio, sonnet>qwen share, slot-key jaccard.

- [ ] **Step 5: Hand audit 30 rows**

Read ~30 rows across ≥5 questions in `distill-extract-sonnet.jsonl` against
their source sessions. Check: (a) claims are real facts, not narrative
padding; (b) update turns REUSE the initial turn's slot key; (c) empty rows
are genuinely empty sessions; (d) no echo-key (`user.`-prefixed attribute)
or spam-fill pathologies.

- [ ] **Step 6: Pilot gate**

Proceed to Task 8 only if claims/session ratio > 1.1 AND the audit is clean.
Otherwise stop arm B, record `memory_outcome(outcome="failure")` with the
numbers, and keep the Qwen dataset (Task 10 then re-trains only if arm A
adopted JEPA; otherwise Stage 1.5 ends with arm A's result).

---

### Task 8: Full Sonnet datagen fan-out + clean

**Files:** data only (gitignored).

- [ ] **Step 1: Emit all remaining briefs**

```powershell
$env:PYTHONPATH="."; python evals/distill_datagen_sonnet.py --emit-briefs
```

- [ ] **Step 2: Dispatch in waves**

Batches of ~8 parallel subagents per usage window until
`evals/data/sonnet_out/` covers all briefed questions; run `--ingest` after
each wave (resumable — ingested questions are skipped, rejects listed for
re-dispatch).

- [ ] **Step 3: Clean**

```powershell
python evals/distill_clean.py --src evals/data/distill-extract-sonnet.jsonl --dst evals/data/distill-extract-sonnet-clean.jsonl
```

Expected: summary line; empty-row share ≤ ~20%; dropped-row share below the
Qwen run's (Sonnet should produce fewer echo-key/spam pathologies — if
dropped share is HIGHER, audit before training).

- [ ] **Step 4: Row-count sanity**

```powershell
Get-Content evals/data/distill-extract-sonnet-clean.jsonl | Measure-Object -Line
```

Expected: same order of magnitude as the Qwen set (~1.7k rows).

---

### Task 9: Arm B training run

**Files:**
- Create (gitignored): `~/e4b-sonnet/*` (WSL), `evals/models/e4b-sonnet-Q4_K_M.gguf`

**Interfaces:**
- Consumes: Task 3's decision (`--jepa-lambda 1.0` if adopted, else omit); `DATA` constant in `distill_train_e4b.py`.

- [ ] **Step 1: Point the trainer at the Sonnet set**

`distill_train_e4b.py` hardcodes `DATA`. Add one more flag next to
`--out-dir` (same pattern):

```python
    ap.add_argument("--data", type=Path, default=DATA)
```

and use `args.data` in `main()`'s `rows = [...]` line. Commit:

```bash
git add evals/distill_train_e4b.py
git commit -m "feat(evals): --data flag for alternate distill sets"
```

- [ ] **Step 2: Smoke, then full run (WSL)**

```bash
source ~/e4b-train/bin/activate
python evals/distill_train_e4b.py --smoke \
    --data evals/data/distill-extract-sonnet-clean.jsonl [--jepa-lambda 1.0] --out-dir ~/e4b-sonnet
nohup python evals/distill_train_e4b.py \
    --data evals/data/distill-extract-sonnet-clean.jsonl [--jepa-lambda 1.0] \
    --out-dir ~/e4b-sonnet > ~/e4b-sonnet-run.log 2>&1 &
```

(`[--jepa-lambda 1.0]` present iff arm A adopted JEPA.) Expected: ~2 epochs,
step count scales with the cleaned row count.

- [ ] **Step 3: Merge + GGUF**

Same as Task 3 Steps 2–3 with `e4b-sonnet` substituted for `e4b-jepa`.

---

### Task 10: Arm B gated eval

**Files:**
- Create: `evals/results/*sonnet*` result JSONs.

Identical procedure to Task 3 Steps 4–6 with tags `sonnet` /
`sonnet-baseline` and the `e4b-sonnet-Q4_K_M.gguf` candidate:

- [ ] **Step 1: Ladder** — serve candidate on :8081, `python evals/ladder_sweep.py e4b-ft`, rename result to `e4b-sonnet-ladder.json`. Gate: gold ≥ 0.9, stale_leak = 0.0 (recall-boosted labels teaching noise would trip exactly here — a stale_leak > 0 is a hard stop).
- [ ] **Step 2: KU-oracle pair** — candidate with `--tag sonnet`, then deployed e4b-ft GGUF with `--tag sonnet-baseline`, same sitting, both `--report`ed.
- [ ] **Step 3: Decision** — deploy candidate iff cortex > baseline AND > 0.564 absolute with guards clean. Record `memory_outcome` either way (main session).

---

### Task 11: Deploy the winner

**Files:**
- Modify: `ops/docker-compose.yml:157` (the GGUF mount line)

Only if a Task 3 or Task 10 candidate cleared every gate. Backup-first, per
project practice.

- [ ] **Step 1: Backup + rollback tag**

```powershell
pwsh ops/backup.ps1
docker tag pseudolife-extractor:gemma4-e4b pseudolife-extractor:pre-stage15-$(Get-Date -Format yyyyMMdd)
```

- [ ] **Step 2: Swap the mount**

In `ops/docker-compose.yml`, change the active mount line

```yaml
      - ../evals/models/e4b-extractor-Q4_K_M.gguf:/models/extractor.gguf:ro
```

to the winner (e.g. `e4b-sonnet-Q4_K_M.gguf`), keeping `:/models/extractor.gguf:ro`.

- [ ] **Step 3: Restart + verify**

```powershell
docker compose -f ops/docker-compose.yml up -d pseudolife-extractor
curl http://127.0.0.1:8081/v1/models
```

Expected: the extractor answers; then one live-smoke dream cycle and a
ladder spot-check against the served sidecar.

- [ ] **Step 4: Commit + record**

```bash
git add ops/docker-compose.yml
git commit -m "feat(ops): deploy Stage-1.5 extractor (<winner>) as sidecar GGUF"
```

Store the deploy memory (`memory_store`, tags `["deploy", "milestone"]`)
with the rollback tag name and the gate numbers.

---

## Self-review notes

- Spec coverage: parity gate (T2), JEPA loss + fixed views (T1), arm-A run/eval/decision (T3), private prompt (T4), briefs/ingest/compare (T5), clean reuse (T6), pilot gate (T7), fan-out (T8), retrain (T9), arm-B gates (T10), deploy + rollback (T11), memory outcomes (T3/T7/T10/T11). λ-sweep contingency lives in T3 Step 6.
- Type consistency: `plan_questions → {question_id, sessions:[{session_id,date,notes}]}` consumed identically by `render_brief`/`ingest_question`/tests; view column names match between `tokenize_fixed` and `_VIEW_KEYS`.
- Known environmental unknowns (flagged in-task, not placeholders): llama.cpp checkout path (T3S3), exact ladder rung name (T3S4 verifies with `rg`), backup script name (T11S1 — verify `ops/backup.ps1` exists before running; adjust if the 2026-06-17 setup named it differently).
