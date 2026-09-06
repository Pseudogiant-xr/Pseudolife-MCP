"""``evals/ladder_pair_compare.py`` has two gate predicates, and picking
the wrong one silently mis-reads a run.

The default ``identity`` mode was built for the chip-5 gate (PR #245),
where the change was predicted **inert** on an unlabelled corpus: any
metric difference at all is a bug. Applied to a *prompt* change it is the
wrong bar — the 2026-09-05 provenance-prompt gate reported ``FAIL`` only
because ``tokens_per_query`` moved 13.4 -> 14.2, well inside the ladder's
own token budget.

``threshold`` mode applies the ladder rule from ``evals/README.md``
("Reading the verdict") to BOTH arms:

    stale_leak        <  naive.stale_leak
    gold_recoverable  >  naive.gold_recoverable
    tokens_per_query  <= 0.6 * naive.tokens_per_query

and additionally requires the post arm to be no worse than the pre arm on
the two quality metrics. Tokens are reported and bounded only by the
0.6x-naive rule, so a change may cost tokens inside the budget.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import ladder_pair_compare as LPC  # noqa: E402

NAIVE = {"rung": "naive-rag", "kind": "naive", "gold_recoverable": 0.7,
         "stale_leak": 0.3, "tokens_per_query": 58.3,
         "search_latency_ms": 9.0, "extract_seconds": 0.0, "status": "ok"}

TALLY = {"pulled": 26, "claims": 16, "inserted": 16, "superseded": 0,
         "literal_flagged": 0, "literal_dropped": 0}


def _rung(gold=1.0, stale=0.0, tokens=13.4, **kw):
    d = {"rung": "qwen-27b", "kind": "llm", "extract_seconds": 34.2,
         "consolidation": dict(TALLY), "gold_recoverable": gold,
         "stale_leak": stale, "tokens_per_query": tokens,
         "search_latency_ms": 90.9, "status": "ok"}
    d.update(kw)
    return d


def _tree(tmp_path: Path, name: str, files: dict) -> str:
    root = tmp_path / name / "evals" / "results"
    root.mkdir(parents=True, exist_ok=True)
    (root / "naive-rag.json").write_text(json.dumps(NAIVE), encoding="utf-8")
    for fname, body in files.items():
        (root / fname).write_text(json.dumps(body), encoding="utf-8")
    return str(tmp_path / name)


def _run(monkeypatch, tmp_path, pre_files, post_files, *extra):
    pre = _tree(tmp_path, "pre", pre_files)
    post = _tree(tmp_path, "post", post_files)
    out = tmp_path / "verdict.json"
    monkeypatch.setattr(sys, "argv", [
        "ladder_pair_compare.py", "--pre", pre, "--post", post,
        "--tag", "t", "--out", str(out), *extra])
    rc = LPC.main()
    return rc, json.loads(out.read_text(encoding="utf-8"))


# -- threshold mode -------------------------------------------------------

def test_threshold_mode_clears_when_both_arms_beat_naive(monkeypatch,
                                                         tmp_path):
    """The shape of the 2026-09-05 qwen-27b arms: identical quality, a
    token cost well inside the budget."""
    rc, v = _run(monkeypatch, tmp_path,
                 {"qwen-27b-t-pre.json": _rung(tokens=13.4)},
                 {"qwen-27b-t-post.json": _rung(tokens=14.2)},
                 "--mode", "threshold", "--rungs", "qwen-27b")
    assert v["mode"] == "threshold"
    r = v["rungs"]["qwen-27b"]
    assert (r["pre_clears"], r["post_clears"]) == (True, True)
    assert r["no_regression"] is True
    assert r["cleared"] is True
    assert r["failed_checks"] == []
    assert v["gate"] == "PASS"
    assert v["no_regression_gate"] == "PASS"
    assert rc == 0


def test_threshold_mode_reports_the_naive_thresholds_it_applied(monkeypatch,
                                                                tmp_path):
    """The bar has to be readable off the artifact, or the verdict cannot
    be checked without re-deriving it."""
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung()},
                {"qwen-27b-t-post.json": _rung()},
                "--mode", "threshold", "--rungs", "qwen-27b")
    n = v["naive"]
    assert n["file"] == "evals/results/naive-rag.json"
    assert (n["gold_recoverable"], n["stale_leak"]) == (0.7, 0.3)
    assert n["tokens_per_query"] == 58.3
    assert n["token_budget"] == pytest.approx(34.98)


def test_a_token_increase_inside_the_budget_is_not_a_failure(monkeypatch,
                                                             tmp_path):
    """The exact thing identity mode gets wrong on a prompt change."""
    _, ident = _run(monkeypatch, tmp_path,
                    {"qwen-27b-t-pre.json": _rung(tokens=13.4)},
                    {"qwen-27b-t-post.json": _rung(tokens=14.2)},
                    "--rungs", "qwen-27b")
    assert ident["gate"] == "FAIL"          # identity mode, unchanged
    _, thr = _run(monkeypatch, tmp_path,
                  {"qwen-27b-t-pre.json": _rung(tokens=13.4)},
                  {"qwen-27b-t-post.json": _rung(tokens=14.2)},
                  "--mode", "threshold", "--rungs", "qwen-27b")
    assert thr["gate"] == "PASS"


def test_a_baseline_arm_that_fails_the_ladder_bar_fails_the_rung(
        monkeypatch, tmp_path):
    """The 2026-09-05 e4b-v3 shape: the PRE arm landed in the sidecar's
    bad mode (stale 1.0, 39.7 tok), which does not clear the ladder at
    all. That is a finding about the rung, not about the change — so the
    rung fails the gate while ``no_regression`` still reports True."""
    rc, v = _run(monkeypatch, tmp_path,
                 {"qwen-27b-t-pre.json": _rung(stale=1.0, tokens=39.7)},
                 {"qwen-27b-t-post.json": _rung(stale=0.1, tokens=14.8)},
                 "--mode", "threshold", "--rungs", "qwen-27b")
    r = v["rungs"]["qwen-27b"]
    assert r["pre_clears"] is False
    assert r["post_clears"] is True
    assert sorted(r["failed_checks"]) == ["pre.stale_leak",
                                          "pre.tokens_per_query"]
    assert r["no_regression"] is True
    assert r["cleared"] is False
    assert (v["gate"], v["no_regression_gate"]) == ("FAIL", "PASS")
    assert rc == 1


@pytest.mark.parametrize("post,failed", [
    (_rung(stale=0.1), "post.stale_leak_vs_pre"),
    (_rung(gold=0.9), "post.gold_recoverable_vs_pre"),
])
def test_the_post_arm_may_not_be_worse_than_the_pre_arm(monkeypatch,
                                                        tmp_path,
                                                        post, failed):
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung(gold=1.0, stale=0.0)},
                {"qwen-27b-t-post.json": post},
                "--mode", "threshold", "--rungs", "qwen-27b")
    r = v["rungs"]["qwen-27b"]
    assert r["no_regression"] is False
    assert failed in r["failed_checks"]
    assert v["no_regression_gate"] == "FAIL"
    assert v["gate"] == "FAIL"


def test_threshold_mode_still_reports_the_consolidation_tally(monkeypatch,
                                                              tmp_path):
    """Reported, not gated: a claim-count move is the thing a reader most
    wants beside a prompt change."""
    post = _rung()
    post["consolidation"] = dict(TALLY, claims=19, inserted=18)
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung()},
                {"qwen-27b-t-post.json": post},
                "--mode", "threshold", "--rungs", "qwen-27b")
    r = v["rungs"]["qwen-27b"]
    assert r["consolidation"]["claims"] == {"pre": 16, "post": 19}
    assert r["cleared"] is True


def test_a_missing_arm_file_fails_the_rung_in_threshold_mode(monkeypatch,
                                                             tmp_path):
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung()}, {},
                "--mode", "threshold", "--rungs", "qwen-27b")
    assert v["rungs"]["qwen-27b"]["status"] == "missing"
    assert v["gate"] == "FAIL"


def test_a_missing_naive_baseline_is_an_error_not_a_pass(monkeypatch,
                                                         tmp_path):
    pre = _tree(tmp_path, "pre", {"qwen-27b-t-pre.json": _rung()})
    post = _tree(tmp_path, "post", {"qwen-27b-t-post.json": _rung()})
    (Path(post) / "evals" / "results" / "naive-rag.json").unlink()
    (Path(pre) / "evals" / "results" / "naive-rag.json").unlink()
    out = tmp_path / "v.json"
    monkeypatch.setattr(sys, "argv", [
        "ladder_pair_compare.py", "--pre", pre, "--post", post, "--tag", "t",
        "--out", str(out), "--mode", "threshold", "--rungs", "qwen-27b"])
    with pytest.raises(SystemExit):
        LPC.main()


# -- --rungs and the unchanged default ------------------------------------

def test_rungs_selects_which_rungs_are_compared(monkeypatch, tmp_path):
    e4b = _rung(rung="e4b-v3", tokens=14.8, stale=0.1)
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung(),
                 "e4b-v3-t-pre.json": e4b},
                {"qwen-27b-t-post.json": _rung(),
                 "e4b-v3-t-post.json": e4b},
                "--mode", "threshold", "--rungs", "qwen-27b,e4b-v3")
    assert sorted(v["rungs"]) == ["e4b-v3", "qwen-27b"]
    assert v["gate"] == "PASS"


def test_the_default_rungs_and_mode_are_unchanged(monkeypatch, tmp_path):
    """The chip-5 contract: floor + qwen-27b, identity predicate."""
    assert LPC.RUNGS == ("floor", "qwen-27b")
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung()},
                {"qwen-27b-t-post.json": _rung()})
    assert v["mode"] == "identity"
    assert sorted(v["rungs"]) == ["floor", "qwen-27b"]
    assert v["rungs"]["qwen-27b"]["identical"] is True
    assert v["rungs"]["floor"]["status"] == "missing"


# -- --post-suffix (the 2026-09-05 re-gate) -------------------------------

def test_post_suffix_pairs_the_pre_arm_with_a_re_run_post_arm(monkeypatch,
                                                              tmp_path):
    """A prompt rewritten *after* its gate ran leaves the shipped text's
    arm beside the original one — `<tag>-post2.json` next to
    `<tag>-post.json` — while the `pre` arm is the same baseline either
    way. Without a way to name the post arm, the re-gate can only be
    verdicted by renaming files, which loses the run it supersedes."""
    rc, v = _run(monkeypatch, tmp_path,
                 {"qwen-27b-t-pre.json": _rung(tokens=13.4)},
                 {"qwen-27b-t-post.json": _rung(tokens=14.2),
                  "qwen-27b-t-post2.json": _rung(tokens=13.4)},
                 "--mode", "threshold", "--rungs", "qwen-27b",
                 "--post-suffix", "post2")
    r = v["rungs"]["qwen-27b"]
    assert r["post_file"].endswith("qwen-27b-t-post2.json")
    assert r["metrics"]["tokens_per_query"] == {"pre": 13.4, "post": 13.4}
    assert r["differences"] == {}
    assert r["identical"] is True
    assert r["cleared"] is True
    assert (v["gate"], v["no_regression_gate"]) == ("PASS", "PASS")
    assert rc == 0


def test_the_default_post_suffix_is_still_post(monkeypatch, tmp_path):
    """The chip-5 contract again: with both arms on disk and no flag, the
    verdict pairs `-post.json`, not whichever file sorts last."""
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung(tokens=13.4)},
                {"qwen-27b-t-post.json": _rung(tokens=14.2),
                 "qwen-27b-t-post2.json": _rung(tokens=13.4)},
                "--mode", "threshold", "--rungs", "qwen-27b")
    r = v["rungs"]["qwen-27b"]
    assert r["post_file"].endswith("qwen-27b-t-post.json")
    assert r["metrics"]["tokens_per_query"] == {"pre": 13.4, "post": 14.2}


# ...and the verdict says which post arm it read.

def test_post_suffix_reads_a_differently_named_post_arm(monkeypatch,
                                                        tmp_path):
    """A re-gate keeps the pre arm and re-runs only the post one, so the two
    arms no longer share a tag suffix. The shim re-gate wrote
    ``opus-5-shimprompt-post2.json`` beside an unchanged
    ``opus-5-shimprompt-pre.json``; without this flag the tool looks for a
    ``-post.json`` that belongs to the SUPERSEDED run and would silently
    compare the wrong file."""
    rc, v = _run(monkeypatch, tmp_path,
                 {"qwen-27b-t-pre.json": _rung(tokens=13.4)},
                 {"qwen-27b-t-post2.json": _rung(tokens=14.2)},
                 "--mode", "threshold", "--rungs", "qwen-27b",
                 "--post-suffix", "post2")
    r = v["rungs"]["qwen-27b"]
    assert r["post_file"] == "evals/results/qwen-27b-t-post2.json"
    assert r["cleared"] is True
    assert v["post_arm"] == "post2"
    assert rc == 0


def test_the_default_post_arm_is_still_post(monkeypatch, tmp_path):
    """The flag is additive: every existing invocation keeps reading
    ``-post.json`` and the verdict says so."""
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung()},
                {"qwen-27b-t-post.json": _rung()},
                "--mode", "threshold", "--rungs", "qwen-27b")
    assert v["post_arm"] == "post"
    assert (v["rungs"]["qwen-27b"]["post_file"]
            == "evals/results/qwen-27b-t-post.json")


# -- whose commit produced each arm (2026-09-05 merge review) --------------
#
# The verdict used to stamp `git rev-parse HEAD` of each worktree AT COMPARE
# TIME and call it the arm's sha. When the two arms are runs from different
# commits of ONE worktree — which is what a re-gate is — that value is the
# same string for both and it names neither run. The rule-v2 shim verdict
# recorded 7083bc33 for both arms while its `pre` files had been produced at
# 0b02e5ea. The arm's own artifact is the only thing that can answer this,
# so the verdict now reads `git_rev` off the artifacts and says plainly when
# they do not carry one.

def test_the_verdict_reads_each_arms_git_rev_off_its_own_artifacts(
        monkeypatch, tmp_path):
    """Stamped artifacts: the verdict reports the run's commit, not the
    compare's."""
    rc, v = _run(monkeypatch, tmp_path,
                 {"qwen-27b-t-pre.json": _rung(git_rev="a" * 40)},
                 {"qwen-27b-t-post.json": _rung(git_rev="b" * 40)},
                 "--mode", "threshold", "--rungs", "qwen-27b")
    assert v["pre"]["sha"] == "a" * 40
    assert v["pre"]["sha_source"] == "artifact"
    assert v["post"]["sha"] == "b" * 40
    assert v["post"]["sha_source"] == "artifact"
    assert rc == 0


