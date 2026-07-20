"""LongMemEval-V2 GPU smoke — `procedure` category, per-trajectory dreams.

Runs the pilot decisions pre-registered in ``lme_v2_adapter.py``'s READY-TO-RUN
block and ``data/lme_v2/FORMAT-NOTES.md`` end to end, mirroring
``longmemeval_bench.py``'s ingest -> dream -> answer -> judge shape but over
LME-V2 *trajectories* instead of chat sessions:

  Category   : ``procedure`` (workflow-knowledge, 74 questions, ALL text-only).
               NOT ``errors-gotchas`` — those are multimodal (FORMAT-NOTES.md).
  Observations: ON, DISTILLED (Fix A). The adapter no longer dumps raw trees
               (that starved the corpus of gold labels AND was 47x the baseline);
               with ``include_observations=True`` each state contributes a
               resolved action label (``clicked: link "Reports"`` — the opaque
               bid mapped to its node) plus a capped page context (title +
               headers). Gold module names go from 0 occurrences to present.
  Extraction : trajectory-mode prompt (Fix B) — extracts the ORDERED WORKFLOW and
               environment affordances, not durable user facts. The shipped
               ``_SYSTEM_PROMPT`` and all product paths stay byte-identical.
  Synthesis  : a cross-trajectory pass (Fix C) clusters same-task procedure
               claims into a canonical 'typical workflow' fact, weighting
               ``outcome=success`` trajectories over ``failure`` on conflict.
  Store policy: every adapted turn stored (store-everything v1; voluntary-capture
               simulation is a future arm).
  Dream cadence: one dream per TRAJECTORY (the natural unit — one goal — and it
               mirrors ingest_and_dream's per-session boundary).
  Scope      : ``--limit N`` questions (default 1), each ingesting only the
               trajectories its haystack references, capped at
               ``--max-trajectories`` (default 20 of 100 — a SMOKE, not the
               bench; the cap is logged loudly per the no-silent-caps rule).
  Arms       : rag / cortex / hybrid, contexts built *exactly* as
               ``longmemeval_bench.build_contexts`` builds them, with the same
               answerer/judge prompts. Per-arm wall-clock query latency is
               recorded (LME-V2 scores latency as well as accuracy).
  Scoring    : LME-V2's own deterministic ``eval_function`` per question is the
               primary metric (``{arm}_correct``); the LLM judge is kept as a
               secondary signal (``{arm}_judge``). Only the three spec heads that
               appear in ``procedure`` are implemented — an unimplemented head in
               the selected slice fails loudly at load, never silently skipped.

Isolation: the dedicated ``pseudolife_memory_bench`` DB (``build_service`` ->
``reset_bench``), a fresh temp data-dir per question — the live bank is never
touched. Results append to a resumable JSONL (kill + rerun to continue) with a
summary written by ``--report``.

Data: the adapter's ``data/lme_v2/`` files. This module may run from a git
worktree that shares no data with the main checkout, so the data dir is resolved
via ``LME_V2_DATA_DIR`` (env) -> local ``evals/data/lme_v2`` -> the main
checkout derived from ``git --git-common-dir`` (see ``resolve_data_dir``).

Usage (repo root):

  # offline format/plan sanity check — no endpoints, no GPU:
  PYTHONPATH=. python evals/lme_v2_smoke.py --dry-run

  # the real smoke (needs qwen-27b at :1234 for extract + answer + judge):
  PYTHONPATH=. python evals/lme_v2_smoke.py --limit 1 --max-trajectories 20
  PYTHONPATH=. python evals/lme_v2_smoke.py --report
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")               # embedder on CPU
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import lme_v2_adapter as A  # noqa: E402 — light module, no heavy imports

RESULTS_DIR = Path(__file__).resolve().parent / "results"
OUT_FILE = RESULTS_DIR / "lme-v2-smoke.jsonl"
SUMMARY_FILE = RESULTS_DIR / "lme-v2-smoke.summary.json"

CATEGORY = "procedure"          # workflow-knowledge; adapter alias resolves it
DEFAULT_MAX_TRAJECTORIES = 20   # of the 100 in a small-tier haystack — SMOKE cap
ARMS = ("rag", "cortex", "hybrid")

# qwen-27b serves extractor AND answerer/judge for the smoke (one endpoint).
EXTRACTOR_URL = os.environ.get("LME_V2_EXTRACTOR_URL", "http://127.0.0.1:1234/v1")


# --------------------------------------------------------------------------- #
# Fix B — trajectory-mode extraction prompt (EVALS-ONLY).
#
# The shipped ``_SYSTEM_PROMPT`` asks for durable, current-state USER facts and
# skips narrative/transient state — exactly what a web-agent trajectory is, so
# it yields ~nothing (5 claims / 458 turns in the overnight smoke). This variant
# targets trajectory content: it extracts the ORDERED WORKFLOW (the class that
# answers `procedure` questions) and environment affordances, and drops the
# click-by-click narrative. It is passed to ``OpenAICompatExtractor`` via its
# optional ``system_prompt`` arg; the shipped daemon prompt and every product
# code path are untouched (the regression gate pins those).
_TRAJECTORY_SYSTEM_PROMPT = (
    "You distil a web/enterprise AGENT TRAJECTORY into durable, reusable "
    "knowledge. The numbered notes are the ordered steps of ONE agent task: the "
    "goal and outcome (step 1), then per step the page it was on, its thought, "
    "the raw action, and — where resolvable — the human-readable label it "
    "clicked and the page title/headers it saw. Output JSON only: "
    '{"claims":[{"entity":..,"attribute":..,"value":..,"confidence":0..1,'
    '"source":<number of the step the claim came from>}]}.\n'
    "Extract exactly two kinds of claim and nothing else:\n"
    "1. PROCEDURE (workflow) claim — for the task the trajectory pursues, the "
    "ordered MODULES/PAGES the workflow moves through. entity = the task type, "
    "a short stable phrase (e.g. 'reassign incidents by assignment group'); "
    "attribute = 'modules used (in order)'; value = the ordered, human-readable "
    "module/page names separated by '; ', read from the resolved click labels "
    "and page titles (NEVER the raw bids). Emit ONE ordered claim for the task, "
    "not one per click.\n"
    "2. ENVIRONMENT/AFFORDANCE claim — for a module or page the agent used, what "
    "it is FOR or what it reaches. entity = the module/page name; attribute = "
    "one of 'purpose' | 'contains' | 'reached via'; value = the concise fact.\n"
    "Read the goal plus the ordered visited/clicked labels and emit the "
    "WORKFLOW, not the narrative. DROP click-by-click detail: individual clicks, "
    "typing, focus changes, transient UI state, and one-off values are not "
    "durable. Reuse one consistent entity+attribute across steps about the SAME "
    "task or module.\n"
    "Worked example — collapsing a navigation sequence into ONE ordered-modules "
    "claim. Steps:\n"
    "  [1] [task] outcome=success  file a new expense report for a business trip\n"
    '  [2] page: Home | Concur; clicked: link "Expenses"\n'
    '  [3] page: Expenses | Concur; clicked: button "New Report"\n'
    '  [4] page: Create Report | Concur; clicked: menuitem "Add Expense"\n'
    "  [5] page: Add Expense | Concur\n"
    "Correct output:\n"
    '  {"claims":['
    '{"entity":"file a new expense report","attribute":"modules used (in order)",'
    '"value":"Expenses; New Report; Add Expense","confidence":0.8,"source":2},'
    '{"entity":"Expenses","attribute":"purpose","value":"lists and creates '
    'expense reports","confidence":0.7,"source":3}]}\n'
    "Five click-steps became ONE ordered-modules procedure claim plus one "
    "affordance claim — no per-click claims. Return {\"claims\":[]} if the "
    "trajectory shows no reusable procedure or environment knowledge."
)

# Attribute keywords that mark a claim as an ordered-workflow PROCEDURE claim
# (Fix C clusters only these; environment/affordance claims pass through).
_PROCEDURE_ATTR_KEYS = ("module", "workflow", "order", "step", "procedure")


def _norm_value(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").strip()).lower()


def synthesize_procedures(claims: list[dict]) -> list[dict]:
    """Fix C — cross-trajectory synthesis over accumulated PROCEDURE claims.

    Clusters procedure claims by (task entity, attribute), then emits one
    canonical 'typical workflow' claim per cluster. When trajectories disagree on
    the ordering, ``outcome='success'`` trajectories outweigh ``'failure'`` ones;
    with no success signal it falls back to the plain majority ordering. Pure and
    GPU-free — structurally unit-tested; full validation is the GPU smoke's job.

    Input claims are dicts with ``entity``/``attribute``/``value``/``outcome``.
    Returns canonical dicts with ``entity``/``attribute``/``value``/``support``
    (success trajectories backing the winner, or total when none succeeded) and
    ``conflicts`` (claims whose ordering differed from the winner).
    """
    clusters: dict[tuple[str, str], list[dict]] = {}
    for c in claims or []:
        attr = str(c.get("attribute", ""))
        if not any(k in attr.lower() for k in _PROCEDURE_ATTR_KEYS):
            continue
        key = (str(c.get("entity", "")).strip().lower(), attr.strip().lower())
        clusters.setdefault(key, []).append(c)

    out: list[dict] = []
    for members in clusters.values():
        # Tally success and total support per distinct (normalised) value.
        success: dict[str, int] = {}
        total: dict[str, int] = {}
        display: dict[str, str] = {}      # normalised -> first-seen spelling
        for c in members:
            nv = _norm_value(c.get("value", ""))
            if not nv:
                continue
            display.setdefault(nv, str(c.get("value", "")).strip())
            total[nv] = total.get(nv, 0) + 1
            if str(c.get("outcome", "")).strip().lower() == "success":
                success[nv] = success.get(nv, 0) + 1
        if not total:
            continue
        # Success majority wins; ties/absence fall back to overall majority.
        if success:
            winner = max(success, key=lambda v: (success[v], total.get(v, 0)))
            support = success[winner]
        else:
            winner = max(total, key=lambda v: total[v])
            support = total[winner]
        conflicts = sum(n for v, n in total.items() if v != winner)
        sample = members[0]
        out.append({
            "entity": str(sample.get("entity", "")).strip(),
            "attribute": f"typical workflow ({str(sample.get('attribute', '')).strip()})",
            "value": display[winner],
            "support": support,
            "conflicts": conflicts,
        })
    return out


class _RecordingExtractor:
    """Wraps an ``OpenAICompatExtractor`` to capture every claim it emits, tagged
    with the CURRENT trajectory's outcome — the per-trajectory attribution Fix C's
    synthesis needs (the cortex loses outcome once claims consolidate). Delegates
    extraction verbatim; adds no behaviour of its own. Precedent:
    ``window_echo_check._Recording``."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.current_outcome: str | None = None
        self.records: list[dict] = []

    def extract(self, texts, vocab, known_facts=None):
        claims = self.inner.extract(texts, vocab, known_facts)
        for c in claims:
            self.records.append({
                "entity": c["entity"], "attribute": c["attribute"],
                "value": c["value"], "outcome": self.current_outcome,
            })
        return claims


