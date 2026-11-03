"""``memory_dream(action="rollback")``'s differential scenarios (task
R8 rollback, spec DR1), merged into ``w2e.py``'s ``SCENARIOS`` by
``scenarios(w2e_module)``.

A rollback replays the latest committed run's journal (``dream_run_slots``)
in reverse through the normal write paths, so each scenario runs a dream
pass against the stub extractor and then unwinds it, comparing the bank
(facts, their supersession history and tiers, chronicle events, the run
row) and each answer's per-row outcomes:

* ``dream-rollback``: the ``dream-run`` batch (superseded, confirmed with a
  raised tier, contested, inserted, member added and removed, events), then
  the refusals around it (``no_committed_run`` before the run, a stale
  ``run_id``, ``no_committed_run`` again once the run is rolled back);
* ``dream-rollback-gates``: the consolidation quarantine's promotion onto an
  empty slot, reversed like an insert, and its parked contenders;
* ``dream-rollback-convert``: a member add that converted a scalar slot to a
  set (the sole survivor unwinds the conversion), a supersede that settled
  a contender an earlier pass parked (the previous value is written back,
  its contest resolved, and the contender re-parked), and a removed member
  re-added with its tiers;
* ``dream-rollback-later``: direct writes between the pass and the rollback:
  the previous value reasserted by the user (the carried and direct tier
  recovery), a superseded slot converted to a set (the rewrite refused,
  ``partial:error:ValueError``, and the replay continuing), a confirmed
  value changed (``skipped:superseded_by_later``), a confirmed member
  (``member_confirmed``) and a converted set gaining another member
  (``partial:set_retained``);
* ``dream-rollback-gates-prev``: the quarantine's promotion over a previous
  value, reversed by rewriting it;
* ``dream-rollback-refused``: seeded run rows, a newer ``failed`` and
  ``running`` run refusing the rollback, and a stale ``run_id``;
* ``dream-rollback-persistence``: one-shot save and chronicle-delete failures report
  ``partial:error:PersistenceError`` and the SQLSTATE-specific
  ``partial:error:DivisionByZero`` while later reversals and the final
  save continue, retaining the rolled-back audit row.

Normalizers: ``dream_runs.rolled_back_at`` is the rollback's clock
(null-or-not), as every other write clock.
"""
from __future__ import annotations

# (table, column) wall clocks the rollback writes (w2e.CLOCK).
CLOCK = {("dream_runs", "rolled_back_at")}
# Deliberate breaks (``src/dream/rollback.rs``): the settled contender is
# not re-parked (dream-rollback-convert), a raised tier is kept
# (dream-rollback), the run's chronicle events are kept (dream-rollback).
MUTANTS = ["mcp-dream-rollback-contender-lost", "mcp-dream-rollback-tiers-kept",
           "mcp-dream-rollback-event-kept"]

# The first pass parks an assistant-stated 6380 against the agent fact
# 6379; the second (the one rolled back) writes 6380 at the user tier,
# superseding 6379 and settling that contender.
PARK_NOTES = [("Maybe the Cache Node port is 6380.", "conversation")]
PARK_CLAIMS = [{"entity": "Cache Node", "attribute": "port", "value": "6380",
                "speaker": "assistant", "source": 1}]
CONVERT_NOTES = [
    ("Build Host OS now also includes Ubuntu 24.04.", "conversation"),
    ("The Cache Node port is 6380 now.", "conversation"),
    ("Hank left the Team members list.", "conversation"),
]
CONVERT_TEXTS = [t for t, _ in CONVERT_NOTES]
CONVERT_CLAIMS = [
    # a member add at a scalar slot: the one-way scalar-to-set conversion
    {"entity": "Build Host", "attribute": "OS", "value": "Ubuntu 24.04", "op": "add",
     "source": 1},
    # a user-tier supersede of an agent fact whose value a parked
    # contender holds: the write settles the contender
    {"entity": "Cache Node", "attribute": "port", "value": "6380", "confidence": 0.9,
     "source": 2},
    # a member removal, re-added with its tiers on rollback
    {"entity": "Team", "attribute": "members", "value": "Hank", "op": "remove",
     "source": 3},
]


