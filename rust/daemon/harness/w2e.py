"""Write-path differential harness (slice W2-E): the Python daemon (oracle)
against the Rust daemon, write by write.

usage:
  python w2e.py live    --rust-bin PATH [--only NAME ...] [--out FILE] [--record]
  python w2e.py golden  --rust-bin PATH [--only NAME ...] [--out FILE]
  python w2e.py calibrate --rust-bin PATH [--out FILE]   (no tolerances: records them)
  python w2e.py mutants --rust-bin PATH_BUILT_WITH_FEATURES_HARNESS_MUTANTS [--only NAME ...]
                        [--mutants NAME ...]   (default: every entry of MUTANTS)

Any Rust build serves ``memory_store``, ``memory_fact_set``,
``memory_set_add``, ``memory_set_remove``, ``memory_fact_resolve``,
``memory_episode_start``, ``memory_episode_end``, ``memory_session_title``,
``memory_episode_summary``, ``memory_outcome``, ``memory_reinforce``,
``memory_history``, ``memory_world_set``, ``memory_world_search``,
``memory_lesson_search``, ``memory_supersede``, ``memory_reinstate``,
``memory_forget``, ``memory_consolidation_candidates`` and ``memory_consolidate`` on ``/mcp``
(``--features mutants`` adds the
deliberate breaks). Each scenario prepares
one template bank through the Python daemon's own write path (seed calls
only, never hand-written rows, except the declared ``sql`` seed op below),
clones it for each side, starts both daemons, then sends every operation to
both. After every operation it compares:

* the response by value: each daemon's MCP ``tools/call`` result
  (``structuredContent``, ``isError``), so argument binding and the
  envelope are compared with the body; ``surprise`` within the measured
  tolerance, everything else exact;
* the full database state of both banks (``dbstate.dump``), after the
  declared normalizers below, which replace values by rule, never by hand.

Normalizers (declared, spec "Free for W1 and W2"):

* wall-clock columns and meta fields keep only null-or-not;
* episode ids (``uuid4().hex``) are renamed ``<ep#i>`` in ``started_at``
  order wherever they appear (any table, any JSON value or key);
* generic session titles keep their shape, not their minute;
* ``entries.embedding`` and ``entries.surprise`` are compared numerically
  per entry id against tolerances set from a measurement
  (``goldens/w2e-tolerance.json``), then replaced by a token; so is
  ``facts.embedding`` and ``world_facts.embedding`` per row id, against the
  same embedding tolerances (both sides rewrite a slot's rows in the same
  order, so the ids match);
* the world cortex clocks: ``world_facts`` ``asserted_at``,
  ``last_confirmed``, ``superseded_at``, ``tx_time``, ``valid_time`` and
  ``hlc_phys`` keep only null-or-not; ``retrieved_at`` (column and answer)
  does only when it lies within a day of now (a write without one takes the
  clock), and an explicit past value compares exactly;
* a candidate cluster's ``seed_score`` compares like a search ``score`` and
  its ``cohesion`` within one rounding unit (1e-4), only inside a cluster
  (an object with ``members``);
* a search ``score`` in a response, and each ``score`` in a
  ``lesson_search_events.served`` list, compares within 1.5e-4: the two
  embedders agree to the measured tolerance, and scores are rounded to 4
  places;
* the canonical-fact clocks: ``facts`` ``asserted_at``, ``last_confirmed``,
  ``superseded_at``, ``tx_time``, ``valid_time`` and ``hlc_phys`` (the HLC's
  wall-clock milliseconds; ``hlc_logical`` stays exact),
  ``retrieval_uses.created_at``, ``entities.created_at`` and each
  ``cortex_supersession_log`` row's ``timestamp`` keep only null-or-not;
* the correction clocks: ``memory_trace_invalidations.invalidated_at``
  (the correction's own clock, like ``entries.superseded_at``) and
  ``entry_reinstatement_decisions.decided_at`` keep only null-or-not; a
  correction's ``new_memory_surprise`` compares like a store's ``surprise``;
* the forget clocks: ``lessons.superseded_at``, ``edges.superseded_at`` and
  ``store_decisions.decided_at`` keep only null-or-not, and so does the
  ``superseded_at`` inside a ``store_decisions.record`` snapshot;
* the lesson write clocks (the dream's synthesis, a snapshot restore):
  ``lessons`` ``asserted_at``, ``last_confirmed``, ``tx_time`` and
  ``valid_time``, a lineage audit's ``asserted_at`` and
  ``outcome_signals.created_at`` keep their equality and order when written
  from the clock: each value within a day of now becomes ``<clock#N>``, its
  rank among the distinct such values in that side's dump, so a lesson's
  valid_time must name its oldest source signal's created_at, a confirm
  must keep asserted_at and move last_confirmed, and an audit must name its
  lesson's asserted_at; an explicit seeded stamp compares exactly (an older
  ``created_at`` keeps only null-or-not); ``lessons.hlc_phys``
  (milliseconds) keeps only null-or-not when written from the clock;
  ``outcome_signals.consumed_at`` keeps only null-or-not;
  ``lessons.embedding`` compares like ``facts.embedding``; an
  ``episode:<id>`` provenance token is renamed like an episode id, and a
  sorted token list is sorted again on the renamed tokens;
* the board columns a session title writes: ``coordination_agents.name_set_at``
  and ``coordination_events.created_at`` keep only null-or-not, and so do the
  audit chain's ``hash`` and ``prev_hash`` (they cover ``created_at``; the
  board harness proves the chain byte for byte);
* in responses, the same record clocks and an episode's ``started_at`` and
  ``ended_at`` keep only null-or-not, a generic session title keeps its
  shape, and a record's ``age`` may differ by one step ("just now" /
  "1 minute ago"), since the two daemons answer seconds apart;
* the dream's display clocks: in a ``memory_dream`` answer, a
  ``timestamp``, ``cursor`` or ``dream_cursor`` equal to an entry's ``ts``
  on the same side becomes ``<ts:ENTRY_ID>`` (and so does a
  ``cortex_dream_cursor`` meta value), so an entry stored seconds apart on
  each side compares by identity; a ``commit_token`` compares as its
  decoded payload (backend, generation, entry ids, timestamp token) without
  its signature, which covers the wall-clock timestamp; ``idle_seconds``
  must be >= 0 on both sides and within the longest step's wall time (plus
  ``IDLE_SLACK_S``) of each other (``idle_diffs``), then compares as
  null-or-not; a run's ``finished_at`` may not precede its ``started_at``,
  in the bank or an answer (``run_clock_diffs``); a stall record's ``since``,
  ``last_success_at`` and ``recovered_at``, the extractor record's ``at``
  and a run row's ``finished_at`` and ``rolled_back_at``; a run row's
  ``cursor_before`` and ``cursor_after`` map to ``<ts:ENTRY_ID>`` like a
  ``cursor``;
* the dream pass's clocks: ``dream_runs`` ``started_at`` and
  ``finished_at``, ``dream_run_slots.at``, ``chronicle_events.recorded_at``,
  ``memory_traces.created_at``, ``entity_sources.updated_at`` and
  ``entity_proposals.created_at`` keep only null-or-not; a ``dream_runs``
  ``cursor_before`` / ``cursor_after`` equal to an entry's ``ts`` becomes
  ``<ts:ENTRY_ID>``, as the cortex dream cursor does;
* the merge judge's clocks: ``entity_proposals`` ``judged_at``,
  ``judged2_at`` and ``judged3_at``, and in an automatic review decision
  record (``automatic_review_decision_v1:<n>``) its ``recorded_at``,
  ``ended_at``, ``decided_at`` and ``dismissed_at`` and its proposal
  snapshot's verdict clocks keep only null-or-not (in memory_graph_review's
  ``automatic_decisions`` listing, a decision's ``recorded_at`` likewise
  when within a day of now); every fingerprint and policy compares
  exactly;
* rows are compared as sorted sets (row order is the primary key's order,
  which renamed ids change), except that entry ids themselves must match.

Arguments may name rows by content, resolved on each side's own bank
just before the call: ``@id:<text>`` (the first row with that text, or for
a deleted row the entry of its first reinstatement decision),
``@idn:<n>:<text>`` (the n-th, from 0), and for the first RETIRED row with
that text ``@sup:<text>`` (its ``superseded_at``) and ``@sha:<field>:<text>``
(the SHA-256 of its ``text``, ``source`` or ``by``, ``superseded_by_text``).

Seed operations may also be ``{"op": "sql"}``: a statement run on the
template only, for state no tool writes (``entity_kinds``, which only an
out-of-process backfill fills). With ``"after_stop": true`` it runs once
the seeding daemon has stopped, for ``facts`` rows: the Python daemon's
exit flush rewrites ``facts`` from its resident cortex and would drop rows
it never loaded (the set-cap scenario's hundred members, with NULL
embeddings so that no cosine decides the cap).

An operation may be ``external(statement)``: a write by another process,
run on both banks between two calls (its result is not compared, only what
the next call makes of it), for a row changed behind the daemons' backs.
Only the scenarios in ``EXTERNAL_WRITES`` may use it.

An operation may carry ``"settle": {"quiet_s", "timeout_s", "until"}``:
after it runs on both sides, the comparison waits until each side's
background work has settled (``settle_sides``): the optional ``until``
query answers true, then neither the judge tables nor that side's stub
requests change for ``quiet_s`` seconds. The merge judge runs from the
sweep, not from a call, so its scenarios (``judge-merge-*``) insert the
pending proposals out of band and compare the banks and the request
sequences once both judges are idle.

An operation may carry ``"expect"``: queries whose first column must
answer true on both banks once it (and its settle) is done, so a scenario
whose two sides both miss the path it is written for still fails
(``expect_diffs``). ``{"op": "restart", "config": ...}`` stops both daemons,
writes the new config on each side and starts them again on fresh ports
(``restart_sides``), for a knob read only at start.

A scenario with ``"shared_stub": true`` points both daemons at one stub:
the merge judge signs each verdict with a fingerprint over its endpoint's
URL, which must be the same on both sides for the banks to compare
exactly. Each side then sends its own bearer key (``side-python``,
``side-rust``) to every endpoint, and the stub's records are read per key.
``@STUB@`` in its config is that stub's origin.

Two operations act on each side's data directory instead of its daemon
(``HOME_OPS``): ``snapshot_file`` answers that side's newest deep-dream
graph snapshot (``graph_snapshots/graph-*.json``: the name's shape, the file
count, every table's columns and rows with CLOCK columns null-or-not), and
``block_snapshots`` puts a plain file where that directory goes so the next
snapshot write fails. In a ``memory_dream`` answer the snapshot name
compares by shape, ``would_orphan[].age_days`` numerically within 0.1 day
(``orphan_age_diffs``), and ``last_apply_at`` (like the meta
``deep_last_apply.ts``) by presence.

A scenario may carry ``"extractor": {...}``, the spec of a stub
OpenAI-style chat-completions endpoint (``stub_extractor.py``: health,
models and chat routes, replies looked up by the SHA-256 of the exact
request body or by prompt kind and user message), plus optional
``"extractor_fallback": true``. Each side's daemon (and the seeding daemon)
gets its own stub through ``PSEUDOLIFE_DREAM_BASE_URL`` and
``PSEUDOLIFE_DREAM_MODEL`` (and the ``_FALLBACK_`` pair). After every
operation the harness drains both stubs, reports any request no table entry
answered, and compares the two request sequences element by element on
method, path, body bytes and the digest of the ``Authorization`` header;
with ``"extractor_keys": {"primary": ..., "fallback": ...}`` (passed as
``PSEUDOLIFE_DREAM_API_KEY`` and ``PSEUDOLIFE_DREAM_FALLBACK_API_KEY``)
each side's chat requests must also carry their own endpoint's key and its
GETs none (``extractor_key_diffs``); each side's stub origin in its
responses becomes ``http://stub``. The claim: given identical model replies, both
daemons send byte-identical requests and write identical banks; it claims
nothing about model behaviour.

Exit status: 0 only when no response and no bank state differs.
Environment: ``ORT_DYLIB_PATH`` and ``PSEUDOLIFE_DAEMON_ONNX_DIR`` for the
Rust daemon; the test login (``pgdisposable``) for the banks.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import re
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daemons  # noqa: E402
import dbstate  # noqa: E402
import pgdisposable as pg  # noqa: E402
import run as w1a  # noqa: E402
from stub_extractor import StubExtractor  # noqa: E402

pg.DISPOSABLE_NAME = re.compile(r"pl_cf_w2e_[a-z0-9_]{1,40}")
GOLDENS = HERE / "goldens" / "w2e"
TOLERANCE_FILE = HERE / "goldens" / "w2e-tolerance.json"
TOKEN = "tok-default-w2e-0001"
AUTH = ("Authorization", f"Bearer {TOKEN}")

# (table, column) holding wall-clock seconds (value or list of values).
CLOCK = {("entries", "ts"), ("entries", "superseded_at"), ("episodes", "started_at"),
         ("episodes", "ended_at"), ("client_sessions", "started_at"),
         ("client_sessions", "ended_at"), ("client_sessions", "start_times"),
         ("facts", "asserted_at"), ("facts", "last_confirmed"), ("facts", "superseded_at"),
         ("facts", "tx_time"), ("facts", "valid_time"), ("facts", "hlc_phys"),
         ("outcome_signals", "created_at"), ("outcome_signals", "consumed_at"),
         ("entities", "created_at"),
         ("coordination_agents", "name_set_at"), ("coordination_events", "created_at"),
         ("coordination_events", "hash"), ("coordination_events", "prev_hash"),
         ("retrieval_uses", "created_at"),
         ("world_facts", "asserted_at"), ("world_facts", "last_confirmed"),
         ("world_facts", "superseded_at"), ("world_facts", "tx_time"),
         ("world_facts", "valid_time"), ("world_facts", "hlc_phys"),
         ("lesson_search_events", "created_at"),
         ("memory_trace_invalidations", "invalidated_at"),
         ("entry_reinstatement_decisions", "decided_at"),
         ("lessons", "superseded_at"), ("edges", "superseded_at"),
         ("store_decisions", "decided_at"), ("slot_reads", "last_read_at"),
         ("edges", "asserted_at"), ("relations", "created_at"),
         ("retrieval_events", "created_at"), ("edge_proposals", "created_at"),
         ("edge_proposals", "decided_at"), ("entity_proposals", "decided_at"),
         ("merge_decisions", "decided_at"), ("dismissed_pairs", "dismissed_at"),
         # the dream pass (memory_dream run)
         ("dream_runs", "started_at"), ("dream_runs", "finished_at"),
         ("dream_run_slots", "at"), ("chronicle_events", "recorded_at"),
         ("memory_traces", "created_at"), ("entity_sources", "updated_at"),
         ("entity_proposals", "created_at"),
         # the merge judge's verdict clocks
         ("entity_proposals", "judged_at"), ("entity_proposals", "judged2_at"),
         ("entity_proposals", "judged3_at")}
# dream_runs cursor columns: an entry's ts becomes its <ts:ID> token.
DREAM_RUN_CURSORS = ("cursor_before", "cursor_after")
# memory_dream answer keys holding wall-clock seconds (the stall record and
# the last extractor record).
DREAM_CLOCK_KEYS = ("since", "last_success_at", "recovered_at", "at", "finished_at",
                    "rolled_back_at", "last_apply_at")
# A deep apply's snapshot file name (its UTC second differs by side).
SNAPSHOT_NAME = re.compile(r"graph-\d{8}-\d{6}\.json")
# memory_dream answer keys holding a dream cursor (an entry's ts).
DREAM_TS_KEYS = ("timestamp", "cursor", "dream_cursor", "cursor_before", "cursor_after")
# Columns that are wall-clock only when written from the clock: a value
# within RECENT_S of now keeps null-or-not, anything else compares exactly.
RECENT_CLOCK = {("world_facts", "retrieved_at")}
# Columns whose clock-written values keep their equality and order: a value
# within RECENT_S of now becomes ``<clock#N>``, N its rank among the distinct
# such values in one dump (per side), so the two sides' wall clocks may
# differ while which values are equal, and which is older, must agree. A
# lesson the dream or a snapshot restore writes takes the clock (seeded
# lessons carry explicit 2023 stamps, compared exactly): its valid_time is
# its oldest source signal's created_at, a confirm moves last_confirmed and
# tx_time but not asserted_at, and its lineage audit names its asserted_at
# (``store_decisions.record``). An older value in a CLOCK column keeps only
# null-or-not; in any other, it compares exactly.
ORDERED_CLOCK = {("lessons", "asserted_at"), ("lessons", "last_confirmed"),
                 ("lessons", "tx_time"), ("lessons", "valid_time"),
                 ("outcome_signals", "created_at")}
# The same in wall-clock milliseconds (the HLC's physical part).
RECENT_CLOCK_MS = {("lessons", "hlc_phys")}
RECENT_S = 86400.0
# Record keys in tool answers holding wall-clock seconds.
RESPONSE_CLOCK = {"asserted_at", "last_confirmed", "superseded_at", "tx_time", "valid_time",
                  "started_at", "ended_at"}
RESPONSE_RECENT_CLOCK = {"retrieved_at", "decided_at"}
# memory_graph_review's ``automatic_decisions`` listing: each decision's
# ``recorded_at`` is the merge judge's clock (its ``ended_at`` is already a
# RESPONSE_CLOCK key).
DECISION_LISTING_RECENT_CLOCK = {"recorded_at"}
# A response score's allowance: embeddings within the measured tolerance
# move a 4-place cosine by at most one rounding unit.
SCORE_TOLERANCE = 1.5e-4
# A cluster's cohesion: cosines over vectors both banks hold identically,
# rounded to 4 places, so at most one rounding unit apart.
COHESION_TOLERANCE = 1e-4
# meta key -> paths (keys) inside its JSON value that are wall-clock.
META_CLOCK = {"capacity_true_drops": {"last_at"}, "active_session_pointer": {"ts"},
              # automatic review decisions a human verdict retires
              "automatic_review_decision_v1:d1": {"ended_at"},
              "automatic_review_decision_v1:d2": {"ended_at"},
              "automatic_review_decision_v1:d3": {"ended_at"},
              # the deep dream's apply watermark (memory_dream deep)
              "deep_last_apply": {"ts"}}
# An automatic review decision record (``automatic_review_decision_v1:<n>``,
# written by the merge judge): its clocks, and the verdict clocks of the
# proposal snapshot it carries.
DECISION_RECORD_PREFIX = "automatic_review_decision_v1:"
DECISION_RECORD_CLOCK = {"recorded_at", "ended_at", "decided_at", "dismissed_at"}
DECISION_PROPOSAL_CLOCK = {"judged_at", "judged2_at", "judged3_at"}
EP_ID = re.compile(r"^[0-9a-f]{32}$")
GENERIC = re.compile(r"^session - \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
# store-cancellable is w2e_shutdown.py's control: over MCP every call runs
# detached from its connection, so only the shutdown drain can observe it.
MUTANTS = ["store-tags-raw", "store-skip-session-episode", "store-no-hint", "store-surprise-raw",
           "mcp-store-no-session",
           # memory_fact_set (spec FS1-FS12): caught by the fact-set-* scenarios.
           "mcp-fact-set-no-supersede", "mcp-fact-set-no-confirm-reinforce",
           "mcp-fact-set-no-correction-signal", "mcp-fact-set-no-entity-mint",
           # memory_set_add / memory_set_remove (spec SA1-SA11): caught by
           # the set-* scenarios.
           "mcp-set-add-no-convert", "mcp-set-add-no-cosine-dedup", "mcp-set-remove-noop",
           # memory_fact_resolve (spec FR1-FR8): caught by the fact-resolve-*
           # scenarios.
           "mcp-fact-resolve-no-user-tier", "mcp-fact-resolve-reject-noop",
           "mcp-fact-resolve-no-set-guard",
           # The episode tools (spec EP1-EP12): caught by the episode-* and
           # session-title scenarios.
           "mcp-episode-start-no-anchor", "mcp-episode-end-pops-root",
           "mcp-session-title-no-entry-stamp", "mcp-session-title-no-board",
           "mcp-episode-summary-count-order",
           # memory_outcome and memory_reinforce (spec OC1-OC12): caught by
           # the outcome-labels scenario.
           "mcp-outcome-no-used-ids-record", "mcp-outcome-credit-latest-only",
           "mcp-outcome-ignore-handle", "mcp-reinforce-no-explicit",
           "mcp-reinforce-no-use-credit",
           # memory_history slot mode (spec HI1-HI7): caught by history-slot.
           "mcp-history-ignore-as-of", "mcp-history-set-unsorted",
           "mcp-history-set-no-removed", "mcp-history-asserted-order",
           # the world cortex (spec WD1-WD12): caught by the world-* scenarios.
           "mcp-world-set-no-confirm", "mcp-world-set-unsafe-allowed",
           "mcp-world-set-no-save", "mcp-world-search-no-correct-with",
           # lessons and the history chain (spec LS1-LS10, CH1-CH6): caught
           # by the lessons scenario.
           "mcp-lesson-search-no-graph-resolve", "mcp-lesson-search-no-log",
           "mcp-history-chain-no-lessons",
           # memory_supersede and memory_reinstate (spec CO1-CO10, RI1-RI9):
           # caught by the supersede-* and reinstate scenarios.
           "mcp-supersede-text-first-live", "mcp-supersede-derived-current-only",
           "mcp-supersede-surprise-gate", "mcp-supersede-no-trace-invalidation",
           "mcp-supersede-resident-stale", "mcp-reinstate-default-principal",
           "mcp-reinstate-ignore-invalidations", "mcp-reinstate-resident-stale",
           "mcp-reinstate-no-mirror",
           # memory_forget (spec FG1-FG16): caught by the forget-* scenarios.
           "mcp-forget-no-bulk-guard", "mcp-forget-delete-digests", "mcp-forget-cortex-stale",
           "mcp-forget-no-lesson-cascade", "mcp-forget-lessons-stale", "mcp-forget-facts-kept",
           "mcp-forget-edges-kept", "mcp-forget-slot-reads-kept", "mcp-forget-no-world-audit",
           "mcp-forget-no-lesson-audit",
           # memory_consolidation_candidates (spec CC1-CC9): caught by
           # consolidation-candidates.
           "mcp-consolidation-unsorted", "mcp-consolidation-unverified",
           # memory_consolidate (spec CN1-CN7): caught by the consolidate-*
           # scenarios.
           "mcp-consolidate-no-dedupe", "mcp-consolidate-first-error-only",
           "mcp-consolidate-labels-first-only", "mcp-consolidate-victim-first-only",
           "mcp-consolidate-derived-keys", "mcp-consolidate-default-source",
           # the graph store's write tools (spec GW1-GW12): caught by the
           # graph-writes and graph-relations scenarios.
           "mcp-graph-no-etype-backfill", "mcp-graph-no-slot-fold", "mcp-graph-no-warnings",
           "mcp-graph-unrelate-missing-dst", "mcp-graph-alias-canonical",
           "mcp-graph-redefine-builtin", "mcp-graph-inverse-unchecked",
           # memory_graph (spec GR1-GR10): caught by graph-read.
           "mcp-graph-no-relation-filter", "mcp-graph-tag-no-ambiguous",
           "mcp-graph-facts-no-alias", "mcp-graph-no-labels",
           # memory_recall (spec RC1-RC16): caught by the recall-* scenarios.
           "mcp-recall-seed-any", "mcp-recall-no-hub-gate", "mcp-recall-no-requery",
           "mcp-recall-no-pin", "mcp-recall-no-annotate", "mcp-recall-flat-caps",
           # memory_graph_review's list (spec RV2-RV5): caught by the
           # review-list scenarios.
           "mcp-review-dup-threshold", "mcp-review-no-merge-veto", "mcp-review-no-fold-swap",
           "mcp-review-terminal-as-pending",
           # memory_graph_review's decision actions (spec RV6-RV14): caught
           # by review-actions.
           "mcp-review-accept-no-fold", "mcp-review-accept-no-floor",
           "mcp-review-dismiss-aliases", "mcp-review-reject-no-dismiss",
           "mcp-review-propose-keep-violations", "mcp-review-pair-index-noop",
           "mcp-review-analyzer-no-dismiss", "mcp-review-relate-state-miss",
           # memory_graph_review's slot actions: caught by review-slots.
           "mcp-review-slot-meta-noop", "mcp-review-slot-restore-oldest",
           "mcp-review-slot-restore-over-live", "mcp-review-slot-no-decision",
           # memory_dream status/pull/commit and the stub extractor: caught by
           # dream-basics.
           "mcp-dream-store-not-pending", "mcp-dream-commit-resident-stale",
           "mcp-dream-pull-newest-first", "mcp-dream-status-no-probe",
           # memory_dream run, the consolidation pass: caught by dream-run.
           "mcp-dream-run-literal-gate-off", "mcp-dream-run-citation-unchecked",
           "mcp-dream-run-no-journal", "mcp-dream-run-trace-contested",
           "mcp-dream-run-gate-order", "mcp-dream-run-stamp-before-commit",
           # the P1 review fixes: Unicode digits (dream-run-literal), lone
           # surrogates (dream-run-surrogate), the reason phrase
           # (dream-run-redirect) and the fallback key (dream-run-fallback).
           "mcp-dream-run-encode-unchecked", "mcp-dream-extractor-ascii-digits",
           "mcp-dream-extractor-strict-surrogates", "mcp-dream-extractor-canonical-reason",
           "mcp-dream-extractor-shared-key",
           # the dream's stages (P1b): outcome inference (dream-stages-infer),
           # digests (dream-stages-digests), rules (dream-stages-rules),
           # lesson synthesis (dream-stages-lessons) and the lesson
           # audit-snapshot restore (review-slots).
           "mcp-dream-stage-infer-retry-forever", "mcp-dream-stage-digest-echo-unchecked",
           "mcp-dream-stage-rule-no-retry", "mcp-dream-stage-lessons-no-lineage",
           "mcp-dream-stage-lessons-ack-unlanded", "mcp-dream-stage-snapshot-restore-skipped",
           # a compound about's lone surrogate fails the slot sync
           # (dream-stages-lessons).
           "mcp-dream-stage-lessons-about-unchecked",
           # memory_dream deep (spec DD1-DD16): caught by the dream-deep-*
           # scenarios.
           "mcp-dream-deep-no-rescore", "mcp-dream-deep-supersede-unsafe",
           "mcp-dream-deep-tombstone-any-degree", "mcp-dream-deep-dropped-merge-filed",
           "mcp-dream-deep-no-rollup", "mcp-dream-deep-no-stamp", "mcp-dream-deep-unbounded",
           "mcp-dream-deep-kept-refiled",
           # junk-owned and lesson-only ids reach the link candidates
           # (dream-deep-exclusion).
           "mcp-dream-deep-no-exclusion",
           # the merge judge (spec JM1-JM24): each caught by the judge-merge-*
           # scenario named beside it.
           "mcp-dream-judge-confidence-bar",       # judge-merge-auto-reject
           "mcp-dream-judge-one-vote",             # judge-merge-auto-reject
           "mcp-dream-judge-same-model-reject",    # judge-merge-same-model
           "mcp-dream-judge-no-served-check",      # judge-merge-unknown-served
           "mcp-dream-judge-tiebreak-minority",    # judge-merge-tiebreak
           "mcp-dream-judge-third-same-model",     # judge-merge-tiebreak-skip
           "mcp-dream-judge-evidence-no-reopen",   # judge-merge-reconsider
           "mcp-dream-judge-no-stale-clear",       # judge-merge-shadow
           "mcp-dream-judge-third-row-copy",       # judge-merge-duplicates
           "mcp-dream-judge-alias-unprobed",       # judge-merge-alias
           "mcp-dream-judge-moved-applies",        # judge-merge-moved
           "mcp-dream-judge-moved-not-cleared",    # judge-merge-moved
           "mcp-dream-judge-replay-no-refusal-filter",  # judge-merge-replay
           "mcp-dream-judge-encode-unchecked",     # judge-merge-unencodable
           "mcp-dream-judge-real-unchecked"]       # judge-merge-unencodable


# ── MCP client for the Python oracle ──────────────────────────────────────

class Mcp:
    """Minimal streamable-HTTP MCP client: initialize once, then tools/call."""

    def __init__(self, port: int):
        self.port, self.session, self.ids = port, None, 0

    def _post(self, payload: dict, extra=()) -> dict | None:
        headers = [AUTH, ("Content-Type", "application/json"),
                   ("Accept", "application/json, text/event-stream"), *extra]
        if self.session:
            headers.append(("mcp-session-id", self.session))
        body = json.dumps(payload).encode()
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
        c.request("POST", "/mcp", body=body, headers=dict(headers))
        r = c.getresponse()
        raw = r.read()
        sid = r.getheader("mcp-session-id")
        if sid:
            self.session = sid
        ctype = r.getheader("content-type") or ""
        c.close()
        if r.status >= 300:
            raise RuntimeError(f"/mcp answered {r.status}: {raw[:300]!r}")
        if not raw.strip():
            return None
        if ctype.startswith("text/event-stream"):
            msgs = [json.loads(line[5:]) for line in raw.decode().splitlines()
                    if line.startswith("data:") and line[5:].strip()]
            return next((m for m in reversed(msgs) if "id" in m), None)
        return json.loads(raw)

    def init(self) -> None:
        self.ids += 1
        self._post({"jsonrpc": "2.0", "id": self.ids, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "w2e-harness", "version": "0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def abandon(self, name: str, args: dict, headers=()) -> None:
        self.ids += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.ids, "method": "tools/call",
                           "params": {"name": name, "arguments": args}}).encode()
        abandon(self.port, "/mcp", [AUTH, ("Content-Type", "application/json"),
                                    ("Accept", "application/json, text/event-stream"),
                                    ("mcp-session-id", self.session), *headers], body)

    def call(self, name: str, args: dict, headers=()) -> dict:
        self.ids += 1
        msg = self._post({"jsonrpc": "2.0", "id": self.ids, "method": "tools/call",
                          "params": {"name": name, "arguments": args}}, headers)
        res = msg["result"]
        value = res.get("structuredContent")
        if value is None:
            value = json.loads(res["content"][0]["text"])
        # FastMCP wraps a non-object return under "result".
        return {"isError": bool(res.get("isError")), "value": value}


CRLF = chr(13) + chr(10)


def abandon(port: int, path: str, headers: list, body: bytes, after_s: float = 0.15) -> None:
    """Send a request and close the socket before the answer: a client that
    disconnects mid-call (a killed session, a timeout)."""
    import socket
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    head = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Content-Length: {len(body)}",
            *[f"{k}: {v}" for k, v in headers], "", ""]
    s.sendall(CRLF.join(head).encode("latin-1") + body)
    time.sleep(after_s)
    s.close()


# ── scenarios ─────────────────────────────────────────────────────────────

def store(text, headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_store", "args": {"text": text, **kw},
            "headers": list(headers)}


def sess(s: str) -> tuple[str, str]:
    return ("X-PL-Session", s)


def fact(entity, attribute, value, headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_fact_set",
            "args": {"entity": entity, "attribute": attribute, "value": value, **kw},
            "headers": list(headers)}


def tool(name, headers=(), **args) -> dict:
    return {"op": "tool", "name": name, "args": args, "headers": list(headers)}


def set_add(entity, attribute, member, headers=()) -> dict:
    return tool("memory_set_add", headers, entity=entity, attribute=attribute, member=member)


def set_remove(entity, attribute, member, headers=()) -> dict:
    return tool("memory_set_remove", headers, entity=entity, attribute=attribute, member=member)


def resolve(entity, attribute, accept) -> dict:
    return tool("memory_fact_resolve", entity=entity, attribute=attribute, accept=accept)


def ep_start(title, headers=(), **kw) -> dict:
    return tool("memory_episode_start", headers, title=title, **kw)


def ep_end(headers=(), **kw) -> dict:
    return tool("memory_episode_end", headers, **kw)


def session_title(title, headers=(), **kw) -> dict:
    return tool("memory_session_title", headers, title=title, **kw)


def ep_summary(eid) -> dict:
    return tool("memory_episode_summary", id=eid)


def outcome(task, result, headers=(), **kw) -> dict:
    return tool("memory_outcome", headers, task=task, outcome=result, **kw)


def reinforce(entry_id, headers=()) -> dict:
    return tool("memory_reinforce", headers, entry_id=entry_id)


def search(query, headers=(), top_k=1) -> dict:
    """Seed-only here: a logged search whose served ids an outcome credits."""
    return tool("memory_search", headers, query=query, top_k=top_k)


def world_set(entity, attribute, value, headers=(), **kw) -> dict:
    return tool("memory_world_set", headers, entity=entity, attribute=attribute, value=value, **kw)


def world_search(query, **kw) -> dict:
    return tool("memory_world_search", query=query, **kw)


def lesson_search(query, headers=(), **kw) -> dict:
    return tool("memory_lesson_search", headers, query=query, **kw)


def history_chain(entity) -> dict:
    return tool("memory_history", entity=entity)


def lesson_row(task, aspect, task_norm, aspect_norm, lesson, about, polarity, outcome,
               status, entry_text) -> dict:
    """Seed-only: a ``lessons`` row (only the dream writes lessons), its
    embedding copied from an entry the Python daemon stored, so both banks
    hold the same vector. After the seeding daemon stops: its exit flush
    rewrites the lessons it holds."""
    def q(v):
        return "NULL" if v is None else "'" + v.replace("'", "''") + "'"
    return sql(
        "INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, value, about, "
        "polarity, outcome, status, confidence, origin, support, provenance, asserted_at, "
        "last_confirmed, embedding) SELECT "
        f"{q(task)}, {q(aspect)}, {q(task_norm)}, {q(aspect_norm)}, {q(lesson)}, {q(about)}, "
        f"{q(polarity)}, {q(outcome)}, {q(status)}, 0.8, 'agent', '[]', "
        "'[\"signal:2\", \"signal:1\"]', 1700000000, 1700000000, embedding "
        f"FROM entries WHERE text = {q(entry_text)}", after_stop=True)


def history(entity, attribute, **kw) -> dict:
    return tool("memory_history", entity=entity, attribute=attribute, **kw)


def register(episode: str, **fields) -> dict:
    """Seed-only: a board row registered under ``episode`` (a session key)."""
    return {"op": "http", "method": "POST", "path": "/api/coordination/register",
            "body": {"label": "claude-code", "project": "p", "task": "t", "episode": episode,
                     **fields}}


def sql(statement: str, after_stop: bool = False) -> dict:
    """Seed-only: a statement run on the template (see the module docstring)."""
    return {"op": "sql", "sql": statement, "after_stop": after_stop}


SLOT_TEXTS = [
    "I have a Ragdoll cat named Jacque",
    "I gave him away last spring",
    "the daemon port is 8765",
    "host: 10.0.0.5",
    "Our build server hostname is ci-box-2.",
    "My editor is vim; my shell is zsh.",
]

LABEL_TEXTS = [
    "Never run the full suite natively on Windows.",
    "You must rebase before opening the PR.",
    "According to the docs, the bank volume is external.",
    "Rule: no PII in commits.",
    "I always prefer tabs over spaces.",
    "The daemon must be restarted after a schema bump.",
]

SCENARIOS: dict[str, dict] = {
    "store-resume-invalid": {
        "config": None,
        "env": {"PSEUDOLIFE_SESSION_RESUME_SECONDS": "not-a-number"},
        "seed_env": {"PSEUDOLIFE_SESSION_RESUME_SECONDS": "21600"},
        "seed": [
            store("A closed root survives invalid duration settings.", [sess("invalid")]),
            {"op": "http", "method": "POST", "path": "/api/hook/session-end",
             "body": {"session_id": "invalid"}},
        ],
        "ops": [store("Invalid resume must refuse before mutation.", [sess("invalid")])],
    },
    "store-resume-disabled": {
        "config": None,
        "env": {"PSEUDOLIFE_SESSION_RESUME_SECONDS": "0_0"},
        "seed": [
            store("A durable note in the session that will close.", [sess("disabled")]),
            {"op": "http", "method": "POST", "path": "/api/hook/session-end",
             "body": {"session_id": "disabled"}},
        ],
        "ops": [store("Disabled resume opens a fresh root.", [sess("disabled")])],
    },
    "store-reaper": {
        "config": "memory:\n  meta_filter:\n    enabled: true\n  dream:\n    enabled: false\n  retrieval_log:\n    enabled: false\n",
        "env": {"PSEUDOLIFE_SESSION_IDLE_SECONDS": "2",
                "PSEUDOLIFE_SESSION_REAP_SECONDS": "0.1",
                "PSEUDOLIFE_SESSION_RESUME_SECONDS": "3600"},
        "seed": [],
        "ops": [
            # Episode creation precedes admission: even a filtered store's
            # empty root must be visible to the background reaper.
            store("I don't have any memories about cats saved.", [sess("reaped")]),
            {"op": "sleep", "s": 4},
        ],
    },
    # Shipped defaults: gate inert, meta filter off, auto-promote off.
    "store-defaults": {
        "config": None,
        "seed": [],
        "ops": [
            store("The bench Postgres listens on port 5433 for disposable test banks."),
            store("   "),
            store(""),
            store("  Leading and trailing spaces are stripped.  "),
            store("Tags are normalized", tags=[" Decision ", "decision", "", "BLOCKER"]),
            store("Status note from a peer session", source="status"),
            store("A user said this", source="user", origin="user"),
            store("The bench Postgres listens on port 5433 for disposable test banks."),
            *[store(t) for t in LABEL_TEXTS],
            # Explicit labels only in the MCP vocabulary: anything else is a
            # binding refusal (W2-G); the body's own ValueError text is
            # proven by the label goldens.
            store("Explicit labels win", authority="directive", distortion_tolerance="belief"),
            store("Observation label", authority="observation"),
            store("Quoted label on a rule", authority="quoted", distortion_tolerance="episodic"),
            store("", authority="observation"),
            *[store(t) for t in SLOT_TEXTS],
            store("First note in session A", [sess("sess-a")]),
            store("Second note in session A", [sess("sess-a")]),
            store("A note in session B", [sess("sess-b")], tags=["b"]),
            store("Handle attribution to A", episode="@root:sess-a:12"),
            store("Handle plus a different header", [sess("sess-b")], episode="@root:sess-a:12"),
            store("Unknown handle", episode="ffffffffffff"),
            store("Short handle", episode="abc"),
            store("Empty handle", episode=""),
            store("Empty header falls through", [sess("")]),
            # The client hangs up mid-call; Python finishes the store on its
            # worker thread regardless.
            # The 2026-10-10 Linux run missed the cancellation mutant with a
            # short input; fill the 512-token embedding cap so the socket can
            # close while the tool body is still working.
            {**store("Stored although the caller disconnected. " * 160,
                     [sess("sess-a")]), "abandon": True},
            store("The next store after a disconnect", [sess("sess-a")]),
        ],
    },
    # The gate on: meta filter, surprise threshold, contradiction admission.
    "store-gate": {
        "config": "memory:\n  surprise_threshold: 0.35\n  meta_filter:\n    enabled: true\n",
        "seed": [],
        "ops": [
            store("I don't have any memories about cats saved."),
            store("I don't have any memories about cats saved.", source="user"),
            store("The release pipeline publishes to PyPI with trusted publishing."),
            store("The release pipeline publishes to PyPI with trusted publishing."),
            store("Release pipeline publishes to PyPI using trusted publishing."),
            store("My dog's name is Rex."),
            store("My dog's name is Max."),
            store("The staging server is running."),
            store("The staging server is not running."),
            store("I have a cat named Jacque."),
            store("I no longer have Jacque."),
            store("the api port is 8080"),
            store("the api port is 9090"),
            store("Completely unrelated: the tea kettle whistles at boiling."),
            # Near-duplicates with no contradiction anchor (no capitals,
            # numbers, negation or possession cues): a plain gate drop.
            store("the tea kettle whistles loudly when the water boils"),
            store("the tea kettle whistles when the water boils"),
            store("the kettle whistles loudly when water boils"),
        ],
    },
    # Capacity: a one-band custom preset of five entries forces true drops.
    "store-capacity": {
        "config": "memory:\n  miras:\n    preset: custom\n    bands:\n      - name: flat\n        max_entries: 5\n",
        "seed": [],
        "ops": [
            store("System prompt text that should be retained.", source="system"),
            store("A tool call record, cheap to lose.", source="tool_call"),
            store("Thinking trace, the cheapest to lose.", source="llm_thinking"),
            store("User statement about the weather in Hobart.", source="user"),
            store("Agent note about the deploy checklist."),
            store("Agent note about rotating the bearer tokens."),
            store("Another thinking trace about indexes.", source="llm_thinking"),
            store("A second user statement about lunch.", source="user"),
        ],
    },
    # A bank seeded by Python: a registered session, a closed root, an open
    # root, the active-session pointer, and a tombstone.
    "store-seeded-episodes": {
        "config": None,
        "seed_env": {"PSEUDOLIFE_SESSION_REAP_SECONDS": "1", "PSEUDOLIFE_SESSION_IDLE_SECONDS": "1",
                     "PSEUDOLIFE_SESSION_RESUME_SECONDS": "2"},
        "seed": [
            {"op": "http", "method": "GET", "path": "/api/hook/session-start?session_id=sess-empty"},
            {"op": "sleep", "s": 8},
            store("Closed session note", [sess("sess-closed")]),
            {"op": "http", "method": "POST", "path": "/api/hook/session-end",
             "body": {"session_id": "sess-closed"}},
            store("Open session note", [sess("sess-open")]),
            {"op": "http", "method": "GET", "path": "/api/hook/session-start?session_id=sess-ptr"},
        ],
        "ops": [
            store("No header: the active-session pointer attributes it"),
            store("Back in the open session", [sess("sess-open")]),
            store("Resume the closed root by its key", [sess("sess-closed")]),
            store("Recreate the swept root by its handle", episode="@tomb:12"),
            store("A brand new session", [sess("sess-new")]),
        ],
    },
}


# memory_fact_set (spec FS1-FS12). Every branch of cortex_write the MCP tool
# reaches, against a template seeded through the Python daemon's own tools.
SCENARIOS.update({
    "fact-set-branches": {
        "config": None,
        "seed": [
            # A session root for the episode handle and the correction signal.
            store("Session note that opens the fs-a root.", [sess("fs-a")]),
            fact("Payments DB", "host", "10.0.0.5", origin="user", confidence=0.9),
            fact("dev box", "os", "Debian 12"),
            fact("pipeline", "schedule", "the deploy pipeline runs nightly on the bench box",
                 origin="action"),
            # The value names an entity nobody has minted yet: its
            # object_entity_id stays NULL until "Billing API" is minted.
            fact("dev box", "runs", "Billing API"),
        ],
        "ops": [
            # inserted; the subject's entity is minted
            fact("Build Server", "hostname", "ci-box-2"),
            # confirmed: case and separators fold to the slot, the agent tier reinforces
            fact("build_server", "Hostname", "ci-box-2"),
            # confirmed: a user restatement adds the user tier
            fact("dev box", "os", " debian 12 ", origin="user", confidence=0.5),
            # compression echo: confirms the richer value and logs it
            fact("pipeline", "schedule", "deploy pipeline runs nightly"),
            # superseded by a user write: correction signal under the fs-a leaf
            fact("Payments DB", "host", "10.0.0.9", [sess("fs-a")], origin="user",
                 confidence=0.9),
            # tier_downgrade: an agent write parks a contender
            fact("Payments DB", "host", "10.0.0.7"),
            # the same value refreshes the contender (action tier added)
            fact("payments db", "host", "10.0.0.7 ", origin="action", confidence=0.6),
            # below_confidence_margin: a new value replaces the contender
            fact("Payments DB", "host", "10.0.0.8", origin="user", confidence=0.5),
            # the contender's value at full weight supersedes and settles the contest;
            # no session: the signal has no episode
            fact("Payments DB", "host", "10.0.0.8", origin="user", confidence=0.9),
            # minting "billing-api" relinks the seeded row's object_entity_id
            fact("Billing API", "owner", "payments team"),
            # a junk-shaped subject keeps the fact but mints no entity
            fact("236 memories", "status", "archived"),
            fact("LOCAL master = 8e2b992", "state", "pushed"),
            # episode handles: valid (session from the root), unknown (warning), empty
            fact("Dev Box", "kernel", "6.8", episode="@root:fs-a:12"),
            fact("Dev Box", "shell", "zsh", episode="ffffffffffff"),
            fact("Dev Box", "editor", "vim", episode=""),
            fact("Dev Box", "pager", "less", episode="abc"),
            # explicit freshness and labels, then a confirm that restates one label
            fact("dev box", "tz", "UTC", freshness_class="volatile", authority="directive",
                 distortion_tolerance="constraint"),
            fact("dev box", "tz", "UTC", authority="quoted"),
            fact("dev box", "locale", "en_AU", freshness_class="slow",
                 authority="observation", distortion_tolerance="preference"),
            fact("dev box", "timezone", "Australia/Hobart", freshness_class="evergreen",
                 distortion_tolerance="episodic"),
            # auto labels inferred from the value
            fact("dev box", "rule", "Never run the full suite natively on Windows."),
            fact("dev box", "doc", "According to the docs, the bank volume is external."),
            # a header session wins over the handle's; the signal takes the handle's episode
            fact("Dev Box", "editor", "emacs", [sess("fs-b")], origin="user",
                 episode="@root:fs-a:12"),
            # an integer confidence binds as a float; agent under user parks
            fact("Dev Box", "editor", "vim", confidence=1),
            fact("dev box", "editor", "nano", origin="action", confidence=0),
        ],
    },
    "fact-set-newer-wins": {
        "config": "memory:\n  cortex:\n    protect_provenance: false\n",
        "seed": [fact("db", "host", "a", origin="user", confidence=0.9)],
        "ops": [
            # tier ignored: an agent write supersedes (no correction signal)
            fact("db", "host", "b"),
            # below the margin: contested, the current answered twice, nothing parked
            fact("db", "host", "c", confidence=0.1),
            fact("db", "host", "c", origin="user", confidence=0.1),
            fact("db", "host", "d", origin="user", confidence=0.9),
        ],
    },
    "fact-set-tuned": {
        "config": ("memory:\n  lessons:\n    enabled: false\n  cortex:\n"
                   "    supersede_confidence_margin: 0.5\n    reinforce_rate: 0.5\n"),
        "seed": [fact("db", "host", "a", origin="user", confidence=0.9)],
        "ops": [
            # within the wider margin: superseded; lessons off, so no signal row
            fact("db", "host", "b", origin="user", confidence=0.45),
            # reinforce_rate 0.5
            fact("db", "host", "b", origin="user", confidence=0.1),
            fact("db", "host", "c", origin="user", confidence=0.1),
        ],
    },
    "fact-set-freshness-auto": {
        "config": None,
        "seed": [
            sql("INSERT INTO entity_kinds (entity_norm, kind, origin, confidence, decided_at) "
                "VALUES ('payments-db', 'system', 'manual', 1.0, 0), "
                "('release-0-9', 'artifact', 'manual', 1.0, 0)"),
        ],
        "ops": [
            fact("Payments DB", "status", "up"),
            fact("payments_db", "deployment-date", "2026-10-01"),
            fact("Payments DB", "schema version", "58"),
            fact("Payments DB", "owner", "payments team"),
            fact("release 0.9", "status", "shipped"),
            fact("unknown thing", "status", "fine"),
            fact("Payments DB", "status", "degraded", freshness_class="evergreen"),
        ],
    },
    "fact-set-set-slot": {
        "config": None,
        "seed": [
            tool("memory_set_add", entity="dev box", attribute="users", member="alice"),
            tool("memory_set_add", entity="dev box", attribute="groups", member="admins"),
            tool("memory_set_remove", entity="dev box", attribute="groups", member="admins"),
        ],
        "ops": [
            # a slot with current members refuses (invalid_argument, nothing written)
            fact("Dev Box", "Users", "bob"),
            # only a removed member: the scalar lands beside the audit row
            fact("dev box", "groups", "ops"),
        ],
    },
})


# memory_set_add / memory_set_remove (spec SA1-SA11). Every branch the MCP
# tools reach. A member's embedding is that of "{entity} {attribute}
# {member}"; the cosines quoted below were measured with the daemon's
# Qwen3-Embedding-0.6B (fp32 torch, CPU), all far from the 0.9 dedup line.
SET_CAP_MEMBERS = (
    "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, value, status, "
    "confidence, asserted_at, last_confirmed, kind, value_norm) "
    "SELECT 'cap box', 'tags', 'cap-box', 'tags', v, 'current', 0.7, 1000.0 + g, 1000.0 + g, "
    "'member', v FROM (SELECT g, 'tag-' || lpad(g::text, 3, '0') AS v "
    "FROM generate_series(0, 99) AS g) AS s ORDER BY g")

SCENARIOS.update({
    "set-add-branches": {
        "config": None,
        "seed": [
            # a labelled volatile user scalar, and an agent contender beside it
            fact("Home Lab", "services", "Jellyfin media server", origin="user", confidence=0.9,
                 freshness_class="volatile", authority="directive",
                 distortion_tolerance="constraint"),
            fact("Home Lab", "services", "Pi-hole DNS ad blocker"),
        ],
        "ops": [
            # converts the scalar (dropped class and labels on the log row), adds
            # the member (cosine 0.71 to the old value) and settles the contender
            set_add("home lab", "Services", "Pi-hole DNS ad blocker", [sess("sa-a")]),
            # the old scalar's value is a member now: confirmed by value
            set_add("Home Lab", "services", "  jellyfin MEDIA server "),
            # confirmed by cosine (0.99 to the converted member)
            set_add("Home Lab", "services", "Jellyfin media streaming server"),
            # added (cosine 0.81 and 0.67 to the two members)
            set_add("Home Lab", "services", "Nextcloud file sync"),
            # blank members are refused with a note; the subject is minted all the same
            set_add("Home Lab", "services", "   "),
            set_add("Bike Rack", "bikes", ""),
            # a real subject is minted, a junk-shaped one is not
            set_add("Garden Shed", "tools", "cordless hedge trimmer"),
            set_add("236 memories", "tags", "archived snapshot"),
            # removed by normalised value; then not found, and an empty slot
            set_remove("HOME LAB", "services", "nextcloud FILE sync "),
            set_remove("Home Lab", "services", "Nextcloud file sync"),
            set_remove("nobody", "things", "anything"),
            # re-added after its removal: a new row beside the removed one
            set_add("Home Lab", "services", "Nextcloud file sync"),
        ],
    },
    "set-add-aggregate": {
        "config": None,
        "seed": [
            fact("Aquarium", "fish count", "32", origin="user"),
            fact("Aquarium", "budget", "$1,500"),
        ],
        "ops": [
            # the same total confirms the scalar
            set_add("aquarium", "Fish Count", " 32 "),
            # anything else parks a contender; the same value refreshes it, a new
            # one replaces it
            set_add("Aquarium", "fish count", "neon tetra"),
            set_add("Aquarium", "fish count", "Neon Tetra "),
            set_add("Aquarium", "fish count", "guppy"),
            set_add("Aquarium", "budget", "new canister filter"),
        ],
    },
    "set-cap": {
        "config": None,
        "seed": [sql(SET_CAP_MEMBERS, after_stop=True)],
        "ops": [
            # a hundred current members: capped, logged, nothing written but the log
            set_add("Cap Box", "tags", "volcanic basalt sample"),
            # dedup runs before the cap
            set_add("cap box", "tags", "TAG-007"),
            # a removal makes room (the slot's rows are rewritten again; the
            # confirm above already marked it dirty)
            set_remove("cap box", "tags", "tag-000"),
            set_add("Cap Box", "tags", "volcanic basalt sample"),
            # cosine 0.66 to the one embedded member: capped again
            set_add("cap box", "tags", "medieval lute tablature"),
        ],
    },
})


# memory_fact_resolve (spec FR1-FR8). Every branch service.cortex_resolve
# reaches over MCP. Contenders are parked through the Python daemon's own
# tools: memory_fact_set at a weaker tier or below the confidence margin
# (protect_provenance is on by default), and memory_set_add's aggregate park.
# A contender with no current scalar exists only once memory_set_add has
# converted the scalar it was parked against; removing every member then
# leaves it parked at a slot with neither a current scalar nor members. The
# member cosines (measured as for the set-* scenarios) sit far below the 0.9
# dedup line: 0.81 (Home Lab), 0.81 (Garden Shed), 0.80 (Bike Rack).
SCENARIOS.update({
    "fact-resolve-scalar": {
        "config": None,
        "seed": [
            # a user scalar; an agent contender, replaced by a second one
            fact("Payments DB", "host", "10.0.0.5", origin="user", confidence=0.9,
                 authority="directive"),
            fact("Payments DB", "host", "10.0.0.6"),
            fact("Payments DB", "host", "10.0.0.7"),
            # a user contender parked below the confidence margin
            fact("Build Server", "hostname", "ci-box-2", origin="user", confidence=0.9),
            fact("Build Server", "hostname", "ci-box-3", origin="user", confidence=0.5),
            # an agent contender under an action scalar
            fact("dev box", "os", "Debian 12", origin="action"),
            fact("dev box", "os", "Debian 13"),
        ],
        "ops": [
            # nothing parked: no_contender with the raw arguments (the accept
            # still ticks the clock)
            resolve("Nobody", "thing", True),
            # accept: the old current superseded, the contender promoted with
            # the user tier and the old value as supersedes_value
            resolve("payments db", "Host", True),
            # nothing left to resolve, either way
            resolve("Payments DB", "host", True),
            resolve("Payments DB", "host", False),
            # the promoted record holds the slot: a restatement confirms it
            fact("Payments DB", "host", "10.0.0.7"),
            # the user tier is already there; accept binds laxly ("yes" is
            # true, so the contender is promoted)
            resolve("Build Server", "hostname", "yes"),
            # reject, bound laxly (0 is false, so the contender is retired
            # rather than promoted); the kept current is answered twice
            resolve("Dev Box", "OS", 0),
            # nothing left to resolve; "no" binds to false
            resolve("dev box", "os", "no"),
        ],
    },
    "fact-resolve-set-slot": {
        "config": None,
        "seed": [
            # a contender parked against a scalar that then converts to a set
            fact("Home Lab", "services", "Jellyfin media server", origin="user",
                 confidence=0.9),
            fact("Home Lab", "services", "Pi-hole DNS ad blocker"),
            set_add("Home Lab", "services", "Nextcloud file sync"),
            # the same, then every member removed: no current, no members;
            # the contender replaced one, so it carries a supersedes_value
            fact("Garden Shed", "tools", "cordless drill", origin="user", confidence=0.9),
            fact("Garden Shed", "tools", "hedge trimmer"),
            fact("Garden Shed", "tools", "leaf blower"),
            set_add("Garden Shed", "tools", "pressure washer"),
            set_remove("Garden Shed", "tools", "cordless drill"),
            set_remove("Garden Shed", "tools", "pressure washer"),
            fact("Bike Rack", "bikes", "road bike", origin="user", confidence=0.9),
            fact("Bike Rack", "bikes", "gravel bike"),
            set_add("Bike Rack", "bikes", "kids balance bike"),
            set_remove("Bike Rack", "bikes", "road bike"),
            set_remove("Bike Rack", "bikes", "kids balance bike"),
            # a member parked by the aggregate guard
            fact("Aquarium", "fish count", "32", origin="user"),
            set_add("Aquarium", "fish count", "neon tetra"),
        ],
        "ops": [
            # accept at a slot with current members: slot_holds_set, nothing written
            resolve("home lab", "Services", True),
            # reject is allowed there: current null, the retired contender answered
            resolve("Home Lab", "services", False),
            # accept with no current: the contender keeps its own supersedes_value
            resolve("garden shed", "tools", True),
            # reject with no current
            resolve("Bike Rack", "bikes", False),
            # the parked member replaces the protected total
            resolve("Aquarium", "Fish Count", True),
        ],
    },
})


# memory_outcome and memory_reinforce (spec OC1-OC12). Entry ids follow the
# seed order (1-5). Seeded searches (top_k 1) give the window its events: id 2
# served twice in sess-a, id 4 once in sess-b, id 5 twice with no session.
# Entry 1 draws the Python daemon's own startup warmup search (a no-session
# event Rust does not write, declared), so no op names it: a session-less
# credit could name that event, and a session's credit would count it as
# served elsewhere.
OUTCOME_SEED = [
    store("The warmup probe runs once when the daemon starts."),
    store("The payments database listens on port 5432 on db-1.", [sess("sess-a")]),
    store("Deploys go out through the blue-green pipeline on Fridays.", [sess("sess-a")]),
    store("The bench box runs Debian 12 with a 4 TB scratch disk.", [sess("sess-b")]),
    store("Nightly backups land in the cold-storage bucket."),
    search("payments database port", [sess("sess-a")]),
    search("which port does the payments database use", [sess("sess-a")]),
    search("bench box scratch disk", [sess("sess-b")]),
    search("nightly backups bucket"),
    search("where do the nightly backups go"),
]

SCENARIOS.update({
    "outcome-labels": {
        "config": None,
        "seed": OUTCOME_SEED,
        "ops": [
            # credited by both sess-a events, unmatched, served elsewhere,
            # unknown; attributed to sess-a's open leaf
            outcome("deploy engine to host", "success", [sess("sess-a")],
                    used_ids=[2, 3, 4, 999]),
            # text with junk tokens and a repeat; a repeat credit writes no
            # row (a JSON list in a string with a non-integer item is
            # refused by the binder, before the body: not this case)
            outcome("deploy engine to host", "failure", [sess("sess-a")],
                    used_ids="2 2 x, true"),
            # handle attribution without a header, every optional field
            outcome("bench maintenance", "correction", episode="@root:sess-b:12",
                    about="debian", detail="the upgrade path", polarity="-"),
            # an unknown handle warns and falls back to the header's leaf
            outcome("unknown handle", "success", [sess("sess-a")], episode="ffffffffffff"),
            # no session: only the most recent NULL-session event per id
            outcome("backups", "success", used_ids="5, 5"),
            # an empty list and junk: the signal lands, the label does not
            outcome("empty label", "success", [sess("sess-a")], used_ids=[]),
            outcome("junk label", "success", [sess("sess-a")], used_ids="abc def"),
            # an id past BIGINT fails the label statement: counted, kept unchecked
            outcome("big id", "success", [sess("sess-a")], used_ids="99999999999999999999 2"),
            # over the cap of fifty (from 2: entry 1 is the warmup's)
            outcome("many ids", "success", [sess("sess-a")], used_ids=list(range(2, 55))),
            # both counters, the resident copy, the dereference credit
            reinforce(2, [sess("sess-a")]),
            reinforce(999999),
            # no session and no NULL-session event serving it: no credit
            reinforce(4),
        ],
    },
    "outcome-lessons-off": {
        "config": "memory:\n  lessons:\n    enabled: false\n",
        "seed": [store("A note so the bank is not empty.")],
        "ops": [
            outcome("lessons are off", "success", used_ids="abc"),
            outcome("lessons are off", "failure", used_ids=[1]),
        ],
    },
    "outcome-log-off": {
        "config": "memory:\n  retrieval_log:\n    enabled: false\n",
        "seed": [store("A note so the bank is not empty.", [sess("sess-a")])],
        "ops": [
            outcome("log is off", "success", [sess("sess-a")], used_ids=[1]),
            reinforce(1, [sess("sess-a")]),
        ],
    },
})


# memory_history slot mode (spec HI1-HI7), on cortex records written through
# the Python daemon's own fact tools. Chain mode answers the declared stub
# and is not compared here.
SCENARIOS.update({
    "history-slot": {
        "config": None,
        "seed": [
            fact("Payments DB", "host", "10.0.0.5", origin="user", confidence=0.9),
            fact("Payments DB", "host", "10.0.0.6", origin="user", confidence=0.9),
            fact("Payments DB", "host", "10.0.0.7", origin="user", confidence=0.9),
            set_add("Home Lab", "hosts", "alpha"),
            set_add("Home Lab", "hosts", "beta"),
            set_remove("Home Lab", "hosts", "alpha"),
            set_add("Home Lab", "hosts", "gamma"),
            # a contender parked below the margin, then the current value
            # restated: its re-confirmation moves its tx_time past the
            # contender's, so transaction order differs from assertion order
            fact("Build Server", "hostname", "ci-box-2", origin="user", confidence=0.9),
            fact("Build Server", "hostname", "ci-box-3", origin="user", confidence=0.5),
            fact("Build Server", "hostname", "ci-box-2", origin="user", confidence=0.9),
        ],
        "ops": [
            history("Payments DB", "host"),
            # the slot key is case- and separator-insensitive
            history("payments db", "HOST"),
            # a cutoff between the first two versions keeps the first
            history("Payments DB", "host", as_of="@txmid:payments-db:host"),
            # ISO with an offset, a naive date (local time), epoch zero
            history("Payments DB", "host", as_of="1970-01-02T00:00:00+00:00"),
            history("Payments DB", "host", as_of="2000-01-01"),
            history("Payments DB", "host", as_of=0.0),
            # float spellings: NaN keeps nothing, infinity everything; both
            # echo as null in structured content (the text block differs,
            # declared)
            history("Payments DB", "host", as_of="nan"),
            history("Payments DB", "host", as_of="inf"),
            # set slot: adds and the removal in time order
            history("Home Lab", "hosts"),
            history("home lab", "Hosts", as_of="9999-12-31T00:00:00+00:00"),
            history("Home Lab", "hosts", as_of="1970-01-02"),
            # transaction time orders the chain, not assertion time
            history("Build Server", "hostname"),
            # an empty slot
            history("Nobody", "nothing"),
            # refused at binding
            history("Payments DB", "host", as_of="yesterday"),
        ],
    },
})


# The world cortex (spec WD1-WD12), on templates whose world facts the
# Python daemon's own tool wrote. A retrieved_at of 1.0 makes a fact stale.
WORLD_SEED = [
    world_set("Rust", "latest stable version", "1.90.0",
              source_url="https://blog.rust-lang.org/", source_quote="Rust 1.90.0 is out.",
              freshness_class="slow"),
    world_set("Python", "latest version", "3.13.0", source_url="https://www.python.org/",
              retrieved_at=1.0),
    world_set("PostgreSQL", "license", "PostgreSQL License", freshness_class="evergreen",
              source_url="https://www.postgresql.org/about/licence/"),
]

SCENARIOS.update({
    "world-facts": {
        "config": None,
        # A non-finite retrieved_at: Python stores and reloads it, so the
        # Rust daemon must start on a bank that holds one.
        "seed": WORLD_SEED + [world_set("Clock", "skew", "unknown", retrieved_at="inf")],
        "ops": [
            # compact entries; the stale one carries correct_with and the note
            world_search("what is the newest Rust release"),
            world_search("python version", verbose=True),
            world_search("database license", top_k=1),
            # the same value restated: confirmed, confidence lifted, citation
            # kept when empty, a new hash recorded
            world_set("rust", "Latest Stable Version", " 1.90.0 ", confidence=0.95,
                      content_hash="sha256:abc"),
            # a different value supersedes, with a session header
            world_set("Rust", "latest stable version", "1.91.0", [sess("sess-w")],
                      source_url="https://blog.rust-lang.org/2026/", retrieved_at=1600000000.0),
            # unsafe schemes are refused before anything runs
            world_set("Evil", "link", "x", source_url="javascript:alert(1)"),
            world_set("Evil", "link", "x", source_url="\tJaVaScRiPt:alert(1)"),
            # a scheme-less citation is inert and allowed
            world_set("Example", "home page", "example.com/page", source_url="example.com/page"),
            world_search("rust release", top_k=10),
            # the non-finite retrieved_at: never stale, undecayed, echoed null
            world_search("clock skew", top_k=1, verbose=True),
            history("Rust", "latest stable version"),
        ],
    },
    "world-facts-demote": {
        "config": "memory:\n  search:\n    stale_policy: demote\n",
        "seed": WORLD_SEED,
        "ops": [
            # the stale fact sorts last and carries the warning
            world_search("python version and rust version", top_k=5),
            world_set("Python", "latest version", "3.14.0", source_url="https://www.python.org/"),
        ],
    },
    "world-facts-quarantine": {
        "config": "memory:\n  search:\n    stale_policy: quarantine\n",
        "seed": WORLD_SEED,
        "ops": [
            # the stale value moves to last_known_value, in both projections
            world_search("python version"),
            world_search("python version", verbose=True),
        ],
    },
})


# Lessons (spec LS1-LS10) and the memory_history chain (CH1-CH6). The
# lessons are SQL seeds (only the dream writes them), asserted in 2023, so a
# fact written now about their subject makes them re_verify: directly for
# "blue-green pipeline", and through the graph alias "bearer tokens" ->
# "auth tokens" for the credentials lesson.
LESSON_TEXTS = [
    "Deploy the engine through the blue-green pipeline, never by hand.",
    "The flaky integration test needs the bench Postgres on port 5433.",
    "Rotate the bearer tokens with the CLI, not by editing files.",
]

SCENARIOS.update({
    "lessons": {
        "config": None,
        "seed": [
            *[store(t, [sess("sess-l")]) for t in LESSON_TEXTS],
            fact("blue-green pipeline", "status", "retired", origin="user", confidence=0.9),
            fact("auth tokens", "rotation", "monthly", origin="user", confidence=0.9),
            tool("memory_alias", entity="auth tokens", alias="bearer tokens"),
            tool("memory_graph_relate", src="blue-green pipeline", relation="uses",
                 dst="Jenkins"),
            # a user rewrite supersedes: the chain's superseded event
            fact("blue-green pipeline", "status", "decommissioned", origin="user",
                 confidence=0.9),
            lesson_row("deploy engine to host", "approach", "deploy-engine-to-host", "approach",
                       "Deploy through the blue-green pipeline, never by hand.",
                       "blue-green pipeline", "+", "success", "current", LESSON_TEXTS[0]),
            lesson_row("run integration tests", "pitfall", "run-integration-tests", "pitfall",
                       "The suite needs the bench Postgres on port 5433.", None, "-",
                       "failure", "current", LESSON_TEXTS[1]),
            lesson_row("rotate credentials", "tool-choice", "rotate-credentials", "tool-choice",
                       "Rotate bearer tokens with the CLI.", "bearer tokens", "+", "success",
                       "current", LESSON_TEXTS[2]),
            lesson_row("deploy engine to host", "approach", "deploy-engine-to-host", "approach",
                       "Deploy by hand over ssh.", "blue-green pipeline", "+", "success",
                       "superseded", LESSON_TEXTS[0]),
        ],
        "ops": [
            # compact entries; re_verify directly and through the graph alias
            lesson_search("how do I deploy the engine to a host"),
            lesson_search("integration tests keep failing", verbose=True),
            lesson_search("rotating credentials", top_k=1),
            # a session's search logs its session and open episode
            lesson_search("deploy and rotate", [sess("sess-l")], top_k=3),
            # the chain: facts, the edge and the about-named lesson
            history_chain("blue-green pipeline"),
            # alias names widen the keys: the bearer-tokens lesson joins
            history_chain("auth tokens"),
            # a lesson matched by its task key
            history_chain("deploy engine to host"),
            history_chain("Nobody at all"),
            history_chain("   "),
        ],
    },
})


SCENARIOS.update({
    # The score floor and a disabled retrieval log: lessons under 0.55 are cut
    # and no lesson_search_events row is written.
    "lessons-config": {
        "config": "memory:\n  lessons:\n    min_confidence: 0.55\n"
                  "  retrieval_log:\n    enabled: false\n",
        "seed": SCENARIOS["lessons"]["seed"],
        "ops": [
            lesson_search("deploy and rotate and test", [sess("sess-l")], top_k=5),
        ],
    },
})


# The episode tools (spec EP1-EP12), on templates whose sessions, board rows
# and active-session pointer the Python daemon's own calls created.
SCENARIOS.update({
    "episode-lifecycle": {
        "config": None,
        "seed": [
            store("First note in session A", [sess("sess-a")]),
            store("A note in session B", [sess("sess-b")]),
            store("Closed session note", [sess("sess-closed")]),
            {"op": "http", "method": "POST", "path": "/api/hook/session-end",
             "body": {"session_id": "sess-closed"}},
        ],
        "ops": [
            # nested under the session root, then under the open sub-episode
            ep_start("Sub A", [sess("sess-a")], hint="porting the episode tools"),
            ep_start("Sub A2", [sess("sess-a")]),
            store("A note inside the nested sub-episode", [sess("sess-a")]),
            # each end pops one level; the session root is never closed
            ep_end([sess("sess-a")]),
            ep_end([sess("sess-a")]),
            ep_end([sess("sess-a")]),
            # another session's sub-episode becomes the global current leaf
            ep_start("Sub B", [sess("sess-b")]),
            # the handle anchors to its own root, not the current leaf
            ep_start("Handle sub", episode="@root:sess-a:12"),
            ep_end(episode="@root:sess-a:12"),
            ep_end(episode="@root:sess-a:12"),
            ep_end([sess("sess-b")]),
            ep_end([sess("sess-b")]),
            # an unknown handle degrades to the header's session, which opens
            # its root lazily, and warns
            ep_start("Unknown handle", [sess("sess-c")], episode="ffffffffffff"),
            # an end refuses an unknown or short handle
            ep_end(episode="ffffffffffff"),
            ep_end(episode="abc"),
            # no handle and no session: the open current leaf is another
            # session's
            ep_end(episode=""),
            # an empty title is a title
            ep_start("", [sess("sess-d")]),
            # nothing open for the session
            ep_end([sess("sess-e")]),
            # no session for a title either
            session_title("No session on this request"),
            ep_summary("@root:sess-a:32"),
            ep_summary("@ep:Sub A2"),
            ep_summary("ffffffffffffffffffffffffffffffff"),
            # a closed root: an end refuses its handle, a start reopens it
            ep_end(episode="@root:sess-closed:12"),
            ep_start("Reopened by its handle", episode="@root:sess-closed:12"),
            ep_summary("@root:sess-closed:32"),
        ],
    },
    "session-title": {
        "config": None,
        "seed": [
            store("First note in session A", [sess("sess-a")], tags=["zeta"]),
            store("Second note in session A", [sess("sess-a")], tags=["alpha", "zeta"]),
            store("A note in session B", [sess("sess-b")]),
            # board rows under the sessions: one the title may name, one an
            # agent named, one in another session
            register("sess-a"),
            register("sess-a", name="Named by its agent"),
            register("sess-b"),
            store("Closed session note", [sess("sess-closed")]),
            {"op": "http", "method": "POST", "path": "/api/hook/session-end",
             "body": {"session_id": "sess-closed"}},
            {"op": "http", "method": "GET", "path": "/api/hook/session-start?session_id=sess-ptr"},
        ],
        "ops": [
            # the root, its entries' stamps and the unnamed board row
            session_title("  Pseudolife-MCP - episode tools  ", [sess("sess-a")]),
            # idempotent: nothing changes
            session_title("Pseudolife-MCP - episode tools", [sess("sess-a")]),
            session_title("   ", [sess("sess-a")]),
            # by handle, without a header
            session_title("Named through the handle", episode="@root:sess-b:12"),
            session_title("Unknown handle", episode="ffffffffffff"),
            session_title("Short handle", episode="abc"),
            # no header: the active-session pointer's root
            session_title("Named by the pointer"),
            # a session with no root yet opens one under the title
            session_title("A brand new session", [sess("sess-new")]),
            # a closed root reopens and takes the title
            session_title("Resumed by its key", [sess("sess-closed")]),
            # a reserved name retitles the session but names no board row
            session_title("maintainer notes", [sess("sess-a")]),
            # the pointer's session nests and pops without a header
            ep_start("Pointer sub"),
            ep_end(),
            ep_summary("@root:sess-a:32"),
        ],
    },
})


# ── per-side argument templates ───────────────────────────────────────────

# memory_supersede (spec CO1-CO10): corrections by id and by exact text on
# templates the Python daemon's own tools wrote. The duplicate row and the
# trace rows are SQL seeds: no tool writes a live exact duplicate, and only
# the dream writes memory_traces.
def supersede(new_text, headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_supersede", "args": {"new_text": new_text, **kw},
            "headers": list(headers)}


# Scenarios whose operations may include ``external`` writes.
EXTERNAL_WRITES = {"supersede-branches", "reinstate", "consolidation-candidates",
                   "review-list", "review-actions", "review-slots",
                   "consolidate-branches"}


def external(statement: str) -> dict:
    """An out-of-band write, run on both banks between two calls."""
    return {"op": "sql", "sql": statement, "external": True}


def trace_sql(text, entity_norm, attribute_norm) -> dict:
    """Seed-only: a memory_traces row tying ``text``'s entry to a slot."""
    return sql("INSERT INTO memory_traces (entity_norm, attribute_norm, entry_id, created_at) "
               f"SELECT '{entity_norm}', '{attribute_norm}', id, 1000.0 FROM entries "
               f"WHERE text = '{text}' ORDER BY id LIMIT 1")


