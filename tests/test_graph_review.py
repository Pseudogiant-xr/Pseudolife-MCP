# tests/test_graph_review.py
from pseudolife_memory.memory import graph_review as gr


def _ents(*names):
    return [{"id": i + 1, "display": n, "canonical": n.lower(), "etype": None}
            for i, n in enumerate(names)]


def test_duplicate_candidates_flags_near_identical_names():
    ents = _ents("Cortex Console web frontend", "web frontend (Cortex Console)", "postgres")
    dups = gr.duplicate_candidates(ents)
    assert dups and dups[0]["type"] == "duplicate" and dups[0]["action"] == "merge"
    assert "postgres" not in dups[0]["label"]


def test_lesson_only_ids_identifies_lesson_minted_nodes():
    # A node whose EVERY edge is a lesson relation (prefers/avoids) is a
    # memory_outcome task/approach node, not a graph entity.
    edges = [{"src_id": 2, "dst_id": 9, "relation": "prefers"},
             {"src_id": 3, "dst_id": 4, "relation": "uses"}]
    ids = gr.lesson_only_ids(edges)
    assert 2 in ids and 9 in ids
    assert 3 not in ids and 4 not in ids
    # residual tail: lesson-referenced entities whose lesson edges were pruned
    assert 7 in gr.lesson_only_ids(edges, lesson_entity_ids=frozenset({7}))


def test_duplicate_candidates_skips_lesson_only_entities():
    # 2026-07-26: lesson-topic nodes shadow the real artifact they name
    # ("ops/update.ps1 deploy verification" vs "ops/update.ps1"). unattributed()
    # already excludes them; the duplicate analyzer must apply the SAME
    # predicate or these pairs regenerate after every dismissal.
    ents = _ents("ops/update.ps1", "ops/update.ps1 deploy verification")
    assert gr.duplicate_candidates(ents) != []          # without the signal
    assert gr.duplicate_candidates(ents, lesson_ids=frozenset({2})) == []


def test_orphans_exclude_lesson_only_entities():
    # Lesson nodes are weakly connected BY DESIGN (one prefers edge each), so
    # counting them inflates the informational orphan finding.
    ents = _ents("real-thing", "run a deploy and verify it")
    edges = [{"src_id": 2, "dst_id": 99, "relation": "prefers"}]
    assert "run a deploy and verify it" in gr.orphans(edges, ents)[0]["entities"]
    filtered = gr.orphans(edges, ents, lesson_ids=frozenset({2}))
    assert filtered[0]["entities"] == ["real-thing"]


def test_file_concept_pair_offers_relate_not_merge():
    # 2026-07-26 curation review: a source file and the feature/role it
    # implements are NOT duplicates — the concept usually has independent
    # runtime identity ("dream" runs-on the host shim; "band" stores-data-in
    # postgres), so merging asserts false things about the file. They are not
    # unrelated either, so a plain dismissal throws the link away. The finding
    # offers `relate`, and the file is listed first so the suggested edge
    # reads <file> implements <concept>.
    dups = gr.duplicate_candidates(_ents("band", "band.py"))
    assert dups, "the pair must still surface as a finding"
    f = dups[0]
    assert f["action"] == "relate"
    assert f["suggested_relation"] == "implements"
    assert f["entities"] == ["band.py", "band"]