# The refused rewrite (Queue) is journaled first, so it is the replay's last
# write: a write right after it in the same millisecond would take HLC
# logical 1 on whichever side happened to be faster (Python's embeddings of
# these texts are cached), and hlc_logical compares exactly.
LATER_NOTES = [
    ("The Queue backend is rabbitmq now.", "conversation"),
    ("The Proxy port is 9090 now.", "conversation"),
    ("Gina is on the Team members list.", "conversation"),
    ("Bob still drinks green tea every morning.", "conversation"),
    ("Host OS now also includes Ubuntu 24.04.", "conversation"),
    ("The Runner OS includes Fedora 40.", "conversation"),
]
LATER_TEXTS = [t for t, _ in LATER_NOTES]
LATER_CLAIMS = [
    {"entity": "Queue", "attribute": "backend", "value": "rabbitmq", "confidence": 0.9,
     "source": 1},
    {"entity": "Proxy", "attribute": "port", "value": "9090", "confidence": 0.9, "source": 2},
    {"entity": "Team", "attribute": "members", "value": "Gina", "op": "add", "source": 3},
    {"entity": "Bob", "attribute": "drink", "value": "green tea", "confidence": 0.8,
     "source": 4},
    {"entity": "Host", "attribute": "OS", "value": "Ubuntu 24.04", "op": "add", "source": 5},
    # a member add equal to the agent-tier scalar: it confirms that scalar
    {"entity": "Runner", "attribute": "OS", "value": "Fedora 40", "op": "add", "source": 6},
]


