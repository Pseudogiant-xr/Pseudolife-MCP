"""Config read/write for the Cortex Console "knobs & dials".

The console edits a *curated whitelist* of scalar knobs — not the whole
:class:`AppConfig`. A whitelist (rather than blind dataclass reflection) lets us
attach a human description, a type-aware control spec, a sane range, and an
honest ``restart_required`` flag to each knob, and guarantees the console can
never poke a structural field (band presets, embedder device, storage write
mode) that would corrupt a running bank.

Read  -> the effective value of each knob from ``service.config``.
Write -> validate a ``{dotted.path: value}`` patch, merge it into
         ``<data_dir>/config.yaml`` atomically (timestamped backup first), and
         live-mutate ``service.config`` for knobs whose read path is live so the
         change takes effect without a restart. Restart-required knobs are
         persisted to YAML and flagged for the operator.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

# ── Knob registry ──────────────────────────────────────────────────────────
# Each entry:
#   path:    dotted attribute path under AppConfig (e.g. "memory.cortex.search_first")
#   group:   UI section
#   label:   short human label
#   type:    "bool" | "int" | "float" | "enum" | "string"
#   default: shipped default (shown as a reset target)
#   min/max/step: for numeric controls (optional)
#   options: for enum (list[str])
#   restart: True when the value is baked at init and needs a daemon restart
#   help:    one-line description (operator-facing)
#
# "restart": False means the read path consults service.config live, so a
# live-mutate takes effect on the next tool call.

KNOBS: list[dict[str, Any]] = [
    # ── Retrieval ──────────────────────────────────────────────────────────
    {"path": "memory.surprise_threshold", "group": "Retrieval",
     "label": "Surprise / novelty gate", "type": "float", "default": 0.0,
     "min": 0.0, "max": 1.0, "step": 0.01, "restart": False,
     "help": "Store gate: 1 − max cosine to existing entries. 0 stores "
             "everything; raise to dedup near-duplicate stores."},
    {"path": "memory.search_confidence_floor", "group": "Retrieval",
     "label": "Abstention floor", "type": "float", "default": 0.0,
     "min": 0.0, "max": 1.0, "step": 0.01, "restart": False,
     "help": "When the top search score is below this, search returns "
             "low_confidence so the agent can abstain. 0 = off."},
    {"path": "memory.top_k", "group": "Retrieval", "label": "Default top-k",
     "type": "int", "default": 8, "min": 1, "max": 50, "step": 1,
     "restart": False, "help": "Episodic retrieval slots across bands."},
    # The depth-recency knobs (recency_boost_enabled /
    # recency_base_half_life_s) left this surface with the 2026-08-15
    # flat default: the ramp is structurally inert with one band
    # (cms.py short-circuits on n=1), and a live-looking knob that can
    # do nothing is a lie. The config FIELDS remain for multi-band
    # presets via config.yaml.
    {"path": "memory.hide_superseded", "group": "Retrieval",
     "label": "Hide superseded", "type": "bool", "default": False,
     "restart": False,
     "help": "Drop entries flagged superseded from retrieval entirely "
             "(the pre-v0.7.3 filter). Off by default: they surface "
             "downranked so the agent can describe what a fact used to be. "
             "Hiding costs knowledge-update recall — debug/audit only."},
    # ── Reranker / BM25 ────────────────────────────────────────────────────
    {"path": "memory.reranker.enabled", "group": "Reranker",
     "label": "Cross-encoder reranker", "type": "bool", "default": False,
     "restart": False,
     "help": "Re-score top-N candidates with ms-marco-MiniLM. ~80MB model "
             "lazy-loaded on first use; ~200ms per search."},
    {"path": "memory.reranker.fusion_weight", "group": "Reranker",
     "label": "Reranker fusion weight", "type": "float", "default": 0.7,
     "min": 0.0, "max": 1.0, "step": 0.05, "restart": True,
     "help": "1.0 = pure cross-encoder, 0.0 = pure bi-encoder. Baked at init."},
    {"path": "memory.reranker.top_n", "group": "Reranker",
     "label": "Reranker top-N", "type": "int", "default": 20, "min": 1,
     "max": 100, "step": 1, "restart": True,
     "help": "How many candidates to rerank. Baked at init."},
    {"path": "memory.bm25.enabled", "group": "Reranker",
     "label": "BM25 hybrid pool", "type": "bool", "default": True,
     "restart": False,
     "help": "Sparse lexical retrieval fused with dense — catches exact "
             "tokens (function names, versions, error codes). On by "
             "default since 2026-07-25."},
    {"path": "memory.bm25.weight", "group": "Reranker",
     "label": "BM25 fusion weight", "type": "float", "default": 0.3,
     "min": 0.0, "max": 1.0, "step": 0.05, "restart": False,
     "help": "Contribution of normalised BM25 to the fused score."},
    {"path": "memory.bm25.cortex_enabled", "group": "Reranker",
     "label": "BM25 on cortex facts", "type": "bool", "default": False,
     "restart": False,
     "help": "Lexical fusion for cortex fact retrieval. Ships OFF: the "
             "2026-07-30 pre-registered A/B moved 56/78 served contexts with "
             "zero accuracy gain and cost ~1 oracle-gate question. Opt in "
             "for identifier-heavy corpora."},
    {"path": "memory.reranker.skip_margin", "group": "Reranker",
     "label": "Reranker skip margin", "type": "float", "default": 0.0,
     "min": 0.0, "max": 1.0, "step": 0.01, "restart": False,
     "help": "Skip the cross-encoder when the top-2 bi-encoder gap ≥ this "
             "(a decisive head can't be fixed by reranking). 0 = always "
             "rerank. CAUTION: skips return raw bi-encoder scores — don't "
             "combine with an abstention floor tuned to the fused scale."},
    # ── Retrieval log (learned-reranker training data, schema v31) ─────────
    {"path": "memory.retrieval_log.enabled", "group": "Retrieval log",
     "label": "Retrieval event log", "type": "bool", "default": True,
     "restart": True,
     "help": "Log every search's query + served entries, and implicit "
             "'used' labels when a served entry is later fetched/"
             "reinforced. Training data for a learned reranker; purely "
             "observational, no retrieval behaviour changes. Also gates "
             "the sweep-thread startup condition alongside dream.enabled "
             "(issue #178) — sweep thread starts at boot, so toggling "
             "needs a restart."},
    {"path": "memory.retrieval_log.retention_days", "group": "Retrieval log",
     "label": "Event retention (days)", "type": "int", "default": 365,
     "min": 1, "max": 3650, "step": 1, "restart": False,
     "help": "Events older than this are pruned on the dream-sweep tick "
             "(their use labels cascade)."},
    {"path": "memory.retrieval_log.use_window_seconds", "group": "Retrieval log",
     "label": "Use-label window (s)", "type": "int", "default": 3600,
     "min": 60, "max": 86400, "step": 60, "restart": False,
     "help": "A get/reinforce this long after a search still labels that "
             "search's served entry as used."},
    # ── MCP payloads (agent-side token cost, 2026-09-04 ledger) ───────────
    {"path": "memory.mcp.compact_payloads", "group": "MCP payloads",
     "label": "Compact tool payloads", "type": "bool", "default": True,
     "restart": False,
     "help": "Shape MCP responses for the agent's context window: search "
             "entry text truncated (memory_get returns it whole), the "
             "cortex block sized to the caller's top_k, and "
             "memory_fact_get's bookkeeping keys behind verbose=True. Off "
             "restores the pre-2026-09-04 payloads. Projection only — "
             "ranking and every eval number are unaffected."},
    {"path": "memory.mcp.entry_text_chars", "group": "MCP payloads",
     "label": "Search entry text cap", "type": "int", "default": 600,
     "min": 80, "max": 10000, "step": 20, "restart": False,
     "help": "Chars of a search hit's text served before truncation "
             "(marked truncated: true; memory_get returns it whole). "
             "Ignored when compact payloads are off. 600 (~150 tokens) "
             "clipped 88% of hits on the 2026-09-04 ledger bank and halved "
             "the served entry text; raise it for long-form notes whose "
             "tail carries the answer. Never applies to superseded_by_text, "
             "which is served whole."},
    # ── Cortex ─────────────────────────────────────────────────────────────
    {"path": "memory.cortex.search_first", "group": "Cortex",
     "label": "Cortex-first search", "type": "bool", "default": True,
     "restart": False,
     "help": "Surface canonical facts ahead of associative recall in search."},
    {"path": "memory.cortex.guard_min_score", "group": "Cortex",
     "label": "Cortex guard min score", "type": "float", "default": 0.2,
     "min": 0.0, "max": 1.0, "step": 0.01, "restart": False,
     "help": "A current fact must score ≥ this to count as a confident answer "
             "(and to suppress abstention)."},
    {"path": "memory.cortex.protect_provenance", "group": "Cortex",
     "label": "Protect provenance", "type": "bool", "default": True,
     "restart": False,
     "help": "A weaker-tier conflicting write is parked as a contender "
             "instead of silently overwriting (user > action > agent)."},
    {"path": "memory.cortex.supersede_confidence_margin", "group": "Cortex",
     "label": "Supersede margin", "type": "float", "default": 0.15,
     "min": 0.0, "max": 1.0, "step": 0.01, "restart": False,
     "help": "Confidence a same-tier write must exceed to supersede vs park."},
    {"path": "memory.cortex.auto_promote", "group": "Cortex",
     "label": "Regex auto-promote (legacy)", "type": "bool", "default": False,
     "restart": False,
     "help": "Deterministic regex promotion on every store. Off by default — "
             "it mis-splits compound entity names. Prefer the dream pass."},
    {"path": "memory.cortex.dream_slot_match_threshold", "group": "Cortex",
     "label": "Dream slot-match floor", "type": "float", "default": 0.0,
     "min": 0.0, "max": 1.0, "step": 0.01, "restart": False,
     "help": "Dream-path slot resolver: a paraphrased claim adopts an existing "
             "slot when its value-free embedding cosine ≥ this. 0 = exact-key "
             "only."},
    {"path": "memory.cortex.pin_constraints", "group": "Cortex",
     "label": "Pin constraint facts", "type": "bool", "default": True,
     "restart": False,
     "help": "Serve in-scope constraint-labelled facts ahead of cosine "
             "ranking in search's cortex block and recall (schema v35). "
             "Off = plain ranking; an unlabelled bank is unaffected."},
    # ── Dream ──────────────────────────────────────────────────────────────
    {"path": "memory.dream.enabled", "group": "Dream", "label": "Dream sweep",
     "type": "bool", "default": True, "restart": True,
     "help": "Background MIRAS→cortex consolidation. Sweep thread starts at "
             "boot, so toggling needs a restart."},
    {"path": "memory.dream.min_batch", "group": "Dream",
     "label": "Min backlog to fire", "type": "int", "default": 8, "min": 1,
     "max": 1000, "step": 1, "restart": False,
     "help": "Unconsolidated entries required before a dream fires."},
    {"path": "memory.dream.idle_seconds", "group": "Dream",
     "label": "Quiescence (s)", "type": "float", "default": 600.0,
     "min": 0.0, "max": 86400.0, "step": 60.0, "restart": False,
     "help": "Idle time required before a dream fires."},
    {"path": "memory.dream.max_batch", "group": "Dream",
     "label": "Max batch", "type": "int", "default": 40, "min": 1, "max": 1000,
     "step": 1, "restart": False, "help": "Cap on entries consolidated per dream."},
    {"path": "memory.dream.sweep_interval_seconds", "group": "Dream",
     "label": "Sweep interval (s)", "type": "float", "default": 600.0,
     "min": 30.0, "max": 86400.0, "step": 30.0, "restart": True,
     "help": "How often the daemon checks the dream trigger. Baked at boot."},
    {"path": "memory.dream.extract_relations", "group": "Dream",
     "label": "Extract graph relations", "type": "bool", "default": True,
     "restart": False,
     "help": "Dream also extracts (src,relation,dst) triples into the graph."},
    {"path": "memory.dream.write_dedup_min_jaccard", "group": "Dream",
     "label": "Write-dedup Jaccard floor", "type": "float", "default": 0.6,
     "min": 0.0, "max": 1.0, "step": 0.05, "restart": False,
     "help": "New dream entity whose name-token Jaccard vs an existing name "
             "reaches this files a merge proposal for review. 0 = off."},
    {"path": "memory.dream.alias_candidate_min_cosine", "group": "Dream",
     "label": "Alias-candidate cosine floor", "type": "float", "default": 0.5,
     "min": 0.0, "max": 1.0, "step": 0.05, "restart": False,
     "help": "New dream entity whose name-embedding cosine vs an existing "
             "entity reaches this files a merge proposal for review (semantic "
             "complement to the Jaccard detector). 0 = off."},
    {"path": "memory.dream.min_relation_confidence", "group": "Dream",
     "label": "Relation confidence floor", "type": "float", "default": 0.2,
     "min": 0.0, "max": 1.0, "step": 0.05, "restart": False,
     "help": "Dream-extracted graph edges scoring below this are dropped at "
             "the source (hard type-violations score ≤0.175). 0 = write "
             "everything."},
    {"path": "memory.dream.relation_quarantine_below", "group": "Dream",
     "label": "Relation quarantine below", "type": "float", "default": 0.5,
     "min": 0.0, "max": 1.0, "step": 0.05, "restart": False,
     "help": "Edges at/above the floor but below this route to the "
             "edge-proposal review queue instead of the live graph. At 0.5 "
             "this quarantines exactly the untyped co-mention edges (0.45). "
             "0 = off."},
    {"path": "memory.dream.retype_quarantined_max", "group": "Dream",
     "label": "Retype pass cap (per dream)", "type": "int", "default": 3,
     "min": 0, "max": 20, "step": 1, "restart": False,
     "help": "Quarantined untyped pairs re-asked for a TYPED relation each "
             "dream (~44% name a real relationship with the wrong label). "
             "No-ops on an empty quarantine. 0 = off."},
    {"path": "memory.dream.literal_gate", "group": "Dream",
     "label": "Literal-faithfulness gate", "type": "enum",
     "default": "enforce", "options": ["off", "log", "enforce"],
     "restart": False,
     "help": "Digit-bearing tokens in a dreamed value must appear in the "
             "source notes: enforce drops unbacked claims, log only flags "
             "them. Default enforce by measured evidence (fires on 1.3-1.7% "
             "of gateable claims, almost all genuinely unbacked)."},
    {"path": "memory.dream.literal_gate_scope", "group": "Dream",
     "label": "Literal gate scope", "type": "enum", "default": "batch",
     "options": ["batch", "source"], "restart": False,
     "help": "Source corpus for the gate: batch = union of the pull's note "
             "texts (default; derived sums and cross-note values are "
             "measured false-drop classes under per-note gating); source = "
             "only the note the claim cites."},
    {"path": "memory.dream.span_gate", "group": "Dream",
     "label": "Provenance-span gate", "type": "enum",
     "default": "off", "options": ["off", "log", "contend"],
     "restart": False,
     "help": "A dreamed claim must carry a verbatim quote from the note it "
             "cites: log counts unbacked claims, contend parks them as "
             "contenders (never a silent drop). Off by default — the live "
             "prompt does not emit quotes; needs the v9 stance+quote prompt "
             "and the gate-4 firing audit before contend is proposed."},
    {"path": "memory.dream.chronicle", "group": "Dream",
     "label": "Chronicle events", "type": "bool", "default": True,
     "restart": False,
     "help": "Dream also extracts dated occurrences (a separate extractor "
             "call per batch; claims are untouched by construction) into "
             "chronicle_events; temporally-cued searches serve them. On by "
             "default since the 2026-08-12 soak review. Needs Postgres; an "
             "events-pass failure never stalls claims."},
    {"path": "memory.dream.digest_enabled", "group": "Dream",
     "label": "Session digests", "type": "bool", "default": False,
     "restart": False,
     "help": "Idle dream cycle writes one narrative prose digest per closed "
             "session episode (a source=\"digest\" band entry; never re-mined "
             "for facts). Off by default pending human review of the sidecar "
             "quality probe (evals/digest_sidecar_probe.py). First enable "
             "backfills all history, digest_max_per_cycle (4) episodes per "
             "dream."},
    {"path": "memory.dream.digest_target_chars", "group": "Dream",
     "label": "Digest length target (chars)", "type": "int", "default": 1200,
     "min": 200, "max": 4000, "step": 100, "restart": False,
     "help": "Prose length target passed to the digest prompt. Re-targeted "
             "from 800 to 1200 after the 2026-08-27 sidecar probe (9 "
             "digests, 3 runs): the extractor wrote 1019-1908 chars against "
             "the 800 target, and the overrun carried retrievable specifics "
             "(versions, deadline changes, error names) — the target now "
             "states the observed natural length instead of fighting it."},
    {"path": "memory.dream.digest_context_chars", "group": "Dream",
     "label": "Digest context cap (chars)", "type": "int", "default": 24000,
     "min": 1000, "max": 200000, "step": 1000, "restart": False,
     "help": "Max session-context characters per summarize call; longer "
             "sessions split on line boundaries and map-reduce merge. 24000 "
             "≈ 6K tokens, sized for the bundled CPU sidecar — raise it on "
             "a large-context GPU endpoint to avoid the split."},
    # ── Deep dream ─────────────────────────────────────────────────────────
    # Read from service.config on every sweep batch (deep_dream_judge).
    {"path": "memory.deep_dream.judge_mode", "group": "Deep dream",
     "label": "Step-C merge judge", "type": "enum",
     "options": ["off", "shadow", "auto-reject", "auto"], "default": "shadow",
     "restart": False,
     "help": "How the autonomous Step-C judge handles pending merge "
             "proposals: \"shadow\" records verdicts without applying them; "
             "\"auto-reject\" additionally applies reject verdicts at/above "
             "the confidence gate (judge_reject_min_confidence, 0.8) and, "
             "with the second opinion on, two agreeing rejects at mean >= "
             "judge_reject_min_confidence_2 (0.7); \"auto\" additionally "
             "folds a pair when two independent accepts agree on "
             "non-low-differential evidence at mean >= "
             "judge_accept_min_confidence (0.6) and only when the two "
             "opinions come from different models (judge_second_model) — "
             "the only path that ever auto-applies an accept (6/6 on the "
             "2026-09-02 panel, evals/results/queue-judge-panel-20260902.json"
             "). Caveat: both "
             "auto modes are only measured-safe on an Opus-class judge "
             "endpoint (live auto-reject precision 1.000, "
             "evals/results/judge-shadow-live-20260821.json); the "
             "2026-08-16 judge ladder shows weaker judges mis-reject with "
             "confident scores (local Qwen 0.918, confidence "
             "uninformative), so leave \"shadow\" unless the dream "
             "extractor or judge_url resolves to an Opus-class model."},
    {"path": "memory.deep_dream.judges_enabled", "group": "Deep dream",
     "label": "Review-queue judges (all)", "type": "bool", "default": True,
     "restart": False,
     "help": "The one switch for every judge stage (merge, link, junk, "
             "store-curation, candidates): off = no model verdicts at all, "
             "the mechanical tick keeps running. The two apply-time "
             "mechanics keep their own switches (analyzer_file_duplicates, "
             "orphan_sweep)."},
    {"path": "memory.deep_dream.judge_snippet_max_chars", "group": "Deep dream",
     "label": "Merge-judge snippet chars", "type": "int", "default": 240,
     "min": 0, "max": 20000, "step": 100, "restart": False,
     "help": "Per-snippet cap on the evidence the merge judge reads, "
             "separate from the review surfaces' snippet_max_chars. 0 = "
             "unbounded. Leave at 240 (the cap every published judge number "
             "was measured at): the 2026-09-03 ladder rerun at 3000 chars "
             "made the judge accept more and be wrong more often (accept "
             "precision 0.70 vs 0.85; two-vote auto-fold 6/7 vs 4/4; "
             "evals/results/queue-judge-ladder-20260903-fulllen.json). "
             "Raise it only behind a new ladder run."},
    {"path": "memory.deep_dream.judge_second_opinion", "group": "Deep dream",
     "label": "Merge judge second opinion", "type": "bool", "default": True,
     "restart": False,
     "help": "Re-judge a pending merge proposal once more in a fresh batch "
             "(optionally judge_second_model) after its first verdict sat "
             "below the single-vote gate; the two-vote gates above apply "
             "only when this is on."},
    {"path": "memory.deep_dream.judge_second_model", "group": "Deep dream",
     "label": "Merge judge second model", "type": "string", "default": None,
     "restart": False,
     "suggestions": ["claude-fable-5", "claude-opus-5", "claude-sonnet-5",
                     "gpt-5.6-terra", "gpt-5.6-luna"],
     "help": "Model for the merge judge's SECOND opinion, served by the same "
             "endpoint as the first (judge_url, else the dream extractor; the "
             "CLI shims honour claude-* / gpt-* names per request). Empty = "
             "the same model in a fresh batch, which is enough to double-check "
             "a reject but never authorizes a fold: \"auto\" accepts require "
             "the two opinions to come from DIFFERENT models (2026-09-02 "
             "panel: claude-fable-5 as the second voter went 6/6 accepts, 8/8 "
             "rejects on the 63-row ladder). Read on every sweep batch; each "
             "second opinion is one call to this model."},
    {"path": "memory.deep_dream.link_judge_mode", "group": "Deep dream",
     "label": "Link judge", "type": "enum",
     "options": ["off", "shadow", "auto"], "default": "shadow",
     "restart": False,
     "help": "Autonomous verdicts on pending link proposals (schema v36): "
             "\"shadow\" records; \"auto\" promotes accept verdicts "
             "at/above link_accept_min_confidence (0.8) to live edges and "
             "rejects at/above link_reject_min_confidence (0.8); a retype "
             "is recorded with its corrected relation but never auto-written "
             "(first ladder: retype 0/1). Edges are "
             "reversible (memory_graph_unrelate), which is why this queue "
             "may run auto; measured per arm by evals/queue_judge_ladder.py."},
    {"path": "memory.deep_dream.junk_judge_mode", "group": "Deep dream",
     "label": "Junk judge", "type": "enum",
     "options": ["off", "shadow", "auto"], "default": "shadow",
     "restart": False,
     "help": "Autonomous verdicts on the evidence-bearing junk proposals the "
             "zero-structure auto-delete skips: \"auto\" keeps at/above "
             "junk_keep_min_confidence (0.8) and deletes at/above "
             "junk_delete_min_confidence (0.85) ONLY under the evidence bar "
             "(degree <= junk_max_auto_degree, at most one fact slot); "
             "richer nodes stay pending with the verdict attached."},
    {"path": "memory.deep_dream.curation_judge_mode", "group": "Deep dream",
     "label": "Store-curation judge", "type": "enum",
     "options": ["off", "shadow", "auto-distinct", "auto"], "default": "shadow",
     "restart": False,
     "help": "Autonomous verdicts on the lesson/world duplicate listings: "
             "\"auto-distinct\" applies distinct verdicts (a reversible "
             "dismissal) at/above curation_distinct_min_confidence (0.8); "
             "\"auto\" additionally forgets the losing slot of a duplicate "
             "verdict at/above curation_forget_min_confidence (0.9) after "
             "folding the judge's carry-over into the survivor (lessons)."},
    {"path": "memory.deep_dream.candidate_judge_mode", "group": "Deep dream",
     "label": "Step-C candidate judge", "type": "enum",
     "options": ["off", "shadow", "auto"], "default": "off",
     "restart": False,
     "help": "Once per deep apply, judge the dream's link CANDIDATES: "
             "\"shadow\" logs the verdict tally; \"auto\" files proposals "
             "(then settled by the link judge) or dismisses co-mention "
             "pairs, at/above candidate_min_confidence (0.6). A dismissal "
             "marks the pair distinct for the merge analyzer too, so the "
             "prompt leaves same-referent pairs alone rather than "
             "dismissing them."},
    {"path": "memory.deep_dream.analyzer_file_duplicates", "group": "Deep dream",
     "label": "File analyzer duplicates", "type": "bool", "default": True,
     "restart": False,
     "help": "Each deep apply files the Console's live duplicate findings "
             "into the merge queue (file/concept pairs into the link queue as "
             "implements), so the judges see them; they were never filed "
             "anywhere before 2026-09-02."},
    {"path": "memory.deep_dream.orphan_sweep", "group": "Deep dream",
     "label": "Unreachable-orphan sweep", "type": "bool", "default": False,
     "restart": False,
     "help": "Each deep apply deletes entities that carry no evidence at all "
             "(no edge — superseded included —, fact, lesson, alias, scope, "
             "proposal or mentioning entry) once older than "
             "orphan_min_age_days (7), at most orphan_max_per_apply (50) per "
             "pass. Off by default: the one destructive switch that would "
             "fire on the first apply after an upgrade. Audited as "
             "dream-auto / deleted."},
    # ── Extractor ──────────────────────────────────────────────────────────
    # All live: build_extractor() constructs the client fresh on every dream
    # invocation from service.config.
    {"path": "memory.dream.extractor_model_override", "group": "Extractor",
     "label": "Dreamer model override", "type": "string", "default": None,
     "restart": False,
     "suggestions": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
                     "claude-fable-5", "gpt-5.6-sol", "gpt-5.6-terra",
                     "gpt-5.6-luna"],
     "help": "Model-only override for the primary extractor — wins over BOTH "
             "env and config ownership, so the dreamer model can be switched "
             "without re-owning the endpoint wiring. Any model id the wired "
             "endpoint serves works here (LM Studio/Ollama/vLLM model names "
             "included). The Claude CLI shim honours claude-* names per "
             "request and the Codex CLI shim gpt-* names; the fallback "
             "sidecar is never affected. Empty = the endpoint's own "
             "default."},
    {"path": "memory.dream.extractor_reasoning_effort", "group": "Extractor",
     "label": "Dreamer reasoning effort", "type": "string", "default": None,
     "restart": False,
     "suggestions": ["low", "medium", "high", "xhigh"],
     "help": "Reasoning effort for the primary extractor, sent per request "
             "as reasoning_effort. Empty = never sent — the endpoint's own "
             "default serves (for the CLI shims that is the host CLI "
             "config). The Claude CLI shim maps it to claude --effort "
             "(low/medium/high/xhigh/max), the Codex CLI shim to "
             "model_reasoning_effort (minimal/low/medium/high/xhigh); "
             "OpenAI-compatible servers read the field natively; most local "
             "runtimes ignore the unknown field, though a hosted API may "
             "reject an unsupported value with a clear 400. The fallback "
             "sidecar is never affected."},
    {"path": "memory.dream.extractor_source", "group": "Extractor",
     "label": "Settings source", "type": "enum", "default": "env",
     "options": ["env", "config"], "restart": False,
     "help": "Who owns the endpoint settings: env = PSEUDOLIFE_DREAM_* vars "
             "(compose/ops contract) override; config = the values below win "
             "and env vars are ignored. Switch to config to edit here."},
    {"path": "memory.dream.extractor_base_url", "group": "Extractor",
     "label": "Endpoint base URL", "type": "string", "format": "url",
     "default": None, "restart": False,
     "suggestions": ["http://host.docker.internal:8082/v1",
                     "http://host.docker.internal:8086/v1",
                     "http://pseudolife-extractor:8081/v1",
                     "http://host.docker.internal:1234/v1",
                     "http://host.docker.internal:11434/v1",
                     "http://127.0.0.1:8081/v1"],
     "help": "OpenAI-compatible /v1 endpoint — any server speaking the "
             "protocol works. From inside the container the host machine is "
             "host.docker.internal (Claude CLI shim = :8082; Codex CLI "
             "shim = :8086; sidecar = pseudolife-extractor:8081; LM "
             "Studio = :1234; Ollama = :11434). Effective only when "
             "settings source = config."},
    {"path": "memory.dream.extractor_model", "group": "Extractor",
     "label": "Model name", "type": "string", "default": None, "restart": False,
     "suggestions": ["extractor", "claude-opus-5", "claude-sonnet-5",
                     "claude-haiku-4-5", "gpt-5.6-terra"],
     "help": "Model id the endpoint expects — any name the endpoint serves "
             "works (the bundled sidecar serves \"extractor\"; LM "
             "Studio/Ollama use their loaded-model names). Against the "
             "Claude CLI shim (:8082) a claude-* name — or the Codex CLI "
             "shim (:8086) a gpt-* name — switches the served model per "
             "request; pick the dreamer here without restarting anything. "
             "Effective only when settings source = config."},
    {"path": "memory.dream.extractor_timeout_seconds", "group": "Extractor",
     "label": "Call timeout (s)", "type": "float", "default": 240.0,
     "min": 10.0, "max": 3600.0, "step": 10.0, "restart": False,
     "help": "Hard timeout per extractor call. CPU sidecars need generous "
             "headroom (E4B ≈ 480s); GPU endpoints can go much lower. "
             "Effective only when settings source = config."},
    {"path": "memory.dream.extractor_max_tokens", "group": "Extractor",
     "label": "Max output tokens", "type": "int", "default": 2048,
     "min": 128, "max": 32768, "step": 128, "restart": False,
     "help": "Output budget per extractor call (truncated JSON parses to "
             "fewer/zero claims). Effective only when settings source = "
             "config."},
    {"path": "memory.dream.extractor_mode", "group": "Extractor",
     "label": "Extractor mode", "type": "enum", "default": "auto",
     "options": ["auto", "primary", "fallback"], "restart": False,
     "help": "auto = use the primary endpoint, fall back to the fallback "
             "endpoint when the primary probe fails; primary = never fall "
             "back (outages hold consolidation); fallback = skip the "
             "primary entirely (sovereign-only override). Effective only "
             "when settings source = config."},
    {"path": "memory.dream.fallback_base_url", "group": "Extractor",
     "label": "Fallback base URL", "type": "string", "format": "url",
     "default": None, "restart": False,
     "suggestions": ["http://pseudolife-extractor:8081/v1"],
     "help": "OpenAI-compatible /v1 endpoint used when the primary is "
             "unreachable (or mode = fallback). Empty disables selection "
             "entirely — single-extractor behavior. Effective only when "
             "settings source = config."},
    {"path": "memory.dream.fallback_model", "group": "Extractor",
     "label": "Fallback model", "type": "string", "default": None,
     "restart": False, "suggestions": ["extractor"],
     "help": "Model id the fallback endpoint expects (the bundled sidecar "
             "serves \"extractor\"). Effective only when settings source = "
             "config."},
    # ── Lessons ────────────────────────────────────────────────────────────
    {"path": "memory.lessons.enabled", "group": "Lessons",
     "label": "Procedural lessons", "type": "bool", "default": True,
     "restart": False, "help": "Enable the procedural / outcome memory store."},
    {"path": "memory.lessons.top_k", "group": "Lessons", "label": "Lesson top-k",
     "type": "int", "default": 5, "min": 1, "max": 50, "step": 1,
     "restart": False, "help": "Default lessons returned by lesson search."},
    {"path": "memory.lessons.signal_retention_days", "group": "Lessons",
     "label": "Signal retention (days)", "type": "int", "default": 30, "min": 1,
     "max": 3650, "step": 1, "restart": False,
     "help": "Outcome signals older than this are pruned on the dream sweep."},
    {"path": "memory.lessons.synthesize_in_dream", "group": "Lessons",
     "label": "Synthesize in dream", "type": "bool", "default": True,
     "restart": False,
     "help": "Dream drains outcome signals into lessons. Off = signals are "
             "still pruned by retention but never become lessons."},
    {"path": "memory.lessons.infer_outcomes", "group": "Lessons",
     "label": "Infer missing outcomes", "type": "bool", "default": True,
     "restart": False,
     "help": "Infer outcome signals for episodes that closed with entries "
             "but zero explicit outcomes (origin=inferred; lessons from "
             "all-inferred batches start at confidence 0.4)."},
    {"path": "memory.lessons.infer_outcomes_max_signals", "group": "Lessons",
     "label": "Inferred signals cap", "type": "int", "default": 3,
     "min": 0, "max": 10, "step": 1, "restart": False,
     "help": "Max inferred outcome signals per closed episode. 0 disables "
             "inference regardless of the toggle above."},
    # ── Recall ─────────────────────────────────────────────────────────────
    {"path": "memory.recall.driver", "group": "Recall", "label": "Recall driver",
     "type": "enum", "default": "mechanical",
     "options": ["mechanical", "llm"], "restart": False,
     "help": "Seed resolution for multi-hop recall: mechanical (word-match, "
             "no model) or llm (dream extractor names seeds)."},
    {"path": "memory.recall.default_hops", "group": "Recall",
     "label": "Default hops", "type": "int", "default": 3, "min": 1, "max": 5,
     "step": 1, "restart": False, "help": "Max graph hops per recall (≤5)."},
    {"path": "memory.recall.default_top_k", "group": "Recall",
     "label": "Recall top-k", "type": "int", "default": 5, "min": 1, "max": 50,
     "step": 1, "restart": False, "help": "Results per internal recall search."},
    {"path": "memory.recall.max_searches_per_hop", "group": "Recall",
     "label": "Re-queries per hop", "type": "int", "default": 6, "min": 0,
     "max": 50, "step": 1, "restart": False,
     "help": "Per hop, re-query only the top N newly discovered entities "
             "(seed-hit mentions first, then lowest degree). The rest are "
             "still returned with their facts. 0 = unlimited."},
    {"path": "memory.recall.max_total_searches", "group": "Recall",
     "label": "Search ceiling per call", "type": "int", "default": 31,
     "min": 0, "max": 200, "step": 1, "restart": False,
     "help": "Hard cap on searches per recall call, seed search included. "
             "On reaching it the walk stops and the response is flagged "
             "truncated. 31 = 1 + 6 x 5, a backstop above the most the "
             "per-hop cap can spend at the tool's max 5 hops. "
             "0 = no ceiling."},
    {"path": "memory.recall.time_budget_seconds", "group": "Recall",
     "label": "Recall time budget (s)", "type": "float", "default": 20.0,
     "min": 0.0, "max": 300.0, "step": 1.0, "restart": False,
     "help": "Return what the walk has, flagged truncated, once it has run "
             "this long. 0 = no budget."},
    {"path": "memory.recall.skip_part_of_expansion", "group": "Recall",
     "label": "Skip part-of re-queries", "type": "bool", "default": False,
     "restart": False,
     "help": "Entities reached only by part-of edges are returned with "
             "their facts but never spend a search."},
    # ── Retention ──────────────────────────────────────────────────────────
    {"path": "memory.compaction.enabled", "group": "Retention",
     "label": "Superseded-row compaction", "type": "bool", "default": True,
     "restart": False,
     "help": "Purge old superseded fact/world/lesson versions on the dream "
             "sweep (keep-newest-N per slot + min-age)."},
    {"path": "memory.compaction.keep_per_slot", "group": "Retention",
     "label": "Versions kept per slot", "type": "int", "default": 3,
     "min": 0, "max": 50, "step": 1, "restart": False,
     "help": "Superseded versions always kept per (entity, attribute) slot."},
    {"path": "memory.compaction.min_age_days", "group": "Retention",
     "label": "Min age before purge (days)", "type": "float", "default": 30.0,
     "min": 0.0, "max": 365.0, "step": 1.0, "restart": False,
     "help": "A superseded version younger than this is never purged, "
             "whatever the per-slot count."},
    {"path": "memory.dream.runs_keep", "group": "Retention",
     "label": "Dream runs kept", "type": "int", "default": 50,
     "min": 1, "max": 1000, "step": 1, "restart": False,
     "help": "Newest N dream-run audit rows (and their pre-image journals) "
             "survive the sweep prune. The journal is the rollback source, "
             "so this bounds how far back a dream pass stays revertible."},
    # ── Presentation ───────────────────────────────────────────────────────
    {"path": "time.relative_age", "group": "Presentation",
     "label": "Relative age labels", "type": "bool", "default": True,
     "restart": False,
     "help": 'Add a human "3 days ago" age to serialised canonical facts.'},
]

_KNOB_BY_PATH = {k["path"]: k for k in KNOBS}


# ── path helpers ───────────────────────────────────────────────────────────

def _get_by_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        cur = getattr(cur, part)
    return cur


def _set_by_path(obj: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        cur = getattr(cur, part)
    setattr(cur, parts[-1], value)


def _nested_set(d: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


# ── validation / coercion ──────────────────────────────────────────────────

def _coerce(knob: dict, value: Any) -> Any:
    t = knob["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if t == "int":
        v = int(value)
    elif t == "float":
        v = float(value)
    elif t == "enum":
        v = str(value)
        if v not in knob.get("options", []):
            raise ValueError(
                f"{knob['path']}: {v!r} not in {knob.get('options')}")
        return v
    else:  # string
        if value is None:
            return None                      # explicit clear
        v = str(value).strip()
        if not v:
            return None                      # empty field clears the value
        if (knob.get("format") == "url"
                and not v.lower().startswith(("http://", "https://"))):
            raise ValueError(f"{knob['path']}: must be an http(s) URL")
        return v
    lo, hi = knob.get("min"), knob.get("max")
    if lo is not None and v < lo:
        raise ValueError(f"{knob['path']}: {v} < min {lo}")
    if hi is not None and v > hi:
        raise ValueError(f"{knob['path']}: {v} > max {hi}")
    return v


# ── public API ─────────────────────────────────────────────────────────────

def config_path_for(service: Any) -> Path:
    """Where the editable ``config.yaml`` lives — the env override if set,
    else ``<data_dir>/config.yaml`` (matches MemoryService construction)."""
    env = os.environ.get("PSEUDOLIFE_MCP_CONFIG")
    if env:
        return Path(env)
    return Path(getattr(service, "data_dir", ".")) / "config.yaml"


def read_config(service: Any) -> dict[str, Any]:
    """Effective knob values + metadata, grouped for the UI."""
    cfg = service.config
    groups: dict[str, list[dict]] = {}
    for knob in KNOBS:
        try:
            current = _get_by_path(cfg, knob["path"])
        except AttributeError:
            continue  # knob not present in this config version — skip gracefully
        item = {k: knob.get(k) for k in (
            "path", "label", "type", "default", "min", "max", "step",
            "options", "restart", "help", "format", "suggestions")
            if knob.get(k) is not None}
        item["value"] = current
        groups.setdefault(knob["group"], []).append(item)
    return {
        "config_path": str(config_path_for(service)),
        "groups": [{"name": g, "knobs": ks} for g, ks in groups.items()],
    }


def write_config(service: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """Validate a ``{dotted.path: value}`` patch, persist to YAML (atomic,
    backed up), and live-mutate ``service.config`` for live knobs.

    Returns ``{"applied": [...], "restart_required": [...], "config_path": ...,
    "backup": ...|None}``. Raises ``ValueError`` on an unknown/invalid knob
    (the caller maps that to a 400).
    """
    if not isinstance(patch, dict) or not patch:
        raise ValueError("empty patch")

    coerced: dict[str, Any] = {}
    for path, raw in patch.items():
        knob = _KNOB_BY_PATH.get(path)
        if knob is None:
            raise ValueError(f"unknown knob: {path}")
        coerced[path] = _coerce(knob, raw)

    cfg_path = config_path_for(service)
    # Merge into existing YAML (preserve unknown keys the console doesn't manage).
    existing: dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    for path, value in coerced.items():
        _nested_set(existing, path, value)

    backup = None
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.exists():
        backup = str(cfg_path) + f".{time.strftime('%Y%m%d-%H%M%S')}.bak"
        shutil.copy2(cfg_path, backup)

    # Atomic write: temp in the same dir, then os.replace.
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, cfg_path)

    applied, restart = [], []
    for path, value in coerced.items():
        knob = _KNOB_BY_PATH[path]
        # Live-mutate in-process for knobs whose read path is live. Restart knobs
        # are persisted only (next boot reads them); mutating in place would lie
        # about the running behaviour.
        if not knob.get("restart"):
            try:
                _set_by_path(service.config, path, value)
                applied.append(path)
            except AttributeError:
                restart.append(path)
        else:
            restart.append(path)

    return {
        "applied": applied,
        "restart_required": restart,
        "config_path": str(cfg_path),
        "backup": backup,
    }
