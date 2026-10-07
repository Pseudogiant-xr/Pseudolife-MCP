"""Tests for evals/replicate.py — the replication/variance layer.

Pure-function tests only: no endpoints, no GPU, no Postgres. The module
must import without pulling ladder_sweep/torch (that is itself asserted).
"""
import json
import random
import statistics
import subprocess
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


def _with_arm(row: dict, arm: str, correct: bool = True) -> dict:
    row = dict(row)
    row["contexts"] = {**row["contexts"], arm: "x"}
    row[f"{arm}_response"] = "resp"
    row[f"{arm}_correct"] = correct
    row[f"{arm}_context_tokens"] = 10
    return row


def test_aggregate_reports_comparator_arms_found_in_the_rows():
    """A run with --refind/--nomem must not aggregate to a table that
    silently omits them — the numbers would look like the arms never ran."""
    r1 = [_with_arm(_with_arm(_row(f"q{i}"), "refind"), "nomem", correct=False)
          for i in range(4)]
    agg = replicate.aggregate({"arm1": r1})
    assert agg["arms"]["refind"]["accuracies"] == [1.0]
    assert agg["arms"]["nomem"]["accuracies"] == [0.0]
    # canonical arms keep their order and come first
    assert list(agg["arms"])[:4] == ["rag", "cortex", "hybrid", "cascade"]


def test_aggregate_is_unchanged_for_legacy_three_arm_rows():
    agg = replicate.aggregate({"arm1": [_row("q0"), _row("q1")]})
    assert list(agg["arms"]) == ["rag", "cortex", "hybrid", "cascade"]


def test_strip_judged_strips_every_arms_verdict_not_just_the_canonical_three():
    """A stripped replicate must carry NO verdicts: leaving a comparator
    arm's judged fields behind would ship a stale verdict beside freshly
    answered ones."""
    stripped = replicate.strip_judged([_with_arm(_row("q1"), "refind")])[0]
    assert "refind_correct" not in stripped
    assert "refind_response" not in stripped
    assert "refind_context_tokens" not in stripped
    assert stripped["contexts"]["refind"] == "x"      # the context stays


def test_gate_verdict_names_a_baseline_arm_the_run_does_not_carry():
    """A baseline established from a run WITH comparator arms records
    them; a later gate-check on a three-arm slice must fail with a
    readable message, not a bare KeyError that reads like a regression."""
    baseline = {"arms": {"rag": {"mean": 0.5, "margin": 0.03},
                         "refind": {"mean": 0.4, "margin": 0.03}}}
    agg = {"arms": {"rag": {"mean": 0.6}}}
    failures = replicate.gate_verdict(agg, baseline)
    assert len(failures) == 1
    assert "refind" in failures[0] and "re-establish" in failures[0]


def test_judged_arms_lists_what_a_row_can_be_compared_on():
    assert replicate.judged_arms([_with_arm(_row("q0"), "refind")]) == (
        "rag", "cortex", "hybrid", "cascade", "refind")
    assert replicate.judged_arms([_row("q0")]) == (
        "rag", "cortex", "hybrid", "cascade")


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


def test_nondeterminism_warnings_flag_a_drifted_server():
    """Replicates re-judge byte-identical contexts, so on the reproducible
    server config every replicate must score identically. Any spread means
    the judge ran on a nondeterministic server (the TBQ4_0 fork), which
    silently widens every margin — surface it instead of averaging it away."""
    # std == 0 across replicates: the expected, reproducible case.
    assert replicate.nondeterminism_warnings(_agg(0.70, std=0.0)) == []
    # A single replicate cannot show spread; absence of evidence, not a pass.
    assert replicate.nondeterminism_warnings(
        {**_agg(0.70), "n_replicates": 1,
         "arms": {a: {"accuracies": [0.70], "mean": 0.70, "std": None}
                  for a in replicate.ARMS}}) == []
    warnings = replicate.nondeterminism_warnings(_agg(0.70, std=0.02))
    assert len(warnings) == len(replicate.ARMS)
    assert "cortex" in " ".join(warnings)


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


def test_cli_spawn_rejects_contextless_source(tmp_path):
    rows = [_row("q0")]
    for r in rows:
        del r["contexts"], r["question_date"]
    _write_jsonl(replicate.result_file("oracle", "qwen-27b", "", tmp_path),
                 rows)
    with pytest.raises(SystemExit) as e:
        replicate.main(["spawn", "--extractor", "qwen-27b", "-n", "1",
                        "--results-dir", str(tmp_path)])
    assert "context" in str(e.value)


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