SUPERSEDE_SEED = [
    store("The daemon port is 8765.", [sess("sess-a")]),
    store("Our build server hostname is ci-box-2."),
    store("According to the docs, the bank volume is external."),
    store("Never deploy on Fridays."),
    # a retired "Alpha config is red." twin and a live one
    store("Alpha config is red."),
    supersede("Alpha config is blue.", old_text="Alpha config is red."),
    supersede("Alpha config is red.", old_text="Alpha config is blue."),
    store("Gamma retired note."),
    supersede("Gamma replacement note.", old_text="Gamma retired note."),
    # derived slots: a live one, one whose only member was removed (named by
    # its records), and one no record names (named by its key)
    store("Payments DB host is 10.0.0.5 per the runbook."),
    fact("Payments DB", "host", "10.0.0.5", origin="user", confidence=0.9),
    set_add("Home Lab", "hosts", "alpha"),
    set_remove("Home Lab", "hosts", "alpha"),
    trace_sql("Payments DB host is 10.0.0.5 per the runbook.", "payments-db", "host"),
    trace_sql("Payments DB host is 10.0.0.5 per the runbook.", "home-lab", "hosts"),
    trace_sql("Payments DB host is 10.0.0.5 per the runbook.", "old-box", "ip"),
    store("Omega note behind many derived slots."),
    sql("INSERT INTO memory_traces (entity_norm, attribute_norm, entry_id, created_at) "
        "SELECT 'slot-' || lpad(g::text, 3, '0'), 'attr', e.id, 1000.0 FROM entries e, "
        "generate_series(1, 52) AS g WHERE e.text = 'Omega note behind many derived slots.'"),
    store("Delta duplicated note."),
    store("Epsilon row to vanish."),
    store("Zeta row retired behind the daemon's back."),
    dict(sql("INSERT INTO entries (band, text, embedding, surprise, ts, access_count, source, "
             "episode_id, episode_title, tags, slots, authority, distortion_tolerance, "
             "dream_state) SELECT band, text, embedding, surprise, ts + 1, 0, 'user', "
             "episode_id, episode_title, tags, slots, authority, distortion_tolerance, "
             "dream_state FROM entries WHERE text = 'Delta duplicated note.'"), after_stop=True),
]

