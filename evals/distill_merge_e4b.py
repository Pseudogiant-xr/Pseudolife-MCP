"""Merge-only recovery for the E4B QLoRA run (2026-07-06).

The full training run in distill_train_e4b.py completed (410 steps,
eval_loss 0.130) but the in-process save_pretrained_merged was OOM-killed:
Unsloth stages the full bf16 base safetensors in system RAM and the 16GB
WSL cap wasn't enough. This reloads the final adapter checkpoint and redoes
just the merge (run after raising .wslconfig memory to 40GB).

    ~/e4b-train/bin/python .../evals/distill_merge_e4b.py
"""
from __future__ import annotations

from pathlib import Path

from unsloth import FastModel                     # import first (patches)

OUT = Path.home() / "e4b-extractor"
CKPT = OUT / "checkpoints" / "checkpoint-410"     # final step of the run

model, tokenizer = FastModel.from_pretrained(
    str(CKPT),                                    # adapter; base pulled from config
    max_seq_length=5120,
    load_in_4bit=True,
)
model.save_pretrained_merged(
    str(OUT / "merged"), tokenizer, save_method="merged_16bit")
print(f"merged model -> {OUT / 'merged'}")