def test_an_unstamped_arm_reports_null_not_the_compare_time_head(
        monkeypatch, tmp_path):
    """The failure this closes. An artifact written before `ladder_sweep`
    stamped `git_rev` cannot say which commit produced it, and guessing from
    the worktree's current HEAD produces a confident wrong answer. `null`
    plus a named reason is the honest report."""
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung()},
                {"qwen-27b-t-post.json": _rung()},
                "--mode", "threshold", "--rungs", "qwen-27b")
    assert v["pre"]["sha"] is None
    assert v["pre"]["sha_source"] == "unstamped"
    assert v["post"]["sha"] is None
    assert v["post"]["sha_source"] == "unstamped"
    # The compare-time HEAD is still recorded, under a name that says what
    # it is — it dates the verdict, it does not identify the run.
    assert "worktree_head_at_compare" in v["pre"]


def test_an_arm_whose_rungs_disagree_reports_mixed(monkeypatch, tmp_path):
    """One arm, two rungs, two commits: there is no single answer, so the
    verdict must not pick one."""
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung(git_rev="a" * 40),
                 "floor-t-pre.json": _rung(git_rev="c" * 40)},
                {"qwen-27b-t-post.json": _rung(git_rev="b" * 40),
                 "floor-t-post.json": _rung(git_rev="b" * 40)},
                "--mode", "threshold", "--rungs", "qwen-27b,floor")
    assert v["pre"]["sha"] is None
    assert v["pre"]["sha_source"] == "mixed"
    assert v["post"]["sha"] == "b" * 40
    assert v["post"]["sha_source"] == "artifact"


