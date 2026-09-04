"""Fixture-level tests for the forgetting-sweep probe's sweep arms.

No DB, no daemon, no model, no bank dumps: every case here is a tiny
hand-built pool whose evidence entries are known by construction, so the
arm contracts the 2026-09-05 preregistration states can be checked
directly.

The contracts under test are the ones a wrong result would be blamed on:
`oracle` never deletes evidence (it is the ceiling), `random` is
seed-deterministic (it is the floor, and a floor that moves between runs
is not a floor), the policy arms delegate to the real
`RetentionPolicy.source_weighted_score` and evict its minimum, `none`
changes nothing, a capacity at or above the pool size is a no-op, and the
canonical artifact is never overwritten by accident.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

import forgetting_sweep_probe as fsp  # noqa: E402


def entry(text: str, *, surprise_seed: float = 0.0, superseded: bool = False,
          ts: float = 1000.0, source: str = "bench",
          emb: list[float] | None = None) -> dict:
    """A band-state dump entry in the shape `load_dumps` yields."""
    return {
        "text": text,
        "emb": emb if emb is not None else [1.0, 0.0],
        "ts": ts,
        "hist_ts": ts - 500.0,
        "source": source,
        "superseded_at": (ts + 1.0) if superseded else None,
        "slots": [],
        # not a dump field — only a handle for building fixtures
        "_surprise": surprise_seed,
    }


def pool(specs: list[tuple[str, float, bool]]) -> tuple[list[dict], list[float]]:
    """(entries, surprises) from (text, surprise, superseded) triples."""
    entries = [entry(t, surprise_seed=s, superseded=sup) for t, s, sup in specs]
    return entries, [e["_surprise"] for e in entries]


def texts(entries: list[dict]) -> list[str]:
    return [e["text"] for e in entries]


# ── `none`: the control must be a literal pass-through ────────────────────

def test_none_arm_returns_the_pool_unchanged_even_under_capacity():
    entries, sur = pool([(f"t{i}", i / 10.0, False) for i in range(8)])
    kept = fsp.sweep(entries, sur, capacity=2, arm="none",
                     now=2000.0, evidence=set(), seed=0)
    assert kept == entries
    assert all(a is b for a, b in zip(kept, entries))


# ── capacity no-op ────────────────────────────────────────────────────────

@pytest.mark.parametrize("arm", fsp.ARMS)
def test_capacity_at_or_above_pool_size_is_a_no_op(arm):
    entries, sur = pool([(f"t{i}", i / 10.0, i % 3 == 0) for i in range(6)])
    for capacity in (6, 7, 99):
        kept = fsp.sweep(entries, sur, capacity=capacity, arm=arm,
                         now=2000.0, evidence={"t1"}, seed=0)
        assert texts(kept) == texts(entries), (arm, capacity)


# ── `oracle`: the ceiling never deletes evidence ──────────────────────────

def test_oracle_never_evicts_an_evidence_entry():
    entries, sur = pool([(f"t{i}", i / 10.0, i % 2 == 0) for i in range(20)])
    evidence = {"t3", "t7", "t11", "t19"}
    for seed in range(5):
        kept = fsp.sweep(entries, sur, capacity=6, arm="oracle",
                         now=2000.0, evidence=evidence, seed=seed)
        assert len(kept) == 6
        assert evidence <= set(texts(kept)), seed


def test_oracle_keeps_only_evidence_when_capacity_equals_the_evidence_count():
    entries, sur = pool([(f"t{i}", 0.5, False) for i in range(10)])
    evidence = {"t2", "t5", "t8"}
    kept = fsp.sweep(entries, sur, capacity=3, arm="oracle",
                     now=2000.0, evidence=evidence, seed=1)
    assert set(texts(kept)) == evidence


# ── `random`: the floor must not move between runs ────────────────────────

def test_random_is_seed_deterministic():
    entries, sur = pool([(f"t{i}", 0.5, False) for i in range(30)])
    a = fsp.sweep(entries, sur, capacity=7, arm="random",
                  now=2000.0, evidence=set(), seed=11)
    b = fsp.sweep(entries, sur, capacity=7, arm="random",
                  now=2000.0, evidence=set(), seed=11)
    c = fsp.sweep(entries, sur, capacity=7, arm="random",
                  now=2000.0, evidence=set(), seed=12)
    assert texts(a) == texts(b)
    assert texts(a) != texts(c)
    assert len(a) == 7


def test_random_ignores_evidence():
    """The floor is uninformed by construction — if it dodged evidence it
    would be a second oracle and G-F3 would compare an arm against itself."""
    entries, sur = pool([(f"t{i}", 0.5, False) for i in range(30)])
    evidence = {f"t{i}" for i in range(20, 30)}
    kept = fsp.sweep(entries, sur, capacity=5, arm="random",
                     now=2000.0, evidence=evidence, seed=3)
    unaware = fsp.sweep(entries, sur, capacity=5, arm="random",
                        now=2000.0, evidence=set(), seed=3)
    assert texts(kept) == texts(unaware)


def test_cell_seed_does_not_depend_on_python_hash_randomization():
    """A seed derived from `hash()` differs between processes, which would
    make the floor unreproducible without any test ever failing in-process."""
    snippet = (
        "import sys; sys.path.insert(0, r'%s');"
        "import forgetting_sweep_probe as f;"
        "print(f.cell_seed('q1', '15x', 'C1', 'random'))" % (REPO / "evals")
    )
    outs = set()
    for hashseed in ("1", "2", "random"):
        res = subprocess.run([sys.executable, "-c", snippet], check=True,
                             capture_output=True, text=True,
                             env={**os.environ, "PYTHONHASHSEED": hashseed})
        outs.add(res.stdout.strip())
    assert len(outs) == 1, outs
    assert fsp.cell_seed("q1", "15x", "C1", "random") != \
        fsp.cell_seed("q1", "15x", "C1", "oracle")
    assert fsp.cell_seed("q1", "15x", "C1", "random") != \
        fsp.cell_seed("q2", "15x", "C1", "random")


# ── policy arms: the real `source_weighted_score`, minimum evicted ────────

def test_policy_arm_evicts_the_lowest_scoring_entries():
    entries, sur = pool([("lo", 0.1, False), ("mid", 0.5, False),
                         ("hi", 0.9, False), ("top", 1.0, False)])
    kept = fsp.sweep(entries, sur, capacity=2, arm="surprise_heavy",
                     now=2000.0, evidence=set(), seed=0)
    assert texts(kept) == ["hi", "top"]


def test_superseded_entries_are_evicted_before_live_ones():
    """`source_weighted_score` multiplies a superseded entry by 0.05, so a
    maximally surprising superseded turn still loses to a dull live one."""
    entries, sur = pool([("sup-high-surprise", 1.0, True),
                         ("live-no-surprise", 0.0, False)])
    kept = fsp.sweep(entries, sur, capacity=1, arm="surprise_heavy",
                     now=2000.0, evidence=set(), seed=0)
    assert texts(kept) == ["live-no-surprise"]


def test_ties_evict_the_earliest_inserted_entry():
    """`_evict_one` takes the FIRST minimum, so an all-tie pool is evicted
    oldest-first. Under `recency_heavy` with no access counts, every live
    entry ties — this rule IS that arm's behaviour."""
    entries, sur = pool([(f"t{i}", 0.5, False) for i in range(6)])
    kept = fsp.sweep(entries, sur, capacity=2, arm="recency_heavy",
                     now=2000.0, evidence=set(), seed=0)
    assert texts(kept) == ["t4", "t5"]


