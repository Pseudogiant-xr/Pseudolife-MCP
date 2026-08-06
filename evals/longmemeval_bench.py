"""LongMemEval knowledge-update bench — the supersession subset, end to end.

Runs the LongMemEval (arXiv 2410.10813) *knowledge-update* questions through
the full Pseudolife pipeline: ingest each haystack session turn-by-turn, dream
after every session (the real cadence — consolidation between sessions), then
answer through three retrieval arms and judge with an LLM:

  * ``rag``    — top-k vector search over the raw turns (the naive baseline)
  * ``cortex`` — consolidated cortex facts + their supersession chains
  * ``hybrid`` — cortex facts + a small top-k of raw turns (the agent's view)

Summaries additionally derive a ``cascade`` line from the judged rag/cortex
arms (cortex answer when that arm commits, rag fallback on abstention) — a
serving-policy metric, not a fourth answered arm; see ``replicate.py``.

Model roles: the EXTRACTOR is the experiment variable (``--extractor``,
floor = the shipped Gemma 4 E2B weights, ceiling = Qwen3.6-27B); the ANSWERER
and JUDGE are always the Qwen endpoint so runs stay comparable. The rag arm
never touches the extractor, so it doubles as a cross-run control. Everything
runs on local OpenAI-compatible endpoints — nothing leaves the machine.

Phases (``--phase``) decouple GPU tenancy: ``extract`` ingests + dreams +
persists the retrieval contexts per question (only the extractor endpoint is
needed); ``answer`` fills in answers + judgements from the persisted contexts
(only the Qwen endpoint is needed); ``full`` (default) does both in one pass.

Dataset: HuggingFace ``xiaowu0162/longmemeval-cleaned`` JSONs downloaded into
``evals/data/`` (gitignored): ``longmemeval_oracle.json`` (evidence sessions
only — pipeline check) and ``longmemeval_s_cleaned.json`` (~50-session /
~115k-token haystacks — the real number).

Isolation: same dedicated ``pseudolife_memory_bench`` DB as the ladder — the
live bank is never touched. Results append per-question to a resumable JSONL
(kill and rerun to continue), with a summary JSON written by ``--report``.

Usage (repo root):

  PYTHONPATH=. python evals/longmemeval_bench.py --dataset oracle --limit 3
  PYTHONPATH=. python evals/longmemeval_bench.py --dataset s --extractor qwen-27b
  PYTHONPATH=. python evals/longmemeval_bench.py --dataset s --extractor gemma-e2b --phase extract
  PYTHONPATH=. python evals/longmemeval_bench.py --dataset s --extractor gemma-e2b --phase answer
  PYTHONPATH=. python evals/longmemeval_bench.py --dataset s --extractor gemma-e2b --report
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")                # embedder on CPU
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from ladder_sweep import approx_tokens, build_service, probe  # noqa: E402
from replicate import cascade_correct, cascade_context_tokens  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DATASETS = {
    "oracle": DATA_DIR / "longmemeval_oracle.json",
    "s": DATA_DIR / "longmemeval_s_cleaned.json",
}
# The experiment variable. gemma-e2b is the smallest ladder-verified sidecar
# bake (the shipped default is now the E4B v2 fine-tune), served on the GPU
# for bench speed (identical outputs at temperature 0).
EXTRACTORS = {
    "qwen-27b": "http://127.0.0.1:1234/v1",
    "gemma-e2b": "http://127.0.0.1:8081/v1",
    "gemma-e4b": "http://127.0.0.1:8081/v1",
    "gemma-e4b-qat": "http://127.0.0.1:8081/v1",
    "e4b-ft": "http://127.0.0.1:8081/v1",
    # Sidecar-upgrade bake-off candidates (2026-07-04) — all on :8081; the
    # operator swaps the served GGUF between runs, as with the gemma rungs.
    "qwen3.5-4b": "http://127.0.0.1:8081/v1",
    "granite-h-tiny": "http://127.0.0.1:8081/v1",
    "lfm2-8b-a1b": "http://127.0.0.1:8081/v1",
    "ornith-9b": "http://127.0.0.1:8081/v1",
    # DiffusionGemma has no llama-server support (PR #24423); serve it with
    # evals/dg_shim.py, which wraps the patched llama-diffusion-cli.
    "diffusiongemma": "http://127.0.0.1:8082/v1",
    "gemma4-26b-qat": "http://127.0.0.1:8081/v1",
    # Claude Sonnet 5 ceiling probe (2026-07-11): served by evals/claude_shim.py
    # wrapping the Max-plan claude CLI (same :8082 shim-swap slot as dg).
    "sonnet-5": "http://127.0.0.1:8082/v1",
    # Smarter-teacher comparators (2026-07-26): claude_shim.py --model
    # claude-opus-5 / claude-fable-5 on dedicated ports (:8082 stays the
    # production sonnet shim).
    "opus-5": "http://127.0.0.1:8083/v1",
    "fable-5": "http://127.0.0.1:8084/v1",
}
# Answerer + judge — constant across runs, so extractor is the only variable.
QWEN_URL = os.environ.get("PSEUDOLIFE_BENCH_QWEN_URL", "http://127.0.0.1:1234/v1")
RAG_TOP_K = 6        # raw-turn context width for the rag + hybrid arms
HYBRID_TOP_K = 3     # raw turns added to cortex facts in the hybrid arm
# 24 @ min_score 0.2 (was 8 @ 0.3): the 2026-07-06 retrieval_sweep.py replay on
# the s-qwen-27b-diag banks showed 0.3 starves 60% of questions outright vs 28%
# at 0.2, with identical judged accuracy (rebuild_contexts.py before/after).
# 0.1 was tried and rejected: more gold facts served, but the extra weak facts
# dilute the context and the answerer abstains on previously-correct questions.
CORTEX_TOP_K = 24
CORTEX_MIN_SCORE = 0.2
ARMS = ("rag", "cortex", "hybrid")

_ANSWER_SYSTEM = (
    "You answer questions about a user from their memory context. Use ONLY the "
    "provided context. When the context shows a fact was updated, answer with "
    "the most CURRENT value — unless the question explicitly asks about an "
    "earlier/initial/previous state, then use the earlier value. Answer in one "
    "short sentence. If the context does not contain the information, say "
    "exactly: I don't know."
)

# Faithful to the official LongMemEval GPT-4o judge for knowledge-update:
# equivalence counts, mentioning the old value is fine IF the updated value is
# what's answered, and abstention questions score on declining to answer.
_JUDGE_SYSTEM = (
    "You grade a model response against a correct answer. Reply with exactly "
    "one word: yes or no.\n"
    "- yes if the response contains or is equivalent to the correct answer.\n"
    "- The question asks about updated knowledge: if the response mentions "
    "outdated information but clearly gives the updated answer as current, "
    "grade yes.\n"
    "- no if the response gives only the outdated value, a different value, "
    "or omits the required information.\n"
    "- If the correct answer indicates the information was never mentioned, "
    "grade yes only if the response abstains (e.g. says it doesn't know)."
)

# Non-KU question types get the same judge minus the update-specific clause
# (its "the question asks about updated knowledge" framing is wrong for the
# other five LongMemEval types). KU rows — and rows from files predating the
# --types extension, which carry no question_type — keep _JUDGE_SYSTEM
# verbatim so canonical results re-judge byte-identically.
_JUDGE_SYSTEM_GENERIC = (
    "You grade a model response against a correct answer. Reply with exactly "
    "one word: yes or no.\n"
    "- yes if the response contains or is equivalent to the correct answer.\n"
    "- no if the response gives a different value or omits the required "
    "information.\n"
    "- If the correct answer indicates the information was never mentioned, "
    "grade yes only if the response abstains (e.g. says it doesn't know)."
)


def _chat(system: str, user: str, *, max_tokens: int = 256,
          timeout: float = 600.0) -> str:
    body = json.dumps({
        "model": "bench",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"{QWEN_URL.rstrip('/')}/chat/completions", data=body,
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return (data["choices"][0]["message"]["content"] or "").strip()


def _parse_date(raw: str) -> datetime:
    # haystack_dates look like "2023/04/10 (Mon) 02:03"
    cleaned = re.sub(r"\s*\(\w+\)\s*", " ", raw or "").strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return datetime.min


# Question-type machinery (2026-08-02 --types extension, design doc
# docs/superpowers/specs/2026-08-02-lme-types-extension-design.md). The
# default stays the KU slice with byte-identical artifact names; the other
# five types add 422 questions for statistical power + LME-500
# comparability.
ALL_TYPES = ("knowledge-update", "multi-session", "temporal-reasoning",
             "single-session-user", "single-session-assistant",
             "single-session-preference")
_TYPE_SLUGS = {"knowledge-update": "ku", "multi-session": "ms",
               "temporal-reasoning": "tr", "single-session-user": "ssu",
               "single-session-assistant": "ssa",
               "single-session-preference": "ssp"}
DEFAULT_TYPES = ("knowledge-update",)


def parse_types(spec: str) -> tuple[str, ...]:
    if not spec or spec == "knowledge-update":
        return DEFAULT_TYPES
    if spec == "all":
        return ALL_TYPES
    types = tuple(s.strip() for s in spec.split(",") if s.strip())
    unknown = [t for t in types if t not in ALL_TYPES]
    if unknown:
        raise SystemExit(f"unknown question types {unknown}; "
                         f"valid: {', '.join(ALL_TYPES)} or 'all'")
    return types


def types_slug(types: tuple[str, ...]) -> str:
    """Artifact-name component: 'ku' for the default (existing filenames
    stay byte-identical), 'all' for the full set, joined codes otherwise."""
    if tuple(types) == DEFAULT_TYPES:
        return "ku"
    if set(types) == set(ALL_TYPES):
        return "all"
    return "-".join(_TYPE_SLUGS[t] for t in types)


def load_questions(dataset: str,
                   types: tuple[str, ...] = DEFAULT_TYPES) -> list[dict]:
    data = json.loads(DATASETS[dataset].read_text(encoding="utf-8"))
    return [q for q in data if q["question_type"] in types]


def out_file(dataset: str, extractor: str, tag: str = "",
             slug: str = "ku") -> Path:
    suffix = f"-{tag}" if tag else ""
    return RESULTS_DIR / f"longmemeval-{slug}-{dataset}-{extractor}{suffix}.jsonl"


def bank_dir(dataset: str, extractor: str, tag: str = "",
             slug: str = "ku") -> Path:
    suffix = f"-{tag}" if tag else ""
    prefix = "" if slug == "ku" else f"{slug}-"     # existing bank dirs keep their names
    return RESULTS_DIR / "banks" / f"{prefix}{dataset}-{extractor}{suffix}"


def _norm_text(s) -> str:
    return re.sub(r"\s+", " ", str(s).lower().strip())


def dump_bank(svc, q: dict, path: Path) -> list[dict]:
    """Persist the question's full fact bank (with per-slot history chains).

    Fact embeddings are encode_single(f"{entity} {attribute} {value}") and
    cortex search is plain cosine over them, so this dump is sufficient to
    replay retrieval offline EXACTLY under different top_k / min_score."""
    facts = svc.cortex_dump().get("entries", [])
    for f in facts:
        f.pop("source_entries", None)             # bulky, not needed offline
        try:
            versions = svc.history(f["entity"], f["attribute"]).get("versions", [])
            f["history"] = [v.get("value") for v in versions]  # oldest→newest
        except Exception:  # noqa: BLE001 — history is garnish, never fatal
            f["history"] = [f.get("value")]
    payload = {"question_id": q["question_id"], "question": q["question"],
               "answer": q["answer"], "question_date": q["question_date"],
               "facts": facts}
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return facts


def diagnose_bank(facts: list[dict], answer) -> dict:
    """Where does the gold answer live? Splits a failure into never-extracted
    (nowhere in the bank), overwritten (history only), or not-retrieved
    (in a current fact but absent from the served context)."""
    ans = _norm_text(answer)
    in_current = any(ans in _norm_text(f.get("value", "")) for f in facts)
    in_history = any(ans in _norm_text(v)
                     for f in facts for v in (f.get("history") or [])[:-1])
    return {"bank_facts": len(facts),
            "answer_in_current_fact": in_current,
            "answer_in_history_only": (in_history and not in_current)}


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


def rewrite_rows(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def ingest_and_dream(svc, extractor, q: dict, ex_url: str) -> dict:
    """Store every turn session-by-session in chronological order, dreaming
    after each session — the product cadence (consolidation fires between
    sessions, when the user goes quiet)."""
    tally = {"turns": 0, "claims": 0, "inserted": 0, "superseded": 0,
             "extract_seconds": 0.0}
    held = 0
    sessions = sorted(
        zip(q["haystack_dates"], q["haystack_sessions"]),
        key=lambda pair: _parse_date(pair[0]))
    for date, session in sessions:
        for turn in session:
            content = (turn.get("content") or "").strip()
            if not content:
                continue
            svc.store(f"[{date}] {turn['role']}: {content}", source="bench")
            tally["turns"] += 1
        t0 = time.perf_counter()
        while True:
            res = svc.dream_run(extractor, limit=100)
            for k in ("claims", "inserted", "superseded"):
                tally[k] += int(res.get(k, 0))
            if res.get("extractor_failed"):
                # A held cursor still reports pulled>0. Transient model
                # hiccups (malformed JSON on one batch) are the service's
                # job — it holds, retries, then isolates + quarantines the
                # poison entry. Abort only when the endpoint is actually
                # dead, or the hold never resolves.
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


# Agg-recall Phase 1 knobs (spec 2026-08-03-aggregation-aware-recall-design).
# Set by --fact-render / --contiguity / --timeline in main(); defaults keep
# every pre-Phase-1 artifact byte-identical. The hybrid/memory arm follows
# these; the rag control arm is pinned to vanilla retrieval in
# build_contexts regardless (the preregistered tripwire's contract).
FACT_RENDER = "inline"
HYBRID_CONTIG: int | None = None
HYBRID_TIMELINE: bool | None = None
# Phase 2 (chronicle): --chronicle enables event extraction on the bench
# service (pair with --system-prompt-file ku_op_prompt_v7_events.txt) and
# adds a hybrid_ev context arm — vanilla hybrid + the served events block.
CHRONICLE = False
# Aggregation-serving variants (2026-08-06 design): --ev-variants adds
# hybrid_ev_agg (events on either cue, full served list) and
# hybrid_ev_syn (agg + the computed tally line). hybrid_ev itself always
# RECONSTRUCTS the pre-change gate — events iff temporal cue, first 6 —
# so it stays byte-comparable to the ev2-sep-0804 run.
EV_VARIANTS = False


def _fmt_epoch_date(v) -> str | None:
    """Epoch seconds → ``YYYY-MM-DD`` (UTC), or None when absent/zero."""
    if not v:
        return None
    return time.strftime("%Y-%m-%d", time.gmtime(float(v)))


def _compose_fact_line(f: dict, versions: list[dict],
                       enumerated: bool = False) -> str:
    """One served fact line: ``entity — attribute: value``, plus garnish.

    Scalar facts (no ``"kind"``, or ``"kind" != "set"``) show earlier
    (superseded) values, oldest first — the existing "earlier values"
    idiom. Set-slot facts (``f["kind"] == "set"``) already carry their
    composed current membership in ``value`` (``cortex_search`` groups a
    set slot into one entry, Task 6); the garnish for a set instead lists
    formerly-current members pulled from the set-shaped ``history()``
    ``"removed"`` events, oldest first — "former members", not "earlier
    values", since a set has no single supersession chain to walk.

    Pure and GPU-free: ``versions`` is whatever the caller already fetched
    from ``svc.history(...)["versions"]`` (or ``[]`` on failure/miss), so
    this composes offline and is unit-testable without a service or model.

    ``enumerated=True`` (Phase 1 knob 3) renders chains and set members as
    numbered, dated, one-per-line blocks instead of inline garnish — the
    2026-08-03 autopsy showed the answerer miscounting values that were
    fully present but "a -> b -> c"-rendered. The current value always
    leads (stale demotion: an older value never renders above its
    replacement) and never repeats in the chain.
    """
    line = (f"{f.get('entity', '')} — {f.get('attribute', '')}: "
            f"{f.get('value', '')}")
    if enumerated:
        if f.get("kind") == "set":
            out = [line]
            members = f.get("members", [])
            if members:
                out.append("  members:")
                for i, m in enumerate(members, 1):
                    d = _fmt_epoch_date(m.get("asserted_at"))
                    out.append(f"  {i}. {m.get('value', '')}"
                               + (f" ({d})" if d else ""))
            current_norm = {(m.get("value") or "").strip().casefold()
                            for m in members}
            removed = [v for v in versions
                       if v.get("event") == "removed" and v.get("value")
                       and (v.get("value") or "").strip().casefold()
                       not in current_norm]
            if removed:
                out.append("  former members:")
                for i, v in enumerate(removed, 1):
                    d = _fmt_epoch_date(v.get("at"))
                    out.append(f"  {i}. {v.get('value', '')}"
                               + (f" (removed {d})" if d else " (removed)"))
            return "\n".join(out)
        older = [v for v in versions[:-1]
                 if v.get("value") and v.get("value") != f.get("value")]
        if not older:
            return line
        out = [line, "  earlier values, oldest first:"]
        for i, v in enumerate(older, 1):
            d = _fmt_epoch_date(v.get("tx_time") or v.get("asserted_at"))
            out.append(f"  {i}. {v.get('value', '')}"
                       + (f" ({d})" if d else ""))
        return "\n".join(out)
    if f.get("kind") == "set":
        # A remove-then-re-add leaves a "removed" event for the value AND a
        # current member carrying it (re-adding mints a fresh current row
        # rather than resurrecting the old one — CortexStore.add_member),
        # so filter "removed" events against the CURRENT membership
        # (normalised — casefold/strip, matching the store's own dedup
        # norm) — otherwise a currently-current member gets mislabeled
        # "former" (Task 6 review finding F3).
        current_norm = {(m.get("value") or "").strip().casefold()
                         for m in f.get("members", [])}
        removed = [v.get("value", "") for v in versions
                   if v.get("event") == "removed" and v.get("value")
                   and (v.get("value") or "").strip().casefold() not in current_norm]
        if removed:
            line += "  (former members: " + " -> ".join(removed) + ")"
        return line
    older = [v.get("value", "") for v in versions[:-1]
             if v.get("value") and v.get("value") != f.get("value")]
    if older:
        line += "  (earlier values, oldest first: " + " -> ".join(older) + ")"
    return line


def build_contexts(svc, question: str, variants: bool = False) -> dict[str, str]:
    # Control-arm contract (spec 2026-08-03): the rag arm ALWAYS uses
    # vanilla retrieval — Phase-1 knobs pinned off per-call — so a rag
    # delta between runs signals harness/era drift, never a knob under
    # test. The hybrid/memory arm follows the CLI/config knobs. With
    # knobs at their defaults the two calls return identical entries and
    # every pre-Phase-1 artifact stays byte-identical.
    #
    # ``variants=True`` (spec Amendment 2026-08-03): five hybrid variants
    # built from the SAME live service — vanilla (shares the pinned
    # control call: byte-identical baseline by construction), +contiguity,
    # +timeline, +enumerated facts, and all three combined — so knob
    # deltas pair within-question over an identical bank.
    pinned = svc.search(question, top_k=RAG_TOP_K,
                        contiguity_neighbors=0, timeline=False)
    raw = pinned.get("entries", [])
    raw_texts = [e.get("text", "") for e in raw]
    if variants:
        mem_texts = raw_texts
    else:
        mem = svc.search(question, top_k=RAG_TOP_K,
                         contiguity_neighbors=HYBRID_CONTIG,
                         timeline=HYBRID_TIMELINE).get("entries", [])
        mem_texts = [e.get("text", "") for e in mem]
    cortex = svc.cortex_search(question, top_k=CORTEX_TOP_K,
                               min_score=CORTEX_MIN_SCORE).get("entries", [])
    # Facts carry their supersession chain: knowledge-update asks about BOTH
    # the current value and the original one ("where did I initially ...") —
    # the version timeline (HLC supersession) is the memory system's actual
    # capability here, so the context must surface it.
    fact_lines, fact_versions = [], []
    for f in cortex:
        try:
            versions = svc.history(f.get("entity", ""),
                                   f.get("attribute", "")).get("versions", [])
        except Exception:  # noqa: BLE001 — history is garnish, never fatal
            versions = []
        fact_versions.append((f, versions))
        fact_lines.append(_compose_fact_line(
            f, versions, enumerated=(FACT_RENDER == "enum")))

    def _hyb(facts: list[str], mems: list[str]) -> str:
        return ("Known facts:\n" + "\n".join(facts) +
                "\n\nRelevant memories:\n" +
                "\n\n".join(mems[:HYBRID_TOP_K]))

    ctx = {
        "rag": "\n\n".join(raw_texts),
        "cortex": "\n".join(fact_lines),
        "hybrid": _hyb(fact_lines, mem_texts),
    }
    if variants:
        def _texts(**kw) -> list[str]:
            got = svc.search(question, top_k=RAG_TOP_K, **kw)
            return [e.get("text", "") for e in got.get("entries", [])]

        ctg = _texts(contiguity_neighbors=1, timeline=False)
        tl = _texts(contiguity_neighbors=0, timeline=True)
        both = _texts(contiguity_neighbors=1, timeline=True)
        enum_lines = [_compose_fact_line(f, v, enumerated=True)
                      for f, v in fact_versions]
        ctx["hybrid_ctg"] = _hyb(fact_lines, ctg)
        ctx["hybrid_tl"] = _hyb(fact_lines, tl)
        ctx["hybrid_enum"] = _hyb(enum_lines, mem_texts)
        ctx["hybrid_all"] = _hyb(enum_lines, both)
    if CHRONICLE:
        # Events come from the PINNED call (same call as the rag control
        # — no extra search). The service now serves on temporal OR
        # aggregation cues (limit 30 on the latter), so the hybrid_ev arm
        # RECONSTRUCTS the pre-change gate — events iff temporal cue,
        # first 6, same ordering (the limit-6 result is a prefix of the
        # limit-30 one) — keeping it byte-comparable to ev2-sep-0804.
        from pseudolife_memory.memory.cms import has_temporal_cue
        events = pinned.get("events") or []

        def _ev_block(evs, total=None, header="Events (dated, oldest first):"):
            if not evs:
                return ""
            lines = [
                (f"- {e['date']}: {e['description']}" if e.get("date")
                 else f"- (undated: {e.get('phrase') or '?'}): "
                      f"{e['description']}")
                for e in evs]
            block = "\n\n" + header + "\n" + "\n".join(lines)
            if total is not None:
                block += f"\nTotal events listed: {total}"
            return block

        old_gate = events[:6] if has_temporal_cue(question) else []
        ctx["hybrid_ev"] = ctx["hybrid"] + _ev_block(old_gate)
        if EV_VARIANTS:
            # agg: either cue (the service already gated), full list.
            # syn: agg + the computed tally — present only when the
            # service marked the query aggregation-cued (events_total).
            # hdr: syn content under a partial-record header — the
            # anti-suppression arm (2026-08-06 quantity+coverage design;
            # 6/8 BEAM event_ordering losses were 'I don't know' on
            # questions the vanilla hybrid context answered).
            ctx["hybrid_ev_agg"] = ctx["hybrid"] + _ev_block(events)
            ctx["hybrid_ev_syn"] = ctx["hybrid"] + _ev_block(
                events, total=pinned.get("events_total"))
            ctx["hybrid_ev_hdr"] = ctx["hybrid"] + _ev_block(
                events, total=pinned.get("events_total"),
                header=("Events (dated, oldest first; partial record — "
                        "other context may hold more):"))
    return ctx


def answer_and_judge(row: dict) -> dict:
    """Fill the answer/judge fields on a row from its persisted contexts."""
    # Missing question_type (pre---types files) falls back to the KU judge
    # so canonical artifacts re-judge byte-identically.
    judge_system = (_JUDGE_SYSTEM
                    if row.get("question_type", "knowledge-update")
                    == "knowledge-update" else _JUDGE_SYSTEM_GENERIC)
    # Every persisted context arm gets answered and judged — pre-variants
    # rows carry exactly the three ARMS keys in that order, so their
    # call sequence (and artifacts) is unchanged; variant rows add their
    # hybrid_* arms.
    for arm in row["contexts"]:
        ctx = row["contexts"].get(arm, "")
        prompt = (f"Question date: {row['question_date']}\n"
                  f"Question: {row['question']}\n\nMemory context:\n{ctx or '(empty)'}")
        response = _chat(_ANSWER_SYSTEM, prompt)
        verdict = _chat(judge_system, (
            f"Question: {row['question']}\n"
            f"Correct answer: {row['answer']}\n"
            f"Model response: {response}"), max_tokens=8)
        row[f"{arm}_response"] = response
        row[f"{arm}_correct"] = verdict.strip().lower().startswith("yes")
        row[f"{arm}_context_tokens"] = approx_tokens(ctx)
    return row


def _make_extractor(ex_url: str, system_prompt_file: str | None):
    """The bench extractor, optionally with a prompt-variant override.
    ``--system-prompt-file`` makes prompt A/B runs first-class — the
    extraction-variance baseline runs the control prompt through the
    identical code path instead of a code flip."""
    from pseudolife_memory.memory.dream import OpenAICompatExtractor
    system_prompt = (Path(system_prompt_file).read_text(encoding="utf-8")
                     if system_prompt_file else None)
    return OpenAICompatExtractor(ex_url, "bench", max_tokens=4096,
                                 timeout_seconds=600.0,
                                 system_prompt=system_prompt)


def run_extract(dataset: str, limit: int | None, extractor_name: str,
                do_answer: bool, tag: str = "", window: int = 0,
                system_prompt_file: str | None = None,
                qids: str | None = None,
                types: tuple[str, ...] = DEFAULT_TYPES,
                variants: bool = False) -> None:
    ex_url = EXTRACTORS[extractor_name]
    if not probe(ex_url):
        sys.exit(f"no extractor server at {ex_url} — start it first")
    if do_answer and not probe(QWEN_URL):
        sys.exit(f"no answer/judge server at {QWEN_URL} — start it first")
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    slug = types_slug(types)
    questions = load_questions(dataset, types)
    if limit:
        questions = questions[:limit]
    if qids:
        keep = {s.strip() for s in qids.split(",") if s.strip()}
        questions = [q for q in questions if q["question_id"] in keep]
        missing = keep - {q["question_id"] for q in questions}
        if missing:
            sys.exit(f"unknown question_ids: {sorted(missing)}")
    out_path = out_file(dataset, extractor_name, tag, slug)
    done = {r["question_id"] for r in load_rows(out_path)}
    print(f"{len(questions)} questions [{slug}], extractor="
          f"{extractor_name} ({len(done)} already done, resuming)", flush=True)

    for i, q in enumerate(questions):
        if q["question_id"] in done:
            continue
        t_start = time.perf_counter()
        tmp = Path(tempfile.mkdtemp(prefix="lme_"))
        svc = build_service(tmp)                      # fresh, truncated bench DB
        svc.config.memory.dream.extract_relations = False   # facts only
        svc.config.memory.dream.known_facts_window = window
        svc.config.memory.dream.chronicle = CHRONICLE
        extractor = _make_extractor(ex_url, system_prompt_file)
        tally = ingest_and_dream(svc, extractor, q, ex_url)
        contexts = build_contexts(svc, q["question"], variants=variants)
        facts = dump_bank(svc, q, bank_dir(dataset, extractor_name, tag,
                                           slug)
                          / f"{q['question_id']}.json.gz")
        svc.flush()
        row = {
            "question_id": q["question_id"],
            "question": q["question"],
            "question_type": q["question_type"],
            "answer": q["answer"],
            "question_date": q["question_date"],
            "abstention": q["question_id"].endswith("_abs"),
            "sessions": len(q["haystack_sessions"]),
            "extractor": extractor_name,
            "window": window,
            "contexts": contexts,
            "consolidation": tally,
            "wall_seconds": round(time.perf_counter() - t_start, 1),
            **diagnose_bank(facts, q["answer"]),
        }
        marks = "extracted"
        if do_answer:
            row = answer_and_judge(row)
            marks = " ".join(f"{a}={'Y' if row[f'{a}_correct'] else 'n'}"
                             for a in ARMS)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{i + 1}/{len(questions)}] {q['question_id']}  {marks}  "
              f"({row['wall_seconds']}s, {tally['turns']} turns, "
              f"{tally['superseded']} superseded)", flush=True)


def run_answer(dataset: str, extractor_name: str, tag: str = "",
               types: tuple[str, ...] = DEFAULT_TYPES) -> None:
    if not probe(QWEN_URL):
        sys.exit(f"no answer/judge server at {QWEN_URL} — start it first")
    out_path = out_file(dataset, extractor_name, tag, types_slug(types))
    rows = load_rows(out_path)
    pending = [r for r in rows if "rag_correct" not in r]
    print(f"answer phase: {len(pending)} of {len(rows)} rows pending", flush=True)
    for i, row in enumerate(pending):
        answer_and_judge(row)
        rewrite_rows(out_path, rows)          # atomic, resumable per row
        marks = " ".join(f"{a}={'Y' if row[f'{a}_correct'] else 'n'}"
                         for a in ARMS)
        print(f"[{i + 1}/{len(pending)}] {row['question_id']}  {marks}", flush=True)


def report(dataset: str, extractor_name: str, tag: str = "",
           types: tuple[str, ...] = DEFAULT_TYPES) -> None:
    out_path = out_file(dataset, extractor_name, tag, types_slug(types))
    rows = [r for r in load_rows(out_path) if "rag_correct" in r]
    if not rows:
        sys.exit(f"no judged results in {out_path}")
    n = len(rows)
    label = f"{extractor_name}{f' [{tag}]' if tag else ''}"
    print(f"\nLongMemEval knowledge-update — {dataset}, extractor="
          f"{label} ({n} questions)")
    print(f"{'arm':<10}{'accuracy':>10}{'ctx tok/q':>12}")
    summary = {"dataset": dataset, "extractor": extractor_name, "n": n,
               "arms": {}}
    # Variant arms (hybrid_ctg etc.) are detected from the rows so old
    # three-arm artifacts report identically.
    extra_arms = tuple(sorted(
        {k.removesuffix("_correct") for k in rows[0] if k.endswith("_correct")}
        - set(ARMS)))
    for arm in ARMS + extra_arms + ("cascade",):
        if arm == "cascade":
            # Derived commit-gated cascade — cortex answer when that arm
            # commits, rag fallback on abstention. Computed from the judged
            # arms above; never persisted per-row, so old JSONLs report it
            # retroactively on --report.
            acc = sum(cascade_correct(r) for r in rows) / n
            tok = sum(cascade_context_tokens(r) for r in rows) / n
        else:
            acc = sum(r[f"{arm}_correct"] for r in rows) / n
            tok = sum(r[f"{arm}_context_tokens"] for r in rows) / n
        summary["arms"][arm] = {"accuracy": round(acc, 3),
                                "context_tokens": round(tok, 1)}
        print(f"{arm:<10}{acc:>10.3f}{tok:>12.1f}")
    sup = sum(r["consolidation"]["superseded"] for r in rows)
    print(f"supersessions across runs: {sup}")
    summary["superseded_total"] = sup
    # Per-type breakdown, only when the run spans more than one type.
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r.get("question_type", "knowledge-update"),
                           []).append(r)
    if len(by_type) > 1:
        summary["types"] = {}
        for qt, trows in sorted(by_type.items()):
            tn = len(trows)
            summary["types"][qt] = {
                "n": tn,
                "arms": {arm: round(
                    sum(r[f"{arm}_correct"] for r in trows) / tn, 3)
                    for arm in ARMS + extra_arms},
                "cascade": round(
                    sum(cascade_correct(r) for r in trows) / tn, 3),
            }
            print(f"  {qt:<28} n={tn:<4} " + " ".join(
                f"{arm}={summary['types'][qt]['arms'][arm]:.3f}"
                for arm in ARMS + extra_arms))
    # NOT with_suffix: extractor names contain dots (qwen3.5-4b), which
    # pathlib would treat as a suffix and truncate.
    out_path.with_name(
        out_path.name.removesuffix(".jsonl") + ".summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", choices=list(DATASETS), default="oracle")
    ap.add_argument("--extractor", choices=list(EXTRACTORS), default="qwen-27b")
    ap.add_argument("--phase", choices=("full", "extract", "answer"),
                    default="full")
    ap.add_argument("--limit", type=int, default=None,
                    help="run only the first N questions (smoke test)")
    ap.add_argument("--report", action="store_true",
                    help="summarise existing results instead of running")
    ap.add_argument("--tag", default="",
                    help="namespace suffix for output files/banks "
                         "(e.g. 'diag' — keeps experiment runs apart)")
    ap.add_argument("--system-prompt-file", default=None,
                    help="override the extraction system prompt from a file "
                         "(prompt-variant / variance-baseline runs)")
    ap.add_argument("--window", type=int, default=0,
                    help="known-facts window size for the dream pass "
                         "(0 = off; use 20 for the window arm — spec 2026-07-10)")
    ap.add_argument("--qids", default=None,
                    help="comma-separated question_ids to run (targeted "
                         "extraction / bank forensics; composes with --tag)")
    ap.add_argument("--fact-render", choices=("inline", "enum"),
                    default="inline",
                    help="Fact-context rendering: 'enum' = numbered, dated, "
                         "one-per-line chains/members (Phase 1 knob 3); "
                         "default keeps pre-Phase-1 artifacts byte-identical")
    ap.add_argument("--contiguity", type=int, default=None,
                    help="Temporal-contiguity neighbors per side for the "
                         "hybrid/memory arm (Phase 1 knob 1); rag control "
                         "arm stays pinned to vanilla retrieval")
    ap.add_argument("--timeline", action="store_true", default=None,
                    help="Enable the timeline channel for the hybrid/memory "
                         "arm (Phase 1 knob 2); rag control arm stays pinned")
    ap.add_argument("--variants", action="store_true",
                    help="Build and judge the five within-run hybrid "
                         "variants per question (spec Amendment 2026-08-03) "
                         "— one extraction, knob-only paired deltas")
    ap.add_argument("--types", default="knowledge-update",
                    help="question types: comma list or 'all' (default "
                         "knowledge-update — canonical filenames unchanged; "
                         "other selections get a type-slug artifact prefix)")
    ap.add_argument("--chronicle", action="store_true",
                    help="Phase 2: enable chronicle event extraction on the "
                         "bench service (pair with --system-prompt-file "
                         "ku_op_prompt_v7_events.txt) and add the hybrid_ev "
                         "context arm (hybrid + served events block)")
    ap.add_argument("--ev-variants", action="store_true",
                    help="aggregation-serving variants (2026-08-06 design): "
                         "add hybrid_ev_agg (events on either cue, full "
                         "list) and hybrid_ev_syn (+ computed tally line); "
                         "requires --chronicle")
    args = ap.parse_args()
    if args.ev_variants and not args.chronicle:
        ap.error("--ev-variants requires --chronicle")
    global FACT_RENDER, HYBRID_CONTIG, HYBRID_TIMELINE, CHRONICLE, EV_VARIANTS
    FACT_RENDER = args.fact_render
    HYBRID_CONTIG = args.contiguity
    HYBRID_TIMELINE = args.timeline
    CHRONICLE = args.chronicle
    EV_VARIANTS = args.ev_variants
    types = parse_types(args.types)
    if args.report:
        report(args.dataset, args.extractor, args.tag, types)
        return 0
    if args.phase == "answer":
        run_answer(args.dataset, args.extractor, args.tag, types)
    else:
        run_extract(args.dataset, args.limit, args.extractor,
                    do_answer=(args.phase == "full"), tag=args.tag,
                    window=args.window,
                    system_prompt_file=args.system_prompt_file,
                    qids=args.qids, types=types, variants=args.variants)
    if args.phase != "extract":
        report(args.dataset, args.extractor, args.tag, types)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
