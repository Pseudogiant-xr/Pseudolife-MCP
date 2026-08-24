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
import longmemeval_bench as lme  # noqa: E402
from longmemeval_bench import (  # noqa: E402
    _chat, ARMS, EXTRACTORS, QWEN_URL, RESULTS_DIR,
    _make_extractor, build_contexts, load_rows,
)


def arms_for(chronicle: bool, only: str | None = None) -> tuple[str, ...]:
    """The answered/judged arms: --chronicle adds hybrid_ev (vanilla
    hybrid + the served events block, same pinned search call — the LME
    ev2 arm contract). ``only`` (--arms) keeps a comma-separated subset in
    canonical order — a partial rerun (e.g. rag,hybrid for the
    budget-matched arm, with rag as the identical-input control) skips
    arms whose answers the question at hand does not need."""
    arms = (*ARMS, "hybrid_ev") if chronicle else ARMS
    if only:
        keep = {a.strip() for a in only.split(",") if a.strip()}
        unknown = keep - set(arms)
        if unknown:
            raise SystemExit(
                f"--arms names {sorted(unknown)} not in {arms} "
                "(hybrid_ev needs --chronicle)")
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
    "asks about an earlier state. Answer completely — include every part "
    "the question asks for; lists and multi-step answers are fine. If the "
    "context does not contain the information, say exactly: I don't know."
)

TIERS = ("100K", "500K", "1M", "10M")


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


def ingest_chat(svc, extractor, turns: list[dict]) -> dict:
    """Store every turn; drain the dream backlog at each BEAM batch
    boundary (the production between-sessions cadence) and at the end."""
    tally = {"turns": 0, "dreams": 0, "claims": 0, "superseded": 0,
             "literal_dropped": 0, "events_inserted": 0,
             "events_pass_failures": 0}
    current_batch = None
    for turn in turns:
        if current_batch is not None and turn["batch"] != current_batch:
            _dream_until_drained(svc, extractor, tally)
        current_batch = turn["batch"]
        anchor = f"[{turn['time_anchor']}] " if turn["time_anchor"] else ""
        svc.store(f"{anchor}{turn['role']}: {turn['content']}", source="beam")
        tally["turns"] += 1
    _dream_until_drained(svc, extractor, tally)
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
        hybrid_top_k: int | None = None) -> None:
    lme.CHRONICLE = chronicle          # build_contexts reads its module global
    if hybrid_top_k is not None:
        # Validate before any probe or global mutation: build_contexts
        # slices the hybrid turns from a top_k=RAG_TOP_K search, so a wider
        # budget would be silently capped at RAG_TOP_K while every row
        # records the wider number — an artifact asserting a budget that
        # was never served.
        if hybrid_top_k < 1:
            raise SystemExit("--hybrid-top-k must be positive")
        if hybrid_top_k > lme.RAG_TOP_K:
            raise SystemExit(
                f"--hybrid-top-k {hybrid_top_k} exceeds "
                f"RAG_TOP_K={lme.RAG_TOP_K}; the hybrid arm cannot serve "
                "more turns than the search returns")
        # build_contexts reads the module global at call time (pinned by
        # test_hybrid_top_k_is_read_at_call_time). Default None keeps every
        # prior artifact's 3-turn hybrid budget byte-identical; 6 matches
        # the rag arm's raw-turn budget so hybrid becomes a superset arm.
        lme.HYBRID_TOP_K = hybrid_top_k
    arms = arms_for(chronicle, arms_only)
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
        tmp = Path(tempfile.mkdtemp(prefix="beam_"))
        svc = build_service(tmp)
        svc.config.memory.dream.extract_relations = False
        svc.config.memory.dream.chronicle = chronicle
        extractor = _make_extractor(ex_url, None)
        tally = ingest_chat(svc, extractor, load_chat_turns(chat_dir))
        ingest_s = round(time.perf_counter() - t0, 1)
        print(f"chat {chat_id}: ingested {tally['turns']} turns, "
              f"{tally['dreams']} dreams, {tally['claims']} claims "
              f"({ingest_s}s)", flush=True)
        dump_chat_bank(svc, chat_id, tally,
                       beam_bank_dir(tier, extractor_name, tag)
                       / f"chat{chat_id}.json.gz")
        for q in pending:
            t1 = time.perf_counter()
            contexts = build_contexts(svc, q["question"], with_parts=True)
            parts = contexts.pop("parts")
            row = {"chat_id": chat_id, "tier": tier, "type": q["type"],
                   "index": q["index"], "question": q["question"],
                   "reference_answer": q["answer"],
                   "difficulty": q["difficulty"], "rubric": q["rubric"],
                   "extractor": extractor_name,
                   "hybrid_top_k": lme.HYBRID_TOP_K,
                   "consolidation": tally, "ingest_seconds": ingest_s,
                   # Served contexts + structured serve state (2026-08-21):
                   # serving-knob reruns and re-judges recompose from these
                   # instead of re-paying the ingest/extraction phase.
                   "contexts": {}, "parts": parts}
            for arm in arms:
                ctx = contexts.get(arm, "")
                row["contexts"][arm] = ctx
                prompt = (f"Question: {q['question']}\n\n"
                          f"Memory context:\n{ctx or '(empty)'}")
                response = _chat(_BEAM_ANSWER_SYSTEM, prompt,
                                 max_tokens=1024)
                verdict = judge_response(judge_prompt, q["question"],
                                         q["rubric"], response)
                row[f"{arm}_response"] = response
                row[f"{arm}_score"] = verdict["llm_judge_score"]
                row[f"{arm}_score_intfaithful"] = \
                    verdict["llm_judge_score_intfaithful"]
                row[f"{arm}_judge"] = verdict["items"]
                row[f"{arm}_judge_failures"] = verdict["judge_failures"]
            row["wall_seconds"] = round(time.perf_counter() - t1, 1)
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  {chat_id}/{q['type']}[{q['index']}] " + " ".join(
                f"{arm}={row[f'{arm}_score']:.2f}" for arm in arms),
                flush=True)
        svc.flush()
        ingested[chat_id] = ()


def report(tier: str, extractor_name: str, tag: str) -> None:
    out_path = out_file(tier, extractor_name, tag)
    rows = load_rows(out_path)
    if not rows:
        sys.exit(f"no results in {out_path}")
    # Arms come off the rows, not a static tuple: a --chronicle run's
    # summary carries hybrid_ev, a vanilla run's does not.
    arms = [a for a in arms_for(True) if f"{a}_score" in rows[0]]
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
    for arm in arms:
        summary["arms"][arm] = {
            "score": round(sum(r[f"{arm}_score"] for r in rows)
                           / len(rows), 4),
            "score_intfaithful": round(
                sum(r[f"{arm}_score_intfaithful"] for r in rows)
                / len(rows), 4),
        }
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    for qtype, trows in sorted(by_type.items()):
        summary["types"][qtype] = {
            "n": len(trows),
            **{arm: round(sum(r[f"{arm}_score"] for r in trows)
                          / len(trows), 4) for arm in arms},
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
                         "bench's HYBRID_TOP_K=3; 6 budget-matches the rag "
                         "arm)")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        report(args.tier, args.extractor, args.out_tag)
        return 0
    run(Path(args.beam_root), args.tier, args.extractor, args.out_tag,
        args.chats, args.limit_chats, chronicle=args.chronicle,
        arms_only=args.arms, hybrid_top_k=args.hybrid_top_k)
    report(args.tier, args.extractor, args.out_tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
