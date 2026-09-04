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
floor = the shipped Gemma 4 E2B weights, ceiling = Qwen3.8-27B); the ANSWERER
and JUDGE are always the Qwen endpoint so runs stay comparable. The rag arm
never touches the extractor, so it doubles as a cross-run control. Everything
runs on local OpenAI-compatible endpoints — nothing leaves the machine.

Phases (``--phase``) decouple GPU tenancy: ``extract`` ingests + dreams +
persists the retrieval contexts per question (only the extractor endpoint is
needed); ``answer`` fills in answers + judgements from the persisted contexts
(only the Qwen endpoint is needed); ``full`` (default) does both in one pass.
One exception: ``--refind`` plans its search with the ANSWERER model, so an
extract phase carrying that arm needs the Qwen endpoint too (probed up front).

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
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")                # embedder on CPU
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from context_format import hybrid_context  # noqa: E402
from ladder_sweep import (approx_tokens, build_service,  # noqa: E402
                          pool_env_knobs, probe)
from replicate import cascade_correct, cascade_context_tokens  # noqa: E402
import answerability_probe  # noqa: E402
import leak_check  # noqa: E402
import nomem_arm  # noqa: E402
import refind_arm  # noqa: E402

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
    # evlora comparators (2026-08-07 design): both served on :8081 like
    # every sidecar rung — operator swaps the GGUF. e4b-v2 is the DEPLOYED
    # artifact (ops/Dockerfile.extractor MODEL_URL, hash-verified from HF);
    # e4b-v3 is the multi-task candidate.
    "e4b-v2": "http://127.0.0.1:8081/v1",
    "e4b-v3": "http://127.0.0.1:8081/v1",
}
# Answerer + judge — constant across runs, so extractor is the only variable.
QWEN_URL = os.environ.get("PSEUDOLIFE_BENCH_QWEN_URL", "http://127.0.0.1:1234/v1")
RAG_TOP_K = 6        # raw-turn context width for the rag + hybrid arms
# 6 (was 3 until 2026-08-21): budget-matched to RAG_TOP_K so the hybrid arm
# is a SUPERSET of the rag control and a hybrid-vs-rag delta isolates the
# fact spine. The 2026-07-30 ceiling-e2e autopsy and the 2026-08-21 BEAM
# review both traced hybrid "losses to rag" to the halved raw-turn budget,
# not the facts. Every artifact row records its effective value
# (hybrid_top_k); rows without the key predate the flip and were served
# at 3. The regression-gate arm1 pinned contexts were also built at 3 —
# re-baseline with a fresh extract before reading the gate's hybrid arm as
# the shipped default.
HYBRID_TOP_K = 6     # raw turns added to cortex facts in the hybrid arm
# 24 @ min_score 0.2 (was 8 @ 0.3): the 2026-07-06 retrieval_sweep.py replay on
# the s-qwen-27b-diag banks showed 0.3 starves 60% of questions outright vs 28%
# at 0.2, with identical judged accuracy (rebuild_contexts.py before/after).
# 0.1 was tried and rejected: more gold facts served, but the extra weak facts
# dilute the context and the answerer abstains on previously-correct questions.
CORTEX_TOP_K = 24
CORTEX_MIN_SCORE = 0.2
ARMS = ("rag", "cortex", "hybrid")
# Token-matched rag arms (2026-09-04 review, lever 1). Every published
# comparison so far pits a small fact context against a large raw-turn one
# and reports accuracy and tokens as two separate findings — when they are
# one trade-off. Measured: cortex 96.7 tokens vs rag 1184.1 on the
# ceiling-v38 LongMemEval run (78 questions), and 551 vs 5539 on BEAM
# chip12-b16 (400 rows) — a 10x cost gap nobody has ever held constant to
# see what the non-consolidating arm scores at it. These two knobs
# serve the rag arm's EXACT retrieval, ranking and formatting at a
# narrower budget, so a non-consolidating comparator can be read at the
# fact spine's token cost. Empty/None = inert: no new context keys, not
# one extra model call, every prior artifact byte-identical.
#   RAG_LITE_TOP_KS  -> arms rag1, rag2, ... (first K ranked turns)
#   RAG_BUDGET_TOKENS -> arm ragb<N> (the ranked turns that fit N tokens)
# Read at CALL time by build_contexts (the HYBRID_TOP_K contract), so both
# harnesses widen together and a knob set by either CLI reaches the same
# single implementation.
RAG_LITE_TOP_KS: tuple[int, ...] = ()
RAG_BUDGET_TOKENS: int | None = None
# Session-digest arm (spec 2026-08-24). OFF: every search call in
# build_contexts is byte-identical to the pre-digest protocol, so all
# prior artifacts pair exactly. ON (beam_adapter --digest): the searches
# OVER-FETCH by DIGEST_COUNT (the bank's exact digest population, set by
# the adapter after ingest) and the control/comparator arms drop digest
# entries post-hoc, then truncate to the original budget. This is the
# only exclusion that preserves the control: the service's ``sources=``
# filter runs AFTER each band's top-k (cms.retrieve), so filtered-out
# digests would silently SHORTEN the served context instead of being
# replaced (2026-08-25 review finding) — and post-hoc dropping also
# covers channels the filter never sees (contiguity neighbors reach the
# result via episode scope). With top_k widened by the full digest count,
# at least the original budget of beam turns survives the drop, in
# exactly the ranking a digest-free bank would produce. The
# hybrid_digest arm reuses the same widened call with digests kept,
# budget-matched BY CHARACTERS to the hybrid arm's mem block (coverage
# pays for itself, never rides on a bigger window).
DIGEST_ARM = False
DIGEST_COUNT = 0

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


