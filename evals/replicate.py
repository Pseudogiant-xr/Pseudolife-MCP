"""Replication + variance tooling over ``longmemeval_bench`` results.

The bench reports single runs as point estimates, but three runs of the
identical sonnet-5-v1 config (byte-identical contexts, temperature 0)
scored cortex 0.808/0.731/0.782 — judge-side noise wider than most
differences being decided. This module makes replication cheap and the
statistics honest:

    spawn    copy a judged JSONL, judge fields stripped, under -rN tags
    run      answer-phase every pending replicate (needs the Qwen endpoint)
    agg      aggregate replicates -> <base>.agg.json with mean +/- std
    compare  paired permutation test between two configs (by question_id)
    copy     strip-copy one file to a new tag (regression-gate fallback)
    gate-check  compare replicate means against the committed baseline
    baseline    (re)establish evals/results/regression_gate.baseline.json

Import-light by design: the bench module (and through it ladder_sweep /
torch) is imported ONLY inside ``cmd_run``. Naming/IO helpers are small
local mirrors of the bench's, kept in lockstep by tests.

Spec: docs/superpowers/specs/2026-07-18-eval-hygiene-sprint-design.md
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("rag", "cortex", "hybrid")
_JUDGE_FIELDS = {f"{arm}_{s}" for arm in ARMS
                 for s in ("response", "correct", "context_tokens")}
_REPLICA_SUFFIX = re.compile(r"-r\d+$")
BASELINE_FLOOR = 0.03
DEFAULT_BASELINE = RESULTS_DIR / "regression_gate.baseline.json"


# ── naming (mirrors longmemeval_bench.out_file — duplicated because that
#    module imports ladder_sweep/torch at module level) ────────────────────
def result_file(dataset: str, extractor: str, tag: str = "",
                results_dir: Path = RESULTS_DIR) -> Path:
    suffix = f"-{tag}" if tag else ""
    return results_dir / f"longmemeval-ku-{dataset}-{extractor}{suffix}.jsonl"


def replicate_tag(base_tag: str, i: int) -> str:
    return f"{base_tag}-r{i}" if base_tag else f"r{i}"


# ── row IO (mirrors the bench's tolerant JSONL semantics) ─────────────────
def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def is_judged(row: dict) -> bool:
    return all(f"{arm}_correct" in row for arm in ARMS)


def strip_judged(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k not in _JUDGE_FIELDS}
            for r in rows]


def discover(dataset: str, extractor: str, tag: str = "",
             results_dir: Path = RESULTS_DIR) -> dict[str, Path]:
    """Replicate tag -> path: the base file plus strict ``-r<digits>``
    variants. ``arm1`` never matches ``arm1-baseline`` or ``arm1-gate``."""
    base = result_file(dataset, extractor, tag, results_dir)
    stem = base.name.removesuffix(".jsonl")
    found: dict[str, Path] = {}
    if base.exists():
        found[tag] = base
    for p in sorted(results_dir.glob(stem + "-r*.jsonl")):
        rest = p.name.removesuffix(".jsonl")[len(stem):]
        if _REPLICA_SUFFIX.fullmatch(rest):
            found[f"{tag}{rest}" if tag else rest[1:]] = p
    return found