def test_a_missing_rung_contributes_no_rev(monkeypatch, tmp_path):
    """A rung that could not be read contributes nothing — it must not drag
    a fully stamped arm down to `unstamped`."""
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung(git_rev="a" * 40)},
                {"qwen-27b-t-post.json": _rung(git_rev="a" * 40)},
                "--mode", "threshold", "--rungs", "qwen-27b,floor")
    assert v["rungs"]["floor"]["status"] == "missing"
    assert v["pre"]["sha"] == "a" * 40
    assert v["pre"]["sha_source"] == "artifact"


def test_a_dirty_run_is_not_reported_as_a_clean_artifact_sha(
        monkeypatch, tmp_path):
    """`ladder_sweep.git_rev` suffixes `-dirty` when the run was made from
    an uncommitted tree, which is the normal case here. Reporting that as a
    plain `artifact` would be the confident-wrong-answer failure this whole
    block exists to stop, one hop along: the sha is a real commit, the code
    that ran was not it."""
    _, v = _run(monkeypatch, tmp_path,
                {"qwen-27b-t-pre.json": _rung(git_rev="a" * 40 + "-dirty")},
                {"qwen-27b-t-post.json": _rung(git_rev="b" * 40)},
                "--mode", "threshold", "--rungs", "qwen-27b")
    assert v["pre"]["sha"] == "a" * 40 + "-dirty"
    assert v["pre"]["sha_source"] == "artifact-dirty"
    # The clean arm is unaffected — the downgrade is per arm, not global.
    assert v["post"]["sha_source"] == "artifact"
