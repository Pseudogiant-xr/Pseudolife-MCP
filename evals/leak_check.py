"""Gold-answer leak check over a judged eval artifact.

The SR-TTT retraction (arXiv 2603.06642) came down to one unchecked
assumption: the gold answer was already sitting in the context the model
was handed, so the reported adaptation win measured nothing. The same
hole is open in any recall benchmark — a question that names its own
answer scores on every arm, and the arm that "retrieved" it did no work.

This checker reads a committed artifact (BEAM ``*_score`` rows or
LongMemEval ``*_correct`` rows) and reports, per arm, the mean over ALL
rows next to the mean over the rows where the gold answer was NOT already
in the non-retrieval input. Rows whose gold answer is too short or too
generic to test for (yes/no, a bare number) — or absent entirely, as on
BEAM's five rubric-judged question types — are reported as untestable,
broken down by reason. They are not leaked, so they still count in
``leak_free``; ``leak_free_testable`` beside it is the mean over the rows
this check could actually examine.

Two leak sites are checked:

* **question** — the gold answer appears verbatim in the question text
  (or the run recorded the ``gold_in_question`` flag at answer time, in
  which case the recorded flag is trusted over a re-derivation).
* **<arm>_context** — an arm declared context-free (the no-memory arm)
  was served a context containing the gold answer. That would flatter
  memory-off in exactly the comparison the arm exists to make.

    python evals/leak_check.py --in evals/results/<artifact>.jsonl

Always writes its report (default ``<artifact>.leakcheck.json``) and
exits 1 when any row leaked, so it can gate a promotion.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# The per-row flag a run may record at answer time (see beam_adapter).
FLAG_KEY = "gold_in_question"
# Arms whose served context must be empty by construction.
CONTEXT_FREE_ARMS = ("nomem",)
# Answers too short or too generic for containment to mean anything: a
# one-word "yes" or a bare "3" matches by chance in almost any question.
MIN_ANSWER_CHARS = 4
TRIVIAL_ANSWERS = frozenset({
    "yes", "no", "none", "true", "false", "n a", "na", "unknown", "never",
    "always", "nothing", "it", "them",
})

_ARM_SCORE_RE = re.compile(r"^(?P<arm>.+)_(?:score|correct)$")


def normalize(text) -> str:
    """Lowercase, collapse every non-alphanumeric run to one space."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def untestable_reason(answer) -> str | None:
    """Why containment cannot be tested for this gold, or None when it
    can. Two different facts, reported separately: ``no_gold`` is a
    rubric-judged row with no gold string at all (five of BEAM's ten
    question types — 200 of the 400 rows in the 2026-08-21 artifact),
    ``trivial_gold`` is a gold so short or generic that containment would
    fire by chance."""
    needle = normalize(answer)
    if not needle:
        return "no_gold"
    if len(needle) < MIN_ANSWER_CHARS or needle in TRIVIAL_ANSWERS:
        return "trivial_gold"
    return None


def answer_present(haystack, answer) -> bool | None:
    """Is the gold answer already in ``haystack``?

    Word-boundary containment over the normalised strings — ``Portland``
    does not match ``the port is open``. Returns None when the gold is
    untestable (see ``untestable_reason``), which the caller must report
    rather than treat as clean.
    """
    if untestable_reason(answer):
        return None
    return f" {normalize(haystack)} ".find(f" {normalize(answer)} ") != -1


def row_id(row: dict, position: int = 0) -> str:
    """A stable label for the row, in whichever harness wrote it."""
    if row.get("question_id"):
        return str(row["question_id"])
    if row.get("chat_id") is not None and "type" in row:
        return f"{row['chat_id']}/{row['type']}[{row.get('index', 0)}]"
    return str(position)


def gold_answer(row: dict) -> str:
    return row.get("reference_answer", row.get("answer", "")) or ""