SCENARIOS.update({
    "supersede-branches": {
        "config": None,
        "seed": SUPERSEDE_SEED,
        "ops": [
            # by id, with a session header
            supersede("The daemon port is 8766.", [sess("sess-a")],
                      entry_id="@id:The daemon port is 8765."),
            # the retired source is resident as retired; the replacement is resident
            supersede("x", entry_id="@id:The daemon port is 8765."),
            supersede("The daemon port is 8767.", old_text="The daemon port is 8766."),
            # by exact text, an observation
            supersede("Our build server hostname is ci-box-3.",
                      old_text="Our build server hostname is ci-box-2."),
            # labels: quoted carries over; the new text's directive wins its axis
            supersede("The bank volume is internal now.",
                      old_text="According to the docs, the bank volume is external."),
            supersede("Always deploy on Fridays after review.", old_text="Never deploy on Fridays."),
            # a retired twin does not make an exact text ambiguous
            supersede("Alpha config is green.", old_text="Alpha config is red."),
            # the derived slots, live first, the cap's totals
            supersede("Payments DB host is 10.0.0.6 per the runbook.",
                      old_text="Payments DB host is 10.0.0.5 per the runbook."),
            # fifty-two derived slots: the first fifty, the total, truncated
            supersede("Omega note behind fewer derived slots.",
                      old_text="Omega note behind many derived slots."),
            # refusals: nothing changes
            supersede("x", old_text="Gamma replacement note.", entry_id=1),
            supersede("x"),
            supersede("x", entry_id=0),
            supersede("x", entry_id=-3),
            supersede("x", entry_id=99999999999999999999),
            supersede("x", old_text="   "),
            supersede("   ", old_text="Gamma replacement note."),
            supersede("x", entry_id=999999),
            supersede("x", old_text="no such note"),
            supersede("x", old_text="Gamma retired note."),
            supersede("x", entry_id="@id:Gamma retired note."),
            supersede("x", old_text="Delta duplicated note."),
            # the meta filter is off by default: this replacement is stored
            supersede("No relevant memories about the deploy.", old_text="Gamma replacement note."),
            # a row deleted behind the daemon's back
            external("DELETE FROM entries WHERE text = 'Epsilon row to vanish.'"),
            supersede("x", old_text="Epsilon row to vanish."),
            # a row retired behind the daemon's back: supersede_entries refuses
            external("UPDATE entries SET superseded_at = 1700000000.5, superseded_by_text = 'elsewhere' "
                "WHERE text = 'Zeta row retired behind the daemon''s back.'"),
            supersede("x", old_text="Zeta row retired behind the daemon's back."),
        ],
    },
    # Capacity: the victim is chosen with the target already retired, so a
    # full band evicts the target itself when nothing cheaper is there.
    "supersede-capacity": {
        "config": "memory:\n  miras:\n    preset: custom\n    bands:\n      - name: flat\n        max_entries: 5\n",
        "seed": [
            store("System prompt one, retained.", source="system"),
            store("System prompt two, retained.", source="system"),
            store("System prompt three, retained.", source="system"),
            store("Agent note about the release checklist."),
            store("Thinking trace about the index rebuild.", source="llm_thinking"),
        ],
        "ops": [
            supersede("Agent note about the revised release checklist.",
                      old_text="Agent note about the release checklist."),
            supersede("System prompt one, revised.", old_text="System prompt one, retained."),
        ],
    },
    # A surprise threshold: a correction bypasses the gate, so a replacement
    # close to what is held is stored where a plain store would be refused.
    "supersede-gate": {
        "config": "memory:\n  surprise_threshold: 0.35\n",
        # store-gate's near-duplicates: no contradiction cue (capitals,
        # numbers, negation, possession), so only the gate decides.
        "seed": [store("the tea kettle whistles loudly when the water boils"),
                 store("the garden hose is coiled behind the shed")],
        "ops": [
            # a near-duplicate of another entry: a plain store is refused
            store("the kettle whistles loudly when water boils"),
            # as a correction of an unrelated entry it is stored
            supersede("the kettle whistles loudly when water boils",
                      old_text="the garden hose is coiled behind the shed"),
        ],
    },
    # The meta filter refuses a replacement: the whole correction rolls back,
    # the trace invalidation included, and the source stays live.
    "supersede-meta-filter": {
        "config": "memory:\n  meta_filter:\n    enabled: true\n",
        "seed": [
            store("The on-call rotation changes on Mondays."),
            trace_sql("The on-call rotation changes on Mondays.", "on-call", "rotation"),
        ],
        "ops": [
            supersede("No relevant memories about the rotation.",
                      old_text="The on-call rotation changes on Mondays."),
            supersede("The on-call rotation changes on Tuesdays.",
                      old_text="The on-call rotation changes on Mondays."),
        ],
    },
    # Traces off: no derived slots are read, but the invalidations still land.
    "supersede-traces-off": {
        "config": "memory:\n  traces:\n    enabled: false\n",
        "seed": [
            store("The queue depth limit is 64."),
            trace_sql("The queue depth limit is 64.", "queue", "depth-limit"),
        ],
        "ops": [
            supersede("The queue depth limit is 128.", old_text="The queue depth limit is 64."),
        ],
    },
})


# memory_reinstate (spec RI1-RI9): reviewed reinstatements of retirements the
# Python daemon's own supersede wrote. The reviewer is a named principal
# (PSEUDOLIFE_MCP_TOKENS); the singular token is the default principal.
REVIEWER = ("Authorization", "Bearer tok-rev-w2e-0002")
OP_A = "0f1e2d3c-4b5a-4968-8776-655443322110"
OP_B = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
OP_C = "2b3c4d5e-6f70-4b8c-9dae-1f2a3b4c5d6e"
OP_D = "3c4d5e6f-7081-4c9d-aebf-2a3b4c5d6e7f"
OP_E = "4d5e6f70-8192-4dae-bfc0-3b4c5d6e7f80"
OP_F = "5e6f7081-92a3-4ebf-c0d1-4c5d6e7f8091"


def reinstate(text, headers=(REVIEWER,), **kw) -> dict:
    args = {"entry_id": f"@id:{text}", "operation_id": OP_A,
            "expected_text_sha256": f"@sha:text:{text}",
            "expected_source_sha256": f"@sha:source:{text}",
            "expected_superseded_at": f"@sup:{text}",
            "expected_superseded_by_text_sha256": f"@sha:by:{text}",
            "evidence_packet_sha256": "ab" * 32, "reviewer_ids": ["reviewer-1", "Zoë"],
            "reason": "the replacement was wrong"}
    args.update(kw)
    return {"op": "tool", "name": "memory_reinstate", "args": args, "headers": list(headers)}


TTL = "The cache TTL is 300 seconds."
SCENARIOS.update({
    "reinstate": {
        "config": None,
        "env": {"PSEUDOLIFE_MCP_TOKENS": "tok-rev-w2e-0002:reviewer"},
        "seed": [
            store(TTL),
            supersede("The cache TTL is 600 seconds.", old_text=TTL),
            store("Backups run nightly at 02:00."),
            trace_sql("Backups run nightly at 02:00.", "backups", "schedule"),
            supersede("Backups run nightly at 03:00.", old_text="Backups run nightly at 02:00."),
            store("The release train leaves on Tuesdays."),
            store("The legacy importer is retired."),
            dict(sql("UPDATE entries SET superseded_at = 1700000000.25, superseded_by_text = NULL "
                     "WHERE text = 'The legacy importer is retired.'"), after_stop=True),
            store("The proxy listens on port 3128."),
            # entry 8: deleted behind the daemons' backs below
            store("Feature flags live in the config service."),
            store("The audit log rotates weekly."),
            sql("UPDATE entries SET superseded_at = 1700000000, superseded_by_text = 'older text' "
                "WHERE text = 'The audit log rotates weekly.'", after_stop=True),
        ],
        "ops": [
            # the default principal is refused before anything runs
            reinstate(TTL, headers=()),
            # request validation, in order
            reinstate(TTL, entry_id=0),
            reinstate(TTL, operation_id="not-a-uuid"),
            reinstate(TTL, operation_id=OP_A.upper()),
            reinstate(TTL, operation_id="{" + OP_A + "}"),
            reinstate(TTL, operation_id="urn:uuid:" + OP_A),
            reinstate(TTL, operation_id=OP_A.replace("-", "")),
            reinstate(TTL, operation_id="0x" + OP_A.replace("-", "")[2:]),
            reinstate(TTL, expected_text_sha256="AB" * 32),
            reinstate(TTL, expected_superseded_at="inf"),
            reinstate(TTL, reviewer_ids=[]),
            reinstate(TTL, reviewer_ids=["  "]),
            reinstate(TTL, reason="   "),
            # the target
            reinstate(TTL, entry_id=999999),
            reinstate(TTL, entry_id=99999999999999999999),
            reinstate("The release train leaves on Tuesdays."),
            reinstate("The legacy importer is retired."),
            reinstate(TTL, expected_text_sha256="0" * 64),
            reinstate("Backups run nightly at 02:00.", operation_id=OP_B),
            # committed; the resident entry is live at once (a correction by
            # text finds it); the replay reports it retired again; a
            # different request under the UUID conflicts
            reinstate(TTL),
            supersede("The cache TTL is 900 seconds.", old_text=TTL),
            reinstate(TTL),
            reinstate(TTL, reason="a different reason"),
            # an integral retirement time: the fingerprint carries 1700000000.0
            reinstate("The audit log rotates weekly.", operation_id=OP_F,
                      expected_superseded_at=1700000000),
            # the target's row is mirrored before the checks: retired, then
            # inserted, behind the daemons' backs
            external("UPDATE entries SET superseded_at = 1700000002.5, "
                     "superseded_by_text = 'retired elsewhere' "
                     "WHERE text = 'The proxy listens on port 3128.'"),
            reinstate("The proxy listens on port 3128.", operation_id=OP_C),
            # a replay of a live commit
            reinstate("The proxy listens on port 3128.", operation_id=OP_C),
            external("INSERT INTO entries (band, text, embedding, surprise, ts, access_count, "
                     "source, superseded_at, superseded_by_text, tags, slots, dream_state) "
                     "SELECT band, 'Inserted elsewhere, then retired.', embedding, surprise, "
                     "ts + 1, 0, 'agent', 1700000003.5, 'replacement elsewhere', '[]'::jsonb, "
                     "'[]'::jsonb, 'pending' FROM entries "
                     "WHERE text = 'The proxy listens on port 3128.'"),
            reinstate("Inserted elsewhere, then retired.", operation_id=OP_D),
            # deleted: the mirror drops the resident entry, which then cannot
            # be corrected either
            external("DELETE FROM entries WHERE text = 'Feature flags live in the config service.'"),
            reinstate("Feature flags live in the config service.", operation_id=OP_E, entry_id=8),
            supersede("Feature flags live in the deploy repo.",
                      old_text="Feature flags live in the config service."),
            # a replay after the row is gone reports it missing
            external(f"DELETE FROM entries WHERE text = '{TTL}'"),
            reinstate(TTL),
        ],
    },
})


# memory_forget (spec FG1-FG16) over the four stores, on templates whose
# entries, facts, world facts, graph edge and slot reads the Python daemon's
# own tools wrote. The digest, the trace rows, the lineage lessons and their
# audits, and the edge's agent origin and evidence are SQL seeds: only the
# dream writes those.
def forget(scope, headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_forget", "args": {"scope": scope, **kw},
            "headers": list(headers)}


def fact_get(entity, attribute) -> dict:
    return tool("memory_fact_get", entity=entity, attribute=attribute)


def lineage_lesson(task, aspect, task_norm, aspect_norm, lesson, about, source_texts,
                   embedding_text) -> list[dict]:
    """Seed-only: a current lesson whose provenance and synthesis lineage
    audit name the entries with ``source_texts`` (by their ids), its
    embedding copied from ``embedding_text``'s entry."""
    def q(v):
        return "NULL" if v is None else "'" + v.replace("'", "''") + "'"
    texts = ", ".join(q(t) for t in source_texts)
    return [
        sql("INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, value, about, "
            "polarity, outcome, status, confidence, origin, support, provenance, asserted_at, "
            "last_confirmed, embedding) SELECT "
            f"{q(task)}, {q(aspect)}, {q(task_norm)}, {q(aspect_norm)}, {q(lesson)}, {q(about)}, "
            "'+', 'success', 'current', 0.7, 'agent', '[\"signal:1\"]', "
            "(SELECT jsonb_agg('entry:' || id ORDER BY id) FROM entries "
            f"WHERE text IN ({texts})), 1700000000, 1700000000, embedding "
            f"FROM entries WHERE text = {q(embedding_text)}", after_stop=True),
        sql("INSERT INTO store_decisions (store, entity_norm, attribute_norm, action, "
            "decided_by, reason, record, decided_at) SELECT 'lesson', "
            f"{q(task_norm)}, {q(aspect_norm)}, 'lineage', 'lesson_synthesis', "
            "'verified_dependencies', jsonb_build_object('lesson', "
            f"{q(lesson)}, 'asserted_at', 1700000000, 'source_entry_ids', "
            f"(SELECT jsonb_agg(id ORDER BY id) FROM entries WHERE text IN ({texts}))), "
            "1700000000.5", after_stop=True),
    ]


DIGEST_TEXT = "Digest of the deploy checklist session."
FORGET_SEED = [
    store("Probe entry alpha.", [sess("sess-f")], source="status"),
    store("Probe entry beta.", [sess("sess-f")], source="status"),
    store("Probe entry gamma.", source="status"),
    store("Keep the deploy checklist current.", [sess("sess-f")]),
    store("Scratch entry with a tag.", tags=["Scratch"]),
    store("Payments DB moved to host 10.0.0.9 per the runbook.", [sess("sess-g")]),
    store("Payments DB listens on 5432 per the on-call.", [sess("sess-k")]),
    fact("Payments DB", "host", "10.0.0.9", origin="user", confidence=0.9),
    fact("Payments DB", "port", "5432", origin="user", confidence=0.9),
    store("The backup job runs at 02:00 nightly.", [sess("sess-h")]),
    store("The flaky test needs a warm cache.", [sess("sess-h")]),
    store("A sess-h aside about lunch.", [sess("sess-h")]),
    store("An on-call handover note.", [sess("sess-m")]),
    store("Payments DB replica is db-2.", [sess("sess-n")]),
    store("Payments DB replica confirmed as db-2 by the DBA.", [sess("sess-n")]),
    fact("Payments DB", "replica", "db-2", origin="user", confidence=0.9),
    tool("memory_graph_relate", src="flaky test", relation="uses", dst="warm cache"),
    # the digest of sess-f's root, retired as history by a source forget
    sql("INSERT INTO entries (band, text, embedding, surprise, ts, access_count, source, "
        "episode_id, episode_title, tags, slots, dream_state) SELECT band, "
        f"'{DIGEST_TEXT}', embedding, surprise, ts + 1, 0, 'digest', episode_id, "
        "episode_title, '[]'::jsonb, '[]'::jsonb, 'pending' FROM entries "
        "WHERE text = 'Keep the deploy checklist current.'", after_stop=True),
    # a retired digest of sess-m's root: kept as history by a source forget
    sql("INSERT INTO entries (band, text, embedding, surprise, ts, access_count, source, "
        "superseded_at, superseded_by_text, episode_id, episode_title, tags, slots, "
        "dream_state) SELECT band, 'Retired digest of the handover session.', embedding, "
        "surprise, ts + 1, 0, 'digest', 1700000000.5, 'an older digest', episode_id, "
        "episode_title, '[]'::jsonb, '[]'::jsonb, 'pending' FROM entries "
        "WHERE text = 'An on-call handover note.'", after_stop=True),
    # an existing digest cursor: ts, retry and context_deferred kept,
    # regenerate merged
    sql("INSERT INTO meta (key, value) VALUES ('session_digest_cursor', "
        "'{\"ts\": 5, \"retry\": {\"x\": 1}, \"regenerate\": [\"zz-root\"], "
        "\"context_deferred\": [\"q\"]}'::jsonb) ON CONFLICT (key) DO UPDATE "
        "SET value = EXCLUDED.value", after_stop=True),
    # replica rests on two entries forgotten together: the bank names the
    # larger source, the facts row the smaller, until the next cortex save
    trace_sql("Payments DB replica is db-2.", "payments-db", "replica"),
    trace_sql("Payments DB replica confirmed as db-2 by the DBA.", "payments-db", "replica"),
    # host rests on one entry; port on two, one of which survives
    trace_sql("Payments DB moved to host 10.0.0.9 per the runbook.", "payments-db", "host"),
    trace_sql("Payments DB moved to host 10.0.0.9 per the runbook.", "payments-db", "port"),
    trace_sql("Payments DB listens on 5432 per the on-call.", "payments-db", "port"),
    # one lesson rests on the backup entry alone, one on it and a survivor
    *lineage_lesson("schedule backups", "timing", "schedule-backups", "timing",
                    "Run backups at night.", None, ["The backup job runs at 02:00 nightly."],
                    "The backup job runs at 02:00 nightly."),
    *lineage_lesson("schedule backups", "checks", "schedule-backups", "checks",
                    "Check the deploy checklist after a backup.", "Payments DB",
                    ["The backup job runs at 02:00 nightly.", "A sess-h aside about lunch."],
                    "The backup job runs at 02:00 nightly."),
    # the dream's edge: agent origin, the flaky-test entry its evidence
    sql("UPDATE edges SET origin = 'agent' WHERE relation = 'uses'", after_stop=True),
    sql("INSERT INTO edge_evidence (edge_id, entry_id) SELECT g.id, e.id FROM edges g, "
        "entries e WHERE g.relation = 'uses' "
        "AND e.text = 'The flaky test needs a warm cache.'", after_stop=True),
]

SCENARIOS.update({
    "forget-memory": {
        "config": "memory:\n  delete_confirm_threshold: 2\n",
        "seed": FORGET_SEED,
        "ops": [
            # a filter is required; an empty string is no filter here
            forget("memory"),
            forget("memory", text=""),
            # nothing matches: the note (an empty text is still a filter below)
            forget("memory", text="no such entry"),
            forget("memory", text="", source="status"),
            # a source and its session's digest together: the digest is kept
            # as retired history, the session root queued for regeneration
            forget("memory", substring="deploy checklist"),
            # over the threshold: refused, nothing removed; then confirmed
            forget("memory", source="status"),
            forget("memory", source="status", confirm_bulk=True),
            # the tag filter normalizes
            forget("memory", tag=" SCRATCH "),
            # the cascade: host loses its only source and retires; port keeps one
            forget("memory", text="Payments DB moved to host 10.0.0.9 per the runbook."),
            fact_get("Payments DB", "host"),
            fact_get("Payments DB", "port"),
            # the lesson resting only on this entry retires, with its audit
            forget("memory", text="The backup job runs at 02:00 nightly."),
            lesson_search("when should backups run"),
            # the dream edge resting only on this entry retires
            forget("memory", text="The flaky test needs a warm cache."),
            # by episode id
            forget("memory", episode="@root:sess-h:32"),
            # a source with its session's retired digest: the digest is kept
            forget("memory", substring="handover"),
            # two sources of one slot together; then a cortex save writes the
            # retired slot (the larger source named) and the audit log
            forget("memory", episode="@root:sess-n:32"),
            fact("Payments DB", "owner", "the ops team", origin="user", confidence=0.9),
            # a digest-only selection deletes the digests
            forget("memory", source="digest"),
        ],
    },
    "forget-slots": {
        "config": None,
        "seed": [
            store("A note so the bank is not empty."),
            fact("Payments DB", "host", "10.0.0.5", origin="user", confidence=0.9),
            fact("Payments DB", "host", "10.0.0.6", origin="user", confidence=0.9),
            fact("Payments DB", "port", "5432", origin="user", confidence=0.9),
            fact("Old Router", "ip", "192.168.1.1", origin="user", confidence=0.9),
            fact_get("Payments DB", "host"),
            fact_get("Payments DB", "port"),
            world_set("Rust", "latest stable version", "1.90.0",
                      source_url="https://blog.rust-lang.org/"),
            world_set("Rust", "edition", "2024", source_url="https://doc.rust-lang.org/"),
            world_set("Python", "latest version", "3.13.0", source_url="https://www.python.org/"),
            *lineage_lesson("deploy engine to host", "approach", "deploy-engine-to-host",
                            "approach", "Deploy through the pipeline.", "Payments DB",
                            ["A note so the bank is not empty."],
                            "A note so the bank is not empty."),
            # a lesson whose task is a graph entity (fact_set minted it)
            *lineage_lesson("Payments DB", "tuning", "payments-db", "tuning",
                            "Tune the pool before the replica.", None,
                            ["A note so the bank is not empty."],
                            "A note so the bank is not empty."),
            *lineage_lesson("deploy engine to host", "pitfall", "deploy-engine-to-host",
                            "pitfall", "Never deploy on Fridays.", None,
                            ["A note so the bank is not empty."],
                            "A note so the bank is not empty."),
        ],
        "ops": [
            # the entity is required for these scopes
            forget("fact"),
            forget("world", entity=""),
            forget("fact", entity="Nobody"),
            # one slot, every version, its read counter too
            forget("fact", entity="payments db", attribute="HOST"),
            fact_get("Payments DB", "host"),
            # the whole entity
            forget("fact", entity="Old Router"),
            # world facts retire with an audit row; the slot is rewritten
            forget("world", entity="rust", attribute="Edition"),
            world_search("rust edition"),
            forget("world", entity="Rust"),
            forget("world", entity="Rust"),
            # lessons likewise; the rewritten row is linked to its entities
            forget("lesson", entity="Deploy Engine To Host", attribute="approach"),
            lesson_search("how do I deploy the engine"),
            forget("lesson", entity="deploy engine to host"),
            forget("lesson", entity="nothing here"),
            # the rewritten row links its subject entity
            forget("lesson", entity="Payments DB", attribute="tuning"),
        ],
    },
    # The bulk guard disabled: any match count is removed.
    "forget-unbounded": {
        "config": "memory:\n  delete_confirm_threshold: 0\n",
        "seed": [store(f"Status probe {n}.", source="status") for n in ("one", "two", "three")],
        "ops": [forget("memory", source="status")],
    },
})


# memory_consolidation_candidates (spec CC1-CC9), read-only, on templates
# whose entries (near-duplicates across sessions, a retired one, a tagged
# one) the Python daemon's own tools wrote.
def candidates(headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_consolidation_candidates", "args": kw,
            "headers": list(headers)}


STAGING = "staging database host"
SCENARIOS.update({
    "consolidation-candidates": {
        "config": None,
        "seed": [
            store("The staging database lives on host db-stage-1.", [sess("sess-c1")]),
            store("Staging DB host is db-stage-1.", [sess("sess-c2")]),
            store("db-stage-1 hosts the staging database.", [sess("sess-c3")], source="user"),
            store("Our CI runs on GitHub Actions.", [sess("sess-c1")]),
            store("Continuous integration uses GitHub Actions runners.", [sess("sess-c2")]),
            store("I like oat milk in my coffee.", [sess("sess-c1")]),
            store("Deploys happen on Tuesdays after the staging database check.",
                  [sess("sess-c1")], tags=["ops"]),
            store("The staging database used to be on db-stage-0.", [sess("sess-c1")]),
            # a twin text in sess-c1 (the episode walk offers one per text),
            # with another entry's vector: an exact score tie would leave the
            # retrieval order free (torch.topk; divergences.md)
            sql("INSERT INTO entries (band, text, embedding, surprise, ts, access_count, source, "
                "episode_id, episode_title, tags, slots, dream_state) SELECT a.band, a.text, "
                "b.embedding, a.surprise, a.ts + 1, 0, a.source, a.episode_id, a.episode_title, "
                "a.tags, a.slots, a.dream_state FROM entries a, entries b "
                "WHERE a.text = 'Our CI runs on GitHub Actions.' "
                "AND b.text = 'I like oat milk in my coffee.'", after_stop=True),
            supersede("The staging database moved off db-stage-0 to db-stage-1.",
                      old_text="The staging database used to be on db-stage-0."),
        ],
        "ops": [
            # no anchor (an empty query is none): nothing to cluster
            candidates(),
            candidates(query=""),
            # the query anchor: retrieval (access accrual), retired entries
            # excluded, clusters by cosine to the seed
            candidates(query=STAGING),
            candidates(query=STAGING, min_cohesion=0.95),
            candidates(query="continuous integration", sources=["agent"], top_k=5),
            candidates(query=STAGING, tags=[" OPS "]),
            candidates(query="what do we run", min_cluster_size=1, max_clusters=2),
            candidates(query=STAGING, min_cohesion="nan"),
            candidates(query=STAGING, episode="@root:sess-c2:32"),
            # empty lists and an empty episode are no filter
            candidates(query=STAGING, sources=[], tags=[], episode=""),
            # the episode anchor: its entries in band order, score 1.0
            candidates(episode="@root:sess-c1:32", min_cohesion=0.3),
            candidates(episode="@root:sess-c1:32", sources=["user"]),
            candidates(episode="@root:sess-c1:32", tags=["ops"], min_cluster_size=1),
            # the episode walk compares tags as stored: no normalization
            candidates(episode="@root:sess-c1:32", tags=[" OPS "], min_cluster_size=1),
            # a row gone behind the daemons' backs is filtered out
            external("DELETE FROM entries WHERE text = 'Staging DB host is db-stage-1.'"),
            candidates(query=STAGING),
            # the episode walk's candidates are checked too, before the cut
            candidates(episode="@root:sess-c2:32", top_k=1, min_cluster_size=1),
        ],
    },
})


# memory_consolidate (spec CN1-CN7): the correction body over lists of
# targets, on templates the Python daemon's own tools wrote. The duplicate
# row and the trace rows are SQL seeds, as for memory_supersede.
def consolidate(new_text, headers=(), **kw) -> dict:
    return {"op": "tool", "name": "memory_consolidate", "args": {"new_text": new_text, **kw},
            "headers": list(headers)}


STAGE = ["The staging cluster runs three nodes.",
         "Staging cluster has three nodes running.",
         "According to the runbook, the staging cluster runs 3 nodes."]
RESTART = ["Primary restarts happen after hours.",
           "Never restart the primary during business hours."]
SCENARIOS.update({
    "consolidate-branches": {
        "config": None,
        "seed": [
            store(STAGE[0], [sess("sess-k1")]),
            store(STAGE[1], [sess("sess-k2")]),
            store(STAGE[2], [sess("sess-k1")]),
            trace_sql(STAGE[0], "staging-cluster", "nodes"),
            trace_sql(STAGE[1], "staging-cluster", "nodes"),
            trace_sql(STAGE[1], "staging-cluster", "size"),
            store(RESTART[0]),
            store(RESTART[1]),
            store("The cache warms at boot."),
            store("Cache warming happens on boot."),
            store("Gamma retired note."),
            supersede("Gamma replacement note.", old_text="Gamma retired note."),
            store("Delta duplicated note."),
            store("Epsilon row to vanish."),
            store("Epsilon twin to vanish."),
            store("Zeta row retired behind the daemon's back."),
            store("Zeta partner note."),
            store("Eta first note retired behind the daemons' backs."),
            store("Eta second note retired behind the daemons' backs."),
            dict(sql("INSERT INTO entries (band, text, embedding, surprise, ts, access_count, "
                     "source, episode_id, episode_title, tags, slots, authority, "
                     "distortion_tolerance, dream_state) SELECT band, text, embedding, surprise, "
                     "ts + 1, 0, 'user', episode_id, episode_title, tags, slots, authority, "
                     "distortion_tolerance, dream_state FROM entries "
                     "WHERE text = 'Delta duplicated note.'"), after_stop=True),
        ],
        "ops": [
            # by ids, with a session header and tags to normalize: every target
            # retired at one stamp, the invalidations for each, the strictest
            # label (the third target's quoted), no derived-slot keys
            consolidate("The staging cluster runs three nodes in total.", [sess("sess-k1")],
                        entry_ids=[f"@id:{t}" for t in STAGE],
                        tags=[" Consolidated ", "ops", "consolidated", "", "OPS"]),
            # by exact texts, repeated: one retirement each; the caller's
            # source; the second target's constraint carries over
            consolidate("Primary restarts are scheduled outside business hours.",
                        replaces=[RESTART[0], RESTART[1], RESTART[0]], source="user"),
            # a repeated id; an empty source takes the default
            consolidate("The cache warms when the service boots.",
                        entry_ids=["@id:The cache warms at boot.", "@id:The cache warms at boot.",
                                   "@id:Cache warming happens on boot."], source=""),
            # refusals: nothing changes
            consolidate("x", replaces=["Zeta partner note."], entry_ids=[1]),
            consolidate("x"),
            consolidate("x", replaces=[]),
            consolidate("x", entry_ids=[]),
            consolidate("x", entry_ids=["@id:Zeta partner note.", 0]),
            consolidate("x", entry_ids=[-4, "@id:Zeta partner note."]),
            consolidate("x", replaces=["Zeta partner note.", "  "]),
            consolidate("   ", replaces=["Zeta partner note."]),
            consolidate("   "),
            # every target's error, the first one's reason; a good target in
            # the list changes nothing
            consolidate("x", replaces=["no such note", "Zeta partner note.", "Gamma retired note.",
                                       "Delta duplicated note.", "no such note"]),
            consolidate("x", entry_ids=["@id:Zeta partner note.", "@id:Gamma retired note.", 999999,
                                        99999999999999999999]),
            consolidate("x", replaces=["Delta duplicated note.", "Gamma retired note."]),
            # rows deleted behind the daemons' backs: each one reported
            external("DELETE FROM entries WHERE text IN "
                     "('Epsilon row to vanish.', 'Epsilon twin to vanish.')"),
            consolidate("x", replaces=["Zeta partner note.", "Epsilon twin to vanish.",
                                       "Epsilon row to vanish."]),
            # one target retired behind the daemons' backs: supersede_entries
            # refuses the whole list
            external("UPDATE entries SET superseded_at = 1700000000.5, "
                     "superseded_by_text = 'elsewhere' "
                     "WHERE text = 'Zeta row retired behind the daemon''s back.'"),
            consolidate("x", replaces=["Zeta partner note.",
                                       "Zeta row retired behind the daemon's back."]),
            # two retired, listed against id order: already_retired is sorted
            external("UPDATE entries SET superseded_at = 1700000001.5, "
                     "superseded_by_text = 'elsewhere' WHERE text LIKE 'Eta % note retired%'"),
            consolidate("x", replaces=["Eta second note retired behind the daemons' backs.",
                                       "Eta first note retired behind the daemons' backs."]),
        ],
    },
    # Capacity: the victim is chosen with every target retired. The first
    # target is a system prompt (heavily weighted even retired), the second
    # an agent note: retired together, the agent note is the cheapest entry;
    # with only the first retired, the thinking trace would be.
    "consolidate-capacity": {
        "config": "memory:\n  miras:\n    preset: custom\n    bands:\n      - name: flat\n        max_entries: 5\n",
        "seed": [
            store("System prompt one, retained.", source="system"),
            store("System prompt two, retained.", source="system"),
            store("System prompt three, retained.", source="system"),
            store("Agent note about the release checklist."),
            store("Thinking trace about the index rebuild.", source="llm_thinking"),
        ],
        "ops": [
            consolidate("System prompt one and the release checklist, consolidated.",
                        entry_ids=["@id:System prompt one, retained.",
                                   "@id:Agent note about the release checklist."]),
        ],
    },
    # The meta filter refuses the replacement: the whole consolidation rolls
    # back, every target's invalidation included, and the targets stay live.
    "consolidate-meta-filter": {
        "config": "memory:\n  meta_filter:\n    enabled: true\n",
        "seed": [
            store("The on-call rotation changes on Mondays."),
            store("On-call rotation switches every Monday."),
            store("The pager escalates after ten minutes."),
            store("Pager escalation happens after 10 minutes."),
            trace_sql("The on-call rotation changes on Mondays.", "on-call", "rotation"),
            trace_sql("On-call rotation switches every Monday.", "on-call", "rotation"),
        ],
        "ops": [
            consolidate("No relevant memories about the rotation.",
                        replaces=["The on-call rotation changes on Mondays.",
                                  "On-call rotation switches every Monday."]),
            consolidate("The on-call rotation changes every Monday.",
                        replaces=["The on-call rotation changes on Mondays.",
                                  "On-call rotation switches every Monday."]),
            # the filter judges with the caller's source as the role: the same
            # kind of text is stored with source "user"
            consolidate("No relevant memories about the pager.", source="user",
                        replaces=["The pager escalates after ten minutes.",
                                  "Pager escalation happens after 10 minutes."]),
        ],
    },
})


def relate(src, relation, dst, **kw) -> dict:
    return tool("memory_graph_relate", src=src, relation=relation, dst=dst, **kw)


def unrelate(src, relation, dst) -> dict:
    return tool("memory_graph_unrelate", src=src, relation=relation, dst=dst)


def alias(entity, name) -> dict:
    return tool("memory_alias", entity=entity, alias=name)


def define(name, description, **kw) -> dict:
    return {"op": "tool", "name": "memory_relation_define",
            "args": {"name": name, "description": description, **kw}, "headers": []}


SCENARIOS.update({
    "graph-writes": {
        "config": None,
        "seed": [
            # fact_set mints the subject entities; the Kettle fact's value
            # names no entity yet, so its object link is empty
            fact("Postgres", "port", "5432"),
            fact("Kettle", "brand", "Breville"),
            fact("Redis", "role", "cache"),
            # a current fact whose owner node is gone: the fold mints it
            fact("Nginx", "port", "80"),
            sql("UPDATE facts SET entity_id = NULL WHERE entity = 'Nginx'; "
                "DELETE FROM entities WHERE canonical = 'nginx'", after_stop=True),
        ],
        "ops": [
            # an unknown relation: suggestions, nothing minted
            relate("pgbouncer", "dependz-on", "Postgres"),
            relate("pgbouncer", "zzzz", "Postgres"),
            # a fresh source, an existing target
            relate("PgBouncer", "depends-on", "Postgres"),
            # the slot-key fold: "postgres.port" is the Postgres port slot
            relate("postgres.port", "related-to", "Kettle"),
            # a typed fold: the owner is backfilled; an owner without a node
            # is minted under its own name and type, relinking its fact
            relate("postgres.port", "uses", "Kettle", src_type="database"),
            relate("nginx.port", "related-to", "Kettle", src_type="proxy"),
            # minting Breville relinks the Kettle fact's object; a later
            # cortex save must keep the link
            relate("Breville", "related-to", "Kettle"),
            fact("Kettle", "colour", "red"),
            # re-assertion: the confidence bump, capped, and the origin rank
            relate("PgBouncer", "depends-on", "Postgres", confidence=0.5),
            relate("PgBouncer", "depends-on", "Postgres", confidence=0.97, origin="user"),
            relate("PgBouncer", "depends-on", "Postgres", origin="agent"),
            relate("Redis", "uses", "Postgres", confidence=0.123456, origin="action"),
            relate("Redis", "uses", "Kettle", confidence=1.0),
            relate("Redis", "uses", "Breville", confidence=0),
            # types: a fresh node takes its type, an untyped one is
            # backfilled, a typed one keeps its own
            relate("Memcached", "uses", "Postgres", src_type="cache", dst_type="database"),
            relate("Memcached", "uses", "Postgres", src_type="store", dst_type="other"),
            # an empty name, a self-loop, a final sigma
            relate("   ", "related-to", "Kettle"),
            relate("Kettle", "related-to", "kettle"),
            relate("\u039f\u0394\u03a5\u03a3\u03a3\u0395\u03a5\u03a3", "related-to", "Kettle"),
            # unrelate: unknown relation (no hint), unknown entities
            # (the source is named first), then a live edge twice
            unrelate("PgBouncer", "dependz-on", "Postgres"),
            unrelate("Nobody", "depends-on", "Postgres"),
            unrelate("PgBouncer", "depends-on", "Nobody"),
            unrelate("Nobody", "depends-on", "Nothing"),
            unrelate("pgbouncer", "Depends On", "postgres"),
            unrelate("pgbouncer", "depends-on", "postgres"),
            # a superseded edge revives on re-assertion
            relate("PgBouncer", "depends-on", "Postgres"),
            # aliases: empty, alias of an alias, the canonical refused (a
            # node minted on the way stays), reassignment, a slot-key owner,
            # an alias that is another node's canonical
            alias("Postgres", "  "),
            alias("Postgres", "pg"),
            alias("pg", "PostgreSQL"),
            alias("Postgres", "POSTGRES"),
            alias("New Thing", "new_thing"),
            alias("Redis", "pg"),
            alias("postgres.port", "pgsql"),
            alias("Redis", "kettle"),
            # unrelate and relate through aliases
            relate("pgsql", "stores-data-in", "Redis"),
            unrelate("PostgreSQL", "stores-data-in", "pg"),
        ],
    },
    "graph-relations": {
        "config": None,
        "seed": [
            fact("Postgres", "port", "5432"),
            fact("Kettle", "brand", "Breville"),
        ],
        "ops": [
            # refusals: no name, no description, a builtin, an unknown
            # inverse (the known list sorted), a blank inverse; punctuation
            # survives normalization, so "!!" is a name
            define("", "a relation"),
            define("!!", "a relation"),
            define("serves", "   "),
            define("Related To", "redefined"),
            define("hosted-on", "src runs on host dst", inverse_of="hosts-of"),
            define("mirrors", "src mirrors dst", inverse_of="   "),
            # code-point order differs from a collation's for these two
            define("ab-z", "a relation"),
            define("abc", "a relation"),
            define("cycles-with", "src cycles with dst", inverse_of="nope"),
            # definitions: types, an inverse, a self-inverse, an empty
            # inverse (none), a redefinition overwriting
            define("Hosted On", "  src runs on host dst  ", src_type="service",
                   dst_type="host"),
            define("hosts-service", "src hosts dst", inverse_of="hosted_on"),
            define("mirrors", "src mirrors dst", inverse_of="mirrors", transitive=True),
            define("peers-with", "src peers with dst", inverse_of=""),
            define("hosted-on", "src is deployed to host dst", src_type="service",
                   dst_type="host", transitive=True),
            # typed relations warn on a mismatch and store anyway
            relate("Redis", "hosted-on", "Postgres", src_type="service", dst_type="database"),
            relate("Redis", "hosted-on", "box-1", dst_type="host"),
            relate("Kettle", "Hosted_On", "box-1"),
            # an empty-string type is stored and stays: a later hint is
            # answered but not backfilled
            relate("Toaster", "related-to", "Kettle", src_type=""),
            relate("Toaster", "hosted-on", "Kettle", src_type="appliance"),
            relate("Toaster", "hosted-on", "Kettle"),
            # the new names resolve in relate and unrelate
            relate("Postgres", "mirrors", "Redis"),
            relate("box-1", "hosts-service", "Redis"),
            unrelate("Redis", "hosted-on", "box-1"),
            relate("Postgres", "hosted on", "box-2"),
        ],
    },
})


def graph_read(entity, **kw) -> dict:
    return tool("memory_graph", entity=entity, **kw)


