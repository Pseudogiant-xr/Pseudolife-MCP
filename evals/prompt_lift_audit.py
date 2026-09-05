"""Blast-radius audit for worked examples lifted from the benchmark corpus.

The extraction prompt's count-exclusion example names the gold answer of
LongMemEval ``affe2881`` (see ``tests/test_prompt_example_lifts.py`` for the
inventory of lifts and the guard). This script measures what that did to the
committed numbers, from the committed per-question rows only, and writes one
artifact so the CHANGELOG's claims about it are backed by a file rather than
a terminal:

* leave-one-out rows for every doc-cited artifact whose extractor ran under a
  prompt carrying the example (drop the question, recompute over N-1) — both
  for ``affe2881`` alone and for both knowledge-update collisions;
* ``affe2881``'s cortex verdict in every committed LongMemEval run, split by
  dataset variant and by whether the run predates the example (the ``_s``
  rows are the negative control). A "run" here is one committed artifact;
  replicate files and knob sweeps of the same configuration each count;
* the e4b-v3 sidecar's train-on-test overlap with the oracle-500 answer
  sessions, by question type (the distillation guard holds out KU only).

Run dates are the artifact's git add-date (a sound upper bound), read in one
``git log`` walk; a file whose add commit is not in the local history stops
the script rather than being bucketed — a shallow clone cannot produce this
artifact. Row loading and accuracy come from ``replicate`` so the numbers are
the harness's own. No GPU, no model call, no network. Usage::

    python evals/prompt_lift_audit.py
"""
from __future__ import annotations

import collections
import functools
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "evals" / "results"
DATA = REPO / "evals" / "data"
OUT = RESULTS / "prompt-example-lift-audit-20260905.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replicate import accuracy, is_judged, load_rows  # noqa: E402

# The example entered the shipped prompt and the shim files in one commit.
BRIGHT_LINE = "2026-08-01"
QIDS = {
    "affe2881": "knowledge-update; gold '32'; the count-exclusion example states it",
    "89941a94": "knowledge-update; gold 'Yes. (You have a road bike too.)'",
    "gpt4_e414231f": "temporal-reasoning; gold 'road bike'",
    "2b8f3739": "multi-session; gold '$495'; the events_pass_v2 jam example is its answer turn",
}
# Doc-cited artifacts whose fact spine was extracted under a carrying prompt.
DOC_CITED = [
    "longmemeval-all-oracle-qwen-27b-alltypes-0803",
    "longmemeval-ku-oracle-qwen-27b-ceiling-v38",
    "longmemeval-ku-oracle-qwen-27b-raglite-v38",
    "longmemeval-all-oracle-qwen-27b-raglite-all-fresh",
    "longmemeval-ku-oracle-e4b-ft-arm1-mtp",
    "longmemeval-ku-oracle-opus-5-opusv2-0802",
    "longmemeval-ku-oracle-sonnet-5-sonnetv2-0802",
    "longmemeval-ku-oracle-sonnet-5-sonnetv3-0802",
]
ARMS = ("rag", "cortex", "hybrid")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@functools.lru_cache(maxsize=None)
def _rows(tag: str) -> list[dict]:
    rows = load_rows(RESULTS / f"{tag}.jsonl")
    if not rows or not all(is_judged(r) for r in rows):
        raise RuntimeError(f"{tag}: missing or not fully judged; refusing to average it")
    return rows


@functools.lru_cache(maxsize=None)
def _add_dates() -> dict[str, str]:
    """Repo-relative path -> date of the commit that ADDED it, one git walk.

    Newest-first output, so a path added twice keeps its oldest add-date."""
    out = subprocess.run(
        ["git", "log", "--diff-filter=A", "--name-only", "--format=%ad",
         "--date=short", "--", "evals/results/"],
        cwd=REPO, capture_output=True, text=True, check=True).stdout
    dates: dict[str, str] = {}
    current = None
    for line in out.splitlines():
        line = line.strip()
        if _DATE.match(line):
            current = line
        elif line and current:
            dates[line.replace("\\", "/")] = current
    return dates


def _add_date(rel: str) -> str:
    try:
        return _add_dates()[rel]
    except KeyError:
        raise RuntimeError(
            f"{rel}: no add commit in the local history (shallow clone?); the "
            "pre/post split cannot be computed") from None


