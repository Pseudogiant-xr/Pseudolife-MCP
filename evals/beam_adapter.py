"""BEAM adapter — run Pseudolife against the BEAM long-term-memory benchmark.

BEAM (arXiv 2510.27246, ICLR 2026; MIT) probes ten memory abilities over
procedurally generated conversations at 100K-10M tokens, scored by an LLM
judge over per-question rubric items (design doc:
docs/superpowers/specs/2026-08-02-beam-adapter-design.md).

This adapter is path A from that design: everything local and reproducible.
Each chat is ingested turn-by-turn into a fresh bench service (dream after
every BEAM batch — the production cadence), each probing question is
answered through the three LME arms (rag / cortex / hybrid; rag doubles as
the extraction-independent control), and every arm response is judged with
BEAM's own ``unified_llm_judge_base_prompt`` — extracted from the harness
clone via ``ast`` at runtime, never vendored — item by item.

Scoring note (recorded in the artifact): the BEAM paper defines a
1.0/0.5/0.0 per-item scale but the reference code applies ``int()`` to the
judge's score, flooring 0.5 to 0. Both readings are recorded per item
(``score`` = paper-faithful float, ``score_int`` = code-faithful); summary
headline uses the paper-faithful mean, with the code-faithful mean beside
it.

The BEAM checkout (data + prompts) stays OUTSIDE this repo:

    PYTHONPATH=. python evals/beam_adapter.py --beam-root <path-to-BEAM> \
        --tier 100K --extractor qwen-27b --out-tag beam100k-qwen

Writes ``evals/results/beam-<tier>-<extractor>-<tag>.jsonl`` (resumable
per question) + a ``.summary.json`` from ``--report``.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from ladder_sweep import build_service, probe  # noqa: E402
import answerability_probe  # noqa: E402
import leak_check  # noqa: E402
import longmemeval_bench as lme  # noqa: E402
import nomem_arm  # noqa: E402
import refind_arm  # noqa: E402
from longmemeval_bench import (  # noqa: E402
    _chat, approx_tokens, ARMS, EXTRACTORS, QWEN_URL, RESULTS_DIR,
    _make_extractor, build_contexts, load_rows,
)


def arms_for(chronicle: bool, only: str | None = None,
             digest: bool = False, refind: bool = False,
             nomem: bool = False,
             rag_lite: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The answered/judged arms: --chronicle adds hybrid_ev (vanilla
    hybrid + the served events block, same pinned search call — the LME
    ev2 arm contract); --digest adds hybrid_digest (spec 2026-08-24: the
    digest-eligible mem block, char-budget-matched to hybrid); --refind
    adds the agentic-lexical arm and --nomem the memory-off control (both
    2026-09-01, see refind_arm.py / nomem_arm.py). ``only``
    (--arms) keeps a comma-separated subset in canonical order — a
    partial rerun (e.g. rag,hybrid for the budget-matched arm, with rag
    as the identical-input control) skips arms whose answers the question
    at hand does not need."""
    arms = (*ARMS, "hybrid_ev") if chronicle else ARMS
    if digest:
        arms = (*arms, "hybrid_digest")
    if refind:
        arms = (*arms, "refind")
    if nomem:
        arms = (*arms, "nomem")
    # Token-matched rag budgets (2026-09-04): named by their width, so
    # the names come from the shared minter rather than a literal here
    # — LongMemEval discovers the same arms from the persisted
    # contexts, and a name minted twice is a name that can differ.
    arms = (*arms, *rag_lite)
    if only:
        keep = {a.strip() for a in only.split(",") if a.strip()}
        unknown = keep - set(arms)
        if unknown:
            raise SystemExit(
                f"--arms names {sorted(unknown)} not in {arms} "
                "(hybrid_ev needs --chronicle; hybrid_digest needs "
                "--digest; refind needs --refind; nomem needs --nomem; "
                "ragK/ragbN need --rag-lite-top-k/--rag-budget-tokens)")
        arms = tuple(a for a in arms if a in keep)
    return arms

# BEAM answers are rubric-judged per nugget, and several abilities
# (summarization, event ordering, instruction following) need multi-part
# answers — the LME answerer's one-short-sentence cap structurally zeroes
# them (measured on the first smoke). Same abstention contract, no length
# cap.
_BEAM_ANSWER_SYSTEM = (
    "You answer questions about a long-running conversation from its "
    "memory context. Use ONLY the provided context. When the context shows "
    "a fact was updated, use the most CURRENT value unless the question "
    "asks about an earlier state. When the context contains genuinely "
    "CONTRADICTORY claims — statements that conflict about whether "
    "something happened or is true, not a value that was simply updated — "
    "say so explicitly and present both sides instead of silently picking "
    "one. Answer completely — include every part the question asks for; "
    "lists and multi-step answers are fine. If the context does not "
    "contain the information, say exactly: I don't know."
)