def test_cli_agg_rejects_unjudged_only(tmp_path):
    _write_jsonl(replicate.result_file("oracle", "e4b-ft", "arm1", tmp_path),
                 [_row("q0", judged=False)])
    with pytest.raises(SystemExit):
        replicate.main(["agg", "--extractor", "e4b-ft", "--tag", "arm1",
                        "--results-dir", str(tmp_path)])


def _seed_two_judged(tmp_path, tag, correct=True):
    for t in (tag, f"{tag}-r2"):
        _write_jsonl(replicate.result_file("oracle", "e4b-ft", t, tmp_path),
                     [_row("q0", correct=correct), _row("q1"), _row("q2")])


def test_cli_compare_persists_result_to_out(tmp_path):
    """A p-value that only ever reaches stdout cannot back a published claim.

    The band-ablation significance table shipped with no artifact at all
    because `compare` printed and forgot (2026-07-21 audit).
    """
    _seed_two_judged(tmp_path, "a")
    _seed_two_judged(tmp_path, "b", correct=False)
    out = tmp_path / "a-vs-b-cortex.compare.json"
    rc = replicate.main(["compare", "--extractor", "e4b-ft", "--tag", "a",
                         "--b-tag", "b", "--arm", "cortex",
                         "--out", str(out),
                         "--results-dir", str(tmp_path)])
    assert rc == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["arm"] == "cortex"
    assert saved["a"] == "e4b-ft/a" and saved["b"] == "e4b-ft/b"
    assert "delta" in saved and "p_value" in saved
    assert saved["n_questions"] == 3


def test_cli_compare_works_on_a_comparator_arm(tmp_path):
    """refind-vs-rag is the comparison the ReFind arm exists to make, so
    `compare` has to accept an arm outside the canonical three."""
    for tag, correct in (("a", True), ("b", False)):
        for t in (tag, f"{tag}-r2"):
            _write_jsonl(
                replicate.result_file("oracle", "e4b-ft", t, tmp_path),
                [_with_arm(_row(f"q{i}"), "refind", correct=correct)
                 for i in range(3)])
    out = tmp_path / "refind.compare.json"
    rc = replicate.main(["compare", "--extractor", "e4b-ft", "--tag", "a",
                         "--b-tag", "b", "--arm", "refind",
                         "--out", str(out), "--results-dir", str(tmp_path)])
    assert rc == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["arm"] == "refind" and saved["delta"] == 1.0


def test_cli_compare_names_the_available_arms_when_one_is_missing(tmp_path):
    _seed_two_judged(tmp_path, "a")
    _seed_two_judged(tmp_path, "b", correct=False)
    with pytest.raises(SystemExit) as e:
        replicate.main(["compare", "--extractor", "e4b-ft", "--tag", "a",
                        "--b-tag", "b", "--arm", "refind",
                        "--results-dir", str(tmp_path)])
    assert "refind" in str(e.value) and "cascade" in str(e.value)


def test_cli_compare_records_its_own_reproducibility_knobs(tmp_path):
    """A permutation p-value is only reproducible if the artifact says how
    it was drawn — the defaults are not self-evident to a later reader."""
    _seed_two_judged(tmp_path, "a")
    _seed_two_judged(tmp_path, "b", correct=False)
    out = tmp_path / "cmp.json"
    replicate.main(["compare", "--extractor", "e4b-ft", "--tag", "a",
                    "--b-tag", "b", "--arm", "cortex",
                    "--seed", "7", "--permutations", "500",
                    "--out", str(out), "--results-dir", str(tmp_path)])
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["permutations"] == 500
    assert saved["seed"] == 7


def test_cli_compare_still_prints_without_out(tmp_path, capsys):
    _seed_two_judged(tmp_path, "a")
    _seed_two_judged(tmp_path, "b", correct=False)
    rc = replicate.main(["compare", "--extractor", "e4b-ft", "--tag", "a",
                         "--b-tag", "b", "--arm", "cortex",
                         "--results-dir", str(tmp_path)])
    assert rc == 0
    assert "p_value" in json.loads(capsys.readouterr().out)


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