_THINKING_LEVELS = ("low", "medium", "xhigh")
# Fields the sampler knob may never override: the conversation itself, the
# model id, and the thinking pin (the thinking knob is the one sanctioned
# way to change that; a sampler JSON that clobbered it would silently turn a
# "sampled, no-think" arm into a thinking arm).
_SAMPLER_PROTECTED = ("messages", "model", "chat_template_kwargs")


def bench_env_knobs() -> dict:
    """The experiment-knob state, for stamping into artifacts — a judged
    run whose config can't be audited afterwards is the failure mode the
    reproducible-server discipline exists to prevent."""
    return {
        "thinking": os.environ.get("PSEUDOLIFE_BENCH_THINKING", "").strip()
        or None,
        "sampler": os.environ.get("PSEUDOLIFE_BENCH_SAMPLER", "").strip()
        or None,
        # Associative retrieval knobs (memory.search). Applied by
        # ladder_sweep.build_service, which every bench service goes
        # through; None means the shipped default. They reach ONLY the
        # arms that call svc.search() (rag, hybrid's raw-turn block), and
        # only on a --phase extract run — rebuild_contexts.py copies the
        # associative context verbatim and cannot honour them.
        "candidate_pool": pool_env_knobs(),
    }


def _chat(system: str, user: str, *, max_tokens: int = 256,
          timeout: float = 600.0) -> str:
    # Experiment knobs (2026-08-17 synthesis plan). Defaults are the
    # permanent regression-gate config and stay byte-identical:
    #   PSEUDOLIFE_BENCH_THINKING=low|medium — labeled thinking arms:
    #     replaces the enable_thinking:false pin with a per-request
    #     reasoning_effort and adds reasoning headroom (reasoning tokens
    #     count against max_tokens).
    #   PSEUDOLIFE_BENCH_SAMPLER=<json> — merged into the body LAST (e.g.
    #     official instruct sampler + fixed seed for the seeded pilot).
    payload: dict = {
        "model": "bench",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    thinking = os.environ.get("PSEUDOLIFE_BENCH_THINKING", "").strip().lower()
    if thinking:
        if thinking not in _THINKING_LEVELS:
            raise ValueError(
                f"PSEUDOLIFE_BENCH_THINKING={thinking!r} — expected one of "
                f"{_THINKING_LEVELS} (the 3.8 template accepts exactly "
                f"these; only 'none' is rejected)")
        payload["chat_template_kwargs"] = {"reasoning_effort": thinking}
        payload["max_tokens"] = max_tokens + 4096
    sampler = os.environ.get("PSEUDOLIFE_BENCH_SAMPLER", "").strip()
    if sampler:
        overrides = json.loads(sampler)
        for k in _SAMPLER_PROTECTED:
            overrides.pop(k, None)
        payload.update(overrides)
    body = json.dumps(payload).encode()
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
        # Read-time annotations over those same traces: absent from the
        # offline replay, and present or not depending on when the dump ran,
        # so leaving them in churns committed bank artifacts for no signal.
        f.pop("re_verify", None)
        f.pop("re_verify_reason", None)
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


def _chronological_sessions(q: dict) -> list[tuple[str, list]]:
    """The haystack in ingest order. Both the ingest path and the ReFind
    archive read it through here so their turn ordering cannot drift."""
    return sorted(zip(q["haystack_dates"], q["haystack_sessions"]),
                  key=lambda pair: _parse_date(pair[0]))


def archive_from_lme_question(q: dict) -> refind_arm.LexicalArchive:
    """The ReFind arm's lexical archive for one question, built from the
    SAME haystack turns, in the same order and the same stored text as
    ``ingest_and_dream`` writes into the bank — so a refind-vs-rag delta
    is about the retrieval loop and not a different corpus. Pinned
    turn-for-turn against the ingest path by
    ``test_archive_mirrors_what_ingest_stores_turn_for_turn``."""
    records, ordinal = [], 0
    for index, (date, session) in enumerate(_chronological_sessions(q), 1):
        for turn in session:
            content = (turn.get("content") or "").strip()
            if not content:
                continue
            ordinal += 1
            records.append(refind_arm.ArchiveRecord(
                text=f"[{date}] {turn['role']}: {content}",
                session=str(index), ordinal=ordinal,
                date=refind_arm.parse_anchor(date)))
    return refind_arm.LexicalArchive(records)


def rag_lite_arm_names(top_ks: tuple[int, ...],
                       budget_tokens: int | None) -> tuple[str, ...]:
    """The arm names a given rag-lite config serves, in served order.

    One implementation for both harnesses: the BEAM adapter needs the
    names up front (its answer loop iterates a fixed arm tuple) while
    LongMemEval discovers them from the persisted contexts, and a name
    minted differently in the two places would pair two different arms.
    """
    names = tuple(f"rag{k}" for k in top_ks)
    if budget_tokens is not None:
        names += (f"ragb{budget_tokens}",)
    return names


def validate_rag_lite(top_ks: tuple[int, ...], budget_tokens: int | None,
                      rag_width: int) -> None:
    """Reject a rag-lite config before any bench global moves.

    A width at or above the rag control's would serve a byte-identical
    copy of the control under a second name — a judged arm that measures
    nothing and costs a full answer+judge pass per question.

    The BUDGET path carries no equivalent near-duplicate guard, and
    cannot: what a token budget resolves to is a property of the data,
    not of the flag. A budget above the control's cost serves a copy of
    the control (``ragb100000`` on a 1,200-token run) and a budget below
    the cost of one turn serves exactly ``rag1`` (measured: ``ragb100``
    equalled ``rag1`` on 74 of the 78 raglite-v38 rows). Only the run's
    recorded ``{arm}_context_tokens`` mean can say which happened, which
    is why ``rag_lite_contexts`` warns at serve time and both summaries
    carry ``budget_overshoot_rows``.
    """
    for k in top_ks:
        if k < 1:
            raise SystemExit("--rag-lite-top-k values must be positive")
        if k >= rag_width:
            raise SystemExit(
                f"--rag-lite-top-k {k} is not narrower than the rag "
                f"control's width {rag_width}; it would serve a copy of "
                "the control under another name")
    if len(set(top_ks)) != len(top_ks):
        raise SystemExit("--rag-lite-top-k lists a width twice")
    if budget_tokens is not None and budget_tokens < 1:
        raise SystemExit("--rag-budget-tokens must be positive")


def parse_rag_lite_top_ks(spec: str | None,
                          flag: str = "--rag-lite-top-k"
                          ) -> tuple[int, ...]:
    """``"1,2"`` -> ``(1, 2)``; empty/None -> ``()`` (arm off).

    ``flag`` only names the flag in the error: rag_lite_rebuild.py
    parses its budget list with the same function.
    """
    if not spec:
        return ()
    try:
        return tuple(int(part) for part in spec.split(",") if part.strip())
    except ValueError:
        raise SystemExit(
            f"{flag} takes a comma-separated list of integers, "
            f"got {spec!r}") from None


def rag_lite_contexts(raw_texts: list[str], top_ks: tuple[int, ...],
                      budget_tokens: int | None) -> dict[str, str]:
    """The token-matched rag arms for one question.

    Built from the rag control's OWN ranked turn list and joined with the
    control's separator, so every arm here is a strict prefix of
    ``contexts["rag"]`` — same retrieval, same ranking, same formatting,
    same answer prompt and judge, only fewer turns. That prefix property
    is what makes a rag-vs-rag1 delta a budget effect and nothing else;
    ``test_rag_lite_arms.py`` pins it.

    The budget arm grows the prefix while the SERVED context still fits
    ``budget_tokens`` under ``approx_tokens`` (the len//4 convention the
    row's ``{arm}_context_tokens`` is recorded in) — measured on the
    joined block, not summed per turn, so the recorded number is the one
    that was bounded. At least one turn is always served: an arm that can
    serve empty would silently become a second no-memory control.
    """
    out: dict[str, str] = {}
    for k in top_ks:
        out[f"rag{k}"] = "\n\n".join(raw_texts[:k])
    if budget_tokens is not None:
        kept: list[str] = []
        for text in raw_texts:
            candidate = kept + [text]
            if kept and approx_tokens("\n\n".join(candidate)) > budget_tokens:
                break
            kept = candidate
        served = "\n\n".join(kept)
        name = f"ragb{budget_tokens}"
        out[name] = served
        if approx_tokens(served) > budget_tokens:
            # Loud, because on LongMemEval this is the COMMON case, not
            # an edge one: a single raw turn is already ~200 tokens, so
            # a 100-token budget served a mean 219.2 tokens over the 78
            # raglite-v38 rows (36 of them above the budget) and was
            # byte-identical to rag1 on 74 of them. An arm named for a
            # budget it misses by 2.2x is not a token-matched
            # comparator, and its name is the only place that says 100.
            # The message text is constant so Python's default filter
            # prints it once per run, not once per question.
            warnings.warn(
                f"the {name} arm's served block exceeds its budget: "
                "one ranked turn alone is over budget and the arm's "
                "floor is one turn, so it serves more than "
                f"{budget_tokens} approximate tokens. Read the run's "
                f"measured {name}_context_tokens mean and its "
                "budget_overshoot_rows count, not the arm's name.",
                stacklevel=2)
    return out


def budget_overshoot(rows: list[dict], arm: str) -> int | None:
    """How many rows a ``ragb<N>`` arm served ABOVE its own budget.

    ``None`` for every other arm: only a budget arm has a budget to
    miss. Both harnesses' summaries carry it beside the arm's mean
    cost, so a reader of the artifact meets the overshoot without
    recomputing it — the runbook's 2026-09-04 correction (ragb100
    served 2.2x its name) had to be measured by hand because nothing
    published it.
    """
    m = re.fullmatch(r"ragb(\d+)", arm)
    if not m:
        return None
    budget = int(m.group(1))
    key = f"{arm}_context_tokens"
    return sum(1 for r in rows if int(r.get(key, 0)) > budget)


def serve_comparator_arms(contexts: dict, question: str, *, archive=None,
                          refind: bool = False, nomem: bool = False,
                          refind_kwargs: dict | None = None,
                          chat=None) -> dict | None:
    """Add the comparator arms to a context dict; returns the ReFind trace
    (None when that arm is off).

    Shared by both harnesses — the BEAM adapter calls it too, so the two
    cannot drift into serving these arms differently. Off by default and
    inert when off: no new context keys and not one extra model call, so
    every pre-existing artifact stays byte-identical.
    """
    trace = None
    if refind:
        if archive is None:
            raise SystemExit("the refind arm needs an archive — "
                             "build one from the question's raw turns first")
        kwargs = dict(refind_kwargs or {})
        if kwargs.get("top_k") is None:
            # Budget-matched to the rag control, read HERE rather than at
            # flag-parse time — the same call-time contract HYBRID_TOP_K
            # carries, so --rag-top-k style widening reaches both arms.
            kwargs["top_k"] = RAG_TOP_K
        contexts["refind"], trace = refind_arm.refind_search(
            archive, question, chat=chat or _chat, **kwargs)
    if nomem:
        # Recorded as an explicit empty context: the artifact has to show
        # the arm was served nothing (leak_check.py checks exactly that).
        contexts["nomem"] = ""
    return trace


def ingest_and_dream(svc, extractor, q: dict, ex_url: str) -> dict:
    """Store every turn session-by-session in chronological order, dreaming
    after each session — the product cadence (consolidation fires between
    sessions, when the user goes quiet)."""
    tally = {"turns": 0, "claims": 0, "inserted": 0, "superseded": 0,
             "extract_seconds": 0.0}
    held = 0
    for date, session in _chronological_sessions(q):
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


def build_contexts(svc, question: str, variants: bool = False,
                   with_parts: bool = False) -> dict[str, str]:
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
    # DIGEST_ARM over-fetches by the bank's digest count and drops digest
    # entries post-hoc (see the DIGEST_ARM comment for why the service's
    # sources= filter cannot do this); OFF adds 0 and drops nothing —
    # byte-identical pre-digest behavior.
    _over = DIGEST_COUNT if DIGEST_ARM else 0

    def _nondigest(entries: list[dict], cap: int) -> list[dict]:
        if not DIGEST_ARM:
            return entries
        return [e for e in entries if e.get("source") != "digest"][:cap]

    pinned = svc.search(question, top_k=RAG_TOP_K + _over,
                        contiguity_neighbors=0, timeline=False)
    raw = _nondigest(pinned.get("entries", []), RAG_TOP_K)
    raw_texts = [e.get("text", "") for e in raw]
    if variants:
        mem_texts = raw_texts
        mem_mixed: list[dict] = []
    else:
        mem_mixed = svc.search(question, top_k=RAG_TOP_K + _over,
                               contiguity_neighbors=HYBRID_CONTIG,
                               timeline=HYBRID_TIMELINE).get("entries", [])
        mem_texts = [e.get("text", "")
                     for e in _nondigest(mem_mixed, RAG_TOP_K)]
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
        return hybrid_context(facts, mems[:HYBRID_TOP_K])

    ctx = {
        "rag": "\n\n".join(raw_texts),
        "cortex": "\n".join(fact_lines),
        "hybrid": _hyb(fact_lines, mem_texts),
    }
    # Token-matched rag arms, from the control's OWN ranked turns
    # (both harnesses reach build_contexts, so they cannot drift into
    # serving these differently). Knobs read at call time; the default
    # empty config adds no keys at all.
    ctx.update(rag_lite_contexts(raw_texts, RAG_LITE_TOP_KS,
                                 RAG_BUDGET_TOKENS))
    digest_texts: list[str] = []
    if DIGEST_ARM and not variants:
        # The digest-eligible arm reuses the widened mem call WITH digests
        # kept, budget-matched BY CHARACTERS to the hybrid arm's served
        # mem block: take ranked beam+digest entries while they fit the
        # byte budget the hybrid block actually used (always at least
        # one, so the arm can never serve empty against a non-empty
        # comparator).
        budget = sum(len(t) for t in mem_texts[:HYBRID_TOP_K])
        used = 0
        for e in mem_mixed:
            t = e.get("text", "")
            if digest_texts and used + len(t) > budget:
                break
            digest_texts.append(t)
            used += len(t)
            if len(digest_texts) >= HYBRID_TOP_K:
                break
        ctx["hybrid_digest"] = _hyb(fact_lines, digest_texts)
    if with_parts:
        # Structured serve state, persisted with the row by the BEAM
        # adapter: a serving-knob rerun (hybrid_top_k slice, fact render)
        # recomposes contexts offline from these instead of re-paying
        # ingest. The chronicle branch below fills events when active.
        ctx["parts"] = {"raw": raw_texts, "mem": mem_texts,
                        "facts": fact_lines, "events": []}
        if DIGEST_ARM and not variants:
            ctx["parts"]["mem_digest"] = digest_texts
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
        if with_parts:
            ctx["parts"]["events"] = old_gate
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
            # ins: syn + a directive footer (2026-08-07 evlora design).
            # The descriptive hdr hedge rescued none of the 3 measured
            # block-authority losses and flipped zero multi-session rows
            # (evq-residual-decomposition-0807); this arm tests the
            # directive lever instead.
            ctx["hybrid_ev_ins"] = ctx["hybrid_ev_syn"] + (
                "\nThis list is an extracted index, not the complete "
                "record: when counting or totaling, re-scan the "
                "conversation above and include occurrences not listed "
                "here.")
    return ctx


def answer_call(arm: str, question: str, question_date, ctx: str
                ) -> tuple[str, str]:
    """The (system, prompt) pair for one arm's answer call.

    Every memory arm shares the answerer and context block, and that
    prompt text is a CONTRACT — the regression gate re-answers pinned
    contexts with it, so it is reproduced here byte for byte. The
    no-memory arm shares the question framing (date prefix included) and
    drops the context clauses entirely rather than serving an "(empty)"
    block, which is itself a framing the other arms do not see.
    """
    if arm == "nomem":
        # Built with THIS harness's length policy: its judge grades on
        # containment, so a no-memory arm told to answer completely would
        # get more shots at the gold than the one-sentence arms it bounds.
        return (nomem_arm.nomem_system(nomem_arm.LENGTH_ONE_SENTENCE),
                nomem_arm.nomem_prompt(question, question_date))
    return _ANSWER_SYSTEM, (f"Question date: {question_date}\n"
                            f"Question: {question}\n\n"
                            f"Memory context:\n{ctx or '(empty)'}")


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
        system, prompt = answer_call(arm, row["question"],
                                     row["question_date"], ctx)
        response = _chat(system, prompt)
        verdict = _chat(judge_system, (
            f"Question: {row['question']}\n"
            f"Correct answer: {row['answer']}\n"
            f"Model response: {response}"), max_tokens=8)
        row[f"{arm}_response"] = response
        row[f"{arm}_correct"] = verdict.strip().lower().startswith("yes")
        row[f"{arm}_context_tokens"] = approx_tokens(ctx)
    return row


def _make_extractor(ex_url: str, system_prompt_file: str | None,
                    events_prompt_file: str | None = None):
    """The bench extractor, optionally with prompt-variant overrides.
    ``--system-prompt-file`` (claims) and ``--events-prompt-file`` (the
    separate events pass) make prompt A/B runs first-class — a candidate
    prompt runs through the identical code path instead of a code flip,
    and the shipped constants stay untouched."""
    from pseudolife_memory.memory.dream import OpenAICompatExtractor
    system_prompt = (Path(system_prompt_file).read_text(encoding="utf-8")
                     if system_prompt_file else None)
    events_prompt = (Path(events_prompt_file).read_text(encoding="utf-8")
                     if events_prompt_file else None)
    return OpenAICompatExtractor(ex_url, "bench", max_tokens=4096,
                                 timeout_seconds=600.0,
                                 system_prompt=system_prompt,
                                 events_prompt=events_prompt)


def run_extract(dataset: str, limit: int | None, extractor_name: str,
                do_answer: bool, tag: str = "", window: int = 0,
                system_prompt_file: str | None = None,
                events_prompt_file: str | None = None,
                qids: str | None = None,
                types: tuple[str, ...] = DEFAULT_TYPES,
                variants: bool = False,
                refind: bool = False, nomem: bool = False,
                refind_kwargs: dict | None = None,
                rag_lite_top_ks: tuple[int, ...] = (),
                rag_budget_tokens: int | None = None) -> None:
    ex_url = EXTRACTORS[extractor_name]
    # Validated (and the bench globals moved) before a single question
    # is ingested: a width the arms cannot serve must fail here, not
    # after paying an ingest, and never half-applied.
    validate_rag_lite(rag_lite_top_ks, rag_budget_tokens, RAG_TOP_K)
    global RAG_LITE_TOP_KS, RAG_BUDGET_TOKENS
    RAG_LITE_TOP_KS = rag_lite_top_ks
    RAG_BUDGET_TOKENS = rag_budget_tokens
    if not probe(ex_url):
        sys.exit(f"no extractor server at {ex_url} — start it first")
    # --refind drives the ANSWERER model during the EXTRACT phase (its
    # search loop is planned by that model), so the phase split's usual
    # "extractor endpoint only" no longer holds for it. Probing here is
    # what stops a run dying mid-question after paying a full ingest and
    # writing no row to resume from.
    if (do_answer or refind) and not probe(QWEN_URL):
        why = "" if do_answer else " (the --refind search loop plans on it)"
        sys.exit(f"no answer/judge server at {QWEN_URL}{why} "
                 "— start it first")
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
        extractor = _make_extractor(ex_url, system_prompt_file,
                                    events_prompt_file)
        tally = ingest_and_dream(svc, extractor, q, ex_url)
        contexts = build_contexts(svc, q["question"], variants=variants)
        # Comparator arms (off by default): the ReFind loop searches the
        # same haystack turns the bank just ingested; the no-memory arm is
        # served nothing. Both contexts are PERSISTED like every other
        # arm, so the answer phase (and a later rebuild_contexts re-answer)
        # replays them without re-paying extraction.
        refind_trace = serve_comparator_arms(
            contexts, q["question"],
            archive=archive_from_lme_question(q) if refind else None,
            refind=refind, nomem=nomem, refind_kwargs=refind_kwargs)
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
            # SR-TTT guard (arXiv 2603.06642): a question that already
            # names its gold answer scores on every arm and measures no
            # retrieval. leak_check.py reads this to report each arm's
            # mean with those rows excluded; None = gold too short or
            # generic to test.
            "gold_in_question": leak_check.answer_present(q["question"],
                                                          q["answer"]),
            # Self-describing rows: only ReFind runs carry these, so every
            # legacy artifact keeps its exact shape.
            **({"refind_trace": refind_trace,
                "refind_top_k": refind_trace["top_k"],
                "refind_rounds": refind_trace["rounds_budget"]}
               if refind_trace is not None else {}),
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
               # Experiment-knob state at report time: a summary that can't
               # say which config produced it is unauditable afterwards.
               "bench_env": bench_env_knobs(),
               "arms": {}}
    # rag_lite_rebuild.py --limit stamps partial=true on every row it
    # writes. Carry it into the summary: the rows said so, but a summary
    # reading "n": 5 beside normal-looking means is exactly what a reader
    # mistakes for a complete run of 5 questions.
    if any(r.get("partial") for r in rows):
        summary["partial"] = True
        print(f"PARTIAL run: {n} rows carry partial=true — a limited "
              "rebuild, not a complete run")
    # Variant arms (hybrid_ctg etc.) are detected from the rows so old
    # three-arm artifacts report identically.
    extra_arms = tuple(sorted(
        {k.removesuffix("_correct") for k in rows[0] if k.endswith("_correct")}
        - set(ARMS)))
    # Rows must agree on their arms. A file resumed with different arm
    # flags than it started with would otherwise either KeyError below or
    # (if arms were intersected) quietly drop an arm from the table.
    for r in rows:
        missing = [a for a in extra_arms if f"{a}_correct" not in r]
        if missing:
            sys.exit(
                f"{out_path.name}: row {r.get('question_id')} is missing "
                f"the {', '.join(missing)} arm(s) that other rows carry — "
                "the file mixes runs with different arm flags. Re-run the "
                "answer phase over the whole file, or report the runs "
                "separately.")
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
        # A ragb<N> arm is named for a budget it does not always keep —
        # the always-serve-one-turn floor overshoots whenever one ranked
        # turn is already over budget, which on LongMemEval is the common
        # case. Published beside the mean so the artifact says it.
        over = budget_overshoot(rows, arm)
        if over is not None:
            summary["arms"][arm]["budget_overshoot_rows"] = over
        print(f"{arm:<10}{acc:>10.3f}{tok:>12.1f}"
              + (f"  ({over}/{n} over budget)" if over else ""))
    sup = sum(r["consolidation"]["superseded"] for r in rows)
    print(f"supersessions across runs: {sup}")
    summary["superseded_total"] = sup
    # Runs that recorded the per-row SR-TTT flag carry the leak check:
    # how many questions named their own gold answer, and every arm's
    # mean with those rows excluded. Legacy artifacts have no flag and
    # their summaries stay unchanged.
    if any(leak_check.FLAG_KEY in r for r in rows):
        summary["leak_check"] = leak_check.check_rows(rows)
        n_leaked = summary["leak_check"]["n_leaked"]
        print(f"gold-answer leaks: {n_leaked} of {n} questions"
              + (" (arm means beside them exclude these rows)"
                 if n_leaked else ""))
    # Rows with persisted contexts carry the answerability + pathway
    # cross-tab (AWM/PAST-Bench); legacy context-less artifacts report
    # unchanged. One implementation, shared with the BEAM adapter.
    ans_block = answerability_probe.report_block(rows)
    if ans_block:
        summary["answerability"] = ans_block
        for arm, a in ans_block["arms"].items():
            pw = a["pathway"]
            examined = pw["supported"] + pw["unsupported"] + pw["spanning"]
            print(f"answerability {arm}: red-flag "
                  f"{a['cells']['unanswerable_correct']}"
                  f"/{a['n_testable']} testable, pathway supported "
                  f"{pw['supported']}/{examined} of correct")
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
    ap.add_argument("--events-prompt-file", default=None,
                    help="override the events-pass system prompt from a file "
                         "(candidate prompts, e.g. events_pass_v2.txt; "
                         "requires --chronicle)")
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
    ap.add_argument("--refind", action="store_true",
                    help="add the ReFind comparator arm: an agentic "
                         "LEXICAL search loop over the same haystack turns "
                         "(temporal narrowing, skip-inspected, "
                         "session-aware fusion), budget-matched to the rag "
                         "control (arXiv 2608.12888)")
    ap.add_argument("--nomem", action="store_true",
                    help="add the no-memory control arm: the question "
                         "alone, same task framing, no context "
                         "(MemTrapBench, arXiv 2608.20202)")
    ap.add_argument("--refind-rounds", type=int,
                    default=refind_arm.DEFAULT_ROUNDS,
                    help="search rounds the ReFind agent may take")
    ap.add_argument("--refind-top-k", type=int, default=None,
                    help="raw-turn budget the ReFind arm serves (default: "
                         "RAG_TOP_K — budget-matched to the control)")
    ap.add_argument("--refind-per-round-k", type=int,
                    default=refind_arm.DEFAULT_PER_ROUND_K,
                    help="turns each ReFind query may inspect")
    ap.add_argument("--refind-max-queries", type=int,
                    default=refind_arm.DEFAULT_MAX_QUERIES,
                    help="searches the ReFind agent may issue per round")
    ap.add_argument("--refind-session-weight", type=float,
                    default=refind_arm.SESSION_FUSION_WEIGHT,
                    help="weight of the session-aware fusion term "
                         "(0 = pure lexical ranking)")
    ap.add_argument("--rag-lite-top-k", default=None,
                    help="comma-separated narrower rag budgets, e.g. "
                         "'1,2': adds arms rag1, rag2 — the rag "
                         "control's exact retrieval, ranking and "
                         "formatting truncated to the first K ranked "
                         "turns, so accuracy and tokens read as one "
                         "trade-off instead of two findings")
    ap.add_argument("--rag-budget-tokens", type=int, default=None,
                    help="adds arm ragb<N>: the rag ranking truncated "
                         "to the turns that fit N approximate tokens "
                         "(len//4), so a run can match the cortex or "
                         "cascade budget exactly rather than by turn "
                         "count")
    ap.add_argument("--ev-variants", action="store_true",
                    help="aggregation-serving variants (2026-08-06 design): "
                         "add hybrid_ev_agg (events on either cue, full "
                         "list) and hybrid_ev_syn (+ computed tally line); "
                         "requires --chronicle")
    args = ap.parse_args()
    if args.ev_variants and not args.chronicle:
        ap.error("--ev-variants requires --chronicle")
    rag_lite_top_ks = parse_rag_lite_top_ks(args.rag_lite_top_k)
    if args.phase == "answer" and (args.refind or args.nomem
                                   or rag_lite_top_ks
                                   or args.rag_budget_tokens is not None):
        # The answer phase only replays PERSISTED contexts, so these flags
        # would do nothing at all — silently, and the user would read the
        # resulting table as if the arms had run.
        ap.error("--refind/--nomem/--rag-lite-top-k/--rag-budget-tokens "
                 "build contexts, so they belong to the extract phase; "
                 "--phase answer only answers what was already "
                 "persisted")
    try:
        validate_rag_lite(rag_lite_top_ks, args.rag_budget_tokens,
                          RAG_TOP_K)
    except SystemExit as e:                       # argparse-shaped usage
        ap.error(str(e))
    if args.refind_top_k is not None and args.refind_top_k < 1:
        ap.error("--refind-top-k must be positive")
    for _flag, _value in (("--refind-rounds", args.refind_rounds),
                          ("--refind-per-round-k", args.refind_per_round_k),
                          ("--refind-max-queries", args.refind_max_queries)):
        if _value < 1:
            ap.error(f"{_flag} must be positive")
    if not 0.0 <= args.refind_session_weight <= 1.0:
        ap.error("--refind-session-weight must be in [0, 1]")
    refind_kwargs = {"rounds": args.refind_rounds,
                     "top_k": args.refind_top_k,
                     "per_round_k": args.refind_per_round_k,
                     "max_queries": args.refind_max_queries,
                     "session_weight": args.refind_session_weight}
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
        if args.events_prompt_file and not args.chronicle:
            ap.error("--events-prompt-file requires --chronicle")
        run_extract(args.dataset, args.limit, args.extractor,
                    do_answer=(args.phase == "full"), tag=args.tag,
                    window=args.window,
                    system_prompt_file=args.system_prompt_file,
                    events_prompt_file=args.events_prompt_file,
                    qids=args.qids, types=types, variants=args.variants,
                    refind=args.refind, nomem=args.nomem,
                    refind_kwargs=refind_kwargs,
                    rag_lite_top_ks=rag_lite_top_ks,
                    rag_budget_tokens=args.rag_budget_tokens)
    if args.phase != "extract":
        report(args.dataset, args.extractor, args.tag, types)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
