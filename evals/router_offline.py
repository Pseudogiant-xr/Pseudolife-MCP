"""Offline query-shape routing analysis — how much is left after the cascade?

The engine concatenates channels for every query: the hybrid arm serves a
cortex block plus the top-k raw entries, and the only routing policy ever
measured to win is the commit-gated cascade (serve cortex when it commits,
else rag — `replicate.cortex_commits`). This script asks, entirely offline
from already-judged per-question artifacts, whether a router that reads the
QUESTION SHAPE could do better, and at what token cost.

Nothing here calls a model. Every number is a re-aggregation of verdicts a
GPU run already produced, so the accuracy figures inherit that run's judge,
its single replicate, and its era. Treat the router numbers as a BOUND on
what a shipped router could reach, not as a measured shipped result: the
oracle rows in particular are fit on the same questions they score.

Sources (all committed):
  LME-500  evals/results/longmemeval-all-oracle-qwen-27b-alltypes-0803.jsonl
  LME-KU78 evals/results/longmemeval-ku-oracle-qwen-27b-ceiling-v38.jsonl
  BEAM-400 evals/results/beam-100K-qwen-27b-chip12-b16.jsonl

Cost units differ by source and are never mixed: the LongMemEval rows carry
a real `*_context_tokens` column, BEAM rows carry none, so BEAM cost is
measured in CHARACTERS of `contexts[arm]` and converted to tokens only for
the accuracy-per-1k ratio, at a flat 4 chars/token (labelled `est_tokens`).

Usage:
    python evals/router_offline.py \
        --out evals/results/router-offline-<date>.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

# The cascade's routing gate is imported, never re-implemented: this whole
# analysis is a comparison AGAINST that policy, so a local copy that drifted
# would flatter the router silently.
from replicate import cortex_commits  # noqa: E402

RESULTS = REPO / "evals" / "results"
LME_ALL = RESULTS / "longmemeval-all-oracle-qwen-27b-alltypes-0803.jsonl"
LME_KU38 = RESULTS / "longmemeval-ku-oracle-qwen-27b-ceiling-v38.jsonl"
BEAM = RESULTS / "beam-100K-qwen-27b-chip12-b16.jsonl"

SEED = 0
N_FOLDS = 5          # CV folds, by question
CHARS_PER_TOKEN = 4.0   # BEAM chars -> est. tokens, for the ratio column only
CASCADE = "cascade"


# ── datasets ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Record:
    """One judged question: its shape, and every arm's score and cost."""

    qid: str
    qtype: str
    question: str
    score: dict[str, float]
    cost: dict[str, float]


@dataclass(frozen=True)
class Dataset:
    name: str
    unit: str                     # "tokens" or "chars"
    cost_to_tokens: float         # multiply cost by this for the ratio column
    arms: tuple[str, ...]         # every arm, cascade last
    records: tuple[Record, ...]
    summary_path: Path | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _summary_for(path: Path) -> Path:
    return path.with_name(path.name[: -len(".jsonl")] + ".summary.json")


def load_lme(path: Path, name: str) -> Dataset:
    """LongMemEval rows: binary `*_correct`, real `*_context_tokens`."""
    arms = ("rag", "cortex", "hybrid")
    records = []
    for row in _read_jsonl(path):
        score = {a: float(bool(row[f"{a}_correct"])) for a in arms}
        cost = {a: float(row[f"{a}_context_tokens"]) for a in arms}
        commits = cortex_commits(row)
        score[CASCADE] = score["cortex"] if commits else score["rag"]
        cost[CASCADE] = cost["cortex"] + (0.0 if commits else cost["rag"])
        records.append(Record(
            qid=str(row["question_id"]), qtype=str(row["question_type"]),
            question=str(row["question"]), score=score, cost=cost))
    return Dataset(
        name=name, unit="tokens", cost_to_tokens=1.0,
        arms=arms + (CASCADE,), records=tuple(records),
        summary_path=_summary_for(path),
        notes=("binary judge verdicts", "context tokens from the run"))