def test_balanced_and_surprise_heavy_agree_when_access_counts_are_absent():
    """Preregistered analytic claim: with `access_count` ≡ 0 both policies
    reduce to a strictly increasing function of surprise, so they induce
    the same total order and delete the same entries."""
    specs = [(f"t{i}", ((i * 37) % 100) / 100.0, i % 4 == 0) for i in range(40)]
    entries, sur = pool(specs)
    for capacity in (5, 13, 31):
        a = fsp.sweep(entries, sur, capacity=capacity, arm="balanced",
                      now=5000.0, evidence=set(), seed=0)
        b = fsp.sweep(entries, sur, capacity=capacity, arm="surprise_heavy",
                      now=5000.0, evidence=set(), seed=0)
        assert texts(a) == texts(b), capacity


def test_policy_arm_uses_the_shipped_retention_score():
    """Delegation, not reimplementation: the probe's per-entry scores must
    equal `RetentionPolicy.source_weighted_score` on the same entries."""
    from pseudolife_memory.memory.miras.retention import build_policy
    from types import SimpleNamespace

    entries, sur = pool([("a", 0.2, False), ("b", 0.8, True),
                         ("c", 0.5, False)])
    now = 4000.0
    got = fsp.eviction_scores(entries, sur, now, "balanced")
    policy = build_policy("balanced")
    want = [
        policy.source_weighted_score(
            SimpleNamespace(source=e["source"], reinforcements=0,
                            superseded_at=e["superseded_at"],
                            timestamp=e["ts"], access_count=0,
                            surprise_score=s), now)
        for e, s in zip(entries, sur)
    ]
    assert got == want