TIERS = ("100K", "500K", "1M", "10M")


def answer_call(arm: str, question: str, ctx: str) -> tuple[str, str]:
    """The (system, prompt) pair for one arm's answer call.

    Every memory arm shares the BEAM answerer and the same context block.
    The no-memory arm shares the task framing and drops the context
    clauses entirely — not an "(empty)" block, which is itself a framing
    the other arms do not see (see nomem_arm.py)."""
    if arm == "nomem":
        # This harness's length policy (rubric judge, multi-part answers).
        return (nomem_arm.nomem_system(nomem_arm.LENGTH_COMPLETE),
                nomem_arm.nomem_prompt(question))
    return _BEAM_ANSWER_SYSTEM, (f"Question: {question}\n\n"
                                 f"Memory context:\n{ctx or '(empty)'}")


def serve_contexts(svc, question: str, arms: tuple[str, ...], *,
                   archive=None, refind_kwargs: dict | None = None,
                   chat=None) -> tuple[dict, dict, dict | None]:
    """Every arm's served context for one question, plus the structured
    serve state and the ReFind trace (None when that arm is not running).

    The memory arms come from the bank; the ReFind arm runs its own loop
    over the raw archive; the no-memory arm is served nothing at all —
    its empty context is recorded so the artifact shows it was empty
    (leak_check.py checks exactly that)."""
    contexts = build_contexts(svc, question, with_parts=True)
    parts = contexts.pop("parts")
    # One implementation, both harnesses (lme.serve_comparator_arms):
    # budget matching, the empty no-memory context and the missing-archive
    # guard are decided in exactly one place, so BEAM and LongMemEval
    # cannot drift into serving these arms differently.
    trace = lme.serve_comparator_arms(
        contexts, question, archive=archive, refind="refind" in arms,
        nomem="nomem" in arms, refind_kwargs=refind_kwargs, chat=chat)
    return contexts, parts, trace


def archive_from_beam_turns(turns: list[dict]) -> refind_arm.LexicalArchive:
    """The ReFind arm's lexical archive, built from the SAME formatted
    turn texts and ordinals that were stored into the bank — so a
    refind-vs-rag delta is about the retrieval loop over identical
    material, never about a different corpus."""
    return refind_arm.LexicalArchive(
        refind_arm.ArchiveRecord(
            text=format_turn(turn, i), session=str(turn["batch"]), ordinal=i,
            date=refind_arm.parse_anchor(turn.get("time_anchor")))
        for i, turn in enumerate(turns, 1))


def load_judge_prompt(beam_root: Path) -> str:
    """Extract ``unified_llm_judge_base_prompt`` from the BEAM checkout's
    ``src/prompts.py`` without importing it (11k lines of templates; an
    ``ast`` walk is side-effect-free and pins us to the exact upstream
    text)."""
    tree = ast.parse((beam_root / "src" / "prompts.py").read_text(
        encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) ==
                        "unified_llm_judge_base_prompt"
                        for t in node.targets)
                and isinstance(node.value, ast.Constant)):
            return node.value.value
    raise SystemExit("unified_llm_judge_base_prompt not found in the BEAM "
                     "checkout — wrong --beam-root or upstream layout change")


def iter_chats(beam_root: Path, tier: str) -> list[tuple[str, Path]]:
    tier_dir = beam_root / "chats" / tier
    if not tier_dir.is_dir():
        raise SystemExit(f"no such tier dir: {tier_dir}")
    return sorted(((p.name, p) for p in tier_dir.iterdir() if p.is_dir()),
                  key=lambda t: int(t[0]))


def load_chat_turns(chat_dir: Path) -> list[dict]:
    """Flatten a chat's batches into (batch_number, time_anchor, role,
    content) turns, preserving order."""
    batches = json.loads((chat_dir / "chat.json").read_text(encoding="utf-8"))
    out = []
    for batch in batches:
        for group in batch["turns"]:
            # A BEAM "turn" is a LIST of message dicts (user/assistant
            # exchange); tolerate a bare dict for robustness.
            messages = group if isinstance(group, list) else [group]
            for turn in messages:
                content = (turn.get("content") or "").strip()
                if not content:
                    continue
                out.append({
                    "batch": batch["batch_number"],
                    "time_anchor": (turn.get("time_anchor")
                                    or batch.get("time_anchor")),
                    "role": turn.get("role", "user"),
                    "content": content,
                })
    return out


