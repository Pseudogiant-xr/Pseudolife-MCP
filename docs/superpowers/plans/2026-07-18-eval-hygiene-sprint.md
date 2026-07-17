# Eval-Hygiene Sprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a replication layer (mean±std + paired permutation test) over the LongMemEval bench, a local regression gate, and variance-honest docs; then re-verify the shipped Arm-1 extractor decision with 4 fresh replicates per config.

**Architecture:** One new import-light module `evals/replicate.py` (pure stats/file functions at top, bench imports lazy inside the `run` command) driven by two PowerShell orchestrators that follow the existing `gate_e4b_ft.ps1` server-lifecycle pattern. Replicates are judged JSONLs copied with judge fields stripped under strict `-rN` tags, then answer-phased by the existing bench.

**Tech Stack:** Python 3.10+ stdlib only (argparse/json/random/re/statistics/subprocess), pytest, PowerShell 7.

**Spec:** `docs/superpowers/specs/2026-07-18-eval-hygiene-sprint-design.md` — read it first.

## Global Constraints

- No new dependencies. `evals/replicate.py` must import without torch/ladder_sweep (pure stdlib at module import; bench imports only inside `cmd_run`).
- Never touch the live bank; everything here reads/writes `evals/results/` only.
- Replicate tag convention: base tag `arm1` → replicates `arm1-r2`…; empty base tag → `r2`…. Discovery accepts ONLY a strict `-r<digits>` suffix on the base filename stem (so `arm1` never matches `arm1-baseline` or `arm1-gate`).
- Result-file naming mirrors `longmemeval_bench.out_file`: `longmemeval-ku-{dataset}-{extractor}{-tag}.jsonl` in `evals/results/`. Summary/agg use `.removesuffix(".jsonl")` — never `with_suffix` (extractor names contain dots, e.g. `qwen3.5-4b`).
- Tests run offline: `HF_HUB_OFFLINE=1` env, no endpoints, no GPU, no Postgres.
- Commits: conventional style, end message with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Windows host; run pytest via `.venv\Scripts\python.exe -m pytest`.
- Pinned gate config: dataset `oracle`, extractor `e4b-ft`, source tag `arm1`, gate namespace tag `arm1-gate`.

---

### Task 1: `evals/replicate.py` core — naming, IO, strip, discovery

**Files:**
- Create: `evals/replicate.py`
- Create: `tests/test_eval_replicate.py`

**Interfaces (Produces):**
- `result_file(dataset: str, extractor: str, tag: str = "", results_dir: Path = RESULTS_DIR) -> Path`
- `replicate_tag(base_tag: str, i: int) -> str`
- `load_rows(path: Path) -> list[dict]` / `write_rows(path: Path, rows: list[dict]) -> None`
- `is_judged(row: dict) -> bool`
- `strip_judged(rows: list[dict]) -> list[dict]`
- `discover(dataset, extractor, tag, results_dir) -> dict[str, Path]` — maps replicate tag → path, base file included under its own tag (`""` for untagged)
- Module constants: `ARMS = ("rag", "cortex", "hybrid")`, `RESULTS_DIR`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_eval_replicate.py`:

```python
"""Tests for evals/replicate.py — the replication/variance layer.

Pure-function tests only: no endpoints, no GPU, no Postgres. The module
must import without pulling ladder_sweep/torch (that is itself asserted).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import replicate  # noqa: E402


def _row(qid: str, judged: bool = True, correct: bool = True) -> dict:
    row = {
        "question_id": qid,
        "question": "q?",
        "answer": "a",
        "question_date": "2023/01/01",
        "contexts": {"rag": "r", "cortex": "c", "hybrid": "h"},
    }
    if judged:
        for arm in replicate.ARMS:
            row[f"{arm}_response"] = "resp"
            row[f"{arm}_correct"] = correct
            row[f"{arm}_context_tokens"] = 100
    return row


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                    encoding="utf-8")


def test_module_imports_light():
    assert "ladder_sweep" not in sys.modules
    assert "torch" not in sys.modules


def test_result_file_matches_bench_convention(tmp_path):
    assert replicate.result_file("oracle", "e4b-ft", "arm1", tmp_path) == \
        tmp_path / "longmemeval-ku-oracle-e4b-ft-arm1.jsonl"
    assert replicate.result_file("oracle", "qwen-27b", "", tmp_path) == \
        tmp_path / "longmemeval-ku-oracle-qwen-27b.jsonl"


def test_replicate_tag():
    assert replicate.replicate_tag("arm1", 2) == "arm1-r2"
    assert replicate.replicate_tag("", 2) == "r2"


def test_strip_judged_removes_only_judge_fields():
    stripped = replicate.strip_judged([_row("q1")])[0]
    for arm in replicate.ARMS:
        assert f"{arm}_correct" not in stripped
        assert f"{arm}_response" not in stripped
        assert f"{arm}_context_tokens" not in stripped
    assert stripped["question_id"] == "q1"
    assert stripped["contexts"] == {"rag": "r", "cortex": "c", "hybrid": "h"}
    assert replicate.is_judged(_row("q1")) is True
    assert replicate.is_judged(stripped) is False


def test_discover_strict_suffix(tmp_path):
    rows = [_row("q1")]
    for name in [
        "longmemeval-ku-oracle-e4b-ft-arm1.jsonl",
        "longmemeval-ku-oracle-e4b-ft-arm1-r2.jsonl",
        "longmemeval-ku-oracle-e4b-ft-arm1-r10.jsonl",
        "longmemeval-ku-oracle-e4b-ft-arm1-baseline.jsonl",   # must NOT match
        "longmemeval-ku-oracle-e4b-ft-arm1-gate.jsonl",       # must NOT match
        "longmemeval-ku-oracle-e4b-ft-arm1-rx.jsonl",         # must NOT match
    ]:
        _write_jsonl(tmp_path / name, rows)
    found = replicate.discover("oracle", "e4b-ft", "arm1", tmp_path)
    assert sorted(found) == ["arm1", "arm1-r10", "arm1-r2"]


def test_discover_untagged_base(tmp_path):
    _write_jsonl(tmp_path / "longmemeval-ku-oracle-qwen-27b.jsonl", [_row("q1")])
    _write_jsonl(tmp_path / "longmemeval-ku-oracle-qwen-27b-r2.jsonl", [_row("q1")])
    found = replicate.discover("oracle", "qwen-27b", "", tmp_path)
    assert sorted(found) == ["", "r2"]


def test_load_rows_tolerates_blank_and_bad_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"question_id": "q1"}\n\nnot json\n', encoding="utf-8")
    rows = replicate.load_rows(p)
    assert [r["question_id"] for r in rows] == ["q1"]
    assert replicate.load_rows(tmp_path / "missing.jsonl") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_replicate.py -v`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'replicate'`

- [ ] **Step 3: Write the implementation**

Create `evals/replicate.py`:

```python
"""Replication + variance tooling over ``longmemeval_bench`` results.