def test_suggested_relations_resolve_against_the_builtin_vocabulary():
    # A `relate` finding is only actionable if the relation it suggests is in
    # the CLOSED registry a fresh bank ships with: `graph_relate` rejects an
    # unregistered name outright (`unknown_relation`), and `resolve_relation`
    # fuzzy-matches too tightly to rescue a miss -- "implements" scores no
    # builtin above the 0.5 cutoff, so the suggestion comes back with an EMPTY
    # `suggestions` list and the user gets no recovery hint at all. A bank that
    # happens to have the relation hand-defined hides this completely, which is
    # why the pinned-suggestion test above is not enough: assert against the
    # seed vocabulary, not against whatever the live registry has drifted to.
    from pseudolife_memory import graph as G
    from pseudolife_memory.storage.postgres import _BUILTIN_RELATIONS

    # `resolve_relation` takes the registry as a list of NAMES -- the same
    # `list(registry)` (dict keys) that `service.graph_relate` passes it.
    seeded = [name for name, _desc, _trans, _inv in _BUILTIN_RELATIONS]
    suggested = {
        f["suggested_relation"]
        for f in gr.duplicate_candidates(_ents("band", "band.py"))
        if "suggested_relation" in f
    }
    assert suggested, "the file/concept finding must suggest a relation"
    for relation in sorted(suggested):
        resolved, _suggestions = G.resolve_relation(seeded, relation)
        assert resolved is not None, (
            f"review suggests {relation!r}, which a fresh bank cannot store: "
            "add it to _BUILTIN_RELATIONS or suggest a seeded relation"
        )


def test_file_concept_detection_ignores_unrelated_and_two_file_pairs():
    # Only a file paired with ITS OWN bare stem qualifies. Two source files,
    # or a file next to an unrelated concept, keep the ordinary merge action.
    assert gr.file_concept_split("band.py", "band") == ("band.py", "band")
    assert gr.file_concept_split("evals/dg_shim.py", "dg_shim") == (
        "evals/dg_shim.py", "dg_shim")
    assert gr.file_concept_split("test_shim.py", "tests/test_shim.py") is None
    assert gr.file_concept_split("band.py", "postgres") is None
    assert gr.file_concept_split("README.md", "README") is None   # not code


def test_file_concept_split_ignores_git_branches():
    # Live verification 2026-07-26: `terra_shim.py` vs the branch
    # `feat/terra-shim` was offered as relate/implements, but a branch is a VCS
    # artifact, not a role a file realizes — a prior judge ruled that exact
    # pair distinct. Stripping the branch prefix makes the stems match, so the
    # branch counterpart has to be rejected explicitly.
    assert gr.file_concept_split("terra_shim.py", "feat/terra-shim") is None
    assert gr.file_concept_split("evals/terra_shim.py", "feat/terra-shim") is None
    assert gr.file_concept_split("band.py", "fix/band") is None
    assert gr.file_concept_split("band.py", "band") == ("band.py", "band")


def test_test_artifacts_matches_known_patterns():
    ents = _ents("payments/payments-db", "pl-healthcheck-target", "pseudolife-mcp")
    arts = gr.test_artifacts(ents)
    assert arts and arts[0]["action"] == "delete"
    assert set(arts[0]["entities"]) == {"payments/payments-db", "pl-healthcheck-target"}


def test_orphans_flags_degree_le_1():
    ents = _ents("a", "b", "lonely")
    edges = [{"src_id": 1, "relation": "uses", "dst_id": 2, "origin": "action", "confidence": 0.9}]
    orph = gr.orphans(edges, ents)
    assert orph and "lonely" in orph[0]["entities"]


def test_dubious_edges_flags_low_conf_agent():
    ents = _ents("memory_recall", "docker-desktop")
    edges = [{"src_id": 1, "relation": "runs-on", "dst_id": 2, "origin": "agent", "confidence": 0.6}]
    dub = gr.dubious_edges(ents and edges, ents)
    assert dub and dub[0]["action"] == "prune" and dub[0]["edges"][0]["src"] == "memory_recall"


def test_unattributed_flags_entities_without_sources():
    ents = _ents("attributed", "orphan-of-project")
    un = gr.unattributed(ents, {1: ["pseudolife"]})
    assert un and un[0]["entities"] == ["orphan-of-project"] and un[0]["action"] == "assign"


def test_unattributed_excludes_lesson_only_entities():
    # 2026-07-19 hygiene follow-up: lesson-minted task/approach entities exist
    # only as prefers/avoids endpoints — the mention-scan can never attribute
    # them, so flagging them is permanent queue noise.
    ents = _ents("deploy engine to host", "ops/update.ps1", "orphan-of-project")
    edges = [{"src_id": 1, "relation": "prefers", "dst_id": 2,
              "origin": "action", "confidence": 0.9}]
    un = gr.unattributed(ents, {}, edges)
    assert un and un[0]["entities"] == ["orphan-of-project"]