def load_beam(path: Path, name: str) -> Dataset:
    """BEAM rows: float rubric `*_score`, no token column -> context chars."""
    arms = ("rag", "cortex", "hybrid", "refind", "nomem")
    records = []
    for row in _read_jsonl(path):
        score = {a: float(row[f"{a}_score"]) for a in arms}
        cost = {a: float(len(row["contexts"][a])) for a in arms}
        commits = cortex_commits(row)
        score[CASCADE] = score["cortex"] if commits else score["rag"]
        cost[CASCADE] = cost["cortex"] + (0.0 if commits else cost["rag"])
        records.append(Record(
            qid=f"{row['chat_id']}/{row['type']}[{row['index']}]",
            qtype=str(row["type"]), question=str(row["question"]),
            score=score, cost=cost))
    return Dataset(
        name=name, unit="chars", cost_to_tokens=1.0 / CHARS_PER_TOKEN,
        arms=arms + (CASCADE,), records=tuple(records),
        summary_path=_summary_for(path),
        notes=("float rubric means (paper-faithful)",
               "cost is context CHARACTERS; no token column exists"))


# ── analysis 1: per-arm and per-type reproduction ─────────────────────────
def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def arm_table(ds: Dataset) -> dict:
    out = {}
    for arm in ds.arms:
        out[arm] = {
            "score": _mean([r.score[arm] for r in ds.records]),
            "cost": _mean([r.cost[arm] for r in ds.records]),
        }
    return out


def type_table(ds: Dataset) -> dict:
    out: dict[str, dict] = {}
    for qtype in sorted({r.qtype for r in ds.records}):
        rows = [r for r in ds.records if r.qtype == qtype]
        out[qtype] = {
            "n": len(rows),
            "arms": {a: {"score": _mean([r.score[a] for r in rows]),
                         "cost": _mean([r.cost[a] for r in rows])}
                     for a in ds.arms},
        }
    return out


def sanity_vs_summary(ds: Dataset) -> dict:
    """Reproduce the committed summary from the rows, arm by arm.

    A mismatch means this script is reading the artifact differently from
    the harness that wrote it, which invalidates everything below.
    """
    if ds.summary_path is None or not ds.summary_path.exists():
        return {"checked": False}
    summary = json.loads(ds.summary_path.read_text(encoding="utf-8"))
    mine = arm_table(ds)
    deltas = {}
    for arm, published in summary.get("arms", {}).items():
        if arm not in mine:
            continue
        key = "accuracy" if "accuracy" in published else "score"
        deltas[arm] = abs(published[key] - mine[arm]["score"])
        if "context_tokens" in published:
            deltas[f"{arm}_cost"] = abs(
                published["context_tokens"] - mine[arm]["cost"])
    n_ok = summary.get("n", summary.get("n_questions")) == len(ds.records)
    return {
        "checked": True,
        "summary": ds.summary_path.name,
        "n_matches": n_ok,
        "max_score_delta": max(
            (v for k, v in deltas.items() if not k.endswith("_cost")),
            default=0.0),
        "max_cost_delta": max(
            (v for k, v in deltas.items() if k.endswith("_cost")),
            default=0.0),
        "deltas": {k: round(v, 6) for k, v in sorted(deltas.items())},
    }


# ── analysis 2/3: oracles ─────────────────────────────────────────────────
def _pick(scores: dict[str, float], costs: dict[str, float],
          candidates: tuple[str, ...]) -> str:
    """Best arm: highest score, ties to the cheapest, then arm name."""
    return min(candidates, key=lambda a: (-scores[a], costs[a], a))


def oracle_by_type(ds: Dataset, candidates: tuple[str, ...]) -> dict:
    """Upper bound for a router that knows only the question TYPE."""
    types = type_table(ds)
    choice = {t: _pick({a: v["arms"][a]["score"] for a in candidates},
                       {a: v["arms"][a]["cost"] for a in candidates},
                       candidates)
              for t, v in types.items()}
    served = [(r.score[choice[r.qtype]], r.cost[choice[r.qtype]])
              for r in ds.records]
    return {
        "choice": choice,
        "score": _mean([s for s, _ in served]),
        "cost": _mean([c for _, c in served]),
    }


def oracle_per_question(ds: Dataset, candidates: tuple[str, ...]) -> dict:
    """The ceiling: the best arm chosen per question, by an oracle that
    already knows every verdict. Not reachable by any real router."""
    served = []
    for r in ds.records:
        arm = _pick(r.score, r.cost, candidates)
        served.append((r.score[arm], r.cost[arm]))
    return {"score": _mean([s for s, _ in served]),
            "cost": _mean([c for _, c in served])}