The bench reports single runs as point estimates, but three runs of the
identical sonnet-5-v1 config (byte-identical contexts, temperature 0)
scored cortex 0.808/0.731/0.782 — judge-side noise wider than most
differences being decided. This module makes replication cheap and the
statistics honest:

    spawn    copy a judged JSONL, judge fields stripped, under -rN tags
    run      answer-phase every pending replicate (needs the Qwen endpoint)
    agg      aggregate replicates -> <base>.agg.json with mean +/- std
    compare  paired permutation test between two configs (by question_id)
    copy     strip-copy one file to a new tag (regression-gate fallback)
    gate-check  compare replicate means against the committed baseline
    baseline    (re)establish evals/results/regression_gate.baseline.json

Import-light by design: the bench module (and through it ladder_sweep /
torch) is imported ONLY inside ``cmd_run``. Naming/IO helpers are small
local mirrors of the bench's, kept in lockstep by tests.

Spec: docs/superpowers/specs/2026-07-18-eval-hygiene-sprint-design.md
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("rag", "cortex", "hybrid")
_JUDGE_FIELDS = {f"{arm}_{s}" for arm in ARMS
                 for s in ("response", "correct", "context_tokens")}
_REPLICA_SUFFIX = re.compile(r"-r\d+$")
BASELINE_FLOOR = 0.03
DEFAULT_BASELINE = RESULTS_DIR / "regression_gate.baseline.json"


# ── naming (mirrors longmemeval_bench.out_file — duplicated because that
#    module imports ladder_sweep/torch at module level) ────────────────────
def result_file(dataset: str, extractor: str, tag: str = "",
                results_dir: Path = RESULTS_DIR) -> Path:
    suffix = f"-{tag}" if tag else ""
    return results_dir / f"longmemeval-ku-{dataset}-{extractor}{suffix}.jsonl"


def replicate_tag(base_tag: str, i: int) -> str:
    return f"{base_tag}-r{i}" if base_tag else f"r{i}"


# ── row IO (mirrors the bench's tolerant JSONL semantics) ─────────────────
def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def is_judged(row: dict) -> bool:
    return all(f"{arm}_correct" in row for arm in ARMS)


def strip_judged(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k not in _JUDGE_FIELDS}
            for r in rows]


def discover(dataset: str, extractor: str, tag: str = "",
             results_dir: Path = RESULTS_DIR) -> dict[str, Path]:
    """Replicate tag -> path: the base file plus strict ``-r<digits>``
    variants. ``arm1`` never matches ``arm1-baseline`` or ``arm1-gate``."""
    base = result_file(dataset, extractor, tag, results_dir)
    stem = base.name.removesuffix(".jsonl")
    found: dict[str, Path] = {}
    if base.exists():
        found[tag] = base
    for p in sorted(results_dir.glob(stem + "-r*.jsonl")):
        rest = p.name.removesuffix(".jsonl")[len(stem):]
        if _REPLICA_SUFFIX.fullmatch(rest):
            found[f"{tag}{rest}" if tag else rest[1:]] = p
    return found
```

(The CLI and stats functions land in Tasks 2–5; for now the module ends here.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_replicate.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add evals/replicate.py tests/test_eval_replicate.py
git commit -m "feat(evals): replicate.py core — naming, IO, strip, strict -rN discovery

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: statistics — `aggregate`, `question_rates`, `paired_permutation`

**Files:**
- Modify: `evals/replicate.py` (append after `discover`)
- Modify: `tests/test_eval_replicate.py` (append)

**Interfaces:**
- Consumes: `ARMS`, `is_judged` (Task 1)
- Produces:
  - `accuracy(rows: list[dict], arm: str) -> float | None`
  - `aggregate(rows_by_tag: dict[str, list[dict]]) -> dict` — `{"n_replicates", "replicates", "n_questions", "arms": {arm: {"accuracies", "mean", "std"}}}`; only fully-judged replicates counted; `std` is sample std (`ddof=1`), `None` when fewer than 2 replicates
  - `question_rates(rows_by_tag, arm) -> dict[str, float]` — qid → mean correctness across replicates; raises `ValueError` on mismatched qid sets between replicates
  - `paired_permutation(a_rates, b_rates, n=10000, seed=0) -> dict` — `{"delta", "p_value", "n_questions"}`; raises `ValueError` on mismatched qid sets

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_replicate.py`:

```python
def test_aggregate_math():
    # 4 questions; r1 = 4/4 correct, r2 = 2/4 correct -> mean 0.75
    r1 = [_row(f"q{i}", correct=True) for i in range(4)]
    r2 = ([_row("q0", correct=True), _row("q1", correct=True),
           _row("q2", correct=False), _row("q3", correct=False)])
    agg = replicate.aggregate({"arm1": r1, "arm1-r2": r2})
    assert agg["n_replicates"] == 2
    assert agg["n_questions"] == 4
    assert agg["replicates"] == ["arm1", "arm1-r2"]
    for arm in replicate.ARMS:
        assert agg["arms"][arm]["accuracies"] == [1.0, 0.5]
        assert agg["arms"][arm]["mean"] == pytest.approx(0.75)
        assert agg["arms"][arm]["std"] == pytest.approx(
            statistics.stdev([1.0, 0.5]))


def test_aggregate_skips_unjudged_replicate():
    r1 = [_row("q0"), _row("q1")]
    pending = [_row("q0", judged=False), _row("q1", judged=False)]
    agg = replicate.aggregate({"arm1": r1, "arm1-r2": pending})
    assert agg["n_replicates"] == 1
    assert agg["arms"]["cortex"]["std"] is None


def test_question_rates_and_mismatch():
    r1 = [_row("q0", correct=True), _row("q1", correct=False)]
    r2 = [_row("q0", correct=False), _row("q1", correct=False)]
    rates = replicate.question_rates({"a": r1, "a-r2": r2}, "cortex")
    assert rates == {"q0": 0.5, "q1": 0.0}
    with pytest.raises(ValueError, match="question sets"):
        replicate.question_rates({"a": r1, "a-r2": [_row("qX")]}, "cortex")


def test_paired_permutation_null_and_signal():
    rng = random.Random(42)
    qids = [f"q{i}" for i in range(78)]
    a = {q: rng.random() for q in qids}
    null = replicate.paired_permutation(a, dict(a))
    assert null["delta"] == 0.0
    assert null["p_value"] > 0.9            # identical sides: no effect
    b = {q: max(0.0, a[q] - 0.3) for q in qids}
    sig = replicate.paired_permutation(a, b)
    assert sig["delta"] > 0.2
    assert sig["p_value"] < 0.01
    assert sig["n_questions"] == 78
    # deterministic under the fixed default seed
    assert replicate.paired_permutation(a, b) == sig
    with pytest.raises(ValueError, match="question sets"):
        replicate.paired_permutation(a, {"other": 1.0})
```

Also add the imports at the top of the test file (with the existing ones):