def test_cli_run_dry_run_stays_lazy(tmp_path):
    # Clean interpreter: the dry-run path must not import the bench stack.
    _seed_base(tmp_path)
    _write_jsonl(replicate.result_file("oracle", "e4b-ft", "arm1-r2",
                                       tmp_path),
                 [_row("q0", judged=False)])
    evals_dir = str(Path(__file__).resolve().parents[1] / "evals")
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import replicate; "
        "rc = replicate.main(['run', '--extractor', 'e4b-ft', "
        "'--tag', 'arm1', '--dry-run', '--results-dir', sys.argv[2]]); "
        "banned = {'torch', 'ladder_sweep', 'longmemeval_bench'}; "
        "hit = sorted(banned & set(sys.modules)); "
        "sys.exit(('heavy imports: ' + ', '.join(hit)) if hit else rc)"
    )
    subprocess.run([sys.executable, "-c", code, evals_dir, str(tmp_path)],
                   check=True)


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


def test_cli_compare_mismatched_questions_exits_cleanly(tmp_path):
    a = [_row("q0"), _row("q1")]
    b = [_row("q0"), _row("qX")]
    for tag, rows in [("arm1", a), ("arm1-r2", a),
                      ("arm1-baseline", b), ("arm1-baseline-r2", b)]:
        _write_jsonl(replicate.result_file("oracle", "e4b-ft", tag,
                                           tmp_path), rows)
    with pytest.raises(SystemExit) as e:
        replicate.main(["compare", "--extractor", "e4b-ft", "--tag", "arm1",
                        "--b-tag", "arm1-baseline",
                        "--results-dir", str(tmp_path)])
    assert "question sets" in str(e.value)


# ── cascade (derived commit-gated arm) ────────────────────────────────────
def _cascade_row(qid: str, cortex_resp: str, cortex_ok: bool,
                 rag_ok: bool) -> dict:
    row = _row(qid)
    row["cortex_response"] = cortex_resp
    row["cortex_correct"] = cortex_ok
    row["rag_correct"] = rag_ok
    return row


def test_cortex_commits_detects_abstention():
    assert replicate.cortex_commits(_cascade_row("q", "Paris.", True, True))
    assert not replicate.cortex_commits(
        _cascade_row("q", "I don't know.", False, True))
    # case- and typographic-apostrophe-tolerant (models emit U+2019)
    assert not replicate.cortex_commits(
        _cascade_row("q", "I DON\u2019T KNOW", False, True))


def test_cascade_correct_commit_gating():
    # cortex commits -> its verdict decides; rag is ignored either way
    assert replicate.cascade_correct(
        _cascade_row("q", "Paris.", True, False)) is True
    assert replicate.cascade_correct(
        _cascade_row("q", "Chicago.", False, True)) is False
    # cortex abstains -> rag verdict decides
    assert replicate.cascade_correct(
        _cascade_row("q", "I don't know.", False, True)) is True
    assert replicate.cascade_correct(
        _cascade_row("q", "I don't know.", False, False)) is False


def test_cascade_context_tokens_pays_rag_only_on_fallback():
    committed = _cascade_row("q", "42", True, True)
    committed["cortex_context_tokens"] = 60
    committed["rag_context_tokens"] = 1000
    assert replicate.cascade_context_tokens(committed) == 60
    fell_back = _cascade_row("q", "I don't know.", False, True)
    fell_back["cortex_context_tokens"] = 60
    fell_back["rag_context_tokens"] = 1000
    assert replicate.cascade_context_tokens(fell_back) == 1060


def test_aggregate_and_question_rates_include_cascade():
    rows = [
        _cascade_row("q0", "yes", True, False),           # commit, right
        _cascade_row("q1", "I don't know.", False, True),   # fallback, right
        _cascade_row("q2", "wrong", False, True),         # commit, wrong
        _cascade_row("q3", "I don't know.", False, False),  # fallback, wrong
    ]
    agg = replicate.aggregate({"t": rows})
    assert agg["arms"]["cascade"]["accuracies"] == [0.5]
    rates = replicate.question_rates({"t": rows}, "cascade")
    assert rates == {"q0": 1.0, "q1": 1.0, "q2": 0.0, "q3": 0.0}


