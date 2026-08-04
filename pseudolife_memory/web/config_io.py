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
    {"path": "memory.recency_boost_enabled", "group": "Retrieval",
     "label": "Depth recency boost", "type": "bool", "default": False,
     "restart": False,
     "help": "Scale scores by a 0.4->0.0 ramp over band depth. Off since "
             "2026-07-25: depth tracks promotion history (i.e. surprise), "
             "not age, so the ramp could rank a weaker shallow match above "
             "a stronger deep one."},
    {"path": "memory.recency_base_half_life_s", "group": "Retrieval",
     "label": "Recency half-life (s)", "type": "float", "default": 86400.0,
     "min": 0.0, "max": 2592000.0, "step": 3600.0, "restart": False,
     "help": "Base recency half-life at band depth 0 (doubles per depth). "
             "MCP build uses 1 day; chat used 1 hour. Only bites when the "
             "depth recency boost above is enabled."},
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
    # ── Extractor ──────────────────────────────────────────────────────────
    # All live: build_extractor() constructs the client fresh on every dream
    # invocation from service.config.
    {"path": "memory.dream.extractor_model_override", "group": "Extractor",
     "label": "Dreamer model override", "type": "string", "default": None,
     "restart": False,
     "suggestions": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
                     "claude-fable-5"],
     "help": "Model-only override for the primary extractor — wins over BOTH "
             "env and config ownership, so the dreamer model can be switched "
             "without re-owning the endpoint wiring. The Claude CLI shim "
             "honours claude-* names per request; the fallback sidecar is "
             "never affected. Empty = the endpoint's own default."},
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
                     "http://pseudolife-extractor:8081/v1",
                     "http://host.docker.internal:1234/v1",
                     "http://host.docker.internal:11434/v1",
                     "http://127.0.0.1:8081/v1"],
     "help": "OpenAI-compatible /v1 endpoint. From inside the container the "
             "host machine is host.docker.internal (Claude CLI shim = :8082; "
             "sidecar = pseudolife-extractor:8081; LM Studio = :1234; "
             "Ollama = :11434). Effective only when settings source = "
             "config."},
    {"path": "memory.dream.extractor_model", "group": "Extractor",
     "label": "Model name", "type": "string", "default": None, "restart": False,
     "suggestions": ["extractor", "claude-opus-5", "claude-sonnet-5",
                     "claude-haiku-4-5"],
     "help": "Model id the endpoint expects (the bundled sidecar serves "
             "\"extractor\"; LM Studio/Ollama use their loaded-model names). "
             "Against the Claude CLI shim (:8082) a claude-* name switches "
             "the served model per request — pick the dreamer here without "
             "restarting anything. Effective only when settings source = "
             "config."},
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