def test_unattributed_excludes_lesson_referenced_ids():
    # 2026-07-19 residual tail: entities referenced by lessons.entity_id /
    # object_entity_id whose prefers/avoids edges were later pruned have ZERO
    # edges, so the edge signal above can't identify them. The service passes
    # the lesson-referenced id set explicitly.
    ents = _ents("deploy engine to host", "orphan-of-project")
    un = gr.unattributed(ents, {}, (), lesson_entity_ids={1})
    assert un and un[0]["entities"] == ["orphan-of-project"]


def test_review_threads_lesson_entity_ids():
    ents = _ents("lesson-task-node")
    out = gr.review([], ents, {}, lesson_entity_ids={1})
    assert not [f for f in out["findings"] if f["type"] == "unattributed"]


def test_unattributed_keeps_entities_with_mixed_edges():
    # A prefers edge alone doesn't immunize: an entity also carrying normal
    # relations is a real graph citizen and stays flagged when sourceless.
    ents = _ents("task-ish", "docker-desktop")
    edges = [{"src_id": 1, "relation": "prefers", "dst_id": 2,
              "origin": "action", "confidence": 0.9},
             {"src_id": 2, "relation": "runs-on", "dst_id": 1,
              "origin": "agent", "confidence": 0.8}]
    un = gr.unattributed(ents, {}, edges)
    assert un and set(un[0]["entities"]) == {"task-ish", "docker-desktop"}


def test_review_aggregates_all_groups():
    ents = _ents("payments-db", "lonely")
    out = gr.review([], ents, {})
    types = {f["type"] for f in out["findings"]}
    assert {"test_artifact", "orphan", "unattributed"} <= types
    assert out["counts"]["total"] == len(out["findings"])


def _pairs(findings):
    return {frozenset(f["entities"]) for f in findings}


def test_version_and_phase_numbers_not_collapsed():
    ents = _ents("schema v8", "schema 11", "schema 15->16",
                 "Phase 1 plan", "Phase 2 plan",
                 "Atlas Stage 1", "Atlas Stage 2")
    assert gr.duplicate_candidates(ents) == []


def test_genuine_phrasing_duplicate_still_flagged():
    ents = _ents("memcot_bench.py", "memcot bench")
    assert frozenset({"memcot_bench.py", "memcot bench"}) in _pairs(gr.duplicate_candidates(ents))


def test_duplicate_candidates_skips_dismissed_pairs():
    # 2026-07-02 review fix 3: a human-dismissed false positive (postgres vs
    # postgres.py class) must stay dismissed across analyzer runs.
    ents = _ents("memcot_bench.py", "memcot bench")
    key = tuple(sorted((ents[0]["canonical"], ents[1]["canonical"])))
    assert gr.duplicate_candidates(ents, dismissed={key}) == []
    # and review() threads the set through
    out = gr.review([], ents, {1: ["p"], 2: ["p"]}, dismissed_pairs={key})
    assert not [f for f in out["findings"] if f["type"] == "duplicate"]


def test_legit_fixtures_and_lessons_not_flagged():
    ents = _ents("fixture devserver",
                 "TDD pattern: PG service test + fixture stubs + web routes")
    assert gr.test_artifacts(ents) == []


def test_real_test_artifacts_still_flagged():
    ents = _ents("deploy-smoke-foo", "pl-healthcheck-probe", "payments/payments-db",
                 "Cortex Console")  # a normal entity, must NOT be flagged
    out = gr.test_artifacts(ents)
    assert out and set(out[0]["entities"]) == {
        "deploy-smoke-foo", "pl-healthcheck-probe", "payments/payments-db"}


from pseudolife_memory.memory.graph_review import dubious_edges


