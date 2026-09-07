"""Cognee adapter — run Cognee (github.com/topoteretes/cognee) against the
BEAM benchmark on the SAME instrument as beam_adapter.py.

Motivation: Cognee's published BEAM-100K number (0.79) was produced with a
gpt-5 judge and their own per-question-type answer prompts, so it cannot be
read against Pseudolife's local-instrument numbers. This adapter measures
Cognee on OUR instrument: same chats, same [session N, turn M] turn
stamping, same answerer + judge (local Qwen, temperature 0), same BEAM
rubric judging — making a cognee row directly pairable with the committed
beam_adapter.py artifacts on identical questions.

Protocol (deviations from Cognee's published run, recorded per row):
  * Retrieval-only search modes (CHUNKS / INSIGHTS) build the context; the
    shared bench answerer answers. Cognee's completion-style search modes
    answer with their own LLM call, which would un-match the instrument.
  * One generic answer prompt (the beam_adapter one), not per-type prompts.
  * ``--context-chars`` caps the served context so the arm can be
    char-budget-matched to a comparator arm's served context.

This file runs inside its own venv (.venv-cognee; Cognee's dependency tree
stays out of the bench venv) and therefore cannot import beam_adapter /
longmemeval_bench (both import torch + the live service at module scope).
The small pure helpers are duplicated from there instead — keep them in
sync with beam_adapter.py / longmemeval_bench.py:

    .venv-cognee/Scripts/python evals/cognee_adapter.py \
        --beam-root <path-to-BEAM> --tier 100K --out-tag cognee-smoke --smoke

Cognee is pointed at the same local endpoints via env (set below, override
by exporting first): LLM on the bench Qwen server (:1234, OpenAI-compatible,
instructor json_schema_mode) and fastembed CPU embeddings.

Writes ``evals/results/beam-<tier>-cognee-<tag>.jsonl`` (resumable per
question) + a ``.summary.json`` from ``--report``. Per-chat Cognee data/
system dirs live under --work-dir (default
``evals/results/banks/cognee-<tier>-<tag>``, gitignored) and are reused
when a ``.cognified`` marker for the same batch count is present, so
answer/judge reruns skip the expensive cognify.
"""
from __future__ import annotations

import argparse
import asyncio
import ast
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

