"""The dream sweep's differential scenarios (task P4), merged into
``w2e.py``'s ``SCENARIOS`` by ``scenarios(w2e_module)``.

``run_sweep_once`` runs on the daemon's own timer, not on a call: after its
reapers, with dreaming enabled, the analyzer filing tick, the deep tick,
the merge judge, ``dream_status``, the stall tick and, when the trigger
would fire, the automatic dream. Each scenario switches on the step it is
written for and leaves the others off, writes the inputs through the
Python daemon's own write path, then waits for both sides' sweeps to
settle (``settle_sides``: an ``until`` query holds, then the judge tables
and the stub's requests stay unchanged) and compares the whole banks, the
answers after it and the model requests. The two daemons' timers are
independent, so every scenario is built so that the settled state does not
depend on how many ticks ran: the automatic dream's trigger fires only once
the whole batch is stored, the deep tick fires once (its watermark stops
it), the analyzer's batch covers every entity (its cursor stays put), and
the stall's failures run out when the failing memory is quarantined.
"""
from __future__ import annotations

import json

SWEEP_BASE = (
    "memory:\n"
    "  lessons:\n    enabled: false\n    infer_outcomes: false\n"
    "  graph_insight:\n    enabled: false\n"
)
JUDGES_OFF = (
    "    judge_mode: \"off\"\n    link_judge_mode: \"off\"\n    junk_judge_mode: \"off\"\n"
    "    curation_judge_mode: \"off\"\n    candidate_judge_mode: \"off\"\n"
)


def config(dream: str = "", deep: str = "", extra: str = "", interval: float = 0.25) -> str:
    """Dreaming on with a fast sweep; the deep tick and the analyzer off
    unless ``deep`` turns them on; every judge off."""
    return (SWEEP_BASE
            + f"  dream:\n    enabled: true\n    sweep_interval_seconds: {interval}\n"
            + "    stall_notice: false\n" + dream
            + "  deep_dream:\n    auto_tick: false\n    analyzer_file_duplicates: false\n"
            + JUDGES_OFF + deep + extra)


SETTLE = {"quiet_s": 3.0, "timeout_s": 120.0}
# The seeding daemon dreams nothing: its sweep must not write the markers
# (the deep watermark, the analyzer cursor) the measured arms are compared
# on, nor leave a snapshot file the template does not carry.
SEED_CONFIG = SWEEP_BASE + "  dream:\n    enabled: false\n    stall_notice: false\n"

AUTO_NOTES = ["The staging ingress moved to nginx 1.27.",
              "Gwen owns the release calendar.",
              "The staging ingress listens on port 8443."]
AUTO_CLAIMS = [
    {"entity": "Staging Ingress", "attribute": "proxy", "value": "nginx 1.27", "source": 1},
    {"entity": "Gwen", "attribute": "owns", "value": "release calendar", "source": 2},
    {"entity": "Staging Ingress", "attribute": "port", "value": "8443", "source": 3},
]
STALL_NOTES = ["The audit exporter writes to bucket audit-7."]

ANALYZER_SEED_SQL = (
    # A decided analyzer link proposal the reconcile pass closes into
    # dismissed_pairs (behind its cursor), and one still pending it leaves.
    "INSERT INTO edge_proposals (src_id, relation, dst_id, confidence, similarity, rationale, "
    "source, status, created_at, decided_at) SELECT a.id, 'related-to', b.id, 0.5, NULL, "
    "'analyzer file/concept pair', 'analyzer', 'rejected', 10, 20 FROM entities a, entities b "
    "WHERE a.display = 'Ledger Export' AND b.display = 'Ledger Exporter'; "
    "INSERT INTO edge_proposals (src_id, relation, dst_id, confidence, similarity, rationale, "
    "source, status, created_at) SELECT a.id, 'related-to', b.id, 0.5, NULL, "
    "'analyzer file/concept pair', 'analyzer', 'pending', 30 FROM entities a, entities b "
    "WHERE a.display = 'Ledger Export' AND b.display = 'Billing Gateway'; "
    # A rejected analyzer merge, the reconcile pass's entity half.
    "INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, status, "
    "created_at, decided_at) SELECT 'merge', a.id, b.id, 0.7, "
    "'analyzer-duplicate: jaccard 0.7', 'rejected', 40, 50 FROM entities a, entities b "
    "WHERE a.display = 'Ledger Exporter' AND b.display = 'Ledger Export'"
)