# --------------------------------------------------------------------------- #
# Data-dir resolution (worktree-safe; never hardcodes a home path)
# --------------------------------------------------------------------------- #
def resolve_data_dir() -> Path:
    """Locate ``data/lme_v2``. Order: ``LME_V2_DATA_DIR`` env -> local tree ->
    the main checkout derived from git's common dir (this file may live in a
    worktree that shares no working tree with the data)."""
    env = os.environ.get("LME_V2_DATA_DIR")
    if env:
        return Path(env)
    local = Path(__file__).resolve().parent / "data" / "lme_v2"
    if local.exists():
        return local
    try:
        import subprocess
        common = subprocess.check_output(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(Path(__file__).resolve().parent),
            text=True, stderr=subprocess.DEVNULL).strip()
        cand = Path(common).parent / "evals" / "data" / "lme_v2"
        if cand.exists():
            return cand
    except Exception:  # noqa: BLE001 — resolution is best-effort; fall through
        pass
    return local  # surfaces a clear missing-file error downstream


def _bind_adapter_paths(data_dir: Path) -> None:
    """Point the adapter's module-level file constants at ``data_dir`` so its
    loaders (load_questions / load_small_haystack) read the resolved dir."""
    A.DATA_DIR = data_dir
    A.QUESTIONS_FILE = data_dir / "questions.jsonl"
    A.SMALL_HAYSTACK_FILE = data_dir / "haystacks" / "lme_v2_small.json"
    A.TRAJECTORIES_SMALL_FILE = data_dir / "trajectories_small.jsonl"


