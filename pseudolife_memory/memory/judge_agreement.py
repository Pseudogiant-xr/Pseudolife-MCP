"""How often each recorded judge vote agreed with the decision a human or
agent later took on the same row: the number a shadow mode is promoted on.

A judge mode in ``shadow`` records votes and applies nothing. Until
2026-10-17 nothing scored those votes against the decisions that
followed, so promoting a mode to ``auto`` rested on a ladder panel from
another month, or on nothing. The decisions themselves are the labels:
every accept, reject or retype a human took in the Console, or an agent
took through ``memory_graph_review``, on a row that carried votes.

Two sources, because an accepted merge CASCADE-deletes its proposal row
and the votes on it: merge and junk decisions read the vote snapshot
``record_merge_decision`` wrote into ``merge_decisions.votes`` (schema
v56) at decision time; link decisions read the surviving
``edge_proposals`` row. Decisions the judges applied themselves
(``decided_by`` ``dream-judge`` / ``dream-auto``) are not labels.

Everything here is pure: the storage reads are in
``PostgresStorage.judge_vote_decisions``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from pseudolife_memory.memory.graph_review import (
    merge_verdict_class, relate_relations, third_relate_relation)

# Decisions the judges took themselves: votes applied, not votes judged.
JUDGE_DECIDERS = frozenset({"dream-judge", "dream-auto"})

MERGE_OPINIONS = ("judge", "judge2", "judge3")
LINK_OPINIONS = ("judge", "judge2")

# A junk row's verdicts: the judge says ``delete`` or ``keep`` (``junk`` and
# ``reject`` are the older spellings of the same two), the decision is an
# accepted deletion or a rejected proposal (the entity kept).
_JUNK_CLASS = {"delete": "delete", "junk": "delete", "keep": "keep",
               "reject": "keep"}
_JUNK_LABEL = {"accepted": "delete", "rejected": "keep"}
_MERGE_LABEL = {"accepted": "accept", "rejected": "reject"}
_LINK_LABEL = {"accepted": "accept", "rejected": "reject", "retyped": "retype"}


def proposal_votes(row: dict) -> dict | None:
    """The votes an ``entity_proposals`` row carries, as the audit stores
    them: ``{judge: {verdict, confidence, model, [served_model,] relation}}``
    for each opinion with a verdict, or None when none was recorded. The
    relation is the one a relate vote named (read back from the note, as
    the gates do). Each opinion's served model is its
    ``<opinion>_served_model`` column (the first's since v56); a vote
    recorded without one carries no ``served_model`` key."""
    rel1, rel2 = relate_relations(row.get("judge_note"))
    rel3 = third_relate_relation(row.get("judge_note"))
    out: dict[str, dict] = {}
    for opinion, rel in zip(MERGE_OPINIONS, (rel1, rel2, rel3)):
        verdict = row.get(f"{opinion}_verdict")
        if not verdict:
            continue
        vote: dict[str, Any] = {
            "verdict": verdict,
            "confidence": _conf(row.get(f"{opinion}_confidence")),
            "model": row.get(f"{opinion}_model"),
        }
        served = row.get(f"{opinion}_served_model")
        if served:
            vote["served_model"] = served
        vote["relation"] = rel if verdict == "relate" else None
        out[opinion] = vote
    return out or None


def compute_judge_agreement(merge_rows: Iterable[dict], link_rows: Iterable[dict],
                            *, accept_gate: float, reject_gate: float,
                            link_gate: float, window_days: float) -> dict:
    """Score every decided row's votes against its decision.

    ``merge_rows`` are ``merge_decisions`` rows with a ``votes`` snapshot
    (``into_display`` None marks a junk row); ``link_rows`` are decided
    ``edge_proposals`` rows. Per opinion: ``voted`` (a verdict other than
    ``leave``), ``agree``, ``abstain``, ``precision`` and ``by_verdict``
    (per vote class). ``agreed_two``: the rows whose first two opinions
    agreed from distinct models at a mean confidence of at least the
    class's gate (``accept_gate`` / ``reject_gate`` for merges,
    ``link_gate`` for links), which is what the two-vote gates would have
    applied. ``majority_three``: the rows with three votes whose 2-of-3
    majority cleared its gate, what the tiebreak would have applied.

    ``unattributed_links`` counts decided link rows with votes but no
    ``decided_by``: decided before the Console and ``memory_graph_review``
    attributed link decisions (2026-10-18), by a human, an agent or the
    dream's retype pass, so they are not labels.

    "Distinct models" is the live gates' rule (``_stored_second_distinct``,
    ``_tiebreak_refusal`` in service_dream): a pair's two votes carry the
    models their endpoints reported serving, those served names differ and
    so do the configured stamps; a majority needs both earlier served names
    known and a third served by a model other than every name heard (the
    first two may share one). A vote whose served name is unknown is never
    part of a pair, as the live gate refuses it; a first vote recorded
    before v56 stamped it carries none, so its pair is not counted here,
    though the live gate, reading the judging memo while the row is
    pending, may still apply it until ``review_rejudge`` re-asks it. The
    accept side also replays the row-shaped refusals of
    ``_merge_accept_refusal`` from the audit row: an analyzer-filed
    proposal, a name veto and a variant conflict are left for a human
    whatever the votes say, so they are never counted as what ``auto``
    would have applied. Not replayed: the merge ``low_differential``
    exclusion (computed from the evidence at judging time), so
    ``agreed_two.accept`` and ``majority_three.accept`` still count a few
    rows ``auto`` would skip; and the dismissed-pair check (``auto``
    closes such a row as a reject, and a human reject dismisses the pair
    itself, so it cannot be told apart afterwards);
    and a decided link row goes with either endpoint when a later merge or
    junk delete takes it (``edge_proposals`` cascades), so links are scored
    while both endpoints exist."""
    labels: Counter[str] = Counter()
    scored = {"merge": 0, "junk": 0, "link": 0}
    unattributed_links = 0
    merge = _Opinions(MERGE_OPINIONS)
    junk = _Opinions(("judge",))
    link = _Opinions(LINK_OPINIONS)
    merge_two = _Pairs()
    merge_three = _Pairs()
    link_two = _Pairs()

    for row in merge_rows:
        decider = row.get("decided_by")
        votes = row.get("votes")
        if not votes or not decider or decider in JUDGE_DECIDERS:
            continue
        is_junk = row.get("into_display") is None
        label = (_JUNK_LABEL if is_junk else _MERGE_LABEL).get(row.get("status"))
        if label is None:
            continue
        labels[decider] += 1
        if is_junk:
            scored["junk"] += 1
            junk.score(votes, label, _junk_class)
            continue
        scored["merge"] += 1
        merge.score(votes, label, _merge_class)
        classes = _classes(votes, MERGE_OPINIONS, _merge_class)
        gates = {"accept": accept_gate, "reject": reject_gate}
        refused = _accept_refused(row)
        pair = _agreed_pair(votes, classes, gates)
        if pair and not (refused and pair[0] == "accept"):
            merge_two.score(pair, label)
        majority = _majority(votes, classes, gates)
        if majority and not (refused and majority[0] == "accept"):
            merge_three.score(majority, label)

    for row in link_rows:
        decider = row.get("decided_by")
        label = _LINK_LABEL.get(row.get("status"))
        if label is None or decider in JUDGE_DECIDERS:
            continue
        if not decider:
            # Decided before link decisions were attributed (2026-10-18):
            # a human, an agent or the dream's retype pass, unknowable now.
            unattributed_links += 1
            continue
        votes = {op: {"verdict": row.get(f"{op}_verdict"),
                      "confidence": _conf(row.get(f"{op}_confidence")),
                      "model": row.get(f"{op}_model"),
                      "served_model": row.get(f"{op}_served_model")}
                 for op in LINK_OPINIONS if row.get(f"{op}_verdict")}
        if not votes:
            continue
        labels[decider] += 1
        scored["link"] += 1
        link.score(votes, label, _link_class)
        classes = _classes(votes, LINK_OPINIONS, _link_class)
        pair = _agreed_pair(votes, classes,
                            {"accept": link_gate, "reject": link_gate})
        if pair:
            link_two.score(pair, label)

    return {
        "window_days": window_days,
        "scored": scored,
        "labels": dict(labels),
        "unattributed_links": unattributed_links,
        "merge": {**merge.report(), "agreed_two": merge_two.report(),
                  "majority_three": merge_three.report()},
        "junk": junk.report(),
        "link": {**link.report(), "agreed_two": link_two.report()},
    }


# ── vote classes ───────────────────────────────────────────────────────────

def _conf(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _merge_class(verdict) -> str | None:
    """``accept`` / ``reject`` (relate included), None for an abstention."""
    if verdict in (None, "leave"):
        return None
    cls = merge_verdict_class(verdict)
    return cls if cls in ("accept", "reject") else None


def _junk_class(verdict) -> str | None:
    return _JUNK_CLASS.get(verdict)


def _link_class(verdict) -> str | None:
    return verdict if verdict in ("accept", "reject", "retype") else None


def _classes(votes: dict, opinions, classify) -> dict[str, str | None]:
    return {op: classify(votes[op].get("verdict")) for op in opinions if op in votes}


def _distinct(first: dict, second: dict) -> bool:
    """The live two-vote rule (``_stored_second_distinct``): both served
    names known and different, and the configured stamps different too. A
    vote whose endpoint named no model is never one of a distinct pair."""
    served1, served2 = first.get("served_model"), second.get("served_model")
    return bool(served1 and served2 and served1 != served2
                and (first.get("model") or "") != (second.get("model") or ""))


def _third_distinct(third: dict, first: dict, second: dict) -> bool:
    """The live tiebreak rule (``_tiebreak_refusal``): the third vote's
    served name known and, like its stamp, other than every name the first
    two votes carry. The caller checks the earlier served names are known."""
    earlier = {m for v in (first, second)
               for m in (v.get("served_model"), v.get("model")) if m}
    served3 = third.get("served_model")
    return bool(served3 and served3 not in earlier
                and third.get("model") not in earlier)


def _accept_refused(row: dict) -> bool:
    """Whether the live accept path (``_merge_accept_refusal``) would have
    left this merge for a human however the votes agreed: an analyzer-filed
    proposal (``reason``), a name veto or a variant conflict between the
    two names. All three are on the audit row, so the report replays them;
    the reject side has no such gate."""
    from pseudolife_memory.memory.graph_consolidation import variant_conflict
    from pseudolife_memory.memory.graph_review import merge_veto
    if str(row.get("reason") or "").startswith("analyzer-duplicate"):
        return True
    a = str(row.get("entity_display") or "")
    b = str(row.get("into_display") or "")
    return merge_veto(a, b) is not None or variant_conflict(a, b)


def _agreed_pair(votes, classes, gates) -> tuple[str, list] | None:
    """``(class, [vote, vote])`` when the first two opinions agree on an
    accept or reject class, come from distinct models and clear the
    class's mean gate; the shape the two-vote gates apply."""
    if "judge" not in votes or "judge2" not in votes:
        return None
    cls = classes.get("judge")
    if cls not in gates or classes.get("judge2") != cls:
        return None
    first, second = votes["judge"], votes["judge2"]
    if not _distinct(first, second):
        return None
    if (first["confidence"] + second["confidence"]) / 2.0 < gates[cls]:
        return None
    return cls, [first, second]


