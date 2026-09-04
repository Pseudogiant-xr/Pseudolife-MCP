"""Fixture tests for the lever-6 graph ablation harness.

CPU only, no database, no model, no git dependency: the DB seam
(``graph_shape``) and the service seam (``run_pairs``) are exercised
through fakes; the redactor is tested both against a stub and
against a real `git grep` over a throwaway repo.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import graph_ablation as ga  # noqa: E402


# Both DSN spellings libpq accepts, and neither is case-sensitive in the
# database name (2026-09-04 review: the URI-only, case-sensitive original
# waved `dbname=pseudolife_memory` and a trailing slash straight through).
_DSN_FORMS = (
    "postgresql://u:p@h:5433/{db}",
    "postgresql://u:p@h:5433/{db}/",
    "postgresql://u:p@h:5433/{DB}",
    "postgresql://u:p@h:5433/{db}?sslmode=disable",
    "host=h port=5433 dbname={db}",
    "dbname={DB} user=u",
)


@pytest.mark.parametrize("dsn_form", _DSN_FORMS)
def test_guard_refuses_the_live_and_bench_dbs(dsn_form):
    for db in ("pseudolife_memory", "pseudolife_memory_bench"):
        with pytest.raises(SystemExit):
            ga.guard_dsn(dsn_form.format(db=db, DB=db.upper()))
    ga.guard_dsn("postgresql://u:p@h:5433/pseudolife_memory_replay_20260904")
    ga.guard_dsn("host=h dbname=pseudolife_memory_replay_20260904")


def test_thirty_relational_questions_each_name_an_expected_entity():
    assert len(ga.RELATIONAL_QUESTIONS) == 30
    qs = [c["q"] for c in ga.RELATIONAL_QUESTIONS]
    assert len(set(qs)) == 30
    assert all(c["expect"] and c["q"] for c in ga.RELATIONAL_QUESTIONS)


def test_percentile_and_degree_map():
    assert ga.percentile([], 95.0) == 0.0
    assert ga.percentile([1.0], 95.0) == 1.0
    assert ga.percentile([1.0, 2.0, 3.0, 4.0], 50.0) == 3.0
    d = ga.degree_map([("a", "part-of", "b"), ("b", "uses", "c")])
    assert d == {"a": 1, "b": 2, "c": 1}


def test_classify_arrivals_splits_part_of_from_domain_edges():
    seeds = ["root"]
    entities = ["root", "childA", "peerB", "loose"]
    edges = [{"src": "root", "relation": "part-of", "dst": "childA"},
             {"src": "root", "relation": "depends-on", "dst": "peerB"}]
    degrees = {"root": 40, "childA": 1, "peerB": 1, "loose": 0}
    out = ga.classify_arrivals(seeds, entities, edges, degrees,
                               hub_threshold=30)
    assert out["added"] == 3
    assert out["via_part_of"] == 1     # childA
    assert out["via_domain"] == 1      # peerB
    assert out["unlinked"] == 1        # loose, no edge mentions it
    # both linked arrivals came through the high-degree root
    assert out["via_hub"] == 2


def test_a_mixed_edge_arrival_counts_as_domain_not_part_of():
    """An entity reached by a part-of edge AND a domain edge is real graph
    work, not containment — only a PURELY part-of arrival is cheap."""
    out = ga.classify_arrivals(
        ["root"], ["root", "mixed"],
        [{"src": "root", "relation": "part-of", "dst": "mixed"},
         {"src": "root", "relation": "depends-on", "dst": "mixed"}],
        {"root": 1, "mixed": 2}, hub_threshold=30)
    assert out["via_part_of"] == 0
    assert out["via_domain"] == 1


def test_classify_arrivals_counts_a_hub_arrival_by_its_own_degree():
    out = ga.classify_arrivals(
        ["root"], ["root", "hubby"],
        [{"src": "root", "relation": "uses", "dst": "hubby"}],
        {"root": 1, "hubby": 99}, hub_threshold=30)
    assert out["via_hub"] == 1 and out["via_domain"] == 1


def test_served_chars_counts_recall_facts_and_edges_not_just_texts():
    search_res = {"entries": [{"text": "abcd"}, {"text": "ef"}]}
    assert ga.served_chars_search(search_res) == 6
    recall_res = {
        "texts": ["abcd"],
        "entities": [{"entity": "ab",
                      "facts": [{"attribute": "cd", "value": "efg"}]}],
        "edges": [{"src": "a", "relation": "bb", "dst": "c"}],
    }
    # 4 (text) + 2 (entity) + 2 + 3 (fact) + 1 + 2 + 1 (edge)
    assert ga.served_chars_recall(recall_res) == 15


def test_expected_hit_matches_entity_names_and_text():
    res = {"entities": [{"entity": "ops-update-ps1", "facts": []}],
           "texts": []}
    assert ga.expected_hit_recall(res, "ops-update-ps1") is True
    assert ga.expected_hit_recall(res, "cortex-console") is False
    # hyphenated entity names are matched against prose text too
    assert ga.expected_hit_search(
        {"entries": [{"text": "run ops update ps1 first"}]},
        "ops-update-ps1") is True


def test_summarize_pairs_reports_ratios_and_exclusive_hits():
    def row(s_hit, r_hit, s_chars, r_chars, s_t, r_t, arrivals=None):
        return {"search": {"expected_hit": s_hit, "served_chars": s_chars,
                           "wall_s": s_t},
                "recall": {"expected_hit": r_hit, "served_chars": r_chars,
                           "wall_s": r_t, "low_confidence": False,
                           "arrivals": arrivals or {}}}

    pairs = [row(True, True, 100, 400, 0.1, 1.0, {"via_part_of": 2}),
             row(False, True, 100, 600, 0.1, 3.0, {"via_part_of": 1,
                                                   "via_domain": 1}),
             row(True, False, 100, 200, 0.1, 2.0)]
    s = ga.summarize_pairs(pairs)
    assert s["n"] == 3
    assert s["recall_only_hits"] == 1 and s["search_only_hits"] == 1
    assert s["search"]["mean_served_chars"] == 100.0
    assert s["recall"]["mean_served_chars"] == 400.0
    assert s["chars_ratio_recall_over_search"] == 4.0
    assert s["time_ratio_recall_over_search"] == 20.0
    assert s["arrivals_total"] == {"via_part_of": 3, "via_domain": 1}


def test_redactor_drops_names_absent_from_the_tracked_tree(monkeypatch):
    """The bank holds personal names and machine identifiers; only strings
    that are already in a public commit may reach the artifact."""
    r = ga.NameRedactor(Path("."), enabled=True)
    monkeypatch.setattr(r, "public", lambda n: n == "pseudolife-mcp")
    assert r("pseudolife-mcp") == "pseudolife-mcp"
    assert r("some-laptop-hostname") == "<redacted>"


def test_redactor_disabled_passes_everything_through():
    r = ga.NameRedactor(Path("."), enabled=False)
    assert r("anything-at-all") == "anything-at-all"


def test_redactor_redacts_when_git_is_unavailable(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no git here")

    monkeypatch.setattr(ga.subprocess, "run", boom)
    r = ga.NameRedactor(Path("."), enabled=True)
    assert r("pseudolife-mcp") == "<redacted>"


def test_redactor_runs_real_git_grep_against_a_tracked_tree(tmp_path):
    """The three tests above stub `public` or break subprocess, so none of
    them exercises the seam that actually decides what leaks: the `git
    grep` call. The 2026-09-04 review found a hostname in the published
    artifact, and the question "did the check pass it, or was it never
    run?" could not be answered from the suite. Drive the real path
    against a throwaway repo: a name in a tracked file survives, a name
    only in an UNTRACKED file does not, and matching is case-insensitive
    (the redactor passes `-i`, and entity names are normalised lower-case
    while the tree may spell them otherwise).
    """
    if shutil.which("git") is None:                     # pragma: no cover
        pytest.skip("git not on PATH")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True,
                   capture_output=True, timeout=30)
    (tmp_path / "tracked.md").write_text(
        "the Pseudolife-MCP daemon and its Cortex Console\n", encoding="utf-8")
    (tmp_path / "untracked.md").write_text(
        "some-laptop-hostname\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.md"], cwd=tmp_path, check=True,
                   capture_output=True, timeout=30)

    r = ga.NameRedactor(tmp_path, enabled=True)
    assert r("pseudolife-mcp") == "pseudolife-mcp"   # tracked, other case
    assert r("cortex console") == "cortex console"   # tracked, other case
    assert r("some-laptop-hostname") == "<redacted>"  # untracked file
    assert r("never-written-anywhere") == "<redacted>"
    # The decision is cached per name, not re-shelled on every entity.
    assert r._cache["some-laptop-hostname"] is False


def test_sample_evenly_is_deterministic_and_spans_the_list():
    xs = list(range(30))
    a = ga.sample_evenly(xs, 6)
    assert a == ga.sample_evenly(xs, 6) and len(a) == 6
    assert a[0] == 0 and a[-1] >= 20          # not a head slice
    assert ga.sample_evenly(xs, 0) is xs


def test_logged_hit_on_recall_matches_text_not_id():
    """`RecallState.texts` is a list of PLAIN STRINGS with no ids, so a
    logged case scored on an entry id would silently miss every time."""
    recall_res = {"texts": ["the daemon owns the bank volumes"]}
    hit = (lambda res, s="the daemon owns the bank volumes": any(
        (x if isinstance(x, str) else (x or {}).get("text", "")) == s
        for x in res.get("texts", [])))
    assert hit(recall_res) is True
    # the shape the bug produced: ids only, never present in `texts`
    id_hit = (lambda res, t=7: any(
        int((x or {}).get("id", -1)) == t
        for x in res.get("texts", []) if isinstance(x, dict)))
    assert id_hit(recall_res) is False


def test_run_pairs_pairs_search_and_recall_on_the_same_query():
    seen = []

    class _Svc:
        def search(self, q, top_k=6):
            seen.append(("search", q))
            return {"entries": [{"id": 1, "text": "the daemon runs"}]}

        def recall(self, q, top_k=6):
            seen.append(("recall", q))
            return {"seeds": ["daemon"],
                    "entities": [{"entity": "daemon", "facts": []},
                                 {"entity": "postgres", "facts": []}],
                    "edges": [{"src": "daemon", "relation": "stores-data-in",
                               "dst": "postgres"}],
                    "texts": ["the daemon runs"], "low_confidence": False}

    rows = ga.run_pairs(_Svc(), [{"q": "what does the daemon use",
                                  "expect": "postgres"}],
                        {"daemon": 5, "postgres": 5}, 4, 6)
    assert [s[0] for s in seen] == ["search", "recall"]
    assert rows[0]["search"]["expected_hit"] is False
    assert rows[0]["recall"]["expected_hit"] is True
    assert rows[0]["recall"]["arrivals"]["via_domain"] == 1