SCENARIOS.update({
    "graph-read": {
        "config": None,
        "seed": [
            fact("Postgres", "port", "5432", authority="directive",
                 distortion_tolerance="constraint"),
            fact("Web App", "framework", "Django"),
            relate("Web App", "runs-on", "host-1", origin="user"),
            relate("host-2", "hosts", "Web App"),
            relate("Web App", "depends-on", "Postgres", origin="action"),
            relate("Postgres", "depends-on", "Disk", confidence=0.9),
            relate("Cache", "uses", "Web App", origin="agent"),
            relate("Crawler", "related-to", "Web App", confidence=0.3),
            relate("Spider", "related-to", "Crawler"),
            relate("Moth", "related-to", "Spider"),
            relate("Island", "related-to", "Atoll"),
            # a transitive relation with an inverse: mirrors of asserted
            # edges (hash order) and of a transitive one (exact)
            define("inside", "src is inside dst", transitive=True),
            define("encloses", "src encloses dst", inverse_of="inside"),
            relate("Web App", "inside", "Pod"),
            relate("Pod", "inside", "Node"),
            alias("Postgres", "pg"),
            # a node without edges, and an alias that is another node's
            # canonical (dropped from the alias map)
            alias("Lonely", "lonely-one"),
            alias("Disk", "web-app"),
            # a fact written under the alias lands on the node
            fact("pg", "version", "16"),
            # an overwritten fact: only the current one is projected
            fact("Web App", "framework", "Flask"),
            # what only the dream writes: communities and source attribution
            sql("INSERT INTO entity_communities (entity_id, community_id, computed_at) "
                "SELECT id, CASE WHEN canonical IN ('island', 'atoll') THEN 2 ELSE 1 END, 0 "
                "FROM entities WHERE canonical IN ('web-app', 'postgres', 'disk', 'island', "
                "'atoll')", after_stop=True),
            sql("INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
                "SELECT id, s, 1, 'derived', 0 FROM entities, "
                "(VALUES ('claude-code'), ('agent')) v(s) WHERE canonical IN ('web-app', "
                "'postgres')", after_stop=True),
        ],
        "ops": [
            # the default neighborhood: asserted edges with tags, a transitive
            # and inverse-derived edges, facts with labels and the alias fact
            graph_read("web app"),
            graph_read("Web App", depth=2),
            graph_read("Web App", depth=3),
            graph_read("Web App", depth=0),
            graph_read("Web App", depth=9),
            graph_read("Web App", depth=10 ** 20),
            graph_read("Web App", depth=-(10 ** 20)),
            graph_read("Web App", include_facts=False),
            # through an alias, and from a leaf
            graph_read("pg", depth=2),
            graph_read("Moth", depth=3),
            # the path: found, to the root, an unknown target, an empty one,
            # and a target in another component
            graph_read("Moth", to="Disk"),
            graph_read("Web App", to="web-app"),
            graph_read("Web App", to="Nobody"),
            graph_read("Web App", to=""),
            graph_read("Web App", to="Atoll"),
            graph_read("Web App", to="Lonely"),
            graph_read("Pod", to="Node", depth=2),
            # the relation filter: a case-folded substring, empty (no filter),
            # one that empties the edges
            graph_read("Web App", relation_filter="DEPENDS"),
            graph_read("Web App", relation_filter=""),
            graph_read("Web App", relation_filter="zzz"),
            graph_read("Nobody", relation_filter="uses"),
            # not found: unknown, blank after normalization
            graph_read("Nobody"),
            graph_read("   "),
            # an empty entity is the whole graph, with sources
            graph_read(""),
            graph_read("", include_facts=False, relation_filter="relat"),
            # the answers follow writes: a superseded edge leaves, a new
            # alias resolves, a revived edge returns
            unrelate("Web App", "depends-on", "Postgres"),
            graph_read("Web App"),
            alias("Web App", "frontend"),
            relate("Web App", "depends-on", "Postgres"),
            graph_read("frontend", to="Disk"),
        ],
    },
})


def recall(query, **kw) -> dict:
    return tool("memory_recall", query=query, **kw)


RECALL_SEED = [
    store("The web app runs on host-1 in the main rack."),
    store("Host-1 is a Dell server bought in 2021."),
    store("Postgres stores the web app's sessions."),
    store("The cache layer uses Redis on host-2."),
    store("Disk failures on host-1 were fixed last spring."),
    store("I like oat milk in my coffee."),
    store("Host-1 vendor is Dell per the asset sheet."),
    fact("Postgres", "port", "5432", distortion_tolerance="constraint"),
    fact("Postgres", "version", "16"),
    fact("Web App", "framework", "Django"),
    fact("Host-1", "vendor", "Dell"),
    # no asserted inverse pair (runs-on / hosts): Python orders their
    # mirrors by string hash, and recall's edges carry no provenance to
    # tell them apart (divergences.md, memory_recall)
    relate("Web App", "uses", "host-1"),
    relate("Web App", "depends-on", "Postgres"),
    relate("Postgres", "depends-on", "host-1"),
    relate("Web App", "uses", "Cache"),
    relate("Cache", "uses", "Redis"),
    relate("Redis", "stores-data-in", "host-2"),
    relate("host-1", "part-of", "Rack A"),
    relate("host-2", "part-of", "Rack A"),
    relate("Rack A", "part-of", "Row 3"),
    alias("Postgres", "pg"),
    # the vendor fact rests on a memory corrected after it was confirmed
    trace_sql("Host-1 vendor is Dell per the asset sheet.", "host-1", "vendor"),
    supersede("Host-1 vendor is Lenovo per the new asset sheet.",
              old_text="Host-1 vendor is Dell per the asset sheet."),
]

SCENARIOS.update({
    "recall": {
        "config": None,
        "seed": RECALL_SEED,
        "ops": [
            # blank: the canned answer, no search
            recall(""),
            recall("   "),
            # the walk from the query's own entities, compact and verbose
            recall("What does the web app run on?"),
            recall("What does the web app run on?", verbose=True),
            # hop and seed-width clamps
            recall("web app", hops=1),
            recall("web app", hops=9),
            recall("web app", hops=-3),
            recall("web app", top_k=1),
            recall("web app", top_k=10 ** 20),
            # memory_search logs an over-range top_k as given too
            search("web app", top_k=10 ** 20),
            # a seed's constraint fact pinned first; an alias seed
            recall("postgres settings", verbose=True),
            recall("tell me about pg", verbose=True),
            # a fact whose source was corrected carries re_verify
            recall("host-1 vendor", verbose=True),
            recall("host-1 vendor"),
            # no entity in the query: the seed hits name one, or none
            recall("Which server was bought in 2021?"),
            recall("oat milk"),
            # the walk follows writes
            relate("Redis", "depends-on", "Disk"),
            recall("What does the cache use?", verbose=True),
        ],
    },
    "recall-wide": {
        "config": None,
        "seed": [
            store("The switch hub links every rack leaf in the hall."),
            *[relate("Switch Hub", "uses", f"Leaf {i:02d}") for i in range(12)],
            relate("Leaf 00", "depends-on", "Deep Store"),
            fact("Deep Store", "tier", "cold"),
        ],
        "ops": [
            # 14 entities over three hops: the entity cap keeps a slot for
            # the deepest hop, which a flat prefix would drop
            recall("what hangs off the switch hub?", hops=3),
            recall("what hangs off the switch hub?", hops=3, verbose=True),
        ],
    },
    "recall-caps": {
        "config": ("memory:\n  recall:\n    hub_floor: 2\n    hub_percentile: 50\n"
                   "    expand_budget: 1\n    max_searches_per_hop: 1\n"
                   "    max_total_searches: 2\n    skip_part_of_expansion: true\n"
                   "    max_entities: 6\n"),
        "seed": RECALL_SEED,
        "ops": [
            # the hub gate, the expansion budget, the per-hop and total
            # search ceilings (truncated), part-of arrivals not re-queried,
            # the entity ceiling
            recall("What does the web app run on?", verbose=True),
            recall("web app and host-1 and redis", hops=5),
            recall("rack a", verbose=True),
        ],
    },
    "recall-ceiling": {
        "config": "memory:\n  recall:\n    max_total_searches: 2\n",
        "seed": RECALL_SEED,
        "ops": [
            # the seed search and one re-query, then the ceiling: truncated,
            # with searches_issued
            recall("What does the web app run on?", verbose=True),
            recall("What does the web app run on?"),
        ],
    },
    "recall-ungated": {
        "config": ("memory:\n  recall:\n    hub_gate: false\n    max_searches_per_hop: 0\n"
                   "    max_total_searches: 0\n    time_budget_seconds: 0\n"
                   "  cortex:\n    pin_constraints: false\n  traces:\n    enabled: false\n"),
        "seed": RECALL_SEED,
        "ops": [
            # no gate, no ceilings, no pin, no trace annotation
            recall("What does the web app run on?", verbose=True),
            recall("postgres settings", verbose=True),
            recall("host-1 vendor", verbose=True),
        ],
    },
})


def review(action="list", **kw) -> dict:
    return tool("memory_graph_review", action=action, **kw)


def eid(canonical: str) -> str:
    return f"(SELECT id FROM entities WHERE canonical = '{canonical}')"


REVIEW_SEED = [
    # duplicates: a Jaccard 2/3 pair, a 0.6 pair a numeric substitution
    # vetoes, a 0.5 pair below the threshold, a pair dismissed by hand
    relate("payments ledger", "related-to", "Warehouse"),
    relate("payments ledger prod", "related-to", "Warehouse"),
    # an unfiled pair, a pair whose merge was decided, a file and its stem
    relate("orbit station alpha", "related-to", "Ocean"),
    relate("orbit station alpha two", "related-to", "Ocean"),
    relate("harbor crane main", "related-to", "Ocean"),
    relate("harbor crane main backup", "related-to", "Ocean"),
    relate("tidecalc.py", "related-to", "Ocean"),
    relate("tidecalc", "related-to", "Ocean"),
    relate("alpha beta gamma ct200", "related-to", "Warehouse"),
    relate("alpha beta gamma ct400", "related-to", "Warehouse"),
    relate("red green blue", "related-to", "Warehouse"),
    relate("red green yellow", "related-to", "Warehouse"),
    relate("blue whale song", "related-to", "Ocean"),
    relate("blue whale songs", "related-to", "Ocean"),
    review("dismiss_pair", src="blue whale song", dst="blue whale songs"),
    # test artifacts, a dubious edge, an orphan
    relate("deploy-smoke run", "uses", "Warehouse"),
    relate("noise agent", "uses", "Ocean", confidence=0.3, origin="agent"),
    relate("Warehouse", "uses", "Ocean", confidence=0.0, origin="agent"),
    alias("Lonely Node", "lonely-node-alias"),
    # a lesson-minted pair (never unattributed)
    outcome("ship the release", "success", about="release checklist"),
    # source attribution for some nodes (only the dream writes it)
    sql("INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
        "SELECT id, 'claude-code', 1, 'derived', 0 FROM entities "
        "WHERE canonical IN ('warehouse', 'payments-ledger', 'ocean')", after_stop=True),
    sql("INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, status, "
        f"created_at) VALUES ('merge', {eid('harbor-crane-main')}, {eid('harbor-crane-main-backup')}, "
        "0.75, 'write-dedup: d', 'rejected', 1)", after_stop=True),
    # proposed links through the oracle's own tool (an unknown relation
    # falls back to related-to; a self-pair and an empty side are skipped)
    review("propose", proposals=[
        {"src": "Warehouse", "relation": "depends-on", "dst": "Ocean", "similarity": 0.8,
         "rationale": "shared tide tables"},
        {"src": "red green blue", "relation": "colour-of", "dst": "Ocean"},
        {"src": "Warehouse", "relation": "uses", "dst": "warehouse"},
        {"src": "", "relation": "uses", "dst": "Ocean"},
    ]),
    # merge and junk proposals with judge votes (only the dream files them):
    # one whose stored direction folds the busier node away, a relate
    # verdict, a three-vote row, a second row on a shared node, a junk row
    sql("INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, created_at, "
        "judge_verdict, judge_confidence, judge_note, judge_model, judged_at) VALUES "
        f"('merge', {eid('warehouse')}, {eid('red-green-yellow')}, 0.7, 'write-dedup: a', 1, "
        "'accept', 0.9, 'same thing', 'judge-a', 2), "
        f"('merge', {eid('red-green-blue')}, {eid('red-green-yellow')}, 0.5, 'write-dedup: b', 1, "
        "'relate', 0.8, 'relate:related-to distinct colours', 'judge-a', 2)",
        after_stop=True),
    sql("INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, created_at, "
        "judge_verdict, judge_confidence, judge_note, judge_model, judged_at, "
        "judge2_verdict, judge2_confidence, judge2_model, judged2_at, "
        "judge3_verdict, judge3_confidence, judge3_model, judged3_at) VALUES "
        f"('merge', {eid('payments-ledger')}, {eid('payments-ledger-prod')}, 0.6667, 'analyzer-duplicate', 1, "
        "'accept', 0.7, 'looks same | 2nd (judge-b): reject 0.6 [shadow] | 3rd (judge-c): accept 0.9',"
        " 'judge-a', 2, 'reject', 0.6, 'judge-b', 3, 'accept', 0.9, 'judge-c', 4)",
        after_stop=True),
    sql("INSERT INTO entity_proposals (kind, entity_id, score, reason, created_at) VALUES "
        f"('junk', {eid('noise-agent')}, 0.9, 'smoke-test residue', 1)", after_stop=True),
    # a recent automatic decision in the audit index (the judges write it)
    sql("INSERT INTO meta (key, value) VALUES ('automatic_review_decision_v1:d1', "
        "'{\"version\": 1, \"decision_id\": \"d1\", \"queue\": \"merge\", \"action\": \"reject\", "
        "\"state\": \"applied\", \"proposal_id\": 7, \"pair\": [\"a\", \"b\"], \"recorded_at\": 10.5, "
        "\"ended_at\": null, \"end_reason\": null, \"secret\": \"not projected\"}') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", after_stop=True),
    sql("UPDATE meta SET value = jsonb_set(value, '{recent_ids}', '[\"gone\", \"d1\"]') "
        "WHERE key = 'automatic_review_decisions_v1'", after_stop=True),
    # an analyzer link proposal (its settlement closes the pair) and a filed
    # relate proposal for the file/stem duplicate; explicit ids leave the
    # sequence to the oracle's own proposals
    sql("INSERT INTO edge_proposals (id, src_id, relation, dst_id, confidence, source, "
        f"created_at) VALUES (500, {eid('red-green-blue')}, 'uses', {eid('harbor-crane-main')}, "
        f"0.5, 'analyzer', 1), (501, {eid('tidecalc-py')}, 'implements', {eid('tidecalc')}, "
        "0.7, 'deep-dream', 1)", after_stop=True),
    # an earlier human decision in the merge audit
    sql("INSERT INTO merge_decisions (proposal_id, entity_display, into_display, status, score, "
        "reason, decided_by, decided_at) VALUES (99, 'Old A', 'Old B', 'accepted', 0.8, 'x', "
        "'agent', 5), (98, 'Old C', NULL, 'rejected', NULL, 'junk keep: y', 'dream-judge', 6)",
        after_stop=True),
]

# memory_graph_review's mutating actions (promoted from the live smoke the
# implementation ran): every action, the batch form and each refusal.
REVIEW_ACTIVE = (
    "UPDATE meta SET value = jsonb_set(jsonb_set(value, '{active}', "
    "'{\"merge:3\": {\"decision_id\": \"d1\", \"seq\": 1, \"queue\": \"merge\", "
    "\"pair\": [\"a\", \"b\"]}, \"candidate:9\": {\"decision_id\": \"d2\", \"seq\": 2, "
    "\"queue\": \"candidate\", \"pair\": [\"orbit-station-alpha\", "
    "\"orbit-station-alpha-two\"]}, \"link:4\": {\"decision_id\": \"d3\", \"seq\": 3, "
    "\"queue\": \"link\", \"pair\": [\"x\", \"y\"]}}'), '{next_seq}', '4.5') "
    "WHERE key = 'automatic_review_decisions_v1'"
)
REVIEW_JUNK = (
    "INSERT INTO entity_proposals (kind, entity_id, score, reason, created_at, "
    "judge_verdict, judge_confidence, judge_model, judge_served_model) "
    "SELECT 'junk', id, 0.8, NULL, 9, 'delete', 0.95, 'judge-a', 'served-a' "
    "FROM entities WHERE canonical = 'deploy-smoke-run'"
)
REVIEW_OPS = [
    review("propose", proposals=[
        {"src": "Tide Gauge", "relation": "Runs On", "dst": "Harbor Host", "similarity": 0.3},
        {"src": "llama-server", "relation": "runs-on", "dst": "postgres"},
        {"src": None, "relation": "uses", "dst": "Ocean"},
        {"src": "Warehouse", "relation": "depends-on", "dst": "Ocean"},
        {"src": "Kelp Farm", "relation": "prefers", "dst": "Ocean", "similarity": "0.25",
         "rationale": 7},
        {"src": "Kelp Farm", "relation": "uses", "dst": "Ocean", "rationale": 1e16},
    ]),
    review("propose"),
    review("propose", proposals=[]),
    external(REVIEW_ACTIVE),
    review("relate", src="tidecalc.py", relation="implements", dst="tidecalc"),
    review("relate", src="a", relation="bogus-rel", dst="b"),
    review("relate", src="x"),
    review("dismiss_pair", src="orbit station alpha", dst="ORBIT STATION ALPHA TWO"),
    review("dismiss_pair", src="orbit station alpha", dst="orbit station alpha"),
    review("dismiss_pair", src="lonely-node-alias", dst="Lonely Node"),
    review("dismiss_pair", src="  ", dst="Lonely Node"),
    review("dismiss_pair", src="a"),
    review("accept_link", proposal_id=1),
    review("accept_link", proposal_ids=[2, 2, 999]),
    review("reject_link", proposal_id=1),
    review("reject_link", proposal_id=4),
    review("reject_link", proposal_id=12345678901234567890),
    review("reject_entity", proposal_id=3),
    review("reject_entity", proposal_id=5),
    review("reject_entity", proposal_id=1),
    external(REVIEW_JUNK),
    review("accept_junk", proposal_id=5),
    review("accept_junk", proposal_id=6),
    review("accept_merge", proposal_id=2),
    review("accept_merge", proposal_ids="[4, 3]"),
    review("accept_link"),
    review("accept_merge", proposal_ids=[], proposal_id=None),
    external("DELETE FROM meta WHERE key = 'automatic_review_decisions_v1'"),
    review("dismiss_pair", src="harbor crane main", dst="tidecalc"),
    # the analyzer row's settlement dismisses its pair; the filed relate
    # proposal, decided, makes its duplicate terminal
    review("accept_link", proposal_id=500),
    review("reject_link", proposal_id=501),
    review(),
]

SCENARIOS.update({
    "review-list": {
        "config": None,
        "seed": REVIEW_SEED,
        "ops": [
            # every finding type, the automation states, the audits
            review(),
            review("list"),
            # a scope narrows entities and edges; queued proposals stay
            review("list", scope="claude-code"),
            review("list", scope="all"),
            review("list", scope="nobody"),
            # the answer follows writes: a new dubious edge and alias, then a
            # proposal the dream files behind the daemons' backs
            relate("Ocean", "related-to", "Lonely Node", confidence=0.2, origin="agent"),
            alias("Ocean", "sea"),
            external("INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, "
                     "created_at) SELECT 'merge', a.id, b.id, 0.61, 'write-dedup: c', 7 FROM "
                     "entities a, entities b WHERE a.canonical = 'alpha-beta-gamma-ct200' AND "
                     "b.canonical = 'alpha-beta-gamma-ct400'"),
            review(),
        ],
    },
    "review-actions": {
        "config": None,
        "seed": REVIEW_SEED,
        "ops": REVIEW_OPS,
    },
    "review-list-gated": {
        "config": "memory:\n  deep_dream:\n    judges_enabled: false\n",
        "seed": REVIEW_SEED,
        "ops": [review()],
    },
    "review-list-no-analyzer": {
        "config": "memory:\n  deep_dream:\n    analyzer_file_duplicates: false\n",
        "seed": REVIEW_SEED,
        "ops": [review()],
    },
})


# memory_graph_review's slot actions: dismiss_slot_pair and restore_slot
# through every branch and refusal. World facts come from
# memory_world_set; the seeding daemon's own memory_forget retires the ones
# a compaction purge (an after-stop DELETE of the retired row) leaves only
# as an audit snapshot. Lessons are SQL seeds (only the dream writes
# lessons), with fixed stamps and, but for one pair, no embedding, and so
# are the curated duplicate retirements (only the dream's curation writes
# them): their evidence is curation_safety.snapshot_record's shape over
# those rows (an embedding as its float32 digest), fingerprinted as
# evidence_fingerprint fingerprints a plain dict.
def slot_norm(s: str) -> str:
    """``cortex._norm_key`` for the ASCII names below."""
    return re.sub(r"[\s._\-/]+", "-", s.strip().casefold()).strip("-")


def evidence_fp(record: dict) -> str:
    """``curation_safety.evidence_fingerprint`` of a plain JSON dict."""
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False)
                          .encode("utf-8")).hexdigest()


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, dict)):
        return "'" + json.dumps(v).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


SLOT_STAMP = 1700000000.0
_SLOT_COMMON = ("entity", "attribute", "value", "polarity", "status", "confidence", "origin",
                "support", "provenance", "asserted_at", "last_confirmed", "supersedes_value",
                "superseded_by_value", "superseded_at", "tx_time", "valid_time", "hlc_phys",
                "hlc_logical", "writer_id", "session_id", "version")


def slot_embedding(seed: int) -> list[float]:
    """A 1024-dim vector (the schema's ``vector(1024)``) whose components
    are sixteenths, exact in float32, so pgvector, numpy and the daemon all
    hold the same bytes."""
    return [float(((i * seed) % 17) - 8) / 16 for i in range(1024)]


def embedding_digest(values: list[float] | None) -> dict | None:
    """``curation_safety._embedding_snapshot`` of a float32 vector."""
    if values is None:
        return None
    return {"dtype": "float32", "shape": [len(values)],
            "sha256": hashlib.sha256(struct.pack(f"<{len(values)}f", *values)).hexdigest()}


def slot_lesson(task, aspect, lesson, *, status="current", about=None, polarity="+",
                asserted=SLOT_STAMP, superseded_at=None, hlc_logical=0,
                embedding=None) -> dict:
    """A lessons row (its columns; ``embedding`` only when given)."""
    extra = {} if embedding is None else {"embedding": embedding}
    return {**extra, "entity": task, "attribute": aspect, "value": lesson, "polarity": polarity,
            "status": status, "confidence": 0.8, "origin": "agent", "support": [],
            "provenance": ["signal:1"], "asserted_at": asserted, "last_confirmed": asserted,
            "supersedes_value": None, "superseded_by_value": None,
            "superseded_at": superseded_at, "tx_time": asserted, "valid_time": asserted,
            "hlc_phys": int(asserted * 1000), "hlc_logical": hlc_logical, "writer_id": "seed",
            "session_id": None, "version": 1, "about": about, "outcome": "success"}


def slot_world(entity, attribute, value, *, status="current", asserted=SLOT_STAMP,
               superseded_at=None, hlc_logical=0, embedding=None) -> dict:
    """A world_facts row (its columns; ``embedding`` only when given)."""
    extra = {} if embedding is None else {"embedding": embedding}
    return {**extra, "entity": entity, "attribute": attribute, "value": value, "polarity": "+",
            "status": status, "confidence": 0.9, "origin": "source", "support": ["source"],
            "provenance": [], "asserted_at": asserted, "last_confirmed": asserted,
            "supersedes_value": None, "superseded_by_value": None,
            "superseded_at": superseded_at, "tx_time": asserted, "valid_time": asserted,
            "hlc_phys": int(asserted * 1000), "hlc_logical": hlc_logical, "writer_id": "seed",
            "session_id": None, "version": 1, "source_url": "https://example.org/host",
            "source_quote": value, "retrieved_at": SLOT_STAMP, "freshness_class": "evergreen",
            "content_hash": None, "source_doc_id": None}


def slot_row_sql(store: str, row: dict) -> dict:
    """Seed-only: the row, after the seeding daemon stopped (its exit flush
    rewrites the slots it holds)."""
    table = "lessons" if store == "lesson" else "world_facts"
    cols = dict(row, entity_norm=slot_norm(row["entity"]),
                attribute_norm=slot_norm(row["attribute"]))
    values = [("'[" + ",".join(repr(x) for x in v) + "]'::vector") if k == "embedding"
              else _lit(v) for k, v in cols.items()]
    return sql(f"INSERT INTO {table} ({', '.join(cols)}) "
               f"VALUES ({', '.join(values)})", after_stop=True)


def slot_snapshot(store: str, row: dict, **changes) -> dict:
    """``snapshot_record(store, record)`` of the row as hydration holds it."""
    row = dict(row, **changes)
    en, an = slot_norm(row["entity"]), slot_norm(row["attribute"])
    snap = {"store": store, "key": f"{en}|{an}", "entity_norm": en, "attribute_norm": an}
    snap.update({k: row[k] for k in _SLOT_COMMON})
    snap.update({k: row[k] for k in (("about", "outcome") if store == "lesson" else
                                     ("source_url", "source_quote", "retrieved_at",
                                      "freshness_class", "content_hash", "source_doc_id"))})
    snap["embedding"] = embedding_digest(row.get("embedding"))
    return snap


def curated_retire(store: str, survivor: dict, loser: dict, *, keep="a", slot=None,
                   decided_at=SLOT_STAMP + 500, loser_embedding=None) -> dict:
    """Seed-only: a curated duplicate retirement's audit row
    (``curation_safety._audit_snapshot``): the loser retired at
    ``decided_at``, the pair's evidence, which side was kept, the loser's
    vector (``_embedding_values``). ``slot`` names the row (by default the
    loser's)."""
    a, b = (survivor, loser) if keep == "a" else (loser, survivor)
    retired = dict(loser, status="retired", superseded_at=decided_at)
    record = dict(retired)
    record.update({
        "_curation_evidence_fingerprint": evidence_fp(loser),
        "_curation_retired_fingerprint": evidence_fp(retired),
        "_curation_duplicate": {"version": 1, "keep": keep, "a": a, "b": b,
                                "a_fingerprint": evidence_fp(a),
                                "b_fingerprint": evidence_fp(b),
                                "loser_embedding": loser_embedding},
    })
    record.pop("embedding")
    if store == "lesson":
        record.update(task=record.pop("entity"), aspect=record.pop("attribute"),
                      lesson=record.pop("value"))
    en, an = slot or (loser["entity_norm"], loser["attribute_norm"])
    return sql("INSERT INTO store_decisions (store, entity_norm, attribute_norm, action, "
               f"decided_by, reason, record, decided_at) VALUES ('{store}', {_lit(en)}, "
               f"{_lit(an)}, 'retire', 'dream-curation', 'duplicate', {_lit(record)}, "
               f"{decided_at!r})", after_stop=True)


def audit_only(store: str, entity_norm: str, attribute_norm: str, record: dict) -> dict:
    """Seed-only: a retire audit row whose slot row compaction purged."""
    return sql("INSERT INTO store_decisions (store, entity_norm, attribute_norm, action, "
               f"decided_by, reason, record, decided_at) VALUES ('{store}', "
               f"{_lit(entity_norm)}, {_lit(attribute_norm)}, 'retire', 'human', NULL, "
               f"{_lit(record)}, {SLOT_STAMP + 10!r})", after_stop=True)


CURATE_SURVIVOR = slot_lesson("Curate Keys", "Approach", "Rotate keys quarterly.")
CURATE_LOSER = slot_lesson("Curate Keys", "Method", "Rotate keys every quarter.",
                           about="vault", asserted=SLOT_STAMP + 100, hlc_logical=1)
CURATE_PURGED = slot_lesson("Curate Keys", "Schedule", "Rotate keys each quarter.",
                            asserted=SLOT_STAMP + 200, hlc_logical=2)
CURATE_WINDOW = slot_lesson("Curate Keys", "Window", "Rotate keys in the window.",
                            asserted=SLOT_STAMP + 300)
CURATE_HOST = slot_world("Curate Host", "OS", "Debian 12")
CURATE_HOST_LOSER = slot_world("Curate Host", "Operating System", "Debian 12 (bookworm)",
                               asserted=SLOT_STAMP + 50, hlc_logical=1)
# a world loser compaction purged, rebuilt from its evidence and vector
CURATE_HOST_DISTRO = slot_world("Curate Host", "Distro", "Debian bookworm",
                                asserted=SLOT_STAMP + 60, hlc_logical=2,
                                embedding=slot_embedding(5))
# a world pair whose retired row is edited behind the daemons' backs
EDIT_HOST = slot_world("Edit Host", "OS", "Ubuntu 24.04")
EDIT_HOST_LOSER = slot_world("Edit Host", "Operating System", "Ubuntu 24.04 LTS",
                             asserted=SLOT_STAMP + 50, hlc_logical=1)
# a lesson pair with vectors: the digests take part in every fingerprint
EMBED_SURVIVOR = slot_lesson("Embed Keys", "Approach", "Pin the signing key id.",
                             embedding=slot_embedding(3))
EMBED_LOSER = slot_lesson("Embed Keys", "Method", "Pin the signing key identifier.",
                          about="vault", asserted=SLOT_STAMP + 100, hlc_logical=1,
                          embedding=slot_embedding(7))
_L, _W = (lambda row, **kw: slot_snapshot("lesson", row, **kw),
          lambda row, **kw: slot_snapshot("world", row, **kw))

# Lesson audit snapshots whose rows compaction purged: rewritten through
# lesson_write on restore (an about that gets a graph node, and a polarity,
# outcome and support of their own).
PURGED_OWNER = {
    "task": "Purge Keys", "aspect": "Owner", "lesson": "The ops team purges keys.",
    "about": "vault", "polarity": "+", "outcome": "success", "status": "retired",
    "confidence": 0.8, "origin": "agent", "provenance": ["signal:1"], "support": [],
    "asserted_at": SLOT_STAMP, "last_confirmed": SLOT_STAMP, "supersedes_value": None,
    "superseded_by_value": None, "superseded_at": SLOT_STAMP + 10}
PURGED_APPROACH = {
    "task": "Purged Task", "aspect": "Approach", "lesson": "Avoid manual purges.",
    "about": None, "polarity": "-", "outcome": "failure", "status": "retired",
    "confidence": 0, "origin": None, "provenance": ["signal:2", "entry:9"],
    "support": ["agent"], "asserted_at": SLOT_STAMP, "last_confirmed": SLOT_STAMP,
    "supersedes_value": None, "superseded_by_value": None, "superseded_at": SLOT_STAMP + 10}
REVIEW_SLOTS_SEED = [
    store("A note so the bank is not empty."),
    # world facts the legacy restore brings back, and ones whose retired row
    # a purge leaves only as an audit snapshot (a whole entity: mixed)
    world_set("Rust", "edition", "2024", source_url="https://doc.rust-lang.org/"),
    world_set("Rust", "latest stable version", "1.90.0", source_url="https://blog.rust-lang.org/"),
    world_set("Python", "latest version", "3.13.0", source_url="https://www.python.org/"),
    world_set("Kotlin", "latest version", "2.1.0", source_url="https://kotlinlang.org/"),
    world_set("Kotlin", "edition", "K2", source_url="https://kotlinlang.org/docs/"),
    forget("world", entity="Python"),
    forget("world", entity="Kotlin"),
    sql("DELETE FROM world_facts WHERE status = 'retired' AND (entity_norm = 'python' OR "
        "(entity_norm = 'kotlin' AND attribute_norm = 'edition'))", after_stop=True),
    # audit snapshots nothing else holds: an unsafe citation (rejected on
    # rewrite) and one carrying a source document id
    audit_only("world", "evil", "link", {
        "entity": "Evil", "attribute": "link", "value": "click", "polarity": "+",
        "status": "retired", "confidence": 0.7, "origin": "source",
        "source_url": "javascript:alert(1)", "source_quote": "", "freshness_class": "volatile",
        "retrieved_at": SLOT_STAMP, "content_hash": None, "source_doc_id": None,
        "asserted_at": SLOT_STAMP, "last_confirmed": SLOT_STAMP}),
    audit_only("world", "manual", "page-count", {
        "entity": "Manual", "attribute": "page count", "value": "212", "polarity": "+",
        "status": "retired", "confidence": 0, "origin": "source",
        "source_url": "https://example.org/manual", "source_quote": "212 pages",
        "freshness_class": "evergreen", "retrieved_at": SLOT_STAMP, "content_hash": "abc123",
        "source_doc_id": 7, "asserted_at": SLOT_STAMP, "last_confirmed": SLOT_STAMP}),
    # lessons: two retired records at one slot (the newer comes back), a
    # live slot with a retired record, and lessons to forget and restore
    # (an avoid lesson, an about that is a compound and gets no node)
    slot_row_sql("lesson", slot_lesson("Rotate Keys", "Cadence", "Rotate keys yearly.",
                                       status="retired", about="vault",
                                       superseded_at=SLOT_STAMP + 100)),
    slot_row_sql("lesson", slot_lesson("Rotate Keys", "Cadence", "Rotate keys quarterly.",
                                       status="retired", about="vault",
                                       asserted=SLOT_STAMP + 50,
                                       superseded_at=SLOT_STAMP + 200)),
    slot_row_sql("lesson", slot_lesson("Rotate Keys", "Owner",
                                       "The security team owns rotation.")),
    slot_row_sql("lesson", slot_lesson("Rotate Keys", "Owner", "The ops team owns rotation.",
                                       status="retired", superseded_at=SLOT_STAMP + 150)),
    slot_row_sql("lesson", slot_lesson("Ship Release", "Checklist",
                                       "Skip the manual checklist.", polarity="-",
                                       about="release checklist")),
    slot_row_sql("lesson", slot_lesson("Ship Release", "Freeze", "Freeze merges a day ahead.",
                                       about="pg+extractor")),
    # curated duplicate retirements: the loser's row retired (or purged),
    # a row naming another slot's loser, and evidence the survivor no
    # longer matches; a world pair
    slot_row_sql("lesson", CURATE_SURVIVOR),
    slot_row_sql("lesson", dict(CURATE_LOSER, status="retired", superseded_at=SLOT_STAMP + 500)),
    curated_retire("lesson", _L(CURATE_SURVIVOR), _L(CURATE_LOSER)),
    curated_retire("lesson", _L(CURATE_SURVIVOR), _L(CURATE_PURGED), decided_at=SLOT_STAMP + 510),
    curated_retire("lesson", _L(CURATE_SURVIVOR), _L(CURATE_LOSER),
                   slot=("curate-keys", "owner"), decided_at=SLOT_STAMP + 520),
    curated_retire("lesson", _L(CURATE_SURVIVOR, value="Rotate keys yearly."),
                   _L(CURATE_WINDOW), keep="b", decided_at=SLOT_STAMP + 530),
    slot_row_sql("world", CURATE_HOST),
    slot_row_sql("world", dict(CURATE_HOST_LOSER, status="retired",
                               superseded_at=SLOT_STAMP + 500)),
    curated_retire("world", _W(CURATE_HOST), _W(CURATE_HOST_LOSER)),
    curated_retire("world", _W(CURATE_HOST), _W(CURATE_HOST_DISTRO), decided_at=SLOT_STAMP + 540,
                   loser_embedding=CURATE_HOST_DISTRO["embedding"]),
    slot_row_sql("world", EDIT_HOST),
    slot_row_sql("world", dict(EDIT_HOST_LOSER, status="retired",
                               superseded_at=SLOT_STAMP + 500)),
    curated_retire("world", _W(EDIT_HOST), _W(EDIT_HOST_LOSER)),
    slot_row_sql("lesson", EMBED_SURVIVOR),
    slot_row_sql("lesson", dict(EMBED_LOSER, status="retired", superseded_at=SLOT_STAMP + 500)),
    curated_retire("lesson", _L(EMBED_SURVIVOR), _L(EMBED_LOSER),
                   loser_embedding=EMBED_LOSER["embedding"]),
    # lessons whose retired row compaction purged: an audit snapshot alone,
    # and beside a retired record at another slot of the task (mixed)
    slot_row_sql("lesson", slot_lesson("Purge Keys", "Cadence", "Purge keys monthly.",
                                       status="retired", superseded_at=SLOT_STAMP + 100)),
    audit_only("lesson", "purge-keys", "owner", PURGED_OWNER),
    audit_only("lesson", "purged-task", "approach", PURGED_APPROACH),
]

AUTO_MARKERS = (
    "INSERT INTO meta (key, value) VALUES ('curation_auto_dismissals_v1', "
    "'{\"m1\": {\"store\": \"lesson\", \"a\": {\"key\": \"rotate-keys|owner\"}, "
    "\"b\": {\"key\": \"rotate-keys|cadence\"}}, \"m2\": {\"store\": \"world\", "
    "\"a\": {\"key\": \"rotate-keys|cadence\"}, \"b\": {\"key\": \"rotate-keys|owner\"}}, "
    "\"m3\": {\"store\": \"lesson\", \"a\": {\"key\": \"x|y\"}, \"b\": {\"key\": \"z|w\"}}, "
    "\"m4\": {\"store\": \"world\", \"a\": {\"key\": \"x|y\"}, \"b\": null}}') "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
)
REVIEW_SLOTS_OPS = [
    # dismiss_slot_pair: the argument refusals, a bad store, a pair that is
    # one slot or has a blank component
    review("dismiss_slot_pair", store="lesson", src="rotate keys|cadence"),
    review("dismiss_slot_pair", store="", src="a|b", dst="c|d"),
    review("dismiss_slot_pair", store="lesson", src="a|b|c", dst="c|d"),
    review("dismiss_slot_pair", store="bogus", src="a|b", dst="c|d"),
    review("dismiss_slot_pair", store="lesson", src="Rotate_Keys|CADENCE",
           dst="rotate keys|cadence"),
    review("dismiss_slot_pair", store="world", src="|x", dst="rust|edition"),
    # the first verdict writes the (absent) markers back as {}; a repeat is
    # not new; with markers, only the same store's same pair is dropped
    review("dismiss_slot_pair", store="lesson", src="rotate keys|cadence",
           dst="Rotate Keys|Owner"),
    review("dismiss_slot_pair", store="lesson", src="Rotate Keys|Owner",
           dst="rotate keys|cadence"),
    external(AUTO_MARKERS),
    # m1 dropped; m4 (another store, a null side) is never sorted
    review("dismiss_slot_pair", store="lesson", src="rotate keys|owner",
           dst="rotate keys|cadence"),
    # a literal pipe is its own slot
    review("dismiss_slot_pair", store="lesson", src="ci%7Ccd|deploy", dst="ci-cd|deploy"),
    # the same store's broken marker raises (invalid_argument, rolled back)
    review("dismiss_slot_pair", store="world", src="rust|edition",
           dst="rust|latest stable version"),
    # restore_slot: the argument refusals
    review("restore_slot", store="bogus", src="rotate keys"),
    review("restore_slot", store="lesson"),
    review("restore_slot", store="world", src="a|b|c"),
    # lessons: the newest retired record; a live slot; nothing retired
    review("restore_slot", store="lesson", src="rotate keys|cadence"),
    review("restore_slot", store="lesson", src="Rotate Keys|Owner"),
    review("restore_slot", store="lesson", src="rotate keys"),
    review("restore_slot", store="lesson", src="nobody"),
    # a forgotten task comes back whole, its graph links with it
    forget("lesson", entity="ship release"),
    review("restore_slot", store="lesson", src="Ship Release"),
    review("restore_slot", store="lesson", src="ship release|freeze"),
    # world facts: a retired record; then the audit snapshots alone, mixed
    # with a retired record, rejected, and carrying a source document
    forget("world", entity="Rust", attribute="edition"),
    review("restore_slot", store="world", src="rust|edition"),
    review("restore_slot", store="world", src="rust|edition"),
    review("restore_slot", store="world", src="python|latest version"),
    review("restore_slot", store="world", src="kotlin"),
    review("restore_slot", store="world", src="evil|link"),
    review("restore_slot", store="world", src="manual"),
    # curated duplicates: a whole entity is ambiguous; a row naming another
    # loser; stale evidence; the retired loser; the purged loser rebuilt
    # from its evidence; then the slot is live
    review("restore_slot", store="lesson", src="curate keys"),
    review("restore_slot", store="lesson", src="curate keys|owner"),
    review("restore_slot", store="lesson", src="curate keys|window"),
    review("restore_slot", store="lesson", src="curate keys|method"),
    review("restore_slot", store="lesson", src="curate keys|schedule"),
    review("restore_slot", store="lesson", src="curate keys|method"),
    review("restore_slot", store="world", src="curate host|operating system"),
    # a purged world loser rebuilt from its evidence, its vector with it
    review("restore_slot", store="world", src="curate host|distro"),
    # the retired row edited after the curated retire: the resident copy
    # still matches, the locked row no longer carries the retired
    # fingerprint
    external("UPDATE world_facts SET source_quote = 'edited out of band' "
             "WHERE entity_norm = 'edit-host' AND attribute_norm = 'operating-system' "
             "AND status = 'retired'"),
    review("restore_slot", store="world", src="edit host|operating system"),
    # vectors on both sides: the digests are part of each fingerprint
    review("restore_slot", store="lesson", src="embed keys|method"),
    # lessons from their audit snapshots, through lesson_write: mixed with
    # a retired record, then the slot is live; a snapshot alone
    review("restore_slot", store="lesson", src="purge keys"),
    review("restore_slot", store="lesson", src="purge keys|owner"),
    review("restore_slot", store="lesson", src="purged task"),
    review(),
]