def test_sweep_preserves_pool_order():
    """`select_topk` tie-breaks on insertion ordinal, so a sweep that
    reordered the survivors would change the ranking on its own."""
    entries, sur = pool([(f"t{i}", ((i * 17) % 23) / 23.0, False)
                         for i in range(25)])
    for arm in fsp.ARMS:
        kept = fsp.sweep(entries, sur, capacity=9, arm=arm, now=2000.0,
                         evidence={"t2", "t20"}, seed=5)
        idx = [texts(entries).index(t) for t in texts(kept)]
        assert idx == sorted(idx), arm


def test_unknown_arm_is_rejected():
    entries, sur = pool([("a", 0.1, False), ("b", 0.2, False)])
    with pytest.raises(ValueError):
        fsp.sweep(entries, sur, capacity=1, arm="wishful",
                  now=1.0, evidence=set(), seed=0)


# ── surprise reconstruction ───────────────────────────────────────────────

def test_surprise_reconstruction_matches_compute_surprise_semantics():
    """`MIRASBand.compute_surprise` is 1 - max cosine to the resident
    entries, clamped to [0, 1], and 1.0 for an empty band."""
    es = [
        entry("first", emb=[1.0, 0.0]),        # empty band -> 1.0
        entry("orthogonal", emb=[0.0, 1.0]),   # 1 - 0 -> 1.0
        entry("duplicate", emb=[2.0, 0.0]),    # 1 - 1 -> 0.0 (magnitude ignored)
        entry("diagonal", emb=[1.0, 1.0]),     # 1 - cos45 -> 1 - 0.7071
    ]
    got = fsp.reconstruct_surprise(es)
    assert got[0] == pytest.approx(1.0)
    assert got[1] == pytest.approx(1.0)
    assert got[2] == pytest.approx(0.0, abs=1e-6)
    assert got[3] == pytest.approx(1.0 - 2 ** -0.5, abs=1e-6)


def test_surprise_reconstruction_is_clamped_into_the_unit_interval():
    es = [entry("a", emb=[1.0, 0.0]), entry("b", emb=[-1.0, 0.0])]
    got = fsp.reconstruct_surprise(es)
    assert 0.0 <= got[1] <= 1.0


# ── measurement, control check, pairing ───────────────────────────────────
# These four functions gate G-F0 and compute every published delta, and the
# only other thing that exercises them is a 20-minute run against gitignored
# dumps that CI does not have. Fixtures instead.

def _tiny_dump(entries: list[dict]) -> dict:
    return {"question": "alpha beta", "query_emb": [1.0, 0.0],
            "search_time": 3000.0, "question_ts": 2000.0,
            "bands": [{"name": "flat", "depth": 0, "entries": entries}]}


