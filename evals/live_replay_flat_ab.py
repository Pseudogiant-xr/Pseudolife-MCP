"""Live-bank flat-vs-banded read-path replay on REAL recorded queries.

Edge case 5 of the 2026-08-14 flat-band verdict preregistration: the
synthetic LongMemEval corpora may not represent the production workload,
so this replays the closest thing to it — the retrieval queries agents
actually issued against the live bank — under both read topologies at
identical capacity, over identical restored bank state.

The daemon records no query text (verified 2026-08-14: no query/search
table in schema v29, uvicorn access logs off, only per-band aggregate
hit counters). The queries DO survive client-side, in Claude Code
session transcripts (``~/.claude/projects/**/*.jsonl``), as
``mcp__pseudolife-memory__memory_search`` tool_use blocks.

``harvest``
    Scans the transcripts read-only and writes a deduplicated query
    corpus JSONL. The corpus can contain private text — it is written to
    ``evals/results`` but MUST NEVER be committed (public repo); the
    committed artifact from ``replay`` carries aggregate stats only.

``replay``
    Runs every harvested query through two offline ``MemoryService``
    instances pointed at two restored copies of the live bank (see the
    preregistration doc for the restore recipe): arm A hydrates the
    as-shipped 8-band topology from the ``entries.band`` column; arm B
    gets ``write_flat_config`` (one band at the continuum's total
    capacity) so hydration seats every row into the single flat band via
    the ``sync.py:103`` unknown-band fallback. Both arms therefore hold
    IDENTICAL entries; only read-time pooling differs. Embedder on CPU;
    the GPU is never touched. Reports top-k membership divergence
    (the preregistered G-E5 screen: >20% top-3 divergence escalates to a
    judged preference run, otherwise the read paths are declared
    near-identical on the production distribution).

Restored copies are the replay's responsibility to create/drop — this
script refuses to run against a DSN whose database name is the live
``pseudolife_memory`` or the shared ``pseudolife_memory_bench``.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

RESULTS = Path(__file__).resolve().parent / "results"
DEFAULT_CORPUS = RESULTS / "abl25-e5-queries.jsonl"   # NEVER COMMIT (PII)
TOOL_PREFIX = "mcp__pseudolife-memory__memory_search"
FORBIDDEN_DBS = {"pseudolife_memory", "pseudolife_memory_bench"}


# ══════════════════════════════════════════════════════════════════════════
# harvest
# ══════════════════════════════════════════════════════════════════════════

def _iter_tool_uses(line: str):
    """Yield memory_search tool_use inputs from one transcript JSONL line.
    Transcript rows are chat-message envelopes; tool_use blocks sit in
    message.content lists. Malformed/foreign rows are skipped silently."""
    if TOOL_PREFIX not in line:
        return
    try:
        row = json.loads(line)
    except ValueError:
        return
    msg = row.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if (isinstance(block, dict) and block.get("type") == "tool_use"
                and str(block.get("name", "")).startswith(TOOL_PREFIX)):
            inp = block.get("input") or {}
            query = (inp.get("query") or "").strip()
            if query:
                yield {
                    "query": query,
                    "top_k": inp.get("top_k"),
                    "timestamp": row.get("timestamp"),
                    "session": row.get("sessionId"),
                }


def cmd_harvest(args) -> int:
    root = Path(args.transcripts_root).expanduser()
    if not root.is_dir():
        sys.exit(f"transcripts root not found: {root}")
    seen: dict[str, dict] = {}
    files = sorted(root.glob("**/*.jsonl"))
    scanned = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for line in text.splitlines():
            for rec in _iter_tool_uses(line):
                seen.setdefault(rec["query"], rec)   # first occurrence wins
    out = Path(args.out)
    with out.open("w", encoding="utf-8") as fh:
        for rec in seen.values():
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"scanned {scanned} transcript files -> {len(seen)} distinct "
          f"queries -> {out}")
    print("NOTE: this corpus may contain private text — do not commit it.")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# replay
# ══════════════════════════════════════════════════════════════════════════

def _guard_dsn(dsn: str) -> None:
    db = re.sub(r"\?.*$", "", dsn).rsplit("/", 1)[-1]
    if db in FORBIDDEN_DBS:
        sys.exit(f"refusing to run against {db!r} — restore a dedicated "
                 "replay copy instead (see module docstring)")


def _build(dsn: str, flat_cap: int | None):
    """Offline MemoryService against a restored replay DB. A fresh empty
    data_dir per arm (a stray legacy .pt would be imported otherwise);
    flat_cap!=None injects the one-band config before construction."""
    from band_ablation import write_flat_config  # noqa: PLC0415
    from pseudolife_memory.service import MemoryService  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="e5_"))
    if flat_cap is not None:
        write_flat_config(tmp, flat_cap)
    svc = MemoryService(data_dir=str(tmp), database_url=dsn)
    svc.config.embedding.device = "cpu"
    if flat_cap is not None:
        n = len(svc.config.memory.miras.bands)
        if svc.config.memory.miras.preset != "custom" or n != 1:
            sys.exit(f"flat injection failed ({n} bands) — aborting")
    return svc


def cmd_replay(args) -> int:
    _guard_dsn(args.banded_dsn)
    _guard_dsn(args.flat_dsn)
    from band_ablation import continuum_total_capacity  # noqa: PLC0415

    queries = []
    with Path(args.corpus).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                queries.append(json.loads(line))
    if args.limit:
        queries = queries[: args.limit]
    if not queries:
        sys.exit("empty query corpus — run harvest first")

    cap = args.flat_cap or continuum_total_capacity()
    svc_a = _build(args.banded_dsn, None)
    svc_b = _build(args.flat_dsn, cap)

    k = args.top_k
    n_div_topk, n_div_top3, per_query = 0, 0, []
    lat_a, lat_b = [], []
    for i, rec in enumerate(queries):
        q = rec["query"]
        t0 = time.perf_counter()
        ra = [e.get("text", "") for e in
              svc_a.search(q, top_k=k).get("entries", [])]
        t1 = time.perf_counter()
        rb = [e.get("text", "") for e in
              svc_b.search(q, top_k=k).get("entries", [])]
        t2 = time.perf_counter()
        lat_a.append(t1 - t0)
        lat_b.append(t2 - t1)
        div_k = set(ra) != set(rb)
        div_3 = set(ra[:3]) != set(rb[:3])
        n_div_topk += div_k
        n_div_top3 += div_3
        ju = len(set(ra) & set(rb)) / max(1, len(set(ra) | set(rb)))
        per_query.append({"i": i, "divergent_topk": div_k,
                          "divergent_top3": div_3,
                          "jaccard_topk": round(ju, 4),
                          "n_a": len(ra), "n_b": len(rb)})
        if args.dump_selections:
            per_query[-1]["a"] = ra   # private text — untracked dump only
            per_query[-1]["b"] = rb
        if (i + 1) % 25 == 0:
            print(f"[{i + 1}/{len(queries)}] top-{k} divergence "
                  f"{n_div_topk / (i + 1):.2%}", flush=True)

    n = len(queries)

    def med(xs):
        return sorted(xs)[len(xs) // 2]

    agg = {
        "n_queries": n,
        "top_k": k,
        "flat_cap": cap,
        "divergence_rate_topk": round(n_div_topk / n, 4),
        "divergence_rate_top3": round(n_div_top3 / n, 4),
        "mean_jaccard_topk": round(
            sum(p["jaccard_topk"] for p in per_query) / n, 4),
        "median_search_s_banded": round(med(lat_a), 4),
        "median_search_s_flat": round(med(lat_b), 4),
        "escalate_to_judge": (n_div_top3 / n) > 0.20,
        "corpus_note": "queries harvested from local agent transcripts; "
                       "texts withheld from this artifact (private)",
    }
    out = Path(args.out)
    out.write_text(json.dumps(agg, indent=2), encoding="utf-8")
    if args.dump_selections:
        det = out.with_name(out.stem + "-detail.jsonl")
        with det.open("w", encoding="utf-8") as fh:
            for p in per_query:
                fh.write(json.dumps(p, ensure_ascii=False) + "\n")
        print(f"per-query detail (private, do not commit) -> {det}")
    print(json.dumps({key: agg[key] for key in
                      ("n_queries", "divergence_rate_topk",
                       "divergence_rate_top3", "mean_jaccard_topk",
                       "escalate_to_judge")}, indent=2))
    print(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("harvest", help="scan agent transcripts for real "
                                      "memory_search queries")
    p.add_argument("--transcripts-root",
                   default=str(Path.home() / ".claude" / "projects"))
    p.add_argument("--out", default=str(DEFAULT_CORPUS))
    p.set_defaults(fn=cmd_harvest)

    p = sub.add_parser("replay", help="A/B the banded vs flat read path "
                                      "over restored live-bank copies")
    p.add_argument("--banded-dsn", required=True)
    p.add_argument("--flat-dsn", required=True)
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    p.add_argument("--top-k", type=int, default=6)
    p.add_argument("--flat-cap", type=int, default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dump-selections", action="store_true",
                   help="also write per-query selections (private text — "
                        "never commit the detail file)")
    p.add_argument("--out",
                   default=str(RESULTS / "abl25-e5-live-replay.json"))
    p.set_defaults(fn=cmd_replay)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