def load_trajectories_by_ids(wanted: list[str]) -> dict[str, dict]:
    """One streaming pass over ``trajectories_small.jsonl`` collecting the wanted
    ids (early-exit once all are found). Cheaper than the adapter's per-id scan,
    which re-reads the 171 MB file once per trajectory."""
    want = set(wanted)
    out: dict[str, dict] = {}
    path = A.TRAJECTORIES_SMALL_FILE
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("id") in want:
                out[obj["id"]] = obj
                if len(out) == len(want):
                    break
    return out


# --------------------------------------------------------------------------- #
# LME-V2 eval_function scorers (only the heads that appear in `procedure`)
# --------------------------------------------------------------------------- #
# procedure slice (verified against questions.jsonl, 2026-07-19):
#   mc_choice_match               (35) — single boxed choice letter
#   norm_phrase_set_match         (24) — unordered normalized phrase set
#   norm_phrase_set_match_ordered (15) — ordered normalized phrase list
IMPLEMENTED_SPECS = {
    "mc_choice_match",
    "norm_phrase_set_match",
    "norm_phrase_set_match_ordered",
}

# Normalisation note (documented approximation of the official flags — the
# reference scorer is not vendored here): normalize_hyphen unifies unicode dash
# glyphs to ascii '-'; strip_punct removes punctuation but PRESERVES the hyphen
# (so normalize_hyphen keeps its purpose) and whitespace; lower lowercases.
_DASHES = "‐‑‒–—―−⁃­"
_BOXED_RE = re.compile(r"\\boxed\{([^{}]*)\}")


def parse_eval_function(spec: str) -> tuple[str, dict[str, str]]:
    parts = [p.strip() for p in (spec or "").split("|")]
    head = parts[0]
    flags: dict[str, str] = {}
    for p in parts[1:]:
        if not p:
            continue
        if "=" in p:
            k, v = p.split("=", 1)
            flags[k.strip()] = v.strip()
        else:
            flags[p] = "true"
    return head, flags


def _extract_boxed(response: str) -> str | None:
    hits = _BOXED_RE.findall(response or "")
    return hits[-1].strip() if hits else None


def _normalize_phrase(s: str, flags: dict[str, str]) -> str:
    s = (s or "").strip()
    if flags.get("lower") == "true":
        s = s.lower()
    if flags.get("normalize_hyphen") == "true":
        for d in _DASHES:
            s = s.replace(d, "-")
    if flags.get("strip_punct") == "true":
        s = re.sub(r"[^\w\s-]", " ", s)   # keep word chars, whitespace, hyphen
    return re.sub(r"\s+", " ", s).strip()