def load_questions(chat_dir: Path) -> list[dict]:
    data = json.loads(
        (chat_dir / "probing_questions" / "probing_questions.json")
        .read_text(encoding="utf-8"))
    out = []
    for qtype, questions in sorted(data.items()):
        for idx, q in enumerate(questions):
            out.append({"type": qtype, "index": idx,
                        "question": q["question"],
                        "answer": q.get("answer", ""),
                        "difficulty": q.get("difficulty"),
                        "rubric": q.get("rubric") or []})
    return out


_SCORE_RE = re.compile(r'"score"\s*:\s*"?([0-9.]+)"?')


def parse_judge_score(raw: str) -> float | None:
    """The judge answers JSON with a ``score`` field (1.0 / 0.5 / 0.0).
    Strip code fences, parse JSON, fall back to a regex — mirrors the
    upstream ``parse_json_response`` + ``repair_json`` tolerance without
    the dependency."""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return float(json.loads(text)["score"])
    except Exception:  # noqa: BLE001 — fall through to the regex
        m = _SCORE_RE.search(text)
        return float(m.group(1)) if m else None


def judge_response(judge_prompt: str, question: str, rubric: list[str],
                   response: str, chat=None) -> dict:
    """BEAM's per-item rubric judging: mean over items. Records the
    paper-faithful float and the code-faithful int per item. ``chat``
    swaps the transport (beam_rejudge.py injects a frontier CLI judge);
    scoring and failure semantics stay identical."""
    chat = chat or _chat
    items = []
    for item in rubric:
        prompt = (judge_prompt
                  .replace("<question>", question)
                  .replace("<rubric_item>", item)
                  .replace("<llm_response>", response or "(empty)"))
        raw = chat("", prompt, max_tokens=512)
        score = parse_judge_score(raw)
        items.append({"rubric_item": item, "score": score,
                      "score_int": None if score is None else int(score)})
    scored = [i for i in items if i["score"] is not None]
    n = max(len(scored), 1)
    return {
        "llm_judge_score": round(sum(i["score"] for i in scored) / n, 4),
        "llm_judge_score_intfaithful": round(
            sum(i["score_int"] for i in scored) / n, 4),
        "judge_failures": len(items) - len(scored),
        "items": items,
    }


def answer_arm(row: dict, arm: str, q: dict, ctx: str,
               judge_prompt: str, chat=None) -> dict:
    """Answer and judge ONE arm, filling every per-arm field on the row.

    Lifted out of the run loop so the recorded fields are testable
    without a GPU: the row shape is what every downstream reader
    (report, leak_check, answerability_probe, beam_within_run_pairs)
    keys off, and it had no pin at all before the token column was
    added.
    """
    chat = chat or _chat
    row.setdefault("contexts", {})[arm] = ctx
    system, prompt = answer_call(arm, q["question"], ctx)
    response = chat(system, prompt, max_tokens=1024)
    verdict = judge_response(judge_prompt, q["question"], q["rubric"],
                             response, chat=chat)
    row[f"{arm}_response"] = response
    # Served-context size in the harness's approximate tokens (len//4,
    # the LongMemEval convention). BEAM recorded characters only until
    # 2026-09-04, which left every accuracy-vs-cost read on this
    # benchmark to be eyeballed across two separate artifacts.
    row[f"{arm}_context_tokens"] = approx_tokens(ctx)
    row[f"{arm}_score"] = verdict["llm_judge_score"]
    row[f"{arm}_score_intfaithful"] = verdict["llm_judge_score_intfaithful"]
    row[f"{arm}_judge"] = verdict["items"]
    row[f"{arm}_judge_failures"] = verdict["judge_failures"]
    return row


def _dream_until_drained(svc, extractor, tally: dict) -> None:
    """One dream_run consumes a capped pull; a BEAM batch holds far more
    turns than one pull (first smoke: 3 dreams left most of 188 turns
    unconsolidated). Drain the backlog like the daemon's repeated sweep
    ticks would."""
    while True:
        r = svc.dream_run(extractor)
        tally["dreams"] += 1
        tally["claims"] += r.get("claims", 0)
        tally["superseded"] += r.get("superseded", 0)
        tally["literal_dropped"] += r.get("literal_dropped", 0)
        tally["events_inserted"] += r.get("events_inserted", 0)
        tally["events_pass_failures"] += int(bool(
            r.get("events_pass_failed")))
        if r.get("pulled", 0) == 0 or svc.dream_status().get("backlog", 0) == 0:
            return