SCENARIOS.update({
    "review-slots": {
        "config": None,
        "seed": REVIEW_SLOTS_SEED,
        "ops": REVIEW_SLOTS_OPS,
    },
})


# memory_dream status/pull/commit/runs (P1-A) against the stub extractor.
# The seed is one canonical fact (an entity, so the deep-dream need block has
# something to report) and no entry: the first status and pull see an empty
# backlog. Entries are then stored through each daemon's own write path, so
# the pull proves a newly stored resident is pullable; one is a ``log``
# entry (an excluded source) and one carries a session (its episode id
# rides the pull). The extractor model is the ``extractor`` alias with a
# fallback configured, so status resolves the served model (GET /v1/models,
# cached five minutes) and probes the primary (GET /health) each call: the
# stub compares those request sequences. No call reaches the model, so the
# reply table is empty: any POST would be an unanswered-request diff.
def dream(action, **kw) -> dict:
    return tool("memory_dream", action=action, **kw)


# A tool argument naming the last commit_token this side answered.
PULL_TOKEN = "@pull_token"


DREAM_NOTES = [
    "The deploy target moved from staging to prod-eu this week.",
    "Bob prefers green tea over coffee in the mornings.",
    "The release runbook requires a signed tag for every release.",
    "Our CI cache lives on the build-02 host now.",
    "The staging database was upgraded to PostgreSQL 17.",
    "Alice owns the billing service rewrite.",
    "The weekly sync moved to Thursday afternoons.",
    "We pinned the embedding model to Qwen3-Embedding-0.6B.",
]

SCENARIOS.update({
    "dream-basics": {
        "config": None,
        "extractor": {"model": "extractor", "served_model": "stub-served-model",
                      "replies": []},
        "extractor_fallback": True,
        "seed": [fact("Dream Host", "OS", "Debian 12")],
        "ops": [
            dream("status"),
            dream("pull"),
            dream("runs"),
            dream("commit", commit_token=PULL_TOKEN),  # no pull token yet: None -> required
            store(DREAM_NOTES[0]),
            store("sweep tick done in 0.4s", source="log"),
            store(DREAM_NOTES[1], headers=[sess("w2e-dream")]),
            dream("status"),
            *[store(t) for t in DREAM_NOTES[2:]],
            dream("status"),
            dream("pull", limit=2),
            dream("commit", commit_token="v1.bad.token"),
            dream("commit", cursor=1.5),
            dream("commit"),
            dream("commit", commit_token=PULL_TOKEN),
            dream("commit", commit_token=PULL_TOKEN),
            dream("status"),
            dream("pull"),
            dream("commit", commit_token=PULL_TOKEN),
            dream("pull", limit=5),
            dream("status"),
            dream("runs", limit=3),
            dream("bogus"),
        ],
    },
})


# memory_dream run (P1-B), the consolidation pass, against the stub
# extractor. Every model request the pass sends is answered from the
# scenario's table by (prompt kind, user message): the claims call and the
# events pass number the notes (``[1] text``, blank-line separated), the
# relations calls join them with a blank line. The vocab hint in the system
# prompt depends on embeddings, so replies are never keyed by body hash.
# The unported stages (outcome inference, lesson synthesis, digests, the
# graph-insight refresh) are switched off: the Rust daemon refuses ``run``
# while any is on. The sweep and the stall notice are off too, so nothing
# runs behind the operations.
DREAM_RUN_CONFIG = (
    "memory:\n"
    "  lessons:\n    enabled: false\n"
    "  graph_insight:\n    enabled: false\n"
    "  dream:\n    enabled: false\n    stall_notice: false\n"
)


def numbered(texts) -> str:
    """The claims and events calls' user message."""
    return "\n\n".join(f"[{i + 1}] {t}" for i, t in enumerate(texts))


def claims_reply(texts, claims, events=()) -> dict:
    return {"kind": "claims", "user": numbered(texts),
            "content": json.dumps({"claims": list(claims), "events": list(events)})}


def events_reply(texts, events) -> dict:
    return {"kind": "events", "user": numbered(texts),
            "content": json.dumps({"events": list(events)})}


def relations_reply(texts, relations) -> dict:
    return {"kind": "relations", "user": "\n\n".join(texts),
            "content": json.dumps({"relations": list(relations)})}


# One pending batch with a claim of each kind. Sources set the write tier:
# conversation is user, claude is agent, tool is action; a cited note backs
# a tier above agent only when it states the value and names the slot.
RUN_NOTES = [
    ("The deploy target region moved from staging to prod-eu this week.", "conversation"),
    ("Bob still drinks green tea every morning.", "conversation"),
    ("Alice is now the billing manager.", "claude"),
    ("The staging database was upgraded to PostgreSQL 17 on 2026-09-03.", "conversation"),
    ("Release runbook: a changelog entry is now required, and the signed tag "
     "requirement was dropped.", "tool"),
    ("Never deploy on Fridays.", "claude"),
    ("Carol said the deployment target for the EU launch is prod-eu.", "conversation"),
    ("Dana manages the payments team and the ledger database.", "conversation"),
]
RUN_TEXTS = [t for t, _ in RUN_NOTES]
RUN_EVENT = {"description": "Staging database upgraded to PostgreSQL 17",
             "date": "2026-09-03", "source": 4}
RUN_CLAIMS = [
    # superseded: a user-tier claim the note backs, over an agent fact
    {"entity": "Deploy Target", "attribute": "region", "value": "prod-eu",
     "confidence": 0.9, "source": 1},
    # confirmed
    {"entity": "Bob", "attribute": "drink", "value": "green tea", "confidence": 0.8,
     "source": 2},
    # contested: an agent-tier claim against a direct user fact
    {"entity": "Alice", "attribute": "role", "value": "billing manager",
     "confidence": 0.8, "source": 3},
    # superseded, over a fact written in this run (it carries a slot embedding)
    {"entity": "Staging DB", "attribute": "version", "value": "PostgreSQL 17",
     "confidence": 0.9, "source": 4},
    # dropped by the literal gate: 15 is in no note
    {"entity": "Staging DB", "attribute": "previous version", "value": "PostgreSQL 15",
     "source": 4},
    # a scalar claim at a set slot: dropped_set_slot
    {"entity": "Release Runbook", "attribute": "requirements", "value": "changelog",
     "source": 5},
    # set add and remove at the action tier
    {"entity": "Release Runbook", "attribute": "requirements", "value": "changelog entry",
     "op": "add", "source": 5},
    {"entity": "Release Runbook", "attribute": "requirements", "value": "signed tag",
     "op": "remove", "source": 5},
    # the same note already formed this slot (the member ops' traces): the
    # has_trace guard skips a scalar claim, before the set-slot refusal
    {"entity": "Release Runbook", "attribute": "requirements", "value": "a changelog entry",
     "source": 5},
    # the constraint carrier: a paraphrase of a constraint note, rewritten
    # to the note's text
    {"entity": "Deploys", "attribute": "friday policy", "value": "deploy freeze on fridays",
     "source": 6},
    # a new entity near an existing one (the alias screen)
    {"entity": "Deployment Target", "attribute": "eu launch", "value": "prod-eu",
     "source": 7},
    # an assistant-stated claim: the contender policy demotes its tier
    {"entity": "EU Launch", "attribute": "date", "value": "next quarter",
     "speaker": "assistant", "source": 7},
    # citation-backed user tier, and an unbacked claim from the same note
    {"entity": "Dana", "attribute": "team", "value": "payments team", "source": 8},
    {"entity": "Dana", "attribute": "title", "value": "director of payments", "source": 8},
    # uncited in a batch of eight: no gate, agent tier, no trace
    {"entity": "Ledger DB", "attribute": "engine", "value": "PostgreSQL"},
    # a vocab key flattened into the entity: repaired to ci-cache.host
    {"entity": "ci-cache.host", "attribute": "value", "value": "build-02"},
]
RUN_RELATIONS = [
    {"src": "Deploy Target", "relation": "related-to", "dst": "MCP"},          # generic hub
    {"src": "1234", "relation": "uses", "dst": "Bob"},                          # junk name
    {"src": "docker-desktop", "relation": "runs-on", "dst": "build.py"},        # under the floor
    {"src": "Billing Service Rewrite", "relation": "depends-on",
     "dst": "Ledger DB"},                                                       # live edge, write-dedup
    {"src": "Grafana", "relation": "depends-on",
     "dst": "Billing Service Rewrite"},                                         # cross-project proposal
    {"src": "Grafana", "relation": "related-to",
     "dst": "Billing Service Rewrite"},                                         # cross-project untyped: dropped
    {"src": "Ledger DB", "relation": "related-to", "dst": "Dana"},              # low-confidence proposal
]
RUN_EXTRACTOR = {"model": "stub-model", "replies": [
    claims_reply(RUN_TEXTS, RUN_CLAIMS, [RUN_EVENT]),
    events_reply(RUN_TEXTS, [
        RUN_EVENT,  # a duplicate of the inline event
        {"description": "Deploy target moved to prod-eu", "date_phrase": "this week",
         "actor": "team", "source": 1},
    ]),
    relations_reply(RUN_TEXTS, RUN_RELATIONS),
    # the retype pass: the low-confidence pair's one shared note
    relations_reply([RUN_TEXTS[7]], [{"src": "Dana", "relation": "uses", "dst": "Ledger DB"}]),
]}

HOLD_NOTES = ["The nightly backup now runs at 02:00 on backup-01.",
              "Erin moved to the platform team.",
              "Frank prefers the dark theme."]
HOLD_EXTRACTOR = {"model": "stub-model", "replies": [
    # the batched call fails: hold, hold, then the per-entry isolation pass
    {"kind": "claims", "user": numbered(HOLD_NOTES[:2]), "status": 500,
     "body": json.dumps({"error": "upstream failure"})},
    claims_reply(HOLD_NOTES[:1], [{"entity": "Nightly Backup", "attribute": "host",
                                   "value": "backup-01", "source": 1}]),
    {"kind": "claims", "user": numbered(HOLD_NOTES[1:2]), "status": 500,
     "body": json.dumps({"error": "upstream failure"})},
    events_reply(HOLD_NOTES[:2], []),
    relations_reply(HOLD_NOTES[:2], []),
    # a reply that is not JSON at all
    {"kind": "claims", "user": numbered(HOLD_NOTES[2:]), "content": "not json at all"},
]}

# The switched-off-by-default paths: the consolidation quarantine (park,
# hold, promote by an independent witness), the span gate in contend mode,
# and a constraint note no claim carries (a guard miss).
GATE_NOTES = [
    ("The cache eviction policy is LRU now.", "claude"),
    ("Cache eviction runs LRU, as noted before.", "claude"),
    ("Confirmed: the cache eviction policy is LRU.", "tool"),
    ("Erin said the release train leaves on Tuesdays.", "conversation"),
    ("Always tag releases before deploying.", "claude"),
]
GATE_TEXTS = [t for t, _ in GATE_NOTES]
GATE_CLAIMS = [
    # first low-trust sighting: parked as a marked contender
    {"entity": "Cache", "attribute": "eviction", "value": "LRU", "source": 1},
    # the same witness again: held, not promoted
    {"entity": "Cache", "attribute": "eviction", "value": "LRU", "source": 2},
    # an independent, trusted witness: promoted
    {"entity": "Cache", "attribute": "eviction", "value": "LRU", "source": 3},
    # a quote the note does not contain: parked by the span gate
    {"entity": "Release Train", "attribute": "day", "value": "Tuesday", "source": 4,
     "quote": "the train leaves every Tuesday morning"},
    # a quote the note contains: written
    {"entity": "Release Train", "attribute": "owner", "value": "Erin", "source": 4,
     "quote": "Erin said the release train"},
]
GATE_EXTRACTOR = {"model": "stub-model", "replies": [
    claims_reply(GATE_TEXTS, GATE_CLAIMS),
    events_reply(GATE_TEXTS, []),
    relations_reply(GATE_TEXTS, []),
]}

SCENARIOS.update({
    "dream-run": {
        "config": DREAM_RUN_CONFIG,
        "extractor": RUN_EXTRACTOR,
        "seed": [
            fact("Deploy Target", "region", "staging"),
            fact("Bob", "drink", "green tea"),
            fact("Alice", "role", "billing lead", origin="user"),
            fact("Billing Service", "owner", "Alice"),
            fact("Grafana", "port", "3000"),
            set_add("Release Runbook", "requirements", "signed tag"),
            # a manual project scope (only the dream and the backfill write
            # entity_sources): Grafana belongs to a project the batch is not
            sql("INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
                "SELECT id, 'homelab', 1, 'manual', 1 FROM entities "
                "WHERE display = 'Grafana'", after_stop=True),
        ],
        "ops": [
            # facts written in-process carry slot embeddings: the vocab
            # hint ranks them, the hydrated rest follows alphabetically
            fact("CI Cache", "host", "build-02"),
            fact("Staging DB", "version", "PostgreSQL 16"),
            *[store(t, source=src) for t, src in RUN_NOTES],
            dream("status"),
            dream("run"),
            dream("status"),
            dream("pull"),
            dream("runs"),
            dream("run"),
        ],
    },
    "dream-run-hold": {
        "config": DREAM_RUN_CONFIG,
        "extractor": HOLD_EXTRACTOR,
        "seed": [fact("Hold Host", "OS", "Debian 12")],
        "ops": [
            store(HOLD_NOTES[0], source="conversation"),
            store(HOLD_NOTES[1], source="conversation"),
            dream("run", limit=5),
            dream("status"),
            dream("run", limit=5),
            dream("status"),
            dream("run", limit=5),
            dream("status"),
            dream("runs"),
            store(HOLD_NOTES[2], source="conversation"),
            dream("run"),
            dream("status"),
            dream("pull"),
        ],
    },
    "dream-run-gates": {
        "config": DREAM_RUN_CONFIG + "    quarantine_low_trust: true\n    span_gate: contend\n",
        "extractor": GATE_EXTRACTOR,
        "seed": [fact("Gate Host", "OS", "Debian 12")],
        "ops": [
            *[store(t, source=src) for t, src in GATE_NOTES],
            dream("run"),
            dream("runs"),
            fact_get("Cache", "eviction"),
            fact_get("Release Train", "day"),
        ],
    },
    "dream-run-empty": {
        "config": DREAM_RUN_CONFIG,
        "extractor": {"model": "stub-model", "replies": []},
        "seed": [
            fact("Empty Host", "OS", "Debian 12"),
            fact("Probe Node", "role", "edge"),
            # a quarantined pair no note mentions: the retype pass considers
            # it and leaves it pending without a model call
            sql("INSERT INTO edge_proposals (src_id, relation, dst_id, confidence, similarity, "
                "rationale, source, created_at) SELECT a.id, 'related-to', b.id, 0.45, NULL, "
                "'low-confidence dream edge (0.45)', 'dream-low-confidence', 1 "
                "FROM entities a, entities b WHERE a.display = 'Empty Host' "
                "AND b.display = 'Probe Node'", after_stop=True),
        ],
        "ops": [
            dream("run"),
            dream("status"),
            dream("runs"),
            dream("run", limit=3),
        ],
    },
})


# The dream pass's remaining branches. Every scenario here switches the
# alias screen off (its proposals turn on embedding cosines, covered by
# dream-run) and writes at most one fact in-process: hydrated facts carry no
# slot embedding, so the vocab hint and the known-facts window rank one
# embedded record and then the rest alphabetically, and no near-tie can
# reorder them.
DREAM_RUN_QUIET = DREAM_RUN_CONFIG + "    alias_candidate_min_cosine: 0\n"


def batch_replies(texts, claims=(), events=(), relations=()) -> list[dict]:
    """The claims, events and relations replies for one committed batch."""
    return [claims_reply(texts, claims), events_reply(texts, list(events)),
            relations_reply(texts, list(relations))]


def failing_claims(texts, status=503, **extra) -> dict:
    return {"kind": "claims", "user": numbered(texts), "status": status,
            "body": json.dumps({"error": "upstream failure"}), **extra}


# The fallback: the primary's /health answers 503 to the first six probes
# (the status call's one, then two per run: the probe and its retry), so the
# first two runs go to the fallback endpoint with the fallback key and the
# second opens a served_by_fallback stall; the seventh probe succeeds, the
# third run goes to the primary with the primary key and the stall closes.
FALLBACK_NOTES = ["The metrics exporter now listens on port 9100.",
                  "Gina leads the observability guild.",
                  "The log shipper moved to the vector agent."]
FALLBACK_EXTRACTOR = {
    "model": "stub-model", "fallback_model": "stub-fallback-model",
    "health": [503] * 6 + [200],
    "replies": [
        *batch_replies(FALLBACK_NOTES[:1], [{"entity": "Metrics Exporter", "attribute": "port",
                                             "value": "9100", "source": 1}]),
        *batch_replies(FALLBACK_NOTES[1:2], [{"entity": "Gina", "attribute": "role",
                                              "value": "observability guild lead",
                                              "source": 1}]),
        *batch_replies(FALLBACK_NOTES[2:], [{"entity": "Log Shipper", "attribute": "agent",
                                             "value": "vector", "source": 1}]),
    ]}

# The hint knobs: a three-fact known-facts window, the dream slot resolver at
# 0.99 (a claim keyed "Deploy" / "Target region" embeds the same text as the
# in-process slot "Deploy Target" / "region", cosine 1, and lands on it; an
# unrelated slot keeps its own key), and assistant-stated claims dropped.
HINT_NOTES = ["The deploy target region is now prod-eu.",
              "Ivan said the build farm runner is the arm64 pool."]
HINT_CLAIMS = [
    {"entity": "Deploy", "attribute": "Target region", "value": "prod-eu", "source": 1},
    {"entity": "Zeta Probe", "attribute": "color", "value": "teal", "source": 2},
    # dropped: assistant_claims is drop
    {"entity": "Build Farm", "attribute": "runner", "value": "x86 pool",
     "speaker": "assistant", "source": 2},
]

# The literal gate in log mode with source scope, assistant claims kept at
# their note's tier (supersede), and a batch with no date in it, so an
# extracted calendar date is dropped (the event stores undated, its phrase
# kept). The first claim's Arabic-Indic 17 is backed by the note's "17"
# (float() folds Unicode digits); the second's 15 is in no note; the
# third's 6380 is in another note than the one it cites, which only the
# batch scope would accept.
LITERAL_NOTES = [("The staging database runs PostgreSQL 17.", "conversation"),
                 ("The cache sidecar listens on port 6380.", "conversation"),
                 ("Kim said the ops board owner is now Lee.", "conversation")]
LITERAL_TEXTS = [t for t, _ in LITERAL_NOTES]
LITERAL_CLAIMS = [
    {"entity": "Staging Database", "attribute": "engine", "value": "PostgreSQL ١٧",
     "source": 1},
    {"entity": "Staging Database", "attribute": "engine version", "value": "PostgreSQL 15",
     "source": 1},
    {"entity": "Cache Sidecar", "attribute": "backup port", "value": "6380", "source": 1},
    {"entity": "Ops Board", "attribute": "owner", "value": "Lee", "speaker": "assistant",
     "source": 3},
]
LITERAL_EXTRACTOR = {"model": "stub-model", "replies": [
    claims_reply(LITERAL_TEXTS, LITERAL_CLAIMS, [
        {"description": "Staging database moved to PostgreSQL 17", "date": "2026-09-03",
         "date_phrase": "last week", "source": 1}]),
    events_reply(LITERAL_TEXTS, [{"description": "Cache sidecar port changed to 6380",
                                  "date": "2026-10-01", "source": 2}]),
    relations_reply(LITERAL_TEXTS, []),
]}

# Isolation: a two-entry batch the endpoint refuses three times, then refuses
# each entry alone (an outage: hold), then a one-entry batch (limit 1) that
# fails three times and alone: set aside with the extractor's error.
OUTAGE_NOTES = ["The VPN gateway rotated its certificate.",
                "Lena joined the security rotation."]
OUTAGE_EXTRACTOR = {"model": "stub-model", "replies": [
    failing_claims(OUTAGE_NOTES),
    failing_claims(OUTAGE_NOTES[:1]),
    failing_claims(OUTAGE_NOTES[1:]),
    events_reply(OUTAGE_NOTES[:1], []),
    relations_reply(OUTAGE_NOTES[:1], []),
]}

# A redirect refused (no_redirect.urlopen: the redirect's own HTTPError, its
# message naming the target, here an OAuth page: a failed login) and a 500
# whose reason phrase names a failed login.
REDIRECT_NOTES = ["The artifact cache moved behind the SSO proxy.",
                  "Omar rotated the deploy keys."]
REDIRECT_EXTRACTOR = {"model": "stub-model", "replies": [
    failing_claims(REDIRECT_NOTES[:1], 302,
                   headers={"Location": "http://elsewhere.example/oauth/authorize"}),
    failing_claims(REDIRECT_NOTES[1:], 500, reason="Login Required"),
    events_reply(REDIRECT_NOTES[:1], []),
    relations_reply(REDIRECT_NOTES[:1], []),
]}

# Lone surrogates in a reply: in a field nothing writes (the quote) the claim
# consolidates; in a value, the write fails as psycopg fails to encode it
# (a write-phase hold, the earlier claim's write kept and journaled).
SURROGATE_NOTES = ["The report generator writes to the reports bucket.",
                   "Pia owns the billing exports."]
SURROGATE_EXTRACTOR = {"model": "stub-model", "replies": [
    claims_reply(SURROGATE_NOTES, [
        {"entity": "Report Generator", "attribute": "output", "value": "reports bucket",
         "quote": "writes to the reports bucket \ud83d", "source": 1},
        {"entity": "Pia", "attribute": "owns", "value": "billing exports \ud83d", "source": 2},
    ]),
]}

SCENARIOS.update({
    "dream-run-fallback": {
        "config": DREAM_RUN_QUIET,
        "extractor": FALLBACK_EXTRACTOR,
        "extractor_fallback": True,
        "extractor_keys": {"primary": "sk-w2e-primary-0001", "fallback": "sk-w2e-fallback-0002"},
        "seed": [fact("Fallback Host", "OS", "Debian 12")],
        "ops": [
            store(FALLBACK_NOTES[0], source="conversation"),
            dream("status"),
            dream("run"),
            store(FALLBACK_NOTES[1], source="conversation"),
            dream("run"),
            dream("status"),
            store(FALLBACK_NOTES[2], source="conversation"),
            dream("run"),
            dream("status"),
            dream("runs"),
        ],
    },
    "dream-run-hints": {
        "config": DREAM_RUN_QUIET + "    known_facts_window: 3\n    assistant_claims: drop\n"
                  "  cortex:\n    dream_slot_match_threshold: 0.99\n",
        "extractor": {"model": "stub-model",
                      "replies": batch_replies(HINT_NOTES, HINT_CLAIMS)},
        "seed": [fact("Hint Host", "OS", "Debian 12"),
                 fact("Build Farm", "runner", "arm64 pool"),
                 fact("Ticket Queue", "owner", "Hana")],
        "ops": [
            fact("Deploy Target", "region", "staging"),
            *[store(t, source="conversation") for t in HINT_NOTES],
            dream("run"),
            dream("runs"),
            fact_get("Deploy Target", "region"),
            fact_get("Build Farm", "runner"),
        ],
    },
    "dream-run-literal": {
        "config": DREAM_RUN_QUIET + "    literal_gate: log\n    literal_gate_scope: source\n"
                  "    assistant_claims: supersede\n",
        "extractor": LITERAL_EXTRACTOR,
        "seed": [fact("Literal Host", "OS", "Debian 12"),
                 # agent tier: the assistant-stated claim at its user note's
                 # tier supersedes it, where the contender policy would park
                 fact("Ops Board", "owner", "Jun")],
        "ops": [
            *[store(t, source=src) for t, src in LITERAL_NOTES],
            dream("run"),
            dream("runs"),
            fact_get("Ops Board", "owner"),
            fact_get("Staging Database", "engine"),
        ],
    },
    "dream-run-outage": {
        "config": DREAM_RUN_QUIET,
        "extractor": OUTAGE_EXTRACTOR,
        "seed": [fact("Outage Host", "OS", "Debian 12")],
        "ops": [
            *[store(t, source="conversation") for t in OUTAGE_NOTES],
            dream("run"),
            dream("run"),
            dream("run"),
            dream("status"),
            dream("run", limit=1),
            dream("run", limit=1),
            dream("run", limit=1),
            dream("status"),
            dream("pull"),
        ],
    },
    "dream-run-redirect": {
        "config": DREAM_RUN_QUIET,
        "extractor": REDIRECT_EXTRACTOR,
        "seed": [fact("Redirect Host", "OS", "Debian 12")],
        "ops": [
            *[store(t, source="conversation") for t in REDIRECT_NOTES],
            dream("run", limit=1),
            dream("run", limit=1),
            dream("status"),
            dream("run", limit=1),
            dream("run", limit=1),
            dream("status"),
        ],
    },
    "dream-run-surrogate": {
        "config": DREAM_RUN_QUIET,
        "extractor": SURROGATE_EXTRACTOR,
        "seed": [fact("Surrogate Host", "OS", "Debian 12")],
        "ops": [
            *[store(t, source="conversation") for t in SURROGATE_NOTES],
            dream("run"),
            dream("status"),
            dream("runs"),
            dream("run"),
            fact_get("Report Generator", "output"),
        ],
    },
})


# memory_dream run's post-consolidation stages (P1b): outcome inference,
# lesson synthesis (clustering and rule mode) and session digests, each
# against the stub. Session entries are ``status`` notes, which the dream
# pull excludes, so every run takes the empty-pull branch (the only one that
# digests). A session root is closed by the SessionEnd hook or the idle
# reaper, neither an MCP tool: the seed closes it with a declared ``sql`` op
# at fixed clocks, so the candidates' order and the cursors are exact.
# Signals come from memory_outcome. The cross-key dedup gate is off: it
# turns on embedding cosines.
def stages_config(lessons: str = "", dream: str = "") -> str:
    return ("memory:\n"
            "  graph_insight:\n    enabled: false\n"
            "  lessons:\n" + (lessons or "    enabled: true\n") +
            "  dream:\n    enabled: false\n    stall_notice: false\n" + dream)


def close_root(key: str, ended: float) -> dict:
    """Seed-only: the session root closed (``started_at`` 50 s earlier)."""
    return sql(f"UPDATE episodes SET started_at = {ended - 50!r}, ended_at = {ended!r} "
               f"WHERE parent_id IS NULL AND session_key = '{key}'", after_stop=True)


def session_context(title: str, entries, subtasks=()) -> str:
    """``_episode_inference_context`` of a session: its title, sub-task
    titles, then ``(text, source)`` entries in store order."""
    lines = [f"Session: {title}"] + [f"Sub-task: {t}" for t in subtasks]
    lines += [f"- ({src}) {text}" for text, src in entries]
    return "\n".join(lines)


def split_context(text: str, cap: int) -> list[str]:
    """``memory/dream.py:split_session_context`` (the digest map step)."""
    cap = max(1, int(cap))
    if len(text) <= cap:
        return [text]
    parts, current, length = [], [], 0
    for line in text.split("\n"):
        while len(line) > cap:
            if current:
                parts.append("\n".join(current))
                current, length = [], 0
            parts.append(line[:cap])
            line = line[cap:]
        extra = len(line) + (1 if current else 0)
        if length + extra > cap and current:
            parts.append("\n".join(current))
            current, length = [], 0
            extra = len(line)
        current.append(line)
        length += extra
    if current:
        parts.append("\n".join(current))
    return parts


def fmt_signals(signals) -> str:
    """``memory/dream.py:_format_signals`` (``repr`` of the free text)."""
    lines = []
    for sg in signals:
        parts = [f"[{sg['outcome']}]", f"task={sg['task']!r}"]
        if sg.get("about"):
            parts.append(f"about={sg['about']!r}")
        if sg.get("detail"):
            parts.append(f"detail={sg['detail']!r}")
        if sg.get("polarity"):
            parts.append(f"polarity={sg['polarity']}")
        line = " ".join(parts)
        if sg.get("origin") == "inferred":
            line = f"[machine-inferred] {line}"
        lines.append(line)
    return "\n".join(lines)


def stage_reply(kind: str, user: str, payload) -> dict:
    return {"kind": kind, "user": user, "content": json.dumps(payload)}


def outcome_signal(task, result, **kw) -> dict:
    """A signal as memory_outcome stores it (origin ``action``)."""
    return {"task": task, "outcome": result, "about": kw.get("about"),
            "detail": kw.get("detail"), "polarity": kw.get("polarity"), "origin": "action"}


# Outcome inference: A infers two signals (an enum violation dropped, a lone
# surrogate refused by the insert, the cap of three reached), B answers
# malformed twice (held, then passed), C finds nothing, D already has a
# signal (no candidate), E's endpoint fails (the stage halts there).
INFER_A = [("Deployed the engine to host-a with the blue-green pipeline.", "status"),
           ("The smoke test passed after the engine deploy.", "status")]
INFER_B = [("Read the css grid guide end to end.", "status")]
INFER_C = [("Retried the flaky integration test twice.", "status")]
INFER_D = [("Audited the nightly backup job.", "status")]
INFER_E = [("Rotated the VPN gateway certificate.", "status")]
INFER_EXTRACTOR = {"model": "stub-model", "replies": [
    stage_reply("outcomes", session_context("Deploy engine", INFER_A), {"outcomes": [
        {"task": "deploy engine to host", "outcome": "success",
         "about": "blue-green pipeline", "detail": "The smoke test passed."},
        {"task": "bad enum", "outcome": "won"},
        {"task": "surrogate \ud83d task", "outcome": "success"},
        {"task": "  verify the deploy  ", "outcome": "failure", "about": "", "detail": None},
        {"task": "past the cap", "outcome": "success"}]}),
    {"kind": "outcomes", "user": session_context("Read docs", INFER_B, ["Skim the guide"]),
     "content": "not json at all"},
    stage_reply("outcomes", session_context("Fix flaky test", INFER_C), {"outcomes": []}),
    {"kind": "outcomes", "user": session_context("Rotate certs", INFER_E), "status": 500,
     "body": json.dumps({"error": "upstream failure"})},
]}

# Lesson synthesis, clustering: three signals (one crediting the entries a
# session search served) make two lessons and a lineage audit; the next
# signals supersede one and confirm the other; a claim whose about holds a
# lone surrogate fails its write, so its route stays pending.
LESSON_SIGNALS = [
    outcome_signal("deploy engine to host", "success", about="blue-green pipeline",
                   detail="the pipeline rollout worked"),
    outcome_signal("deploy engine to host", "failure", about="friday deploys",
                   detail="cache broke", polarity="-"),
    outcome_signal("trivia", "success"),
]
LESSON_SIGNALS_2 = [
    outcome_signal("deploy engine to host", "success", about="blue-green pipeline",
                   detail="rolled out again with a canary"),
    outcome_signal("deploy engine to host", "failure", about="friday deploys",
                   detail="broke again", polarity="-"),
]
LESSON_SIGNALS_3 = [outcome_signal("cache warmup", "failure", detail="cold start")]
# Batch failures: a claim whose compound about (no graph link) holds a lone
# surrogate writes, then fails the slot sync's entity lookup (psycopg's
# UnicodeEncodeError, its position within norm_name(about)); a claim whose
# lesson holds one is refused by the embedder's tokenizer (a TypeError).
# Both fail the whole batch: every signal stays pending.
LESSON_SIGNALS_4 = [outcome_signal("queue backlog", "success", about="redis + kafka",
                                   detail="drained")]
LESSON_SIGNALS_5 = [outcome_signal("ship hotfix", "success", detail="patched")]
APPROACH = {"task": "deploy engine to host", "aspect": "approach",
            "lesson": "Deploy through the blue-green pipeline.", "about": "blue-green pipeline",
            "polarity": "+", "outcome": "success", "confidence": 0.8}
PITFALL = {"task": "deploy engine to host", "aspect": "pitfall",
           "lesson": "Avoid Friday deploys.", "about": "friday deploys", "polarity": "-",
           "outcome": "failure", "confidence": 0.7}
LESSONS_EXTRACTOR = {"model": "stub-model", "replies": [
    stage_reply("lessons", fmt_signals(LESSON_SIGNALS), {"lessons": [APPROACH, PITFALL]}),
    stage_reply("lessons", fmt_signals(LESSON_SIGNALS_2), {"lessons": [
        dict(APPROACH, lesson="Deploy through the blue-green pipeline after a canary."),
        dict(PITFALL, confidence=0.9)]}),
    stage_reply("lessons", fmt_signals(LESSON_SIGNALS_3), {"lessons": [
        {"task": "cache warmup", "lesson": "Warm the cache first.",
         "about": "cache \ud83d"}]}),
    stage_reply("lessons", fmt_signals(LESSON_SIGNALS_3 + LESSON_SIGNALS_4), {"lessons": [
        {"task": "queue backlog", "aspect": "approach",
         "lesson": "Drain the backlog before scaling.", "about": "redis  +  kafka\ud83d"}]}),
    stage_reply("lessons", fmt_signals(LESSON_SIGNALS_3 + LESSON_SIGNALS_4 + LESSON_SIGNALS_5),
                {"lessons": [{"task": "ship hotfix", "aspect": "approach",
                              "lesson": "Patch \ud83d first.", "about": "hotfix"}]}),
]}

# Rule mode by the about prefix: one rule retried for a missing required
# value, one empty, one failing; and a plain signal on the clustering path.
RULE_SIGNALS = [
    outcome_signal("checkout refund", "success", about="rule: refund flow",
                   detail="user asked for a refund\nMUST INCLUDE: Alpha; beta"),
    outcome_signal("checkout cancel", "success", about="rule: cancel flow",
                   detail="cancel request"),
    outcome_signal("checkout retry", "failure", about="rule: retry flow",
                   detail="payment timeout"),
    outcome_signal("deploy engine", "success", about="pipeline"),
]
RULE_REFUND = {"task": "refund request", "lesson": "WHEN a refund is requested THEN use Alpha",
               "about": "rule: refund flow", "polarity": "+", "outcome": "success",
               "confidence": 0.9}
RULES_EXTRACTOR = {"model": "stub-model", "replies": [
    stage_reply("rules", fmt_signals(RULE_SIGNALS[:1]), {"lessons": [RULE_REFUND]}),
    stage_reply("rules", fmt_signals(RULE_SIGNALS[:1]) + "\n\nYour previous rule omitted "
                "these values, which must appear verbatim: beta. Reply again with the "
                "complete rule.", {"lessons": [
                    dict(RULE_REFUND, lesson="WHEN a refund is requested THEN use Alpha "
                                             "and beta")]}),
    stage_reply("rules", fmt_signals(RULE_SIGNALS[1:2]), {"lessons": []}),
    {"kind": "rules", "user": fmt_signals(RULE_SIGNALS[2:3]), "status": 500,
     "body": json.dumps({"error": "upstream failure"})},
    stage_reply("lessons", fmt_signals(RULE_SIGNALS[3:]), {"lessons": [
        {"task": "deploy engine", "aspect": "approach", "lesson": "Use the pipeline.",
         "about": "pipeline", "confidence": "0.75"}]}),
]}

# Inference feeding synthesis in one run: an all-inferred batch's lesson
# starts at confidence 0.4, its provenance naming the episode.
INFERRED_ENTRIES = [("Raised the cache TTL to 600 seconds; the hit rate improved.", "status")]
INFERRED_SIGNAL = {"task": "tune cache ttl", "outcome": "success", "about": "cache ttl",
                   "detail": "hit rate improved", "polarity": None, "origin": "inferred"}
INFERRED_EXTRACTOR = {"model": "stub-model", "replies": [
    stage_reply("outcomes", session_context("Tune the cache", INFERRED_ENTRIES), {"outcomes": [
        {k: INFERRED_SIGNAL[k] for k in ("task", "outcome", "about", "detail")}]}),
    stage_reply("lessons", fmt_signals([INFERRED_SIGNAL]), {"lessons": [
        {"task": "tune cache ttl", "aspect": "approach",
         "lesson": "Raise the TTL when the hit rate is low.", "about": "cache ttl",
         "polarity": "+", "outcome": "success", "confidence": 0.9}]}),
]}