# ── analysis 4: cheap query-shape router ──────────────────────────────────
# Hand-written surface cues over the QUESTION TEXT ONLY. No verdict, no
# context, no bank state — a shipped router sees exactly this much.
CUES: tuple[tuple[str, str], ...] = (
    ("temporal", r"\b(when|before|after|earlier|later|first|last|recent|"
                 r"ago|since|during|order|sequence|timeline|how long|"
                 r"january|february|march|april|may|june|july|august|"
                 r"september|october|november|december|yesterday|today|"
                 r"week|month|year|date)\b"),
    ("aggregate", r"\b(how many|how much|total|count|all of|all the|list|"
                  r"every|each of|sum|average|overall|combined)\b"),
    ("lookup", r"\b(what is|what's|what was|what did|which|where|who|"
               r"whose|name of)\b"),
    ("preference", r"\b(prefer|preference|favou?rite|like|dislike|enjoy|"
                   r"usually|always|style|rather)\b"),
    ("instruction", r"\b(should|remind|make sure|follow|instruct|told you|"
                    r"asked you|remember to|guideline|rule)\b"),
    ("update", r"\b(change|changed|update|updated|still|now|new|switch|"
               r"no longer|instead|current|currently|latest)\b"),
    ("summarize", r"\b(summar\w*|overview|describe|explain|walk me through|"
                  r"how did|how do|why did|why do|discuss)\b"),
    ("multihop", r"\b(and also|both|compare|relationship|between|across|"
                 r"connect|based on)\b"),
    ("self", r"\b(i|me|my|mine|myself)\b"),
    ("assistant", r"\b(you|your|we|us|our)\b"),
)

FEATURE_NAMES: tuple[str, ...] = tuple(
    [name for name, _ in CUES] + ["n_words", "n_chars", "n_question_marks"])

_COMPILED = tuple((name, re.compile(pat, re.IGNORECASE))
                  for name, pat in CUES)


def features(question: str) -> list[float]:
    """Deterministic surface features of one question. Order is fixed by
    FEATURE_NAMES so a persisted tree stays readable."""
    counts = [float(len(rx.findall(question))) for _, rx in _COMPILED]
    return counts + [float(len(question.split())),
                     float(len(question)),
                     float(question.count("?"))]


def _labels(ds: Dataset, candidates: tuple[str, ...], policy: str,
            rows: list[Record] | None = None) -> list[str]:
    """Per-question training label = the arm a token-aware oracle would
    pick. `cheap` breaks score ties toward the cheaper arm on THAT question;
    `acc` breaks them toward the strongest arm overall, which keeps the
    router from collapsing onto cortex whenever every arm happens to be
    right.

    `rows` restricts BOTH the records labelled and the arm ranking `acc`
    ties are broken by. The cross-validated callers pass a training fold,
    so "strongest arm overall" means strongest ON THE TRAINING FOLD: a
    held-out question never contributes to the statistic that decides its
    own label. (`router_via_type` has always recomputed its per-fold
    `oracle_by_type` mapping this way; this makes `acc` consistent with it.)
    """
    records = ds.records if rows is None else tuple(rows)
    if policy == "cheap":
        return [_pick(r.score, r.cost, candidates) for r in records]
    if policy != "acc":
        raise ValueError(f"unknown label policy {policy!r}")
    order = {a: i for i, a in enumerate(
        sorted(candidates,
               key=lambda a: -_mean([r.score[a] for r in records])))}
    return [min(candidates, key=lambda a: (-r.score[a], order[a], a))
            for r in records]


