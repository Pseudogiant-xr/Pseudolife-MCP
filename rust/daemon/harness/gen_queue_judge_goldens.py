"""Write the golden cases for the Rust review-queue judges
(``src/dream/queue_judges.rs``): for the junk judge, the prompt row
(``format_junk_proposal``), the reply validation (``judge_junk`` over
``_verdict_head`` / ``_text``) and the evidence pack
(``_enrich_junk_proposals_from``); for the link judge, the same three
(``format_link_proposal``, ``judge_links``, ``_enrich_link_proposals_from``)
and the two-vote gate over recorded votes (``_link_two_vote_action``,
``_stored_second_distinct``).

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


LINK_FORMAT_ROWS = [
    {"n": 1, "src": "Bob", "relation": "uses", "dst": "origin/master",
     "src_scopes": ["homelab", "pseudolife"], "dst_scopes": [], "rationale": "co-mention",
     "src_edges": [f"uses>x{i}" + "y" * 90 for i in range(10)], "dst_edges": [],
     "co_mentions": ["Bob pushed origin/master " + "p" * 300, "two", "three", "four"],
     "src_mentions": [], "dst_mentions": []},
    {"n": 2, "src": "élan", "relation": "related-to", "dst": "🙂 Face",
     "src_scopes": None, "dst_scopes": ["Ünicode"], "rationale": None,
     "src_edges": ["a>b"], "dst_edges": ["c>related-to"], "co_mentions": [],
     "src_mentions": ["[conversation] " + "m" * 300, "two", "three"], "dst_mentions": []},
    {"n": 3, "src": None},
    {"n": 4},
]

LINK_PARSE_CASES = [
    [{"id": 1, "verdict": "accept", "confidence": 0.93, "note": "real"},
     {"id": 2, "verdict": " RETYPE ", "confidence": "0.81", "relation": "depends-on"},
     {"id": 3, "verdict": "retype", "confidence": 0.9, "relation": ""},
     {"id": 3, "verdict": "retype", "confidence": 0.9},
     {"id": 1, "verdict": "retype", "relation": "r" * 100, "note": "n" * 500},
     {"id": 2, "verdict": "reject", "confidence": 2, "relation": "ignored"},
     {"id": 2, "verdict": "leave", "confidence": None},
     {"id": 9, "verdict": "accept"},
     {"id": 1, "verdict": "keep", "confidence": 0.5},
     {"id": 1.7, "verdict": "retype", "relation": 42, "note": ["x"]},
     "not a dict"],
    [],
]


def judge_links(rows, raw):
    ex = dream.OpenAICompatExtractor("http://stub/v1", "stub-model")
    ex._judge_request = lambda *a, **k: raw
    return ex.judge_links(rows)


def link_pack(pending, evidence, cap=240):
    me = SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(
        deep_dream=SimpleNamespace(snippet_max_chars=cap))))
    return DreamOps._enrich_link_proposals_from(me, pending, evidence)


LINK_EVIDENCE = {
    "graph": {
        "entities": [
            {"id": 1, "display": "Bob"},
            {"id": 2, "display": "origin/master"},
            {"id": 3, "display": "Alice Smith"},
            {"id": 4, "display": "Lonely"},
            {"id": 5, "display": "!!!"},
        ],
        "edges": [
            {"src_id": 1, "relation": "uses", "dst_id": 2},
            {"src_id": 3, "relation": "knows", "dst_id": 1},
            {"src_id": 2, "relation": "related-to", "dst_id": 2},
            {"src_id": 1, "relation": "uses", "dst_id": 99},
        ] + [{"src_id": 3, "relation": f"r{i}", "dst_id": 2} for i in range(9)],
    },
    "scopes": {1: ["zeta", "Alpha", "beta"], 3: ["pseudolife"]},
    "entries": [
        {"id": 1, "text": "Bob pushed origin/master today", "source": "conversation"},
        {"id": 2, "text": "Alice Smith met Bob " + "q" * 300, "source": ""},
        {"id": 3, "text": "alice smith and bob again", "source": None},
        {"id": 4, "text": "Bob alone", "source": "claude"},
        {"id": 5, "text": "Bob once more", "source": "x"},
        {"id": 6, "text": "Bob a third time", "source": "y"},
        {"id": 7, "text": "Bob and Alice Smith, fourth", "source": "z"},
        {"id": 8, "text": "Lonely is lonely " + "l" * 300, "source": "w"},
    ],
}
LINK_PENDING = [
    {"id": 20, "src_id": 1, "relation": "uses", "dst_id": 2, "src": "Bob",
     "dst": "origin/master", "rationale": "co-mention", "source": "deep-dream"},
    {"id": 21, "src_id": 3, "relation": "knows", "dst_id": 1, "src": "Alice Smith",
     "dst": "Bob", "rationale": None, "source": "analyzer"},
    {"id": 22, "src_id": 4, "relation": "related-to", "dst_id": 1, "src": "Lonely",
     "dst": "Bob", "rationale": "x"},
    {"id": 23, "src_id": 5, "relation": "related-to", "dst_id": 4, "src": "!!!",
     "dst": "Lonely", "rationale": "y"},
]

TWO_VOTE_CASES = [
    # (row, two_vote, mode, min)
    ({"judge_verdict": "accept", "judge_confidence": 0.7,
      "judge2_verdict": "accept", "judge2_confidence": 0.7}, "auto", "auto", 0.7),
    ({"judge_verdict": "reject", "judge_confidence": 0.6,
      "judge2_verdict": "reject", "judge2_confidence": 0.79}, "auto", "auto", 0.7),
    ({"judge_verdict": "reject", "judge_confidence": 0.6,
      "judge2_verdict": "reject", "judge2_confidence": 0.79}, "auto", "auto", 0.71),
    ({"judge_verdict": "accept", "judge_confidence": 0.9,
      "judge2_verdict": "reject", "judge2_confidence": 0.9}, "auto", "auto", 0.7),
    ({"judge_verdict": "retype", "judge_confidence": 0.9,
      "judge2_verdict": "retype", "judge2_confidence": 0.9}, "auto", "auto", 0.7),
    ({"judge_verdict": "accept", "judge_confidence": 0.9,
      "judge2_verdict": "accept", "judge2_confidence": 0.9}, "shadow", "auto", 0.7),
    ({"judge_verdict": "accept", "judge_confidence": 0.9,
      "judge2_verdict": "accept", "judge2_confidence": 0.9}, "auto", "shadow", 0.7),
    ({"judge_verdict": "accept", "judge_confidence": None,
      "judge2_verdict": "accept", "judge2_confidence": 1.0}, "auto", "auto", 0.5),
    ({}, "auto", "auto", 0.7),
]


# (row, the memo's served model for the first verdict, configured2)
DISTINCT_CASES = [
    ({"judge_model": "judge-one", "judge2_model": "judge-two",
      "judge2_served_model": "judge-two"}, "judge-one", None),
    ({"judge_model": "judge-one", "judge2_model": "judge-two",
      "judge2_served_model": "judge-one"}, "judge-one", None),
    ({"judge_model": "judge-one", "judge2_model": "judge-two",
      "judge2_served_model": "judge-two"}, None, None),
    ({"judge_model": "judge-one", "judge2_model": "judge-two",
      "judge2_served_model": "judge-two"}, "", None),
    ({"judge_model": "judge-one", "judge2_model": "judge-two",
      "judge2_served_model": None}, "judge-one", None),
    ({"judge_model": "same", "judge2_model": "same",
      "judge2_served_model": "b"}, "a", None),
    ({"judge_model": "judge-one", "judge2_model": "judge-two",
      "judge2_served_model": "b"}, "a", "judge-one"),
    ({"judge_model": "judge", "judge2_model": "judge-two",
      "judge2_served_model": "b"}, "a", "judge"),
    ({"judge_model": "judge-one", "judge2_model": "judge-two",
      "judge2_served_model": "b"}, "a", "other"),
    ({"judge_model": None, "judge2_model": None,
      "judge2_served_model": "b"}, "a", None),
    ({"judge_model": None, "judge2_model": "",
      "judge2_served_model": "b"}, "a", None),
]


def distinct(row, first_served, configured2):
    review = SimpleNamespace(observed=lambda p: first_served)
    return DreamOps._stored_second_distinct(review, row, configured2)


def two_vote(row, tv, mode, floor):
    cfg = SimpleNamespace(link_judge_two_vote=tv, link_two_vote_min_confidence=floor)
    return DreamOps._link_two_vote_action(row, cfg, mode)


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
        "link_format": [{"row": r, "text": dream.format_link_proposal(r)}
                        for r in LINK_FORMAT_ROWS],
        "link_parse": [{"raw": raw, "rows": rows, "verdicts": judge_links(rows, raw)}
                       for raw in LINK_PARSE_CASES],
        "link_pack": [{"cap": cap, "pending": LINK_PENDING,
                       "evidence": {**LINK_EVIDENCE,
                                    "scopes": {str(k): v
                                               for k, v in LINK_EVIDENCE["scopes"].items()}},
                       "rows": link_pack(LINK_PENDING, LINK_EVIDENCE, cap)}
                      for cap in (240, 10)],
        "link_distinct": [{"row": r, "first_served": fs, "configured2": c2,
                           "distinct": distinct(r, fs, c2)}
                          for r, fs, c2 in DISTINCT_CASES],
        "link_two_vote": [{"row": r, "two_vote": tv, "mode": m, "min": f,
                           "action": two_vote(r, tv, m, f)}
                          for r, tv, m, f in TWO_VOTE_CASES],
    }
    TARGET.write_text(json.dumps(golden, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