```python
import random
import statistics
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_replicate.py -v -k "aggregate or rates or permutation"`
Expected: FAIL — `AttributeError: module 'replicate' has no attribute 'aggregate'`

- [ ] **Step 3: Write the implementation**

Append to `evals/replicate.py`:

```python
# ── statistics ────────────────────────────────────────────────────────────
def accuracy(rows: list[dict], arm: str) -> float | None:
    judged = [r for r in rows if is_judged(r)]
    if not judged:
        return None
    return sum(bool(r[f"{arm}_correct"]) for r in judged) / len(judged)


def aggregate(rows_by_tag: dict[str, list[dict]]) -> dict:
    judged = {t: rows for t, rows in rows_by_tag.items()
              if rows and all(is_judged(r) for r in rows)}
    tags = sorted(judged)
    out = {
        "n_replicates": len(tags),
        "replicates": tags,
        "n_questions": len(judged[tags[0]]) if tags else 0,
        "arms": {},
    }
    for arm in ARMS:
        accs = [round(accuracy(judged[t], arm), 4) for t in tags]
        out["arms"][arm] = {
            "accuracies": accs,
            "mean": round(statistics.fmean(accs), 4) if accs else None,
            "std": round(statistics.stdev(accs), 4) if len(accs) >= 2
            else None,
        }
    return out


def question_rates(rows_by_tag: dict[str, list[dict]],
                   arm: str) -> dict[str, float]:
    judged = {t: rows for t, rows in rows_by_tag.items()
              if rows and all(is_judged(r) for r in rows)}
    per_q: dict[str, list[bool]] = {}
    qid_sets = []
    for rows in judged.values():
        qid_sets.append({r["question_id"] for r in rows})
        for r in rows:
            per_q.setdefault(r["question_id"], []).append(
                bool(r[f"{arm}_correct"]))
    if len({frozenset(s) for s in qid_sets}) > 1:
        raise ValueError("question sets differ between replicates")
    return {q: statistics.fmean(v) for q, v in per_q.items()}


def paired_permutation(a_rates: dict[str, float], b_rates: dict[str, float],
                       n: int = 10000, seed: int = 0) -> dict:
    if set(a_rates) != set(b_rates):
        raise ValueError("question sets differ between configs")
    diffs = [a_rates[q] - b_rates[q] for q in sorted(a_rates)]
    observed = statistics.fmean(diffs)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n):
        flipped = statistics.fmean(
            d if rng.random() < 0.5 else -d for d in diffs)
        if abs(flipped) >= abs(observed) - 1e-12:
            hits += 1
    return {"delta": round(observed, 4),
            "p_value": round((hits + 1) / (n + 1), 5),
            "n_questions": len(diffs)}
```

- [ ] **Step 4: Run all module tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_replicate.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add evals/replicate.py tests/test_eval_replicate.py
git commit -m "feat(evals): mean±std aggregation and paired permutation test

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: gate verdict + baseline construction

**Files:**
- Modify: `evals/replicate.py` (append)
- Modify: `tests/test_eval_replicate.py` (append)

**Interfaces:**
- Consumes: `aggregate` output shape (Task 2), `BASELINE_FLOOR` (Task 1)
- Produces:
  - `gate_verdict(agg: dict, baseline: dict) -> list[str]` — empty list = pass; one message per failing arm
  - `make_baseline(agg: dict, commit: str, floor: float = BASELINE_FLOOR) -> dict` — `{"established_at", "commit", "n_replicates", "arms": {arm: {"mean", "std", "margin"}}}` with `margin = max(floor, 2*std)` (std `None` → floor)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_replicate.py`:

```python
def _agg(mean: float, std: float = 0.02) -> dict:
    return {"n_replicates": 3, "replicates": ["t", "t-r2", "t-r3"],
            "n_questions": 78,
            "arms": {arm: {"accuracies": [mean], "mean": mean, "std": std}
                     for arm in replicate.ARMS}}


def test_make_baseline_margins():
    base = replicate.make_baseline(_agg(0.7, std=0.04), commit="abc1234")
    assert base["commit"] == "abc1234"
    assert base["arms"]["cortex"] == {"mean": 0.7, "std": 0.04,
                                      "margin": 0.08}
    tight = replicate.make_baseline(_agg(0.7, std=0.005), commit="abc")
    assert tight["arms"]["cortex"]["margin"] == replicate.BASELINE_FLOOR
    single = replicate.make_baseline(
        {**_agg(0.7), "arms": {a: {"accuracies": [0.7], "mean": 0.7,
                                   "std": None}
                               for a in replicate.ARMS}}, commit="abc")
    assert single["arms"]["cortex"]["margin"] == replicate.BASELINE_FLOOR


def test_gate_verdict():
    baseline = replicate.make_baseline(_agg(0.70, std=0.02), commit="abc")
    assert replicate.gate_verdict(_agg(0.70), baseline) == []
    assert replicate.gate_verdict(_agg(0.67), baseline) == []   # inside margin
    failures = replicate.gate_verdict(_agg(0.60), baseline)
    assert len(failures) == len(replicate.ARMS)
    assert "cortex" in " ".join(failures)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_replicate.py -v -k "baseline or verdict"`
Expected: FAIL — `AttributeError: module 'replicate' has no attribute 'make_baseline'`

- [ ] **Step 3: Write the implementation**

Append to `evals/replicate.py`:

```python
# ── regression gate ───────────────────────────────────────────────────────
def make_baseline(agg: dict, commit: str,
                  floor: float = BASELINE_FLOOR) -> dict:
    arms = {}
    for arm, a in agg["arms"].items():
        margin = max(floor, 2 * (a["std"] or 0.0))
        arms[arm] = {"mean": a["mean"], "std": a["std"],
                     "margin": round(margin, 4)}
    return {"established_at": datetime.now().isoformat(timespec="seconds"),
            "commit": commit, "n_replicates": agg["n_replicates"],
            "arms": arms}


def gate_verdict(agg: dict, baseline: dict) -> list[str]:
    failures = []
    for arm, b in baseline["arms"].items():
        cur = agg["arms"][arm]["mean"]
        if cur is None or cur < b["mean"] - b["margin"]:
            failures.append(
                f"{arm}: mean {cur} < baseline {b['mean']} - "
                f"margin {b['margin']}")
    return failures
```

- [ ] **Step 4: Run all module tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_replicate.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add evals/replicate.py tests/test_eval_replicate.py
git commit -m "feat(evals): gate verdict + noise-margin baseline construction

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: CLI — `spawn`, `copy`, `agg`

**Files:**
- Modify: `evals/replicate.py` (append)
- Modify: `tests/test_eval_replicate.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1–2
- Produces: `main(argv: list[str] | None = None) -> int` with subcommands. All subcommands share `--dataset` (default `oracle`), `--extractor` (required), `--tag` (default `""`), `--results-dir` (default `RESULTS_DIR`, for tests). Errors exit nonzero via `sys.exit(str)`.
  - `spawn -n N` — requires fully-judged base file; creates missing `-r2`…`-r<N+1>` files with judge fields stripped; never overwrites
  - `copy --to-tag T` — strip-copy the base file to tag T (gate fallback); refuses to overwrite
  - `agg` — aggregates discovered replicates, writes `<base>.agg.json`, prints a table