# Digests: a short session in one call; a long one split over the 100-char
# context and reduced; one whose reply echoes the prompt (malformed: held,
# then passed).
DIGEST_CHARS = 100
DIGEST_A = [("Tagged release 1.4.0.", "status"), ("Published the changelog.", "status")]
DIGEST_B = [("Planned the migration from PostgreSQL 15 to 17 for billing.", "status"),
            ("Copied the billing tables to the new cluster overnight.", "status"),
            ("Switched billing to the new cluster and verified reads.", "status")]
DIGEST_C = [("Removed stale feature flags.", "status")]
DIGEST_B_CONTEXT = session_context("Migrate database", DIGEST_B, ["Copy data"])
DIGEST_B_PARTS = split_context(DIGEST_B_CONTEXT, DIGEST_CHARS)
DIGEST_B_SEGS = [f"Part {i + 1} of the migration session." for i in range(len(DIGEST_B_PARTS))]
DIGESTS_EXTRACTOR = {"model": "stub-model", "replies": [
    stage_reply("digest", session_context("Release prep", DIGEST_A), {
        "digest": "In this session the user tagged release 1.4.0 and published the changelog."}),
    *[stage_reply("digest", part, {"digest": f"  {seg}  "})
      for part, seg in zip(DIGEST_B_PARTS, DIGEST_B_SEGS)],
    stage_reply("digest", "\n\n".join(DIGEST_B_SEGS), {
        "digest": "In this session the billing database moved from PostgreSQL 15 to 17."}),
    stage_reply("digest", session_context("Clean up", DIGEST_C), {
        "digest": "It was written in past tense anchored to the session."}),
]}

SCENARIOS.update({
    "dream-stages-infer": {
        "config": stages_config("    synthesize_in_dream: false\n"
                                "    infer_outcomes_max_signals: 3\n"),
        "extractor": INFER_EXTRACTOR,
        "seed": [
            *[store(t, [sess("inf-a")], source=src) for t, src in INFER_A],
            session_title("Deploy engine", [sess("inf-a")]),
            ep_start("Skim the guide", [sess("inf-b")]),
            *[store(t, [sess("inf-b")], source=src) for t, src in INFER_B],
            ep_end([sess("inf-b")]),
            session_title("Read docs", [sess("inf-b")]),
            *[store(t, [sess("inf-c")], source=src) for t, src in INFER_C],
            session_title("Fix flaky test", [sess("inf-c")]),
            *[store(t, [sess("inf-d")], source=src) for t, src in INFER_D],
            outcome("audit backups", "success", [sess("inf-d")]),
            *[store(t, [sess("inf-e")], source=src) for t, src in INFER_E],
            session_title("Rotate certs", [sess("inf-e")]),
            close_root("inf-a", 1700000100.0), close_root("inf-b", 1700000200.0),
            close_root("inf-c", 1700000300.0), close_root("inf-d", 1700000350.0),
            close_root("inf-e", 1700000400.0),
        ],
        "ops": [dream("status"), dream("run"), dream("run"), dream("run"), dream("status")],
    },
    "dream-stages-lessons": {
        "config": stages_config("    infer_outcomes: false\n"
                                "    synthesis_dedup_min_similarity: 0\n"),
        "extractor": LESSONS_EXTRACTOR,
        "seed": [
            store("The warmup probe runs once when the daemon starts.", source="status"),
            store("The engine deploys through the blue-green pipeline.", [sess("les-a")],
                  source="status"),
            store("Friday deploys broke the cache twice.", [sess("les-a")], source="status"),
            search("blue-green pipeline deploys", [sess("les-a")], top_k=3),
            *[outcome(sg["task"], sg["outcome"], [sess("les-a")],
                      **{k: sg[k] for k in ("about", "detail", "polarity") if sg[k]},
                      **({"used_ids": [2, 3]} if i == 0 else {}))
              for i, sg in enumerate(LESSON_SIGNALS)],
        ],
        "ops": [
            dream("run"),
            dream("run"),
            *[outcome(sg["task"], sg["outcome"], [sess("les-a")],
                      **{k: sg[k] for k in ("about", "detail", "polarity") if sg[k]},
                      **({"used_ids": [2]} if i == 0 else {}))
              for i, sg in enumerate(LESSON_SIGNALS_2)],
            dream("run"),
            outcome("cache warmup", "failure", [sess("les-a")], detail="cold start"),
            dream("run"),
            dream("run"),
            dream("status"),
            outcome("queue backlog", "success", [sess("les-a")], about="redis + kafka",
                    detail="drained"),
            dream("run"),
            outcome("ship hotfix", "success", [sess("les-a")], detail="patched"),
            dream("run"),
            dream("status"),
        ],
    },
    "dream-stages-rules": {
        "config": stages_config("    infer_outcomes: false\n"
                                "    synthesis_dedup_min_similarity: 0\n"),
        "extractor": RULES_EXTRACTOR,
        "seed": [
            store("A note so the bank is not empty.", source="status"),
            *[outcome(sg["task"], sg["outcome"],
                      **{k: sg[k] for k in ("about", "detail", "polarity") if sg[k]})
              for sg in RULE_SIGNALS],
        ],
        "ops": [dream("run"), dream("run")],
    },
    "dream-stages-inferred": {
        "config": stages_config("    synthesis_dedup_min_similarity: 0\n"),
        "extractor": INFERRED_EXTRACTOR,
        "seed": [
            *[store(t, [sess("inl-a")], source=src) for t, src in INFERRED_ENTRIES],
            session_title("Tune the cache", [sess("inl-a")]),
            close_root("inl-a", 1700000100.0),
        ],
        "ops": [dream("run"), dream("run")],
    },
    "dream-stages-digests": {
        "config": stages_config("    enabled: false\n",
                                "    digest_enabled: true\n"
                                f"    digest_context_chars: {DIGEST_CHARS}\n"
                                "    digest_target_chars: 300\n"),
        "extractor": DIGESTS_EXTRACTOR,
        "seed": [
            *[store(t, [sess("dig-a")], source=src) for t, src in DIGEST_A],
            session_title("Release prep", [sess("dig-a")]),
            store(DIGEST_B[0][0], [sess("dig-b")], source=DIGEST_B[0][1]),
            ep_start("Copy data", [sess("dig-b")]),
            *[store(t, [sess("dig-b")], source=src) for t, src in DIGEST_B[1:]],
            ep_end([sess("dig-b")]),
            session_title("Migrate database", [sess("dig-b")]),
            *[store(t, [sess("dig-c")], source=src) for t, src in DIGEST_C],
            session_title("Clean up", [sess("dig-c")]),
            close_root("dig-a", 1700000100.0), close_root("dig-b", 1700000200.0),
            close_root("dig-c", 1700000300.0),
        ],
        "ops": [dream("status"), dream("run"), dream("run"), dream("run"), dream("status")],
    },
})


# memory_dream deep (P2a): the deep dream, dry run and apply, compared
# answer by answer and bank by bank, plus each side's newest graph snapshot
# file (the ``snapshot_file`` op reads both daemons' data dirs). The graph
# is seeded through the oracle's own tools (relate, store, fact_set,
# outcome, world_set, the review tool's propose and dismiss_pair); declared
# SQL seeds stand for what only the dream or the reviewer writes: junk
# tombstones and a kept-junk dismissal, pending merge and junk proposals
# with judge votes, scope attribution, a memory trace, lessons with fixed
# embeddings, and entities whose evidence a forget or merge removed (the
# orphan census; their created_at is a fixed old stamp, so the default
# seven-day age bar admits only them). The dream's background tick and the
# shallow sweep are off. min_similarity -1 lists every scope-coherent pair,
# so candidate order rests on the float32 cosines both banks compute over
# the same stored vectors.
DEEP_DREAM_CONFIG = (
    "memory:\n"
    "  dream:\n    enabled: false\n"
    "  scopes:\n"
    "    exclude: [status, claude, agent, correction, noise]\n"
    "    rollup:\n      proj-a: family\n"
    "  deep_dream:\n"
    "    auto_tick: false\n"
    "    min_similarity: -1.0\n"
    "    merge_min_similarity: -1.0\n"
    "    min_entity_mentions: 1\n"
    "    top_k_candidates: 25\n"
    "    curation_min_similarity: -1.0\n"
    "    curation_top_k: 8\n"
)


def deep_config(extra: str = "") -> str:
    return DEEP_DREAM_CONFIG + extra


def snapshot_file() -> dict:
    """Each side's newest ``graph_snapshots/graph-*.json``, normalized."""
    return {"op": "snapshot_file"}


def block_snapshots() -> dict:
    """A plain file where each side's ``graph_snapshots`` directory goes."""
    return {"op": "block_snapshots"}


def deep_lesson(task, aspect, lesson, seed) -> dict:
    return slot_row_sql("lesson", slot_lesson(task, aspect, lesson,
                                              embedding=slot_embedding(seed)))


DEEP_NOTES = [
    ("The Kafka Broker cluster feeds the Harbor Registry nightly.", "proj-a"),
    ("Broker Kafka handles retries for the billing-service.", "proj-a"),
    ("Tide Gauge readings land in the Ocean warehouse.", "proj-b"),
    ("Kelp Farm sensors report to the Tide Gauge hub.", "proj-b"),
    ("The orbit station alpha two telemetry is noisy.", "noise"),
    ("orbit station alpha docking schedule changed.", "conversation"),
    ("deploy runbook 2026-10-01 final covers the rollback.", "proj-a"),
    ("The deploy runbook lives in the ops wiki.", "Proj-A"),
    ("payments ledger prod reconciles every night.", "proj-b"),
    ("payments ledger exports to the Warehouse.", "proj-b"),
    ("tidecalc.py computes the tide tables; tidecalc is the module.", "proj-b"),
    ("Mentioned Orphan shows up in this note.", "status"),
    ("The deploy agent pushes images to the Harbor Registry.", "proj-a"),
    ("postgres backs the billing-service ledger.", "proj-a"),
]

DEEP_SEED = [
    *[store(t, source=s) for t, s in DEEP_NOTES],
    # rescore (an agent edge stored at 0.8) and a hard violation
    relate("deploy agent", "uses", "Harbor Registry", origin="agent"),
    relate("billing-service", "runs-on", "postgres", origin="agent"),
    relate("Warehouse", "related-to", "Ocean", confidence=0.45, origin="agent"),
    # an exact duplicate (same token set) and a name-contained third
    relate("Kafka Broker", "uses", "Harbor Registry"),
    relate("Broker Kafka", "related-to", "billing-service"),
    relate("Kafka Broker Cluster", "related-to", "Warehouse"),
    relate("Tide Gauge", "related-to", "Ocean"),
    relate("Kelp Farm", "related-to", "Ocean"),
    relate("orbit station alpha", "related-to", "Ocean"),
    relate("orbit station alpha two", "related-to", "Ocean"),
    # a name-contained pair the event-slug veto keeps out of the merge queue
    relate("deploy runbook 2026-10-01 final", "related-to", "Warehouse"),
    relate("deploy runbook", "related-to", "Warehouse"),
    # analyzer duplicates: a merge pair and a file and its stem
    relate("payments ledger", "related-to", "Warehouse"),
    relate("payments ledger prod", "related-to", "Warehouse"),
    relate("tidecalc.py", "related-to", "Ocean"),
    relate("tidecalc", "related-to", "Ocean"),
    # junk: zero structure (one fact), tombstoned at degree 1 and 2, a junk
    # name with two facts, a kept name
    fact("QQ", "color", "blue"),
    relate("ZZ", "related-to", "Ocean"),
    relate("alpha, beta", "related-to", "Ocean"),
    relate("alpha, beta", "related-to", "Warehouse"),
    fact("WW", "color", "red"),
    fact("WW", "size", "large"),
    relate("KK", "related-to", "Ocean"),
    # a fact naming an orphan-shaped entity, lesson-minted nodes
    fact("Named Orphan", "status", "parked"),
    outcome("ship the release", "success", about="release checklist"),
    # world duplicates across entities, identifier-keyed siblings
    world_set("Host A", "os", "Debian 12", source_url="https://example.org/a"),
    world_set("Host B", "os", "Debian 12", source_url="https://example.org/b"),
    world_set("Host A", "distro", "Debian bookworm", source_url="https://example.org/a"),
    world_set("arxiv:2602-05665", "title", "Graph memory", source_url="https://arxiv.org/a"),
    world_set("arxiv:2602-05666", "title", "Graph memory", source_url="https://arxiv.org/b"),
    # a pending link proposal (its pair leaves the candidates) and a
    # dismissed pair
    review("propose", proposals=[{"src": "Tide Gauge", "relation": "related-to",
                                  "dst": "Harbor Registry"}]),
    review("dismiss_pair", src="Kelp Farm", dst="Harbor Registry"),
    trace_sql(DEEP_NOTES[12][0], "harbor-registry", "receives"),
    # lessons with fixed embeddings: a near-verbatim cross-key pair, aspect
    # siblings, a dismissed pair
    deep_lesson("Rotate Keys", "Cadence", "Rotate keys quarterly.", 3),
    deep_lesson("Rotate Secrets", "Cadence", "Rotate secrets quarterly.", 3),
    deep_lesson("Rotate Keys", "Owner", "The security team owns rotation.", 5),
    deep_lesson("Ship Release", "Checklist", "Run the checklist.", 7),
    deep_lesson("Ship Release", "Freeze", "Freeze merges a day ahead.", 7),
    sql("INSERT INTO dismissed_pairs (a_norm, b_norm, dismissed_at) VALUES "
        "('lesson:rotate-keys|owner', 'lesson:ship-release|checklist', 1), "
        "('junk:kk', 'junk:kk', 1)", after_stop=True),
    # junk tombstones (accepted junk verdicts), one with a NULL display
    # (norm_name(None) is "", the norm of the "-" entity below: at degree 1
    # only that tombstone lets the apply delete it)
    sql("INSERT INTO merge_decisions (proposal_id, entity_display, into_display, status, score, "
        "reason, decided_by, decided_at) VALUES (NULL, 'ZZ', NULL, 'accepted', NULL, 'junk: x', "
        "'agent', 1), (NULL, 'alpha, beta', NULL, 'accepted', NULL, 'junk: y', 'agent', 2), "
        "(NULL, NULL, NULL, 'accepted', NULL, 'junk: z', 'agent', 3)", after_stop=True),
    sql("INSERT INTO entities (canonical, display, etype, created_at) VALUES "
        "('dash-node', '-', NULL, 1700000000.0)", after_stop=True),
    sql("INSERT INTO edges (src_id, relation, dst_id, confidence, origin, asserted_at) VALUES "
        f"({eid('dash-node')}, 'related-to', {eid('ocean')}, 0.8, NULL, 1.0)", after_stop=True),
    # scope attribution (only the dream writes it): disjoint scopes drop a pair
    sql("INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
        f"VALUES ({eid('harbor-registry')}, 'proj-a', 1, 'derived', 0), "
        f"({eid('ocean')}, 'proj-b', 1, 'derived', 0), "
        f"({eid('kafka-broker')}, 'proj-a', 1, 'manual', 0)", after_stop=True),
    # pending proposals (only the dream files them): merges with judge votes
    # sharing an endpoint, an already-proposed merge, a pending junk row
    sql("INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, created_at, "
        "judge_verdict, judge_confidence, judge_note, judge_model, judged_at) VALUES "
        f"('merge', {eid('tide-gauge')}, {eid('kelp-farm')}, 0.71, 'write-dedup: a', 1, "
        "'relate', 0.8, 'relate:related-to distinct sensors', 'judge-a', 2), "
        f"('merge', {eid('tide-gauge')}, {eid('ocean')}, 0.6, 'write-dedup: b', 1, "
        "'reject', 0.9, 'different', 'judge-a', 2), "
        f"('merge', {eid('orbit-station-alpha-two')}, {eid('orbit-station-alpha')}, NULL, "
        "'write-dedup: c', 1, NULL, NULL, NULL, NULL, NULL)", after_stop=True),
    sql("INSERT INTO entity_proposals (kind, entity_id, score, reason, created_at) VALUES "
        f"('junk', {eid('ww')}, NULL, 'too-short', 1)", after_stop=True),
    # entities whose evidence is gone: two unmentioned (the census and the
    # sweep's cap), one an entry mentions, one a fact names
    sql("INSERT INTO entities (canonical, display, etype, created_at) VALUES "
        "('orphan-node', 'Orphan Node', NULL, 1700000000.0), "
        "('orphan-two', 'Orphan Two', NULL, 1700000100.0), "
        "('mentioned-orphan', 'Mentioned Orphan', NULL, 1700000000.0), "
        "('named-orphan-x', 'Named Orphan', NULL, 1700000000.0)", after_stop=True),
]

# Long notes (about 6 KB each) for the response bound: every candidate
# carries up to three snippets a side at snippet_max_chars 100000.
DEEP_LONG = [
    (f"{name} long note {i}: " + " ".join(f"{word}{i}x{j}" for j in range(700)), "proj-a")
    for i, (name, word) in enumerate([
        ("Alder Node", "alder"), ("Birch Node", "birch"), ("Cedar Node", "cedar"),
        ("Dogwood Node", "dogwood"), ("Elm Node", "elm"), ("Fir Node", "fir"),
        ("Alder Node and Birch Node", "pair"), ("Cedar Node with Elm Node", "duo")])
]

# Junk-owned and lesson-only entities WITH context vectors (a note mentions
# each), on a graph small enough that every candidate pair fits top_k: the
# exclusion alone keeps them out of the candidates.
DEEP_EXCLUSION_NOTES = [
    ("Harbor Crane lifts containers at dawn.", "proj-a"),
    ("Tide Gauge readings land in the warehouse.", "proj-a"),
    ("Kelp Farm sensors report every hour.", "proj-a"),
    ("The alpha, beta rollout reached the fleet.", "proj-a"),
    ("The release checklist gained a smoke test step.", "proj-a"),
    ("The backup window moved to Sunday night.", "proj-a"),
]

DEEP_EXCLUSION_SEED = [
    *[store(t, source=s) for t, s in DEEP_EXCLUSION_NOTES],
    relate("Harbor Crane", "related-to", "Ocean"),
    relate("Tide Gauge", "related-to", "Ocean"),
    relate("Kelp Farm", "related-to", "Ocean"),
    # junk at any degree (a list artifact)
    relate("alpha, beta", "related-to", "Ocean"),
    # lesson-only, both ways: "release checklist" has only a prefers edge
    # (as a lesson's task-type link mints it); "Backup Window" is a lesson's
    # entity_id
    sql("INSERT INTO entities (canonical, display, etype, created_at) VALUES "
        "('ship-the-release', 'ship the release', NULL, 1700000000.0), "
        "('release-checklist', 'release checklist', NULL, 1700000000.0), "
        "('backup-window', 'Backup Window', NULL, 1700000000.0)", after_stop=True),
    sql("INSERT INTO edges (src_id, relation, dst_id, confidence, origin, asserted_at) VALUES "
        f"({eid('ship-the-release')}, 'prefers', {eid('release-checklist')}, 0.7, 'action', 1.0)",
        after_stop=True),
    deep_lesson("Backup Window", "Day", "Sunday night.", 9),
    sql(f"UPDATE lessons SET entity_id = {eid('backup-window')} WHERE attribute = 'Day'",
        after_stop=True),
]

DEEP_BOUND_SEED = [
    *[store(t, source=s) for t, s in DEEP_LONG],
    *[relate(n, "related-to", "Hub") for n in
      ("Alder Node", "Birch Node", "Cedar Node", "Dogwood Node", "Elm Node", "Fir Node")],
]

SCENARIOS.update({
    "dream-deep-dry": {
        "config": deep_config(),
        "seed": DEEP_SEED,
        "ops": [
            dream("deep"),
            dream("deep", snippets=False),
            dream("deep", apply=False, snippets=True),
            dream("status"),
        ],
    },
    "dream-deep-apply": {
        "config": deep_config("    orphan_sweep: true\n    orphan_max_per_apply: 1\n"
                              "    snapshot_keep: 1\n"),
        "seed": DEEP_SEED,
        "ops": [
            dream("deep", apply=True),
            snapshot_file(),
            dream("status"),
            dream("deep"),
            {"op": "sleep", "s": 1.2},
            dream("deep", apply=True, snippets=False),
            snapshot_file(),
            review(),
        ],
    },
    "dream-deep-apply-cautious": {
        "config": deep_config("    auto_apply_safe: false\n"
                              "    analyzer_file_duplicates: false\n"),
        "seed": DEEP_SEED,
        "ops": [dream("deep", apply=True), snapshot_file(), dream("status")],
    },
    "dream-deep-bound": {
        "config": deep_config("    snippet_max_chars: 100000\n    top_k_candidates: 50\n"),
        "seed": DEEP_BOUND_SEED,
        "ops": [dream("deep"), dream("deep", snippets=False)],
    },
    "dream-deep-snapshot-failed": {
        "config": deep_config(),
        "seed": DEEP_SEED,
        "ops": [block_snapshots(), dream("deep", apply=True), dream("status"), dream("deep")],
    },
    "dream-deep-exclusion": {
        "config": deep_config(),
        "seed": DEEP_EXCLUSION_SEED,
        "ops": [dream("deep"), dream("deep", apply=True, snippets=False)],
    },
})


# ── the merge judge (deep_dream_judge, run from the sweep) ────────────────
#
# The judge runs on the sweep timer, never from a call: each scenario seeds
# notes and entities through the Python write path, inserts its pending
# merge proposals out of band (only the dream files them) and compares
# both banks and both request sequences once each side's judge is idle
# (``settle``). Dreaming is enabled for the sweep's judge step; every other
# step a tick would run is switched off by config: the analyzer filing and
# the deep tick (P4), the link, junk, curation and candidate judges (P3b),
# and the automatic dream (an unreachable min_batch and idle time, no
# outcome inference). One stub serves both daemons (``shared_stub``), so
# the fingerprints over each endpoint's URL agree.

JUDGE_NOTES = [
    "Alpha Node runs the nightly batch on rack one.",
    "Alpha Node reboots every Sunday morning.",
    "The Alpha Service exposes the billing API to partners.",
    "Alpha Service writes its audit logs to the archive.",
    "Beta Node caches the search index in memory.",
    "The Beta Store keeps the order history in Postgres.",
    "Gamma Node schedules the render queue.",
    "The Gamma Tool converts the render output to video.",
    "Delta Node mirrors the package registry.",
    "The Delta Unit measures the cooling loop pressure.",
    "Llama 26B answers the long questions.",
    "Llama 4B answers the quick questions.",
    "Epsilon Node backs up the wiki.",
    "The Epsilon Box stores the spare drives.",
]
JUDGE_ENTITIES = ["Alpha Node", "Alpha Service", "Beta Node", "Beta Store", "Gamma Node",
                  "Gamma Tool", "Delta Node", "Delta Unit", "Llama 26B", "Llama 4B",
                  "Epsilon Node", "Epsilon Box"]
JUDGE_SEED = [
    *[store(t, source="proj-a") for t in JUDGE_NOTES],
    *[relate(n, "related-to", "Hub") for n in JUDGE_ENTITIES],
    # a pair settled as distinct before any judge saw it
    sql("INSERT INTO dismissed_pairs (a_norm, b_norm, dismissed_at) VALUES "
        "('epsilon-box', 'epsilon-node', 1)", after_stop=True),
]

# The proposals, by pair; the score orders the queue.
JUDGE_PAIRS = {
    "A": ("alpha-node", "alpha-service", 0.95), "B": ("beta-node", "beta-store", 0.9),
    "G": ("gamma-node", "gamma-tool", 0.85), "D": ("delta-node", "delta-unit", 0.8),
    "L": ("llama-26b", "llama-4b", 0.75), "E": ("epsilon-node", "epsilon-box", 0.7),
}
JUDGE_KEYS = {"A": "Alpha Node|Alpha Service", "B": "Beta Node|Beta Store",
              "G": "Gamma Node|Gamma Tool", "D": "Delta Node|Delta Unit",
              "L": "Llama 26B|Llama 4B", "E": "Epsilon Node|Epsilon Box"}
JUDGE_SETTLE = {"quiet_s": 3.0, "timeout_s": 240.0}
JUDGE_ALL_FIRST = ("SELECT NOT EXISTS (SELECT 1 FROM entity_proposals "
                   "WHERE status = 'pending' AND judge_verdict IS NULL)")


def judge_config(mode="shadow", tiebreak="off", second="judge-two", third=None, extra=""):
    """The merge judge's config: second and third opinions on the shared
    stub's own endpoints, every other sweep step off."""
    text = ("memory:\n"
            "  dream:\n    enabled: true\n    sweep_interval_seconds: 0.25\n"
            "    min_batch: 1000000\n    idle_seconds: 1000000000\n"
            "  lessons:\n    infer_outcomes: false\n"
            "  deep_dream:\n"
            "    auto_tick: false\n    analyzer_file_duplicates: false\n"
            "    link_judge_mode: \"off\"\n    junk_judge_mode: \"off\"\n"
            "    curation_judge_mode: \"off\"\n    candidate_judge_mode: \"off\"\n"
            f"    judge_mode: \"{mode}\"\n    judge_tiebreak_mode: \"{tiebreak}\"\n")
    if second:
        text += f"    judge_second_url: \"@STUB@/second/v1\"\n    judge_second_model: \"{second}\"\n"
    if third:
        text += f"    judge_third_url: \"@STUB@/third/v1\"\n    judge_third_model: \"{third}\"\n"
    return text + extra


def V(verdict, confidence, relation=None, note=None) -> dict:
    """One stub verdict."""
    out = {"verdict": verdict, "confidence": confidence}
    if relation is not None:
        out["relation"] = relation
    if note is not None:
        out["note"] = note
    return out


def judge_stub(first, second=None, third=None, *, reply_model="judge-one",
               second_model="judge-two", third_model="judge-three", extra=()) -> dict:
    """A stub answering each endpoint's judge request by pair (keys of
    ``JUDGE_KEYS``); ``extra`` entries answer first (an erroring call)."""
    def by_pair(table):
        return {JUDGE_KEYS[k]: v for k, v in table.items()}
    replies = list(extra) + [{"kind": "judge_merges", "verdicts": by_pair(first)}]
    if second is not None:
        replies.append({"kind": "judge_merges", "endpoint": "second", "verdicts": by_pair(second)})
    if third is not None:
        replies.append({"kind": "judge_merges", "endpoint": "third", "verdicts": by_pair(third)})
    spec = {"model": "judge-one", "reply_model": reply_model,
            "second_reply_model": second_model, "third_reply_model": third_model,
            "replies": replies}
    return spec


def judge_insert(*pairs, until=JUDGE_ALL_FIRST) -> dict:
    """The pending merge proposals, inserted on both banks at once, then
    the judges' settle."""
    rows = ", ".join(
        f"('merge', {eid(JUDGE_PAIRS[p][0])}, {eid(JUDGE_PAIRS[p][1])}, "
        f"{JUDGE_PAIRS[p][2]}, 'write-dedup: {p.lower()}', 1.0)" for p in pairs)
    op = external("INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, "
                  f"created_at) VALUES {rows}")
    op["settle"] = {**JUDGE_SETTLE, "until": until}
    return op


def judge_evidence_change(entity: str) -> dict:
    """A new edge on ``entity`` (its degree moves), then the settle."""
    op = external("INSERT INTO edges (src_id, relation, dst_id, confidence, origin, asserted_at) "
                  f"VALUES ({eid(entity)}, 'uses', {eid('hub')}, 0.8, NULL, 1.0)")
    op["settle"] = dict(JUDGE_SETTLE)
    return op


def judge_scenario(config: str, stub: dict, ops: list) -> dict:
    return {"config": config, "seed": JUDGE_SEED, "extractor": stub, "shared_stub": True,
            "ops": ops}


def expect(op: dict, *queries: str) -> dict:
    """``op`` with queries that must answer true on both banks after it."""
    return {**op, "expect": [*op.get("expect", ()), *queries]}


def ep(pair: str, cond: str) -> str:
    """The pair's proposal (by its ``judge_insert`` reason) satisfies ``cond``."""
    return ("SELECT EXISTS (SELECT 1 FROM entity_proposals "
            f"WHERE reason = 'write-dedup: {pair.lower()}' AND {cond})")


def judged_by_dream(pair: str, status: str) -> str:
    return ep(pair, f"status = '{status}' AND decided_by = 'dream-judge'")


def folded(pair: str) -> str:
    """An automatic fold of the pair, audited in ``merge_decisions``."""
    a, b = JUDGE_KEYS[pair].split("|")
    return ("SELECT EXISTS (SELECT 1 FROM merge_decisions WHERE status = 'accepted' "
            f"AND decided_by = 'dream-judge' AND '{a}' IN (entity_display, into_display) "
            f"AND '{b}' IN (entity_display, into_display))")


def relate_link(relation: str) -> str:
    """A link proposal the merge judge filed from a relate vote."""
    return ("SELECT EXISTS (SELECT 1 FROM edge_proposals WHERE source = 'merge-judge-relate' "
            f"AND relation = '{relation}')")


def decisions_for(pair: str, status: str, n: int) -> str:
    """Exactly ``n`` ``merge_decisions`` rows of ``status`` for the pair."""
    a, b = JUDGE_KEYS[pair].split("|")
    return (f"SELECT count(*) = {n} FROM merge_decisions WHERE status = '{status}' "
            f"AND '{a}' IN (entity_display, into_display) "
            f"AND '{b}' IN (entity_display, into_display)")


def pending(*pairs: str) -> list[str]:
    return [ep(p, "status = 'pending'") for p in pairs]


def judge_restart(config: str, until: str | None = None) -> dict:
    """Both daemons restarted on ``config``, then the judges' settle."""
    op = {"op": "restart", "config": config, "settle": dict(JUDGE_SETTLE)}
    if until:
        op["settle"]["until"] = until
    return op


def three_votes(*pairs: str) -> str:
    reasons = ", ".join(f"'write-dedup: {p.lower()}'" for p in pairs)
    return ("SELECT NOT EXISTS (SELECT 1 FROM entity_proposals WHERE reason IN "
            f"({reasons}) AND judge3_verdict IS NULL)")


# A lone surrogate, as a reply's JSON escapes it.
LONE = chr(0xD83D)
REFUSED_SECOND = "tiebreak needs the second vote''s served model"


