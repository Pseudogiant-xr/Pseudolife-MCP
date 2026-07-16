"""QLoRA fine-tune of Gemma-4 E4B on the teacher-labeled extraction set.

Stage-1 SFT of the bespoke-extractor plan (docs/specs/2026-07-04): the
student learns the production extraction task from 1,756 cleaned Qwen3.6-27B
teacher rows (evals/distill_clean.py — echo-key/spam/mega-row filtered).

Runs in the WSL uv venv (~/e4b-train, unsloth + CUDA torch), NOT the repo
.venv. Hard-won shape/memory decisions (2026-07-06 debugging):

* PRE-TOKENIZED, ONE FIXED SHAPE. Everything is padded to MAX_SEQ so the
  whole run compiles exactly once. With variable shapes unsloth recompiled
  every step (29s -> 1205s/it); with compilation disabled instead, the fused
  cross-entropy was lost and the 262k-vocab logits materialised (~13GB at 6k
  tokens), overflowing the 24GB card into WSL's sysmem fallback (~100x slow,
  the "100% util at 120W" signature).
* Labels: -100 on prompt + padding; loss lands on the completion tokens only
  (unsloth's train_on_responses_only marker-matching silently masked out 96%
  of samples against Gemma-4's "<|turn>" template — do not use it here).
* add_special_tokens=False everywhere: the chat template text already
  carries <bos>.

The merged bf16 model is written for GGUF conversion; the acceptance gate is
the KU-oracle bench (beat base E4B cortex 0.333 / hybrid 0.564; ladder
stale_leak 0.0).

    source ~/e4b-train/bin/activate
    python .../evals/distill_train_e4b.py [--smoke]

Stage-1.5 (docs/superpowers/specs/2026-07-07): --jepa-lambda adds the
LLM-JEPA auxiliary loss (arXiv 2509.14252) — last-token view embeddings,
<unusedN> predictor tokens, cosine loss, fixed view shapes 4096/1024.
--jepa-lambda 0 (default) is the exact Stage-1 path; parity-gate it against
an unmodified checkout before any ablation run.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import torch
import torch.nn.functional as F
from unsloth import FastModel        # before transformers/trl (patches them)
from datasets import Dataset
from transformers import default_data_collator
from transformers.trainer_utils import get_last_checkpoint
from trl import SFTConfig, SFTTrainer

# Runs inside WSL; point at the Windows checkout (/mnt/c/Users/<you>/...).
REPO = Path(os.environ.get("PSEUDOLIFE_REPO",
                           str(Path.home() / "Pseudolife-MCP")))
DATA = REPO / "evals/data/distill-extract-clean.jsonl"
OUT = Path.home() / "e4b-extractor"               # WSL-local: fast disk io
# 5120 (not the measured max 6,418): at 8192 the fixed-shape compile itself
# overflowed 24GB into WSL sysmem spill. ~96% of rows fit in 5120; the rest
# are DROPPED (truncating would cut off the completion we train on).
MAX_SEQ = 5120
# JEPA view shapes (aux loss only — CE rows keep MAX_SEQ + drop-don't-truncate).
# Fixed so the run compiles exactly three graphs: 5120 SFT + 4096 + 1024.
NOTES_VIEW_SEQ = 4096
CLAIMS_VIEW_SEQ = 1024
HOLDOUT = 64                                      # eval rows (seeded split)


def find_resume_checkpoint(checkpoints_dir: Path) -> str | None:
    """Auto-detect a checkpoint to resume from — makes a crashed/killed run
    resumable by just re-running the exact same command (2026-07-12: save_steps
    checkpointing existed but nothing reloaded it, so a restart silently began
    from scratch and threw away every prior checkpoint's progress)."""
    if not checkpoints_dir.exists():
        return None
    return get_last_checkpoint(str(checkpoints_dir))


def tokenize_fixed(tok, rows: list[dict], jepa_k: int = 0) -> Dataset:
    feats = {"input_ids": [], "labels": [], "attention_mask": []}
    if jepa_k:
        # Predictor tokens = Gemma's reserved <unusedN> vocab. Their embedding
        # rows stay frozen under QLoRA; the LoRA-adapted layers learn to emit
        # the prediction at those positions (deviation from the paper's newly
        # learned tokens — embedding surgery on a 4-bit base is not worth it).
        # E4B ships a Gemma4Processor, not a tokenizer: convert_tokens_to_ids /
        # unk_token_id live on its inner .tokenizer, not the processor itself.
        _tk = getattr(tok, "tokenizer", tok)
        pred_ids = [_tk.convert_tokens_to_ids(f"<unused{i}>")
                    for i in range(jepa_k)]
        assert all(isinstance(i, int) and i >= 0
                   and i != _tk.unk_token_id for i in pred_ids), pred_ids
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


_VIEW_KEYS = ("notes_ids", "notes_mask", "notes_pred_pos",
              "claims_ids", "claims_mask", "claims_last_pos")


class JEPATrainer(SFTTrainer):
    """SFTTrainer + LLM-JEPA auxiliary loss (arXiv 2509.14252).

    L = L_SFT + lambda * (1 - cos(pred(E(notes)), sg[E(claims)])), last-token
    hidden states as view embeddings, predictor = k reserved tokens appended
    to the notes view.

    Why two separate backwards (not one summed loss): Gemma-4 E-series shares
    K/V across layers via a MODULE-scoped carrier on transformers <= 5.5.0
    (the ceiling unsloth 2026.6.9 pins). Running the SFT forward AND the view
    forwards before a single backward makes the second forward corrupt the
    first graph's gradients, and unsloth raises rather than train silently
    wrong (fix is transformers >= 5.5.2 function-scoped K/V, unreachable under
    the pinned unsloth). So the SFT term takes its own forward+backward
    (unsloth-native, fused CE), then the JEPA term takes a separate
    forward+backward; gradients accumulate on the LoRA params before the
    shared optimizer step. Gradient checkpointing stays ON throughout
    (disabling it around the 4096-token notes forward overflows the 24GB
    card). What keeps the module-scoped shared-KV carrier from being
    corrupted is the split into two backwards plus a no_grad (stop-gradient)
    claims target — canonical for JEPA/BYOL — so only ONE grad graph is ever
    alive. JEPA is TRAIN-only; eval_loss stays CE-only, comparable to
    baseline.
    """

    def __init__(self, *args, jepa_lambda: float = 0.0, **kw):
        super().__init__(*args, **kw)
        self.jepa_lambda = jepa_lambda

    def compute_loss(self, model, inputs, return_outputs=False,
                     num_items_in_batch=None):
        # Eval path strips the view columns (training_step pops them before the
        # train forward, but evaluate() bypasses training_step). With
        # remove_unused_columns=False they would otherwise ride into model(**
        # inputs); keep eval CE-only and identical to baseline.
        for k in _VIEW_KEYS:
            inputs.pop(k, None)
        return super().compute_loss(model, inputs, return_outputs=return_outputs,
                                    num_items_in_batch=num_items_in_batch)

    def _jepa_term(self, model, view):
        """One JEPA forward pair; returns the (already lambda-weighted) loss."""
        # logits_to_keep=1: only hidden states are read, but a plain forward
        # materialises the full (seq, ~262k-vocab) logits tensor as a side
        # effect — the blowup the header warns about. One position bounds it.
        with torch.no_grad():                          # stop-gradient target
            h_c = model(input_ids=view["claims_ids"],
                        attention_mask=view["claims_mask"],
                        output_hidden_states=True,
                        logits_to_keep=1).hidden_states[-1]
            tgt = h_c[torch.arange(h_c.size(0), device=h_c.device),
                      view["claims_last_pos"]].float()
        h_n = model(input_ids=view["notes_ids"],
                    attention_mask=view["notes_mask"],
                    output_hidden_states=True,
                    logits_to_keep=1).hidden_states[-1]
        pred = h_n[torch.arange(h_n.size(0), device=h_n.device),
                   view["notes_pred_pos"]].float()
        return self.jepa_lambda * (
            1.0 - F.cosine_similarity(pred, tgt, dim=-1).mean())

    def training_step(self, model, inputs, num_items_in_batch=None):
        view = {k: inputs.pop(k) for k in _VIEW_KEYS if k in inputs}
        # SFT forward+backward first (unsloth-native), freeing its graph before
        # the JEPA term's own forward+backward. Gradient checkpointing stays ON
        # throughout — disabling it around the 4096-token notes forward
        # overflows the 24GB card (WSL sysmem spill). Two separate backwards
        # with a no_grad (stop-gradient) target keep only ONE grad graph alive
        # at a time, so the module-scoped shared-KV carrier is never corrupted.
        sft_loss = super().training_step(model, inputs, num_items_in_batch)
        if not (self.jepa_lambda and view):
            return sft_loss
        jepa = self._jepa_term(model, view)
        # match how the base Trainer scales the SFT loss before backward — the
        # dynamic denominator is smaller on the last, partial accumulation
        # window of an epoch (falls back to the static arg pre-loop).
        gas = getattr(self, "current_gradient_accumulation_steps",
                      self.args.gradient_accumulation_steps)
        self.accelerator.backward(jepa / gas)
        return sft_loss + (jepa.detach() / gas)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true",
                    help="5 steps on 40 rows — shape/memory/throughput check")
    ap.add_argument("--jepa-lambda", type=float, default=0.0,
                    help="JEPA aux-loss weight; 0 = exact baseline path")
    ap.add_argument("--jepa-pred-tokens", type=int, default=2)
    ap.add_argument("--out-dir", type=Path, default=OUT,
                    help="run dir (checkpoints + merged); default unchanged")
    ap.add_argument("--data", type=Path, default=DATA,
                    help="training jsonl (cleaned); a relative path resolves "
                         "against the repo root. Default = Qwen baseline set")
    ap.add_argument("--save-steps", type=int, default=100)
    ap.add_argument("--eval-steps", type=int, default=200)
    ap.add_argument("--logging-steps", type=int, default=10)
    ap.add_argument("--no-eval", action="store_true",
                    help="skip all eval (mid-run + final); the eval pass spills "
                         "to WSL sysmem at the VRAM cap and is the documented "
                         "run killer. save-steps still gives recovery points; "
                         "eval_loss is not comparable across arms anyway")
    args = ap.parse_args()
    jepa_k = args.jepa_pred_tokens if args.jepa_lambda > 0 else 0

    model, tokenizer = FastModel.from_pretrained(
        "unsloth/gemma-4-E4B-it",
        max_seq_length=MAX_SEQ,
        load_in_4bit=True,
    )
    model = FastModel.get_peft_model(
        model,
        finetune_vision_layers=False,             # text-only task
        finetune_language_layers=True,
        finetune_attention_modules=True,
        finetune_mlp_modules=True,
        r=16, lora_alpha=16, lora_dropout=0.0, bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=42,
    )

    data_path = args.data if args.data.is_absolute() else REPO / args.data
    rows = [json.loads(l) for l in data_path.open(encoding="utf-8")]
    if args.smoke:
        rows = rows[-40:]                         # tail rows include long ones
    ds = tokenize_fixed(tokenizer, rows, jepa_k=jepa_k)
    split = ds.train_test_split(test_size=(4 if args.smoke else HOLDOUT),
                                seed=42)

    trainer = JEPATrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=split["train"],
        eval_dataset=split["test"],
        data_collator=default_data_collator,
        jepa_lambda=args.jepa_lambda,
        args=SFTConfig(
            output_dir=str(args.out_dir / "checkpoints"),
            dataset_kwargs={"skip_prepare_dataset": True},
            max_length=MAX_SEQ,
            remove_unused_columns=(False if jepa_k else True),
            per_device_train_batch_size=1,
            # eval batch must match: the default (8) pushed 8 fixed-5120 rows
            # through the fused CE at once at step 200 and wedged the driver
            # ("CUDA driver error: device not ready"), killing run 1 at 49%.
            per_device_eval_batch_size=1,
            gradient_accumulation_steps=8,
            max_steps=(5 if args.smoke else -1),
            num_train_epochs=2,
            learning_rate=2e-4,
            lr_scheduler_type="cosine",
            warmup_ratio=0.03,
            logging_steps=(1 if args.smoke else args.logging_steps),
            eval_strategy=("no" if (args.smoke or args.no_eval) else "steps"),
            eval_steps=args.eval_steps,
            # steps, not epoch: run 1 died at step 200 with the first epoch
            # checkpoint still 5 steps away — 75 minutes lost. Adapters are
            # ~150MB; checkpoint often.
            save_strategy=("no" if args.smoke else "steps"),
            save_steps=args.save_steps,
            save_total_limit=2,
            bf16=True,
            optim="adamw_8bit",
            seed=42,
            report_to="none",
        ),
    )
    resume = None if args.smoke else find_resume_checkpoint(
        args.out_dir / "checkpoints")
    if resume:
        print(f"resuming from checkpoint: {resume}", flush=True)
    trainer.train(resume_from_checkpoint=resume)
    if args.smoke:
        print("SMOKETEST OK")
        return 0
    if not args.no_eval:
        print("final eval:", trainer.evaluate())
    model.save_pretrained_merged(
        str(args.out_dir / "merged"), tokenizer, save_method="merged_16bit")
    print(f"merged model -> {args.out_dir / 'merged'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