- Later tasks rely on: `cmd_agg` writing the agg JSON exactly at `result_file(...).with_name(stem + ".agg.json")`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_replicate.py`:

```python
def _seed_base(tmp_path, tag="arm1", n_rows=3, extractor="e4b-ft"):
    rows = [_row(f"q{i}") for i in range(n_rows)]
    _write_jsonl(replicate.result_file("oracle", extractor, tag, tmp_path),
                 rows)
    return rows


def test_cli_spawn_creates_stripped_replicates(tmp_path):
    _seed_base(tmp_path)
    rc = replicate.main(["spawn", "--extractor", "e4b-ft", "--tag", "arm1",
                         "-n", "2", "--results-dir", str(tmp_path)])
    assert rc == 0
    for i in (2, 3):
        rows = replicate.load_rows(replicate.result_file(
            "oracle", "e4b-ft", f"arm1-r{i}", tmp_path))
        assert len(rows) == 3
        assert not any(replicate.is_judged(r) for r in rows)
    # idempotent: re-spawn leaves existing files alone
    marker = replicate.result_file("oracle", "e4b-ft", "arm1-r2", tmp_path)
    before = marker.read_text(encoding="utf-8")
    assert replicate.main(["spawn", "--extractor", "e4b-ft", "--tag", "arm1",
                           "-n", "2", "--results-dir", str(tmp_path)]) == 0
    assert marker.read_text(encoding="utf-8") == before


def test_cli_spawn_rejects_unjudged_source(tmp_path):
    _write_jsonl(replicate.result_file("oracle", "e4b-ft", "arm1", tmp_path),
                 [_row("q0", judged=False)])
    with pytest.raises(SystemExit):
        replicate.main(["spawn", "--extractor", "e4b-ft", "--tag", "arm1",
                        "-n", "1", "--results-dir", str(tmp_path)])


def test_cli_copy(tmp_path):
    _seed_base(tmp_path)
    rc = replicate.main(["copy", "--extractor", "e4b-ft", "--tag", "arm1",
                         "--to-tag", "arm1-gate",
                         "--results-dir", str(tmp_path)])
    assert rc == 0
    rows = replicate.load_rows(replicate.result_file(
        "oracle", "e4b-ft", "arm1-gate", tmp_path))
    assert len(rows) == 3 and not any(replicate.is_judged(r) for r in rows)
    with pytest.raises(SystemExit):        # refuses to overwrite
        replicate.main(["copy", "--extractor", "e4b-ft", "--tag", "arm1",
                        "--to-tag", "arm1-gate",
                        "--results-dir", str(tmp_path)])


def test_cli_agg_writes_agg_json(tmp_path):
    _seed_base(tmp_path)
    _write_jsonl(replicate.result_file("oracle", "e4b-ft", "arm1-r2",
                                       tmp_path),
                 [_row("q0", correct=False), _row("q1"), _row("q2")])
    rc = replicate.main(["agg", "--extractor", "e4b-ft", "--tag", "arm1",
                         "--results-dir", str(tmp_path)])
    assert rc == 0
    agg = json.loads((tmp_path /
                      "longmemeval-ku-oracle-e4b-ft-arm1.agg.json"
                      ).read_text(encoding="utf-8"))
    assert agg["n_replicates"] == 2
    assert agg["arms"]["cortex"]["accuracies"] == [1.0, 0.6667]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_replicate.py -v -k cli`
Expected: FAIL — `AttributeError: module 'replicate' has no attribute 'main'`

- [ ] **Step 3: Write the implementation**

Append to `evals/replicate.py`:

```python
# ── CLI ───────────────────────────────────────────────────────────────────
def _agg_path(base: Path) -> Path:
    # removesuffix, not with_suffix: extractor names contain dots.
    return base.with_name(base.name.removesuffix(".jsonl") + ".agg.json")


def _load_judged_source(args) -> list[dict]:
    src = result_file(args.dataset, args.extractor, args.tag,
                      args.results_dir)
    rows = load_rows(src)
    if not rows:
        sys.exit(f"source not found or empty: {src}")
    if not all(is_judged(r) for r in rows):
        sys.exit(f"source not fully judged: {src}")
    return rows


def cmd_spawn(args) -> int:
    rows = _load_judged_source(args)
    stripped = strip_judged(rows)
    for i in range(2, args.n + 2):
        dst = result_file(args.dataset, args.extractor,
                          replicate_tag(args.tag, i), args.results_dir)
        if dst.exists():
            print(f"exists, kept: {dst.name}")
            continue
        write_rows(dst, stripped)
        print(f"spawned: {dst.name}")
    return 0


def cmd_copy(args) -> int:
    rows = _load_judged_source(args)
    dst = result_file(args.dataset, args.extractor, args.to_tag,
                      args.results_dir)
    if dst.exists():
        sys.exit(f"refusing to overwrite: {dst}")
    write_rows(dst, strip_judged(rows))
    print(f"copied (stripped): {dst.name}")
    return 0


def cmd_agg(args) -> int:
    found = discover(args.dataset, args.extractor, args.tag,
                     args.results_dir)
    if not found:
        sys.exit("no result files found")
    agg = aggregate({t: load_rows(p) for t, p in found.items()})
    agg["source_files"] = [p.name for p in found.values()]
    base = result_file(args.dataset, args.extractor, args.tag,
                       args.results_dir)
    _agg_path(base).write_text(json.dumps(agg, indent=2), encoding="utf-8")
    label = f"{args.extractor}{f' [{args.tag}]' if args.tag else ''}"
    print(f"\n{args.dataset} / {label} — {agg['n_replicates']} replicates, "
          f"{agg['n_questions']} questions")
    print(f"{'arm':<10}{'mean':>8}{'std':>8}  accuracies")
    for arm in ARMS:
        a = agg["arms"][arm]
        std = f"{a['std']:.4f}" if a["std"] is not None else "-"
        print(f"{arm:<10}{a['mean']:>8.4f}{std:>8}  {a['accuracies']}")
    print(f"wrote {_agg_path(base).name}")
    return 0


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dataset", default="oracle")
    p.add_argument("--extractor", required=True)
    p.add_argument("--tag", default="")
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("spawn", help="create stripped replicate files")
    _common(p)
    p.add_argument("-n", type=int, default=4,
                   help="replicates to create beyond the original (r2..)")
    p.set_defaults(fn=cmd_spawn)

    p = sub.add_parser("copy", help="strip-copy the base file to a new tag")
    _common(p)
    p.add_argument("--to-tag", required=True)
    p.set_defaults(fn=cmd_copy)

    p = sub.add_parser("agg", help="aggregate replicates -> .agg.json")
    _common(p)
    p.set_defaults(fn=cmd_agg)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run all module tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_replicate.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add evals/replicate.py tests/test_eval_replicate.py
