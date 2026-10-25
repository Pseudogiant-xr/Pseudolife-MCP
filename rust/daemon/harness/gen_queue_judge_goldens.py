"""Write the golden cases for the Rust review-queue judges
(``src/dream/queue_judges.rs``): for the junk judge, the prompt row
(``format_junk_proposal``), the reply validation (``judge_junk`` over
``_verdict_head`` / ``_text``) and the evidence pack
(``_enrich_junk_proposals_from``).

Every expected value comes from calling the Python code in this checkout:
``judge_junk`` runs on an ``OpenAICompatExtractor`` whose ``_judge_request``
returns the case's raw verdict list, and the pack builder runs unbound on a
stand-in service carrying only the config it reads.

Output: ``goldens/queue-judges.json``.

usage: python gen_queue_judge_goldens.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "queue-judges.json"
sys.path.insert(0, str(REPO))

from pseudolife_memory.memory import dream  # noqa: E402
from pseudolife_memory.service_dream import DreamOps  # noqa: E402

FORMAT_ROWS = [
    {"n": 1, "display": "a, b, c", "reason": "list-artifact", "degree": 0, "edges": [],
     "facts": 0, "fact_text": [], "lesson_object": False, "scopes": [], "mentions": []},
    {"n": 2, "display": "origin/master", "reason": "compound", "degree": 3,
     "edges": [f"x{i} -uses-> origin/master [agent]" + "y" * 120 for i in range(10)],
     "facts": 2, "fact_text": ["role=" + "z" * 150, "owner=me", "x=1", "y=2"],
     "lesson_object": True, "scopes": ["homelab", "pseudolife"],
     "mentions": ["[conversation] " + "m" * 300, "two", "three", "four"]},
    {"n": 3, "display": "élan — 🙂", "reason": None, "degree": 1, "edges": ["e"],
     "facts": 1, "fact_text": ["k=v"], "lesson_object": False, "scopes": ["Ünicode"],
     "mentions": []},
    {"n": 4},
]

PARSE_CASES = [
    [{"id": 1, "verdict": "delete", "confidence": 0.93, "note": "a list"},
     {"id": 2, "verdict": " KEEP ", "confidence": "0.81", "note": ""},
     {"id": 3, "verdict": "leave"},
     {"id": 9, "verdict": "delete", "confidence": 1},
     {"id": "2", "verdict": "accept", "confidence": 0.5},
     {"id": None, "verdict": "keep"},
     "not a dict",
     {"id": 1.9, "verdict": "keep", "confidence": 7, "note": 12345},
     {"id": True, "verdict": "delete", "confidence": [1], "note": ["x"] * 3},
     {"id": 3, "verdict": "keep", "confidence": -2, "note": "n" * 500}],
    [],
]


def judge_junk(rows, raw):
    ex = dream.OpenAICompatExtractor("http://stub/v1", "stub-model")
    ex._judge_request = lambda *a, **k: raw
    return ex.judge_junk(rows)


def pack(pending, evidence, cap=240):
    me = SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(
        deep_dream=SimpleNamespace(snippet_max_chars=cap))))
    return DreamOps._enrich_junk_proposals_from(me, pending, evidence)


EVIDENCE = {
    "graph": {
        "entities": [
            {"id": 1, "canonical": "a, b, c", "display": "a, b, c"},
            {"id": 2, "canonical": "origin/master", "display": "origin/master"},
            {"id": 3, "canonical": "bob", "display": "Bob"},
            {"id": 4, "canonical": "lesson object", "display": "Lesson Object"},
            {"id": 5, "canonical": "shell lesson", "display": "Shell Lesson"},
            {"id": 6, "canonical": "orphan", "display": "Orphan"},
        ],
        "edges": [
            {"src_id": 3, "relation": "uses", "dst_id": 2, "origin": "agent"},
            {"src_id": 2, "relation": "related-to", "dst_id": 2, "origin": None},
            {"src_id": 5, "relation": "prefers", "dst_id": 4, "origin": "action"},
            {"src_id": 5, "relation": "avoids", "dst_id": 4, "origin": "action"},
            {"src_id": 3, "relation": "prefers", "dst_id": 1, "origin": "agent"},
        ] + [{"src_id": 3, "relation": "uses", "dst_id": 2, "origin": ""} for _ in range(8)],
    },
    "scopes": {2: ["zeta", "alpha"], 4: ["pseudolife"]},
    "entries": [
        {"id": 1, "text": "Bob pushed origin/master today", "source": "conversation"},
        {"id": 2, "text": "origin master is behind", "source": ""},
        {"id": 3, "text": "The orphan " + "o" * 400, "source": None},
        {"id": 4, "text": "origin/master again", "source": "claude"},
        {"id": 5, "text": "and origin/master once more", "source": "x"},
    ],
    "facts": {2: 1, 3: 2},
    "fact_texts": {"Origin/Master": 2, "lesson object": 3, "": 4},
    "fact_rows": {2: [("role", "the main branch"), ("note", "n" * 200)], 6: []},
}
PENDING = [
    {"id": 10, "kind": "junk", "entity_id": 1, "reason": "list-artifact"},
    {"id": 11, "kind": "junk", "entity_id": 2, "reason": "compound"},
    {"id": 12, "kind": "junk", "entity_id": 4, "reason": "slot-key"},
    {"id": 13, "kind": "junk", "entity_id": 6, "reason": None},
    {"id": 14, "kind": "junk", "entity_id": 99, "entity": "Gone", "reason": "x"},
]


def main() -> int:
    rows = [{"n": i} for i in (1, 2, 3)]
    golden = {
        "format": [{"row": r, "text": dream.format_junk_proposal(r)} for r in FORMAT_ROWS],
        "parse": [{"raw": raw, "rows": rows, "verdicts": judge_junk(rows, raw)}
                  for raw in PARSE_CASES],
        "pack": [{"cap": cap, "pending": PENDING,
                  "evidence": {**EVIDENCE,
                               "scopes": {str(k): v for k, v in EVIDENCE["scopes"].items()},
                               "facts": {str(k): v for k, v in EVIDENCE["facts"].items()},
                               "fact_rows": {str(k): v for k, v in EVIDENCE["fact_rows"].items()}},
                  "rows": pack(PENDING, EVIDENCE, cap)}
                 for cap in (240, 12)],
    }
    TARGET.write_text(json.dumps(golden, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