def _split_phrases(text: str, separators: str) -> list[str]:
    seps = separators or ","
    pattern = "[" + re.escape(seps) + "]"
    return re.split(pattern, text or "")


def score_mc(response: str, answer, flags: dict[str, str]) -> bool:
    boxed = _extract_boxed(response)
    pred = None
    if boxed:
        m = re.search(r"[A-Za-z]", boxed)
        pred = m.group(0).upper() if m else None
    if pred is None:  # no box — fall back to a standalone A-H letter
        m = re.search(r"\b([A-Ha-h])\b", response or "")
        pred = m.group(1).upper() if m else None
    if flags.get("require_non_empty") == "true" and not pred:
        return False
    return pred is not None and pred == str(answer).strip().upper()


def score_phrase_set(response: str, answer, flags: dict[str, str],
                     *, ordered: bool) -> bool:
    boxed = _extract_boxed(response)
    pred_raw = boxed if boxed is not None else (response or "")
    seps = flags.get("separators", ",")
    pred = [p for p in (_normalize_phrase(x, flags)
                        for x in _split_phrases(pred_raw, seps)) if p]
    gold = [p for p in (_normalize_phrase(x, flags)
                        for x in _split_phrases(str(answer), seps)) if p]
    if flags.get("require_non_empty") == "true" and not pred:
        return False
    return pred == gold if ordered else set(pred) == set(gold)


def score_answer(spec: str, response: str, answer) -> bool:
    head, flags = parse_eval_function(spec)
    if head == "mc_choice_match":
        return score_mc(response, answer, flags)
    if head == "norm_phrase_set_match":
        return score_phrase_set(response, answer, flags, ordered=False)
    if head == "norm_phrase_set_match_ordered":
        return score_phrase_set(response, answer, flags, ordered=True)
    raise ValueError(f"unimplemented eval_function head: {head!r}")


def assert_specs_implemented(questions: list[dict]) -> None:
    """Fail LOUDLY (not silently skip) if any selected question uses a spec head
    this harness cannot score."""
    unimpl: dict[str, int] = {}
    for q in questions:
        head = parse_eval_function(q.get("eval_function", ""))[0]
        if head not in IMPLEMENTED_SPECS:
            unimpl[head] = unimpl.get(head, 0) + 1
    if unimpl:
        raise SystemExit(
            f"unimplemented eval_function spec head(s) in the selected "
            f"questions: {unimpl}. Implement them or narrow the slice; "
            f"implemented = {sorted(IMPLEMENTED_SPECS)}.")


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def select_trajectory_ids(q: dict, haystack: dict[str, list[str]],
                          max_traj: int) -> tuple[list[str], int]:
    """This question's haystack trajectory ids, capped at ``max_traj``. Returns
    (selected_ids, full_count)."""
    ids = haystack.get(q["id"], [])
    return ids[:max_traj], len(ids)


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


# --------------------------------------------------------------------------- #
# Ingest (per-trajectory dream boundary) — mirrors ingest_and_dream's dream drain
# --------------------------------------------------------------------------- #
def ingest_and_dream(svc, extractor, trajectories: list[dict], ex_url,
                     probe) -> dict:
    """Store every adapted turn of each trajectory (observations ON — resolved
    action labels + capped page context, Fix A), dreaming to consolidation after
    EACH trajectory — one trajectory ~= one session. When ``extractor`` is a
    ``_RecordingExtractor`` its ``current_outcome`` is set per trajectory so
    Fix C's synthesis can weight success over failure."""
    tally = {"trajectories": 0, "turns": 0, "claims": 0, "inserted": 0,
             "superseded": 0, "extract_seconds": 0.0}
    held = 0
    for traj in trajectories:
        if hasattr(extractor, "current_outcome"):
            extractor.current_outcome = traj.get("outcome")
        for turn in A.trajectory_to_turns(traj, include_observations=True):
            svc.store(turn, source="bench")
            tally["turns"] += 1
        tally["trajectories"] += 1
        t0 = time.perf_counter()
        while True:
            res = svc.dream_run(extractor, limit=100)
            for k in ("claims", "inserted", "superseded"):
                tally[k] += int(res.get(k, 0))
            if res.get("extractor_failed"):
                held += 1
                if held >= 8 or not probe(ex_url):
                    raise RuntimeError(
                        "extractor endpoint failing — aborting "
                        "(restart the model server and rerun)")
                continue
            held = 0
            if not res.get("pulled"):
                break
        tally["extract_seconds"] += time.perf_counter() - t0
    tally["extract_seconds"] = round(tally["extract_seconds"], 1)
    return tally