# --- Cognee environment: set BEFORE importing cognee (it reads env/.env at
# import). setdefault everywhere so an operator export wins.
os.environ.setdefault("LLM_PROVIDER", "custom")
os.environ.setdefault("LLM_MODEL", "openai/bench")
os.environ.setdefault("LLM_ENDPOINT", "http://127.0.0.1:1234/v1")
os.environ.setdefault("LLM_API_KEY", "sk-local-bench")
os.environ.setdefault("LLM_INSTRUCTOR_MODE", "json_schema_mode")
os.environ.setdefault("EMBEDDING_PROVIDER", "fastembed")
# The cognee docs show the bare "all-MiniLM-L6-v2", but fastembed's registry
# needs the fully-qualified name (smoke failure 2026-09-01).
os.environ.setdefault("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
os.environ.setdefault("EMBEDDING_DIMENSIONS", "384")
os.environ.setdefault("TELEMETRY_DISABLED", "1")
# The bench convention exports offline HF flags; fastembed needs its
# one-time embedding-model download, so they must not leak into this venv.
os.environ.pop("HF_HUB_OFFLINE", None)
os.environ.pop("TRANSFORMERS_OFFLINE", None)

import cognee  # noqa: E402

try:  # re-export location has moved across cognee releases
    from cognee import SearchType  # noqa: E402
except ImportError:  # pragma: no cover — older layouts
    from cognee.modules.search.types import SearchType  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
QWEN_URL = os.environ.get("PSEUDOLIFE_BENCH_QWEN_URL",
                          "http://127.0.0.1:1234/v1")
TIERS = ("100K", "500K", "1M", "10M")
# Retrieval-only modes as of cognee 1.5.x (INSIGHTS was removed upstream;
# every *_COMPLETION mode answers with cognee's own LLM and is deliberately
# unsupported here — see module docstring). Graph-triplet context WITHOUT
# their answerer would need the GraphCompletionRetriever.get_context
# internals — candidate follow-up for the full run so their graph layer is
# represented in the served context.
SEARCH_TYPES = {"chunks": "CHUNKS", "chunks_lexical": "CHUNKS_LEXICAL",
                "summaries": "SUMMARIES", "temporal": "TEMPORAL"}

# --- duplicated from beam_adapter.py / longmemeval_bench.py (see module
# docstring for why import is impossible here). VERBATIM copies:
# tests/test_cognee_adapter.py holds every name below AST-identical to its
# origin, so a change to the instrument that is not mirrored here fails
# the suite instead of quietly un-matching the Cognee row.

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


_THINKING_LEVELS = ("low", "medium", "xhigh")
# Fields the sampler knob may never override: the conversation itself, the
# model id, and the thinking pin (the thinking knob is the one sanctioned
# way to change that; a sampler JSON that clobbered it would silently turn a
# "sampled, no-think" arm into a thinking arm).
_SAMPLER_PROTECTED = ("messages", "model", "chat_template_kwargs")


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


def probe(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/models", timeout=5):
            return True
    except Exception:  # noqa: BLE001
        return False


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


# --- cognee-specific machinery

def batch_documents(turns: list[dict],
                    limit_batches: int | None) -> list[str]:
    """One document per BEAM batch (the benchmark's session unit), turns
    stamped exactly as beam_adapter stores them so both systems read
    identically annotated text."""
    docs: dict[int, list[str]] = {}
    for i, turn in enumerate(turns, 1):
        docs.setdefault(turn["batch"], []).append(format_turn(turn, i))
    batches = sorted(docs)
    if limit_batches:
        batches = batches[:limit_batches]
    return ["\n".join(docs[b]) for b in batches]


async def ingest_chat(chat_id: str, docs: list[str], work_dir: Path) -> dict:
    """Point cognee at a per-chat data/system root (fresh-bank isolation,
    mirroring beam_adapter's fresh service per chat), add one document per
    BEAM batch, cognify. A ``.cognified`` marker makes reruns skip the
    LLM-heavy extraction."""
    chat_root = work_dir / f"chat{chat_id}"
    dataset = f"beam_chat_{chat_id}"
    marker = chat_root / ".cognified"
    discarded_partial = False
    if marker.exists():
        stamped = json.loads(marker.read_text(encoding="utf-8"))
        # A marker written for FEWER batches than this invocation wants is a
        # different corpus, not a cache hit — the 2026-09-01 smoke left a
        # 2-of-3-batch bank behind exactly this way, and silently reusing it
        # would serve two thirds of a chat while the row claimed a full one.
        if stamped.get("batches") == len(docs):
            cognee.config.data_root_directory(str(chat_root / "data"))
            cognee.config.system_root_directory(str(chat_root / "system"))
            return {"batches": len(docs), "reused": True,
                    "cognify_seconds": stamped.get("cognify_seconds")}
        print(f"chat {chat_id}: marker says {stamped.get('batches')} batches, "
              f"want {len(docs)} — discarding and re-cognifying", flush=True)
        discarded_partial = True
    # No marker but a populated root means a cognify died mid-flight (the run
    # spans several nights, so this WILL happen). cognee.add is additive, so
    # resuming onto that root would double part of the corpus; the only safe
    # resume unit is the whole chat.
    if chat_root.exists() and any(chat_root.iterdir()):
        shutil.rmtree(chat_root)
    chat_root.mkdir(parents=True, exist_ok=True)
    cognee.config.data_root_directory(str(chat_root / "data"))
    cognee.config.system_root_directory(str(chat_root / "system"))
    t0 = time.perf_counter()
    for doc in docs:
        await cognee.add(doc, dataset_name=dataset)
    await cognee.cognify([dataset])
    marker.write_text(json.dumps({
        "batches": len(docs),
        "cognify_seconds": round(time.perf_counter() - t0, 1),
    }), encoding="utf-8")
    # `discarded_partial` rides the row: on a multi-night run it is the audit
    # trail for "this chat was re-cognified because a shorter bank was found".
    return {"batches": len(docs), "reused": False,
            "discarded_partial": discarded_partial,
            "cognify_seconds": round(time.perf_counter() - t0, 1)}


def _flatten_results(results) -> list:
    """cognee 1.5 wraps each dataset's hits in an envelope dict
    ``{dataset_id, dataset_name, ..., search_result: [items]}`` — serve the
    ranked items, never the envelope (the first smoke json-dumped a 355K-char
    envelope as one 'result')."""
    flat = []
    for r in results or []:
        if isinstance(r, dict) and isinstance(r.get("search_result"), list):
            flat.extend(r["search_result"])
        else:
            flat.append(r)
    return flat


def _render_result(r) -> str:
    """Search results vary by mode/version: plain strings, dicts, graph
    triplet tuples. Render one line of context per result."""
    if isinstance(r, str):
        return r
    if isinstance(r, dict):
        for key in ("text", "chunk", "content", "summary"):
            if isinstance(r.get(key), str) and r[key].strip():
                return r[key]
        return json.dumps(r, ensure_ascii=False, default=str)
    if isinstance(r, (list, tuple)):
        return " — ".join(_render_result(x) for x in r)
    return str(r)


def _assemble(admitted: dict[str, list[str]], order: list[str]) -> str:
    return "\n\n".join(f"[{st}]\n" + "\n".join(admitted[st])
                       for st in order if admitted[st])


def _fit_to_budget(retrieved: dict[str, list[str]], order: list[str],
                   context_chars: int) -> dict[str, list[str]]:
    """Fill the character budget with WHOLE ranked results, never a slice of
    one.

    A hard ``ctx[:context_chars]`` cut hands the reader a chunk severed
    mid-sentence, which is not what either system would serve, and the
    penalty lands entirely on whichever system has the coarser retrieval
    granularity — cognee's chunks are far larger than our turns, so the cut
    would have been a systematic thumb on the scale at tight budgets
    (2026-09-02 fairness review, before any comparison was run).

    Two properties the budget must preserve, both stated rather than
    incidental:

    * **Rank-prefix per type.** Once one result from a type does not fit, that
      type is closed. Serving rank 5 because rank 3 was too fat would rewrite
      cognee's own ranking under the guise of budgeting.
    * **No type starved.** Types are interleaved by rank, so a wide first
      search type cannot eat the whole budget before the second is consulted.

    Admission is checked against the fully assembled string (section headers
    and separators included), so the returned context is exactly within
    budget rather than approximately so.
    """
    admitted: dict[str, list[str]] = {st: [] for st in order}
    if not context_chars:
        return {st: list(retrieved[st]) for st in order}
    open_types = [st for st in order if retrieved[st]]
    rank = 0
    while open_types:
        for st in list(open_types):
            if rank >= len(retrieved[st]):
                open_types.remove(st)
                continue
            admitted[st].append(retrieved[st][rank])
            if len(_assemble(admitted, order)) > context_chars:
                admitted[st].pop()
                open_types.remove(st)
        rank += 1
    return admitted


async def build_context(chat_id: str, question: str, search_types: list[str],
                        context_chars: int,
                        top_k: int | None) -> tuple[str, dict]:
    dataset = f"beam_chat_{chat_id}"
    retrieved: dict[str, list[str]] = {}
    legacy_signature = False
    for st_name in search_types:
        st = getattr(SearchType, SEARCH_TYPES[st_name])
        kwargs = {"query_text": question, "query_type": st,
                  "datasets": [dataset]}
        if top_k:
            kwargs["top_k"] = top_k
        try:
            results = await cognee.search(**kwargs)
        except TypeError as exc:
            # Older cognee: positional (query_type, query_text) and NO
            # datasets / top_k. That fallback serves a different retrieval
            # width and scope than the row records in `top_k` /
            # `search_types`, so it is flagged in meta rather than silently
            # taken — and only taken when the TypeError is about the call
            # itself, not one raised inside cognee's own search.
            if "argument" not in str(exc):
                raise
            legacy_signature = True
            results = await cognee.search(st, question)
        lines = [_render_result(r) for r in _flatten_results(results)]
        retrieved[st_name] = [ln for ln in lines if ln.strip()]
    admitted = _fit_to_budget(retrieved, search_types, context_chars)
    ctx = _assemble(admitted, search_types)
    # ACHIEVED, not requested: cognee's chunks are coarse, so at a tight cap
    # it lands under budget where a turn-granular system lands on it. A table
    # that reports the requested cap would hide that gap and quietly flatter
    # the finer-grained system.
    meta = {"retrieved_counts": {st: len(retrieved[st])
                                 for st in search_types},
            "served_counts": {st: len(admitted[st]) for st in search_types},
            "context_chars_served": len(ctx),
            # True means `top_k` and the dataset scope above were NOT applied
            # (old cognee search signature); such rows are not comparable to
            # rows where they were.
            "search_legacy_signature": legacy_signature}
    return ctx, meta


def out_file(tier: str, tag: str) -> Path:
    return RESULTS_DIR / f"beam-{tier}-cognee-{tag}.jsonl"


def default_work_dir(tier: str, tag: str) -> Path:
    """Per-chat Cognee data/system roots. Under the gitignored
    ``evals/results/banks/`` like every other bench bank — the first
    smoke used a root-level scratch dir that then sat untracked in the
    checkout for a week."""
    return RESULTS_DIR / "banks" / f"cognee-{tier}-{tag}"


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]


async def run(beam_root: Path, tier: str, tag: str, chats: str | None,
              limit_chats: int | None, limit_batches: int | None,
              limit_questions: int | None, search_types: list[str],
              context_chars: int, top_k: int | None,
              work_dir: Path) -> None:
    if not probe(os.environ["LLM_ENDPOINT"]):
        sys.exit(f"no LLM server at {os.environ['LLM_ENDPOINT']} — "
                 "start it first (evals/qwen_server.ps1 Start-Qwen)")
    if not probe(QWEN_URL):
        sys.exit(f"no answer/judge server at {QWEN_URL} — start it first")
    judge_prompt = load_judge_prompt(beam_root)
    all_chats = iter_chats(beam_root, tier)
    if chats:
        keep = {c.strip() for c in chats.split(",")}
        all_chats = [c for c in all_chats if c[0] in keep]
    if limit_chats:
        all_chats = all_chats[:limit_chats]
    out_path = out_file(tier, tag)
    done = {(r["chat_id"], r["type"], r["index"])
            for r in load_rows(out_path)}
    print(f"BEAM {tier} / cognee: {len(all_chats)} chats "
          f"({len(done)} question-rows already done)", flush=True)

    for chat_id, chat_dir in all_chats:
        questions = load_questions(chat_dir)
        if limit_questions:
            questions = questions[:limit_questions]
        pending = [q for q in questions
                   if (chat_id, q["type"], q["index"]) not in done]
        if not pending:
            continue
        t0 = time.perf_counter()
        docs = batch_documents(load_chat_turns(chat_dir), limit_batches)
        tally = await ingest_chat(chat_id, docs, work_dir)
        ingest_s = round(time.perf_counter() - t0, 1)
        print(f"chat {chat_id}: {tally['batches']} batch-docs "
              f"{'(reused)' if tally.get('reused') else 'cognified'} "
              f"({ingest_s}s)", flush=True)
        for q in pending:
            t1 = time.perf_counter()
            ctx, ctx_meta = await build_context(chat_id, q["question"],
                                                search_types, context_chars,
                                                top_k)
            prompt = (f"Question: {q['question']}\n\n"
                      f"Memory context:\n{ctx or '(empty)'}")
            response = _chat(_BEAM_ANSWER_SYSTEM, prompt, max_tokens=1024)
            verdict = judge_response(judge_prompt, q["question"],
                                     q["rubric"], response)
            row = {"chat_id": chat_id, "tier": tier, "type": q["type"],
                   "index": q["index"], "question": q["question"],
                   "reference_answer": q["answer"],
                   "difficulty": q["difficulty"], "rubric": q["rubric"],
                   "system": "cognee",
                   "search_types": search_types,
                   "context_chars_cap": context_chars,
                   "top_k": top_k,
                   **ctx_meta,
                   "limit_batches": limit_batches,
                   "consolidation": tally, "ingest_seconds": ingest_s,
                   "contexts": {"cognee": ctx},
                   "cognee_response": response,
                   "cognee_score": verdict["llm_judge_score"],
                   "cognee_score_intfaithful":
                       verdict["llm_judge_score_intfaithful"],
                   "cognee_judge": verdict["items"],
                   "cognee_judge_failures": verdict["judge_failures"],
                   "wall_seconds": round(time.perf_counter() - t1, 1)}
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  {chat_id}/{q['type']}[{q['index']}] "
                  f"cognee={row['cognee_score']:.2f} "
                  f"(ctx {len(ctx)}/{context_chars or '-'} chars, "
                  f"served {ctx_meta['served_counts']} of "
                  f"{ctx_meta['retrieved_counts']})", flush=True)