def _cv_predict(feats, labels, model: str):
    """5-fold CV over QUESTIONS: every prediction comes from a model that
    never saw that question.

    `labels` is either one label per row, or a callable taking a fold's
    TRAINING row indices and returning that fold's labels. The callable
    form exists so a label policy that depends on a dataset-wide statistic
    (the `acc` tie-break ranking) computes that statistic inside the
    training fold instead of over the whole dataset.

    Seeded; sklearn's tree and lbfgs solver are deterministic at a fixed
    seed.
    """
    import numpy as np
    from sklearn.dummy import DummyClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import KFold
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.tree import DecisionTreeClassifier

    X = np.asarray(feats, dtype=float)
    n = len(X)
    if callable(labels):
        label_fn = labels
    else:
        _fixed = list(labels)

        def label_fn(idx, _fixed=_fixed):
            return [_fixed[i] for i in idx]

    if n < 2:
        return [str(v) for v in label_fn(list(range(n)))]
    preds = np.empty(n, dtype=object)
    for train, test in KFold(n_splits=min(N_FOLDS, n), shuffle=True,
                             random_state=SEED).split(X):
        y_train = np.asarray(label_fn(list(train)), dtype=object)
        if len(set(y_train.tolist())) < 2:
            est = DummyClassifier(strategy="most_frequent")
        elif model == "tree_d3":
            est = DecisionTreeClassifier(max_depth=3, random_state=SEED)
        elif model == "logreg":
            est = make_pipeline(
                StandardScaler(),
                LogisticRegression(max_iter=2000, random_state=SEED))
        else:
            raise ValueError(f"unknown model {model!r}")
        est.fit(X[train], y_train)
        preds[test] = est.predict(X[test])
    return [str(p) for p in preds]


def type_predictability(ds: Dataset, model: str) -> dict:
    """Can the surface features recover the question TYPE at all?

    The oracle-by-type bound assumes a router that knows the type. If the
    question text cannot predict the type, that bound is unreachable for
    reasons that have nothing to do with the arms.
    """
    feats = [features(r.question) for r in ds.records]
    types = [r.qtype for r in ds.records]
    preds = _cv_predict(feats, types, model)
    majority = max(set(types), key=types.count)
    return {
        "model": model,
        "cv_accuracy": _mean([float(p == t) for p, t in zip(preds, types)]),
        "majority_baseline": _mean([float(t == majority) for t in types]),
    }


def _serve(ds: Dataset, arms_per_row: list[str]) -> dict:
    served = [(r.score[a], r.cost[a])
              for r, a in zip(ds.records, arms_per_row)]
    return {"score": _mean([s for s, _ in served]),
            "cost": _mean([c for _, c in served])}