git commit -m "feat(evals): replicate CLI — spawn/copy/agg

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: CLI — `run`, `compare`, `gate-check`, `baseline`

**Files:**
- Modify: `evals/replicate.py` (append; also extend `main`)
- Modify: `tests/test_eval_replicate.py` (append)

**Interfaces:**
- Consumes: Tasks 1–4; `longmemeval_bench.run_answer(dataset, extractor_name, tag)` and `longmemeval_bench.report(dataset, extractor_name, tag)` (lazy import)
- Produces:
  - `run [--dry-run]` — answer-phases every discovered replicate with pending rows (base file included), then writes its summary; `--dry-run` prints pending tags without importing the bench
  - `compare --b-extractor X --b-tag Y --arm cortex` — paired permutation; `--b-*` default to the A side's values; exits 1 if either side has <2 judged replicates
  - `gate-check --baseline PATH` — exit 0 pass / 1 regression / 2 missing-or-invalid baseline (with establishment instructions)
  - `baseline --out PATH [--floor 0.03]` — writes the baseline file (git commit hash included)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_replicate.py`:

```python
def test_cli_run_dry_run(tmp_path, capsys):
    _seed_base(tmp_path)                                   # judged base
    _write_jsonl(replicate.result_file("oracle", "e4b-ft", "arm1-r2",
                                       tmp_path),
                 [_row("q0", judged=False)])               # pending
    rc = replicate.main(["run", "--extractor", "e4b-ft", "--tag", "arm1",
                         "--dry-run", "--results-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "arm1-r2" in out and "pending" in out
    assert "ladder_sweep" not in sys.modules               # still lazy


def _seed_pair(tmp_path):
    """arm1 clearly better than arm1-baseline, 2 judged replicates each."""
    n = 20
    good = [_row(f"q{i}", correct=(i % 10 != 0)) for i in range(n)]
    bad = [_row(f"q{i}", correct=(i % 2 == 0)) for i in range(n)]
    for tag, rows in [("arm1", good), ("arm1-r2", good),
                      ("arm1-baseline", bad), ("arm1-baseline-r2", bad)]:
        _write_jsonl(replicate.result_file("oracle", "e4b-ft", tag,
                                           tmp_path), rows)


def test_cli_compare(tmp_path, capsys):
    _seed_pair(tmp_path)
    rc = replicate.main(["compare", "--extractor", "e4b-ft", "--tag", "arm1",
                         "--b-tag", "arm1-baseline", "--arm", "cortex",
                         "--results-dir", str(tmp_path)])
    assert rc == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["delta"] == pytest.approx(0.9 - 0.5)
    assert result["p_value"] < 0.05
    assert result["a_mean"] == pytest.approx(0.9)
    assert result["b_mean"] == pytest.approx(0.5)


def test_cli_compare_requires_two_replicates(tmp_path):
    _seed_base(tmp_path)                                   # only r1 on A side
    _write_jsonl(replicate.result_file("oracle", "e4b-ft", "arm1-baseline",
                                       tmp_path), [_row("q0")])
    with pytest.raises(SystemExit):
        replicate.main(["compare", "--extractor", "e4b-ft", "--tag", "arm1",
                        "--b-tag", "arm1-baseline",
                        "--results-dir", str(tmp_path)])


def test_cli_gate_check_exit_codes(tmp_path):
    _seed_base(tmp_path, tag="arm1-gate")
    _write_jsonl(replicate.result_file("oracle", "e4b-ft", "arm1-gate-r2",
                                       tmp_path),
                 [_row(f"q{i}") for i in range(3)])
    baseline_path = tmp_path / "regression_gate.baseline.json"
    # missing baseline -> exit 2
    with pytest.raises(SystemExit) as e:
        replicate.main(["gate-check", "--extractor", "e4b-ft",
                        "--tag", "arm1-gate",
                        "--baseline", str(baseline_path),
                        "--results-dir", str(tmp_path)])
    assert e.value.code == 2
    # establish baseline at current perf (mean 1.0) -> pass
    assert replicate.main(["baseline", "--extractor", "e4b-ft",
                           "--tag", "arm1-gate",
                           "--out", str(baseline_path),
                           "--results-dir", str(tmp_path)]) == 0
    assert replicate.main(["gate-check", "--extractor", "e4b-ft",
                           "--tag", "arm1-gate",
                           "--baseline", str(baseline_path),
                           "--results-dir", str(tmp_path)]) == 0
    # regressed data -> exit 1
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    for arm in replicate.ARMS:
        baseline["arms"][arm]["mean"] = 1.5      # unreachable baseline
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        replicate.main(["gate-check", "--extractor", "e4b-ft",
                        "--tag", "arm1-gate",
                        "--baseline", str(baseline_path),
                        "--results-dir", str(tmp_path)])
    assert e.value.code == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_replicate.py -v -k "run_dry or compare or gate_check"`
Expected: FAIL — argparse error `invalid choice: 'run'`

- [ ] **Step 3: Write the implementation**

Append to `evals/replicate.py` (before `_common`):

```python
def cmd_run(args) -> int:
    found = discover(args.dataset, args.extractor, args.tag,
                     args.results_dir)
    if not found:
        sys.exit("no result files found")
    pending = [t for t, p in found.items()
               if any(not is_judged(r) for r in load_rows(p))]
    if not pending:
        print("nothing pending — all replicates judged")
        return 0
    print(f"pending replicates: {', '.join(pending)}")
    if args.dry_run:
        return 0
    if args.results_dir != RESULTS_DIR:
        sys.exit("run only operates on the real results dir "
                 "(the bench owns file placement)")
    from longmemeval_bench import report, run_answer  # noqa: PLC0415 — heavy
    for t in pending:
        run_answer(args.dataset, args.extractor, t)
        report(args.dataset, args.extractor, t)
    return 0


def _rates_for(args, extractor: str, tag: str, arm: str) -> dict[str, float]:
    found = discover(args.dataset, extractor, tag, args.results_dir)
    rows_by_tag = {t: load_rows(p) for t, p in found.items()}
    judged = {t: rows for t, rows in rows_by_tag.items()
              if rows and all(is_judged(r) for r in rows)}
    if len(judged) < 2:
        sys.exit(f"{extractor}/{tag or '(untagged)'}: need >=2 judged "
                 f"replicates, have {len(judged)} — run spawn/run first")
    return question_rates(judged, arm), aggregate(judged)


def cmd_compare(args) -> int:
    b_extractor = args.b_extractor or args.extractor
    a_rates, a_agg = _rates_for(args, args.extractor, args.tag, args.arm)
    b_rates, b_agg = _rates_for(args, b_extractor, args.b_tag, args.arm)
    result = paired_permutation(a_rates, b_rates,
                                n=args.permutations, seed=args.seed)
    result.update({
        "arm": args.arm,
        "a": f"{args.extractor}/{args.tag or '(untagged)'}",
        "b": f"{b_extractor}/{args.b_tag or '(untagged)'}",
        "a_mean": a_agg["arms"][args.arm]["mean"],
        "a_std": a_agg["arms"][args.arm]["std"],
        "b_mean": b_agg["arms"][args.arm]["mean"],
        "b_std": b_agg["arms"][args.arm]["std"],
    })
    print(json.dumps(result))
    return 0


def cmd_gate_check(args) -> int:
    if not args.baseline.exists():
        print(f"no baseline at {args.baseline}\n"
              "establish one on a known-good tree with:\n"
              "  evals\\regression_gate.ps1 -Establish")
        sys.exit(2)
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        baseline["arms"]
    except (ValueError, KeyError):
        print(f"invalid baseline file: {args.baseline}")
        sys.exit(2)
    found = discover(args.dataset, args.extractor, args.tag,
                     args.results_dir)
    agg = aggregate({t: load_rows(p) for t, p in found.items()})
    failures = gate_verdict(agg, baseline)
    if failures:
        print("REGRESSION GATE: FAIL")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print(f"REGRESSION GATE: PASS ({agg['n_replicates']} replicates vs "
          f"baseline {baseline['commit']})")
    return 0


def cmd_baseline(args) -> int:
    found = discover(args.dataset, args.extractor, args.tag,
                     args.results_dir)
    agg = aggregate({t: load_rows(p) for t, p in found.items()})
    if agg["n_replicates"] < 1:
        sys.exit("no judged replicates to establish a baseline from")
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True,
            text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    baseline = make_baseline(agg, commit, floor=args.floor)
    args.out.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    print(f"baseline established at {args.out} (commit {commit}, "
          f"{agg['n_replicates']} replicates)")
    return 0
```

And extend `main` with the four new subparsers (insert before `args = ap.parse_args(argv)`):

```python
    p = sub.add_parser("run", help="answer-phase all pending replicates")
    _common(p)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("compare", help="paired permutation test A vs B")
    _common(p)
    p.add_argument("--b-extractor", default=None)
    p.add_argument("--b-tag", default="")
    p.add_argument("--arm", choices=ARMS, default="cortex")
    p.add_argument("--permutations", type=int, default=10000)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("gate-check", help="replicate means vs baseline")
    _common(p)
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    p.set_defaults(fn=cmd_gate_check)

    p = sub.add_parser("baseline", help="(re)establish the gate baseline")
    _common(p)
    p.add_argument("--out", type=Path, default=DEFAULT_BASELINE)
    p.add_argument("--floor", type=float, default=BASELINE_FLOOR)
    p.set_defaults(fn=cmd_baseline)
```

Note `_rates_for` returns a tuple — the annotation in the snippet above is
the dict for the first element; keep the implementation exactly as written
(callers unpack two values).

- [ ] **Step 4: Run all module tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_replicate.py -v`
Expected: 21 passed

- [ ] **Step 5: Commit**

```bash
git add evals/replicate.py tests/test_eval_replicate.py
git commit -m "feat(evals): replicate CLI — run/compare/gate-check/baseline

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: `evals/regression_gate.ps1`

**Files:**
- Create: `evals/regression_gate.ps1`

**Interfaces:**
- Consumes: `replicate.py` subcommands (Tasks 4–5), `rebuild_contexts.py` flags (`--dataset --extractor --src-tag --out-tag`), the Qwen server pattern from `evals/gate_e4b_ft.ps1`
- Produces: exit 0 pass / 1 regression / 2 infrastructure failure; `-Establish` writes `evals/results/regression_gate.baseline.json`

No automated test (endpoint-dependent); manual smoke happens in Task 10.

- [ ] **Step 1: Write the script**

```powershell
# Regression gate: pinned oracle/e4b-ft "arm1" slice, replicated, vs the
# committed baseline (evals/results/regression_gate.baseline.json).
#
# SCOPE: retrieval knobs + fact-ranking + answer/judge path. Extraction and
# dream-path changes are NOT covered here — re-run the ladder for those
# (existing rule). Run this before committing eval- or retrieval-affecting
# changes (CLAUDE.md review discipline).
#
# Stages: 0 cleanup of the arm1-gate namespace (stale judged gate files
# would resume as no-ops and silently pass); 1 rebuild contexts from local
# bank dumps with CURRENT knobs (falls back to strip-copying pinned
# contexts if banks are absent — reduced scope, loud warning); 2 judge
# N replicates; 3 verdict vs baseline.
#
#   evals\regression_gate.ps1                # 3 replicates, gate verdict
#   evals\regression_gate.ps1 -Replicates 1  # quick mode
#   evals\regression_gate.ps1 -Establish     # (re)write the baseline
#
# Exit codes: 0 pass, 1 regression, 2 infrastructure (endpoint/rebuild).
param([int]$Replicates = 3, [switch]$Establish)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$replicatePy = Join-Path $repo "evals\replicate.py"
$rebuild = Join-Path $repo "evals\rebuild_contexts.py"
$results = Join-Path $repo "evals\results"
$banks = Join-Path $results "banks\oracle-e4b-ft-arm1"
$qwenDir = "$env:USERPROFILE\ClaudeCode\llama.ccp"
$env:PYTHONPATH = $repo

function Log($msg) { Write-Host "$(Get-Date -Format 'HH:mm:ss') $msg" }

function Wait-Endpoint($url, $seconds) {
    for ($i = 0; $i -lt ($seconds / 5); $i++) {
        try { Invoke-RestMethod -Uri $url -TimeoutSec 3 | Out-Null; return $true }
        catch { Start-Sleep -Seconds 5 }
    }
    return $false
}

function Stop-Qwen {
    Get-Process llama-server -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

function Start-Qwen {
    if (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 5) { return $true }
    Log "starting Qwen 27B server"
    Start-Process -FilePath cmd.exe -WorkingDirectory $qwenDir -WindowStyle Minimized `
        -ArgumentList '/c', "`"$qwenDir\run-server-turboq.bat`" > qwen-server.log 2>&1"
    return (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 300)
}

# -- Stage 0: cleanup ------------------------------------------------------
Log "stage 0: clearing arm1-gate namespace"
Remove-Item (Join-Path $results "longmemeval-ku-oracle-e4b-ft-arm1-gate*") `
    -Force -ErrorAction SilentlyContinue

# -- Stage 1: contexts -----------------------------------------------------
if (Test-Path $banks) {
    Log "stage 1: rebuilding contexts from banks with current knobs"
    & $py $rebuild --dataset oracle --extractor e4b-ft `
        --src-tag arm1 --out-tag arm1-gate
    if ($LASTEXITCODE -ne 0) { Log "rebuild failed"; exit 2 }
} else {
    Write-Warning ("banks missing at $banks — falling back to pinned " +
        "contexts; gate covers answer/judge drift only")
    & $py $replicatePy copy --extractor e4b-ft --tag arm1 --to-tag arm1-gate
    if ($LASTEXITCODE -ne 0) { Log "copy failed"; exit 2 }
}

# -- Stage 2: judge replicates --------------------------------------------
if (-not (Start-Qwen)) { Log "no Qwen endpoint"; exit 2 }
try {
    & $py $replicatePy run --extractor e4b-ft --tag arm1-gate
    if ($LASTEXITCODE -ne 0) { Log "run (r1) failed"; exit 2 }
    if ($Replicates -gt 1) {
        & $py $replicatePy spawn --extractor e4b-ft --tag arm1-gate `
            -n ($Replicates - 1)
        if ($LASTEXITCODE -ne 0) { Log "spawn failed"; exit 2 }
        & $py $replicatePy run --extractor e4b-ft --tag arm1-gate
        if ($LASTEXITCODE -ne 0) { Log "run (rN) failed"; exit 2 }
    }
    & $py $replicatePy agg --extractor e4b-ft --tag arm1-gate

    # -- Stage 3: verdict --------------------------------------------------
    if ($Establish) {
        & $py $replicatePy baseline --extractor e4b-ft --tag arm1-gate
        exit $LASTEXITCODE
    }
    & $py $replicatePy gate-check --extractor e4b-ft --tag arm1-gate
    exit $LASTEXITCODE
} finally {
    Stop-Qwen
    Log "regression gate finished"
}
```

- [ ] **Step 2: Syntax-check the script**

Run: `pwsh -NoProfile -Command "$null = [System.Management.Automation.PSParser]::Tokenize((Get-Content evals/regression_gate.ps1 -Raw), [ref]$null)"`
Expected: no output, exit 0

- [ ] **Step 3: Commit**

```bash
git add evals/regression_gate.ps1
git commit -m "feat(evals): regression gate — pinned arm1-gate slice vs committed baseline

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: `evals/overnight_replicates.ps1`

**Files:**
- Create: `evals/overnight_replicates.ps1`

**Interfaces:**
- Consumes: `replicate.py` `spawn`/`run`/`agg`/`compare` (Tasks 4–5); Qwen server functions identical to Task 6
- Produces: 4 extra replicates + agg for each of `e4b-ft/arm1`, `e4b-ft/arm1-baseline`, `qwen-27b/(untagged)`; compare verdicts for cortex + hybrid

- [ ] **Step 1: Write the script**

```powershell
# Arm-1 re-verification overnight run (spec 2026-07-18).
#
# For each config below: spawn N stripped replicates of the existing judged
# JSONL, answer-phase them against the local Qwen 27B judge, aggregate to
# mean +/- std. Then paired-permutation compare arm1 vs arm1-baseline.
#
# PRE-REGISTERED RULE: paired p < 0.05 on the cortex arm confirms the Arm-1
# gain (shipped default stands, docs get mean+/-std); otherwise the
# extractor-default decision is flagged for revisit.
#
# Resumable: kill and re-run continues per row (bench semantics).
param([int]$N = 4)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$replicatePy = Join-Path $repo "evals\replicate.py"
$qwenDir = "$env:USERPROFILE\ClaudeCode\llama.ccp"
$env:PYTHONPATH = $repo
$maxRetries = 8

function Log($msg) { Write-Host "$(Get-Date -Format 'HH:mm:ss') $msg" }

function Wait-Endpoint($url, $seconds) {
    for ($i = 0; $i -lt ($seconds / 5); $i++) {
        try { Invoke-RestMethod -Uri $url -TimeoutSec 3 | Out-Null; return $true }
        catch { Start-Sleep -Seconds 5 }
    }
    return $false
}

function Stop-Qwen {
    Get-Process llama-server -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

function Start-Qwen {
    if (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 5) { return $true }
    Log "starting Qwen 27B server"
    Start-Process -FilePath cmd.exe -WorkingDirectory $qwenDir -WindowStyle Minimized `
        -ArgumentList '/c', "`"$qwenDir\run-server-turboq.bat`" > qwen-server.log 2>&1"
    return (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 300)
}

function Invoke-WithRetry($label, $stepArgs) {
    for ($try = 1; $try -le $maxRetries; $try++) {
        if (-not (Start-Qwen)) { Log "$label : no endpoint (try $try)"; Stop-Qwen; continue }
        & $py @stepArgs
        if ($LASTEXITCODE -eq 0) { Log "$label : done"; return $true }
        Log "$label : exited $LASTEXITCODE (try $try/$maxRetries) — restarting server"
        Stop-Qwen
        Start-Sleep -Seconds 10
    }
    Log "$label : GAVE UP after $maxRetries tries"
    return $false
}

$configs = @(
    @{ Extractor = "e4b-ft";   Tag = "arm1" },
    @{ Extractor = "e4b-ft";   Tag = "arm1-baseline" },
    @{ Extractor = "qwen-27b"; Tag = "" }
)

try {
    foreach ($cfg in $configs) {
        $label = "$($cfg.Extractor)/$(if ($cfg.Tag) { $cfg.Tag } else { '(untagged)' })"
        Log "=== $label : spawn $N replicates ==="
        & $py $replicatePy spawn --extractor $cfg.Extractor --tag $cfg.Tag -n $N
        if ($LASTEXITCODE -ne 0) { Log "$label : spawn failed"; continue }
        Invoke-WithRetry "$label run" @(
            $replicatePy, "run", "--extractor", $cfg.Extractor,
            "--tag", $cfg.Tag) | Out-Null
        & $py $replicatePy agg --extractor $cfg.Extractor --tag $cfg.Tag
    }

    Log "=== compare: arm1 vs arm1-baseline ==="
    foreach ($arm in @("cortex", "hybrid")) {
        & $py $replicatePy compare --extractor e4b-ft --tag arm1 `
            --b-tag arm1-baseline --arm $arm
    }
    Log ("PRE-REGISTERED RULE: cortex p < 0.05 confirms the Arm-1 gain; " +
         "otherwise flag the extractor default for revisit.")
} finally {
    Stop-Qwen
    Log "overnight replicates finished"
}
```

- [ ] **Step 2: Syntax-check the script**

Run: `pwsh -NoProfile -Command "$null = [System.Management.Automation.PSParser]::Tokenize((Get-Content evals/overnight_replicates.ps1 -Raw), [ref]$null)"`
Expected: no output, exit 0

- [ ] **Step 3: Commit**

```bash
git add evals/overnight_replicates.ps1
git commit -m "feat(evals): overnight Arm-1 re-verification orchestrator

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: docs — variance section, honesty note, CHANGELOG, CLAUDE.md

**Files:**
- Modify: `evals/README.md` (new section after the LongMemEval findings section)
- Modify: `docs/guide/benchmarks.md` (paragraph near the top, after the results intro)
- Modify: `CHANGELOG.md` (under `## [Unreleased]`, new dated subsection in the existing style)
- Modify: `CLAUDE.md` (one bullet in "Review discipline")

- [ ] **Step 1: Add the variance section to `evals/README.md`**

Insert after the LongMemEval bench's findings section (match surrounding heading level):

```markdown
### Variance and replication

Single runs of this bench are noisy: three runs of the identical
sonnet-5-v1 config (same bank, byte-identical contexts, temperature 0)
scored cortex 0.808 / 0.731 / 0.782 — a ~7.7 pp spread coming entirely
from the answerer/judge side. Differences inside that band are not
decisions. MemDelta (arXiv 2606.29914) documents the same failure across
the field: identical aggregate scores can disagree on 16–66 % of items,
and single-run memory-bench comparisons routinely measure judge noise.

Convention: any comparison used for a decision runs ≥3 answer-phase
replicates per config and reports mean ± std; config-vs-config claims
use the paired permutation test. Findings tables in this file are
point-in-time snapshots — where a `.agg.json` exists next to a results
file, the aggregate is authoritative.

Workflow (contexts are persisted at extract time, so replicates never
re-extract):

    python evals/replicate.py spawn --extractor e4b-ft --tag arm1 -n 4
    python evals/replicate.py run   --extractor e4b-ft --tag arm1
    python evals/replicate.py agg   --extractor e4b-ft --tag arm1
    python evals/replicate.py compare --extractor e4b-ft --tag arm1 \
        --b-tag arm1-baseline --arm cortex

`evals/regression_gate.ps1` runs a pinned, replicated slice against the
committed baseline (`evals/results/regression_gate.baseline.json`) —
see the script header for scope and the `-Establish` flow.
```

- [ ] **Step 2: Add the honesty paragraph to `docs/guide/benchmarks.md`**

Insert after the introductory framing (before the first results table):

```markdown
> **Reading the numbers.** Accuracies below are single-run point
> estimates unless marked mean ± std. Repeated runs of an *identical*
> config vary by several points from answerer/judge noise alone (observed
> spread: ~7.7 pp on the cortex arm at n=78), so small single-run
> differences between configs are not meaningful. Decision-grade
> comparisons use replicates and a paired test — see
> [Variance and replication](../../evals/README.md#variance-and-replication).
```

- [ ] **Step 3: Add the CHANGELOG entry**

Under `## [Unreleased]`, in the existing dated-subsection style (check the current file for the exact heading format and match it):

```markdown
### 2026-07-18 — eval replication layer + regression gate

- **evals**: `evals/replicate.py` — answer-phase replication over
  `longmemeval_bench.py` (`spawn`/`run`/`agg`/`compare`/`gate-check`/
  `baseline`): stripped `-rN` replicate files, mean±std aggregation to
  `.agg.json`, paired permutation compare. Measured motivation: identical
  configs vary ~7.7 pp cortex accuracy run-to-run (judge-side noise), wider
  than several previously-published single-run deltas.
- **evals**: `evals/regression_gate.ps1` (pinned replicated `arm1-gate`
  slice vs committed `regression_gate.baseline.json`; covers
  retrieval/serving/judging — the ladder still covers extraction) and
  `evals/overnight_replicates.ps1` (Arm-1 re-verification, pre-registered
  p < 0.05 rule).
- **docs**: variance/replication methodology in `evals/README.md`; honesty
  note in `docs/guide/benchmarks.md` (single-run numbers carry a ~7.7 pp
  noise band).
```

- [ ] **Step 4: Add the CLAUDE.md review-discipline bullet**

In `CLAUDE.md` under "## Review discipline", append:

```markdown
- Eval- or retrieval-affecting changes run `evals/regression_gate.ps1`
  before commit (pinned replicated slice vs committed baseline; exit 1 =
  regression). Extraction/dream-path changes re-run the ladder instead —
  the gate deliberately does not cover them.
```

- [ ] **Step 5: Verify docs guards still pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_release_ux.py -v`
Expected: PASS (no maintainer identifiers introduced; CHANGELOG guard intact)

- [ ] **Step 6: Commit**

```bash
git add evals/README.md docs/guide/benchmarks.md CHANGELOG.md CLAUDE.md
git commit -m "docs(evals): variance/replication methodology + regression-gate discipline

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: full-suite verification

- [ ] **Step 1: Run the full test suite**

Run (bench Postgres up at 127.0.0.1:5433 if available):
`$env:HF_HUB_OFFLINE = "1"; .venv\Scripts\python.exe -m pytest tests/`
Expected: all green (note PG-backed tests skip without the bench DB — that is not a pass for them; state which case applied).

- [ ] **Step 2: Fix anything red, re-run, commit fixes if any**

---

### Task 10: launch the overnight re-verification (main session)

Not subagent work — GPU tenancy and judgment involved.

- [ ] **Step 1: Check GPU tenancy** — confirm no other llama-server workload is in use (the orchestrator kills `llama-server` processes). If something else is running on the 4090, ask the user before proceeding.
- [ ] **Step 2: Smoke the pipeline** — `python evals/replicate.py run --extractor e4b-ft --tag arm1 --dry-run` then a 1-replicate spawn+run with `--limit`-free but single replicate on `arm1` (spawn `-n 1`, run) to confirm end-to-end before committing hours.
- [ ] **Step 3: Launch** `evals/overnight_replicates.ps1` in the background; monitor the first config's first few judged rows, then leave it running.

### Task 11: post-run — verdict, baseline, doc renumbering (main session)

Blocked by Task 10 completion (hours later).

- [ ] **Step 1: Review** the three `.agg.json` files and the two `compare` outputs; apply the pre-registered rule (cortex p < 0.05 confirms Arm-1).
- [ ] **Step 2: Establish the gate baseline**: `evals/regression_gate.ps1 -Establish` (writes + commit `evals/results/regression_gate.baseline.json`).
- [ ] **Step 3: Renumber docs** — update `docs/guide/benchmarks.md` and `evals/README.md` findings for the replicated configs to mean±std; if the Arm-1 verdict failed, flag the extractor default for revisit (do NOT revert in this sprint); CHANGELOG note for the verdict.
- [ ] **Step 4: Memory capture** — `memory_store` the verdict (source `pseudolife-mcp`, origin `action`) and `memory_outcome` for the sprint.

---

## Self-review notes (done at plan-writing time)

- Spec coverage: spawn/run/agg/compare/copy (§Component 1) → Tasks 1–5; gate + baseline (§Component 2) → Tasks 3, 5, 6; overnight + pre-registered rule (§Component 3) → Tasks 7, 10, 11; docs/CHANGELOG/CLAUDE.md/tests (§Component 4) → Tasks 1–5, 8. Error handling (§Error handling) → spawn/copy overwrite refusal (T4), run results-dir guard + resume semantics (T5), compare <2-replicates + mismatch (T2/T5), gate exit codes 0/1/2 (T5/T6), banks-missing fallback (T6).
- `_rates_for` returns `(rates, agg)` — annotated in Task 5's note; callers unpack consistently.
- Test count checkpoints (7/11/13/17/21) assume tests are appended in task order; if a count drifts, trust `-v` output over the number.