def report(tier: str, tag: str) -> None:
    rows = load_rows(out_file(tier, tag))
    if not rows:
        raise SystemExit("no rows to report")
    summary = {"tier": tier, "tag": tag, "system": "cognee", "n": len(rows),
               "score": round(sum(r["cognee_score"] for r in rows)
                              / len(rows), 4),
               "score_intfaithful": round(
                   sum(r["cognee_score_intfaithful"] for r in rows)
                   / len(rows), 4),
               "types": {}}
    for qtype in sorted({r["type"] for r in rows}):
        trows = [r for r in rows if r["type"] == qtype]
        summary["types"][qtype] = {
            "n": len(trows),
            "cognee": round(sum(r["cognee_score"] for r in trows)
                            / len(trows), 4)}
    path = out_file(tier, tag).with_suffix(".summary.json")
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--beam-root", required=True)
    ap.add_argument("--tier", choices=TIERS, default="100K")
    ap.add_argument("--out-tag", required=True)
    ap.add_argument("--chats", default=None,
                    help="comma-separated chat ids (default: all)")
    ap.add_argument("--limit-chats", type=int, default=None)
    ap.add_argument("--limit-batches", type=int, default=None,
                    help="ingest only the first N BEAM batches per chat "
                         "(smoke-only: probing questions target the whole "
                         "chat, so scores over a truncated ingest are "
                         "plumbing-validation only)")
    ap.add_argument("--limit-questions", type=int, default=None)
    ap.add_argument("--search-types", default="chunks,summaries",
                    help=f"comma-separated, from {sorted(SEARCH_TYPES)} — "
                         "retrieval-only modes; completion modes are "
                         "deliberately unsupported (they answer with "
                         "cognee's own LLM and un-match the instrument)")
    ap.add_argument("--top-k", type=int, default=None,
                    help="per-search-type retrieval width (default: cognee's "
                         "own default, 15 as of 1.5.3 — the first smoke's "
                         "uncapped 15 chunks + 15 summaries served ~328K "
                         "chars, the whole 2-batch corpus; set this AND "
                         "--context-chars for budget-matched runs)")
    ap.add_argument("--context-chars", type=int, default=0,
                    help="cap the served context (0 = uncapped; set for "
                         "budget-matched comparisons and recorded per row)")
    ap.add_argument("--work-dir", default=None,
                    help="cognee data/system roots per chat (default: "
                         "evals/results/banks/cognee-<tier>-<tag>, "
                         "gitignored like every other bench bank)")
    ap.add_argument("--smoke", action="store_true",
                    help="1 chat, 2 batches, 3 questions — validates "
                         "cognify + search + answer/judge plumbing only")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        report(args.tier, args.out_tag)
        return 0
    search_types = [s.strip() for s in args.search_types.split(",")
                    if s.strip()]
    unknown = set(search_types) - set(SEARCH_TYPES)
    if unknown:
        raise SystemExit(f"--search-types names {sorted(unknown)} not in "
                         f"{sorted(SEARCH_TYPES)}")
    limit_chats, limit_batches, limit_questions = (
        args.limit_chats, args.limit_batches, args.limit_questions)
    if args.smoke:
        limit_chats = limit_chats or 1
        limit_batches = limit_batches or 2
        limit_questions = limit_questions or 3
    work_dir = (Path(args.work_dir) if args.work_dir
                else default_work_dir(args.tier, args.out_tag))
    work_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(Path(args.beam_root), args.tier, args.out_tag,
                    args.chats, limit_chats, limit_batches, limit_questions,
                    search_types, args.context_chars, args.top_k, work_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