def format_turn(turn: dict, ordinal: int) -> str:
    """One stored turn, with ordering metadata riding the text: session
    (the BEAM batch) and a per-chat turn ordinal. Cognee's retrieved
    passages carry literal Session:/Turn: headers and their reader gets
    ordering for free; ours discarded it at ingest — the 2026-08-22
    reader-sweep verdict left event_ordering weakest at every context
    budget. Banks stored before this stamp are not byte-comparable."""
    anchor = f"[{turn['time_anchor']}] " if turn["time_anchor"] else ""
    return (f"{anchor}[session {turn['batch']}, turn {ordinal}] "
            f"{turn['role']}: {turn['content']}")


def ingest_chat(svc, extractor, turns: list[dict],
                digest: bool = False) -> dict:
    """Store every turn; drain the dream backlog at each BEAM batch
    boundary (the production between-sessions cadence) and at the end.

    ``digest`` (spec 2026-08-24): each BEAM batch becomes one session
    episode — the digest scope matches the benchmark's session structure —
    and after the final drain the digest backlog is drained by calling the
    stage directly (the bench extractor is built directly, not via the
    PSEUDOLIFE_DREAM_* config, so dream_status's endpoint-gated pending
    count reads 0 here and cannot drive the drain). Episode header dates
    are ingest-time wall clock; the chat's own time anchors ride the turn
    texts inside the digest body."""
    tally = {"turns": 0, "stored": 0, "dreams": 0, "claims": 0,
             "superseded": 0, "literal_dropped": 0, "events_inserted": 0,
             "events_pass_failures": 0}
    current_batch = None
    for i, turn in enumerate(turns, 1):
        if current_batch is not None and turn["batch"] != current_batch:
            if digest:
                svc.episode_end_session(f"beam-{current_batch}",
                                        run_dream=False)
            _dream_until_drained(svc, extractor, tally)
        if digest and turn["batch"] != current_batch:
            svc.episode_start_session(f"beam-{turn['batch']}",
                                      f"session {turn['batch']}")
        current_batch = turn["batch"]
        # ``stored`` vs ``turns``: the ReFind arm indexes every turn while
        # the bank holds only what store() accepted. The bench service
        # disables the meta filter and sets surprise_threshold 0
        # (MemoryService._apply_mcp_defaults), so the two corpora should
        # match exactly — recorded rather than assumed, so a config that
        # re-enables either filter shows up in the artifact instead of
        # quietly making the refind-vs-rag comparison unfair.
        result = svc.store(format_turn(turn, i), source="beam")
        tally["turns"] += 1
        tally["stored"] += int(
            result.get("stored", True) if isinstance(result, dict) else True)
    if digest and current_batch is not None:
        svc.episode_end_session(f"beam-{current_batch}", run_dream=False)
    _dream_until_drained(svc, extractor, tally)
    if digest:
        tally["digests"] = 0
        while True:
            d = svc.generate_digests_stage(extractor)
            tally["digests"] += d.get("written", 0)
            if d.get("scanned", 0) == 0:
                break
    return tally


def out_file(tier: str, extractor: str, tag: str) -> Path:
    return RESULTS_DIR / f"beam-{tier}-{extractor}-{tag}.jsonl"


def beam_bank_dir(tier: str, extractor: str, tag: str) -> Path:
    return RESULTS_DIR / "banks" / f"beam-{tier}-{extractor}-{tag}"