def test_measure_scores_the_documented_metrics():
    ev = entry("alpha beta gamma", emb=[1.0, 0.0])
    near = entry("alpha delta", emb=[0.95, 0.31])
    far = entry("zeta eta", emb=[0.0, 1.0])
    pool = [near, ev, far]
    m = fsp._measure(_tiny_dump(pool), pool, {"alpha beta gamma"})
    assert m["n_pool_entries"] == 3
    assert m["evidence_in_top6"] == 1.0
    assert m["any_evidence_served"] is True
    assert m["rank_first_evidence"] == 1          # exact cosine match wins
    assert m["evidence_survival"] == 1.0
    assert m["select_topk_latency_ms"] >= 0.0


def test_measure_reports_deleted_evidence_as_unsurvived():
    """`evidence_survival` is measured on the pool handed in, so a sweep that
    deleted the evidence must show 0.0 — not 1.0 from the original bank."""
    near = entry("alpha delta", emb=[0.95, 0.31])
    far = entry("zeta eta", emb=[0.0, 1.0])
    swept = [near, far]
    m = fsp._measure(_tiny_dump(swept), swept, {"alpha beta gamma"})
    assert m["evidence_survival"] == 0.0
    assert m["evidence_in_top6"] == 0.0
    assert m["any_evidence_served"] is False
    assert m["rank_first_evidence"] is None


def _reference(tmp_path: Path, rows: list[dict]) -> Path:
    repo = tmp_path / "repo"
    p = repo / "evals" / "results"
    p.mkdir(parents=True)
    (p / "distractor-scale-probe-2026-08-15.json").write_text(
        json.dumps({"per_question": rows}), encoding="utf-8")
    return repo


def _control_row(qid: str, **over) -> dict:
    base = {"n_pool_entries": 10, "evidence_in_top6": 0.5,
            "evidence_in_top3": 0.25, "any_evidence_served": True,
            "rank_first_evidence": 2}
    base.update(over)
    return {"question_id": qid, "none": {lbl: dict(base) for lbl, _ in fsp.SCALES}}


def test_control_check_passes_only_on_exact_agreement(tmp_path):
    rows = [_control_row("q1")]
    ref = [{"question_id": "q1",
            "scales": {lbl: dict(rows[0]["none"][lbl]) for lbl, _ in fsp.SCALES}}]
    got = fsp._control_check(rows, _reference(tmp_path, ref))
    assert got["exact_match"] is True
    assert got["n_cells_checked"] == len(fsp.SCALES)
    assert got["mismatches"] == []


@pytest.mark.parametrize("field,bad", [
    ("n_pool_entries", 11), ("evidence_in_top6", 0.6),
    ("evidence_in_top3", 0.3), ("any_evidence_served", False),
    ("rank_first_evidence", 3),
])
def test_control_check_flags_every_guarded_field(tmp_path, field, bad):
    rows = [_control_row("q1")]
    ref_scales = {lbl: dict(rows[0]["none"][lbl]) for lbl, _ in fsp.SCALES}
    ref_scales["15x"][field] = bad
    got = fsp._control_check(rows, _reference(
        tmp_path, [{"question_id": "q1", "scales": ref_scales}]))
    assert got["exact_match"] is False
    assert [m["field"] for m in got["mismatches"]] == [field]
    assert got["mismatches"][0]["scale"] == "15x"


def test_control_check_flags_a_question_absent_from_the_reference(tmp_path):
    got = fsp._control_check([_control_row("q1")], _reference(tmp_path, []))
    assert got["exact_match"] is False
    assert got["mismatches"][0]["question_id"] == "q1"
    assert got["n_cells_checked"] == 0


def test_control_check_cannot_pass_on_an_empty_run(tmp_path):
    """A control that checked nothing must not report a clean control."""
    got = fsp._control_check([], _reference(tmp_path, []))
    assert got["n_cells_checked"] == 0
    assert got["exact_match"] is True   # vacuous — so main must never get here
    # ...which is why an empty run cannot reach it: `--limit 0` means "all".
    assert fsp.N_QUESTIONS == 78


