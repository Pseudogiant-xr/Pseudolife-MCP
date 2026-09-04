"""Within-run paired arm comparison over one judged results JSONL.

``beam_cross_run_paired.py`` pairs the SAME arm across two runs; this pairs
every non-control arm against the ``rag`` control INSIDE one run, which is
the comparison a five-arm run exists to make. Per arm it writes:

  * the arm's mean score and the control's mean score
  * the paired per-question delta (arm minus rag), its 95% CI (normal
    approximation, 1.96 x SE over n rows) and a two-sided sign-flip
    permutation p (10k draws, seed 0)
  * per-type means for the arm and the control
  * mean served-context characters per question where the row persisted
    the arm's context (``contexts[arm]``), so a turn-matched comparison can
    say how far it is from character-matched
  * mean served-context TOKENS per question, preferring the recorded
    ``{arm}_context_tokens`` and falling back to the harness's len//4
    approximation — the units every published context cost is quoted in,
    so accuracy and cost read off one table instead of two artifacts

Harness-agnostic since 2026-09-04. BEAM rows key their verdict
``{arm}_score`` and their ability ``type``; LongMemEval rows key theirs
``{arm}_correct`` (a boolean, read as 1.0/0.0) and ``question_type``.
Both are the same paired comparison, and a second inline copy of it is
how an artifact ends up with no producer — which is exactly what happened
to the first LongMemEval pairing file this tool now writes.

Usage (tags resolve against evals/results/):

  # BEAM (the defaults)
  python evals/beam_within_run_pairs.py --tag chip12-b16 \
      --arms refind,hybrid,cortex,nomem

  # LongMemEval, 500-question six-type sweep, with the derived cascade
  python evals/beam_within_run_pairs.py --tag raglite-all-fresh \
      --prefix longmemeval-all-oracle-qwen-27b- \
      --score-key correct --type-key question_type \
      --arms cortex,hybrid,rag1,rag2,ragb400,cascade \
      --pairs cortex:rag1

Writes ``evals/results/<prefix><tag>.arms-vs-rag.json`` and refuses to
overwrite an existing artifact (never overwrite a canonical result file —
rerun with ``--out-tag``).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CONTROL = "rag"
PERMS = 10_000
SEED = 0
# The commit-gated cascade is DERIVED from the judged cortex/rag arms
# rather than answered, so it has no ``{arm}_score`` key and no persisted
# context. ``replicate`` owns the routing gate and the cost rule; a local
# copy here would drift from the arm the bench actually reports.
CASCADE = "cascade"


def _cascade():
    """Import ``replicate`` lazily — this module is stdlib-only by design
    and only the cascade arm needs it. (``replicate`` is itself
    import-light: no bench, no torch.)"""
    import replicate
    return replicate


def _score(row: dict, arm: str, score_key: str) -> float | None:
    """One arm's verdict on one row, as a float.

    ``score_key`` is ``score`` for BEAM's rubric means and ``correct`` for
    LongMemEval's booleans; a bool reads as 1.0/0.0 so both harnesses feed
    the same paired arithmetic.
    """
    if arm == CASCADE:
        # The gate reads cortex_response and both arms' boolean verdicts,
        # whatever score_key the rest of the run uses. Named explicitly so
        # a BEAM run asked for this arm fails with a sentence rather than
        # a bare KeyError three frames down.
        needed = ("cortex_response", "cortex_correct", "rag_correct")
        if any(k not in row for k in needed):
            raise SystemExit(
                "the derived cascade arm needs "
                f"{', '.join(needed)} on every row; this run has "
                f"{', '.join(k for k in needed if k not in row)} on none "
                "of them (BEAM rows score a rubric, not a boolean, so they "
                "carry no cascade)")
        return float(_cascade().cascade_correct(row))
    key = f"{arm}_{score_key}"
    if key not in row:
        return None
    return float(row[key])


def _context_chars(row: dict, arm: str) -> int | None:
    if arm == CASCADE:
        # Same rule the token cost uses: the cascade always pays the
        # cortex context and adds rag only when cortex abstains.
        cortex = _context_chars(row, "cortex")
        if cortex is None:
            return None
        if _cascade().cortex_commits(row):
            return cortex
        rag = _context_chars(row, CONTROL)
        return None if rag is None else cortex + rag
    ctx = (row.get("contexts") or {}).get(arm)
    if ctx is None:
        return None
    if isinstance(ctx, str):
        return len(ctx)
    if isinstance(ctx, list):
        return sum(len(x if isinstance(x, str) else json.dumps(x))
                   for x in ctx)
    if isinstance(ctx, dict):
        return len(json.dumps(ctx))
    return len(str(ctx))


def _context_tokens(row: dict, arm: str) -> int | None:
    """The arm's served-context size in the harness's approximate tokens.

    Prefers the ``{arm}_context_tokens`` the harnesses record (BEAM rows
    written from 2026-09-04, LongMemEval rows for much longer); older rows
    are re-estimated from the persisted characters with the SAME rule the
    adapter applies — ``max(1, chars // 4)`` (ladder_sweep.approx_tokens;
    deliberately duplicated rather than imported, because that module
    pulls torch and this script is stdlib-only by design). The floor of 1
    is why a served-nothing arm reads 1 token beside 0 characters.
    """
    if arm == CASCADE:
        try:
            return int(_cascade().cascade_context_tokens(row))
        except KeyError:
            return None
    recorded = row.get(f"{arm}_context_tokens")
    if recorded is not None:
        return int(recorded)
    chars = _context_chars(row, arm)
    return None if chars is None else max(1, chars // 4)


def _perm_p(deltas: list[float], perms: int, seed: int) -> float:
    """Two-sided sign-flip permutation p for mean(deltas) != 0."""
    observed = abs(statistics.fmean(deltas))
    if observed == 0.0:
        return 1.0
    rng = random.Random(seed)
    hits = 0
    for _ in range(perms):
        flipped = statistics.fmean(
            d if rng.random() < 0.5 else -d for d in deltas)
        if abs(flipped) >= observed:
            hits += 1
    return (hits + 1) / (perms + 1)


def _paired_block(deltas: list[float], perms: int, seed: int) -> dict:
    """The delta / CI / permutation-p / W-L-T block every pairing shares."""
    n = len(deltas)
    mean_d = statistics.fmean(deltas)
    se = statistics.stdev(deltas) / math.sqrt(n) if n > 1 else 0.0
    return {
        "delta": round(mean_d, 4),
        "ci95_halfwidth": round(1.96 * se, 4),
        "perm_p": round(_perm_p(deltas, perms, seed), 4),
        "wins": sum(1 for d in deltas if d > 0),
        "losses": sum(1 for d in deltas if d < 0),
        "ties": sum(1 for d in deltas if d == 0),
    }


def pair_run(rows: list[dict], arms: list[str], perms: int = PERMS,
             seed: int = SEED, *, score_key: str = "score",
             type_key: str = "type",
             pairs: tuple[tuple[str, str], ...] = ()) -> dict:
    """Pair every arm against the ``rag`` control inside one run.

    The keyword-only parameters are what make this harness-agnostic; their
    defaults are BEAM's, so the committed BEAM artifacts regenerate
    byte-identically and the pre-existing CLI is unchanged.
    """
    out: dict = {
        "control": CONTROL, "n_rows": len(rows), "perms": perms,
        "seed": seed, "arms": {},
    }
    ctrl_types: dict[str, list[float]] = defaultdict(list)
    ctrl_scores = []
    for r in rows:
        s = _score(r, CONTROL, score_key)
        ctrl_scores.append(s)
        ctrl_types[r[type_key]].append(s)
    # Over ALL rows, deliberately: the control is the run's fixed
    # reference and every committed arm scores every row. An arm's own
    # "mean" below is over the rows THAT arm scored, so on a run with
    # partial arm coverage delta_vs_control (a per-row paired mean) would
    # not equal mean - control_mean. The paired delta is the trustworthy
    # one; the two means are context.
    out["control_mean"] = round(statistics.fmean(ctrl_scores), 4)
    out["control_types"] = {
        t: round(statistics.fmean(v), 4) for t, v in sorted(ctrl_types.items())}
    ctrl_chars = [c for c in (_context_chars(r, CONTROL) for r in rows)
                  if c is not None]
    out["control_context_chars_mean"] = (
        round(statistics.fmean(ctrl_chars)) if ctrl_chars else None)
    ctrl_tokens = [t for t in (_context_tokens(r, CONTROL) for r in rows)
                   if t is not None]
    out["control_context_tokens_mean"] = (
        round(statistics.fmean(ctrl_tokens)) if ctrl_tokens else None)
    for arm in arms:
        paired = [(a, c) for a, c in
                  ((_score(r, arm, score_key), _score(r, CONTROL, score_key))
                   for r in rows) if a is not None]
        if not paired:
            out["arms"][arm] = {"n": 0}
            continue
        deltas = [a - b for a, b in paired]
        types: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            s = _score(r, arm, score_key)
            if s is not None:
                types[r[type_key]].append(s)
        scored = [r for r in rows if _score(r, arm, score_key) is not None]
        chars = [c for c in (_context_chars(r, arm) for r in scored)
                 if c is not None]
        tokens = [t for t in (_context_tokens(r, arm) for r in scored)
                  if t is not None]
        block = _paired_block(deltas, perms, seed)
        out["arms"][arm] = {
            "n": len(deltas),
            "mean": round(statistics.fmean(a for a, _ in paired), 4),
            "delta_vs_control": block["delta"],
            "ci95_halfwidth": block["ci95_halfwidth"],
            "perm_p": block["perm_p"],
            "wins": block["wins"],
            "losses": block["losses"],
            "ties": block["ties"],
            "full_marks_rows": sum(1 for a, _ in paired if a == 1.0),
            "types": {t: round(statistics.fmean(v), 4)
                      for t, v in sorted(types.items())},
            "context_chars_mean": (round(statistics.fmean(chars))
                                   if chars else None),
            "context_tokens_mean": (round(statistics.fmean(tokens))
                                    if tokens else None),
        }
    # Arm-vs-arm pairings, when asked for. Absent (not empty) by default,
    # so a BEAM artifact written before this option regenerates unchanged.
    if pairs:
        out["pairs"] = {}
        for left, right in pairs:
            deltas = [a - b for a, b in
                      ((_score(r, left, score_key),
                        _score(r, right, score_key)) for r in rows)
                      if a is not None and b is not None]
            out["pairs"][f"{left}-{right}"] = (
                {"n": 0} if not deltas
                else {"n": len(deltas), **_paired_block(deltas, perms, seed)})
    return out


def _parse_pairs(spec: str | None) -> tuple[tuple[str, str], ...]:
    """``"cortex:rag1,hybrid:cortex"`` -> (("cortex","rag1"), ...)."""
    if not spec:
        return ()
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.count(":") != 1:
            raise SystemExit(f"--pairs takes left:right entries, got {part!r}")
        left, right = part.split(":")
        out.append((left.strip(), right.strip()))
    return tuple(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tag", required=True,
                    help="run tag, e.g. chip12-b16")
    ap.add_argument("--prefix", default="beam-100K-qwen-27b-",
                    help="results filename prefix; LongMemEval runs use "
                         "e.g. longmemeval-all-oracle-qwen-27b-")
    ap.add_argument("--arms", default="refind,hybrid,cortex,nomem",
                    help="comma-separated arms to pair against the rag "
                         f"control; {CASCADE!r} is derived from the judged "
                         "cortex/rag arms rather than read off a key")
    ap.add_argument("--score-key", default="score", choices=("score",
                                                             "correct"),
                    help="row key suffix holding an arm's verdict: score "
                         "(BEAM rubric means, the default) or correct "
                         "(LongMemEval booleans, read as 1.0/0.0)")
    ap.add_argument("--type-key", default="type",
                    choices=("type", "question_type"),
                    help="row key holding the question's ability/type")
    ap.add_argument("--pairs", default=None,
                    help="extra arm-vs-arm pairings, e.g. cortex:rag1")
    ap.add_argument("--note", default=None,
                    help="a sentence recorded in the artifact — what the "
                         "rows are and what is excluded")
    ap.add_argument("--out-tag", default=None,
                    help="write <prefix><out-tag>.arms-vs-rag.json instead")
    ap.add_argument("--perms", type=int, default=PERMS)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)

    src = RESULTS_DIR / f"{args.prefix}{args.tag}.jsonl"
    out_path = RESULTS_DIR / (
        f"{args.prefix}{args.out_tag or args.tag}.arms-vs-rag.json")
    if out_path.exists():
        raise SystemExit(f"refusing to overwrite {out_path}; use --out-tag")
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8")
            .splitlines() if line.strip()]
    result = pair_run(rows, [a for a in args.arms.split(",") if a],
                      perms=args.perms, seed=args.seed,
                      score_key=args.score_key, type_key=args.type_key,
                      pairs=_parse_pairs(args.pairs))
    result["source"] = src.name
    if args.note:
        result["note"] = args.note
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("control_types",)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