# --------------------------------------------------------------------------- #
# Answer + judge + deterministic score (per arm, with query latency)
# --------------------------------------------------------------------------- #
# Composition-aware answer prompt (2026-07-20). The sonnet-full run showed the
# corpus carries the ANSWER COMPONENTS (a sibling Reports->Problems workflow +
# the "Assigned to can be changed to reassign" affordance fact) but the KU
# answer prompt makes the model parrot the nearest single stored workflow.
# Procedure questions need composition + the benchmark's module-list format.
_V2_ANSWER_SYSTEM = """\
You answer questions about how to perform tasks in a software environment,
using ONLY the memory context provided.

The context holds procedural memory from past task executions: 'typical
workflow (modules used (in order))' facts, module-purpose facts, and raw
navigation snippets. The question may describe a task that NO stored
workflow matches verbatim. In that case, COMPOSE the procedure from
components: first work out what information the task needs and which module
provides it (see the module-purpose facts), then which module carries out
the action. A workflow for a related task can lend its structure.

Answer format: output ONLY the module names, in order of use, separated by
'; '. Use each module's short canonical navigator name (e.g. 'Reports',
'Problems', 'Incidents') — not page or record variants ('Problems list',
'Problem record' -> 'Problems'). No explanations, no extra words. If the
context contains nothing relevant to the task, answer exactly: I don't know."""


def answer_judge_score(row: dict, answer_system: str | None = None) -> dict:
    """Fill answer/score/judge/latency fields from the row's persisted contexts.

    Default ``answer_system`` is longmemeval_bench's ``_ANSWER_SYSTEM``
    verbatim (LME-V1 comparability); pass ``_V2_ANSWER_SYSTEM`` for the
    composition-aware variant. The primary metric is LME-V2's deterministic
    eval_function; the LLM judge is a secondary signal."""
    from longmemeval_bench import (_chat, _ANSWER_SYSTEM, _JUDGE_SYSTEM)
    from ladder_sweep import approx_tokens
    system = answer_system if answer_system is not None else _ANSWER_SYSTEM
    for arm in ARMS:
        ctx = row["contexts"].get(arm, "")
        prompt = (f"Question: {row['question']}\n\n"
                  f"Memory context:\n{ctx or '(empty)'}")
        t0 = time.perf_counter()
        response = _chat(system, prompt)
        row[f"{arm}_answer_seconds"] = round(time.perf_counter() - t0, 2)
        row[f"{arm}_response"] = response
        # Primary: LME-V2's own deterministic scorer over the eval_function spec.
        row[f"{arm}_correct"] = score_answer(row["eval_function"], response,
                                             row["answer"])
        # Secondary: the LLM judge (same prompt as the LME-V1 bench).
        verdict = _chat(_JUDGE_SYSTEM, (
            f"Question: {row['question']}\n"
            f"Correct answer: {row['answer']}\n"
            f"Model response: {response}"), max_tokens=8)
        row[f"{arm}_judge"] = verdict.strip().lower().startswith("yes")
        row[f"{arm}_context_tokens"] = approx_tokens(ctx)
    return row


# --------------------------------------------------------------------------- #
# Retrieval variants (2026-07-20 experiment)
# --------------------------------------------------------------------------- #
_WORD_RE = re.compile(r"[a-z0-9]{4,}")


def _content_tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower().replace("-", " ")))


def _fact_text(f: dict) -> str:
    return f"{f.get('entity', '')} {f.get('attribute', '')} {f.get('value', '')}"