def test_paired_subtracts_b_from_a_in_that_order():
    by_cell = {
        ("C1", "15x", "oracle"): [{"evidence_in_top6": 1.0},
                                  {"evidence_in_top6": 0.5}],
        ("C1", "15x", "none"): [{"evidence_in_top6": 0.25},
                                {"evidence_in_top6": 0.25}],
    }
    got = fsp._paired(by_cell, "C1", "15x", "oracle", "none")
    assert got["comparison"] == "oracle - none"
    assert got["delta_mean"] == pytest.approx(0.5)   # (0.75 + 0.25) / 2
    flipped = fsp._paired(by_cell, "C1", "15x", "none", "oracle")
    assert flipped["delta_mean"] == pytest.approx(-0.5)
    assert 0.0 <= got["p"] <= 1.0


def test_aggregate_means_medians_and_drops_missing_ranks():
    rows = [
        {"evidence_in_top6": 1.0, "evidence_in_top3": 0.5,
         "evidence_survival": 1.0, "any_evidence_served": True,
         "rank_first_evidence": 1, "n_pool_entries": 10,
         "select_topk_latency_ms": 4.0, "bm25_latency_ms": 2.0},
        {"evidence_in_top6": 0.0, "evidence_in_top3": 0.0,
         "evidence_survival": 0.0, "any_evidence_served": False,
         "rank_first_evidence": None, "n_pool_entries": 20,
         "select_topk_latency_ms": 8.0, "bm25_latency_ms": 6.0},
    ]
    agg = fsp._aggregate(rows)
    assert agg["n_questions"] == 2
    assert agg["evidence_in_top6_mean"] == pytest.approx(0.5)
    assert agg["any_evidence_served_mean"] == pytest.approx(0.5)
    assert agg["evidence_survival_mean"] == pytest.approx(0.5)
    assert agg["rank_first_evidence_median"] == 1.0   # the None is dropped
    assert agg["n_pool_entries_mean"] == pytest.approx(15.0)
    assert agg["bm25_latency_ms_median"] == pytest.approx(4.0)


def test_aggregate_reports_no_rank_when_nothing_was_ever_served():
    rows = [{"evidence_in_top6": 0.0, "evidence_in_top3": 0.0,
             "evidence_survival": 0.0, "any_evidence_served": False,
             "rank_first_evidence": None, "n_pool_entries": 5,
             "select_topk_latency_ms": 1.0, "bm25_latency_ms": 1.0}]
    assert fsp._aggregate(rows)["rank_first_evidence_median"] is None


# ── corpus properties (the mechanism number the docs publish) ─────────────

def test_corpus_properties_counts_superseded_evidence_separately():
    """The published mechanism sentence is "gold evidence is superseded more
    often than an average turn", so the two rates must be computed over
    different denominators, not the same one."""
    dumps = {
        "q1": {"bands": [{"entries": [
            entry("a", superseded=True), entry("b"), entry("c"),
            entry("d", superseded=True)]}]},
        "q2": {"bands": [{"entries": [entry("e"), entry("f", superseded=True)]}]},
    }
    props = fsp.corpus_properties(dumps, {"q1": {"a", "b"}, "q2": {"f"}})
    assert props["n_questions"] == 2
    assert props["n_entries"] == 6
    assert props["n_superseded"] == 3
    assert props["superseded_rate"] == pytest.approx(0.5)
    assert props["n_evidence_entries"] == 3
    assert props["n_evidence_superseded"] == 2
    assert props["evidence_superseded_rate"] == pytest.approx(0.6667, abs=1e-4)


def test_corpus_properties_survives_an_evidence_free_question():
    dumps = {"q1": {"bands": [{"entries": [entry("a"), entry("b")]}]}}
    props = fsp.corpus_properties(dumps, {})
    assert props["n_evidence_entries"] == 0
    assert props["evidence_superseded_rate"] is None
    assert props["superseded_rate"] == pytest.approx(0.0)


# ── dump-directory resolution ─────────────────────────────────────────────
# The 2026-08-15 artifact was NOT produced from the directory
# `distractor_scale_probe.DUMP_DIR` names: that one holds the retired 384-d
# MiniLM replay, and its numbers do not reproduce the published control. The
# v25 dumps that DO reproduce it sit in a sibling directory, so the sweep
# probe identifies the right one by backbone dimension rather than by name.