def test_dubious_edges_discriminate_by_confidence():
    entities = _ents("a", "b", "c")
    ids = {e["display"]: e["id"] for e in entities}
    edges = [
        {"src_id": ids["a"], "relation": "runs-on", "dst_id": ids["b"],
         "origin": "agent", "confidence": 0.175},   # violation -> flagged
        {"src_id": ids["a"], "relation": "uses", "dst_id": ids["c"],
         "origin": "agent", "confidence": 0.70},      # good -> NOT flagged
    ]
    out = dubious_edges(edges, entities)
    assert out, "low-confidence edge should produce a finding"
    flagged = out[0]["edges"]
    assert len(flagged) == 1 and flagged[0]["confidence"] == 0.175


def test_proposed_links_finding_shape():
    props = [{"id": 7, "src": "alpha", "relation": "related-to", "dst": "beta",
              "confidence": 0.45, "similarity": 0.91, "rationale": "co-discussed"}]
    out = gr.proposed_links(props)
    assert len(out) == 1
    f = out[0]
    assert f["type"] == "proposed_link" and f["action"] == "review"
    assert f["links"][0]["src"] == "alpha" and f["links"][0]["dst"] == "beta"
    # the edge_proposals id must travel so the link is accept/reject-able
    assert f["links"][0]["id"] == 7


def test_proposed_links_empty_when_none():
    assert gr.proposed_links([]) == []


def test_review_includes_proposals_when_passed():
    out = gr.review([], [], {}, proposals=[
        {"src": "a", "relation": "related-to", "dst": "b", "confidence": 0.45}])
    assert any(f["type"] == "proposed_link" for f in out["findings"])


def test_merge_and_junk_candidate_findings():
    eprops = [
        {"id": 1, "kind": "merge", "entity": "live daemon", "into": "daemon",
         "score": 0.99, "reason": "token-subset"},
        {"id": 2, "kind": "junk", "entity": "2", "into": None, "reason": "bare-number"},
    ]
    out = gr.review([], [], {}, entity_proposals=eprops)
    types = {f["type"] for f in out["findings"]}
    assert "merge_candidate" in types and "junk_candidate" in types
    mc = next(f for f in out["findings"] if f["type"] == "merge_candidate")
    assert mc["merges"][0]["from"] == "live daemon" and mc["merges"][0]["into"] == "daemon"
    jc = next(f for f in out["findings"] if f["type"] == "junk_candidate")
    assert jc["entities"][0]["entity"] == "2" and jc["entities"][0]["reason"] == "bare-number"


def test_entity_proposals_default_none_no_findings():
    out = gr.review([], [], {})
    assert all(f["type"] not in ("merge_candidate", "junk_candidate") for f in out["findings"])


def test_classify_edge_extracted_wins_over_low_confidence():
    from pseudolife_memory.memory.graph_review import classify_edge
    assert classify_edge({"origin": "user", "confidence": 0.2}) == "EXTRACTED"
    assert classify_edge({"origin": "action", "confidence": 0.9}) == "EXTRACTED"


def test_classify_edge_ambiguous_on_low_confidence_or_proposed():
    from pseudolife_memory.memory.graph_review import classify_edge
    assert classify_edge({"origin": "agent", "confidence": 0.4}) == "AMBIGUOUS"
    assert classify_edge({"origin": "agent", "confidence": 0.9},
                         proposed=True) == "AMBIGUOUS"


def test_classify_edge_inferred_default():
    from pseudolife_memory.memory.graph_review import classify_edge
    assert classify_edge({"origin": "agent", "confidence": 0.8}) == "INFERRED"
    assert classify_edge({"origin": None, "confidence": None}) == "INFERRED"


def test_dubious_edge_rows_carry_ambiguous_tag():
    from pseudolife_memory.memory import graph_review as gr
    edges = [{"src_id": 1, "dst_id": 2, "relation": "x",
              "confidence": 0.3, "origin": "agent"}]
    entities = [{"id": 1, "display": "a", "etype": None},
                {"id": 2, "display": "b", "etype": None}]
    findings = gr.dubious_edges(edges, entities)
    assert findings and all(r["tag"] == "AMBIGUOUS" for r in findings[0]["edges"])


