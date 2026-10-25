"""The review-queue judges' differential scenarios (task P3b), merged into
``w2e.py``'s ``SCENARIOS`` by ``scenarios(w2e_module)``.

Each judge runs from the sweep, as the merge judge does, so these follow the
``judge-merge-*`` pattern: the merge judge's seed (twelve entities on a
hub, notes mentioning them), one shared stub answering by display, the
pending proposals inserted out of band on both banks at once, then the
judges' settle, and the banks, answers and request sequences compared. The
judge under test is on; every other judge and sweep step is off.

The link judge (``link-*``): shadow records first opinions and asks a
second opinion on a below-gate accept or reject; auto promotes an accept at
its gate (the edge floored to 0.7, origin ``action``), rejects at its gate
(an analyzer row's pair closed, an automatic decision recorded either way),
records a retype without applying it, stamps a skipped row as a
zero-confidence leave, and after an accept leaves the batch rows sharing
its endpoints for the next tick. ``link-two-vote`` applies two agreeing
votes from distinct models and leaves a split pending;
``link-two-vote-same-model`` refuses agreeing votes when the second
endpoint served the first model; ``link-auto-reopen`` moves a rejected
row's evidence, so the automatic reject reopens and is judged again.

The candidate judge (``candidate-*``) runs on the deep dream's seed: a
``memory_dream deep`` apply writes the watermark it waits for, then each
tick judges one ``judge_batch`` slice of the scan's candidates. Shadow
memoises every verdict only; auto files a propose as a ``deep-dream-judge``
link proposal (the slice's later rows sharing an endpoint wait for the next
tick), dismisses a pair and records the automatic dismissal, leaves a
propose naming another pair or under its gate, and stamps a skipped row.
``candidate-auto-reopen`` moves a dismissed pair's evidence, so the
dismissal reopens and the pair is judged again. The memo, the watermark and
the decision records carry Python's fingerprints, compared as they are.

The junk judge (``junk-*``): shadow records verdicts only; auto keeps (the
proposal rejected, its keep pair closed, an automatic decision recorded),
deletes under the evidence bar (degree at most ``junk_max_auto_degree``, at
most one fact; snapshot first), leaves a delete above the bar or under its
confidence gate pending with the verdict attached, and stamps a row the
model skipped as a zero-confidence leave. ``junk-auto-reopen`` moves a kept
entity's evidence, so the automatic keep is reopened and judged again.
"""
from __future__ import annotations

JUNK_ALL_JUDGED = ("SELECT NOT EXISTS (SELECT 1 FROM entity_proposals "
                   "WHERE kind = 'junk' AND status = 'pending' AND judge_verdict IS NULL)")

# The junk proposals, by entity (canonical) and the detector reason the
# prompt shows.
JUNK = {"alpha-node": "list-artifact", "beta-store": "compound", "gamma-tool": "slot-key",
        "delta-unit": "list-artifact", "hub": "compound", "llama-4b": "slot-key",
        "delta-node": "compound"}

LINK_ALL_JUDGED = ("SELECT NOT EXISTS (SELECT 1 FROM edge_proposals "
                   "WHERE status = 'pending' AND judge_verdict IS NULL)")

# The link proposals, by key: (src, relation, dst, proposal confidence,
# source). The proposal confidence orders the queue.
LINKS = {
    "P1": ("alpha-node", "uses", "beta-store", 0.5, "deep-dream"),
    "P7": ("beta-store", "uses", "hub", 0.45, "deep-dream"),
    "P2": ("gamma-node", "uses", "delta-unit", 0.4, "analyzer"),
    "P3": ("gamma-tool", "uses", "llama-4b", 0.35, "deep-dream"),
    "P4": ("epsilon-box", "uses", "llama-26b", 0.3, "deep-dream"),
    "P5": ("beta-node", "uses", "delta-node", 0.25, "deep-dream"),
    "P6": ("epsilon-node", "uses", "alpha-service", 0.2, "deep-dream"),
}
LINK_KEYS = {"P1": "Alpha Node|Beta Store", "P7": "Beta Store|Hub",
             "P2": "Gamma Node|Delta Unit", "P3": "Gamma Tool|Llama 4B",
             "P4": "Epsilon Box|Llama 26B", "P5": "Beta Node|Delta Node",
             "P6": "Epsilon Node|Alpha Service"}