def check_row(row: dict, *, position: int = 0,
              context_free_arms=CONTEXT_FREE_ARMS) -> dict:
    answer = gold_answer(row)
    if FLAG_KEY in row:
        # A run that checked at answer time wins over a re-derivation: it
        # saw the exact question text it served.
        question_leak = row[FLAG_KEY]
    else:
        question_leak = answer_present(row.get("question", ""), answer)
    sites = ["question"] if question_leak else []
    contexts = row.get("contexts") or {}
    for arm in context_free_arms:
        ctx = contexts.get(arm) or ""
        if ctx and answer_present(ctx, answer):
            sites.append(f"{arm}_context")
    return {"id": row_id(row, position), "leak": bool(sites), "sites": sites,
            "testable": question_leak is not None}


def _arm_value(row: dict, arm: str) -> float | None:
    if f"{arm}_score" in row:
        return float(row[f"{arm}_score"])
    if f"{arm}_correct" in row:
        return 1.0 if row[f"{arm}_correct"] else 0.0
    return None


def _arms(rows: list[dict]) -> list[str]:
    found: set[str] = set()
    for row in rows:
        for key in row:
            m = _ARM_SCORE_RE.match(key)
            if m and isinstance(row[key], (int, float, bool)):
                found.add(m.group("arm"))
    return sorted(found)


def check_rows(rows: list[dict], source: str | None = None) -> dict:
    verdicts = [check_row(r, position=i) for i, r in enumerate(rows)]
    leaked = [v["id"] for v in verdicts if v["leak"]]
    untestable = [v["id"] for v in verdicts if not v["testable"]]
    clean = [r for r, v in zip(rows, verdicts) if not v["leak"]]
    # Untestable rows are not leaked, so they sit inside `clean` and its
    # mean is partly over rows this check could not examine. The
    # testable-only subset is what the check can actually stand behind.
    testable = [r for r, v in zip(rows, verdicts)
                if not v["leak"] and v["testable"]]
    reasons: dict[str, int] = {}
    for row, verdict in zip(rows, verdicts):
        if not verdict["testable"]:
            why = untestable_reason(gold_answer(row)) or "flagged_untestable"
            reasons[why] = reasons.get(why, 0) + 1
    summary: dict = {
        "check": "gold-answer leak (SR-TTT arXiv 2603.06642)",
        "n_rows": len(rows),
        "n_leaked": len(leaked),
        "n_untestable": len(untestable),
        "untestable_reasons": reasons,
        "leaked": leaked,
        "untestable": untestable,
        "sites": {v["id"]: v["sites"] for v in verdicts if v["leak"]},
        "min_answer_chars": MIN_ANSWER_CHARS,
        "arms": {},
    }
    if source:
        summary["source"] = source
    for arm in _arms(rows):
        allv = [v for v in (_arm_value(r, arm) for r in rows) if v is not None]
        freev = [v for v in (_arm_value(r, arm) for r in clean)
                 if v is not None]
        testv = [v for v in (_arm_value(r, arm) for r in testable)
                 if v is not None]
        summary["arms"][arm] = {
            "n": len(allv),
            "all": round(sum(allv) / len(allv), 4) if allv else None,
            "n_leak_free": len(freev),
            "leak_free": (round(sum(freev) / len(freev), 4) if freev
                          else None),
            "n_testable": len(testv),
            "leak_free_testable": (round(sum(testv) / len(testv), 4)
                                   if testv else None),
        }
    return summary


def load_rows(path: Path) -> list[dict]:
    """JSONL (one row per line, the harness default) or a JSON list."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.lstrip().startswith("["):
        return json.loads(text)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def default_out(path: Path) -> Path:
    return path.with_name(path.name.removesuffix(".jsonl").removesuffix(".json")
                          + ".leakcheck.json")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="in_path", required=True,
                    help="judged artifact (.jsonl rows or a .json list)")
    ap.add_argument("--out", default=None,
                    help="report path (default: <artifact>.leakcheck.json)")
    args = ap.parse_args(argv)
    src = Path(args.in_path)
    rows = load_rows(src)
    summary = check_rows(rows, source=src.name)
    out = Path(args.out) if args.out else default_out(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 1 if summary["n_leaked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