SCENARIOS.update({
    # Shadow: every verdict is recorded, nothing applies; a split row gets
    # its third opinion; an evidence change clears a row's verdicts, which
    # are asked again.
    "judge-merge-shadow": judge_scenario(
        judge_config("shadow", "shadow", third="judge-three"),
        judge_stub({"A": V("accept", 0.9, note="same service"),
                    "B": V("reject", 0.9, note="different roles"),
                    "G": V("accept", 0.7)},
                   {"A": V("accept", 0.8), "B": V("reject", 0.85, relation="part-of"),
                    "G": V("reject", 0.8), "D": V("accept", 0.6)},
                   {"G": V("reject", 0.9), "D": V("leave", 0.5)}),
        [expect(judge_insert("A", "B", "G", "D"),
                ep("G", "judge_note LIKE '%| 3rd (judge-three): reject 0.90 [2-1 reject]'"),
                *pending("A", "B", "G", "D")),
         judge_evidence_change("alpha-node"), review()]),
    # auto-reject: a single reject at the bar applies (and files its
    # relate link), two distinct rejects at the mean apply, a split and a
    # mean below the bar do not.
    "judge-merge-auto-reject": judge_scenario(
        judge_config("auto-reject"),
        judge_stub({"A": V("reject", 0.8, relation="uses", note="a host and a service"),
                    "B": V("reject", 0.79), "G": V("accept", 0.9), "D": V("reject", 0.6)},
                   {"B": V("reject", 0.7), "G": V("reject", 0.9), "D": V("reject", 0.6)}),
        [expect(judge_insert("A", "B", "G", "D"),
                judged_by_dream("A", "rejected"), relate_link("uses"),
                judged_by_dream("B", "rejected"), *pending("G", "D")),
         review()]),
    # auto: two distinct accepts on differential evidence fold (snapshot
    # first), at the bar too; a name veto refuses; a dismissed pair's row
    # is closed instead.
    "judge-merge-auto": judge_scenario(
        judge_config("auto", extra="    snapshot_keep: 1\n"),
        judge_stub({"A": V("accept", 0.9), "L": V("accept", 0.9), "E": V("accept", 0.9),
                    "B": V("accept", 0.6)},
                   {"A": V("accept", 0.8), "L": V("accept", 0.8), "E": V("accept", 0.8),
                    "B": V("accept", 0.6)}),
        [expect(judge_insert("A", "B", "L", "E"),
                folded("A"), folded("B"), judged_by_dream("E", "rejected"),
                ep("L", "status = 'pending' AND judge_note LIKE "
                        "'%| auto-accept refused: name veto'")),
         snapshot_file(), review()]),
    # The second endpoint is configured with the first's model name: its
    # votes authorize neither a reject nor a fold.
    "judge-merge-same-model": judge_scenario(
        judge_config("auto", second="judge-one"),
        judge_stub({"A": V("accept", 0.9), "B": V("reject", 0.75)},
                   {"A": V("accept", 0.9), "B": V("reject", 0.9)},
                   second_model="judge-one-b"),
        [expect(judge_insert("A", "B"),
                ep("A", "judge_note LIKE '%| auto-accept needs a distinct second model'"),
                ep("B", "judge_note LIKE '%| auto-reject needs a distinct second model'"),
                *pending("A", "B")),
         review()]),
    # The second endpoint asked for judge-two serves the first's model.
    "judge-merge-served-mismatch": judge_scenario(
        judge_config("auto-reject"),
        judge_stub({"B": V("reject", 0.75), "D": V("reject", 0.75)},
                   {"B": V("reject", 0.9), "D": V("reject", 0.75)},
                   second_model="judge-one"),
        [expect(judge_insert("B", "D"),
                ep("B", "judge_note LIKE '%| auto-reject needs a distinct second model'"),
                ep("D", "judge_note LIKE '%| auto-reject needs a distinct second model'"),
                *pending("B", "D")),
         review()]),
    # The first endpoint names no served model: the two-vote reject is
    # refused for want of the first vote's identity.
    "judge-merge-unknown-served": judge_scenario(
        judge_config("auto-reject"),
        judge_stub({"B": V("reject", 0.75)}, {"B": V("reject", 0.9)}, reply_model=None),
        [expect(judge_insert("B"),
                ep("B", "status = 'pending' AND judge_note LIKE "
                        "'%| auto-reject needs a distinct second model'")),
         review()]),
    # The tiebreak in auto: a 2-1 reject applies (filing the third vote's
    # relation), a 2-1 accept under auto-reject does not, no majority
    # leaves the row.
    "judge-merge-tiebreak": judge_scenario(
        judge_config("auto-reject", "auto", third="judge-three"),
        judge_stub({"G": V("accept", 0.8), "B": V("reject", 0.7), "D": V("accept", 0.9)},
                   {"G": V("reject", 0.8), "B": V("accept", 0.9), "D": V("reject", 0.9)},
                   {"G": V("reject", 0.9, relation="part-of"), "B": V("accept", 0.8),
                    "D": V("leave", 0.5)}),
        [expect(judge_insert("G", "B", "D"),
                judged_by_dream("G", "rejected"), relate_link("part-of"),
                ep("B", "status = 'pending' AND judge_note LIKE '%[2-1 accept]'"),
                ep("D", "status = 'pending' AND judge_note LIKE '%[no majority]'")),
         review()]),
    # The third endpoint serves the first's model: the tiebreak is refused.
    "judge-merge-tiebreak-same-model": judge_scenario(
        judge_config("auto-reject", "auto", third="judge-three"),
        judge_stub({"G": V("accept", 0.8)}, {"G": V("reject", 0.8)}, {"G": V("reject", 0.9)},
                   third_model="judge-one"),
        [expect(judge_insert("G"),
                ep("G", "status = 'pending' AND judge_note LIKE "
                        "'%| tiebreak needs a distinct third model'")),
         review()]),
    # The second endpoint named no served model: the tiebreak is refused.
    "judge-merge-tiebreak-unknown-second": judge_scenario(
        judge_config("auto-reject", "auto", third="judge-three"),
        judge_stub({"G": V("accept", 0.8)}, {"G": V("reject", 0.8)}, {"G": V("reject", 0.9)},
                   second_model=None),
        [expect(judge_insert("G"),
                ep("G", f"status = 'pending' AND judge_note LIKE '%| {REFUSED_SECOND}'")),
         review()]),
    # The third model is configured as the first's: no third call.
    "judge-merge-tiebreak-skip": judge_scenario(
        judge_config("auto-reject", "auto", third="judge-one"),
        judge_stub({"G": V("accept", 0.8)}, {"G": V("reject", 0.8)}, {"G": V("reject", 0.9)}),
        [expect(judge_insert("G"),
                ep("G", "status = 'pending' AND judge2_verdict IS NOT NULL "
                        "AND judge3_verdict IS NULL")),
         review()]),
    # The second and the third endpoints each fail once: the first opinions
    # go on in the same tick, and the failed opinions are asked again.
    "judge-merge-endpoint-errors": judge_scenario(
        judge_config("shadow", "shadow", third="judge-three",
                     extra="    judge_batch: 2\n"),
        judge_stub({"A": V("accept", 0.9), "B": V("reject", 0.9), "G": V("accept", 0.8)},
                   {"A": V("accept", 0.8), "B": V("accept", 0.7), "G": V("reject", 0.9)},
                   {"B": V("reject", 0.8), "G": V("accept", 0.6)},
                   extra=[{"kind": "judge_merges", "endpoint": "second", "times": 1,
                           "status": 500, "body": "{\"error\": \"down\"}"},
                          {"kind": "judge_merges", "endpoint": "third", "times": 1,
                           "status": 503, "body": "{\"error\": \"down\"}"}]),
        [expect(judge_insert("A", "B", "G"),
                ep("A", "judge2_verdict = 'accept'"), ep("B", "judge3_verdict = 'reject'"),
                ep("G", "judge3_verdict = 'accept'")),
         review()]),
    # An automatic reject is reconsidered when its evidence changes: the
    # row reopens, its pair is released, and the judge rejects it again.
    "judge-merge-reconsider": judge_scenario(
        judge_config("auto-reject", second=None),
        judge_stub({"A": V("reject", 0.9), "B": V("reject", 0.9)}),
        [expect(judge_insert("A", "B"), judged_by_dream("A", "rejected"),
                judged_by_dream("B", "rejected")),
         expect(judge_evidence_change("alpha-node"), judged_by_dream("A", "rejected"),
                decisions_for("A", "rejected", 2), decisions_for("B", "rejected", 1)),
         review()]),
    # A reply repeating an id: the third opinion's verdicts for one row
    # share that row (Python's one dict), so each stacks its own tail and a
    # refusal the first noted is not noted again; the second opinion's
    # repeat overwrites. The second endpoint names no model, so the 2-1
    # reject is refused.
    "judge-merge-duplicates": judge_scenario(
        judge_config("auto-reject", "auto", third="judge-three"),
        judge_stub({"G": V("accept", 0.8), "D": V("accept", 0.9), "B": V("accept", 0.7)},
                   {"G": V("reject", 0.8), "D": V("reject", 0.9),
                    "B": [V("accept", 0.6), V("accept", 0.65)]},
                   {"G": [V("reject", 0.9), V("reject", 0.85, relation="part-of")],
                    "D": [V("leave", 0.5), V("leave", 0.4)]},
                   second_model=None),
        [expect(judge_insert("G", "D", "B"),
                ep("G", "status = 'pending' AND judge_note LIKE "
                        f"'%[2-1 reject] | {REFUSED_SECOND} | 3rd (judge-three): "
                        "relate:part-of 0.85 [2-1 reject]'"),
                ep("D", "judge_note LIKE '%| 3rd (judge-three): leave 0.50 [no majority] "
                        "| 3rd (judge-three): leave 0.40 [no majority]'"),
                ep("B", "judge_note LIKE '%| 2nd (judge-two): accept 0.65 [agree]' "
                        "AND judge2_confidence = 0.65::real")),
         review()]),
    # The Docker default: the first endpoint's model is the launch alias
    # ``extractor`` and the second's ``judge``, so each tick probes GET
    # /models for the served identity (observed_model) and checks the
    # reply's served model against it: the first reply, served as
    # judge-one-x while /models says judge-one, records nothing and is
    # asked again. (The shared stub cannot tell whose keyless GET a probe
    # is, so the probe shows only through what it decides.) The two
    # distinct served models then authorize the two-vote reject.
    "judge-merge-alias": judge_scenario(
        judge_config("auto-reject", second="judge"),
        {**judge_stub({"B": V("reject", 0.75), "A": V("accept", 0.9)},
                      {"B": V("reject", 0.9), "A": V("accept", 0.8)},
                      extra=[{"kind": "judge_merges", "times": 1, "model": "judge-one-x",
                              "verdicts": {JUDGE_KEYS["A"]: V("reject", 0.95),
                                           JUDGE_KEYS["B"]: V("reject", 0.95)}}]),
         "model": "extractor", "served_model": "judge-one"},
        [expect(judge_insert("A", "B"), judged_by_dream("B", "rejected"),
                ep("A", "status = 'pending' AND judge_model = 'judge-one' "
                        "AND judge2_model = 'judge-two'")),
         review()]),
    # The second endpoint moves (judge-two, then judge-two-b) under a batch
    # of two: its first call fails, so three rows await it; the second call
    # (judge-two) rejects B on two votes and splits G; the third
    # (judge-two-b, on D) moves its identity in the same tick as G's third
    # vote, so that tiebreak applies nothing; the next tick reopens B's
    # decision from the moved vote (reconsideration) and clears G's moved
    # second and third votes, which are asked again and reject.
    "judge-merge-moved": judge_scenario(
        judge_config("auto-reject", "auto", third="judge-three",
                     extra="    judge_batch: 2\n"),
        judge_stub({"B": V("reject", 0.75), "G": V("accept", 0.8), "D": V("accept", 0.9)},
                   {"B": V("reject", 0.9), "G": V("reject", 0.8), "D": V("accept", 0.8)},
                   {"G": V("reject", 0.9, relation="part-of")},
                   second_model="judge-two-b",
                   extra=[{"kind": "judge_merges", "endpoint": "second", "times": 1,
                           "status": 500, "body": "{\"error\": \"down\"}"},
                          {"kind": "judge_merges", "endpoint": "second", "times": 1,
                           "model": "judge-two",
                           "verdicts": {JUDGE_KEYS["B"]: V("reject", 0.9),
                                        JUDGE_KEYS["G"]: V("reject", 0.8),
                                        JUDGE_KEYS["D"]: V("accept", 0.8)}}]),
        [expect(judge_insert("B", "G", "D"),
                judged_by_dream("B", "rejected"), decisions_for("B", "rejected", 2),
                judged_by_dream("G", "rejected"), decisions_for("G", "rejected", 1),
                ep("G", "judge2_served_model = 'judge-two-b'"), relate_link("part-of"),
                *pending("D")),
         review()]),
    # The first endpoint moves (judge-one, then judge-one-b) under a batch
    # of one: the first verdict recorded under judge-one is cleared once
    # the endpoint has served judge-one-b, and asked again.
    "judge-merge-first-moved": judge_scenario(
        judge_config("shadow", extra="    judge_batch: 1\n"),
        judge_stub({"A": V("accept", 0.85), "B": V("accept", 0.8)},
                   {"A": V("accept", 0.7), "B": V("accept", 0.75)},
                   reply_model="judge-one-b",
                   extra=[{"kind": "judge_merges", "times": 1, "model": "judge-one",
                           "verdicts": {JUDGE_KEYS["A"]: V("accept", 0.9),
                                        JUDGE_KEYS["B"]: V("accept", 0.9)}}]),
        [expect(judge_insert("A", "B"),
                ep("A", "judge_model = 'judge-one-b' AND judge_confidence = 0.85::real "
                        "AND judge2_verdict IS NOT NULL"),
                ep("B", "judge_model = 'judge-one-b'")),
         review()]),
    # Replay: votes recorded under tiebreak shadow, the mode then promoted
    # to auto (a restart): the recorded 2-1 reject applies with no model
    # call and files its link, and a 2-1 accept on a dismissed pair closes
    # the row, recording the policies the previous row's tiebreak left on
    # the review. A row the live tiebreak refused (its note carries the
    # refusal) is not replayed when its pair is later dismissed.
    "judge-merge-replay": judge_scenario(
        judge_config("auto", "shadow", third="judge-three", extra="    snapshot_keep: 1\n"),
        judge_stub({"G": V("accept", 0.8), "E": V("accept", 0.9), "L": V("accept", 0.9)},
                   {"G": V("reject", 0.8), "E": V("reject", 0.7), "L": V("reject", 0.7)},
                   {"G": V("reject", 0.9, relation="part-of"), "E": V("accept", 0.8),
                    "L": V("accept", 0.8)}),
        [expect(judge_insert("G", "E", until=three_votes("G", "E")), *pending("G", "E")),
         expect(judge_restart(
             judge_config("auto", "auto", third="judge-three", extra="    snapshot_keep: 1\n"),
             until=("SELECT NOT EXISTS (SELECT 1 FROM entity_proposals WHERE status = "
                    "'pending' AND reason IN ('write-dedup: g', 'write-dedup: e'))")),
             judged_by_dream("G", "rejected"), relate_link("part-of"),
             judged_by_dream("E", "rejected")),
         expect(judge_insert("L", until=three_votes("L")),
                ep("L", "status = 'pending' AND judge_note LIKE "
                        "'%| auto-accept refused: name veto'")),
         expect({**external("INSERT INTO dismissed_pairs (a_norm, b_norm, dismissed_at) "
                            "VALUES ('llama-26b', 'llama-4b', 1)"),
                 "settle": dict(JUDGE_SETTLE)}, *pending("L")),
         review()]),
    # Reply values the database refuses, each once: a first opinion's note
    # holding a lone surrogate (psycopg's UnicodeEncodeError), a confidence
    # that underflows REAL, and a second endpoint whose served model holds
    # one (PostgreSQL refuses the response identity's JSON). Each fails its
    # tick before anything of it is written; the next reply records.
    "judge-merge-unencodable": judge_scenario(
        judge_config("shadow"),
        judge_stub({"A": V("accept", 0.9), "B": V("accept", 0.8)},
                   {"A": V("accept", 0.8), "B": V("reject", 0.7)},
                   extra=[{"kind": "judge_merges", "times": 1,
                           "verdicts": {JUDGE_KEYS["A"]: V("accept", 0.9, note=f"x{LONE}"),
                                        JUDGE_KEYS["B"]: V("accept", 0.8)}},
                          {"kind": "judge_merges", "times": 1,
                           "verdicts": {JUDGE_KEYS["A"]: V("accept", 1e-50),
                                        JUDGE_KEYS["B"]: V("accept", 0.8)}},
                          {"kind": "judge_merges", "endpoint": "second", "times": 1,
                           "model": f"judge-{LONE}",
                           "verdicts": {JUDGE_KEYS["A"]: V("accept", 0.8),
                                        JUDGE_KEYS["B"]: V("accept", 0.7)}}]),
        [expect(judge_insert("A", "B"),
                ep("A", "judge_confidence = 0.9::real AND judge2_model = 'judge-two' "
                        "AND judge_note = ' | 2nd (judge-two): accept 0.80 [agree]'"),
                ep("B", "judge2_verdict = 'reject'"),
                "SELECT value->>'model' = 'judge-two' FROM meta "
                "WHERE key = 'review_response_identity_v1_merge_second'"),
         review()]),
})
EXTERNAL_WRITES.update(n for n in SCENARIOS if n.startswith("judge-merge-"))


def resolve_args(args: dict, dsn: str) -> dict:
    out = {}
    import psycopg
    for k, v in args.items():
        if isinstance(v, list) and any(isinstance(x, str) and x.startswith(("@id:", "@idn:"))
                                       for x in v):
            # a list of row ids: each element resolved as a value of its own
            out[k] = [resolve_args({"v": x}, dsn)["v"] for x in v]
            continue
        if isinstance(v, str) and v.startswith("@root:"):
            _, key, n = v.split(":")
            with psycopg.connect(dsn) as c:
                row = c.execute("SELECT id FROM episodes WHERE session_key = %s AND parent_id IS NULL "
                                "ORDER BY started_at LIMIT 1", (key,)).fetchone()
            v = row[0][:int(n)] if row else "0" * int(n)
        elif isinstance(v, str) and v.startswith("@txmid:"):
            # ``@txmid:<entity_norm>:<attribute_norm>``: the midpoint between
            # the slot's first two transaction times (a cutoff that keeps
            # exactly the first version); both banks hold the same clocks.
            _, ent, attr = v.split(":")
            with psycopg.connect(dsn) as c:
                rows = c.execute("SELECT tx_time FROM facts WHERE entity_norm = %s "
                                 "AND attribute_norm = %s ORDER BY tx_time LIMIT 2",
                                 (ent, attr)).fetchall()
            v = (rows[0][0] + rows[1][0]) / 2
        elif isinstance(v, str) and v.startswith("@ep:"):
            # ``@ep:<title>``: the first episode with that title, whole.
            title = v[len("@ep:"):]
            with psycopg.connect(dsn) as c:
                row = c.execute("SELECT id FROM episodes WHERE title = %s "
                                "ORDER BY started_at LIMIT 1", (title,)).fetchone()
            v = row[0] if row else "0" * 32
        elif isinstance(v, str) and v.startswith(("@id:", "@idn:")):
            if v.startswith("@idn:"):
                _, n, text = v.split(":", 2)
            else:
                n, text = 0, v[len("@id:"):]
            import hashlib
            with psycopg.connect(dsn) as c:
                rows = c.execute("SELECT id FROM entries WHERE text = %s ORDER BY id",
                                 (text,)).fetchall()
                if not rows:
                    rows = c.execute(
                        "SELECT entry_id FROM entry_reinstatement_decisions "
                        "WHERE entry_text_sha256 = %s ORDER BY decided_at LIMIT 1",
                        (hashlib.sha256(text.encode("utf-8")).hexdigest(),)).fetchall()
            v = rows[int(n)][0] if len(rows) > int(n) else 999999
        elif isinstance(v, str) and v.startswith(("@sup:", "@sha:")):
            # The text and source hashes read the first row with the text.
            # The retirement prefers the first reinstatement decision's prior
            # values for that row (a replay repeats its request), then the
            # first retired row with the text; a live row yields values that
            # cannot match.
            import hashlib
            if v.startswith("@sup:"):
                field, text = "sup", v[len("@sup:"):]
            else:
                _, field, text = v.split(":", 2)
            text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            with psycopg.connect(dsn) as c:
                row = c.execute("SELECT id, text, source FROM entries WHERE text = %s "
                                "ORDER BY id LIMIT 1", (text,)).fetchone()
                if row is None:
                    # A deleted row resolves through its first decision (the
                    # audit has no foreign key): a replay repeats its request.
                    gone = c.execute(
                        "SELECT entry_id, entry_source_sha256 FROM "
                        "entry_reinstatement_decisions WHERE entry_text_sha256 = %s "
                        "ORDER BY decided_at LIMIT 1", (text_sha,)).fetchone()
                prior = c.execute(
                    "SELECT prior_superseded_at, prior_superseded_by_text FROM "
                    "entry_reinstatement_decisions WHERE entry_id = %s ORDER BY decided_at "
                    "LIMIT 1", (row[0] if row else (gone[0] if gone else -1),)).fetchone()
                retired = c.execute(
                    "SELECT superseded_at, superseded_by_text FROM entries WHERE text = %s "
                    "AND superseded_at IS NOT NULL ORDER BY id LIMIT 1", (text,)).fetchone()
            retirement = prior or retired or (1.0, "")
            if field == "sup":
                v = retirement[0]
            elif row is None and gone is not None and field in ("text", "source"):
                v = text_sha if field == "text" else gone[1]
            else:
                value = {"text": row[1] if row else "", "source": row[2] if row else "",
                         "by": retirement[1] or ""}[field]
                v = hashlib.sha256(value.encode("utf-8")).hexdigest()
        elif isinstance(v, str) and v.startswith("@tomb:"):
            n = int(v.split(":")[1])
            with psycopg.connect(dsn) as c:
                row = c.execute("SELECT value FROM meta WHERE key = 'episode_tombstones'").fetchone()
            ids = sorted((row[0] or {}).keys()) if row else []
            v = ids[0][:n] if ids else "0" * n
        out[k] = v
    return out


# ── state normalization ───────────────────────────────────────────────────

def _table(state, name):
    return state["rows"].get(f"public.{name}")


def _col(t, name):
    return t["columns"].index(name) if t and name in t["columns"] else None


def extract_numeric(state: dict) -> dict:
    """Pull entry embeddings and surprises out by entry id, leaving tokens."""
    t = _table(state, "entries")
    out = {}
    if not t:
        return out
    i_id, i_emb, i_s = _col(t, "id"), _col(t, "embedding"), _col(t, "surprise")
    for row in t["rows"]:
        emb = row[i_emb]
        out[row[i_id]] = {"embedding": [float(x) for x in emb.strip("[]").split(",")] if emb else None,
                          "surprise": row[i_s]}
        row[i_emb] = "<embedding>" if emb is not None else None
        row[i_s] = "<surprise>" if row[i_s] is not None else None
    return out


def extract_fact_numeric(state: dict, table: str = "facts") -> dict:
    """Pull a canonical table's embeddings out by row id, leaving tokens."""
    t = _table(state, table)
    if t is None:
        return {}
    out = {}
    i_id, i_emb = _col(t, "id"), _col(t, "embedding")
    if i_id is None or i_emb is None:
        return out
    for row in t["rows"]:
        emb = row[i_emb]
        out[row[i_id]] = [float(x) for x in emb.strip("[]").split(",")] if emb else None
        row[i_emb] = "<embedding>" if emb is not None else None
    return out


def extract_lesson_scores(state: dict) -> dict:
    """Pull each ``lesson_search_events.served`` score out by row id, leaving
    tokens (compared within ``SCORE_TOLERANCE``)."""
    t = _table(state, "lesson_search_events")
    if t is None:
        return {}
    i_id, i_served = _col(t, "id"), _col(t, "served")
    out = {}
    for row in t["rows"]:
        served = row[i_served]
        if isinstance(served, list):
            out[row[i_id]] = [x.get("score") if isinstance(x, dict) else None for x in served]
            row[i_served] = [{**x, "score": "<score>"} if isinstance(x, dict) else x
                             for x in served]
    return out


def compare_lesson_scores(py: dict, rs: dict, stats: dict) -> list[str]:
    diffs = []
    for rid in sorted(set(py) & set(rs)):
        for i, (a, b) in enumerate(zip(py[rid], rs[rid])):
            if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                if a != b:
                    diffs.append(f"lesson_search_events[{rid}].served[{i}].score: {a} vs {b}")
                continue
            d = abs(a - b)
            stats["lesson_score_max_diff"] = max(stats.get("lesson_score_max_diff", 0.0), d)
            if d > SCORE_TOLERANCE:
                diffs.append(f"lesson_search_events[{rid}].served[{i}].score: {a} vs {b}")
    return diffs


def compare_fact_numeric(py: dict, rs: dict, tol: dict, stats: dict,
                         table: str = "facts") -> list[str]:
    diffs = []
    for fid in sorted(set(py) & set(rs)):
        ea, eb = py[fid], rs[fid]
        if (ea is None) != (eb is None):
            diffs.append(f"{table}[{fid}].embedding: python/rust presence mismatch")
        elif ea is not None and len(ea) != len(eb):
            diffs.append(f"{table}[{fid}].embedding: length python {len(ea)} vs rust {len(eb)}")
        elif ea:
            mad = max(abs(x - y) for x, y in zip(ea, eb))
            cos = sum(x * y for x, y in zip(ea, eb)) / (
                math.sqrt(sum(x * x for x in ea)) * math.sqrt(sum(y * y for y in eb)))
            stats["max_abs_diff"] = max(stats.get("max_abs_diff", 0.0), mad)
            stats["min_cosine"] = min(stats.get("min_cosine", 1.0), cos)
            if tol and (mad > tol["embedding_max_abs_diff"] or cos < tol["embedding_min_cosine"]):
                diffs.append(f"{table}[{fid}].embedding: max abs diff {mad:.3e}, cosine {cos:.9f}")
    return diffs


def decision_record_clocks(value: dict) -> dict:
    """A decision record with its clocks (and its proposal snapshot's
    verdict clocks) reduced to null-or-not."""
    out = {k: (_clock(x) if k in DECISION_RECORD_CLOCK else x) for k, x in value.items()}
    if isinstance(out.get("proposal"), dict):
        out["proposal"] = {k: (_clock(x) if k in DECISION_PROPOSAL_CLOCK else x)
                           for k, x in out["proposal"].items()}
    return out


def episode_map(state: dict) -> dict[str, str]:
    ids: list[str] = []
    t = _table(state, "episodes")
    if t:
        i_id, i_st = _col(t, "id"), _col(t, "started_at")
        ids += [r[i_id] for r in sorted(t["rows"], key=lambda r: (r[i_st] or 0, r[i_id]))]
    m = _table(state, "meta")
    if m:
        for key, value in m["rows"]:
            if key in ("episode_tombstones", "deferred_empty_roots") and isinstance(value, dict):
                def when(k):
                    v = value[k]
                    return v.get("ended_at", 0) if isinstance(v, dict) else v
                ids += [k for k in sorted(value, key=lambda k: (when(k) or 0, k)) if k not in ids]
    return {eid: f"<ep#{i}>" for i, eid in enumerate(dict.fromkeys(ids))}


def _rename(v, emap):
    if isinstance(v, str):
        if v in emap:
            return emap[v]
        # a synthesized lesson's provenance token names its episode
        if v.startswith("episode:") and v[8:] in emap:
            return "episode:" + emap[v[8:]]
        if GENERIC.match(v):
            return "session - <minute>"
        return v
    if isinstance(v, list):
        out = [_rename(x, emap) for x in v]
        # A sorted token list (a lesson's provenance) was sorted on the raw
        # episode ids, whose order differs per side: sort it again on the
        # renamed tokens. An unsorted list keeps its order, so a side that
        # did not sort still differs.
        if (out != v and all(isinstance(x, str) for x in v) and v == sorted(v)
                and any(x.startswith("episode:") for x in v)):
            out.sort()
        return out
    if isinstance(v, dict):
        return {_rename(k, emap): _rename(x, emap) for k, x in v.items()}
    return v


def _clock(v):
    if isinstance(v, list):
        return [_clock(x) for x in v]
    return None if v is None else "<clock>"


def _recent_clock_ms(v):
    """``_recent_clock`` for wall-clock milliseconds."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) \
            and abs(v / 1000.0 - time.time()) < RECENT_S:
        return "<clock>"
    return v


def _recent_clock(v):
    """A clock-written value keeps null-or-not; an explicit past one stays."""
    if _recent(v):
        return "<clock>"
    return v


def _recent(v) -> bool:
    """A number within ``RECENT_S`` of now: written from the clock."""
    return _number(v) and abs(v - time.time()) < RECENT_S


def _audit_asserted_at(t: dict):
    """The ``store_decisions.record`` dicts naming a lesson's asserted_at."""
    if "record" not in t["columns"]:
        return
    ri = t["columns"].index("record")
    for row in t["rows"]:
        if isinstance(row[ri], dict) and "asserted_at" in row[ri]:
            yield row, ri


def clock_ranks(state: dict) -> dict[float, str]:
    """Each recent value in an ``ORDERED_CLOCK`` column (or a lineage
    audit's asserted_at) -> ``<clock#N>``, N its rank among the distinct
    ones in this dump."""
    values = set()
    for qualified, t in state["rows"].items():
        name = qualified.split(".", 1)[1]
        for idx, col in enumerate(t["columns"]):
            if (name, col) in ORDERED_CLOCK:
                values.update(float(r[idx]) for r in t["rows"] if _recent(r[idx]))
        if name == "store_decisions":
            values.update(float(row[ri]["asserted_at"]) for row, ri in _audit_asserted_at(t)
                          if _recent(row[ri]["asserted_at"]))
    return {v: f"<clock#{i}>" for i, v in enumerate(sorted(values))}


def _ordered_clock(v, ranks: dict[float, str], clock_column: bool):
    """A recent value as its rank token; an older one as ``_clock`` in a
    CLOCK column, else exact."""
    if _number(v) and float(v) in ranks:
        return ranks[float(v)]
    return _clock(v) if clock_column else v


HEX_RUN = re.compile(r"[0-9a-f]{8,32}")


def rename_prefixes(v, emap: dict[str, str]):
    """Episode-id prefixes inside response strings (handles in hints)."""
    if isinstance(v, str):
        def sub(m):
            hits = [tok for eid, tok in emap.items() if eid.startswith(m.group(0))]
            return f"{hits[0]}[:{len(m.group(0))}]" if len(hits) == 1 else m.group(0)
        return HEX_RUN.sub(sub, v)
    if isinstance(v, list):
        return [rename_prefixes(x, emap) for x in v]
    if isinstance(v, dict):
        return {k: rename_prefixes(x, emap) for k, x in v.items()}
    return v


def ts_tokens(raw: dict) -> dict[float, str]:
    """Each entry's raw ``ts`` -> ``<ts:ID>`` (first id on a tie), read from
    a dump before ``normalize_state`` drops the clock."""
    t = raw["rows"].get("public.entries")
    out: dict[float, str] = {}
    if not t:
        return out
    idx, ts = t["columns"].index("id"), t["columns"].index("ts")
    for row in sorted(t["rows"], key=lambda r: r[idx]):
        if isinstance(row[ts], (int, float)):
            out.setdefault(float(row[ts]), f"<ts:{row[idx]}>")
    return out


def _dream_token_payload(token):
    """A commit token as its payload, signature dropped (the token signs a
    wall-clock timestamp, so the bytes cannot match across sides)."""
    import base64
    try:
        version, body, _sig = token.split(".")
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        return {"version": version, "payload": json.loads(raw)}
    except Exception:  # noqa: BLE001 - a malformed token compares as text
        return token


def dream_tokens(value, ts_map: dict[float, str]):
    """The ``memory_dream`` answer normalizer (module docstring)."""
    def walk(v, key=None):
        if isinstance(v, dict):
            return {k: walk(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, key) for x in v]
        if key in DREAM_TS_KEYS and isinstance(v, (int, float)) \
                and not isinstance(v, bool) and float(v) in ts_map:
            return ts_map[float(v)]
        if key == "commit_token" and isinstance(v, str):
            payload = _dream_token_payload(v)
            if isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
                t = payload["payload"].get("t")
                if isinstance(t, (int, float)) and float(t) in ts_map:
                    payload["payload"]["t"] = ts_map[float(t)]
            return payload
        if key == "idle_seconds" or key in DREAM_CLOCK_KEYS:
            return _clock(v)
        if key == "snapshot" and isinstance(v, str) and SNAPSHOT_NAME.fullmatch(v):
            return "<snapshot>"
        return v
    return walk(value)


# ``age_days`` is rounded to 0.1 day and each side reads its own clock: two
# reads straddling a rounding boundary differ by one unit.
ORPHAN_AGE_TOL_DAYS = 0.1


def orphan_age_diffs(py, rs) -> list[str]:
    """A deep dry run's ``would_orphan[].age_days`` (wall-clock days since
    each orphan's creation), compared pairwise by position within
    ``ORPHAN_AGE_TOL_DAYS``, then masked as ``<age>`` on both sides in place
    so the answer comparison checks the rest of each entry. An unpaired
    entry or a non-number is left to that comparison."""
    out: list[str] = []

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in a.keys() & b.keys():
                if k == "would_orphan" and isinstance(a[k], list) and isinstance(b[k], list):
                    for i, (x, y) in enumerate(zip(a[k], b[k])):
                        if not (isinstance(x, dict) and isinstance(y, dict)):
                            continue
                        u, v = x.get("age_days"), y.get("age_days")
                        if not (_number(u) and _number(v)):
                            continue
                        if abs(u - v) > ORPHAN_AGE_TOL_DAYS + 1e-9:
                            out.append(f"{path}would_orphan[{i}].age_days: python {u} vs "
                                       f"rust {v} (apart by more than {ORPHAN_AGE_TOL_DAYS} d)")
                        x["age_days"] = y["age_days"] = "<age>"
                else:
                    walk(a[k], b[k], f"{path}{k}.")
        elif isinstance(a, list) and isinstance(b, list):
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}[{i}].")

    walk(py, rs, "")
    return out


def _values_at(v, key: str):
    """Every value held under ``key`` anywhere in ``v``, in document order."""
    if isinstance(v, dict):
        for k, x in v.items():
            if k == key:
                yield x
            else:
                yield from _values_at(x, key)
    elif isinstance(v, list):
        for x in v:
            yield from _values_at(x, key)


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def idle_diffs(py, rs, bound: float) -> list[str]:
    """``idle_seconds`` within a bound, before ``dream_tokens`` drops it.

    Each side answers ``now - (its newest entry's ts)``. The two sides run
    every operation one after the other, so the Python call that wrote the
    newest entry and the Python status call each start at most one step's
    wall time before their Rust counterparts: the two answers differ by at
    most the longest step so far. ``bound`` is that wall time plus slack."""
    out = []
    a, b = list(_values_at(py, "idle_seconds")), list(_values_at(rs, "idle_seconds"))
    if len(a) != len(b):
        return [f"idle_seconds: python {a} vs rust {b}"]
    for x, y in zip(a, b):
        if not (_number(x) and _number(y)) or x < 0 or y < 0:
            out.append(f"idle_seconds: python {x!r} vs rust {y!r} (not both >= 0)")
        elif abs(x - y) > bound:
            out.append(f"idle_seconds: python {x:.3f} vs rust {y:.3f} "
                       f"(apart by more than {bound:.3f} s)")
    return out


def _run_clock_pairs(v):
    """(started_at, finished_at) of every run record in an answer."""
    if isinstance(v, dict):
        if "started_at" in v and "finished_at" in v:
            yield v["started_at"], v["finished_at"]
        for x in v.values():
            yield from _run_clock_pairs(x)
    elif isinstance(v, list):
        for x in v:
            yield from _run_clock_pairs(x)


def run_clock_diffs(side: str, raw: dict, answer=None) -> list[str]:
    """A dream run row finishes no earlier than it starts, in the bank and
    in a ``runs`` answer, wherever both clocks are set (before the clock
    normalizers reduce them to null-or-not)."""
    out = []
    t = raw["rows"].get("public.dream_runs")
    pairs = []
    if t:
        cols = t["columns"]
        if "started_at" in cols and "finished_at" in cols:
            si, fi = cols.index("started_at"), cols.index("finished_at")
            pairs += [("bank", row[si], row[fi]) for row in t["rows"]]
    if answer is not None:
        pairs += [("answer", s, f) for s, f in _run_clock_pairs(answer)]
    for where, s, f in pairs:
        if _number(s) and _number(f) and f < s:
            out.append(f"dream run clock ({side} {where}): finished_at {f} before started_at {s}")
    return out


# Slack on top of the longest step's wall time for ``idle_seconds``: the
# daemons read their clocks inside the call, so the bound needs no more
# than scheduling noise.
IDLE_SLACK_S = 1.0


def normalize_state(state: dict, before: dict,
                    ts_map: dict[float, str] | None = None) -> tuple[dict, dict, dict]:
    state = w1a.scrub_declared_rows(state, before)["state"]
    state = dbstate.normalize(state, w1a.DB_NONDETERMINISTIC, before)
    numeric = extract_numeric(state)
    numeric["facts"] = extract_fact_numeric(state)
    numeric["world_facts"] = extract_fact_numeric(state, "world_facts")
    numeric["lessons"] = extract_fact_numeric(state, "lessons")
    numeric["lesson_scores"] = extract_lesson_scores(state)
    emap = episode_map(state)
    ranks = clock_ranks(state)
    for qualified, t in state["rows"].items():
        name = qualified.split(".", 1)[1]
        for idx, col in enumerate(t["columns"]):
            if (name, col) in ORDERED_CLOCK:
                for row in t["rows"]:
                    row[idx] = _ordered_clock(row[idx], ranks, (name, col) in CLOCK)
            elif (name, col) in CLOCK:
                for row in t["rows"]:
                    row[idx] = _clock(row[idx])
            elif (name, col) in RECENT_CLOCK:
                for row in t["rows"]:
                    row[idx] = _recent_clock(row[idx])
            elif (name, col) in RECENT_CLOCK_MS:
                for row in t["rows"]:
                    row[idx] = _recent_clock_ms(row[idx])
        if name == "meta":
            for row in t["rows"]:
                key, value = row
                if key in META_CLOCK and isinstance(value, dict):
                    row[1] = {k: (_clock(x) if k in META_CLOCK[key] else x) for k, x in value.items()}
                elif key.startswith(DECISION_RECORD_PREFIX) and isinstance(value, dict):
                    row[1] = decision_record_clocks(value)
                elif key == "episode_tombstones" and isinstance(value, dict):
                    row[1] = {k: {**x, "ended_at": _clock(x.get("ended_at"))} if isinstance(x, dict) else x
                              for k, x in value.items()}
                elif key == "deferred_empty_roots" and isinstance(value, dict):
                    row[1] = {k: _clock(x) for k, x in value.items()}
                elif (key == "cortex_dream_cursor" and ts_map
                      and isinstance(value, (int, float)) and not isinstance(value, bool)
                      and float(value) in ts_map):
                    row[1] = ts_map[float(value)]
                elif key == "cortex_supersession_log" and isinstance(value, list):
                    row[1] = [{**x, "timestamp": _clock(x.get("timestamp"))}
                              if isinstance(x, dict) else x for x in value]
        if name == "dream_runs" and ts_map:
            for col in DREAM_RUN_CURSORS:
                if col in t["columns"]:
                    ci = t["columns"].index(col)
                    for row in t["rows"]:
                        v = row[ci]
                        if isinstance(v, (int, float)) and not isinstance(v, bool) \
                                and float(v) in ts_map:
                            row[ci] = ts_map[float(v)]
        if name == "store_decisions" and "record" in t["columns"]:
            ri = t["columns"].index("record")
            for row in t["rows"]:
                if isinstance(row[ri], dict) and "superseded_at" in row[ri]:
                    row[ri] = {**row[ri], "superseded_at": _clock(row[ri]["superseded_at"])}
                # a synthesis lineage audit names its lesson by asserted_at
                if isinstance(row[ri], dict) and "asserted_at" in row[ri]:
                    row[ri] = {**row[ri], "asserted_at": _ordered_clock(
                        row[ri]["asserted_at"], ranks, False)}
        t["rows"] = [_rename(r, emap) for r in t["rows"]]
        if name != "entries":
            t["rows"].sort(key=lambda r: json.dumps(r, sort_keys=True))
    return state, numeric, emap


def state_diff(a: dict, b: dict) -> list[str]:
    """``dbstate.diff``, with list-valued catalog entries diffed row by row
    so a moved sequence names itself."""
    out = []
    for d in dbstate.diff(a, b):
        if d.startswith("/catalog/"):
            key = d.split(":", 1)[0].rsplit("/", 1)[1]
            if key not in a["catalog"] or key not in b["catalog"]:
                out.append(d)
                continue
            ra = {json.dumps(r) for r in a["catalog"][key]}
            rb = {json.dumps(r) for r in b["catalog"][key]}
            out += [f"/catalog/{key}: python {r}" for r in sorted(ra - rb)]
            out += [f"/catalog/{key}: rust {r}" for r in sorted(rb - ra)]
        else:
            out.append(d)
    return out


def compare_numeric(py: dict, rs: dict, tol: dict, stats: dict) -> list[str]:
    diffs = compare_fact_numeric(py.get("facts", {}), rs.get("facts", {}), tol, stats)
    diffs += compare_fact_numeric(py.get("world_facts", {}), rs.get("world_facts", {}), tol,
                                  stats, "world_facts")
    diffs += compare_fact_numeric(py.get("lessons", {}), rs.get("lessons", {}), tol, stats,
                                  "lessons")
    diffs += compare_lesson_scores(py.get("lesson_scores", {}), rs.get("lesson_scores", {}),
                                   stats)
    skip = ("facts", "world_facts", "lessons", "lesson_scores")
    py = {k: v for k, v in py.items() if k not in skip}
    rs = {k: v for k, v in rs.items() if k not in skip}
    for eid in sorted(set(py) | set(rs)):
        a, b = py.get(eid), rs.get(eid)
        if a is None or b is None:
            continue  # the row diff reports the missing entry
        ea, eb = a["embedding"], b["embedding"]
        if (ea is None) != (eb is None):
            diffs.append(f"entries[{eid}].embedding: python/rust presence mismatch")
        elif ea is not None and len(ea) != len(eb):
            diffs.append(f"entries[{eid}].embedding: length python {len(ea)} vs rust {len(eb)}")
        elif ea:
            mad = max(abs(x - y) for x, y in zip(ea, eb))
            cos = sum(x * y for x, y in zip(ea, eb)) / (
                math.sqrt(sum(x * x for x in ea)) * math.sqrt(sum(y * y for y in eb)))
            stats["max_abs_diff"] = max(stats.get("max_abs_diff", 0.0), mad)
            stats["min_cosine"] = min(stats.get("min_cosine", 1.0), cos)
            if tol and (mad > tol["embedding_max_abs_diff"] or cos < tol["embedding_min_cosine"]):
                diffs.append(f"entries[{eid}].embedding: max abs diff {mad:.3e}, cosine {cos:.9f}")
        sa, sb = a["surprise"], b["surprise"]
        if (sa is None) != (sb is None):
            diffs.append(f"entries[{eid}].surprise: python {sa} vs rust {sb}")
        elif sa is not None:
            d = abs(sa - sb)
            stats["surprise_max_diff"] = max(stats.get("surprise_max_diff", 0.0), d)
            if tol and d > tol["surprise_abs"]:
                diffs.append(f"entries[{eid}].surprise: python {sa} vs rust {sb}")
    return diffs


AGE = re.compile(r"^(?:just now|(\d+) (minute|hour|day)s? ago)$")
AGE_STEP = {"minute": 1, "hour": 2, "day": 3}


def _age_step(text):
    m = AGE.match(text) if isinstance(text, str) else None
    if not m:
        return None
    return (0, 0) if m.group(1) is None else (AGE_STEP[m.group(2)], int(m.group(1)))


def _age_adjacent(x, y) -> bool:
    """One step apart: the same unit one apart, or across a unit boundary
    ("just now" / "1 minute ago", "59 minutes ago" / "1 hour ago", ...)."""
    a, b = _age_step(x), _age_step(y)
    if a is None or b is None:
        return False
    a, b = sorted((a, b))
    if a[0] == b[0]:
        return b[1] - a[1] == 1
    return (a, b) in {((0, 0), (1, 1)), ((1, 59), (2, 1)), ((2, 23), (3, 1))}


