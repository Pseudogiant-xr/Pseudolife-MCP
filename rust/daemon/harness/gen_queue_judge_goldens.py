"""Write the golden cases for the Rust review-queue judges
(``src/dream/queue_judges.rs``): for the junk judge, the prompt row
(``format_junk_proposal``), the reply validation (``judge_junk`` over
``_verdict_head`` / ``_text``) and the evidence pack
(``_enrich_junk_proposals_from``); for the link judge, the same three
(``format_link_proposal``, ``judge_links``, ``_enrich_link_proposals_from``)
and the two-vote gate over recorded votes (``_link_two_vote_action``,
``_stored_second_distinct``); for the candidate judge, the prompt row
(``format_candidate``), the reply validation (``judge_candidates``) and the
scan generation and pair-local evidence fingerprints
(``candidate_generation``, ``candidate_evidence_fingerprints``) over one
synthetic bank snapshot.

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


CAND_FORMAT_ROWS = [
    {"n": 1, "src": "Alpha", "dst": "Beta", "similarity": 0.8123,
     "src_snippets": ["[conversation] " + "s" * 300, "two", "three", "four"],
     "dst_snippets": []},
    {"n": 2, "src": "élan", "dst": "🙂", "similarity": 1.0, "src_snippets": None,
     "dst_snippets": ["x"]},
    {"n": 3, "src": None, "similarity": None},
    {"n": 4},
]

CAND_PARSE_CASES = [
    [{"id": 1, "verdict": "propose", "confidence": 0.9, "relation": "uses",
      "rationale": "both run it"},
     {"id": 2, "verdict": " PROPOSE ", "confidence": "0.7", "relation": "depends-on",
      "src": "Gamma", "dst": "x" * 300},
     {"id": 3, "verdict": "propose", "confidence": 0.9, "relation": ""},
     {"id": 1, "verdict": "propose", "relation": "r" * 100, "rationale": "n" * 500},
     {"id": 2, "verdict": "dismiss", "confidence": 2, "relation": "ignored", "src": "s"},
     {"id": 3, "verdict": "leave", "confidence": None},
     {"id": 9, "verdict": "propose", "relation": "uses"},
     {"id": 1, "verdict": "accept"},
     {"id": 2.5, "verdict": "propose", "relation": 7, "src": 0, "dst": ""},
     "nope"],
    [],
]
CAND_ROWS = [{"n": 1, "src": "Alpha", "dst": "Beta"}, {"n": 2, "src": "Gamma", "dst": "Delta"},
             {"n": 3, "src": "Eps", "dst": "Zeta"}]


def judge_candidates(rows, raw):
    ex = dream.OpenAICompatExtractor("http://stub/v1", "stub-model")
    ex._judge_request = lambda *a, **k: raw
    return ex.judge_candidates(rows)


def _cand_inputs():
    import numpy as np
    emb = lambda xs: np.asarray(xs, dtype=np.float32)  # noqa: E731
    return {
        "graph": {
            "entities": [
                {"id": 1, "canonical": "alpha", "display": "Alpha", "etype": None,
                 "created_at": 1700000000.123456},
                {"id": 2, "canonical": "beta-node", "display": "Beta Node", "etype": "service",
                 "created_at": 1700000001.5},
                {"id": 10, "canonical": "élan", "display": "Élan", "etype": "", "created_at": 3.0},
                {"id": 11, "canonical": "orphan", "display": None, "created_at": 4.25,
                 "etype": None},
                # no tokens and no traces: the fallback scan names no note
                {"id": 12, "canonical": "bang", "display": "!!!", "etype": None,
                 "created_at": 6.0},
            ],
            "aliases": {10: ["elan"], 2: ["beta", "bn"]},
            "edges": [
                {"id": 7, "src_id": 1, "relation": "uses", "dst_id": 2, "confidence": 0.7,
                 "origin": "agent", "asserted_at": 1700000002.0},
                {"id": 3, "src_id": 10, "relation": "related-to", "dst_id": 1,
                 "confidence": 0.45, "origin": None, "asserted_at": None},
                {"id": 12, "src_id": 2, "relation": "related-to", "dst_id": 2,
                 "confidence": 1.0, "origin": "action", "asserted_at": 5.5},
            ],
        },
        "entries": [
            {"id": 1, "text": "Alpha uses Beta Node daily", "embedding": emb([0.1, -0.25, 3.0])},
            {"id": 2, "text": "Élan was here 🙂", "embedding": emb([0.5, 0.5, 0.5])},
            {"id": 5, "text": "alpha alone", "embedding": None},
            {"id": 9, "text": "beta node notes", "embedding": emb([1e-8, 2.5e10, -0.0])},
        ],
        "scopes": {10: ["proj-b", "Proj-A"], 2: ["zeta", "alpha"]},
        "traces": {"beta-node": [9, 1, 9], "orphan": [42]},
        "facts": {10: 2, 2: 1},
        "fact_texts": {"Alpha": 1, "None": 3, "2024": 2},
        "pending_entities": [
            {"id": 30, "kind": "merge", "entity_id": 1, "into_id": 10, "score": 0.91,
             "reason": "write-dedup", "status": "pending", "created_at": 9.0,
             "judge_verdict": "reject", "judge_confidence": 0.8, "judged_at": 10.0,
             "judge2_policy": "x", "entity": "Alpha", "into": "Élan"},
            {"id": 31, "kind": "junk", "entity_id": 11, "into_id": None, "score": None,
             "reason": "list-artifact", "status": "pending", "created_at": 11.0,
             "judge_verdict": None, "entity": None, "into": None},
        ],
        "lesson_refs": {2, 10, 3},
        "pending_links": [
            {"id": 40, "src_id": 2, "relation": "uses", "dst_id": 10, "confidence": 0.5,
             "similarity": None, "rationale": "co-mention", "source": "analyzer",
             "created_at": 12.0, "status": "pending", "judge_verdict": "accept",
             "judged_at": 13.0, "src": "Beta Node", "dst": "Élan"},
            {"id": 41, "src_id": 1, "relation": "related-to", "dst_id": 1, "confidence": 0.3,
             "similarity": 0.66, "rationale": None, "source": "deep-dream",
             "created_at": 14.0, "status": "pending", "src": "Alpha", "dst": "Alpha"},
        ],
        "dismissed": {("b", "c"), ("a", "z"), ("a", "b")},
    }


CAND_CANDIDATES = [
    {"src_id": 1, "dst_id": 10, "src": "Alpha", "dst": "Élan", "similarity": 0.8123,
     "src_snippets": ["a"], "dst_snippets": []},
    {"src": "beta node", "dst": "Ghost", "similarity": 0.6},
    {"src_id": 99, "dst_id": 11, "src": "Alpha", "dst": "Orphan"},
    {"src_id": 12, "dst_id": 1, "src": "!!!", "dst": "Alpha", "similarity": 0.55},
    {},
]


def _cand_service():
    from pseudolife_memory.utils.config import DeepDreamConfig
    return SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(
        deep_dream=DeepDreamConfig())))


def _jsonable_inputs(inputs):
    def emb(e):
        return None if e is None else [float(x) for x in e]
    g = inputs["graph"]
    return {
        "entities": g["entities"],
        "aliases": [[k, v] for k, v in g["aliases"].items()],
        "edges": g["edges"],
        "entries": [{"id": e["id"], "text": e["text"], "embedding": emb(e["embedding"])}
                    for e in inputs["entries"]],
        "scopes": [[k, v] for k, v in inputs["scopes"].items()],
        "traces": [[k, v] for k, v in inputs["traces"].items()],
        "facts": [[k, v] for k, v in inputs["facts"].items()],
        "fact_texts": [[k, v] for k, v in inputs["fact_texts"].items()],
        "pending_entities": inputs["pending_entities"],
        "lesson_refs": sorted(inputs["lesson_refs"], key=str),
        "pending_links": inputs["pending_links"],
        "dismissed": sorted(inputs["dismissed"]),
    }


def candidate_fingerprints():
    from pseudolife_memory.memory.review_decisions import candidate_evidence_fingerprints
    from pseudolife_memory.memory.review_judgments import candidate_generation
    inputs, svc = _cand_inputs(), _cand_service()
    return {"inputs": _jsonable_inputs(inputs), "candidates": CAND_CANDIDATES,
            "generation": candidate_generation(svc, inputs=inputs),
            "decision_generation": candidate_generation(svc, decision_inputs=False,
                                                        inputs=inputs),
            "evidence": candidate_evidence_fingerprints(svc, CAND_CANDIDATES, inputs=inputs)}


def _lesson(task, aspect, lesson, **kw):
    rec = {"task": task, "aspect": aspect, "lesson": lesson, "polarity": "+",
           "status": "current", "confidence": 0.9, "origin": "agent", "support": [],
           "provenance": ["p2", "p1"], "asserted_at": 10.0, "last_confirmed": 11.0,
           "version": 1, "about": None, "outcome": "success", "embedding": None}
    rec.update(kw)
    return rec


def _world(entity, attribute, value, **kw):
    rec = {"entity": entity, "attribute": attribute, "value": value, "polarity": "+",
           "status": "current", "confidence": 0.8, "origin": "agent",
           "support": ["source"], "provenance": [], "asserted_at": 20.0,
           "last_confirmed": 21.0, "version": 1, "source_url": "https://example.org/a",
           "source_quote": "q", "retrieved_at": 30.0, "freshness_class": "slow",
           "content_hash": "h", "source_doc_id": None, "embedding": None}
    rec.update(kw)
    return rec


def curation_cases():
    import numpy as np
    from pseudolife_memory import curation_safety as cs
    lesson_pairs = [
        (_lesson("Rotate Keys", "Cadence", "Rotate keys quarterly."),
         _lesson("Rotate Secrets", "Cadence", "  Rotate keys quarterly. ")),
        (_lesson("Rotate Keys", "Cadence", "Rotate keys quarterly.", about="ops"),
         _lesson("Rotate Secrets", "Cadence", "Rotate keys quarterly.")),
        (_lesson("Ship", "Freeze", "Freeze merges.", embedding=np.asarray([0.5, -1.0],
                                                                          dtype=np.float32)),
         _lesson("Ship Release", "Freeze", "Freeze all merges.")),
        (_lesson("Ünicode | pipe", "Asp", "x", polarity="-"),
         _lesson("Unicode", "Asp", "x")),
    ]
    world_pairs = [
        (_world("Host A", "os", "Debian 12"), _world("Host B", "os", "Debian 12")),
        (_world("Host A", "os", "Debian 12", retrieved_at=10.0),
         _world("Host B", "os", "Debian 12")),
        (_world("Host A", "os", "Debian 12", source_url="https://example.org/b"),
         _world("Host B", "os", "Debian 12")),
    ]
    verdicts = [
        {"verdict": "duplicate", "keep": "a", "fold": None},
        {"verdict": "duplicate", "keep": "b", "fold": ""},
        {"verdict": "duplicate", "keep": "a", "fold": "invented"},
        {"verdict": "duplicate", "keep": "c"},
        {"verdict": "distinct"},
        {"keep": "a"},
    ]
    out = []
    for store, pairs in (("lesson", lesson_pairs), ("world", world_pairs)):
        for a, b in pairs:
            ev = cs.capture_pair_evidence(store, a, b)
            policy = "p" * 64
            sig = cs._judgment_signature(ev, policy)
            pid = cs._pair_id(ev)
            states = []
            for judgment, binding, at in (
                    (None, None, 100.0),
                    ({"judged_at": 50.0}, None, 100.0),
                    ({"judged_at": 50.0}, sig, 100.0),
                    ({"judged_at": 50.0}, {"signature": sig, "action": "pending"}, 100.0),
                    ({"judged_at": 50.0}, {"signature": sig, "action": ""}, 100.0),
                    ({"judged_at": 50.0}, {"signature": "other"}, 100.0),
                    ({"judged_at": 50.0}, {"signature": sig}, 1000.0),
                    ({"judged_at": "x"}, {"signature": sig}, 100.0)):
                bindings = {} if binding is None else {pid: binding}
                states.append({
                    "judgment": judgment, "bindings": bindings, "at": at,
                    "state": cs.curation_judgment_state(judgment, bindings, ev, policy,
                                                        now=at, horizon=500.0),
                    "observed": cs.curation_bound_observed_model(bindings, ev),
                    "action": cs.curation_bound_action(bindings, ev)})
            out.append({
                "store": store, "a": ev.a, "b": ev.b,
                "a_fingerprint": ev.a_fingerprint, "b_fingerprint": ev.b_fingerprint,
                "pair_id": pid, "signature": sig, "policy": policy,
                "pair_keys": list(cs.curation_pair_keys(ev)),
                "folds": [{"verdict": v, "allowed": cs.can_auto_fold(ev, v).allowed,
                           "reason": cs.can_auto_fold(ev, v).reason} for v in verdicts],
                "states": states})
    return out


def curation_audit():
    import numpy as np
    from pseudolife_memory import curation_safety as cs
    out = []
    for store, a, b in (
            ("lesson", _lesson("Backup Drill", "Cadence", "Run the drill monthly."),
             _lesson("Restore Drill", "Cadence", "Run the drill monthly.")),
            ("world", _world("Host C", "os", "Alpine"), _world("Host D", "os", "Alpine"))):
        ev = cs.capture_pair_evidence(store, a, b)
        # A resident record, as the fold retires it: attributes, not a dict
        # (``_audit_snapshot`` reads the embedding with getattr).
        fields = dict(b, status="retired", superseded_at=1700000123.5,
                      embedding=np.asarray([0.1, -2.5, 3.0], dtype=np.float32))
        for alias, name in (("task", "entity"), ("aspect", "attribute"), ("lesson", "value")):
            if alias in fields:
                fields[name] = fields.pop(alias)
        retired = SimpleNamespace(**fields)
        out.append({"store": store, "a": ev.a, "b": ev.b,
                    "a_fingerprint": ev.a_fingerprint, "b_fingerprint": ev.b_fingerprint,
                    "retired_snapshot": cs.snapshot_record(store, retired),
                    "embedding": [float(x) for x in retired.embedding],
                    "audit": cs._audit_snapshot(store, retired, ev.b_fingerprint, ev, "a")})
    return out


def curation_policy():
    from pseudolife_memory import curation_safety as cs
    from pseudolife_memory.utils.config import MemoryConfig
    svc = SimpleNamespace(config=SimpleNamespace(memory=MemoryConfig()))
    ex = SimpleNamespace(model="judge-one", base_url="http://127.0.0.1:9/v1", max_tokens=4096,
                         timeout=240.0, extra_body={"z": 1, "a": [1, 2]}, judge_thinking=False)
    return [{"observed": o, "fingerprint": cs.curation_policy_fingerprint(svc, ex,
                                                                           observed_model=o)}
            for o in (None, "judge-one", "élan")]


SLOT_FORMAT_ROWS = [
    {"n": 1, "store": "lesson", "similarity": 0.91,
     "a": {"entity": "Rotate Keys", "attribute": "Cadence", "value": "v" * 300,
           "polarity": "+", "outcome": "success", "about": "ops"},
     "b": {"entity": "Rotate Secrets", "attribute": "Cadence", "value": "x",
           "polarity": "-", "outcome": None, "about": None}},
    {"n": 2, "store": "world", "similarity": 1.0,
     "a": {"entity": "Host A", "attribute": "os", "value": "Debian 12",
           "source_url": "https://example.org/a"},
     "b": {"entity": "Host B", "attribute": "os", "value": "Debian 12", "source_url": ""}},
    {"n": 3, "store": "world", "a": None},
]

SLOT_PARSE_CASES = [
    [{"id": 1, "verdict": "duplicate", "keep": " A ", "fold": "f" * 700, "confidence": 0.9},
     {"id": 2, "verdict": "duplicate", "keep": "c", "fold": "x"},
     {"id": 3, "verdict": "duplicate"},
     {"id": 1, "verdict": "distinct", "keep": "a", "fold": "ignored", "note": "n" * 500},
     {"id": 2, "verdict": "leave", "confidence": "0.4"},
     {"id": 3, "verdict": "duplicate", "keep": 0, "confidence": 1},
     {"id": 3, "verdict": "duplicate", "keep": "B", "note": ""},
     {"id": 9, "verdict": "distinct"},
     "x"],
    [],
]


def judge_slot_pairs(rows, raw):
    ex = dream.OpenAICompatExtractor("http://stub/v1", "stub-model")
    ex._judge_request = lambda *a, **k: raw
    return ex.judge_slot_pairs(rows)


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
        "curation_cases": curation_cases(),
        "curation_policy": curation_policy(),
        "curation_audit": curation_audit(),
        "slot_format": [{"row": r, "text": dream.format_slot_pair(r)} for r in SLOT_FORMAT_ROWS],
        "slot_parse": [{"raw": raw, "rows": rows, "verdicts": judge_slot_pairs(rows, raw)}
                       for raw in SLOT_PARSE_CASES],
        "candidate_format": [{"row": r, "text": dream.format_candidate(r)}
                             for r in CAND_FORMAT_ROWS],
        "candidate_parse": [{"raw": raw, "rows": CAND_ROWS,
                             "verdicts": judge_candidates(CAND_ROWS, raw)}
                            for raw in CAND_PARSE_CASES],
        "candidate_fingerprints": candidate_fingerprints(),
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