def _acc(rows: list[dict], arm: str) -> float:
    return round(accuracy(rows, arm), 4)


def _accs(rows: list[dict]) -> dict[str, float]:
    return {arm: _acc(rows, arm) for arm in ARMS}


def leave_one_out() -> dict:
    out = {}
    for tag in DOC_CITED:
        rows = _rows(tag)
        hit = next((r for r in rows if r["question_id"] == "affe2881"), None)
        entry = {
            "n": len(rows),
            "added": _add_date(f"evals/results/{tag}.jsonl"),
            "affe2881_verdicts": {a: hit.get(f"{a}_correct") for a in ARMS} if hit else {},
            "published": _accs(rows),
        }
        for label, drop in (("loo_affe2881", {"affe2881"}),
                            ("loo_both_ku_collisions", {"affe2881", "89941a94"})):
            kept = [r for r in rows if r["question_id"] not in drop]
            entry[label] = {"n": len(kept), **_accs(kept)}
        ku = [r for r in rows if r.get("question_type") == "knowledge-update"]
        if ku and len(rows) > len(ku):
            kept = [r for r in ku if r["question_id"] != "affe2881"]
            entry["ku_subrow"] = {"n": len(ku), "published": _accs(ku),
                                  "loo_affe2881": {"n": len(kept), **_accs(kept)}}
        out[tag] = entry
    return out


def affe2881_by_variant_and_era() -> dict:
    cells: dict = collections.defaultdict(
        lambda: {"cortex_correct": 0, "runs": 0, "cortex_wrong": []})
    for path in sorted(RESULTS.glob("longmemeval-*.jsonl")):
        rows = load_rows(path)
        hit = next((r for r in rows if r.get("question_id") == "affe2881"), None)
        if hit is None:
            continue
        name = path.name
        variant = ("s_full_haystack" if "-ku-s-" in name
                   else "oracle_500" if "-all-oracle-" in name else "oracle_ku78")
        era = "pre" if _add_date(f"evals/results/{name}") < BRIGHT_LINE else "post"
        cell = cells[f"{variant}/{era}"]
        cell["runs"] += 1
        if hit.get("cortex_correct") is True:
            cell["cortex_correct"] += 1
        else:
            cell["cortex_wrong"].append(name)
    return {k: {**v, "cortex_wrong": sorted(v["cortex_wrong"])}
            for k, v in sorted(cells.items())}


def train_on_test_overlap() -> dict | None:
    sets = ["distill-extract-opus1.jsonl", "distill-events-opus1.jsonl"]
    oracle = DATA / "longmemeval_oracle.json"
    if not oracle.exists() or not all((DATA / s).exists() for s in sets):
        return None
    sids: set[str] = set()
    for s in sets:
        with (DATA / s).open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rid = json.loads(line)["id"]
                if ":" not in rid:
                    raise RuntimeError(f"{s}: row id {rid!r} is not 'qid:sid'")
                sids.add(rid.split(":", 1)[1])
    rows = json.loads(oracle.read_text(encoding="utf-8"))
    by: dict = collections.defaultdict(lambda: {"answer_session_in_training": 0, "n": 0})
    for r in rows:
        cell = by[r["question_type"]]
        cell["n"] += 1
        if set(r.get("answer_session_ids", [])) & sids:
            cell["answer_session_in_training"] += 1
    return {"training_sets": sets, "training_sessions": len(sids),
            "by_type": dict(sorted(by.items())),
            "total": sum(v["answer_session_in_training"] for v in by.values())}


def main() -> None:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                          capture_output=True, text=True, check=False).stdout.strip()
    artifact = {
        "audit": "prompt-example-lift-blast-radius",
        "date": "2026-09-05",
        "code": head,
        "method": ("leave-one-out from committed per-question rows; run era by git "
                   "add-date vs the commit that added the example (a4686df6, "
                   f"{BRIGHT_LINE}; the op block a day earlier, e4776729); one run = "
                   "one committed artifact, replicates and sweeps included"),
        "questions": QIDS,
        "leave_one_out": leave_one_out(),
        "affe2881_cortex_by_variant_and_era": affe2881_by_variant_and_era(),
        "e4b_v3_train_on_test_overlap": train_on_test_overlap(),
    }
    OUT.write_text(json.dumps(artifact, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
