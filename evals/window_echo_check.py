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