def build_contexts_v2(svc, question: str,
                      retrv: dict) -> tuple[dict[str, str], list[dict]]:
    """``longmemeval_bench.build_contexts`` with the retrieval-experiment
    knobs, plus a full-pool cortex dump (top 40 at floor 0) so "claim never
    extracted" vs "extracted but ranked out" is distinguishable post-hoc.
    Returns (contexts, cortex_dump)."""
    from longmemeval_bench import (CORTEX_MIN_SCORE, CORTEX_TOP_K,
                                   HYBRID_TOP_K, RAG_TOP_K)

    raw = svc.search(question, top_k=RAG_TOP_K,
                     bm25=(True if retrv.get("bm25") else None),
                     rerank=(True if retrv.get("rerank") else None),
                     ).get("entries", [])
    raw_texts = [e.get("text", "") for e in raw]

    cortex = svc.cortex_search(question, top_k=CORTEX_TOP_K,
                               min_score=CORTEX_MIN_SCORE).get("entries", [])
    # Instrumentation: the whole ranked fact pool, floor 0.
    pool = svc.cortex_search(question, top_k=400, min_score=0.0
                             ).get("entries", [])
    dump = [{"entity": f.get("entity"), "attribute": f.get("attribute"),
             "value": f.get("value"), "score": round(f.get("score", 0.0), 4)}
            for f in pool[:40]]

    if retrv.get("lexical_cortex"):
        # Token-overlap rescue: cosine misses synonym-phrased procedure keys
        # ("rebalance workload" vs "redistribute ... workload balancing").
        q_toks = _content_tokens(question)
        seen = {(f.get("entity"), f.get("attribute")) for f in cortex}
        lex = []
        for f in pool:
            if (f.get("entity"), f.get("attribute")) in seen:
                continue
            overlap = len(q_toks & _content_tokens(_fact_text(f)))
            if overlap >= 2:
                lex.append((overlap, f))
        lex.sort(key=lambda p: -p[0])
        cortex = cortex + [f for _, f in lex[:8]]

    fact_lines = []
    for f in cortex:
        line = (f"{f.get('entity', '')} — {f.get('attribute', '')}: "
                f"{f.get('value', '')}")
        try:
            versions = svc.history(f.get("entity", ""),
                                   f.get("attribute", "")).get("versions", [])
            older = [v.get("value", "") for v in versions[:-1]
                     if v.get("value") and v.get("value") != f.get("value")]
            if older:
                line += "  (earlier values, oldest first: " + " -> ".join(older) + ")"
        except Exception:  # noqa: BLE001 — history is garnish, never fatal
            pass
        fact_lines.append(line)
    contexts = {
        "rag": "\n\n".join(raw_texts),
        "cortex": "\n".join(fact_lines),
        "hybrid": ("Known facts:\n" + "\n".join(fact_lines) +
                   "\n\nRelevant memories:\n" +
                   "\n\n".join(raw_texts[:HYBRID_TOP_K])),
    }
    return contexts, dump


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def run_smoke(limit: int, max_traj: int, retrv: dict | None = None,
              answer_system: str | None = None) -> None:
    from ladder_sweep import build_service, probe
    from pseudolife_memory.memory.dream import OpenAICompatExtractor
    from longmemeval_bench import build_contexts

    if not probe(EXTRACTOR_URL):
        sys.exit(f"no qwen-27b endpoint at {EXTRACTOR_URL} — start it first "
                 "(serves extractor + answerer + judge for the smoke)")

    questions = A.load_questions(CATEGORY)
    if not questions:
        sys.exit(f"no questions for category={CATEGORY!r}")
    questions = questions[:limit]
    assert_specs_implemented(questions)
    haystack = A.load_small_haystack()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    done = {r["question_id"] for r in load_rows(OUT_FILE)}
    print(f"LME-V2 smoke: {len(questions)} `{CATEGORY}` question(s), "
          f"max-trajectories={max_traj}, extractor+answerer+judge={EXTRACTOR_URL} "
          f"({len(done)} already done, resuming)", flush=True)

    for i, q in enumerate(questions):
        if q["id"] in done:
            continue
        traj_ids, full = select_trajectory_ids(q, haystack, max_traj)
        if full > len(traj_ids):
            print(f"  !! CAP: haystack for {q['id']} has {full} trajectories; "
                  f"--max-trajectories={max_traj} -> ingesting {len(traj_ids)} "
                  f"({full - len(traj_ids)} dropped). SMOKE, not the bench.",
                  flush=True)
        trajs_by_id = load_trajectories_by_ids(traj_ids)
        trajectories = [trajs_by_id[t] for t in traj_ids if t in trajs_by_id]
        missing = len(traj_ids) - len(trajectories)
        if missing:
            print(f"  !! {missing} of {len(traj_ids)} selected trajectories "
                  f"not on disk (partial download?) — ingesting "
                  f"{len(trajectories)}.", flush=True)

        t_start = time.perf_counter()
        tmp = Path(tempfile.mkdtemp(prefix="lme2_"))
        svc = build_service(tmp)                       # dedicated bench DB
        svc.config.memory.dream.extract_relations = False   # facts only
        # Fix B: trajectory-mode extraction prompt (product prompt untouched).
        base_extractor = OpenAICompatExtractor(
            EXTRACTOR_URL, "bench", max_tokens=4096, timeout_seconds=600.0,
            system_prompt=_TRAJECTORY_SYSTEM_PROMPT)
        # Fix C: record per-trajectory claims (with outcome) for synthesis.
        extractor = _RecordingExtractor(base_extractor)
        tally = ingest_and_dream(svc, extractor, trajectories, EXTRACTOR_URL,
                                 probe)

        # Fix C: one cross-trajectory synthesis pass over the accumulated
        # procedure claims -> canonical 'typical workflow' facts, written back so
        # the cortex/hybrid arms can retrieve them.
        canon = synthesize_procedures(extractor.records)
        for c in canon:
            # support=='action' corroboration: success-backed workflow knowledge.
            svc.cortex_write(c["entity"], c["attribute"], c["value"],
                             support="action")
        tally["procedure_claims"] = len(
            [r for r in extractor.records
             if any(k in r["attribute"].lower() for k in _PROCEDURE_ATTR_KEYS)])
        tally["canonical_workflows"] = len(canon)

        t_retr = time.perf_counter()
        if retrv and any(retrv.values()):
            contexts, cortex_dump = build_contexts_v2(svc, q["question"], retrv)
        else:
            contexts = build_contexts(svc, q["question"])
            _, cortex_dump = build_contexts_v2(svc, q["question"], {})
        retrieval_seconds = round(time.perf_counter() - t_retr, 2)
        svc.flush()

        row = {
            "question_id": q["id"],
            "question": q["question"],
            "answer": q["answer"],
            "question_type": q["question_type"],
            "eval_function": q["eval_function"],
            "domain": q.get("domain"),
            "environment": q.get("environment"),
            "trajectories_ingested": len(trajectories),
            "trajectories_available": full,
            "max_trajectories": max_traj,
            "contexts": contexts,          # persisted — re-scorable without GPU
            "retrieval_config": retrv or {},
            "cortex_dump": cortex_dump,    # full-pool top-40 with scores
            "consolidation": tally,
            "synthesized_workflows": canon,   # Fix C canonical claims (audit)
            "retrieval_seconds": retrieval_seconds,
            "wall_seconds": round(time.perf_counter() - t_start, 1),
        }
        row = answer_judge_score(row, answer_system=answer_system)
        marks = " ".join(f"{a}={'Y' if row[f'{a}_correct'] else 'n'}"
                         for a in ARMS)
        with OUT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{i + 1}/{len(questions)}] {q['id']}  {marks}  "
              f"({row['wall_seconds']}s, {tally['turns']} turns, "
              f"{tally['superseded']} superseded, retr {retrieval_seconds}s)",
              flush=True)