def _fake_bank(root: Path, name: str, dim: int, n: int = 78,
               preset: str = "flat", evicted: bool = False) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for i in range(n):
        entries = [{"text": f"t{j}"} for j in range(4)]
        payload = {"query_emb": [0.0] * dim, "band_preset": preset,
                   "turns_stored": len(entries) + (3 if evicted else 0),
                   "bands": [{"name": "flat", "depth": 0, "entries": entries}]}
        with gzip.open(d / f"q{i:04d}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
    return d


def test_resolve_dump_dir_picks_the_v25_backbone(tmp_path):
    _fake_bank(tmp_path, "s-qwen-27b-ablbands-flat", 384)
    want = _fake_bank(tmp_path, "s-qwen-27b-ablbands-flat--somewhere", 1024)
    assert fsp.resolve_dump_dir(None, banks_root=tmp_path) == want


def test_resolve_dump_dir_ignores_a_short_directory(tmp_path):
    _fake_bank(tmp_path, "s-qwen-27b-ablbands-flat", 1024, n=12)
    want = _fake_bank(tmp_path, "s-qwen-27b-ablbands-flat--full", 1024)
    assert fsp.resolve_dump_dir(None, banks_root=tmp_path) == want


def test_resolve_dump_dir_rejects_a_capacity_scaled_replay(tmp_path):
    """A `flat257`-style arm is 1024-d and 78 dumps too, but it EVICTED
    during the replay — which breaks the exact surprise reconstruction the
    spec's substitution 2 depends on."""
    _fake_bank(tmp_path, "s-qwen-27b-ablbands-flat257", 1024,
               preset="flat257", evicted=True)
    want = _fake_bank(tmp_path, "s-qwen-27b-ablbands-flat--full", 1024)
    assert fsp.resolve_dump_dir(None, banks_root=tmp_path) == want


def test_resolve_dump_dir_refuses_when_ambiguous(tmp_path):
    _fake_bank(tmp_path, "s-qwen-27b-ablbands-flat--a", 1024)
    _fake_bank(tmp_path, "s-qwen-27b-ablbands-flat--b", 1024)
    with pytest.raises(SystemExit):
        fsp.resolve_dump_dir(None, banks_root=tmp_path)


def test_resolve_dump_dir_refuses_when_nothing_matches(tmp_path):
    _fake_bank(tmp_path, "s-qwen-27b-ablbands-flat", 384)
    with pytest.raises(SystemExit):
        fsp.resolve_dump_dir(None, banks_root=tmp_path)


def test_resolve_dump_dir_honours_an_explicit_path(tmp_path):
    d = _fake_bank(tmp_path, "s-qwen-27b-ablbands-flat", 384)
    assert fsp.resolve_dump_dir(d, banks_root=tmp_path) == d
    with pytest.raises(SystemExit):
        fsp.resolve_dump_dir(tmp_path / "nope", banks_root=tmp_path)


# ── the canonical artifact is never overwritten by accident ───────────────

def test_check_out_path_refuses_an_existing_artifact(tmp_path):
    p = tmp_path / "forgetting-sweep-probe-20260905.json"
    p.write_text(json.dumps({"keep": "me"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        fsp.check_out_path(p, force=False)
    assert json.loads(p.read_text(encoding="utf-8")) == {"keep": "me"}


def test_check_out_path_allows_force_and_a_fresh_path(tmp_path):
    p = tmp_path / "forgetting-sweep-probe-20260905.json"
    fsp.check_out_path(p, force=False)          # does not exist yet
    p.write_text("{}", encoding="utf-8")
    fsp.check_out_path(p, force=True)           # explicit overwrite


def test_main_refuses_before_doing_any_work(tmp_path):
    p = tmp_path / "already-there.json"
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        fsp.main(["--out", str(p)])
    assert p.read_text(encoding="utf-8") == "{}"
