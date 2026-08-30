"""Offline re-score of the committed lme-v2 artifacts under the tightened
multiple-choice scorer (issue #173, 2026-08-25).

``lme_v2_smoke.score_mc``'s no-box fallback used to accept any standalone
``[A-Ha-h]`` token, so the English article "a" in a truncated reasoning trace
scored as answer **A**. This re-runs the deterministic scorer — and nothing
else — over the committed run artifacts, so it needs no GPU, no endpoint and
no bench database: it reads ``{arm}_response`` out of the JSONL and recomputes
``{arm}_correct``.

House rule "never overwrite a canonical result file": every output here is
written to a NEW ``*-rescored-strictmc.*`` path beside the original. The
originals stay exactly as the runs left them, and each correction artifact
carries the superseded numbers next to the corrected ones.

Usage (repo root, no GPU):

  PYTHONPATH=. python evals/rescore_strict_mc.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lme_v2_smoke as S  # noqa: E402

RESULTS = Path(__file__).resolve().parents[1] / "evals" / "results"
ARMS = ("rag", "cortex", "hybrid")
TAG = "-rescored-strictmc"
SCORER = ("score_mc no-box fallback: anchored uppercase A-H "
          "(whole-response letter or explicit answer marker) "
          "instead of any standalone [A-Ha-h]")


def load(name: str) -> list[dict]:
    path = RESULTS / f"{name}.jsonl"
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if "rag_correct" in r]


def rescore(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (rescored rows, flips). Rows are copies; originals untouched."""
    out, flips = [], []
    for row in rows:
        new = dict(row)
        for arm in ARMS:
            was = bool(row[f"{arm}_correct"])
            now = S.score_answer(row["eval_function"],
                                 row.get(f"{arm}_response", ""), row["answer"])
            new[f"{arm}_correct"] = now
            if now != was:
                flips.append({"question_id": row["question_id"], "arm": arm,
                              "eval_function": row["eval_function"].split(
                                  "|")[0],
                              "gold": row["answer"], "was": was, "now": now,
                              "response_tail": (row.get(f"{arm}_response")
                                                or "")[-120:]})
        out.append(new)
    return out, flips


def accuracies(rows: list[dict]) -> dict[str, float]:
    n = len(rows)
    return {a: round(sum(1 for r in rows if r[f"{a}_correct"]) / n, 3)
            for a in ARMS}


def summary(name: str, date: str = "2026-08-25") -> dict:
    """report()'s summary shape, recomputed, plus the correction block."""
    rows = load(name)
    new_rows, flips = rescore(rows)
    n = len(rows)
    out = {"category": "procedure", "n": n, "arms": {}}
    for arm in ARMS:
        out["arms"][arm] = {
            "eval_accuracy": round(
                sum(1 for r in new_rows if r[f"{arm}_correct"]) / n, 3),
            "judge_accuracy": round(
                sum(1 for r in rows if r.get(f"{arm}_judge", False)) / n, 3),
            "context_tokens": round(
                sum(r[f"{arm}_context_tokens"] for r in rows) / n, 1),
            "answer_seconds": round(
                sum(r.get(f"{arm}_answer_seconds", 0.0) for r in rows) / n, 2),
        }
    out["rescore"] = {
        "issue": 173, "date": date, "scorer": SCORER,
        "source": f"evals/results/{name}.jsonl",
        "supersedes": f"evals/results/{name}.summary.json",
        "eval_accuracy_before": accuracies(rows),
        "eval_accuracy_after": accuracies(new_rows),
        "flips": flips,
    }
    return out


def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided sign test — the statistic the paired artifacts use."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 4)


def paired(a_name: str, b_name: str, b_limit: int | None = None) -> dict:
    """Paired-by-question_id comparison, re-scored on both sides.

    ``b_limit`` keeps a comparison source-stable when its b-side JSONL has
    since grown: the 2026-08-19 paired56 run's 56 rows are the FIRST 56 of
    ``lme-v2-smoke-qwen38-slice.jsonl`` (rows 57-74 were appended by the
    completion run, never re-run), so truncating after load reproduces the
    original comparison exactly instead of overwriting it with a 74-row one.
    """
    a_rows, a_flips = rescore(load(a_name))
    b_rows, b_flips = rescore(load(b_name)[:b_limit])
    a_by = {r["question_id"]: r for r in a_rows}
    b_by = {r["question_id"]: r for r in b_rows}
    shared = [q for q in a_by if q in b_by]
    arms = {}
    for arm in ARMS:
        for metric in ("correct", "judge"):
            key = f"{arm}_{metric}"
            a_hit = [bool(a_by[q].get(key)) for q in shared]
            b_hit = [bool(b_by[q].get(key)) for q in shared]
            wins = sum(1 for x, y in zip(a_hit, b_hit) if y and not x)
            losses = sum(1 for x, y in zip(a_hit, b_hit) if x and not y)
            acc_a = sum(a_hit) / len(shared)
            acc_b = sum(b_hit) / len(shared)
            # Key names are the superseded artifact's, kept so the two files
            # can be diffed field-for-field. a is the 3.6 side, b the 3.8.
            arms[key] = {"acc_36": round(acc_a, 4), "acc_38": round(acc_b, 4),
                         "delta": round(acc_b - acc_a, 4), "wins": wins,
                         "losses": losses,
                         "sign_test_p": sign_test_p(wins, losses)}
    return {"comparison": "lme-v2 procedure, paired by question_id",
            "a": a_name, "b": b_name, "paired_n": len(shared),
            "primary_metric": "eval (deterministic scorer)", "arms": arms,
            "flips": {"a": a_flips, "b": b_flips}}