def dump_chat_bank(svc, chat_id: str, tally: dict, path: Path) -> None:
    """Persist a chat's consolidated fact bank (with per-slot history
    chains), the LME dump_bank pattern per chat: fact embeddings are
    encode_single over "entity attribute value" and cortex search is
    cosine over them, so fact retrieval replays offline exactly. Turns are
    NOT dumped — they come verbatim from the static BEAM chat.json, and
    re-embedding them is CPU-cheap. Chronicle event serving is the one
    channel a dump cannot replay (it rides the live search call)."""
    facts = svc.cortex_dump().get("entries", [])
    for f in facts:
        f.pop("source_entries", None)             # bulky, not needed offline
        # Same treatment as the LME dump: read-time annotations over those
        # traces are not part of the offline replay and would churn the
        # committed bank artifacts.
        f.pop("re_verify", None)
        f.pop("re_verify_reason", None)
        try:
            versions = svc.history(f.get("entity", ""),
                                   f.get("attribute", "")).get("versions", [])
            f["history"] = [v.get("value") for v in versions]  # oldest→newest
        except Exception:  # noqa: BLE001 — history is garnish, never fatal
            f["history"] = [f.get("value")]
    payload = {"chat_id": chat_id, "consolidation": tally, "facts": facts}
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def run(beam_root: Path, tier: str, extractor_name: str, tag: str,
        chats: str | None, limit_chats: int | None,
        chronicle: bool = False, arms_only: str | None = None,
        hybrid_top_k: int | None = None,
        rag_top_k: int | None = None,
        digest: bool = False,
        refind: bool = False, nomem: bool = False,
        refind_rounds: int = refind_arm.DEFAULT_ROUNDS,
        refind_top_k: int | None = None,
        refind_per_round_k: int = refind_arm.DEFAULT_PER_ROUND_K,
        refind_session_weight: float = refind_arm.SESSION_FUSION_WEIGHT,
        refind_max_queries: int = refind_arm.DEFAULT_MAX_QUERIES,
        rag_lite_top_ks: tuple[int, ...] = (),
        rag_budget_tokens: int | None = None
        ) -> None:
    # Validate BOTH budget knobs before mutating EITHER module global:
    # build_contexts slices the hybrid turns from a top_k=RAG_TOP_K
    # search, so a hybrid budget wider than the effective rag width would
    # be silently capped while every row records the wider number — an
    # artifact asserting a budget that was never served. A validation
    # failure must also not leave a half-applied global behind.
    effective_rag = rag_top_k if rag_top_k is not None else lme.RAG_TOP_K
    if rag_top_k is not None and rag_top_k < 1:
        raise SystemExit("--rag-top-k must be positive")
    # Same rule for the ReFind knobs: validated before any global moves,
    # so a bad flag cannot leave a half-applied bench config behind.
    if refind_top_k is not None and refind_top_k < 1:
        raise SystemExit("--refind-top-k must be positive")
    if refind_rounds < 1:
        raise SystemExit("--refind-rounds must be positive")
    if refind_per_round_k < 1:
        raise SystemExit("--refind-per-round-k must be positive")
    if refind_max_queries < 1:
        raise SystemExit("--refind-max-queries must be positive")
    if not 0.0 <= refind_session_weight <= 1.0:
        raise SystemExit("--refind-session-weight must be in [0, 1]")
    # Same rule, same single implementation as the LongMemEval CLI: a
    # rag-lite width is checked against the EFFECTIVE rag width (after
    # --rag-top-k), because an arm as wide as the control serves a copy
    # of the control under another name and costs a full judged pass.
    lme.validate_rag_lite(rag_lite_top_ks, rag_budget_tokens,
                          effective_rag)
    if hybrid_top_k is not None:
        if hybrid_top_k < 1:
            raise SystemExit("--hybrid-top-k must be positive")
        if hybrid_top_k > effective_rag:
            raise SystemExit(
                f"--hybrid-top-k {hybrid_top_k} exceeds the effective "
                f"rag width {effective_rag}; the hybrid arm cannot serve "
                "more turns than the search returns")
    # Only now that EVERY flag is validated do the bench globals move.
    # These two used to be assigned first, which left a half-applied bench
    # config behind on any bad flag (2026-09-01 review).
    lme.CHRONICLE = chronicle          # build_contexts reads its module global
    lme.DIGEST_ARM = digest            # same module-global contract
    if rag_top_k is not None:
        # Both arms widen together (the pinned search serves rag AND the
        # hybrid raw block), so budget-matching survives the knob. The
        # 2026-08-23 reader-sweep verdict: naive rag at 16 turns is
        # +0.084 over 6 under a frontier reader; this knob measures the
        # same curve on the local stack for free on the GPU.
        lme.RAG_TOP_K = rag_top_k
    if hybrid_top_k is not None:
        # build_contexts reads the module global at call time (pinned by
        # test_hybrid_top_k_is_read_at_call_time). Default None keeps every
        # prior artifact's hybrid budget byte-identical.
        lme.HYBRID_TOP_K = hybrid_top_k
    # Same call-time contract: empty/None serves no rag-lite arm and
    # adds no context key, so a vanilla run stays byte-identical.
    lme.RAG_LITE_TOP_KS = rag_lite_top_ks
    lme.RAG_BUDGET_TOKENS = rag_budget_tokens
    rag_lite = lme.rag_lite_arm_names(rag_lite_top_ks, rag_budget_tokens)
    arms = arms_for(chronicle, arms_only, digest, refind=refind,
                    nomem=nomem, rag_lite=rag_lite)
    ex_url = EXTRACTORS[extractor_name]
    if not probe(ex_url):
        sys.exit(f"no extractor server at {ex_url} — start it first")
    if not probe(QWEN_URL):
        sys.exit(f"no answer/judge server at {QWEN_URL} — start it first")
    judge_prompt = load_judge_prompt(beam_root)
    all_chats = iter_chats(beam_root, tier)
    if chats:
        keep = {c.strip() for c in chats.split(",")}
        all_chats = [c for c in all_chats if c[0] in keep]
    if limit_chats:
        all_chats = all_chats[:limit_chats]
    out_path = out_file(tier, extractor_name, tag)
    done = {(r["chat_id"], r["type"], r["index"])
            for r in load_rows(out_path)}
    ingested: dict[str, tuple] = {}
    print(f"BEAM {tier}: {len(all_chats)} chats, extractor={extractor_name} "
          f"({len(done)} question-rows already done)", flush=True)

    for chat_id, chat_dir in all_chats:
        questions = load_questions(chat_dir)
        pending = [q for q in questions
                   if (chat_id, q["type"], q["index"]) not in done]
        if not pending:
            continue
        t0 = time.perf_counter()
        chat_turns = load_chat_turns(chat_dir)
        archive = (archive_from_beam_turns(chat_turns)
                   if "refind" in arms else None)
        tmp = Path(tempfile.mkdtemp(prefix="beam_"))
        svc = build_service(tmp)
        svc.config.memory.dream.extract_relations = False
        svc.config.memory.dream.chronicle = chronicle
        svc.config.memory.dream.digest_enabled = digest
        extractor = _make_extractor(ex_url, None)
        tally = ingest_chat(svc, extractor, chat_turns, digest=digest)
        # Per-chat over-fetch width for build_contexts: the exact digest
        # population of THIS bank (see lme.DIGEST_COUNT for the why).
        lme.DIGEST_COUNT = tally.get("digests", 0) if digest else 0
        ingest_s = round(time.perf_counter() - t0, 1)
        print(f"chat {chat_id}: ingested {tally['turns']} turns, "
              f"{tally['dreams']} dreams, {tally['claims']} claims "
              f"({ingest_s}s)", flush=True)
        dump_chat_bank(svc, chat_id, tally,
                       beam_bank_dir(tier, extractor_name, tag)
                       / f"chat{chat_id}.json.gz")
        for q in pending:
            t1 = time.perf_counter()
            contexts, parts, refind_trace = serve_contexts(
                svc, q["question"], arms, archive=archive, refind_kwargs={
                    "rounds": refind_rounds, "top_k": refind_top_k,
                    "per_round_k": refind_per_round_k,
                    "max_queries": refind_max_queries,
                    "session_weight": refind_session_weight})
            row = {"chat_id": chat_id, "tier": tier, "type": q["type"],
                   "index": q["index"], "question": q["question"],
                   "reference_answer": q["answer"],
                   "difficulty": q["difficulty"], "rubric": q["rubric"],
                   "extractor": extractor_name,
                   "rag_top_k": lme.RAG_TOP_K,
                   "hybrid_top_k": lme.HYBRID_TOP_K,
                   # Self-describing rows: only digest runs carry the key,
                   # so legacy artifacts stay byte-shape-identical.
                   **({"digest_arm": True} if digest else {}),
                   # The budget comes off the TRACE, not the flag: the row
                   # records the width the arm actually served.
                   **({"refind_trace": refind_trace,
                       "refind_top_k": refind_trace["top_k"],
                       "refind_rounds": refind_rounds}
                      if refind_trace is not None else {}),
                   # SR-TTT guard (arXiv 2603.06642): a question that
                   # already names its gold answer scores on every arm and
                   # measures no retrieval. Recorded per row at answer
                   # time; leak_check.py reports the arm means with these
                   # rows excluded. None = the gold is too short/generic
                   # to test.
                   "gold_in_question": leak_check.answer_present(
                       q["question"], q["answer"]),
                   "consolidation": tally, "ingest_seconds": ingest_s,
                   # Served contexts + structured serve state (2026-08-21):
                   # serving-knob reruns and re-judges recompose from these
                   # instead of re-paying the ingest/extraction phase.
                   "contexts": {}, "parts": parts}
            for arm in arms:
                answer_arm(row, arm, q, contexts.get(arm, ""),
                           judge_prompt)
            row["wall_seconds"] = round(time.perf_counter() - t1, 1)
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  {chat_id}/{q['type']}[{q['index']}] " + " ".join(
                f"{arm}={row[f'{arm}_score']:.2f}" for arm in arms),
                flush=True)
        svc.flush()
        ingested[chat_id] = ()