def normalize_records(a, b, stats=None):
    """Record clocks keep null-or-not; an ``age`` one step apart compares equal;
    a search ``score`` within ``SCORE_TOLERANCE`` compares equal."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) & set(b):
            if k in RESPONSE_CLOCK:
                a[k], b[k] = _clock(a[k]), _clock(b[k])
            elif k in RESPONSE_RECENT_CLOCK:
                a[k], b[k] = _recent_clock(a[k]), _recent_clock(b[k])
            elif (k == "automatic_decisions" and isinstance(a[k], list)
                  and isinstance(b[k], list)):
                for x, y in zip(a[k], b[k]):
                    if isinstance(x, dict) and isinstance(y, dict):
                        for c in DECISION_LISTING_RECENT_CLOCK & set(x) & set(y):
                            x[c], y[c] = _recent_clock(x[c]), _recent_clock(y[c])
                normalize_records(a[k], b[k], stats)
            elif (k in ("cohesion", "seed_score") and "members" in a and "members" in b
                  and isinstance(a[k], (int, float)) and isinstance(b[k], (int, float))
                  and not isinstance(a[k], bool)):
                # A candidate cluster: its seed score rests on the query
                # embedding (the score allowance); its cohesion on vectors
                # both banks hold identically (one rounding unit).
                allow = COHESION_TOLERANCE if k == "cohesion" else SCORE_TOLERANCE
                if abs(a[k] - b[k]) <= allow:
                    a[k] = b[k] = f"<{k}>"
            elif (k == "score" and isinstance(a[k], (int, float))
                  and isinstance(b[k], (int, float)) and not isinstance(a[k], bool)):
                d = abs(a[k] - b[k])
                if stats is not None:
                    stats["response_score_max_diff"] = max(
                        stats.get("response_score_max_diff", 0.0), d)
                if d <= SCORE_TOLERANCE:
                    a[k] = b[k] = "<score>"
            elif k == "age" and a[k] != b[k] and _age_adjacent(a[k], b[k]):
                a[k] = b[k] = "<age>"
            else:
                normalize_records(a[k], b[k], stats)
    elif isinstance(a, list) and isinstance(b, list):
        for x, y in zip(a, b):
            normalize_records(x, y, stats)


def generic_titles(v):
    """A generic session title in a response keeps its shape, not its minute."""
    if isinstance(v, str):
        return "session - <minute>" if GENERIC.match(v) else v
    if isinstance(v, list):
        return [generic_titles(x) for x in v]
    if isinstance(v, dict):
        return {k: generic_titles(x) for k, x in v.items()}
    return v


def _hash_ordered_mirror(e, edges) -> bool:
    """An inverse-derived edge mirroring an asserted one: Python's
    ``derive_edges`` walks those over a set of ``(src, relation, dst)``
    tuples, in per-process string-hash order. Mirrors of transitive edges
    follow insertion order and stay exact. A mirror whose source edge a
    relation filter removed is taken as hash-ordered."""
    via = e.get("via") or [] if isinstance(e, dict) else []
    if not e.get("derived") or len(via) != 1 or not str(via[0]).startswith("inverse:"):
        return False
    rel = str(via[0])[len("inverse:"):]
    source = [x for x in edges if isinstance(x, dict) and x.get("src") == e.get("dst")
              and x.get("relation") == rel and x.get("dst") == e.get("src")]
    return not source or not source[0].get("derived")


def graph_answer_free(v) -> None:
    """In a graph answer: hash-ordered mirrors compare as a multiset in their
    slots (divergences.md, memory_graph), and a node's ``created_at`` (a
    float of wall-clock seconds) compares as present."""
    if isinstance(v, dict):
        edges = v.get("edges")
        if isinstance(edges, list):
            slots = [i for i, e in enumerate(edges)
                     if isinstance(e, dict) and _hash_ordered_mirror(e, edges)]
            for i, e in zip(slots, sorted((edges[i] for i in slots),
                                          key=lambda e: json.dumps(e, sort_keys=True))):
                edges[i] = e
        if "canonical" in v and isinstance(v.get("created_at"), float):
            v["created_at"] = "<clock>"
        for x in v.values():
            graph_answer_free(x)
    elif isinstance(v, list):
        for x in v:
            graph_answer_free(x)


# Search telemetry: the float leaves w2d_search.py compares within its
# SCORE_TOL (scores and fusion inputs rest on the embeddings).
EVENT_FLOAT_KEYS = {"score", "dense", "ce", "bm25", "slot", "recency", "margin"}
EVENT_SCORE_TOL = 2e-4


def _float_pair_free(x, y) -> None:
    if isinstance(x, dict) and isinstance(y, dict):
        for k in x.keys() & y.keys():
            a, b = x[k], y[k]
            if (k in EVENT_FLOAT_KEYS and isinstance(a, (int, float)) and isinstance(b, (int, float))
                    and not isinstance(a, bool) and not isinstance(b, bool)
                    and abs(a - b) <= EVENT_SCORE_TOL):
                x[k] = y[k] = f"<{k}>"
            else:
                _float_pair_free(a, b)
    elif isinstance(x, list) and isinstance(y, list):
        for a, b in zip(x, y):
            _float_pair_free(a, b)


def events_diff(a: dict, b: dict) -> list[str]:
    """The retrieval events both daemons log for a search, compared row by
    row (matched by id) and field by field: the served entries' scores and
    fusion inputs within w2d_search.py's tolerance, everything else exact.
    Unlike w2d_search.py it aligns no score ties, so a declared tie swap
    shows as a difference rather than passing."""
    ea = a.get("rows", {}).get("public.retrieval_events")
    eb = b.get("rows", {}).get("public.retrieval_events")
    cols = ea.get("columns") or []
    out = []
    by_id = {r[0]: r for r in eb.get("rows", [])}
    ids_a = {r[0] for r in ea.get("rows", [])}
    for ra in ea.get("rows", []):
        rb = by_id.get(ra[0])
        if rb is None:
            out.append(f"retrieval_events[{ra[0]}]: row only in python")
            continue
        for c, x, y in zip(cols, ra, rb):
            x, y = json.loads(json.dumps(x)), json.loads(json.dumps(y))
            if c == "served":
                _float_pair_free(x, y)
            out += [f"retrieval_events[{ra[0]}].{c}{d}" for d in w1a.diff_values(x, y)]
    out += [f"retrieval_events[{i}]: row only in rust" for i in sorted(set(by_id) - ids_a)]
    return out


def state_and_events_diff(a: dict, b: dict) -> list[str]:
    """``state_diff`` with the retrieval events compared by ``events_diff``
    when both sides hold them (the inputs are left as they are)."""
    ea = a.get("rows", {}).get("public.retrieval_events")
    eb = b.get("rows", {}).get("public.retrieval_events")
    if not (isinstance(ea, dict) and isinstance(eb, dict)) or ea.get("columns") != eb.get("columns"):
        return state_diff(a, b)
    rest = lambda s: {**s, "rows": {k: v for k, v in s["rows"].items()  # noqa: E731
                                    if k != "public.retrieval_events"}}
    return events_diff(a, b) + state_diff(rest(a), rest(b))


def compare_response(py: dict, rs: dict, tol: dict, stats: dict) -> list[str]:
    a, b = generic_titles(json.loads(json.dumps(py))), generic_titles(json.loads(json.dumps(rs)))
    graph_answer_free(a)
    graph_answer_free(b)
    normalize_records(a, b, stats)
    out = []
    va, vb = a.get("value"), b.get("value")
    for key in ("surprise", "new_memory_surprise"):
        if isinstance(va, dict) and isinstance(vb, dict) and key in va and key in vb:
            d = abs(float(va[key]) - float(vb[key]))
            stats["response_surprise_max_diff"] = max(
                stats.get("response_surprise_max_diff", 0.0), d)
            # The response is rounded to 4 dp, so one rounding unit is allowed
            # on top of the stored tolerance; the decision (`reason`) stays exact.
            if tol and d > tol["surprise_abs"] + 1e-4:
                out.append(f"{key}: python {va[key]} vs rust {vb[key]}")
            va[key] = vb[key] = "<surprise>"
    out += w1a.diff_values(a, b)
    return out


# ── running ───────────────────────────────────────────────────────────────

def run_root(prefix: str = "w2e") -> Path:
    """Keep each invocation's homes and evidence separate, including on failure."""
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-{os.getpid()}-", dir=daemons.scratch_root()))


# ── dream extractor stub ──────────────────────────────────────────────────

STUB_ORIGIN_TOKEN = "http://stub"
EXTRACTOR_BODY_DIFF_CHARS = 600


def replace_origin(value, origin: str, token: str = STUB_ORIGIN_TOKEN):
    """``value`` with every occurrence of ``origin`` in its strings (keys
    included) replaced by ``token``, recursively."""
    if isinstance(value, str):
        return value.replace(origin, token)
    if isinstance(value, list):
        return [replace_origin(v, origin, token) for v in value]
    if isinstance(value, tuple):
        return tuple(replace_origin(v, origin, token) for v in value)
    if isinstance(value, dict):
        return {replace_origin(k, origin, token): replace_origin(v, origin, token)
                for k, v in value.items()}
    return value


def start_stubs(scn: dict, names) -> dict[str, StubExtractor]:
    """One started stub per name when the scenario declares an extractor
    (``shared_stub``: one for both daemons, another for the seed)."""
    if "extractor" not in scn:
        return {}
    stubs: dict[str, StubExtractor] = {}
    try:
        for n in names:
            if scn.get("shared_stub") and n == "rust" and "python" in stubs:
                stubs[n] = stubs["python"]
                continue
            stubs[n] = StubExtractor(scn["extractor"], n).start()
    except BaseException:
        close_stubs(stubs)
        raise
    return stubs


def close_stubs(stubs: dict[str, StubExtractor]) -> None:
    for stub in stubs.values():
        stub.close()


def side_key(side: str) -> str:
    """The bearer key a side sends to a shared stub."""
    return f"side-{side}"


def side_auth(side: str) -> str:
    from stub_extractor import auth_digest
    return auth_digest(f"Bearer {side_key(side)}")


def scenario_config(scn: dict, stub: StubExtractor | None) -> str | None:
    """The scenario's config, ``@STUB@`` replaced by the stub's origin."""
    config = scn.get("config")
    if config and stub is not None:
        config = config.replace("@STUB@", stub.origin)
    return config


def drain_side(stubs: dict[str, StubExtractor], scn: dict, side: str) -> list[dict]:
    if scn.get("shared_stub"):
        return stubs[side].drain(auth=side_auth(side))
    return stubs[side].drain()


# The tables the merge judge writes: settled when none of them changes.
JUDGE_TABLES = ("entity_proposals", "merge_decisions", "dismissed_pairs", "edge_proposals",
                "entities", "edges", "meta")


def judge_digest(dsn: str) -> str:
    import psycopg
    parts = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        for t in JUDGE_TABLES:
            row = conn.execute(f"SELECT md5(COALESCE(string_agg(x, '|' ORDER BY x), '')) "
                               f"FROM (SELECT t::text AS x FROM {t} t) s").fetchone()
            parts.append(row[0])
    return ",".join(parts)


def settle_sides(spec: dict, dsns: dict, stubs: dict, scn: dict) -> None:
    """Wait until each side's background work is idle: ``until`` (a query
    whose first column must be true) holds, then neither the judge tables
    nor the side's stub requests change for ``quiet_s`` seconds."""
    import psycopg
    quiet = float(spec.get("quiet_s", 3.0))
    deadline = time.monotonic() + float(spec.get("timeout_s", 180.0))
    for side in ("python", "rust"):
        dsn = dsns[side]
        if spec.get("until"):
            while True:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    ok = conn.execute(spec["until"]).fetchone()[0]
                if ok:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{side}: settle condition never held: {spec['until']}")
                time.sleep(0.2)
        last, since = None, time.monotonic()
        while True:
            count = stubs[side].count(side_auth(side) if scn.get("shared_stub") else None) \
                if stubs else 0
            snap = (judge_digest(dsn), count)
            if snap != last:
                last, since = snap, time.monotonic()
            elif time.monotonic() - since >= quiet:
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"{side}: background work did not settle")
            time.sleep(0.2)


def extractor_env(scn: dict, stub: StubExtractor | None, side: str | None = None) -> dict:
    """The dream-extractor endpoint variables pointing a daemon at its stub
    (``shared_stub``: with the side's own key on every judge endpoint)."""
    if stub is None:
        return {}
    spec = scn.get("extractor") or {}
    env = {"PSEUDOLIFE_DREAM_BASE_URL": stub.base_url,
           "PSEUDOLIFE_DREAM_MODEL": spec.get("model", "stub-model")}
    if scn.get("shared_stub") and side:
        for name in ("PSEUDOLIFE_DREAM_API_KEY", "PSEUDOLIFE_JUDGE_API_KEY",
                     "PSEUDOLIFE_JUDGE_SECOND_API_KEY", "PSEUDOLIFE_JUDGE_THIRD_API_KEY"):
            env[name] = side_key(side)
    if scn.get("extractor_fallback"):
        env["PSEUDOLIFE_DREAM_FALLBACK_BASE_URL"] = stub.fallback_base_url
        env["PSEUDOLIFE_DREAM_FALLBACK_MODEL"] = spec.get("fallback_model", "stub-fallback-model")
    keys = scn.get("extractor_keys") or {}
    if keys.get("primary"):
        env["PSEUDOLIFE_DREAM_API_KEY"] = keys["primary"]
    if keys.get("fallback"):
        env["PSEUDOLIFE_DREAM_FALLBACK_API_KEY"] = keys["fallback"]
    return env


def extractor_key_diffs(side: str, scn: dict, requests: list[dict]) -> list[str]:
    """With ``extractor_keys`` declared, each chat request must carry its own
    endpoint's key (the fallback key on a ``/fallback/`` path, the primary
    key elsewhere, no header where none is configured) and every probe and
    models GET none: no key reaches the other endpoint."""
    keys = scn.get("extractor_keys")
    if not keys:
        return []
    from stub_extractor import auth_digest
    out = []
    for i, r in enumerate(requests):
        if r["method"] == "POST":
            key = keys.get("fallback") if r["path"].startswith("/fallback/") else keys.get("primary")
            want = auth_digest(f"Bearer {key}") if key else None
        else:
            want = None
        if r.get("authorization") != want:
            which = "no key" if want is None else ("the fallback key" if key == keys.get("fallback")
                                                    else "the primary key")
            out.append(f"extractor {side} request {i} ({r['method']} {r['path']}): "
                       f"authorization {r.get('authorization')}, expected {which}")
    return out


def _request_label(req: dict | None, short: bool = True) -> str:
    if req is None:
        return "<none>"
    digest = req.get("sha256")
    digest = (digest[:16] if short else digest) if digest else "-"
    return f"{req['method']} {req['path']} sha256={digest}"


def _body_diff(a: str, b: str) -> str:
    """A short unified diff of two request bodies, split after each comma so
    a one-line JSON body shows where it differs."""
    at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    lines = difflib.unified_diff(re.split(r"(?<=,)", a), re.split(r"(?<=,)", b),
                                 "python", "rust", n=1, lineterm="")
    text = f"first difference at byte {at}\n" + "\n".join(lines)
    if len(text) > EXTRACTOR_BODY_DIFF_CHARS:
        text = text[:EXTRACTOR_BODY_DIFF_CHARS] + "..."
    return text


def extractor_summary(requests: list[dict]) -> list[dict]:
    return [{"method": r["method"], "path": r["path"], "sha256": r["sha256"],
             "authorization": r.get("authorization")} for r in requests]


def extractor_unanswered(side: str, requests: list[dict]) -> list[str]:
    return [f"extractor {side}: unanswered {_request_label(r, short=False)}"
            for r in requests if not r.get("answered")]


def extractor_diffs(py: list[dict], rs: list[dict]) -> list[str]:
    """Unanswered requests on either side, then the two request sequences
    compared element by element on (method, path, body, authorization
    digest)."""
    out = extractor_unanswered("python", py) + extractor_unanswered("rust", rs)
    if len(py) != len(rs):
        out.append(f"extractor requests: python sent {len(py)}, rust sent {len(rs)}")
    for i in range(max(len(py), len(rs))):
        a = py[i] if i < len(py) else None
        b = rs[i] if i < len(rs) else None
        key = (lambda r: None if r is None else (r["method"], r["path"], r["body"]))
        if key(a) == key(b):
            if a is not None and a.get("authorization") != b.get("authorization"):
                out.append(f"extractor request {i}: authorization python "
                           f"{a.get('authorization')} vs rust {b.get('authorization')}")
            continue
        line = f"extractor request {i}: python {_request_label(a)} vs rust {_request_label(b)}"
        if a and b and a["method"] == "POST" and b["method"] == "POST":
            line += "\n" + _body_diff(a["body"] or "", b["body"] or "")
        out.append(line)
    return out


def side_env(scn: dict, dsn: str, extra: dict | None = None) -> dict:
    env = {"PSEUDOLIFE_RELEASE_CHECK": "0", "PSEUDOLIFE_PLUGIN_DIR": str(w1a.REPO / "plugin"),
           "PSEUDOLIFE_MCP_DATABASE_URL": dsn, "PSEUDOLIFE_MCP_TOKEN": TOKEN}
    env.update(scn.get("env", {}))
    env.update(extra or {})
    return env


def with_pull_token(op: dict, token: str | None) -> dict:
    """``@pull_token`` in a tool argument: the last ``commit_token`` this
    side's daemon answered (each side commits its own pull)."""
    args = op.get("args")
    if not isinstance(args, dict) or PULL_TOKEN not in args.values():
        return op
    return {**op, "args": {k: (token if v == PULL_TOKEN else v) for k, v in args.items()}}


def run_op(op: dict, port: int, mcp: Mcp, dsn: str) -> dict | None:
    if op["op"] == "sleep":
        time.sleep(op["s"])
        return {"isError": False, "value": None}
    if op["op"] == "sql":
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(op["sql"])
        return {"isError": False, "value": None}
    if op["op"] == "http":
        body = json.dumps(op["body"]).encode() if "body" in op else None
        hdrs = [AUTH] + ([("Content-Type", "application/json")] if body else [])
        r = w1a.call(port, op["method"], op["path"], hdrs, body)
        return {"isError": r["status"] != 200, "value": r.get("json", r.get("bytes"))}
    args = resolve_args(op["args"], dsn)
    headers = [tuple(h) for h in op.get("headers", [])]
    if op.get("abandon"):
        mcp.abandon(op["name"], args, headers)
        if op["name"] == "memory_store":
            wait_landed(dsn, args.get("text", ""))
        else:
            time.sleep(4)  # let a call that survives the disconnect finish
        return {"isError": False, "value": "<abandoned>"}
    return mcp.call(op["name"], args, headers)


# Operations on each side's data directory rather than its daemon.
HOME_OPS = ("snapshot_file", "block_snapshots")


def restart_sides(op: dict, scn: dict, procs: dict, homes: dict, envs: dict,
                  stubs: dict, binary: Path) -> dict:
    """``restart``: stop both daemons once their background work has
    settled, write ``op["config"]`` (``@STUB@`` replaced) as each side's
    config, and start them again on fresh ports, for a knob read only at
    start (``judge_tiebreak_mode`` promoted after votes were recorded).
    Answers the new MCP clients."""
    import psycopg
    dsns = {side: procs[side].env["PSEUDOLIFE_MCP_DATABASE_URL"] for side in procs}
    probes = "SELECT count(*) FROM retrieval_events WHERE query_text = 'warmup probe'"

    def count(side):
        with psycopg.connect(dsns[side], autocommit=True) as conn:
            return conn.execute(probes).fetchone()[0]
    booted = {side: count(side) + 1 for side in procs}
    for d in procs.values():
        d.stop()
    for side in ("python", "rust"):
        config = op["config"]
        if stubs.get(side) is not None:
            config = config.replace("@STUB@", stubs[side].origin)
        (homes[side] / "data" / "config.yaml").write_text(config, encoding="utf-8")
    procs["python"] = daemons.python_daemon(
        homes["python"], daemons.free_port(), daemons.base_env(homes["python"], envs["python"]))
    procs["rust"] = daemons.rust_daemon(
        binary, homes["rust"], daemons.free_port(), daemons.base_env(homes["rust"], envs["rust"]))
    for d in procs.values():
        d.start(300)
    w1a.wait_settled([d.port for d in procs.values()], token=TOKEN)
    # Each boot's warmup search logs one event, which the state comparison
    # scrubs once per run (``scrub_declared_rows``): once the restart's
    # probe has landed, both boots' probes leave both banks alike.
    deadline = time.monotonic() + 120
    for side in procs:
        while count(side) < booted[side] and time.monotonic() < deadline:
            time.sleep(0.2)
        with psycopg.connect(dsns[side], autocommit=True) as conn:
            conn.execute("DELETE FROM retrieval_events WHERE query_text = 'warmup probe' "
                         "AND id > (SELECT COALESCE(min(id), 0) FROM retrieval_events "
                         "WHERE query_text = 'warmup probe')")
    clients = {side: Mcp(d.port) for side, d in procs.items()}
    for client in clients.values():
        client.init()
    return clients


def expect_diffs(op: dict, dsns: dict) -> list[str]:
    """``op["expect"]``: queries whose first column must answer true on
    both banks once the operation (and its settle) is done, so a scenario
    whose sides both miss the path it is written for still fails."""
    import psycopg
    out = []
    for query in op.get("expect", ()):
        for side in ("python", "rust"):
            with psycopg.connect(dsns[side], autocommit=True) as conn:
                row = conn.execute(query).fetchone()
            if not (row and row[0]):
                out.append(f"expect {side}: {query}")
    return out


def home_op(op: dict, home: Path) -> dict:
    """``snapshot_file``: the side's newest ``graph_snapshots/graph-*.json``
    with its name's shape, the file count and every table's columns and rows
    (CLOCK columns null-or-not, rows as a sorted set); ``block_snapshots``:
    a plain file where the directory goes, so the snapshot write fails."""
    snaps = home / "data" / "graph_snapshots"
    if op["op"] == "block_snapshots":
        snaps.write_text("blocked", encoding="utf-8")
        return {"isError": False, "value": None}
    names = sorted(p.name for p in snaps.glob("graph-*.json")) if snaps.is_dir() else []
    if not names:
        return {"isError": False, "value": {"files": 0}}
    raw = (snaps / names[-1]).read_text(encoding="utf-8")
    tables = {}
    for t, rows in json.loads(raw).items():
        cols = list(rows[0]) if rows else []
        norm = [[_clock(r[c]) if (t, c) in CLOCK else r[c] for c in cols] for r in rows]
        tables[t] = {"columns": cols, "rows": sorted(norm, key=json.dumps)}
    return {"isError": False, "value": {
        "files": len(names), "name_shape": bool(SNAPSHOT_NAME.fullmatch(names[-1])),
        "ascii": raw.isascii(), "tables": tables}}


def wait_landed(dsn: str, text: str, deadline_s: float = 30.0) -> None:
    """Wait until an abandoned store's row exists, up to the deadline: a
    store that survives its caller's disconnect lands whatever the embedder's
    speed, and one that was cancelled never does (the comparison then shows
    the missing row). A fixed sleep judged a slow debug build's 512-token
    embedding as lost."""
    import psycopg
    stripped = text.strip()  # the stored text (service.py strips it)
    end = time.monotonic() + deadline_s
    with psycopg.connect(dsn, autocommit=True) as conn:
        while time.monotonic() < end:
            if conn.execute("SELECT 1 FROM entries WHERE text = %s", (stripped,)).fetchone():
                return
            time.sleep(0.25)


def warmup_expected(scn: dict) -> bool:
    """Whether the startup warmup probe (spec S17) writes its retrieval
    event: ``memory.retrieval_log.enabled`` (default on) gates every event
    row on both daemons."""
    import yaml
    memory = (yaml.safe_load(scn.get("config") or "") or {}).get("memory") or {}
    return bool((memory.get("retrieval_log") or {}).get("enabled", True))


def warmup_landed(dsn: str, before: dict) -> bool:
    """The warmup probe's event row is committed: a ``warmup probe`` search
    event whose id the pre-start state did not hold (a template may keep its
    seeding daemon's probe)."""
    import psycopg
    t = before["rows"].get("public.retrieval_events", {})
    prior = [r[t["columns"].index("id")] for r in t.get("rows", [])]
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT 1 FROM retrieval_events WHERE query_text = 'warmup probe' "
            "AND origin = 'search' AND NOT (id = ANY(%s::bigint[]))", (prior,)).fetchone() is not None


def wait_warmups(dsns: dict, before: dict, deadline_s: float = 300.0,
                 landed=warmup_landed) -> list[str]:
    """Wait until each side's warmup probe event has landed, up to the
    deadline, and return a difference for each side whose never did.

    ``wait_settled`` returns once init is done, but each daemon runs the
    probe after init on its own task. Before ``dbstate.dump`` read every row
    from one snapshot, it read sequences before rows, so a probe committing
    between the two reads showed its row without its ``nextval``;
    ``scrub_declared_rows`` rightly refused that pair and step 0 reported
    ``retrieval_events[...]: row only in`` one side (fact-set-tuned,
    dream-deep-dry, review-actions, 2026-10-19/20). The snapshot dump turns
    that around, since sequence values are not transactional: a probe that
    took its ``nextval`` before the dump's sequence read but committed after
    the snapshot began shows the sequence moved without its row. A probe
    still running at step 1 could also take an id the step's own search
    would have had. Waiting for the probe to land covers both."""
    pending = set(dsns)
    end = time.monotonic() + deadline_s
    while pending:
        pending = {side for side in pending if not landed(dsns[side], before[side])}
        if not pending or time.monotonic() >= end:
            break
        time.sleep(0.25)
    return [f"warmup: {side}'s startup probe event did not land within {deadline_s:g}s"
            for side in sorted(pending)]


def prepare_template(scn: dict, tag: str, root: Path, owned: list[str],
                     stub: StubExtractor | None = None) -> str:
    template = pg.name(f"pl_cf_w2e_{tag}_t")
    owned.append(template)
    dsn = pg.create(template)
    home = daemons.make_home(root, f"{tag}-seed", scenario_config(scn, stub))
    extra = {**extractor_env(scn, stub, "seed"), **(scn.get("seed_env") or {})}
    d = daemons.python_daemon(home, daemons.free_port(),
                              daemons.base_env(home, side_env(scn, dsn, extra)))
    try:
        d.start(300)
        w1a.wait_settled([d.port], token=TOKEN)
        mcp = Mcp(d.port)
        mcp.init()
        for op in scn["seed"]:
            if op.get("after_stop"):
                continue
            r = run_op(op, d.port, mcp, dsn)
            if r and r["isError"]:
                raise RuntimeError(f"seed op failed: {op} -> {r}")
    finally:
        d.stop()
    for op in scn["seed"]:
        if op.get("after_stop"):
            run_op(op, 0, None, dsn)
    return template


def run_scenario(name: str, binary: Path, root: Path, mode: str, record: bool,
                 tol: dict, mutant: str | None = None) -> dict:
    owned = []
    try:
        return _run_scenario(name, binary, root, mode, record, tol, mutant, owned)
    finally:
        for database in reversed(owned):
            pg.drop(database)


def _run_scenario(name: str, binary: Path, root: Path, mode: str, record: bool,
                  tol: dict, mutant: str | None, owned: list[str]) -> dict:
    scn = SCENARIOS[name]
    tag = name.replace("-", "_")
    if mode == "golden":
        return run_golden(name, binary, root, tol, owned)
    stubs = start_stubs(scn, ("seed", "python", "rust"))
    try:
        return _run_live(name, scn, tag, binary, root, record, tol, mutant, owned, stubs)
    finally:
        close_stubs(stubs)


def _run_live(name: str, scn: dict, tag: str, binary: Path, root: Path, record: bool,
              tol: dict, mutant: str | None, owned: list[str],
              stubs: dict[str, StubExtractor]) -> dict:
    template = prepare_template(scn, tag, root, owned, stubs.get("seed"))
    dbs = {side: pg.name(f"pl_cf_w2e_{tag}_{suffix}")
           for side, suffix in (("python", "py"), ("rust", "rs"))}
    owned.extend(dbs.values())
    dsns = {k: pg.create(v, template=template) for k, v in dbs.items()}
    before = {k: dbstate.dump(dsn) for k, dsn in dsns.items()}
    procs = {}
    steps, stats = [], {}
    try:
        home = daemons.make_home(root, f"{tag}-py", scenario_config(scn, stubs.get("python")))
        homes = {"python": home}
        penv = side_env(scn, dsns["python"],
                        extractor_env(scn, stubs.get("python"), "python"))
        procs["python"] = daemons.python_daemon(home, daemons.free_port(),
                                                daemons.base_env(home, penv))
        renv = w1a.rust_env(side_env(scn, dsns["rust"],
                                     extractor_env(scn, stubs.get("rust"), "rust")))
        if mutant:
            renv["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
        home = daemons.make_home(root, f"{tag}-rs", scenario_config(scn, stubs.get("rust")))
        homes["rust"] = home
        procs["rust"] = daemons.rust_daemon(binary, home, daemons.free_port(), daemons.base_env(home, renv))
        envs = {"python": penv, "rust": renv}
        daemons.start_all(procs, 300)
        w1a.wait_settled([d.port for d in procs.values()], token=TOKEN)
        warmup_diffs = wait_warmups(dsns, before) if warmup_expected(scn) else []
        clients = {side: Mcp(d.port) for side, d in procs.items()}
        for client in clients.values():
            client.init()
        tokens: dict[str, str | None] = {"python": None, "rust": None}
        max_wall = 0.0
        for i, op in enumerate([{"op": "init"}] + scn["ops"]):
            row = {"step": i, "op": op, "diffs": [] if i else list(warmup_diffs)}
            if op["op"] != "init":
                started = time.monotonic()
                if op["op"] in HOME_OPS:
                    py, rs = home_op(op, homes["python"]), home_op(op, homes["rust"])
                elif op["op"] == "restart":
                    clients = restart_sides(op, scn, procs, homes, envs, stubs, binary)
                    py = rs = {"isError": False, "value": None}
                else:
                    py = run_op(with_pull_token(op, tokens["python"]), procs["python"].port,
                                clients["python"], dsns["python"])
                    rs = run_op(with_pull_token(op, tokens["rust"]), procs["rust"].port,
                                clients["rust"], dsns["rust"])
                if op.get("settle"):
                    settle_sides(op["settle"], dsns, stubs, scn)
                row["diffs"] += expect_diffs(op, dsns)
                row["wall_s"] = time.monotonic() - started
                max_wall = max(max_wall, row["wall_s"])
                for side, r in (("python", py), ("rust", rs)):
                    v = r.get("value") if isinstance(r, dict) else None
                    if isinstance(v, dict) and isinstance(v.get("commit_token"), str):
                        tokens[side] = v["commit_token"]
                if stubs:
                    py = replace_origin(py, stubs["python"].origin)
                    rs = replace_origin(rs, stubs["rust"].origin)
                row["python"], row["rust"] = py, rs
            if stubs:
                sent = {side: drain_side(stubs, scn, side) for side in ("python", "rust")}
                row["extractor"] = {side: extractor_summary(r) for side, r in sent.items()}
                if scn.get("shared_stub"):
                    # Each side sends its own key by design; drain_side
                    # already read each side's requests by it.
                    sent = {side: [{**r, "authorization": None} for r in recs]
                            for side, recs in sent.items()}
                row["diffs"] += extractor_diffs(sent["python"], sent["rust"])
                for side in ("python", "rust"):
                    row["diffs"] += extractor_key_diffs(side, scn, sent[side])
            raw_py, raw_rs = dbstate.dump(dsns["python"]), dbstate.dump(dsns["rust"])
            for side, raw in (("python", raw_py), ("rust", raw_rs)):
                answer = row.get(side) if op.get("name") == "memory_dream" else None
                row["diffs"] += run_clock_diffs(side, raw, answer)
            tsm_py, tsm_rs = ts_tokens(raw_py), ts_tokens(raw_rs)
            sp, np_, ep = normalize_state(raw_py, before["python"], tsm_py)
            sr, nr, er = normalize_state(raw_rs, before["rust"], tsm_rs)
            if op["op"] != "init":
                if op.get("name") == "memory_dream":
                    row["diffs"] += idle_diffs(row["python"], row["rust"],
                                               max_wall + IDLE_SLACK_S)
                    row["diffs"] += orphan_age_diffs(row["python"], row["rust"])
                    row["python"] = dream_tokens(row["python"], tsm_py)
                    row["rust"] = dream_tokens(row["rust"], tsm_rs)
                row["python"] = rename_prefixes(row["python"], ep)
                row["rust"] = rename_prefixes(row["rust"], er)
                row["diffs"] += compare_response(row["python"], row["rust"], tol, stats)
            row["diffs"] += compare_numeric(np_, nr, tol, stats)
            row["diffs"] += [f"db {d}" for d in state_and_events_diff(sp, sr)]
            steps.append(row)
            if record:
                row["_python_state"], row["_python_numeric"] = sp, np_
    finally:
        for d in procs.values():
            d.stop()
    result = {"scenario": name, "mutant": mutant, "steps": steps, "stats": stats}
    if record:
        GOLDENS.mkdir(parents=True, exist_ok=True)
        seen: set = set()
        (GOLDENS / f"{name}.json").write_text(json.dumps(
            {"scenario": name, "normalizers": "harness/w2e.py normalize_state, pack",
             "steps": [{"op": s["op"], "python": s.get("python"),
                        "state": pack(s["_python_state"], full=s["step"] == 0),
                        "numeric_new": pack_numeric(s["_python_numeric"], seen)} for s in steps]},
            indent=1, sort_keys=True), encoding="utf-8")
        for s in steps:
            s.pop("_python_state", None)
            s.pop("_python_numeric", None)
    return result


# Tables the write path touches: recorded every step (the rest only at init).
WRITE_TABLES = ("entries", "episodes", "meta", "client_sessions", "facts", "entities",
                "outcome_signals", "world_facts", "memory_trace_invalidations",
                "entry_reinstatement_decisions", "lessons", "edges", "store_decisions",
                "slot_reads", "entity_aliases", "relations")


def pack(state: dict, full: bool) -> dict:
    if full:
        return state
    return {"rows": {f"public.{t}": state["rows"][f"public.{t}"] for t in WRITE_TABLES
                     if f"public.{t}" in state["rows"]},
            "catalog": {"sequences": state["catalog"]["sequences"]}}


def pack_numeric(numeric: dict, seen: set) -> dict:
    """New entries only, embeddings as base64 little-endian float32."""
    import base64
    import struct
    out = {}
    for eid, v in numeric.items():
        if eid in ("facts", "world_facts", "lessons", "lesson_scores") or eid in seen:
            continue  # fact embeddings are compared live only (declared)
        seen.add(eid)
        emb = v["embedding"]
        out[str(eid)] = {"surprise": v["surprise"], "embedding_f32": base64.b64encode(
            struct.pack(f"<{len(emb)}f", *emb)).decode() if emb else None}
    return out


def unpack_numeric(packed: dict, into: dict) -> dict:
    import base64
    import struct
    for eid, v in packed.items():
        raw = base64.b64decode(v["embedding_f32"]) if v["embedding_f32"] else None
        into[int(eid)] = {"surprise": v["surprise"],
                          "embedding": list(struct.unpack(f"<{len(raw) // 4}f", raw)) if raw else None}
    return into


def run_golden(name: str, binary: Path, root: Path, tol: dict, owned: list[str]) -> dict:
    """Replay a recorded scenario against the Rust daemon alone, from a bank
    its own constructor builds. Seeded scenarios need the Python daemon to
    seed and are skipped (declared)."""
    scn = SCENARIOS[name]
    if scn["seed"]:
        return {"scenario": name, "skipped": "seeded through the Python daemon", "steps": [],
                "stats": {}}
    golden = json.loads((GOLDENS / f"{name}.json").read_text(encoding="utf-8"))
    tag = name.replace("-", "_")
    db = pg.name(f"pl_cf_w2e_{tag}_rs")
    owned.append(db)
    dsn = pg.create(db)
    before = dbstate.dump(dsn)
    home = daemons.make_home(root, f"{tag}-rs", scn.get("config"))
    stubs = start_stubs(scn, ("rust",))
    renv = w1a.rust_env(side_env(scn, dsn, extractor_env(scn, stubs.get("rust"))))
    d = daemons.rust_daemon(binary, home, daemons.free_port(), daemons.base_env(home, renv))
    steps, stats, numeric = [], {}, {}
    try:
        # The oracle's template was booted once to seed it and once to run,
        # so its writer-lease epoch is 2: boot the Rust bank twice as well.
        d.start(300)
        w1a.wait_settled([d.port], token=TOKEN)
        d.stop()
        d.start(300)
        w1a.wait_settled([d.port], token=TOKEN)
        client = Mcp(d.port)
        client.init()
        for i, rec in enumerate(golden["steps"]):
            op = rec["op"]
            row = {"step": i, "op": op, "diffs": []}
            rs = run_op(op, d.port, client, dsn) if op["op"] != "init" else None
            if stubs:
                if rs is not None:
                    rs = replace_origin(rs, stubs["rust"].origin)
                # No oracle requests to compare against: record the sequence
                # and report what the table left unanswered.
                sent = stubs["rust"].drain()
                row["extractor"] = {"rust": extractor_summary(sent)}
                row["diffs"] += extractor_unanswered("rust", sent)
            sr, nr, er = normalize_state(dbstate.dump(dsn), before)
            if rs is not None:
                row["rust"] = rename_prefixes(rs, er)
                row["diffs"] += compare_response(rec["python"], row["rust"], tol, stats)
            unpack_numeric(rec["numeric_new"], numeric)
            row["diffs"] += compare_numeric(numeric, nr, tol, stats)
            got = pack(sr, full=i == 0)
            if i:
                # Recordings that predate a write table are compared on theirs.
                got["rows"] = {k: v for k, v in got["rows"].items() if k in rec["state"]["rows"]}
            row["diffs"] += [f"db {x}" for x in state_and_events_diff(rec["state"], got)]
            steps.append(row)
    finally:
        d.stop()
        close_stubs(stubs)
    return {"scenario": name, "steps": steps, "stats": stats}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live", "golden", "mutants", "calibrate"])
    ap.add_argument("--rust-bin", type=Path, required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--mutants", nargs="*", choices=MUTANTS)
    args = ap.parse_args()
    args.rust_bin = args.rust_bin.resolve()
    root = run_root()
    names = args.only or list(SCENARIOS)
    tol = {} if args.mode == "calibrate" else json.loads(TOLERANCE_FILE.read_text(encoding="utf-8"))
    results = []
    if args.mode == "mutants":
        caught = {}
        for m in args.mutants or MUTANTS:
            diffs = 0
            for n in names:
                r = run_scenario(n, args.rust_bin, root, "live", False, tol, mutant=m)
                diffs += sum(len(s["diffs"]) for s in r["steps"])
            caught[m] = diffs
            print(f"mutant {m}: {diffs} diffs")
        summary = {"mutants": caught, "uncaught": [m for m, d in caught.items() if d == 0]}
        print(json.dumps(summary))
        if args.out:
            args.out.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        return 1 if summary["uncaught"] else 0
    for n in names:
        r = run_scenario(n, args.rust_bin, root, "golden" if args.mode == "golden" else "live",
                         args.record, tol)
        results.append(r)
        for s in r["steps"]:
            for d in s["diffs"][:6]:
                print(f"DIFF [{n}] step {s['step']} {json.dumps(s['op'])[:90]}: {d}")
    steps = sum(len(r["steps"]) for r in results)
    diff_steps = sum(1 for r in results for s in r["steps"] if s["diffs"])
    stats = {}
    for r in results:
        for k, v in r["stats"].items():
            stats[k] = min(stats.get(k, v), v) if k == "min_cosine" else max(stats.get(k, v), v)
    summary = {"scenarios": len(results), "steps": steps, "diff_steps": diff_steps, "stats": stats}
    print(json.dumps(summary))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "scenarios": results}, indent=1,
                                       default=str), encoding="utf-8")
    if args.mode == "calibrate":
        return 0
    return 1 if diff_steps else 0


if __name__ == "__main__":
    raise SystemExit(main())
