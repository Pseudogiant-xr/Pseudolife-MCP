"""LongMemEval knowledge-update bench — the supersession subset, end to end.

Runs the LongMemEval (arXiv 2410.10813) *knowledge-update* questions through
the full PseudoLife pipeline: ingest each haystack session turn-by-turn, dream
after every session (the real cadence — consolidation between sessions), then
answer through three retrieval arms and judge with an LLM:

  * ``rag``    — top-k vector search over the raw turns (the naive baseline)
  * ``cortex`` — consolidated cortex facts + their supersession chains
  * ``hybrid`` — cortex facts + a small top-k of raw turns (the agent's view)

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

DATA_DIR = Path(__file__).resolve().parent / "data"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DATASETS = {
    "oracle": DATA_DIR / "longmemeval_oracle.json",
    "s": DATA_DIR / "longmemeval_s_cleaned.json",
}
# The experiment variable. gemma-e2b is the SHIPPED sidecar's QAT weights,
# served on the GPU for bench speed (identical outputs at temperature 0).
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


def load_questions(dataset: str) -> list[dict]:
    data = json.loads(DATASETS[dataset].read_text(encoding="utf-8"))
    return [q for q in data if q["question_type"] == "knowledge-update"]


def out_file(dataset: str, extractor: str, tag: str = "") -> Path:
    suffix = f"-{tag}" if tag else ""
    return RESULTS_DIR / f"longmemeval-ku-{dataset}-{extractor}{suffix}.jsonl"


def bank_dir(dataset: str, extractor: str, tag: str = "") -> Path:
    suffix = f"-{tag}" if tag else ""
    return RESULTS_DIR / "banks" / f"{dataset}-{extractor}{suffix}"


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


def build_contexts(svc, question: str) -> dict[str, str]:
    raw = svc.search(question, top_k=RAG_TOP_K).get("entries", [])
    raw_texts = [e.get("text", "") for e in raw]
    cortex = svc.cortex_search(question, top_k=CORTEX_TOP_K,
                               min_score=CORTEX_MIN_SCORE).get("entries", [])
    # Facts carry their supersession chain: knowledge-update asks about BOTH
    # the current value and the original one ("where did I initially ...") —
    # the version timeline (HLC supersession) is the memory system's actual
    # capability here, so the context must surface it.
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
    return {
        "rag": "\n\n".join(raw_texts),
        "cortex": "\n".join(fact_lines),
        "hybrid": ("Known facts:\n" + "\n".join(fact_lines) +
                   "\n\nRelevant memories:\n" +
                   "\n\n".join(raw_texts[:HYBRID_TOP_K])),
    }


def answer_and_judge(row: dict) -> dict:
    """Fill the answer/judge fields on a row from its persisted contexts."""
    for arm in ARMS:
        ctx = row["contexts"].get(arm, "")
        prompt = (f"Question date: {row['question_date']}\n"
                  f"Question: {row['question']}\n\nMemory context:\n{ctx or '(empty)'}")
        response = _chat(_ANSWER_SYSTEM, prompt)
        verdict = _chat(_JUDGE_SYSTEM, (
            f"Question: {row['question']}\n"
            f"Correct answer: {row['answer']}\n"
            f"Model response: {response}"), max_tokens=8)
        row[f"{arm}_response"] = response
        row[f"{arm}_correct"] = verdict.strip().lower().startswith("yes")
        row[f"{arm}_context_tokens"] = approx_tokens(ctx)
    return row


def run_extract(dataset: str, limit: int | None, extractor_name: str,
                do_answer: bool, tag: str = "", window: int = 0) -> None:
    ex_url = EXTRACTORS[extractor_name]
    if not probe(ex_url):
        sys.exit(f"no extractor server at {ex_url} — start it first")
    if do_answer and not probe(QWEN_URL):
        sys.exit(f"no answer/judge server at {QWEN_URL} — start it first")
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    questions = load_questions(dataset)
    if limit:
        questions = questions[:limit]
    out_path = out_file(dataset, extractor_name, tag)
    done = {r["question_id"] for r in load_rows(out_path)}
    print(f"{len(questions)} knowledge-update questions, extractor="
          f"{extractor_name} ({len(done)} already done, resuming)", flush=True)

    for i, q in enumerate(questions):
        if q["question_id"] in done:
            continue
        t_start = time.perf_counter()
        tmp = Path(tempfile.mkdtemp(prefix="lme_"))
        svc = build_service(tmp)                      # fresh, truncated bench DB
        svc.config.memory.dream.extract_relations = False   # facts only
        svc.config.memory.dream.known_facts_window = window
        extractor = OpenAICompatExtractor(ex_url, "bench", max_tokens=4096,
                                          timeout_seconds=600.0)
        tally = ingest_and_dream(svc, extractor, q, ex_url)
        contexts = build_contexts(svc, q["question"])
        facts = dump_bank(svc, q, bank_dir(dataset, extractor_name, tag)
                          / f"{q['question_id']}.json.gz")
        svc.flush()
        row = {
            "question_id": q["question_id"],
            "question": q["question"],
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


def run_answer(dataset: str, extractor_name: str, tag: str = "") -> None:
    if not probe(QWEN_URL):
        sys.exit(f"no answer/judge server at {QWEN_URL} — start it first")
    out_path = out_file(dataset, extractor_name, tag)
    rows = load_rows(out_path)
    pending = [r for r in rows if "rag_correct" not in r]
    print(f"answer phase: {len(pending)} of {len(rows)} rows pending", flush=True)
    for i, row in enumerate(pending):
        answer_and_judge(row)
        rewrite_rows(out_path, rows)          # atomic, resumable per row
        marks = " ".join(f"{a}={'Y' if row[f'{a}_correct'] else 'n'}"
                         for a in ARMS)
        print(f"[{i + 1}/{len(pending)}] {row['question_id']}  {marks}", flush=True)


def report(dataset: str, extractor_name: str, tag: str = "") -> None:
    out_path = out_file(dataset, extractor_name, tag)
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
    for arm in ARMS:
        acc = sum(r[f"{arm}_correct"] for r in rows) / n
        tok = sum(r[f"{arm}_context_tokens"] for r in rows) / n
        summary["arms"][arm] = {"accuracy": round(acc, 3),
                                "context_tokens": round(tok, 1)}
        print(f"{arm:<10}{acc:>10.3f}{tok:>12.1f}")
    sup = sum(r["consolidation"]["superseded"] for r in rows)
    print(f"supersessions across runs: {sup}")
    summary["superseded_total"] = sup
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
    ap.add_argument("--window", type=int, default=0,
                    help="known-facts window size for the dream pass "
                         "(0 = off; use 20 for the window arm — spec 2026-07-10)")
    args = ap.parse_args()
    if args.report:
        report(args.dataset, args.extractor, args.tag)
        return 0
    if args.phase == "answer":
        run_answer(args.dataset, args.extractor, args.tag)
    else:
        run_extract(args.dataset, args.limit, args.extractor,
                    do_answer=(args.phase == "full"), tag=args.tag,
                    window=args.window)
    if args.phase != "extract":
        report(args.dataset, args.extractor, args.tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
