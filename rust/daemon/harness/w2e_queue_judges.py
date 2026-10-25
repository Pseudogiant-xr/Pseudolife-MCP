"""The review-queue judges' differential scenarios (task P3b), merged into
``w2e.py``'s ``SCENARIOS`` by ``scenarios(w2e_module)``.

Each judge runs from the sweep, as the merge judge does, so these follow the
``judge-merge-*`` pattern: the merge judge's seed (twelve entities on a
hub, notes mentioning them), one shared stub answering by display, the
pending proposals inserted out of band on both banks at once, then the
judges' settle, and the banks, answers and request sequences compared. The
judge under test is on; every other judge and sweep step is off.

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

# The deep_dream_judge_junk mutants (src/dream/queue_judges.rs), caught by
# the scenario beside each.
MUTANTS = ["mcp-dream-junk-no-evidence-bar",   # junk-auto
           "mcp-dream-junk-keep-unrecorded"]    # junk-auto


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

    V = w.V
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
