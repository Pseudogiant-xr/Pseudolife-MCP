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
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from unsloth import FastModel                     # import first (patches)
from datasets import Dataset
from transformers import default_data_collator
from trl import SFTConfig, SFTTrainer

REPO = Path("/mnt/c/Users/HAMO9/ClaudeCode/PseudoLife-MCP")
DATA = REPO / "evals/data/distill-extract-clean.jsonl"
OUT = Path.home() / "e4b-extractor"               # WSL-local: fast disk io
# 5120 (not the measured max 6,418): at 8192 the fixed-shape compile itself
# overflowed 24GB into WSL sysmem spill. ~96% of rows fit in 5120; the rest
# are DROPPED (truncating would cut off the completion we train on).
MAX_SEQ = 5120
HOLDOUT = 64                                      # eval rows (seeded split)


def tokenize_fixed(tok, rows: list[dict]) -> Dataset:
    feats = {"input_ids": [], "labels": [], "attention_mask": []}
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
    return Dataset.from_dict(feats)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true",
                    help="5 steps on 40 rows — shape/memory/throughput check")
    args = ap.parse_args()

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

    rows = [json.loads(l) for l in DATA.open(encoding="utf-8")]
    if args.smoke:
        rows = rows[-40:]                         # tail rows include long ones
    ds = tokenize_fixed(tokenizer, rows)
    split = ds.train_test_split(test_size=(4 if args.smoke else HOLDOUT),
                                seed=42)

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=split["train"],
        eval_dataset=split["test"],
        data_collator=default_data_collator,
        args=SFTConfig(
            output_dir=str(OUT / "checkpoints"),
            dataset_kwargs={"skip_prepare_dataset": True},
            max_length=MAX_SEQ,
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
            logging_steps=(1 if args.smoke else 10),
            eval_strategy=("no" if args.smoke else "steps"),
            eval_steps=200,
            # steps, not epoch: run 1 died at step 200 with the first epoch
            # checkpoint still 5 steps away — 75 minutes lost. Adapters are
            # ~150MB; checkpoint often.
            save_strategy=("no" if args.smoke else "steps"),
            save_steps=100,
            save_total_limit=2,
            bf16=True,
            optim="adamw_8bit",
            seed=42,
            report_to="none",
        ),
    )
    trainer.train()
    if args.smoke:
        print("SMOKETEST OK")
        return 0
    print("final eval:", trainer.evaluate())
    model.save_pretrained_merged(
        str(OUT / "merged"), tokenizer, save_method="merged_16bit")
    print(f"merged model -> {OUT / 'merged'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