def _majority(votes, classes, gates) -> tuple[str, list] | None:
    """``(class, majority votes)`` when all three opinions voted, both
    earlier served names are known, the third came from a model other
    than every name heard, and two votes share an accept or reject class
    at a mean of at least that class's gate; the shape the tiebreak
    applies."""
    if any(op not in votes for op in MERGE_OPINIONS):
        return None
    first, second, third = (votes[op] for op in MERGE_OPINIONS)
    # The live tiebreak (``_tiebreak_refusal``) needs both earlier served
    # names known and the third other than every name heard; the first two
    # need not differ from each other (a same-model second vote is still
    # recorded, and a split row still gets its third).
    if not (first.get("served_model") and second.get("served_model")
            and _third_distinct(third, first, second)):
        return None
    for cls in gates:
        side = [votes[op] for op in MERGE_OPINIONS if classes.get(op) == cls]
        if len(side) >= 2:
            mean = sum(v["confidence"] for v in side) / len(side)
            return (cls, side) if mean >= gates[cls] else None
    return None


# ── tallies ────────────────────────────────────────────────────────────────

class _Opinions:
    def __init__(self, opinions):
        self._opinions = tuple(opinions)
        self._voted: Counter[str] = Counter()
        self._agree: Counter[str] = Counter()
        self._abstain: Counter[str] = Counter()
        self._by_verdict: dict[str, Counter[str]] = {
            op: Counter() for op in self._opinions}
        self._by_verdict_agree: dict[str, Counter[str]] = {
            op: Counter() for op in self._opinions}

    def score(self, votes: dict, label: str, classify) -> None:
        for op in self._opinions:
            vote = votes.get(op)
            if not vote:
                continue
            cls = classify(vote.get("verdict"))
            if cls is None:
                self._abstain[op] += 1
                continue
            self._voted[op] += 1
            self._by_verdict[op][cls] += 1
            if cls == label:
                self._agree[op] += 1
                self._by_verdict_agree[op][cls] += 1

    def report(self) -> dict:
        out = {}
        for op in self._opinions:
            voted = self._voted[op]
            out[op] = {
                "voted": voted,
                "agree": self._agree[op],
                "abstain": self._abstain[op],
                "precision": _ratio(self._agree[op], voted),
                "by_verdict": {
                    cls: {"n": n, "agree": self._by_verdict_agree[op][cls]}
                    for cls, n in sorted(self._by_verdict[op].items())},
            }
        return out


class _Pairs:
    """Agreeing votes by class: how often the gate's choice matched."""

    def __init__(self):
        self._n: Counter[str] = Counter()
        self._agree: Counter[str] = Counter()

    def score(self, agreed: tuple[str, list], label: str) -> None:
        cls, _votes = agreed
        self._n[cls] += 1
        if cls == label:
            self._agree[cls] += 1

    def report(self) -> dict:
        return {cls: {"n": self._n[cls], "agree": self._agree[cls],
                      "precision": _ratio(self._agree[cls], self._n[cls])}
                for cls in ("accept", "reject")}


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None