def scenarios(w) -> dict:
    """The scenarios, built with ``w2e``'s own op helpers (``w`` is the
    ``w2e`` module, passed in to avoid a circular import)."""
    run = w.SCENARIOS["dream-run"]
    # dream-run's in-process writes and notes, up to its first pass
    first_pass = run["ops"][:run["ops"].index(w.dream("run")) + 1]
    gates = w.SCENARIOS["dream-run-gates"]
    convert_extractor = {"model": "stub-model", "replies": [
        *w.batch_replies([t for t, _ in PARK_NOTES], PARK_CLAIMS),
        *w.batch_replies(CONVERT_TEXTS, CONVERT_CLAIMS)]}
    return {
        "dream-rollback": {
            "config": run["config"],
            "extractor": run["extractor"],
            "seed": run["seed"],
            "ops": [
                w.dream("rollback"),
                *first_pass,
                w.dream("rollback", run_id=999),
                # beyond i64: still a run_id, so still stale
                w.dream("rollback", run_id=2**63),
                w.dream("rollback"),
                w.dream("runs"),
                w.fact_get("Deploy Target", "region"),
                w.fact_get("Bob", "drink"),
                w.fact_get("Alice", "role"),
                w.fact_get("Staging DB", "version"),
                w.fact_get("Release Runbook", "requirements"),
                w.dream("rollback"),
            ],
        },
        "dream-rollback-gates": {
            "config": gates["config"],
            "extractor": gates["extractor"],
            "seed": gates["seed"],
            "ops": [
                *gates["ops"][:-3],
                w.dream("rollback"),
                w.dream("runs"),
                w.fact_get("Cache", "eviction"),
                w.fact_get("Release Train", "day"),
                w.fact_get("Release Train", "owner"),
            ],
        },
        "dream-rollback-persistence": {
            "config": run["config"],
            "extractor": run["extractor"],
            "seed": run["seed"],
            "ops": [
                *first_pass,
                # Sequences are not rolled back: only the first facts INSERT
                # fails, so later reversals and the final save can succeed.
                # An independent delete failure exercises the raw DB class.
                w.external("CREATE SEQUENCE rollback_fail_once; "
                      "CREATE FUNCTION rollback_fail_once() RETURNS trigger LANGUAGE plpgsql AS $$ "
                      "BEGIN IF NOT (SELECT is_called FROM rollback_fail_once) THEN "
                      "PERFORM nextval('rollback_fail_once'); "
                      "RAISE EXCEPTION 'fixture cortex save failure'; END IF; RETURN NEW; END $$; "
                      "CREATE TRIGGER rollback_fail_once BEFORE INSERT ON facts "
                      "FOR EACH ROW EXECUTE FUNCTION rollback_fail_once(); "
                      "CREATE SEQUENCE rollback_event_fail_once; "
                      "CREATE FUNCTION rollback_event_fail_once() RETURNS trigger LANGUAGE plpgsql AS $$ "
                      "BEGIN IF NOT (SELECT is_called FROM rollback_event_fail_once) THEN "
                      "PERFORM nextval('rollback_event_fail_once'); "
                      "RAISE EXCEPTION 'fixture chronicle delete failure' USING ERRCODE = '22012'; "
                      "END IF; RETURN OLD; END $$; "
                      "CREATE TRIGGER rollback_event_fail_once BEFORE DELETE ON chronicle_events "
                      "FOR EACH ROW EXECUTE FUNCTION rollback_event_fail_once()"),
                w.dream("rollback"),
                w.dream("runs"),
                w.fact_get("Deploy Target", "region"),
                w.fact_get("Release Runbook", "requirements"),
            ],
        },
        "dream-rollback-convert": {
            "config": w.DREAM_RUN_QUIET,
            "extractor": convert_extractor,
            "seed": [
                w.fact("Build Host", "OS", "Debian 12"),
                w.fact("Cache Node", "port", "6379"),
                w.set_add("Team", "members", "Gina"),
                w.set_add("Team", "members", "Hank"),
            ],
            "ops": [
                *[w.store(t, source=src) for t, src in PARK_NOTES],
                w.dream("run"),
                w.fact_get("Cache Node", "port"),
                *[w.store(t, source=src) for t, src in CONVERT_NOTES],
                w.dream("run"),
                w.fact_get("Build Host", "OS"),
                w.fact_get("Cache Node", "port"),
                w.fact_get("Team", "members"),
                w.dream("rollback"),
                w.fact_get("Build Host", "OS"),
                w.fact_get("Cache Node", "port"),
                w.fact_get("Team", "members"),
                w.dream("runs"),
            ],
        },
        "dream-rollback-later": {
            "config": w.DREAM_RUN_QUIET,
            "extractor": {"model": "stub-model",
                          "replies": w.batch_replies(LATER_TEXTS, LATER_CLAIMS)},
            "seed": [
                w.fact("Proxy", "port", "8080"),
                w.fact("Queue", "backend", "redis"),
                # a tool-added member has no origin: an unknown prior tier
                w.set_add("Team", "members", "Gina"),
                w.fact("Runner", "OS", "Fedora 40"),
                w.fact("Bob", "drink", "green tea"),
                w.fact("Host", "OS", "Debian 12"),
            ],
            "ops": [
                *[w.store(t, source=src) for t, src in LATER_NOTES],
                w.dream("run"),
                w.fact("Proxy", "port", "8080", origin="user"),
                w.set_add("Queue", "backend", "kafka"),
                w.fact("Bob", "drink", "coffee", origin="user"),
                w.set_add("Host", "OS", "Alpine"),
                w.dream("rollback"),
                w.fact_get("Proxy", "port"),
                w.fact_get("Queue", "backend"),
                w.fact_get("Team", "members"),
                w.fact_get("Bob", "drink"),
                w.fact_get("Host", "OS"),
                w.fact_get("Runner", "OS"),
            ],
        },
        "dream-rollback-gates-prev": {
            "config": gates["config"],
            "extractor": gates["extractor"],
            "seed": [*gates["seed"], w.fact("Cache", "eviction", "FIFO")],
            "ops": [
                *gates["ops"][:-3],
                w.dream("rollback"),
                w.fact_get("Cache", "eviction"),
            ],
        },
        "dream-rollback-refused": {
            "config": w.DREAM_RUN_QUIET,
            "extractor": {"model": "stub-model", "replies": []},
            "seed": [
                w.fact("Refused Host", "OS", "Debian 12"),
                w.sql("INSERT INTO dream_runs (id, started_at, finished_at, cursor_before, "
                      "status) VALUES (5, 1, 2, 0, 'committed'), (6, 3, NULL, 0, 'failed'), "
                      "(7, 4, NULL, 0, 'running')", after_stop=True),
            ],
            "ops": [
                w.dream("rollback"),
                w.dream("rollback", run_id=6),
                w.dream("rollback", run_id=5),
                w.dream("runs"),
            ],
        },
    }