def row_context_tokens(row: dict, arm: str) -> int | None:
    """One arm's served-context size in approximate tokens.

    Recorded per row since 2026-09-04. Rows written before it are
    re-estimated from the persisted context with the same len//4
    convention, so a legacy artifact still reports the column instead
    of a hole; None when the row kept no context for the arm at all
    (the pre-2026-08-21 context-less runs).
    """
    if f"{arm}_context_tokens" in row:
        return int(row[f"{arm}_context_tokens"])
    ctx = (row.get("contexts") or {}).get(arm)
    if ctx is None:
        return None
    return approx_tokens(ctx if isinstance(ctx, str) else str(ctx))


def mean_context_tokens(rows: list[dict], arm: str) -> int | None:
    """Mean served tokens over the rows that carry the arm's context."""
    seen = [t for t in (row_context_tokens(r, arm) for r in rows)
            if t is not None]
    return round(sum(seen) / len(seen)) if seen else None


def report(tier: str, extractor_name: str, tag: str) -> None:
    out_path = out_file(tier, extractor_name, tag)
    rows = load_rows(out_path)
    if not rows:
        sys.exit(f"no results in {out_path}")
    # Arms come off the rows, not a static tuple: a --chronicle run's
    # summary carries hybrid_ev, a vanilla run's does not.
    arms = [a for a in arms_for(True, digest=True, refind=True, nomem=True)
            if f"{a}_score" in rows[0]]
    # Arms minted by a knob rather than a flag (the rag-lite budgets,
    # whose names carry their width) are not in that static tuple, so
    # they come off the row — otherwise a run reports fewer arms than
    # it answered and the omission is invisible.
    arms += sorted({k.removesuffix("_score") for k in rows[0]
                    if k.endswith("_score")} - set(arms))
    summary = {"benchmark": "BEAM", "tier": tier, "extractor": extractor_name,
               "n_questions": len(rows),
               "n_chats": len({r["chat_id"] for r in rows}),
               "scoring_note": ("paper-faithful float mean; _intfaithful "
                                "mirrors upstream int() flooring of 0.5"),
               "arms": {}, "types": {}}
    # Self-describing artifacts: rows written since the --hybrid-top-k flag
    # carry the effective budget; legacy artifacts (no key) stay untouched.
    if "hybrid_top_k" in rows[0]:
        summary["hybrid_top_k"] = rows[0]["hybrid_top_k"]
    if "rag_top_k" in rows[0]:
        summary["rag_top_k"] = rows[0]["rag_top_k"]
    if "refind_top_k" in rows[0]:
        summary["refind_top_k"] = rows[0]["refind_top_k"]
        summary["refind_rounds"] = rows[0].get("refind_rounds")
    # Runs that recorded the per-row SR-TTT flag carry the leak check in
    # their summary: how many rows named their own gold answer, and every
    # arm's mean with those rows excluded. Legacy artifacts have no flag
    # and their summaries stay unchanged.
    if any(leak_check.FLAG_KEY in r for r in rows):
        summary["leak_check"] = leak_check.check_rows(rows)
    # Rows with persisted contexts carry the answerability + pathway
    # cross-tab (AWM/PAST-Bench); legacy context-less artifacts (e.g. the
    # 2026-08-21 qwen38 run) report unchanged. Same implementation the
    # LongMemEval report uses.
    ans_block = answerability_probe.report_block(rows)
    if ans_block:
        summary["answerability"] = ans_block
    for arm in arms:
        summary["arms"][arm] = {
            "score": round(sum(r[f"{arm}_score"] for r in rows)
                           / len(rows), 4),
            "score_intfaithful": round(
                sum(r[f"{arm}_score_intfaithful"] for r in rows)
                / len(rows), 4),
            # Accuracy and cost in one table: the review's finding was
            # that BEAM published them as two findings when they are
            # one trade-off. None only when no row carries a context.
            "context_tokens": mean_context_tokens(rows, arm),
        }
        # Same column the LongMemEval summary carries, from the same
        # implementation: how often a ragb<N> arm served ABOVE the budget
        # its name quotes (the always-serve-one-turn floor). None — and
        # so absent — for every arm that has no budget to miss.
        over = lme.budget_overshoot(rows, arm)
        if over is not None:
            summary["arms"][arm]["budget_overshoot_rows"] = over
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    for qtype, trows in sorted(by_type.items()):
        summary["types"][qtype] = {
            "n": len(trows),
            **{arm: round(sum(r[f"{arm}_score"] for r in trows)
                          / len(trows), 4) for arm in arms},
            # Nested rather than flattened: an ability whose questions
            # are long carries a different cost from the run mean, and
            # a flat {arm}_context_tokens key here would collide with
            # the score columns above.
            "context_tokens": {arm: mean_context_tokens(trows, arm)
                                for arm in arms},
        }
    out_path.with_name(
        out_path.name.removesuffix(".jsonl") + ".summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--beam-root", required=True,
                    help="path to the BEAM checkout (data + prompts; "
                         "never committed here)")
    ap.add_argument("--tier", choices=TIERS, default="100K")
    ap.add_argument("--extractor", choices=list(EXTRACTORS),
                    default="qwen-27b")
    ap.add_argument("--out-tag", required=True)
    ap.add_argument("--chats", default=None,
                    help="comma-separated chat ids (default: all in tier)")
    ap.add_argument("--limit-chats", type=int, default=None)
    ap.add_argument("--chronicle", action="store_true",
                    help="enable chronicle event extraction on the bench "
                         "service and answer/judge the hybrid_ev arm "
                         "(hybrid + served events block)")
    ap.add_argument("--arms", default=None,
                    help="comma-separated subset of arms to answer/judge "
                         "(default: all)")
    ap.add_argument("--hybrid-top-k", type=int, default=None,
                    help="raw-turn budget for the hybrid arm (default: the "
                         "bench's HYBRID_TOP_K; must not exceed the "
                         "effective rag width)")
    ap.add_argument("--rag-top-k", type=int, default=None,
                    help="raw-turn budget for the rag arm and the hybrid "
                         "raw block's search (default: the bench's "
                         "RAG_TOP_K)")
    ap.add_argument("--digest", action="store_true",
                    help="enable session digests on the bench service "
                         "(one episode per BEAM batch, digested at ingest) "
                         "and answer/judge the hybrid_digest arm — "
                         "char-budget-matched to hybrid (spec 2026-08-24)")
    ap.add_argument("--refind", action="store_true",
                    help="answer/judge the ReFind arm: an agentic LEXICAL "
                         "search loop over the same raw turns (temporal "
                         "narrowing, skip-inspected, session-aware "
                         "fusion), budget-matched to the rag control "
                         "(ReFind, arXiv 2608.12888)")
    ap.add_argument("--nomem", action="store_true",
                    help="answer/judge the no-memory control arm: the "
                         "question alone, same task framing, no context "
                         "(MemTrapBench, arXiv 2608.20202)")
    ap.add_argument("--refind-rounds", type=int,
                    default=refind_arm.DEFAULT_ROUNDS,
                    help="search rounds the ReFind agent may take")
    ap.add_argument("--refind-top-k", type=int, default=None,
                    help="raw-turn budget the ReFind arm serves (default: "
                         "the effective rag width — budget-matched)")
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
                         "turns (token-matched comparator, 2026-09-04)")
    ap.add_argument("--rag-budget-tokens", type=int, default=None,
                    help="adds arm ragb<N>: the rag ranking truncated "
                         "to the turns that fit N approximate tokens "
                         "(len//4), matching a fact-spine budget "
                         "exactly rather than by turn count")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        report(args.tier, args.extractor, args.out_tag)
        return 0
    run(Path(args.beam_root), args.tier, args.extractor, args.out_tag,
        args.chats, args.limit_chats, chronicle=args.chronicle,
        arms_only=args.arms, hybrid_top_k=args.hybrid_top_k,
        rag_top_k=args.rag_top_k, digest=args.digest,
        refind=args.refind, nomem=args.nomem,
        refind_rounds=args.refind_rounds, refind_top_k=args.refind_top_k,
        refind_per_round_k=args.refind_per_round_k,
        refind_max_queries=args.refind_max_queries,
        refind_session_weight=args.refind_session_weight,
        rag_lite_top_ks=lme.parse_rag_lite_top_ks(args.rag_lite_top_k),
        rag_budget_tokens=args.rag_budget_tokens)
    report(args.tier, args.extractor, args.out_tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
