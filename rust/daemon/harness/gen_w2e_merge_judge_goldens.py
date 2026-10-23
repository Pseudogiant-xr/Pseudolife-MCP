"""Write the golden cases for the merge judge's pure pieces
(``src/dream/judge.rs``, ``src/dream/review_judgments.rs``).

Every expected value comes from calling the real Python code in this
checkout: ``memory/dream.format_judge_proposal``, the validation loop of
``OpenAICompatExtractor.judge_merges`` (driven through a stub
``_judge_request``), ``memory/graph_review.note_with_tail``,
``memory/review_judgments.fingerprint`` / ``signed_deep_dream`` /
``judging_policy`` and ``DreamOps._served_model_mismatch``.

Output: ``goldens/w2e-merge-judge.json``.

usage: python gen_w2e_merge_judge_goldens.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "w2e-merge-judge.json"

SIDE_A = {"display": "Kafka Broker", "etype": None, "degree": 3,
          "scopes": ["proj-a", "it's"], "snippets": ["one " * 80, "two", "three"]}
SIDE_B = {"display": None, "etype": "tool", "degree": 0, "scopes": [], "snippets": []}
SIDE_C = {"display": "Ünïcode ✓", "degree": 1, "scopes": ["b\"q"],
          "snippets": ["x" * 300, "é" * 10]}

PROPOSALS = [
    {"n": 1, "from": SIDE_A, "into": SIDE_B, "reason": "write-dedup: a",
     "score": 0.7099999785423279, "low_differential": True, "snippet_chars": 240},
    {"n": 2, "from": SIDE_C, "into": SIDE_A, "reason": None, "score": None,
     "low_differential": False, "snippet_chars": 0},
    {"n": 3, "from": {}, "into": SIDE_C, "reason": "", "score": 1.0,
     "low_differential": None, "snippet_chars": 5},
    {"n": 4, "from": SIDE_B, "into": SIDE_B, "reason": "r", "score": 1e-05},
]

RAW_VERDICTS = [
    {"id": 1, "verdict": "Accept", "confidence": 0.9, "note": "same"},
    {"id": "2", "verdict": " reject ", "confidence": "0.85", "relation": "Uses"},
    {"id": 3, "verdict": "relate", "confidence": 2, "relation": "teleports"},
    {"id": 4.7, "verdict": "leave", "note": "n" * 300},
    {"id": 9, "verdict": "accept"},
    {"id": None, "verdict": "accept"},
    {"id": 1, "verdict": "maybe"},
    {"id": True, "verdict": "reject", "confidence": None, "relation": 0},
    {"id": 2, "verdict": "relate", "confidence": "nan", "relation": "part of"},
    "not a dict",
    {"id": 3, "verdict": "reject", "confidence": -1, "relation": "related-to"},
]

NOTES = [
    (None, " | 3rd (m3): reject 0.90 [2-1 reject]"),
    ("relate:uses " + "x" * 500 + " | 2nd (m2): reject 0.80 [split]",
     " | 3rd (model-three): relate:part-of 0.70 [2-1 reject]"),
    ("plain note | 2nd (" + "m" * 420 + "): accept 0.60 [split]", " | 3rd (m): accept 0.50 [2-1 accept]"),
    ("short", " | 3rd (m): leave 0.00 [no majority]"),
]

SERVED = [
    ("gpt-4o", "gpt-4o-2024-08-06"), ("gpt-4o", "gpt-4o-20240806"),
    ("gpt-4o", "gpt-4o-mini"), ("judge", "anything"), ("model-b", "model-a"),
    ("model-b", None), ("model-b", "model-b"), ("gpt-4o", "gpt-4o-2024-08-0"),
]


def main() -> int:
    sys.path.insert(0, str(REPO))
    from pseudolife_memory.memory import dream
    from pseudolife_memory.memory.graph_review import note_with_tail
    from pseudolife_memory.memory.review_judgments import (
        fingerprint, judging_policy, signed_deep_dream)
    from pseudolife_memory.service_dream import DreamOps
    from pseudolife_memory.utils.config import AppConfig

    formatted = [[p, dream.format_judge_proposal(p)] for p in PROPOSALS]

    ex = dream.OpenAICompatExtractor("http://127.0.0.1:9/v1", "judge-one")
    ex._judge_request = lambda *a, **k: list(RAW_VERDICTS)
    proposals = [{"n": n} for n in (1, 2, 3, 4)]
    verdicts = ex.judge_merges(proposals)

    notes = [[n, t, note_with_tail(n, t)] for n, t in NOTES]

    cfg = AppConfig()
    service = SimpleNamespace(config=cfg)
    compat = dream.OpenAICompatExtractor(
        "http://127.0.0.1:9/v1/", "judge-one", max_tokens=2048,
        timeout_seconds=240.0, extra_body={"cache_prompt": False})
    policy = judging_policy(service, compat, dream._JUDGE_SYSTEM_PROMPT)
    sample = {"b": [1, 2.0, None, 1e16, -0.0], "a": "é\x7f—\U0001F600",
              "c": {"z": True, "y": 0.1}}

    served = []
    for requested, got in SERVED:
        e = SimpleNamespace(model=requested, served_model=got)
        served.append([requested, got, DreamOps._served_model_mismatch(e)])

    TARGET.write_text(json.dumps({
        "formatted": formatted,
        "raw_verdicts": RAW_VERDICTS,
        "verdicts": verdicts,
        "notes": notes,
        "signed_deep_dream": signed_deep_dream(cfg.memory.deep_dream),
        "policy": policy,
        "policy_fingerprint": fingerprint(policy),
        "sample": sample,
        "sample_fingerprint": fingerprint(sample),
        "served": served,
    }, indent=1, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