def test_near_duplicate_names_matches_token_identical_variant():
    from pseudolife_memory.memory.graph_review import near_duplicate_names
    existing = [{"id": 7, "canonical": "graph-review-py",
                 "display": "graph_review.py", "aliases": []}]
    got = near_duplicate_names("graph review", existing)
    assert got and got[0]["entity_id"] == 7 and got[0]["score"] == 1.0


def test_near_duplicate_names_matches_via_alias():
    from pseudolife_memory.memory.graph_review import near_duplicate_names
    existing = [{"id": 3, "canonical": "dev-box", "display": "dev-box",
                 "aliases": ["gaming rig 4090"]}]
    got = near_duplicate_names("4090 gaming rig", existing)
    assert got and got[0]["entity_id"] == 3


def test_near_duplicate_names_respects_dismissed_and_threshold():
    from pseudolife_memory.memory.graph_review import near_duplicate_names
    existing = [{"id": 1, "canonical": "postgres-py",
                 "display": "postgres.py", "aliases": []}]
    dismissed = frozenset({("postgres", "postgres-py")})
    assert near_duplicate_names("postgres", existing, dismissed=dismissed) == []
    # unrelated name: below threshold
    assert near_duplicate_names("cortex console", existing) == []
    # disabled
    assert near_duplicate_names("postgres", existing, min_jaccard=0) == []


def test_near_duplicate_names_blocks_variant_conflicts():
    from pseudolife_memory.memory.graph_review import near_duplicate_names
    existing = [{"id": 7, "canonical": "gemma-4-e2b-it-qat-ud-q4-k-xl",
                 "display": "gemma-4-E2B-it-qat-UD-Q4_K_XL", "aliases": []}]
    hits = near_duplicate_names("gemma-4-E4B-it-qat-UD-Q4_K_XL", existing,
                                min_jaccard=0.3)
    assert hits == []


def test_near_duplicate_names_still_matches_same_variant():
    from pseudolife_memory.memory.graph_review import near_duplicate_names
    existing = [{"id": 7, "canonical": "ops-update-ps1",
                 "display": "ops/update.ps1", "aliases": []}]
    hits = near_duplicate_names("update.ps1", existing, min_jaccard=0.3)
    assert [h["entity_id"] for h in hits] == [7]


def test_proposed_links_carry_the_link_judge_opinion():
    # Schema v35: a judged link row shows the verdict beside the evidence,
    # exactly as merge rows show the merge judge's; unjudged rows show
    # nothing.
    rows = gr.proposed_links([
        {"id": 1, "src": "a", "relation": "uses", "dst": "b", "confidence": 0.7,
         "source": "deep-dream", "judge_verdict": "retype", "judge_confidence": 0.9,
         "judge_note": "direction", "judge_model": "m", "judge_relation": "implements"},
        {"id": 2, "src": "c", "relation": "uses", "dst": "d", "confidence": 0.7},
    ])[0]["links"]
    assert rows[0]["judge"] == {"verdict": "retype", "confidence": 0.9,
                                "note": "direction", "model": "m",
                                "relation": "implements"}
    assert rows[0]["source"] == "deep-dream"
    assert "judge" not in rows[1]


def test_merge_candidates_carry_the_second_opinion():
    finding = gr.merge_candidates([
        {"id": 5, "kind": "merge", "entity": "x svc", "into": "x service",
         "score": 0.9, "reason": "t", "judge_verdict": "reject",
         "judge_confidence": 0.6, "judge_note": "n", "judge_model": "m",
         "judge2_verdict": "accept", "judge2_confidence": 0.7, "judge2_model": "m2"},
    ])[0]["merges"][0]
    assert finding["judge"]["verdict"] == "reject"
    assert finding["judge2"] == {"verdict": "accept", "confidence": 0.7, "model": "m2"}