# The judge mutants (src/dream/queue_judges.rs, src/dream/candidates.rs),
# caught by the scenario beside each.
MUTANTS = ["mcp-dream-junk-no-evidence-bar",          # junk-auto
           "mcp-dream-junk-keep-unrecorded",          # junk-auto
           "mcp-dream-link-retype-applied",           # link-auto
           "mcp-dream-link-two-vote-same-model",      # link-two-vote-same-model
           "mcp-dream-candidate-dismiss-unrecorded",  # candidate-auto
           "mcp-dream-candidate-endpoint-stale",      # candidate-auto
           # candidate-auto, by Python's fingerprints over the bank
           "mcp-dream-candidate-real-binary"]

# Every candidate of the current scan judged under this apply.
CANDIDATES_DONE = (
    "SELECT EXISTS (SELECT 1 FROM meta j JOIN meta a ON a.key = 'deep_last_apply' "
    "WHERE j.key = 'deep_candidates_judged' AND (j.value->>'complete')::boolean "
    "AND j.value->'ts' = a.value->'ts')")
CANDIDATE_REOPENED = (
    "SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE 'automatic_review_decision_v1:%' "
    "AND value->>'queue' = 'candidate' AND value->>'state' = 'reopened') "
    "AND EXISTS (SELECT 1 FROM meta WHERE key LIKE 'automatic_review_decision_v1:%' "
    "AND value->>'queue' = 'candidate' AND value->>'state' = 'active')")


def fingerprint_diffs(dsns: dict, config_text: str) -> list[str]:
    """Python's own candidate fingerprints over each side's bank: the scan
    generation against the bank's watermark, and each active automatic
    dismissal's evidence against its record. The bank is the same on both
    sides of each comparison, so each side's clocks cancel out, and a read
    that renders a column unlike ``storage/postgres.py`` shows here even
    though the cross-side comparison only sees the hashes' presence."""
    import tempfile
    from types import SimpleNamespace

    import psycopg
    from pseudolife_memory.memory.review_decisions import candidate_evidence_fingerprints
    from pseudolife_memory.memory.review_judgments import candidate_generation
    from pseudolife_memory.storage import postgres
    from pseudolife_memory.utils.config import load_config

    class ReadStorage:
        """The storage reads the candidate inputs make, on one plain
        connection: no schema work, no writer lease."""
        def __init__(self, conn):
            self.conn = conn

    for name in ("load_graph", "load_entries", "entity_sources_map", "traces_by_entity_norm",
                 "entity_fact_counts", "current_fact_counts_by_entity_text",
                 "pending_entity_proposals", "lesson_entity_ids", "pending_proposals",
                 "dismissed_pairs"):
        setattr(ReadStorage, name, getattr(postgres.PostgresStorage, name))
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/config.yaml"
        with open(path, "w", encoding="utf-8") as f:
            f.write(config_text)
        cfg = load_config(path)
    out = []
    for side in ("python", "rust"):
        with psycopg.connect(dsns[side], autocommit=True) as conn:
            postgres.register_vector(conn)
            svc = SimpleNamespace(_storage=ReadStorage(conn), config=cfg)
            mark = conn.execute(
                "SELECT value FROM meta WHERE key = 'deep_candidates_judged'").fetchone()
            want = candidate_generation(svc)
            if not mark or mark[0].get("generation") != want:
                out.append(f"candidate generation {side}: bank {want}, watermark "
                           f"{mark[0].get('generation') if mark else None}")
            for (rec,) in conn.execute(
                    "SELECT value FROM meta WHERE key LIKE 'automatic_review_decision_v1:%' "
                    "AND value->>'queue' = 'candidate' AND value->>'state' = 'active'"):
                got = candidate_evidence_fingerprints(svc, [rec.get("candidate") or {}])[0]
                if got != rec.get("evidence_fingerprint"):
                    out.append(f"candidate evidence {side} {rec.get('target')}: bank {got}, "
                               f"record {rec.get('evidence_fingerprint')}")
    return out