def _confusion(pred: list[str], ref: list[str]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for p, t in zip(pred, ref):
        out.setdefault(t, {}).setdefault(p, 0)
        out[t][p] += 1
    return {t: dict(sorted(v.items())) for t, v in sorted(out.items())}


def cheap_router(ds: Dataset, candidates: tuple[str, ...],
                 model: str, policy: str) -> dict:
    feats = [features(r.question) for r in ds.records]
    # Labels go in as a per-fold callable, not a precomputed list: the
    # `acc` tie-break ranks arms by mean score, and that ranking has to be
    # recomputed inside each training fold or a held-out question helps
    # decide the label it is later scored against.
    preds = _cv_predict(
        feats,
        lambda idx: _labels(ds, candidates, policy,
                            rows=[ds.records[i] for i in idx]),
        model)
    by_type = oracle_by_type(ds, candidates)["choice"]
    result = _serve(ds, preds)
    result.update({
        "model": model, "label_policy": policy,
        "candidates": list(candidates),
        "arm_share": {a: preds.count(a) for a in sorted(set(preds))},
        "agree_with_oracle_by_type": _mean(
            [float(p == by_type[r.qtype])
             for p, r in zip(preds, ds.records)]),
        "confusion_vs_oracle_by_type": _confusion(
            preds, [by_type[r.qtype] for r in ds.records]),
    })
    return result


def router_via_type(ds: Dataset, candidates: tuple[str, ...],
                    model: str) -> dict:
    """The realizable form of the oracle-by-type bound.

    Predict the question TYPE from surface features, then serve the arm
    that type's best-arm mapping names. Both halves are fit on the
    training fold only — the mapping is recomputed per fold, so a test
    question never contributes to the choice made for it. This is the
    strongest realizable variant here, and the gap between it and
    `oracle_by_type` is exactly the price of not knowing the type.
    """
    from sklearn.model_selection import KFold

    feats = [features(r.question) for r in ds.records]
    types = [r.qtype for r in ds.records]
    # The same seeded split _cv_predict uses, so a row's predicted type and
    # the mapping applied to it come from the identical training fold.
    preds = _cv_predict(feats, types, model)
    served: list[tuple[float, float] | None] = [None] * len(ds.records)
    arms_used: list[str] = [""] * len(ds.records)
    for train, test in KFold(n_splits=min(N_FOLDS, len(feats)), shuffle=True,
                             random_state=SEED).split(feats):
        sub = Dataset(name=ds.name, unit=ds.unit,
                      cost_to_tokens=ds.cost_to_tokens, arms=ds.arms,
                      records=tuple(ds.records[i] for i in train))
        choice = oracle_by_type(sub, candidates)["choice"]
        tbl = arm_table(sub)
        fallback = _pick({a: tbl[a]["score"] for a in candidates},
                         {a: tbl[a]["cost"] for a in candidates}, candidates)
        for i in test:
            rec = ds.records[i]
            arm = choice.get(preds[i], fallback)
            arms_used[i] = arm
            served[i] = (rec.score[arm], rec.cost[arm])
    assert all(s is not None for s in served)
    return {
        "model": model, "label_policy": "predicted_type",
        "candidates": list(candidates),
        "type_cv_accuracy": _mean(
            [float(p == t) for p, t in zip(preds, types)]),
        "arm_share": {a: arms_used.count(a) for a in sorted(set(arms_used))},
        "score": _mean([s for s, _ in served]),
        "cost": _mean([c for _, c in served]),
    }


def two_stage_router(ds: Dataset, rows_commit: list[bool],
                     candidates: tuple[str, ...],
                     model: str, policy: str) -> dict:
    """Cascade first (cortex whenever it commits), router only on the rest.

    The router is trained AND cross-validated on the non-committing subset
    alone, so it never sees a question it is later scored on.
    """
    rest_idx = [i for i, c in enumerate(rows_commit) if not c]
    rest = Dataset(name=ds.name + "-rest", unit=ds.unit,
                   cost_to_tokens=ds.cost_to_tokens, arms=ds.arms,
                   records=tuple(ds.records[i] for i in rest_idx))
    feats = [features(r.question) for r in rest.records]
    # per-fold labels, for the reason given in `cheap_router`
    preds = (_cv_predict(
        feats,
        lambda idx: _labels(rest, candidates, policy,
                            rows=[rest.records[i] for i in idx]),
        model) if rest.records else [])
    pred_by_idx = dict(zip(rest_idx, preds))
    served: list[tuple[float, float]] = []
    arm_share: dict[str, int] = {"cortex(commit)": 0}
    for i, r in enumerate(ds.records):
        if rows_commit[i]:
            served.append((r.score["cortex"], r.cost["cortex"]))
            arm_share["cortex(commit)"] += 1
        else:
            arm = pred_by_idx[i]
            # the cascade pays the cortex block before it can see the
            # abstention, so the fallback keeps paying it too
            served.append((r.score[arm], r.cost["cortex"] + r.cost[arm]))
            arm_share[arm] = arm_share.get(arm, 0) + 1
    return {
        "model": model, "label_policy": policy,
        "candidates": list(candidates),
        "n_commit": sum(rows_commit), "n_routed": len(rest_idx),
        "arm_share": dict(sorted(arm_share.items())),
        "score": _mean([s for s, _ in served]),
        "cost": _mean([c for _, c in served]),
    }


# ── analysis 5: token-matched view ────────────────────────────────────────
def with_ratio(ds: Dataset, block: dict) -> dict:
    """Score, cost, and score per 1k served tokens, side by side."""
    tokens = block["cost"] * ds.cost_to_tokens
    out = dict(block)
    out["est_tokens"] = tokens
    out["score_per_1k_tokens"] = (block["score"] / (tokens / 1000.0)
                                  if tokens > 0 else None)
    return out


# ── analysis 6: cross-dataset robustness ──────────────────────────────────
# The two benchmarks name overlapping question types differently. Only
# these four pairs describe the same thing; the rest exist on one side only.
TYPE_PAIRS: tuple[tuple[str, str], ...] = (
    ("knowledge-update", "knowledge_update"),
    ("temporal-reasoning", "temporal_reasoning"),
    ("multi-session", "multi_session_reasoning"),
    ("single-session-preference", "preference_following"),
)


def cross_dataset_agreement(lme_choice: dict, beam_choice: dict) -> dict:
    """Do the two datasets' oracle-by-type choices agree on shared types?

    A per-type best arm that flips between benchmarks is a property of the
    benchmark, not of the question shape, and cannot be shipped.
    """
    rows = []
    for lme_t, beam_t in TYPE_PAIRS:
        a = lme_choice.get(lme_t)
        b = beam_choice.get(beam_t)
        rows.append({"lme_type": lme_t, "beam_type": beam_t,
                     "lme_best": a, "beam_best": b, "agree": a == b})
    return {"pairs": rows,
            "n_agree": sum(1 for r in rows if r["agree"]),
            "n_pairs": len(rows)}


# ── the ship / don't-ship criterion ───────────────────────────────────────
# Fixed before the numbers were read: a query-shape router is worth
# building only if a REALIZABLE one (cross-validated, never fit on the
# question it scores) clears the best single arm by >= 3 points at no more
# served cost, on BOTH benchmarks. Oracle rows never satisfy this — they
# are fit on their own test set and exist to bound the search.
GAIN_POINTS = 0.03


def verdict_for(ds_report: dict) -> dict:
    arms, pol = ds_report["arms"], ds_report["policies"]
    best_arm = ds_report["best_single_arm"]
    base_score = arms[best_arm]["score"]
    base_cost = arms[best_arm]["cost"]
    realizable = {k: v for k, v in pol.items()
                  if k.startswith(("router[", "router_via_type[",
                                   "two_stage["))}
    top = max(sorted(realizable), key=lambda k: realizable[k]["score"])
    oracle = max(sorted(k for k in pol if k.startswith("oracle_by_type[")),
                 key=lambda k: pol[k]["score"])
    ceiling = max(sorted(k for k in pol
                         if k.startswith("oracle_per_question[")),
                  key=lambda k: pol[k]["score"])
    return {
        "best_single_arm": best_arm,
        "best_single_score": base_score,
        "best_single_cost": base_cost,
        "cascade_score": arms[CASCADE]["score"],
        "cascade_cost": arms[CASCADE]["cost"],
        "best_realizable_router": top,
        "best_realizable_score": realizable[top]["score"],
        "best_realizable_cost": realizable[top]["cost"],
        "realizable_gain": realizable[top]["score"] - base_score,
        "realizable_cost_delta": realizable[top]["cost"] - base_cost,
        "best_oracle_by_type": oracle,
        "oracle_by_type_gain": pol[oracle]["score"] - base_score,
        "oracle_by_type_cost_delta": pol[oracle]["cost"] - base_cost,
        "ceiling": ceiling,
        "ceiling_gain": pol[ceiling]["score"] - base_score,
        "passes": bool(
            realizable[top]["score"] - base_score >= GAIN_POINTS
            and realizable[top]["cost"] <= base_cost),
        "oracle_bound_would_pass": bool(
            pol[oracle]["score"] - base_score >= GAIN_POINTS
            and pol[oracle]["cost"] <= base_cost),
    }


# ── driver ────────────────────────────────────────────────────────────────
def analyse(ds: Dataset, candidate_sets: dict[str, tuple[str, ...]]) -> dict:
    out: dict = {
        "name": ds.name,
        "n": len(ds.records),
        "cost_unit": ds.unit,
        "notes": list(ds.notes),
        "sanity_vs_summary": sanity_vs_summary(ds),
        "arms": {a: with_ratio(ds, v) for a, v in arm_table(ds).items()},
        "types": type_table(ds),
        "type_predictability": {m: type_predictability(ds, m)
                                for m in ("tree_d3", "logreg")},
        "policies": {},
    }
    singles = tuple(a for a in ds.arms if a != CASCADE)
    out["best_single_arm"] = _pick(
        {a: out["arms"][a]["score"] for a in singles},
        {a: out["arms"][a]["cost"] for a in singles}, singles)
    for label, cands in candidate_sets.items():
        by_type = oracle_by_type(ds, cands)
        key = f"oracle_by_type[{label}]"
        out["policies"][key] = with_ratio(ds, by_type)
        out["policies"][key]["choice"] = by_type["choice"]
        out["policies"][f"oracle_per_question[{label}]"] = with_ratio(
            ds, oracle_per_question(ds, cands))
        for model in ("tree_d3", "logreg"):
            out["policies"][f"router_via_type[{label}|{model}]"] = with_ratio(
                ds, router_via_type(ds, cands, model))
            for policy in ("acc", "cheap"):
                out["policies"][f"router[{label}|{model}|{policy}]"] = \
                    with_ratio(ds, cheap_router(ds, cands, model, policy))
    return out


def build(out_path: Path) -> dict:
    lme = load_lme(LME_ALL, "LME-500")
    ku = load_lme(LME_KU38, "LME-KU78")
    beam = load_beam(BEAM, "BEAM-400")

    lme_sets = {"base": ("rag", "cortex", "hybrid"),
                "with_cascade": ("rag", "cortex", "hybrid", CASCADE)}
    beam_sets = {"base": ("rag", "cortex", "hybrid", "refind"),
                 "with_cascade": ("rag", "cortex", "hybrid", "refind",
                                  CASCADE),
                 "with_nomem": ("rag", "cortex", "hybrid", "refind",
                                "nomem", CASCADE)}

    report = {
        "generated_by": "evals/router_offline.py",
        "seed": SEED,
        "chars_per_token": CHARS_PER_TOKEN,
        "framing": (
            "Offline re-use of already-judged per-question verdicts. No new "
            "answer or judge calls; a single replicate per source run; a "
            "local judge. Oracle rows are fit on the questions they score, "
            "so they are BOUNDS, not shipped results."),
        "sources": {
            "LME-500": LME_ALL.relative_to(REPO).as_posix(),
            "LME-KU78": LME_KU38.relative_to(REPO).as_posix(),
            "BEAM-400": BEAM.relative_to(REPO).as_posix(),
        },
        "features": list(FEATURE_NAMES),
        "datasets": {},
    }
    for ds, sets in ((lme, lme_sets), (ku, lme_sets), (beam, beam_sets)):
        report["datasets"][ds.name] = analyse(ds, sets)

    # two-stage: cascade gate first, router among rag/hybrid on the rest
    for ds, rows_path in ((lme, LME_ALL), (ku, LME_KU38), (beam, BEAM)):
        commits = [cortex_commits(r) for r in _read_jsonl(rows_path)]
        for model in ("tree_d3", "logreg"):
            for policy in ("acc", "cheap"):
                report["datasets"][ds.name]["policies"][
                    f"two_stage[{model}|{policy}]"] = with_ratio(
                        ds, two_stage_router(ds, commits, ("rag", "hybrid"),
                                             model, policy))

    report["robustness"] = {
        "type_pairs": cross_dataset_agreement(
            report["datasets"]["LME-500"]
            ["policies"]["oracle_by_type[with_cascade]"]["choice"],
            report["datasets"]["BEAM-400"]
            ["policies"]["oracle_by_type[with_cascade]"]["choice"]),
    }
    report["criterion"] = (
        f"a cross-validated router beats the best single arm by "
        f">= {GAIN_POINTS:.2f} at no more served cost, on BOTH benchmarks")
    report["verdict"] = {
        name: verdict_for(ds) for name, ds in report["datasets"].items()}
    report["verdict"]["worth_building"] = bool(
        report["verdict"]["LME-500"]["passes"]
        and report["verdict"]["BEAM-400"]["passes"])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline routing analysis")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    report = build(args.out)
    for name, ds in report["datasets"].items():
        print(f"\n=== {name} (n={ds['n']}, cost in {ds['cost_unit']}) ===")
        print(f"  sanity vs summary: "
              f"n_matches={ds['sanity_vs_summary'].get('n_matches')} "
              f"max_score_delta="
              f"{ds['sanity_vs_summary'].get('max_score_delta')} "
              f"max_cost_delta="
              f"{ds['sanity_vs_summary'].get('max_cost_delta')}")
        for arm, v in ds["arms"].items():
            print(f"  arm {arm:>9}  score {v['score']:.4f}  "
                  f"cost {v['cost']:9.1f}  /1k-tok "
                  f"{(v['score_per_1k_tokens'] or 0):.4f}")
        for pol, v in ds["policies"].items():
            print(f"  {pol:<40} score {v['score']:.4f}  "
                  f"cost {v['cost']:9.1f}  /1k-tok "
                  f"{(v['score_per_1k_tokens'] or 0):.4f}")
        tp = ds["type_predictability"]["tree_d3"]
        print(f"  type predictability (tree_d3): "
              f"{tp['cv_accuracy']:.3f} vs majority "
              f"{tp['majority_baseline']:.3f}")
    print("\nrobustness:", json.dumps(report["robustness"], indent=2))
    print("\ncriterion:", report["criterion"])
    print("verdict:", json.dumps(report["verdict"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