def test_cli_compare_accepts_cascade_arm(tmp_path, capsys):
    a = [_cascade_row("q0", "yes", True, False),
         _cascade_row("q1", "I don't know.", False, False)]
    b = [_cascade_row("q0", "I don't know.", False, False),
         _cascade_row("q1", "I don't know.", False, False)]
    for tag, rows in [("arm1", a), ("arm1-r2", a),
                      ("arm1-base", b), ("arm1-base-r2", b)]:
        _write_jsonl(replicate.result_file("oracle", "e4b-ft", tag,
                                           tmp_path), rows)
    assert replicate.main(["compare", "--extractor", "e4b-ft",
                           "--tag", "arm1", "--b-tag", "arm1-base",
                           "--arm", "cascade",
                           "--results-dir", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["arm"] == "cascade"
    assert out["delta"] == pytest.approx(0.5)   # a: 1/2 vs b: 0/2


# ── embedder precision (cpu_dtype "auto" differs by host) ─────────────────
_BF16 = {"backend": "torch", "device": "cpu", "dtype": "bf16"}
_FP32 = {"backend": "torch", "device": "cpu", "dtype": "fp32"}


def _stamped(rows: list[dict], desc: dict,
             stage: str = "rebuild_contexts") -> list[dict]:
    return [{**r, "embedder": {stage: desc}} for r in rows]


def _seed_stamped(tmp_path, tag: str, desc: dict | None,
                  correct: bool = True) -> None:
    rows = [_row("q0", correct=correct), _row("q1"), _row("q2")]
    if desc is not None:
        rows = _stamped(rows, desc)
    for t in (tag, f"{tag}-r2"):
        _write_jsonl(replicate.result_file("oracle", "e4b-ft", t, tmp_path),
                     rows)


def test_strip_judged_keeps_the_embedder_stamp():
    """A spawned replicate re-judges the same contexts, so it inherits the
    stamp of the embedder that built them."""
    stripped = replicate.strip_judged(_stamped([_row("q1")], _BF16))[0]
    assert stripped["embedder"] == {"rebuild_contexts": _BF16}


def test_aggregate_carries_the_rows_embedder_stamp():
    agg = replicate.aggregate({"t": _stamped([_row("q0"), _row("q1")],
                                             _BF16)})
    assert agg["embedder"] == {"rebuild_contexts": _BF16}


def test_aggregate_of_legacy_rows_has_no_embedder_key():
    assert "embedder" not in replicate.aggregate({"t": [_row("q0")]})


def test_make_baseline_records_the_embedder_it_was_measured_with():
    agg = {**_agg(0.7), "embedder": {"rebuild_contexts": _FP32}}
    base = replicate.make_baseline(agg, commit="abc")
    assert base["embedder"] == {"rebuild_contexts": _FP32}
    assert "embedder" not in replicate.make_baseline(_agg(0.7), commit="abc")


def _gate(tmp_path, run_desc, base_desc, capsys) -> tuple[int, str]:
    _seed_stamped(tmp_path, "arm1-gate", run_desc)
    baseline_path = tmp_path / "regression_gate.baseline.json"
    baseline = replicate.make_baseline(_agg(1.0), commit="abc")
    if base_desc is not None:
        baseline["embedder"] = {"rebuild_contexts": base_desc}
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    capsys.readouterr()
    rc = replicate.main(["gate-check", "--extractor", "e4b-ft",
                         "--tag", "arm1-gate",
                         "--baseline", str(baseline_path),
                         "--results-dir", str(tmp_path)])
    return rc, capsys.readouterr().out


def test_gate_check_warns_when_the_run_and_baseline_precisions_differ(
        tmp_path, capsys):
    """Warn, not fail: bf16 and fp32 scored identically on the gate, so a
    precision change is a caveat on the verdict, not a regression."""
    rc, out = _gate(tmp_path, _BF16, _FP32, capsys)
    assert rc == 0
    warnings = [ln for ln in out.splitlines() if ln.startswith("WARNING")]
    assert len(warnings) == 1
    assert "bf16" in warnings[0] and "fp32" in warnings[0]
    assert "REGRESSION GATE: PASS" in out


def test_gate_check_is_silent_when_precisions_match(tmp_path, capsys):
    rc, out = _gate(tmp_path, _BF16, _BF16, capsys)
    assert rc == 0
    assert "WARNING" not in out


def test_gate_check_reads_an_unstamped_baseline_as_unknown(tmp_path, capsys):
    """The committed baseline predates stamping. Unknown is not a
    mismatch — but the output says it was not compared."""
    rc, out = _gate(tmp_path, _BF16, None, capsys)
    assert rc == 0
    assert "WARNING" not in out
    assert "baseline unknown" in out
    # both sides legacy: nothing to compare, nothing to warn about
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    rc, out = _gate(legacy, None, None, capsys)
    assert rc == 0 and "WARNING" not in out


def test_cli_baseline_writes_the_embedder_stamp(tmp_path):
    _seed_stamped(tmp_path, "arm1-gate", _FP32)
    out = tmp_path / "baseline.json"
    assert replicate.main(["baseline", "--extractor", "e4b-ft",
                           "--tag", "arm1-gate", "--out", str(out),
                           "--results-dir", str(tmp_path)]) == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["embedder"] == {"rebuild_contexts": _FP32}


def test_cli_agg_writes_the_embedder_stamp(tmp_path):
    _seed_stamped(tmp_path, "arm1", _BF16)
    assert replicate.main(["agg", "--extractor", "e4b-ft", "--tag", "arm1",
                           "--results-dir", str(tmp_path)]) == 0
    agg = json.loads((tmp_path / "longmemeval-ku-oracle-e4b-ft-arm1.agg.json"
                      ).read_text(encoding="utf-8"))
    assert agg["embedder"] == {"rebuild_contexts": _BF16}


def test_cli_compare_warns_when_the_two_runs_precisions_differ(
        tmp_path, capsys):
    _seed_stamped(tmp_path, "a", _BF16)
    _seed_stamped(tmp_path, "b", _FP32, correct=False)
    out = tmp_path / "cmp.json"
    assert replicate.main(["compare", "--extractor", "e4b-ft", "--tag", "a",
                           "--b-tag", "b", "--arm", "cortex",
                           "--out", str(out),
                           "--results-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    # stdout stays one JSON document; the warning goes to stderr
    printed = json.loads(captured.out)
    assert "WARNING" in captured.err
    assert "bf16" in captured.err and "fp32" in captured.err
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved == printed
    assert len(saved["embedder_warnings"]) == 1
    assert saved["a_embedder"] == {"rebuild_contexts": _BF16}
    assert saved["b_embedder"] == {"rebuild_contexts": _FP32}


def test_cli_compare_reads_unstamped_runs_as_unknown(tmp_path, capsys):
    _seed_stamped(tmp_path, "a", _BF16)
    _seed_stamped(tmp_path, "b", None, correct=False)
    out = tmp_path / "cmp.json"
    assert replicate.main(["compare", "--extractor", "e4b-ft", "--tag", "a",
                           "--b-tag", "b", "--arm", "cortex",
                           "--out", str(out),
                           "--results-dir", str(tmp_path)]) == 0
    assert "WARNING" not in capsys.readouterr().err
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["embedder_warnings"] == []
    assert saved["a_embedder"] == {"rebuild_contexts": _BF16}
    assert saved["b_embedder"] == "unknown"


def test_cli_compare_warns_on_a_rebuilt_run_against_its_source(tmp_path,
                                                               capsys):
    """rebuild_contexts.py's own workflow: `diag` extracted in fp32, rebuilt
    into `diag-knobs` on a bf16 host, compared on the default cortex arm.
    The source's cortex ranking came from its (known) extract stage."""
    source = [{**_row(f"q{i}"), "embedder": {"extract": _FP32}}
              for i in range(3)]
    rebuilt = [{**r, "embedder": {"extract": _FP32,
                                  "rebuild_contexts": _BF16}}
               for r in source]
    for tag, rows in (("diag", source), ("diag-knobs", rebuilt)):
        for t in (tag, f"{tag}-r2"):
            _write_jsonl(replicate.result_file("oracle", "e4b-ft", t,
                                               tmp_path), rows)
    assert replicate.main(["compare", "--extractor", "e4b-ft",
                           "--tag", "diag-knobs", "--b-tag", "diag",
                           "--results-dir", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "WARNING embedder rebuild_contexts" in err


def test_aggregate_counts_rows_once_not_once_per_replicate():
    """Replicates are copies of one file's rows; a mixed stage's row
    counts must describe that file, not rows x replicates."""
    rows = [{**_row("q0"), "embedder": {"extract": _BF16}},
            {**_row("q1"), "embedder": {"extract": _FP32}}]
    agg = replicate.aggregate({"t": rows, "t-r2": [dict(r) for r in rows]})
    assert agg["embedder"]["extract"]["mixed"] == [
        {"embedder": _BF16, "rows": 1}, {"embedder": _FP32, "rows": 1}]