def reanswer(from_tag: str, answer_system: str | None) -> None:
    """Re-run ONLY the answer/judge/score phase over an earlier run's
    persisted contexts (no ingest, no dreams, no GPU) — the controlled way
    to iterate the answer prompt: same contexts, same answerer endpoint,
    one variable."""
    src = RESULTS_DIR / f"lme-v2-smoke-{from_tag}.jsonl"
    rows = load_rows(src)
    if not rows:
        sys.exit(f"no rows in {src}")
    from ladder_sweep import probe
    if not probe(EXTRACTOR_URL):
        sys.exit(f"no answerer endpoint at {EXTRACTOR_URL}")
    print(f"re-answering {len(rows)} row(s) from {src.name} "
          f"-> {OUT_FILE.name}", flush=True)
    with OUT_FILE.open("w", encoding="utf-8") as fh:
        for row in rows:
            row = {k: v for k, v in row.items()
                   if not any(k.startswith(a + "_") for a in ARMS)}
            row["reanswered_from"] = from_tag
            row = answer_judge_score(row, answer_system=answer_system)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            marks = " ".join(f"{a}={'Y' if row[f'{a}_correct'] else 'n'}"
                             for a in ARMS)
            print(f"  {row['question_id']}  {marks}", flush=True)


def report() -> None:
    rows = [r for r in load_rows(OUT_FILE) if "rag_correct" in r]
    if not rows:
        sys.exit(f"no judged results in {OUT_FILE}")
    n = len(rows)
    print(f"\nLongMemEval-V2 smoke — `{CATEGORY}` ({n} question(s))")
    print(f"{'arm':<10}{'eval_acc':>10}{'judge_acc':>11}"
          f"{'ctx tok/q':>12}{'answer s/q':>12}")
    summary = {"category": CATEGORY, "n": n,
               "retrieval_seconds_mean": round(
                   sum(r.get("retrieval_seconds", 0.0) for r in rows) / n, 2),
               "arms": {}}
    for arm in ARMS:
        acc = sum(r[f"{arm}_correct"] for r in rows) / n
        jacc = sum(r.get(f"{arm}_judge", False) for r in rows) / n
        tok = sum(r[f"{arm}_context_tokens"] for r in rows) / n
        lat = sum(r.get(f"{arm}_answer_seconds", 0.0) for r in rows) / n
        summary["arms"][arm] = {"eval_accuracy": round(acc, 3),
                                "judge_accuracy": round(jacc, 3),
                                "context_tokens": round(tok, 1),
                                "answer_seconds": round(lat, 2)}
        print(f"{arm:<10}{acc:>10.3f}{jacc:>11.3f}{tok:>12.1f}{lat:>12.2f}")
    sup = sum(r["consolidation"]["superseded"] for r in rows)
    print(f"supersessions across runs: {sup}   "
          f"retrieval s/q: {summary['retrieval_seconds_mean']}")
    summary["superseded_total"] = sup
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"summary -> {SUMMARY_FILE}")


