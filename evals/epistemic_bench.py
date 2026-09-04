"""Epistemic bench — score the SERVED CONTEXT, not a model answer.

Design + preregistration:
``docs/superpowers/specs/2026-09-05-epistemic-bench-design.md``.

Every published retrieval number this project has asks one question — did
the served context contain the gold string — and on that question the fact
spine ties naive RAG (LongMemEval-500 rag 0.690 vs cascade 0.692; BEAM-100K
rag 0.6425 vs hybrid 0.6226). This bench asks the other question: does the
served context tell the agent WHICH value is current, how old it is,
whether it was retracted, and when nothing is known at all.

Judge-free and CPU-only by construction. Each metric is a deterministic
predicate over one arm's served context — word-boundary string containment
on the served text, or a structural read of the served payload (the entry /
fact dicts the serving call returned). No answerer, no judge, no GPU, so a
rerun is byte-reproducible and costs seconds.

Five dimensions (see the spec for the full definitions and the
preregistered expectations):

  D1 ``update_following``   the slot's current value is served
  D2 ``stale_serving``      a superseded value is served and the current
                            value is not (a DEFECT count: lower is better)
  D3 ``staleness_marking``  a slot past 2xTTL is served carrying the stale
                            signal
  D4 ``abstention_support`` a never-stated slot surfaces no value —
                            reported beside ``answer_coverage``, which the
                            no-memory arm trivially fails
  D5 ``retraction_handling``a corrected value is served together with its
                            correction signal (the supersession chain, or a
                            served turn's ``superseded_by_text``)

Arms are IMPORTED, never re-implemented: ``longmemeval_bench.build_contexts``
and ``serve_comparator_arms`` build every served context, so an arm here and
an arm of the same name in the LongMemEval harness are the same object. The
``cascade`` arm is a CONTEXT-level proxy (cortex when non-empty, else rag)
and is not comparable to the judged answer-level cascade — every artifact
says so in ``caveats``.

Ground truth comes from two sources:

  synthetic  a seeded generator; facts are written straight through
             ``cortex_write`` with the session's timestamp, so no extractor
             runs. Extraction is held at PERFECT, which makes the synthetic
             cortex arm a ceiling on the deployed system, not a measurement
             of it. Stamped in every artifact's ``caveats``.
  lme        the LongMemEval knowledge-update slice, derived by pure
             parsing (23 of 78 questions qualify). Its bank is built
             per question by the real ``ingest_and_dream`` path, so the
             cortex/hybrid arms measure the DEPLOYED pipeline — retrieval
             and extraction together — and need an extractor endpoint
             (``--extractor``, default ``qwen-27b``; ``--extractor floor``
             is the no-LLM regex rung for CPU plumbing checks). The slice
             grades D1, D2 and D5; D3 and D4 report ``n: 0`` because the
             dataset carries no freshness class and no never-stated
             question. The run is resumable per question: rows are
             appended as each finishes and a restart skips them, because
             this source shares the GPU.

Usage (repo root):

  PYTHONPATH=. python evals/epistemic_bench.py --source synthetic \\
      --tag smoke-20260905 --contexts-only
  PYTHONPATH=. python evals/epistemic_bench.py --derive-lme oracle \\
      --tag lme-derivation-20260905
  PYTHONPATH=. python evals/epistemic_bench.py --source lme \\
      --contexts-only --extractor qwen-27b --tag lme-qwen27b-20260905

Isolation: a private bench database ``pseudolife_memory_bench_<pid>``,
created at start and dropped at exit. No other database is touched, and the
live daemon is never contacted.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DATA_DIR = Path(__file__).resolve().parent / "data"

DIMENSIONS = ("update_following", "stale_serving", "staleness_marking",
              "abstention_support", "retraction_handling")
# stale_serving is the only defect count in the table. A flipped direction
# would invert the verdict silently, so it is data, not prose.
HIGHER_IS_BETTER = {"update_following": True, "stale_serving": False,
                    "staleness_marking": True, "abstention_support": True,
                    "retraction_handling": True}
COMPANION = "answer_coverage"
ARMS = ("rag", "cortex", "hybrid", "cascade", "nomem")

# Mirrors pseudolife_memory/service.py's serving-side staleness policy
# strings. The predicate below matches on SHAPE as well as on these, so a
# wording change there degrades the metric's precision rather than silently
# zeroing it.
STALE_WARNING = "stale — re-verify before relying on this value"
STALE_QUARANTINE_WRAPPER = "(stale — re-verify; last known value below)"
# pseudolife_memory/memory/freshness.py: _TTL["volatile"]. A fact is flagged
# stale past 2xTTL, which is what the generator's stale slots must clear.
VOLATILE_TTL_SECONDS = 21 * 86400

DAY = 86400.0


# ─────────────────────────────────────────────────────────────────────────
# Value matching
# ─────────────────────────────────────────────────────────────────────────
def value_present(text: str, value: str) -> bool:
    """Word-boundary containment, so a short value ('4') does not match
    inside a longer one ('24') or inside a decimal ('1.5').

    Deliberately close to ``ladder_sweep.value_present``, with ONE
    deliberate divergence: that matcher excludes any adjacent ``.`` at all,
    which also rejects a value a sentence ends on ("the engine is
    ENG-2200."). Measured 2026-09-05 on the first synthetic smoke: it
    scored the rag arm at 0.150 coverage on contexts that plainly carried
    the value, because the generator's turns end sentences on values. Here
    a period only blocks a match when it continues a number
    (``1.5`` searched for ``1``), which is the case the original exclusion
    existed for. Kept local rather than imported because ``ladder_sweep``
    pulls in the bench stack (torch) and this module must stay importable
    on a bare CPU box.
    """
    if not text or not value:
        return False
    pattern = (r"(?<!\w)(?<!\d\.)" + re.escape(str(value))
               + r"(?!\w)(?!\.\d)")
    return re.search(pattern, str(text), re.IGNORECASE) is not None


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().casefold()


# ─────────────────────────────────────────────────────────────────────────
# Ground-truth and served-context types
# ─────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Question:
    """One bench question plus the epistemic state its answer depends on.

    ``kind`` is what the slot did over the timeline — ``update`` (the value
    changed), ``stable`` (stated once), ``stale`` (volatile and past 2xTTL),
    ``correction`` (explicitly retracted), ``unstated`` (never written).
    Which dimensions apply follows from these fields, never from ``kind``
    alone, so an LME-derived question scores the shared dimensions without
    pretending to carry the ones its dataset cannot support.
    """

    question_id: str
    kind: str
    entity: str
    attribute: str
    question: str
    current_value: str | None
    superseded_values: tuple[str, ...]
    corrected_from: str | None
    stale_slot: bool
    decoy_values: tuple[str, ...]


@dataclass(frozen=True)
class Served:
    """What one arm served for one question.

    ``text`` is the context string an answerer would see. ``facts`` and
    ``entries`` are the structured payloads the same serving call returned
    — the cortex fact dicts and the band entry dicts. Both are scored:
    a marker an agent can read off the MCP payload counts, and the metrics
    say which channel carried it.
    """

    text: str = ""
    facts: tuple[dict, ...] = ()
    entries: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Turn:
    epoch: float
    text: str


@dataclass(frozen=True)
class FactWrite:
    epoch: float
    entity: str
    attribute: str
    value: str
    freshness_class: str


@dataclass(frozen=True)
class Corpus:
    questions: tuple[Question, ...]
    turns: tuple[Turn, ...]
    fact_writes: tuple[FactWrite, ...]
    meta: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────
# The metrics. Each returns True / False / None (not applicable).
# ─────────────────────────────────────────────────────────────────────────
def _facts_for_slot(q: Question, served: Served) -> list[dict]:
    return [f for f in served.facts
            if _norm(f.get("entity")) == _norm(q.entity)
            and _norm(f.get("attribute")) == _norm(q.attribute)]


def update_following(q: Question, served: Served) -> bool | None:
    """D1 — the current value of a CHANGED slot is in the served text.

    Restricted to slots whose value actually moved: on a slot that never
    changed, "the current value is served" is plain retrieval, which the
    existing benchmarks already measure. ``answer_coverage`` carries the
    unrestricted version.
    """
    if q.kind not in ("update", "correction") or not q.current_value:
        return None
    return value_present(served.text, q.current_value)


def stale_serving(q: Question, served: Served) -> bool | None:
    """D2 — DEFECT: a superseded value is served and the current one is not.

    The failure the retrieval benchmarks cannot see. True means the arm put
    a value in front of the agent that is no longer true, with nothing in
    the context contradicting it. Serving both values is NOT a defect here:
    an agent can adjudicate two values, it cannot adjudicate one.
    """
    if not q.superseded_values:
        return None
    if q.current_value and value_present(served.text, q.current_value):
        return False
    return any(value_present(served.text, v) for v in q.superseded_values)


def staleness_marking(q: Question, served: Served) -> bool | None:
    """D3 — a slot past 2xTTL is served carrying the stale signal.

    Accepts all three shapes ``stale_policy`` can produce: the ``annotate``
    default leaves ``stale: true`` on the record, ``demote`` adds the
    warning, ``quarantine`` replaces the value and parks the original in
    ``last_known_value``. Scored on the PAYLOAD: the flattened context
    string carries no freshness annotation on any arm, which is itself
    worth reporting rather than hiding behind a zero.
    """
    if not q.stale_slot:
        return None
    for f in _facts_for_slot(q, served):
        if f.get("stale") is True:
            return True
        if f.get("warning") == STALE_WARNING or "last_known_value" in f:
            return True
        if f.get("value") == STALE_QUARANTINE_WRAPPER:
            return True
    return False


def abstention_support(q: Question, served: Served) -> bool | None:
    """D4 — a never-stated slot surfaces no value the agent could commit to.

    Two ways to fail, and the second is the interesting one: serving a fact
    for the slot (impossible — nothing was ever written) or serving another
    entity's value for the SAME attribute, which cosine retrieval is happy
    to do. The no-memory arm passes trivially, which is why this is only
    ever read beside ``answer_coverage``.
    """
    if q.kind != "unstated":
        return None
    if _facts_for_slot(q, served):
        return False
    return not any(value_present(served.text, d) for d in q.decoy_values)


def retraction_handling(q: Question, served: Served) -> bool | None:
    """D5 — a corrected value is served WITH its correction signal.

    Two channels, one per representation, so the arms are not scored on a
    mechanism only one of them has:

    * the fact spine — a served fact for the slot whose ``supersedes_value``
      is the retracted value (rendered in the context as the
      ``earlier values, oldest first:`` chain);
    * the associative store — a served entry that states the retracted
      value and carries a ``superseded_by_text`` naming the correction,
      which contradiction detection stamps at ``store()`` time.

    Serving the correction without the signal is not enough: the agent has
    to be able to tell which of two values won.
    """
    if not q.corrected_from or not q.current_value:
        return None
    if not value_present(served.text, q.current_value):
        return False
    for f in _facts_for_slot(q, served):
        if value_present(str(f.get("supersedes_value") or ""),
                         q.corrected_from):
            return True
    for e in served.entries:
        by = e.get("superseded_by_text") or ""
        if (value_present(e.get("text") or "", q.corrected_from)
                and value_present(by, q.current_value)):
            return True
    return False


def answer_coverage(q: Question, served: Served) -> bool | None:
    """Companion to D4 — over EVERY answerable question, is the current
    value served at all? Never reported alone: D4 and this one only mean
    something as a pair (the no-memory arm scores 1.000 and 0.000)."""
    if q.kind == "unstated" or not q.current_value:
        return None
    return value_present(served.text, q.current_value)


METRICS = {"update_following": update_following,
           "stale_serving": stale_serving,
           "staleness_marking": staleness_marking,
           "abstention_support": abstention_support,
           "retraction_handling": retraction_handling}
ALL_METRICS = dict(METRICS, **{COMPANION: answer_coverage})


def score_arm(questions, serve) -> dict:
    """Score one arm over the question set.

    ``serve`` maps a question to its ``Served``. Counts travel with every
    rate: a dimension no question in the set scores reports ``n: 0`` and a
    NULL rate, never a 0.0 that a reader would take for a failing arm.
    """
    out = {name: {"n": 0, "hits": 0, "rate": None}
           for name in ALL_METRICS}
    for q in questions:
        served = serve(q)
        for name, fn in ALL_METRICS.items():
            verdict = fn(q, served)
            if verdict is None:
                continue
            out[name]["n"] += 1
            out[name]["hits"] += int(bool(verdict))
    for cell in out.values():
        if cell["n"]:
            cell["rate"] = round(cell["hits"] / cell["n"], 4)
    return out


def score_from_rows(rows, arm: str) -> dict:
    """Score one arm from PERSISTED rows rather than live ``Served``
    objects.

    The resumable LongMemEval path has to total questions scored by an
    earlier process, whose per-question banks are long gone. Same shape as
    ``score_arm``, plus the ``context_chars_mean`` column that
    ``abstention_support`` must not be read without (spec amendment A3).
    A missing key and a JSON ``null`` both mean not-applicable.
    """
    out = {name: {"n": 0, "hits": 0, "rate": None} for name in ALL_METRICS}
    for row in rows:
        for name in ALL_METRICS:
            verdict = row.get(f"{arm}_{name}")
            if verdict is None:
                continue
            out[name]["n"] += 1
            out[name]["hits"] += int(bool(verdict))
    for cell in out.values():
        if cell["n"]:
            cell["rate"] = round(cell["hits"] / cell["n"], 4)
    out["context_chars_mean"] = round(
        sum(int(r.get(f"{arm}_context_chars") or 0) for r in rows)
        / max(1, len(rows)), 1)
    return out


def ground_truth_row(q: Question) -> dict:
    """The question's epistemic state as artifact fields — the half of a
    row a reader needs to re-derive any verdict by hand."""
    return {"question_id": q.question_id, "kind": q.kind,
            "entity": q.entity, "attribute": q.attribute,
            "question": q.question,
            "current_value": q.current_value,
            "superseded_values": list(q.superseded_values),
            "corrected_from": q.corrected_from,
            "stale_slot": q.stale_slot,
            "decoy_values": list(q.decoy_values)}


def score_row(q: Question, served_by_arm: dict, base: dict) -> dict:
    """One artifact row: the ground truth, then per arm the served
    context, its size, and every metric verdict.

    The served context is persisted, not only its length: a metric bug is
    auditable from the artifact only if the artifact carries the text the
    metric ran on (spec amendment A4).
    """
    row = dict(base)
    for arm in ARMS:
        served = served_by_arm[arm]
        row[f"{arm}_context"] = served.text
        row[f"{arm}_context_chars"] = len(served.text)
        for name, fn in ALL_METRICS.items():
            row[f"{arm}_{name}"] = fn(q, served)
    return row


# ─────────────────────────────────────────────────────────────────────────
# Source (a): the seeded synthetic generator
# ─────────────────────────────────────────────────────────────────────────
KINDS = ("update", "stable", "stale", "correction", "unstated")
_ENTITY_POOL = ("ledger-db", "ledger-cache", "router-a", "router-b",
                "payments-api", "billing-worker", "search-index",
                "audit-log")
_ATTRIBUTE_POOL = ("engine", "port", "owner", "region", "tier", "quota")
_VALUE_TAG = {"engine": "ENG", "port": "PRT", "owner": "OWN",
              "region": "RGN", "tier": "TIR", "quota": "QTA"}
# Phrasing varies so a containment metric is not quietly measuring one
# template (spec section 7, confound 2). Values stay opaque tokens that no
# arm can derive from the question.
_SAY = ("[{date}] user: the {attribute} for {entity} is {value}.",
        "[{date}] user: we set {entity} {attribute} to {value}.",
        "[{date}] user: {entity} is running {attribute} {value} now.")
_CORRECT = ("[{date}] user: correction — {entity} {attribute} is not "
            "{old}, it is {value}.",
            "[{date}] user: I was wrong about {entity} {attribute}; "
            "it is not {old}, it is {value}.")
_FILLER = ("[{date}] user: routine check on {entity}, nothing to report.",
           "[{date}] user: reviewed the {entity} runbook again today.",
           "[{date}] assistant: noted, I will keep an eye on {entity}.")
SESSION_SPACING_DAYS = 30
ANCHOR_LAG_DAYS = 7


def _mint(rng: random.Random, tag: str, used: set) -> str:
    while True:
        v = f"{tag}-{rng.randint(1000, 9999)}"
        if v not in used:
            used.add(v)
            return v


def generate(seed: int, entities: int = 6, attributes: int = 4,
             sessions: int = 4, now: float | None = None,
             filler_per_session: int = 3) -> Corpus:
    """Build the seeded synthetic corpus.

    Content is a pure function of ``seed`` and the sizes; only the absolute
    timestamps depend on ``now`` (staleness is measured against the wall
    clock, so the timeline has to be anchored in the real past — spec
    section 7, confound 4). ``now`` is recorded as ``anchor_epoch`` in the
    corpus meta and in every artifact.
    """
    if entities < 3:
        raise ValueError("need at least 3 entities so an unstated slot has "
                         "real near-miss values to resist")
    if sessions < 2:
        raise ValueError("need at least 2 sessions for a value to change")
    now = float(now if now is not None else time.time())
    rng = random.Random(seed)
    ents = [_ENTITY_POOL[i] if i < len(_ENTITY_POOL) else f"node-{i:02d}"
            for i in range(entities)]
    attrs = [_ATTRIBUTE_POOL[i] if i < len(_ATTRIBUTE_POOL)
             else f"field{i:02d}" for i in range(attributes)]
    # Session 0 is the oldest. The last session sits ANCHOR_LAG_DAYS before
    # ``now``, so a volatile slot asserted only at session 0 clears 2xTTL
    # whenever the timeline is long enough — asserted here, checked by
    # tests/test_epistemic_bench.py rather than assumed.
    epochs = [now - (ANCHOR_LAG_DAYS
                     + (sessions - 1 - s) * SESSION_SPACING_DAYS) * DAY
              for s in range(sessions)]
    if now - epochs[0] <= 2 * VOLATILE_TTL_SECONDS:
        raise ValueError("timeline too short for a stale slot: widen "
                         "--sessions or SESSION_SPACING_DAYS")

    slots = [(e, a) for e in ents for a in attrs]
    rng.shuffle(slots)
    kind_of = {slot: KINDS[i % len(KINDS)] for i, slot in enumerate(slots)}
    # Every attribute needs at least two stated entities, or an unstated
    # slot on it would have no near-miss value to resist and D4 would be
    # trivially passed by every arm.
    for a in attrs:
        stated = [e for e in ents if kind_of[(e, a)] != "unstated"]
        for e in ents:
            if len(stated) >= 2:
                break
            if kind_of[(e, a)] == "unstated":
                kind_of[(e, a)] = "stable"
                stated.append(e)

    used: set[str] = set()
    writes: list[FactWrite] = []
    turns: list[tuple[float, int, str]] = []
    seq = 0
    current: dict[tuple[str, str], str] = {}
    history: dict[tuple[str, str], list[str]] = {}

    def _say(epoch, entity, attribute, value, old=None):
        nonlocal seq
        date = time.strftime("%Y-%m-%d", time.gmtime(epoch))
        tmpl = (rng.choice(_CORRECT) if old is not None
                else rng.choice(_SAY))
        turns.append((epoch, seq, tmpl.format(
            date=date, entity=entity, attribute=attribute, value=value,
            old=old)))
        seq += 1

    for slot in sorted(slots):
        entity, attribute = slot
        kind = kind_of[slot]
        if kind == "unstated":
            continue
        tag = _VALUE_TAG.get(attribute, "VAL")
        if kind == "stale":
            # Volatile and asserted only in the oldest session.
            v = _mint(rng, tag, used)
            writes.append(FactWrite(epochs[0], entity, attribute, v,
                                    "volatile"))
            _say(epochs[0], entity, attribute, v)
            current[slot], history[slot] = v, []
        elif kind == "stable":
            s = rng.randrange(0, max(1, sessions - 1))
            v = _mint(rng, tag, used)
            writes.append(FactWrite(epochs[s], entity, attribute, v,
                                    "evergreen"))
            _say(epochs[s], entity, attribute, v)
            current[slot], history[slot] = v, []
        else:                                   # update / correction
            first, second = sorted(rng.sample(range(sessions), 2))
            old = _mint(rng, tag, used)
            new = _mint(rng, tag, used)
            writes.append(FactWrite(epochs[first], entity, attribute, old,
                                    "evergreen"))
            _say(epochs[first], entity, attribute, old)
            writes.append(FactWrite(epochs[second], entity, attribute, new,
                                    "evergreen"))
            _say(epochs[second], entity, attribute, new,
                 old=old if kind == "correction" else None)
            current[slot], history[slot] = new, [old]

    for s, epoch in enumerate(epochs):
        for _ in range(filler_per_session):
            subject = rng.choice(ents)
            date = time.strftime("%Y-%m-%d", time.gmtime(epoch))
            turns.append((epoch, seq, rng.choice(_FILLER).format(
                date=date, entity=subject)))
            seq += 1

    questions: list[Question] = []
    for slot in sorted(slots):
        entity, attribute = slot
        kind = kind_of[slot]
        decoys = tuple(sorted(
            v for (e2, a2), v in current.items()
            if a2 == attribute and e2 != entity))
        questions.append(Question(
            question_id=f"{entity}:{attribute}",
            kind=kind, entity=entity, attribute=attribute,
            question=f"What is the {attribute} for {entity}?",
            current_value=current.get(slot),
            superseded_values=tuple(history.get(slot, ())),
            corrected_from=(history[slot][0] if kind == "correction"
                            else None),
            stale_slot=(kind == "stale"),
            decoy_values=decoys if kind == "unstated" else ()))

    writes.sort(key=lambda w: (w.epoch, w.entity, w.attribute, w.value))
    turns.sort(key=lambda t: (t[0], t[1]))
    counts: dict[str, int] = {}
    for q in questions:
        counts[q.kind] = counts.get(q.kind, 0) + 1
    return Corpus(
        questions=tuple(questions),
        turns=tuple(Turn(epoch, text) for epoch, _, text in turns),
        fact_writes=tuple(writes),
        meta={"seed": seed, "entities": entities, "attributes": attributes,
              "sessions": sessions, "filler_per_session": filler_per_session,
              "anchor_epoch": now, "questions_by_kind": counts,
              "turns": len(turns), "fact_writes": len(writes)})


# ─────────────────────────────────────────────────────────────────────────
# Source (b): the LongMemEval knowledge-update derivation
# ─────────────────────────────────────────────────────────────────────────
# Value families, in priority order. A gold answer's leading token under
# the first family that matches decides which tokens in the earlier
# evidence are candidates for the old value — comparing a time against a
# bare number would pair values that were never the same fact.
_FAMILIES = (("time", re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")),
             ("money", re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")),
             ("percent", re.compile(r"\b\d+(?:\.\d+)?\s?%")),
             ("number", re.compile(r"\b\d+(?:\.\d+)?\b")))


def _parse_lme_date(raw: str) -> datetime:
    """``"2023/04/10 (Mon) 02:03"`` — the same shape
    ``longmemeval_bench._parse_date`` reads, kept local so this module
    stays importable without the bench stack."""
    cleaned = re.sub(r"\s*\(\w+\)\s*", " ", raw or "").strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return datetime.min


def _value_core(text) -> tuple[str | None, str | None]:
    for name, rx in _FAMILIES:
        m = rx.search(str(text or ""))
        if m:
            return name, m.group(0).strip()
    return None, None


def _evidence_text(session) -> str:
    return " ".join(str(t.get("content") or "") for t in session
                    if t.get("has_answer"))


def derive_lme_pairs(questions) -> tuple[list[dict], dict[str, int]]:
    """Derive (old value, new value) pairs from LongMemEval questions.

    Pure parsing, no model and no judgement: a question that does not
    derive cleanly is skipped, never guessed at. Returns the qualifying
    pairs and a histogram of skip reasons — the histogram is the honest
    half of the result and is committed with the pairs.
    """
    pairs: list[dict] = []
    skips: dict[str, int] = {}

    def _skip(reason):
        skips[reason] = skips.get(reason, 0) + 1

    for q in questions:
        if q.get("question_type") != "knowledge-update":
            continue
        ordered = sorted(zip(q["haystack_dates"], q["haystack_session_ids"],
                             q["haystack_sessions"]),
                         key=lambda p: _parse_lme_date(p[0]))
        answer_ids = set(q["answer_session_ids"])
        evidence = [t for t in ordered if t[1] in answer_ids]
        if len(evidence) != 2:
            _skip("not-two-evidence-sessions")
            continue
        family, gold = _value_core(q["answer"])
        if not gold:
            _skip("gold-has-no-value-token")
            continue
        early = _evidence_text(evidence[0][2])
        late = _evidence_text(evidence[1][2])
        if not value_present(late, gold):
            _skip("gold-not-in-later-evidence")
            continue
        if value_present(early, gold):
            _skip("gold-also-in-earlier-evidence")
            continue
        rx = dict(_FAMILIES)[family]
        candidates = sorted({m.group(0).strip() for m in rx.finditer(early)}
                            - {gold})
        candidates = [c for c in candidates if not value_present(late, c)]
        if len(candidates) != 1:
            _skip(f"ambiguous-old-value({len(candidates)})")
            continue
        pairs.append({"question_id": q["question_id"], "family": family,
                      "old_value": candidates[0], "new_value": gold,
                      "old_date": evidence[0][0], "new_date": evidence[1][0],
                      "question": q["question"]})
    return pairs, skips


def lme_question(pair: dict) -> Question:
    """One derived pair as a bench question.

    Scores D1, D2 and D5 only. The slot is synthetic — LongMemEval has no
    entity/attribute structure — so it never matches a served fact by name;
    D5 therefore reads the served TEXT and the entry channel, which is what
    the dataset can actually support. D3 and D4 report not-applicable
    because the dataset carries no freshness class and no never-stated
    questions (spec section 4).

    ``corrected_from`` is set to the old value: in a knowledge-update
    question the later statement IS the retraction, even though it is not
    phrased as one. That difference from an explicit synthetic correction
    is real and is why the two sources are reported separately.
    """
    return Question(
        question_id=pair["question_id"], kind="correction",
        entity=f"lme:{pair['question_id']}", attribute="value",
        question=pair["question"], current_value=pair["new_value"],
        superseded_values=(pair["old_value"],),
        corrected_from=pair["old_value"], stale_slot=False, decoy_values=())


# ─────────────────────────────────────────────────────────────────────────
# LongMemEval source: extractor rungs, resume state, artifact naming
# ─────────────────────────────────────────────────────────────────────────
# The no-LLM rung, named the way ``ladder_sweep`` names it. It needs no
# endpoint and no GPU, which is what makes a CPU plumbing check of the
# whole per-question bank lifecycle possible without booking the GPU.
FLOOR_EXTRACTOR = "floor"


def _probe(url: str) -> bool:
    """Is an OpenAI-compatible endpoint answering? Indirected through a
    module-level name so the abort path is testable without a server."""
    import longmemeval_bench as lmb

    return lmb.probe(url)


def extractor_url(name: str) -> str | None:
    """The endpoint for an ``--extractor`` rung; ``None`` for the floor.

    The map is ``longmemeval_bench.EXTRACTORS`` — imported, never copied,
    so a rung added there is runnable here without a second edit.
    """
    if name == FLOOR_EXTRACTOR:
        return None
    import longmemeval_bench as lmb

    if name not in lmb.EXTRACTORS:
        raise SystemExit(f"unknown --extractor {name!r}. Known rungs: "
                         + ", ".join(sorted(lmb.EXTRACTORS)
                                     + [FLOOR_EXTRACTOR]))
    return lmb.EXTRACTORS[name]


def require_extractor(name: str) -> str | None:
    """Resolve the rung and prove it answers BEFORE anything is ingested.

    A run that dies on question 1 after paying a bank build has written no
    row to resume from, and this one shares the GPU with judged work: it
    has to fail at the door or not at all.
    """
    url = extractor_url(name)
    if url is None:
        return None
    if not _probe(url):
        raise SystemExit(
            f"no extractor server at {url} for --extractor {name} — start "
            "it first (dot-source evals/qwen_server.ps1 and call "
            f"Start-Qwen), or run --extractor {FLOOR_EXTRACTOR} for a "
            "CPU-only plumbing check.")
    return url


def make_extractor(name: str, url: str | None):
    """The extractor object — the same two the other harnesses build:
    ``longmemeval_bench._make_extractor`` for an endpoint rung, the
    ``RegexExtractor`` floor for the no-LLM one."""
    if url is None:
        from pseudolife_memory.memory.dream import RegexExtractor

        return RegexExtractor()
    import longmemeval_bench as lmb

    return lmb._make_extractor(url, None)


def load_rows(path) -> list[dict]:
    """Rows written so far. A truncated final line is dropped rather than
    crashing the resume: a run killed mid-write must still be resumable."""
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def done_question_ids(rows_path) -> set[str]:
    return {r["question_id"] for r in load_rows(rows_path)
            if r.get("question_id")}


def lme_pending(pairs, done, limit: int | None = None) -> list[dict]:
    """The questions still to run.

    ``--limit`` counts questions in the SLICE, not questions still
    pending, so a resumed run covers the same slice as the interrupted one
    instead of walking further down the derivation on each restart.
    """
    selected = list(pairs)[:limit] if limit else list(pairs)
    return [p for p in selected if p["question_id"] not in done]


def lme_out_path(tag: str) -> Path:
    """``epistemic-bench-lme-<tag>.json`` — without stuttering when the tag
    already names the source, which the derivation artifacts do."""
    stem = tag if tag.startswith("lme-") else f"lme-{tag}"
    return RESULTS_DIR / f"epistemic-bench-{stem}.json"


def guard_lme_artifact(out, force: bool) -> None:
    """Refuse to overwrite a FINISHED run; treat a rows file as a resume.

    Deliberately weaker than ``write_artifact``'s guard, which also blocks
    on an orphaned rows file. This run shares the GPU and is EXPECTED to be
    interrupted, so its rows file is the resume point, not the wreckage of
    a half-written run. The summary JSON is written only once every
    selected question has been scored, so its presence still means "this
    tag is done".
    """
    out = Path(out)
    if out.exists() and not force:
        raise SystemExit(
            f"refusing to overwrite a finished run at {out}; tag the run "
            "differently or pass --force. (The sibling .jsonl alone is a "
            "resume point, not a finished run, and does not block.)")


# ─────────────────────────────────────────────────────────────────────────
# Serving
# ─────────────────────────────────────────────────────────────────────────
def serve_arms(svc, question: str) -> dict[str, Served]:
    """Every arm's served context for one question.

    Texts come from ``longmemeval_bench.build_contexts`` and
    ``serve_comparator_arms`` — imported, never re-implemented, so an arm
    here is the same object an arm of that name is there. The structured
    payloads come from the SAME service calls ``build_contexts`` makes,
    pinned to its own module constants, so a width change there reaches
    both channels together.
    """
    import longmemeval_bench as lmb

    contexts = lmb.build_contexts(svc, question)
    lmb.serve_comparator_arms(contexts, question, nomem=True)
    facts = tuple(svc.cortex_search(question, top_k=lmb.CORTEX_TOP_K,
                                    min_score=lmb.CORTEX_MIN_SCORE
                                    ).get("entries", []))
    entries = tuple(svc.search(question, top_k=lmb.RAG_TOP_K,
                               contiguity_neighbors=0, timeline=False
                               ).get("entries", []))
    hybrid_entries = entries[:lmb.HYBRID_TOP_K]
    served = {
        "rag": Served(text=contexts["rag"], entries=entries),
        "cortex": Served(text=contexts["cortex"], facts=facts),
        "hybrid": Served(text=contexts["hybrid"], facts=facts,
                         entries=hybrid_entries),
        "nomem": Served(text=contexts["nomem"]),
    }
    # Context-level cascade PROXY: the judged cascade routes on whether the
    # cortex answer commits, and there is no answer here. Never compare a
    # number from this arm to a judged cascade accuracy.
    served["cascade"] = (served["cortex"] if contexts["cortex"].strip()
                         else served["rag"])
    return served


# ─────────────────────────────────────────────────────────────────────────
# Bench database lifecycle
# ─────────────────────────────────────────────────────────────────────────
def _bench_db_name() -> str:
    """Private per-run database, named the way the test fixtures name
    theirs — ``pseudolife_memory_bench_<pid>``, which
    ``tests/pg_fixtures.py`` also knows how to prune after a hard kill."""
    return f"pseudolife_memory_bench_{os.getpid()}"


def _admin_url() -> str:
    base = os.environ.get(
        "PSEUDOLIFE_BENCH_ADMIN_URL",
        "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres")
    return base.rsplit("/", 1)[0] + "/postgres"


def drop_bench_db(name: str) -> None:
    """Drop the database this run created, and only that one.

    The name guard is not decoration: a bench that can be pointed at a
    database it did not create is one typo away from dropping a bank.
    """
    if not (name.startswith("pseudolife_memory_bench_")
            and name.endswith(f"_{os.getpid()}")):
        raise SystemExit(f"refusing to drop {name!r}: this run did not "
                         "create it")
    import psycopg
    with psycopg.connect(_admin_url(), connect_timeout=5,
                         autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ─────────────────────────────────────────────────────────────────────────
# Artifacts
# ─────────────────────────────────────────────────────────────────────────
def _git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                              capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                     # noqa: BLE001
        return "unknown"


def write_artifact(path: Path, payload: dict, rows, force: bool = False
                   ) -> Path:
    """Write ``<path>`` and its sibling ``.jsonl`` rows.

    Refuses to overwrite either file unless ``force``. A canonical result
    file silently rewritten by a rerun is a failure this project has
    already shipped once (2026-07-21); a half-written run must not be
    completable by accident either, so an orphaned rows file blocks too.
    """
    path = Path(path)
    rows_path = path.with_suffix(".jsonl")
    existing = [p for p in (path, rows_path) if p.exists()]
    if existing and not force:
        raise SystemExit(
            "refusing to overwrite an existing artifact: "
            + ", ".join(str(p) for p in existing)
            + "\nTag the run differently, or pass --force if you really "
              "mean to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False)
                    + "\n", encoding="utf-8")
    with rows_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


CAVEATS = {
    "scores_context_not_answers": (
        "Every number is a property of the SERVED CONTEXT, not of a model "
        "answer. 'The current value is in the served text' is necessary "
        "for a correct answer and never sufficient, so each figure bounds "
        "an answerer from above and none of them is an accuracy."),
    "synthetic_extraction_is_perfect": (
        "The synthetic source writes facts directly through cortex_write, "
        "so no extractor runs and extraction quality is held at perfect. "
        "The cortex and hybrid arms here are therefore a CEILING on the "
        "representation, not a measurement of a deployed bank, whose "
        "cortex is only as good as the dream pass that filled it."),
    "cascade_proxy": (
        "The cascade arm is a CONTEXT-level proxy — the cortex context "
        "when non-empty, else the rag context. The published cascade is an "
        "ANSWER-level policy routing on whether the cortex arm commits. "
        "The two are different objects and must not be compared."),
    "marker_dimensions_have_a_structural_floor": (
        "staleness_marking and the fact channel of retraction_handling are "
        "0 by construction for the rag and nomem arms: a raw turn carries "
        "no freshness_class and no supersession chain. A delta there is a "
        "statement about what each representation can express, not about "
        "how well either retrieves."),
    "stale_flag_is_payload_only": (
        "staleness_marking reads the served PAYLOAD. No arm renders the "
        "stale flag into the flattened context string an answerer sees, so "
        "the marker reaches an agent reading the MCP payload and not an "
        "answerer reading the context block."),
}


# Source-specific caveats. The synthetic caveats above claim perfect
# extraction; on this source that claim is false in the opposite
# direction, so the two sets are never both stamped on one artifact.
LME_CAVEATS = {
    "extraction_is_the_real_path": (
        "Unlike the synthetic source, this bank is built by "
        "longmemeval_bench.ingest_and_dream — the product's own "
        "store-then-dream cadence — so the cortex and hybrid arms measure "
        "the DEPLOYED pipeline, retrieval and extraction together. A low "
        "cortex number here is a claim about the extractor at least as "
        "much as about the fact spine, and the extractor is named in "
        "meta.extractor."),
    "dimensions_the_dataset_cannot_ground": (
        "Only D1 (update_following), D2 (stale_serving) and D5 "
        "(retraction_handling) are graded, per section 4 of the spec. D3 "
        "(staleness_marking) needs a freshness_class and a TTL "
        "LongMemEval does not carry; D4 (abstention_support) needs "
        "questions whose answer was never stated, which the "
        "knowledge-update type does not contain by construction. Both "
        "report n=0 and a NULL rate — never a 0.0, which a reader would "
        "take for a failing arm."),
    "d5_is_the_entry_channel_only": (
        "The slot is synthetic (entity 'lme:<question_id>', attribute "
        "'value') because LongMemEval has no entity/attribute structure, "
        "so a bench question never matches a served fact BY NAME and D5's "
        "fact channel — the supersession chain — cannot fire on this "
        "source. D5 here is the served text plus a served entry's "
        "superseded_by_text. The cortex arm serves no entries, so its D5 "
        "is 0 BY CONSTRUCTION: a measurement artefact, not a finding."),
    "correction_is_implicit": (
        "A knowledge-update question's later statement IS the retraction, "
        "but it is never phrased as one — no 'correction:', no 'not X, it "
        "is Y'. That differs from the synthetic source's explicit "
        "corrections, which is why the two sources are reported "
        "separately and their rates are never pooled."),
    "one_question_one_bank": (
        "Each question gets its own bank, built from its own haystack and "
        "truncated before the next — the LongMemEval protocol. No "
        "cross-question interference, and no measurement of a bank that "
        "has accumulated many sessions."),
}


# ─────────────────────────────────────────────────────────────────────────
# Runs
# ─────────────────────────────────────────────────────────────────────────
def run_synthetic(args) -> int:
    import tempfile

    corpus = generate(seed=args.seed, entities=args.entities,
                      attributes=args.attributes, sessions=args.sessions,
                      filler_per_session=args.filler)
    db_name = _bench_db_name()
    os.environ["PSEUDOLIFE_BENCH_DB"] = db_name
    out = RESULTS_DIR / f"epistemic-bench-{args.tag}.json"
    # Refuse BEFORE paying an ingest, not after.
    if not args.force:
        for p in (out, out.with_suffix(".jsonl")):
            if p.exists():
                raise SystemExit(f"refusing to overwrite {p}; tag the run "
                                 "differently or pass --force")

    import longmemeval_bench as lmb                      # noqa: F401
    from ladder_sweep import build_service

    t0 = time.perf_counter()
    tmp = Path(tempfile.mkdtemp(prefix="epistemic_"))
    svc = build_service(tmp)
    try:
        print(f"bench db {db_name}: ingesting {len(corpus.turns)} turns, "
              f"{len(corpus.fact_writes)} fact writes", flush=True)
        for turn in corpus.turns:
            svc.store(turn.text, source="bench")
        # Chronological order IS supersession order: cortex_write ticks the
        # HLC per call, so the engine builds the chain, not the generator.
        for w in corpus.fact_writes:
            svc.cortex_write(w.entity, w.attribute, w.value, support="user",
                             now=w.epoch, freshness_class=w.freshness_class)
        rows, per_arm = [], {}
        served_by_q = {}
        for q in corpus.questions:
            served_by_q[q.question_id] = serve_arms(svc, q.question)
        # Serving widths against bank size. A bank smaller than the widths
        # makes every arm serve nearly everything it holds, which flatters
        # coverage and destroys abstention — a regime a reader has to be
        # able to see from the artifact.
        selectivity = {
            "cortex_slots_in_bank": len(svc.cortex_dump().get("entries", [])),
            "cortex_top_k": lmb.CORTEX_TOP_K,
            "turns_in_bank": len(corpus.turns),
            "rag_top_k": lmb.RAG_TOP_K,
        }
        for arm in ARMS:
            per_arm[arm] = score_arm(
                corpus.questions,
                lambda q, a=arm: served_by_q[q.question_id][a])
            # Beside every rate, what that rate cost in served text. An arm
            # that serves four times the context is four times as likely to
            # sweep in a near-miss value, so abstention_support in
            # particular cannot be read without this column (the project's
            # 2026-09-04 lesson: accuracy and context cost are one
            # trade-off, not two findings).
            per_arm[arm]["context_chars_mean"] = round(
                sum(len(served_by_q[q.question_id][arm].text)
                    for q in corpus.questions)
                / max(1, len(corpus.questions)), 1)
        for q in corpus.questions:
            # Shared with the LongMemEval path, so the two sources cannot
            # drift into persisting different row shapes.
            rows.append(score_row(q, served_by_q[q.question_id],
                                  ground_truth_row(q)))
    finally:
        svc.flush()

    payload = {
        "meta": {
            "bench": "epistemic", "source": "synthetic", "tag": args.tag,
            "git_rev": _git_rev(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime()),
            "contexts_only": True,
            "arms": list(ARMS), "dimensions": list(DIMENSIONS),
            "higher_is_better": HIGHER_IS_BETTER,
            "companion_metric": COMPANION,
            "spec": ("docs/superpowers/specs/"
                     "2026-09-05-epistemic-bench-design.md"),
            "stale_policy": getattr(svc.config.memory.search,
                                    "stale_policy", "annotate"),
            "rag_top_k": lmb.RAG_TOP_K, "hybrid_top_k": lmb.HYBRID_TOP_K,
            "cortex_top_k": lmb.CORTEX_TOP_K,
            "cortex_min_score": lmb.CORTEX_MIN_SCORE,
            "selectivity": selectivity,
            "wall_seconds": round(time.perf_counter() - t0, 1),
            **corpus.meta,
        },
        "caveats": CAVEATS,
        "arms": per_arm,
    }
    write_artifact(out, payload, rows, force=args.force)
    _print_table(payload)
    print(f"\nwrote {out}\n      {out.with_suffix('.jsonl')}")
    return 0


def run_lme(args) -> int:
    """Score the LongMemEval-derived slice on a bank built the real way.

    Per question: a fresh, truncated bench bank; every haystack turn
    stored and dreamed session-by-session by
    ``longmemeval_bench.ingest_and_dream`` (the product's consolidation
    cadence); then the same ``build_contexts`` / ``serve_comparator_arms``
    arms the synthetic path serves. The bank lifecycle is the one
    ``longmemeval_bench.run_extract`` uses, imported rather than
    re-derived, so a question scored here saw the bank it would have seen
    there.

    Resumable by construction: a row is appended as each question
    finishes, and a restart skips the ids already in the file. This run
    shares the GPU with judged work, so an interruption must never cost a
    second round of extraction — and the summary is therefore totalled
    from the ROWS, not from live serving, so questions scored by an
    earlier process still count.
    """
    # Before anything costs: a dead endpoint must not buy a database, a
    # dataset load, or a half-written artifact.
    ex_url = require_extractor(args.extractor)

    derivation = Path(args.derivation)
    if derivation.suffix == ".jsonl":
        derivation = derivation.with_suffix(".json")
    pairs_path = derivation.with_suffix(".jsonl")
    if not pairs_path.exists():
        raise SystemExit(
            f"{pairs_path} not found — the derived old/new pairs live in "
            "the derivation artifact's .jsonl. Run --derive-lme first "
            "(see evals/README.md), or point --derivation at a copy.")
    pairs = load_rows(pairs_path)
    if not pairs:
        raise SystemExit(f"{pairs_path} carries no derived pairs")
    dmeta = {}
    if derivation.exists():
        dmeta = json.loads(
            derivation.read_text(encoding="utf-8")).get("meta", {})

    lme_path = (Path(args.lme_path) if args.lme_path
                else DATA_DIR / dmeta.get("dataset",
                                          "longmemeval_oracle.json"))
    if not lme_path.exists():
        raise SystemExit(
            f"{lme_path} not found. The LongMemEval JSONs are gitignored; "
            "download them into evals/data/ (see evals/README.md) or point "
            "--lme-path at a copy.")

    out = lme_out_path(args.tag)
    guard_lme_artifact(out, args.force)
    rows_path = out.with_suffix(".jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)

    by_id = {q["question_id"]: q
             for q in json.loads(lme_path.read_text(encoding="utf-8"))}
    missing = [p["question_id"] for p in pairs
               if p["question_id"] not in by_id]
    if missing:
        raise SystemExit(
            f"{len(missing)} derived question_ids are absent from "
            f"{lme_path.name} (first: {missing[0]}) — the derivation and "
            "the dataset disagree. Re-derive before running.")

    import tempfile

    import longmemeval_bench as lmb
    from ladder_sweep import build_service

    db_name = _bench_db_name()
    os.environ["PSEUDOLIFE_BENCH_DB"] = db_name
    done = done_question_ids(rows_path)
    todo = lme_pending(pairs, done, args.limit)
    t0 = time.perf_counter()
    print(f"bench db {db_name}: {len(pairs)} derived questions from "
          f"{pairs_path.name}, extractor={args.extractor} "
          f"({len(done)} already done, resuming; {len(todo)} to run)",
          flush=True)

    for i, pair in enumerate(todo, 1):
        raw = by_id[pair["question_id"]]
        q = lme_question(pair)
        t_q = time.perf_counter()
        tmp = Path(tempfile.mkdtemp(prefix="epistemic_lme_"))
        svc = build_service(tmp)         # fresh, truncated per-question bank
        svc.config.memory.dream.extract_relations = False    # facts only
        extractor = make_extractor(args.extractor, ex_url)
        try:
            tally = lmb.ingest_and_dream(svc, extractor, raw,
                                         ex_url or "floor://regex")
            served = serve_arms(svc, q.question)
            slots = len(svc.cortex_dump().get("entries", []))
            stale_policy = getattr(svc.config.memory.search,
                                   "stale_policy", "annotate")
        finally:
            svc.flush()
        row = score_row(q, served, dict(
            ground_truth_row(q),
            family=pair.get("family"),
            old_date=pair.get("old_date"), new_date=pair.get("new_date"),
            sessions=len(raw.get("haystack_sessions") or []),
            extractor=args.extractor, dataset=lme_path.name,
            consolidation=tally, cortex_slots_in_bank=slots,
            stale_policy=stale_policy,
            wall_seconds=round(time.perf_counter() - t_q, 1)))
        # Appended per question: this IS the resume point.
        with rows_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{i}/{len(todo)}] {pair['question_id']}  "
              f"({row['wall_seconds']}s, {tally['turns']} turns, "
              f"{tally['claims']} claims, {slots} slots)", flush=True)

    # Summarise from the ROWS, so questions scored by an earlier process
    # count. Last write wins per id: a row can only be duplicated by a
    # hand-edited resume file, and double-counting one question silently
    # is worse than preferring the newer verdict.
    keep = {p["question_id"] for p in
            (list(pairs)[:args.limit] if args.limit else pairs)}
    by_qid = {r["question_id"]: r for r in load_rows(rows_path)
              if r.get("question_id") in keep}
    rows = list(by_qid.values())
    if len(rows) != len(keep):
        raise SystemExit(
            f"{len(rows)} rows for {len(keep)} selected questions — the "
            "run did not finish its slice. Rerun the same command to "
            "resume; a partial slice is never summarised.")
    per_arm = {arm: score_from_rows(rows, arm) for arm in ARMS}
    payload = {
        "meta": {
            "bench": "epistemic", "source": "lme", "tag": args.tag,
            "git_rev": _git_rev(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "contexts_only": True,
            "extractor": args.extractor, "extractor_url": ex_url,
            "derivation_file": pairs_path.name,
            "derivation_git_rev": dmeta.get("git_rev"),
            "dataset": lme_path.name,
            "knowledge_update_questions":
                dmeta.get("knowledge_update_questions"),
            "derived_questions": len(pairs),
            "questions_scored": len(rows), "limit": args.limit,
            "arms": list(ARMS), "dimensions": list(DIMENSIONS),
            "higher_is_better": HIGHER_IS_BETTER,
            "companion_metric": COMPANION,
            "spec": ("docs/superpowers/specs/"
                     "2026-09-05-epistemic-bench-design.md"),
            "stale_policy": rows[-1].get("stale_policy"),
            "rag_top_k": lmb.RAG_TOP_K, "hybrid_top_k": lmb.HYBRID_TOP_K,
            "cortex_top_k": lmb.CORTEX_TOP_K,
            "cortex_min_score": lmb.CORTEX_MIN_SCORE,
            # Serving widths against bank size, averaged over the
            # per-question banks: a bank smaller than the widths makes
            # every arm serve nearly everything it holds, which flatters
            # coverage.
            "selectivity": {
                "cortex_slots_in_bank_mean": round(
                    sum(int(r.get("cortex_slots_in_bank") or 0)
                        for r in rows) / max(1, len(rows)), 1),
                "cortex_top_k": lmb.CORTEX_TOP_K,
                "turns_in_bank_mean": round(
                    sum(int((r.get("consolidation") or {}).get("turns", 0))
                        for r in rows) / max(1, len(rows)), 1),
                "rag_top_k": lmb.RAG_TOP_K,
            },
            "env_knobs": lmb.bench_env_knobs(),
            "extract_seconds_total": round(
                sum(float((r.get("consolidation") or {})
                          .get("extract_seconds", 0.0)) for r in rows), 1),
            "wall_seconds_this_process": round(time.perf_counter() - t0, 1),
        },
        "caveats": {k: v for k, v in CAVEATS.items()
                    if k != "synthetic_extraction_is_perfect"},
        "arms": per_arm,
    }
    payload["caveats"].update(LME_CAVEATS)
    # Only the summary is written here — deliberately NOT through
    # write_artifact, which also rewrites the rows file. That file is this
    # run's append-only resume log: rewriting it from the summarised slice
    # would silently drop rows a narrower --limit excluded. The
    # refuse-overwrite guard for the summary already ran, before the
    # dataset load.
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    _print_table(payload)
    print(f"\nwrote {out}\n      {rows_path} ({len(rows)} rows scored)")
    return 0


def run_derive_lme(args) -> int:
    stem = "oracle" if args.derive_lme == "oracle" else "s_cleaned"
    path = DATA_DIR / f"longmemeval_{stem}.json"
    if args.lme_path:
        path = Path(args.lme_path)
    if not path.exists():
        raise SystemExit(
            f"{path} not found. The LongMemEval JSONs are gitignored; "
            "download them into evals/data/ (see evals/README.md) or point "
            "--lme-path at a copy.")
    questions = json.loads(path.read_text(encoding="utf-8"))
    ku = [q for q in questions if q.get("question_type") == "knowledge-update"]
    pairs, skips = derive_lme_pairs(questions)
    out = RESULTS_DIR / f"epistemic-bench-{args.tag}.json"
    payload = {
        "meta": {"bench": "epistemic", "source": "lme-derivation",
                 "tag": args.tag, "dataset": path.name,
                 "git_rev": _git_rev(),
                 "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()),
                 "knowledge_update_questions": len(ku),
                 "qualified": len(pairs),
                 "spec": ("docs/superpowers/specs/"
                          "2026-09-05-epistemic-bench-design.md")},
        "caveats": {"derivation_is_parsing_only": (
            "Pure parsing: a question that does not derive cleanly is "
            "skipped, never guessed at. The skip histogram is half the "
            "result.")},
        "skips": dict(sorted(skips.items(), key=lambda kv: -kv[1])),
    }
    write_artifact(out, payload, pairs, force=args.force)
    print(f"{path.name}: {len(ku)} knowledge-update questions, "
          f"{len(pairs)} qualified")
    for reason, n in payload["skips"].items():
        print(f"  skip {reason}: {n}")
    print(f"\nwrote {out}\n      {out.with_suffix('.jsonl')}")
    return 0


def _print_table(payload: dict) -> None:
    arms = payload["meta"]["arms"]
    names = list(DIMENSIONS) + [COMPANION]
    width = max(len(n) for n in names) + 2
    print()
    print("dimension".ljust(width) + "".join(a.rjust(12) for a in arms))
    for name in names:
        arrow = "" if name == COMPANION else (
            " ^" if HIGHER_IS_BETTER[name] else " v")
        cells = []
        for arm in arms:
            cell = payload["arms"][arm][name]
            cells.append("n/a".rjust(12) if cell["rate"] is None
                         else f"{cell['rate']:.3f}".rjust(12))
        n = payload["arms"][arms[0]][name]["n"]
        print(f"{name}{arrow}".ljust(width) + "".join(cells) + f"   n={n}")
    print("context chars".ljust(width) + "".join(
        f"{payload['arms'][a]['context_chars_mean']:.0f}".rjust(12)
        for a in arms))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", choices=("synthetic", "lme"),
                    default="synthetic")
    ap.add_argument("--derive-lme", choices=("oracle", "s"),
                    help="derive the LongMemEval old/new pairs and write "
                         "the derivation artifact; runs no bank")
    ap.add_argument("--lme-path", help="explicit path to a LongMemEval JSON")
    ap.add_argument("--derivation",
                    default=str(RESULTS_DIR
                                / "epistemic-bench-lme-derivation-"
                                  "20260905.json"),
                    help="--source lme: the derivation artifact whose "
                         ".jsonl carries the old/new pairs")
    ap.add_argument("--extractor", default="qwen-27b",
                    help="--source lme: the extractor rung that fills the "
                         "cortex. Names come from "
                         "longmemeval_bench.EXTRACTORS, plus "
                         f"'{FLOOR_EXTRACTOR}' for the no-LLM regex "
                         "extractor (CPU-only, for plumbing checks)")
    ap.add_argument("--limit", type=int,
                    help="--source lme: score only the first N derived "
                         "questions. Counts the slice, not the pending "
                         "set, so a resume stays on the same slice")
    ap.add_argument("--tag", required=True, help="artifact tag")
    ap.add_argument("--seed", type=int, default=20260905)
    ap.add_argument("--entities", type=int, default=6)
    ap.add_argument("--attributes", type=int, default=4)
    ap.add_argument("--sessions", type=int, default=4)
    ap.add_argument("--filler", type=int, default=3,
                    help="filler turns per session")
    ap.add_argument("--contexts-only", action="store_true",
                    help="stop after building the served contexts. The "
                         "only implemented mode: this bench is judge-free "
                         "by design and never calls an answerer.")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing artifact")
    args = ap.parse_args()

    if args.derive_lme:
        return run_derive_lme(args)
    if not args.contexts_only:
        raise SystemExit(
            "pass --contexts-only. This bench scores served contexts and "
            "never calls an answerer or a judge; an answered mode does not "
            "exist, and the flag makes that explicit in the artifact.")
    run = run_lme if args.source == "lme" else run_synthetic
    db_name = _bench_db_name()
    try:
        return run(args)
    finally:
        try:
            drop_bench_db(db_name)
            print(f"dropped bench db {db_name}")
        except Exception as exc:                          # noqa: BLE001
            print(f"WARNING: could not drop {db_name}: {exc}",
                  file=sys.stderr)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(REPO))
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    raise SystemExit(main())