# The sweep's mutants (src/dream/ticks.rs, deep.rs), each caught by the
# scenario named beside it. The overdue rule and the notices are checked
# against Python's tracker in src/dream/stall_tests.rs.
MUTANTS = ["mcp-sweep-no-auto-dream",         # sweep-dream-auto
           "mcp-sweep-no-deep-tick",          # sweep-deep-tick
           "mcp-sweep-analyzer-no-cursor",    # sweep-analyzer-tick
           "compaction-off"]                  # sweep-compaction

# Compaction on a one-second sweep with dreams off (the retrieval log, on by
# default, keeps the sweep running in both daemons): one slot keeps its
# newest superseded version, the two older ones go.
COMPACTION_CONFIG = ("memory:\n  dream:\n    enabled: false\n    sweep_interval_seconds: 1\n"
                     "  compaction:\n    enabled: true\n    keep_per_slot: 1\n"
                     "    min_age_days: 30\n")
COMPACTION_SLOT = "entity_norm = 'compaction-host' AND attribute_norm = 'os'"


def scenarios(w) -> dict:
    """The scenarios, built with ``w2e``'s helpers (``w`` is the module)."""
    auto_stub = {"model": "stub-model",
                 "replies": w.batch_replies(AUTO_NOTES, AUTO_CLAIMS)}
    fail = {"kind": "claims", "user": w.numbered(STALL_NOTES), "status": 503,
            "body": json.dumps({"error": "upstream failure"})}
    return {
        # Compaction (memory/compaction.py): four versions of one slot, the
        # three superseded ones backdated 100 days after the seed; with
        # keep_per_slot 1 the sweep purges the two oldest from the resident
        # store and, through the per-slot sync, from the bank. The current
        # value and the newest superseded one stay.
        "sweep-compaction": {
            "config": COMPACTION_CONFIG,
            "seed": [w.fact("Compaction Host", "OS", "Debian 9"),
                     w.fact("Compaction Host", "OS", "Debian 10"),
                     w.fact("Compaction Host", "OS", "Debian 11"),
                     w.fact("Compaction Host", "OS", "Debian 12"),
                     w.sql("UPDATE facts SET superseded_at = superseded_at - 100 * 86400 "
                           f"WHERE {COMPACTION_SLOT} AND status = 'superseded'",
                           after_stop=True)],
            "ops": [
                # Both daemons settled before the first op, so the 1 s sweep
                # has ticked several times by now. No ``until``: a mutant
                # that never compacts must answer a diff (the ``expect``),
                # not time the settle out.
                {"op": "sleep", "s": 6,
                 "expect": [f"SELECT count(*) = 2 FROM facts WHERE {COMPACTION_SLOT}",
                            f"SELECT count(*) = 1 FROM facts WHERE {COMPACTION_SLOT} "
                            "AND status = 'current'"]},
                w.fact_get("Compaction Host", "OS"),
                w.history("Compaction Host", "OS"),
            ],
        },
        # The automatic dream: the trigger fires once the third note makes
        # the batch, the sweep dreams it, the cursor and the run row land.
        "sweep-dream-auto": {
            "config": config(dream="    min_batch: 3\n    idle_seconds: 1000000000\n"),
            "seed_config": SEED_CONFIG,
            "extractor": auto_stub,
            "seed": [w.fact("Staging Ingress", "proxy", "nginx 1.25")],
            "ops": [
                w.store(AUTO_NOTES[0], source="conversation"),
                w.store(AUTO_NOTES[1], source="conversation"),
                # No ``until``: a mutant that never dreams must answer a
                # diff (the ``expect``), not time the settle out.
                {**w.store(AUTO_NOTES[2], source="conversation"),
                 "settle": dict(SETTLE),
                 "expect": ["SELECT EXISTS (SELECT 1 FROM dream_runs WHERE status = 'committed')",
                            "SELECT EXISTS (SELECT 1 FROM meta "
                            "WHERE key = 'cortex_dream_cursor')"]},
                w.dream("status"),
                w.dream("runs"),
                w.fact_get("Staging Ingress", "proxy"),
            ],
        },
        # The deep tick: a never-deep-dreamed graph is recommended, the
        # sweep applies the deep dream once (snapshot first), and its
        # watermark keeps it from firing again.
        "sweep-deep-tick": {
            "config": config(dream="    min_batch: 1000000\n    idle_seconds: 1000000000\n",
                             deep="    auto_tick: true\n"),
            "seed_config": SEED_CONFIG,
            # The tick fires at the daemons' first sweep, before any op.
            "init_settle": {**SETTLE, "expect": [
                "SELECT EXISTS (SELECT 1 FROM meta WHERE key = 'deep_last_apply')"]},
            "seed": [w.relate("Cache Node", "uses", "Redis"),
                     w.relate("Cache-Node", "uses", "Redis"),
                     w.relate("TODO", "related-to", "Redis"),
                     w.fact("Redis", "port", "6379")],
            "ops": [
                {"op": "snapshot_file"},
                w.dream("status"),
                w.review("list"),
            ],
        },
        # The analyzer tick: one batch covers every entity, so it files the
        # near-duplicate pair and leaves its cursor on the last entity; the
        # reconcile pass closes the decided analyzer proposal.
        "sweep-analyzer-tick": {
            "config": config(dream="    min_batch: 1000000\n    idle_seconds: 1000000000\n",
                             deep="    analyzer_file_duplicates: true\n    judge_batch: 50\n"),
            "seed_config": SEED_CONFIG,
            # The reconcile pass and the filing run at the first sweep.
            "init_settle": {**SETTLE,
                            "until": "SELECT EXISTS (SELECT 1 FROM meta "
                                     "WHERE key = 'analyzer_pair_reconcile_cursor')",
                            "expect": ["SELECT EXISTS (SELECT 1 FROM meta "
                                       "WHERE key = 'analyzer_duplicate_cursor')"]},
            "seed": [w.fact("Billing Gateway", "region", "eu-west"),
                     w.fact("Billing Gateway Service", "owner", "Hana"),
                     w.relate("Ledger Export", "uses", "Billing Gateway"),
                     w.relate("Ledger Exporter", "uses", "Billing Gateway"),
                     w.sql(ANALYZER_SEED_SQL, after_stop=True)],
            "ops": [
                w.dream("status"),
                w.review("list"),
            ],
        },
        # A stall the sweep opens: the extractor fails every call, the
        # second failed automatic dream opens the stall, the hold and the
        # per-entry isolation run out and the memory is quarantined. A
        # sweep interval of one second keeps the overdue rule (three
        # intervals of a due backlog) from opening it first.
        "sweep-stall": {
            "config": config(dream="    min_batch: 1\n    idle_seconds: 1000000000\n",
                             interval=1.0),
            "seed_config": SEED_CONFIG,
            # Every model call fails: the batch, the per-entry retry and any
            # other prompt the pass sends (kind-only entries answer them).
            "extractor": {"model": "stub-model", "replies": [
                fail, *({"kind": k, "status": 503,
                         "body": json.dumps({"error": "upstream failure"})}
                        for k in ("claims", "events", "relations"))]},
            "seed": [w.fact("Audit Exporter", "bucket", "audit-6")],
            "ops": [
                {**w.store(STALL_NOTES[0], source="conversation"),
                 # An outage writes no run row; the quarantined memory is
                 # acknowledged (the cursor moves past it).
                 "settle": {**SETTLE, "until": "SELECT EXISTS (SELECT 1 FROM entries "
                                               "WHERE dream_state = 'acknowledged')"}},
                w.dream("status"),
                w.dream("runs"),
            ],
        },
    }