def dry_run(limit: int, max_traj: int) -> int:
    """Offline: load ``limit`` question(s) + their capped trajectory lists, print
    the plan, exit 0. No endpoints, no GPU, no heavy imports."""
    print(f"data dir: {A.DATA_DIR}")
    if not A.QUESTIONS_FILE.exists():
        print(f"  !! questions.jsonl not found at {A.QUESTIONS_FILE} — set "
              f"LME_V2_DATA_DIR", file=sys.stderr)
        return 1
    questions = A.load_questions(CATEGORY)
    if not questions:
        print(f"no `{CATEGORY}` questions", file=sys.stderr)
        return 1
    questions = questions[:limit]
    assert_specs_implemented(questions)          # loud fail if a head is new
    heads = sorted({parse_eval_function(q["eval_function"])[0]
                    for q in questions})
    haystack = A.load_small_haystack()

    print("=" * 72)
    print(f"PLAN — LME-V2 smoke (DRY RUN, offline)")
    print(f"  category            : {CATEGORY} (workflow-knowledge, text-only)")
    print(f"  observations        : ON (Fix A: resolved action labels + capped "
          f"page context; NOT raw trees)")
    print(f"  extraction prompt   : trajectory-mode (Fix B; product prompt "
          f"untouched)")
    print(f"  synthesis           : cross-trajectory workflow pass (Fix C)")
    print(f"  store policy        : every adapted turn")
    print(f"  dream cadence        : one dream per trajectory")
    print(f"  questions selected  : {len(questions)} (--limit {limit})")
    print(f"  max-trajectories cap: {max_traj} of 100 per haystack")
    print(f"  eval_function heads : {heads} (all implemented)")
    print(f"  arms                : {list(ARMS)}")
    print(f"  extractor+ans+judge : {EXTRACTOR_URL} (NOT contacted in dry-run)")
    print(f"  output              : {OUT_FILE}")
    print("=" * 72)

    for q in questions:
        traj_ids, full = select_trajectory_ids(q, haystack, max_traj)
        dropped = full - len(traj_ids)
        print(f"\nQUESTION {q['id']}  type={q['question_type']}")
        print(f"  Q: {q['question'][:200]}"
              f"{'...' if len(q['question']) > 200 else ''}")
        print(f"  A: {q['answer']!r}")
        print(f"  eval_function: {q['eval_function']}")
        print(f"  haystack: {full} trajectories -> ingest {len(traj_ids)} "
              f"(cap {max_traj}); {dropped} dropped by the SMOKE cap")
        # cheap on-disk existence check (one pass, early-exit) — proves the data
        # dir resolved and the small tier is present.
        found = load_trajectories_by_ids(traj_ids)
        print(f"  on disk: {len(found)}/{len(traj_ids)} selected trajectories "
              f"present in {A.TRAJECTORIES_SMALL_FILE.name}")
        n_turns = sum(len(A.trajectory_to_turns(found[t],
                                                include_observations=True))
                      for t in traj_ids if t in found)
        print(f"  adapted turns (observations distilled): {n_turns} across "
              f"{len(found)} trajectories")
    print("\ndry-run OK — no endpoints contacted.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=1,
                    help="run only the first N `procedure` questions (default 1)")
    ap.add_argument("--max-trajectories", type=int,
                    default=DEFAULT_MAX_TRAJECTORIES,
                    help=f"cap trajectories ingested per question "
                         f"(default {DEFAULT_MAX_TRAJECTORIES} of 100 — SMOKE)")
    ap.add_argument("--dry-run", action="store_true",
                    help="offline plan check: load a question + its capped "
                         "trajectory list, print the plan, exit 0")
    ap.add_argument("--report", action="store_true",
                    help="summarise existing results instead of running")
    # Retrieval experiment flags (2026-07-20): the first smoke localized the
    # remaining failure to retrieval relevance — question phrasing vs evidence
    # phrasing ("rebalance workload" vs "redistribute problems"). These enable
    # the shipped-but-default-off lexical/rerank channels for entries, plus an
    # experiment-local lexical union over cortex facts (cortex_search has no
    # BM25 channel of its own).
    ap.add_argument("--bm25", action="store_true",
                    help="enable the BM25 hybrid channel on raw-turn retrieval")
    ap.add_argument("--rerank", action="store_true",
                    help="enable the cross-encoder reranker on raw-turn retrieval")
    ap.add_argument("--lexical-cortex", action="store_true",
                    help="union a token-overlap lexical channel into the "
                         "cortex-fact selection (experiment-local)")
    ap.add_argument("--out-tag", default="",
                    help="suffix for the result files so runs don't overwrite "
                         "each other (e.g. 'retrv1')")
    ap.add_argument("--answer-prompt", choices=("ku", "compose"), default="ku",
                    help="answer system prompt: 'ku' = longmemeval_bench "
                         "verbatim (default, LME-V1 comparable); 'compose' = "
                         "the composition-aware module-list prompt")
    ap.add_argument("--reanswer-from", default="",
                    help="re-run ONLY answer/judge over the persisted contexts "
                         "of an earlier run's tag (no ingest, no dreams)")
    args = ap.parse_args()

    if args.out_tag:
        global OUT_FILE, SUMMARY_FILE
        OUT_FILE = RESULTS_DIR / f"lme-v2-smoke-{args.out_tag}.jsonl"
        SUMMARY_FILE = RESULTS_DIR / f"lme-v2-smoke-{args.out_tag}.summary.json"

    _bind_adapter_paths(resolve_data_dir())

    if args.dry_run:
        return dry_run(args.limit, args.max_trajectories)
    if args.report:
        report()
        return 0
    answer_system = _V2_ANSWER_SYSTEM if args.answer_prompt == "compose" else None
    if args.reanswer_from:
        if not args.out_tag:
            sys.exit("--reanswer-from requires --out-tag (don't overwrite the source)")
        reanswer(args.reanswer_from, answer_system)
        report()
        return 0
    retrv = {"bm25": args.bm25, "rerank": args.rerank,
             "lexical_cortex": args.lexical_cortex}
    run_smoke(args.limit, args.max_trajectories, retrv, answer_system)
    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