def scenarios(w) -> dict:
    """The scenarios, built with ``w2e``'s helpers (``w`` is the module)."""

    def config(mode: str, extra: str = "") -> str:
        text = w.judge_config("off", second=None, extra=extra)
        return text.replace('junk_judge_mode: "off"', f'junk_judge_mode: "{mode}"')

    def stub(verdicts: dict, *, model: str = "judge-one") -> dict:
        return {"model": "judge-one", "reply_model": model,
                "replies": [{"kind": "judge_junk", "verdicts": verdicts}]}

    def junk_insert(*entities: str) -> dict:
        rows = ", ".join(f"('junk', {w.eid(e)}, NULL, NULL, '{JUNK[e]}', 1.0)"
                         for e in entities)
        op = w.external("INSERT INTO entity_proposals (kind, entity_id, into_id, score, "
                        f"reason, created_at) VALUES {rows}")
        op["settle"] = {**w.JUDGE_SETTLE, "until": JUNK_ALL_JUDGED}
        return op

    def junk_row(entity: str, cond: str) -> str:
        return ("SELECT EXISTS (SELECT 1 FROM entity_proposals p JOIN entities e "
                f"ON e.id = p.entity_id WHERE p.kind = 'junk' AND e.canonical = '{entity}' "
                f"AND {cond})")

    def gone(entity: str) -> str:
        return f"SELECT NOT EXISTS (SELECT 1 FROM entities WHERE canonical = '{entity}')"

    def keep_pair(entity: str) -> str:
        return ("SELECT EXISTS (SELECT 1 FROM dismissed_pairs "
                f"WHERE a_norm = 'junk:{entity}' AND b_norm = 'junk:{entity}')")

    def link_config(mode: str, two_vote: str, second: str | None = "judge-two") -> str:
        text = w.judge_config("off", second=second,
                              extra=f"    link_judge_two_vote: \"{two_vote}\"\n")
        return text.replace('link_judge_mode: "off"', f'link_judge_mode: "{mode}"')

    def link_stub(first: dict, second: dict | None = None, *,
                  second_model: str = "judge-two") -> dict:
        by_pair = lambda table: {LINK_KEYS[k]: v for k, v in table.items()}  # noqa: E731
        replies = [{"kind": "judge_links", "verdicts": by_pair(first)}]
        if second is not None:
            replies.append({"kind": "judge_links", "endpoint": "second",
                            "verdicts": by_pair(second)})
        return {"model": "judge-one", "reply_model": "judge-one",
                "second_reply_model": second_model, "replies": replies}

    def link_insert(*keys: str, until: str = LINK_ALL_JUDGED) -> dict:
        rows = ", ".join(
            f"({w.eid(LINKS[k][0])}, '{LINKS[k][1]}', {w.eid(LINKS[k][2])}, {LINKS[k][3]}, "
            f"'detector: {k.lower()}', '{LINKS[k][4]}', 1.0)" for k in keys)
        op = w.external("INSERT INTO edge_proposals (src_id, relation, dst_id, confidence, "
                        f"rationale, source, created_at) VALUES {rows}")
        op["settle"] = {**w.JUDGE_SETTLE, "until": until}
        return op

    def link_row(key: str, cond: str) -> str:
        return ("SELECT EXISTS (SELECT 1 FROM edge_proposals "
                f"WHERE rationale = 'detector: {key.lower()}' AND {cond})")

    def link_edge(key: str) -> str:
        src, rel, dst = LINKS[key][:3]
        return ("SELECT EXISTS (SELECT 1 FROM edges e JOIN entities s ON s.id = e.src_id "
                f"JOIN entities d ON d.id = e.dst_id WHERE s.canonical = '{src}' "
                f"AND d.canonical = '{dst}' AND e.relation = '{rel}' "
                "AND e.origin = 'action' AND e.confidence >= 0.69 "
                "AND e.superseded_at IS NULL)")

    def link_pair(key: str) -> str:
        a, b = sorted((LINKS[key][0], LINKS[key][2]))
        return ("SELECT EXISTS (SELECT 1 FROM dismissed_pairs "
                f"WHERE a_norm = '{a}' AND b_norm = '{b}')")

    def link_decision(key: str, pair: bool) -> str:
        cond = "value->'pair' IS NOT NULL AND jsonb_typeof(value->'pair') = 'array'" if pair \
            else "jsonb_typeof(value->'pair') = 'null'"
        return ("SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                "'automatic_review_decision_v1:%' AND value->>'queue' = 'link' "
                "AND value->>'action' = 'reject' AND value->>'state' = 'active' "
                f"AND value->'proposal'->>'rationale' = 'detector: {key.lower()}' AND {cond})")

    def candidate_config(mode: str) -> str:
        text = w.judge_config("off", second=None, extra=(
            "    min_similarity: -1.0\n    merge_min_similarity: -1.0\n"
            "    min_entity_mentions: 1\n    top_k_candidates: 25\n"
            "    curation_min_similarity: -1.0\n    curation_top_k: 8\n"))
        text = text.replace("memory:\n", "memory:\n  scopes:\n"
                            "    exclude: [status, claude, agent, correction, noise]\n"
                            "    rollup:\n      proj-a: family\n", 1)
        return text.replace('candidate_judge_mode: "off"', f'candidate_judge_mode: "{mode}"')

    def candidate_stub(verdicts: dict) -> dict:
        return {"model": "judge-one", "reply_model": "judge-one",
                "replies": [{"kind": "judge_candidates", "verdicts": verdicts}]}

    def apply_then_judge() -> dict:
        op = w.dream("deep", apply=True)
        op["settle"] = {**w.JUDGE_SETTLE, "until": CANDIDATES_DONE}
        return op

    def checked(op: dict, mode: str) -> dict:
        """``op`` with Python's fingerprints checked over each bank."""
        return {**op, "candidate_fingerprints": candidate_config(mode)}

    def candidate_scenario(mode: str, verdicts: dict, ops: list) -> dict:
        return {"config": candidate_config(mode), "seed_config": w.deep_config(),
                "seed": w.DEEP_SEED, "extractor": candidate_stub(verdicts),
                "shared_stub": True, "ops": ops}

    def proposed(src: str, dst: str) -> str:
        return ("SELECT EXISTS (SELECT 1 FROM edge_proposals p "
                "JOIN entities s ON s.id = p.src_id JOIN entities d ON d.id = p.dst_id "
                f"WHERE p.source = 'deep-dream-judge' AND ((s.canonical = '{src}' AND "
                f"d.canonical = '{dst}') OR (s.canonical = '{dst}' AND d.canonical = '{src}')))")

    def candidate_pair(a: str, b: str) -> str:
        a, b = sorted((a, b))
        return ("SELECT EXISTS (SELECT 1 FROM dismissed_pairs "
                f"WHERE a_norm = '{a}' AND b_norm = '{b}')")

    candidate_decision = ("SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                          "'automatic_review_decision_v1:%' AND value->>'queue' = 'candidate' "
                          "AND value->>'action' = 'dismiss' AND value->>'state' = 'active')")
    memo_count = ("SELECT (SELECT COUNT(*) FROM jsonb_object_keys("
                  "(SELECT value->'pairs' FROM meta WHERE key = 'deep_candidate_verdicts'))) "
                  "> 0")
    candidate_verdicts = {
        # filed; the slice's later rows on Warehouse or Tide Gauge wait
        "Warehouse|Tide Gauge": w.V("propose", 0.9, relation="related-to"),
        # dismissed and recorded
        "postgres|Kafka Broker": w.V("dismiss", 0.9),
        # shares Warehouse with the filed pair: waits a tick, then dismissed
        # on the evidence the filing moved (dismissed at once, it would be
        # reopened by the next tick's reconsideration)
        "Warehouse|Kelp Farm": w.V("dismiss", 0.9),
        # a propose naming another pair: left
        "billing-service|Kafka Broker Cluster": {
            **w.V("propose", 0.95, relation="uses"), "src": "Somebody Else"},
        # under the gate: left
        "deploy agent|Kafka Broker Cluster": w.V("propose", 0.4, relation="uses"),
        # every other candidate gets no verdict: a stamped leave
    }

    two_vote_done = ("SELECT NOT EXISTS (SELECT 1 FROM edge_proposals WHERE status = 'pending' "
                     "AND (judge_verdict IS NULL OR (judge_verdict IN ('accept', 'reject') "
                     "AND judge2_verdict IS NULL AND judge_confidence < 0.8)))")

    V = w.V
    link_auto = {
        # promoted; P7 shares Beta Store, so it waits for the next tick
        "P1": V("accept", 0.95, note="a real use"),
        "P7": V("accept", 0.9),
        # rejected: the analyzer row's pair closes
        "P2": V("reject", 0.9, note="no such use"),
        "P3": V("reject", 0.85),
        # recorded, never applied
        "P4": V("retype", 0.95, relation="depends-on"),
        # under the accept gate
        "P5": V("accept", 0.6),
        # P6 gets no verdict: stamped as a zero-confidence leave
    }
    auto_verdicts = {
        # deleted: degree 1, no facts
        "Alpha Node": V("delete", 0.95, note="a captured list"),
        # kept: rejected, keep pair, automatic decision
        "Beta Store": V("keep", 0.9, note="a real store"),
        # under the delete gate: pending with the verdict
        "Gamma Tool": V("delete", 0.7),
        # above the evidence bar (the hub's degree is twelve): pending
        "Hub": V("delete", 0.99),
        # a leave: pending
        "Llama 4B": V("leave", 0.4),
        # above the fact bar (two current facts at degree one): pending
        "Delta Node": V("delete", 0.99),
        # Delta Unit gets no verdict: stamped as a zero-confidence leave
    }
    return {
        "candidate-shadow": candidate_scenario("shadow", candidate_verdicts, [
            w.expect(checked(apply_then_judge(), "shadow"), memo_count,
                     "SELECT NOT EXISTS (SELECT 1 FROM edge_proposals "
                     "WHERE source = 'deep-dream-judge')",
                     f"SELECT NOT ({candidate_pair('postgres', 'kafka-broker')[7:]})"),
            w.review()]),
        "candidate-auto": candidate_scenario("auto", candidate_verdicts, [
            w.expect(checked(apply_then_judge(), "auto"), memo_count,
                     proposed("warehouse", "tide-gauge"),
                     candidate_pair("postgres", "kafka-broker"), candidate_decision,
                     candidate_pair("warehouse", "kelp-farm"),
                     "SELECT NOT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                     "'automatic_review_decision_v1:%' AND value->>'queue' = 'candidate' "
                     "AND value->>'state' = 'reopened')",
                     f"SELECT NOT ({proposed('billing-service', 'kafka-broker-cluster')[7:]})",
                     f"SELECT NOT ({proposed('deploy-agent', 'kafka-broker-cluster')[7:]})"),
            w.review()]),
        # The dismissed pair's postgres gains an edge: the dismissal's
        # evidence moves, it reopens (the pair removed, the record retired)
        # and the judge dismisses it again.
        "candidate-auto-reopen": candidate_scenario("auto", candidate_verdicts, [
            w.expect(checked(apply_then_judge(), "auto"),
                     candidate_pair("postgres", "kafka-broker"), candidate_decision),
            w.expect({**w.external(
                "INSERT INTO edges (src_id, relation, dst_id, confidence, origin, "
                f"asserted_at) VALUES ({w.eid('postgres')}, 'uses', {w.eid('ocean')}, "
                "0.8, NULL, 1.0)"),
                "settle": {**w.JUDGE_SETTLE, "until": CANDIDATE_REOPENED},
                "candidate_fingerprints": candidate_config("auto")},
                candidate_pair("postgres", "kafka-broker"), candidate_decision),
            w.review()]),
        "link-shadow": w.judge_scenario(
            link_config("shadow", "shadow"),
            link_stub({"P1": V("accept", 0.95), "P2": V("reject", 0.7, note="weak")},
                      {"P2": V("reject", 0.9)}),
            [w.expect(link_insert("P1", "P2", until=f"{LINK_ALL_JUDGED} AND " + link_row(
                          "P2", "judge2_verdict IS NOT NULL").removeprefix("SELECT ")),
                      link_row("P1", "status = 'pending' AND judge_verdict = 'accept' "
                                     "AND judge2_verdict IS NULL"),
                      link_row("P2", "status = 'pending' AND judge2_verdict = 'reject' "
                                     "AND judge_note = 'weak | 2nd (judge-two): reject 0.90 "
                                     "[agree]'")),
             w.review()]),
        "link-auto": w.judge_scenario(
            link_config("auto", "off"),
            link_stub(link_auto),
            [w.expect(link_insert("P1", "P7", "P2", "P3", "P4", "P5", "P6"),
                      link_row("P1", "status = 'accepted' AND decided_by = 'dream-judge'"),
                      link_edge("P1"),
                      link_row("P7", "status = 'accepted'"), link_edge("P7"),
                      link_row("P2", "status = 'rejected' AND decided_by = 'dream-judge'"),
                      link_pair("P2"), link_decision("P2", pair=True),
                      link_row("P3", "status = 'rejected'"), link_decision("P3", pair=False),
                      link_row("P4", "status = 'pending' AND judge_verdict = 'retype' "
                                     "AND judge_relation = 'depends-on'"),
                      link_row("P5", "status = 'pending' AND judge_verdict = 'accept'"),
                      link_row("P6", "status = 'pending' AND judge_verdict = 'leave' "
                                     "AND judge_confidence = 0")),
             w.review()]),
        "link-two-vote": w.judge_scenario(
            link_config("auto", "auto"),
            link_stub({"P1": V("accept", 0.75), "P2": V("reject", 0.7),
                       "P3": V("accept", 0.6), "P4": V("retype", 0.9, relation="part-of")},
                      {"P1": V("accept", 0.85), "P2": V("reject", 0.8),
                       "P3": V("reject", 0.9)}),
            [w.expect(link_insert("P1", "P2", "P3", "P4", until=two_vote_done),
                      link_row("P1", "status = 'accepted' AND judge2_verdict = 'accept'"),
                      link_edge("P1"),
                      link_row("P2", "status = 'rejected'"), link_pair("P2"),
                      link_decision("P2", pair=True),
                      link_row("P3", "status = 'pending' AND judge2_verdict = 'reject' "
                                     "AND judge_note LIKE '%[split]'"),
                      link_row("P4", "status = 'pending' AND judge2_verdict IS NULL")),
             w.review()]),
        "link-two-vote-same-model": w.judge_scenario(
            link_config("auto", "auto"),
            link_stub({"P1": V("accept", 0.75)}, {"P1": V("accept", 0.85)},
                      second_model="judge-one"),
            [w.expect(link_insert("P1", until=two_vote_done),
                      link_row("P1", "status = 'pending' AND judge2_verdict = 'accept' "
                                     "AND judge_note LIKE "
                                     "'%| two-vote apply needs a distinct second model'")),
             w.review()]),
        # The rejected row's source gains an edge: its evidence moves, the
        # automatic reject reopens (the pair reopened, the record retired)
        # and the judge rejects it again.
        "link-auto-reopen": w.judge_scenario(
            link_config("auto", "off"),
            link_stub({"P2": V("reject", 0.9)}),
            [w.expect(link_insert("P2"), link_row("P2", "status = 'rejected'"),
                      link_pair("P2")),
             w.expect({**w.judge_evidence_change("gamma-node"),
                       "settle": {**w.JUDGE_SETTLE,
                                  "until": "SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                                           "'automatic_review_decision_v1:%' AND "
                                           "value->>'queue' = 'link' AND "
                                           "value->>'state' = 'reopened')"}},
                      link_row("P2", "status = 'rejected'"), link_pair("P2"),
                      link_decision("P2", pair=True)),
             w.review()]),
        "junk-shadow": w.judge_scenario(
            config("shadow"),
            stub({"Alpha Node": V("delete", 0.95), "Beta Store": V("keep", 0.9)}),
            [w.expect(junk_insert("alpha-node", "beta-store"),
                      junk_row("alpha-node", "status = 'pending' AND judge_verdict = 'delete'"),
                      junk_row("beta-store", "status = 'pending' AND judge_verdict = 'keep'")),
             w.review()]),
        "junk-auto": {**w.judge_scenario(
            config("auto", extra="    snapshot_keep: 1\n"),
            stub(auto_verdicts),
            [w.expect(junk_insert("alpha-node", "beta-store", "gamma-tool", "hub",
                                  "llama-4b", "delta-unit", "delta-node"),
                      gone("alpha-node"),
                      junk_row("beta-store", "status = 'rejected' AND decided_by = 'dream-judge'"),
                      keep_pair("beta-store"),
                      junk_row("gamma-tool", "status = 'pending' AND judge_verdict = 'delete'"),
                      junk_row("hub", "status = 'pending' AND judge_verdict = 'delete'"),
                      junk_row("delta-node", "status = 'pending' AND judge_verdict = 'delete'"),
                      junk_row("delta-unit", "status = 'pending' AND judge_verdict = 'leave' "
                                             "AND judge_confidence = 0"),
                      "SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                      "'automatic_review_decision_v1:%' AND value->>'queue' = 'junk' "
                      "AND value->>'action' = 'keep')"),
             w.snapshot_file(), w.review()]),
            # Delta Node carries two current facts: its delete fails the bar.
            "seed": [*w.JUDGE_SEED, w.fact("Delta Node", "port", "7001"),
                     w.fact("Delta Node", "owner", "ops")]},
        # The kept entity gains an edge: its evidence moves, the automatic
        # keep reopens (the keep pair removed, the record retired) and the
        # judge asks again, keeping it again.
        "junk-auto-reopen": w.judge_scenario(
            config("auto"),
            stub({"Beta Store": V("keep", 0.9)}),
            [w.expect(junk_insert("beta-store"),
                      junk_row("beta-store", "status = 'rejected'"), keep_pair("beta-store")),
             w.expect({**w.judge_evidence_change("beta-store"),
                       "settle": {**w.JUDGE_SETTLE,
                                  "until": "SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                                           "'automatic_review_decision_v1:%' AND "
                                           "value->>'state' = 'reopened')"}},
                      junk_row("beta-store", "status = 'rejected'")),
             w.review()]),
    }