def write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"  -> {path.relative_to(RESULTS.parents[1])}")


def main() -> int:
    # ── the two 74-question summaries the benchmarks table publishes ──
    for name in ("lme-v2-smoke-slice2", "lme-v2-smoke-slice2-compose"):
        s = summary(name)
        print(f"{name}: {s['rescore']['eval_accuracy_before']} -> "
              f"{s['rescore']['eval_accuracy_after']} "
              f"({len(s['rescore']['flips'])} flips)")
        write(RESULTS / f"{name}{TAG}.summary.json", s)

    # ── the replicated 10-question slice the guide publishes as an agg ──
    reps = {"KU": ("lme-v2-smoke-slice1", "lme-v2-smoke-slice1-r2",
                   "lme-v2-smoke-slice1-r3"),
            "compose": ("lme-v2-smoke-slice1-compose",
                        "lme-v2-smoke-slice1-r2-compose",
                        "lme-v2-smoke-slice1-r3-compose")}
    agg = {"replicates": 3, "questions": 10, "arms": {},
           "rescore": {"issue": 173, "date": "2026-08-25", "scorer": SCORER,
                       "supersedes": "evals/results/"
                                     "lme-v2-smoke-slice1.agg.json",
                       "sources": {k: list(v) for k, v in reps.items()},
                       "flips": []}}
    before: dict[str, list[float]] = {}
    for mode, names in reps.items():
        old_rows = [load(n) for n in names]
        new = [rescore(r) for r in old_rows]
        agg["rescore"]["flips"] += [f for _, fl in new for f in fl]
        for arm in ARMS:
            vals = [accuracies(rows)[arm] for rows, _ in new]
            agg["arms"][f"{mode}.{arm}"] = {
                "replicates": vals, "mean": round(sum(vals) / 3, 3),
                "min": min(vals), "max": max(vals)}
            before[f"{mode}.{arm}"] = [accuracies(r)[arm] for r in old_rows]
    agg["rescore"]["mean_before"] = {
        k: round(sum(v) / 3, 3) for k, v in before.items()}
    agg["rescore"]["mean_after"] = {
        k: agg["arms"][k]["mean"] for k in agg["arms"]}
    print(f"lme-v2-smoke-slice1.agg: {agg['rescore']['mean_before']} -> "
          f"{agg['rescore']['mean_after']} "
          f"({len(agg['rescore']['flips'])} flips)")
    write(RESULTS / f"lme-v2-smoke-slice1{TAG}.agg.json", agg)

    # ── the paired 3.6-vs-3.8 verdict the CHANGELOG states ──
    # b_limit pins this to the 56 rows the 2026-08-19 run produced; the
    # JSONL has since grown to 74 (2026-08-30 promotion) and an unpinned
    # re-run would overwrite this canonical file with different numbers.
    p = paired("lme-v2-smoke-slice2", "lme-v2-smoke-qwen38-slice",
               b_limit=56)
    p["supersedes"] = "evals/results/lme-v2-qwen38-vs-slice2-paired56.json"
    p["rescore"] = {"issue": 173, "date": "2026-08-25", "scorer": SCORER}
    p["note"] = ("Both sides re-scored. The judge arms are untouched by the "
                 "scorer change and reproduce the superseded artifact "
                 "exactly, which is what licenses reading the eval-arm "
                 "movement as the scorer fix and nothing else.")
    for key, v in p["arms"].items():
        print(f"paired {key:<16} delta {v['delta']:+.4f} "
              f"{v['wins']}W/{v['losses']}L p={v['sign_test_p']}")
    write(RESULTS / f"lme-v2-qwen38-vs-slice2-paired56{TAG}.json", p)

    # ── the 2026-08-30 promotion of the completed 74-row 3.8 slice ──
    # (CHANGELOG, 2026-08-30). Same scorer fix, applied at promotion time.
    for name in ("lme-v2-smoke-qwen38-slice",
                 "lme-v2-smoke-qwen38-slice-compose"):
        s = summary(name, date="2026-08-30")
        print(f"{name}: {s['rescore']['eval_accuracy_before']} -> "
              f"{s['rescore']['eval_accuracy_after']} "
              f"({len(s['rescore']['flips'])} flips)")
        write(RESULTS / f"{name}{TAG}.summary.json", s)

    # The full-74 paired comparison. The b-side is the fixjudge variant to
    # mirror the raw paired74 artifact; its eval rows are identical to the
    # base slice's (only the judge parse differs), so the eval movement is
    # attributable to the scorer fix alone.
    p74 = paired("lme-v2-smoke-slice2", "lme-v2-smoke-qwen38-slice-fixjudge")
    p74["a"] = "slice2 (Qwen3.6, 74 rows)"
    p74["b"] = "qwen38-slice-fixjudge (Qwen3.8, 74 rows, uniform judge parse)"
    p74["supersedes"] = "evals/results/lme-v2-qwen38-vs-slice2-paired74.json"
    p74["rescore"] = {"issue": 173, "date": "2026-08-30", "scorer": SCORER}
    p74["note"] = ("Both sides re-scored under the strict MC scorer (#173). "
                   "The judge arms are untouched by the scorer and reproduce "
                   "the superseded artifact exactly. Judge identity remains "
                   "confounded with answerer; eval is the preregistered "
                   "primary.")
    for key, v in p74["arms"].items():
        print(f"paired74 {key:<16} delta {v['delta']:+.4f} "
              f"{v['wins']}W/{v['losses']}L p={v['sign_test_p']}")
    write(RESULTS / f"lme-v2-qwen38-vs-slice2-paired74{TAG}.json", p74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
