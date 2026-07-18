"""LongMemEval-V2 GPU smoke — `procedure` category, per-trajectory dreams.

Runs the pilot decisions pre-registered in ``lme_v2_adapter.py``'s READY-TO-RUN
block and ``data/lme_v2/FORMAT-NOTES.md`` end to end, mirroring
``longmemeval_bench.py``'s ingest -> dream -> answer -> judge shape but over
LME-V2 *trajectories* instead of chat sessions:

  Category   : ``procedure`` (workflow-knowledge, 74 questions, ALL text-only).
               NOT ``errors-gotchas`` — those are multimodal (FORMAT-NOTES.md).
  Observations: OFF (thought+action turns only; the adapter's default). Workflow
               knowledge lives in thought+action, and observations reintroduce
               the 115M-token scale — see OPEN QUESTION 1.
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
    """Store every adapted turn of each trajectory (observations off), dreaming
    to consolidation after EACH trajectory — one trajectory ~= one session."""
    tally = {"trajectories": 0, "turns": 0, "claims": 0, "inserted": 0,
             "superseded": 0, "extract_seconds": 0.0}
    held = 0
    for traj in trajectories:
        for turn in A.trajectory_to_turns(traj):   # include_observations=False
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
def answer_judge_score(row: dict) -> dict:
    """Fill answer/score/judge/latency fields from the row's persisted contexts.

    Reuses longmemeval_bench's answerer/judge prompts and _chat verbatim (kept
    identical so LME-V2 vs LME-V1 runs stay comparable). The primary metric is
    LME-V2's deterministic eval_function; the LLM judge is a secondary signal."""
    from longmemeval_bench import (_chat, _ANSWER_SYSTEM, _JUDGE_SYSTEM)
    from ladder_sweep import approx_tokens
    for arm in ARMS:
        ctx = row["contexts"].get(arm, "")
        prompt = (f"Question: {row['question']}\n\n"
                  f"Memory context:\n{ctx or '(empty)'}")
        t0 = time.perf_counter()
        response = _chat(_ANSWER_SYSTEM, prompt)
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
# Run
# --------------------------------------------------------------------------- #
def run_smoke(limit: int, max_traj: int) -> None:
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
        extractor = OpenAICompatExtractor(EXTRACTOR_URL, "bench",
                                          max_tokens=4096, timeout_seconds=600.0)
        tally = ingest_and_dream(svc, extractor, trajectories, EXTRACTOR_URL,
                                 probe)

        t_retr = time.perf_counter()
        contexts = build_contexts(svc, q["question"])
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
            "consolidation": tally,
            "retrieval_seconds": retrieval_seconds,
            "wall_seconds": round(time.perf_counter() - t_start, 1),
        }
        row = answer_judge_score(row)
        marks = " ".join(f"{a}={'Y' if row[f'{a}_correct'] else 'n'}"
                         for a in ARMS)
        with OUT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{i + 1}/{len(questions)}] {q['id']}  {marks}  "
              f"({row['wall_seconds']}s, {tally['turns']} turns, "
              f"{tally['superseded']} superseded, retr {retrieval_seconds}s)",
              flush=True)


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
    print(f"  observations        : OFF (thought+action only)")
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
        n_turns = sum(len(A.trajectory_to_turns(found[t]))
                      for t in traj_ids if t in found)
        print(f"  adapted turns (observations off): {n_turns} across "
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
    args = ap.parse_args()

    _bind_adapter_paths(resolve_data_dir())

    if args.dry_run:
        return dry_run(args.limit, args.max_trajectories)
    if args.report:
        report()
        return 0
    run_smoke(args.limit, args.max_trajectories)
    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
