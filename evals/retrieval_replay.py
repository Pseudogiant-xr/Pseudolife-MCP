"""Offline replay of logged retrieval events against a restored bank.

Re-runs the queries the daemon actually served (``retrieval_events``, the
learned-reranker Phase-0 log) through an offline ``MemoryService`` pointed
at a RESTORED copy of the bank, under several ranking settings, and scores
each setting against a label set.

What this measures, and what it does not
----------------------------------------
The bank has GROWN since these events were logged (entries added, facts
superseded, band recency and access counts moved). Replaying a 2026-08-20
query today does not reproduce the 2026-08-20 pool, so the ABSOLUTE MRR /
hit@k here are indicative only. The valid read is the PAIRED one: every
arm sees the identical restored bank and the identical query list, so a
difference between arms is attributable to the setting.

Label sources (``--label-source``)
----------------------------------
``uses``
    The real implicit relevance labels — a ``retrieval_uses`` row (a
    ``memory_get`` / ``memory_reinforce`` on a served entry within the
    session window). This is the label set Phase 1 would train on. On the
    2026-09-04 bank there is exactly ONE such event, so this mode is a
    plumbing check, not a measurement.
``logged-top1`` / ``logged-top3``
    The entry ids the daemon itself served at rank 0 (or ranks 0-2) for
    that query, used as pseudo-labels. This is NOT a relevance measurement
    — it measures agreement with the shipped ranker's own past decision,
    i.e. how far each arm moves the served head. It exists because it is
    the only label set with enough events (n≈1.2k) to separate arms at
    all, and it is reported as drift, never as accuracy.

Arms
----
``shipped``    no per-call overrides — the deployed config as-is.
``bm25_off``   ``bm25=False`` (the lexical fusion off).
``rerank_on``  ``rerank=True`` (cross-encoder over the top-N pool).
``pool_fusion`` the ``feat/retrieval-candidate-pool`` knobs, run only when
               this checkout actually carries them; otherwise reported as
               skipped with the reason.

PRIVACY: query text and entry text are private (public repo). The artifact
carries aggregates and ids only.

    python evals/retrieval_replay.py \
        --dsn postgresql://.../pseudolife_memory_replay_YYYYMMDD \
        --config /path/to/deployed/config.yaml --limit 120
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

RESULTS = Path(__file__).resolve().parent / "results"
FORBIDDEN_DBS = {"pseudolife_memory", "pseudolife_memory_bench"}

# Per-call overrides for each arm. ``pool_fusion`` is resolved at runtime
# against the checkout's config (see ``pool_knob_status``).
ARMS: dict[str, dict[str, Any]] = {
    "shipped": {},
    "bm25_off": {"bm25": False},
    "rerank_on": {"rerank": True},
}


def guard_dsn(dsn: str) -> None:
    """Refuse the live and shared-bench banks by name, in either DSN
    spelling libpq accepts and regardless of case.

    The 2026-09-04 pre-merge review found the original matched only a
    lower-case URI path segment: ``dbname=pseudolife_memory``, a trailing
    slash, and an upper-cased name each walked through onto the live bank.
    """
    text = re.sub(r"\?.*$", "", dsn.strip())
    names = {text.rstrip("/").rsplit("/", 1)[-1].lower()}
    names.update(m.group(1).lower() for m in re.finditer(
        r"\bdbname\s*=\s*['\"]?([^\s'\"]+)", text, re.IGNORECASE))
    hit = sorted(names & {d.lower() for d in FORBIDDEN_DBS})
    if hit:
        sys.exit(f"refusing to run against {hit[0]!r} — restore a dedicated "
                 "replay copy instead (see the module docstring)")


# ══════════════════════════════════════════════════════════════════════════
# pure scoring (unit-tested on fixtures; no DB, no model)
# ══════════════════════════════════════════════════════════════════════════

def first_label_rank(served_ids: Iterable[int],
                     labels: set[int]) -> int | None:
    """0-based rank of the first labelled id in a served list, or None."""
    for rank, sid in enumerate(served_ids):
        if int(sid) in labels:
            return rank
    return None


def score_case(served_ids: list[int], labels: set[int],
               ks: tuple[int, ...] = (1, 3, 6)) -> dict[str, Any]:
    """Reciprocal rank + hit@k for one replayed query."""
    r = first_label_rank(served_ids, labels)
    return {
        "rr": 0.0 if r is None else 1.0 / (r + 1),
        "rank": r,
        **{f"hit@{k}": bool(r is not None and r < k) for k in ks},
    }


def aggregate(cases: list[dict[str, Any]], latencies: list[float],
              ks: tuple[int, ...] = (1, 3, 6)) -> dict[str, Any]:
    """Arm-level MRR / hit@k / latency. Empty input yields zeros, not a
    crash — an arm with no scorable case must still appear in the table."""
    n = len(cases)
    if n == 0:
        return {"n": 0, "mrr": 0.0,
                **{f"hit@{k}": 0.0 for k in ks},
                "median_latency_s": 0.0, "mean_latency_s": 0.0}
    lat = sorted(latencies)
    return {
        "n": n,
        "mrr": round(sum(c["rr"] for c in cases) / n, 4),
        **{f"hit@{k}": round(
            sum(1 for c in cases if c[f"hit@{k}"]) / n, 4) for k in ks},
        "median_latency_s": round(lat[len(lat) // 2], 4) if lat else 0.0,
        "mean_latency_s": round(sum(lat) / len(lat), 4) if lat else 0.0,
    }


def build_cases(events: list[dict[str, Any]],
                uses: list[dict[str, Any]],
                label_source: str) -> list[dict[str, Any]]:
    """(query, labels) pairs for a label source. Events with no label are
    dropped — an input with no target trains and scores nothing."""
    by_event_uses: dict[int, set[int]] = {}
    for u in uses:
        by_event_uses.setdefault(int(u["event_id"]), set()).add(int(u["entry_id"]))
    out = []
    for e in events:
        served = sorted(e.get("served") or [],
                        key=lambda s: int(s.get("rank", 0)))
        ids = [int(s["entry_id"]) for s in served
               if s.get("entry_id") is not None]
        if label_source == "uses":
            labels = by_event_uses.get(int(e["id"]), set())
        elif label_source == "logged-top1":
            labels = set(ids[:1])
        elif label_source == "logged-top3":
            labels = set(ids[:3])
        else:  # pragma: no cover — argparse constrains the choices
            raise ValueError(f"unknown label source {label_source!r}")
        if not labels:
            continue
        out.append({"event_id": int(e["id"]), "query": e["query_text"],
                    "labels": labels, "logged_served_n": len(ids)})
    return out


def sample(cases: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """Deterministic even-stride subsample — every arm must see the same
    queries, and a head-slice would take one day's burst only."""
    if not limit or limit >= len(cases):
        return cases
    step = len(cases) / float(limit)
    return [cases[int(i * step)] for i in range(limit)]


def pool_knob_status(config: Any) -> dict[str, Any]:
    """Is the ``feat/retrieval-candidate-pool`` arm runnable here? Probes
    the live config object for the knobs rather than trusting a branch
    name — a worktree can sit on the branch with none of the change."""
    search = getattr(getattr(config, "memory", None), "search", None)
    names = [n for n in ("candidate_pool_size", "candidate_pool",
                         "fusion_mode", "pool_fusion")
             if search is not None and hasattr(search, n)]
    return {"available": bool(names), "knobs_found": names,
            "reason": ("" if names else
                       "no candidate-pool/fusion knobs on this checkout's "
                       "memory.search config — arm skipped")}


# ══════════════════════════════════════════════════════════════════════════
# replay
# ══════════════════════════════════════════════════════════════════════════

def fetch_events(dsn: str) -> tuple[list[dict], list[dict]]:
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, query_text, served, created_at "
                    "FROM retrieval_events ORDER BY id")
        events = [dict(zip(("id", "query_text", "served", "created_at"), r))
                  for r in cur.fetchall()]
        cur.execute("SELECT event_id, entry_id, used_via FROM retrieval_uses")
        uses = [dict(zip(("event_id", "entry_id", "used_via"), r))
                for r in cur.fetchall()]
    return events, uses


def build_service(dsn: str, config_path: str | None):
    """Offline ``MemoryService`` on a fresh data_dir (a stray legacy .pt in
    a shared dir would be imported). ``config_path`` seeds the deployed
    config so the ``shipped`` arm is production, not the dataclass
    defaults."""
    from pseudolife_memory.service import MemoryService  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="rreplay_"))
    if config_path:
        shutil.copyfile(config_path, tmp / "config.yaml")
    svc = MemoryService(data_dir=str(tmp), database_url=dsn)
    svc.config.embedding.device = "cpu"
    # The replay must not append to the very log it is replaying.
    svc.config.memory.retrieval_log.enabled = False
    return svc


def clear_query_embedding_cache(svc) -> bool:
    """Drop the embedder's ``(text, normalize)`` LRU between arms.

    Every arm replays the SAME query strings, so the second arm would read
    its query vectors out of ``EmbeddingPipeline._cache`` and post a
    latency an order of magnitude below the first arm's — a measurement
    artefact, not a setting effect. Returns False if the pipeline has no
    cache to clear (then the latency column is still comparable).
    """
    emb = getattr(svc, "_embedder", None)
    cache = getattr(emb, "_cache", None)
    if cache is None:
        return False
    lock = getattr(emb, "_cache_lock", None)
    if lock is not None:
        with lock:
            cache.clear()
    else:  # pragma: no cover — the pipeline always carries the lock
        cache.clear()
    return True


def run_arm(search: Callable[..., dict], cases: list[dict[str, Any]],
            overrides: dict[str, Any], top_k: int,
            progress_every: int = 25, label: str = "") -> dict[str, Any]:
    scored, lats, rank_hist = [], [], {}
    for i, c in enumerate(cases):
        t0 = time.perf_counter()
        res = search(c["query"], top_k=top_k, **overrides)
        lats.append(time.perf_counter() - t0)
        ids = [int(e["id"]) for e in res.get("entries", [])
               if e.get("id") is not None]
        s = score_case(ids, c["labels"])
        scored.append(s)
        key = "miss" if s["rank"] is None else str(s["rank"])
        rank_hist[key] = rank_hist.get(key, 0) + 1
        if progress_every and (i + 1) % progress_every == 0:
            print(f"  [{label}] {i + 1}/{len(cases)} "
                  f"mrr={sum(x['rr'] for x in scored) / len(scored):.3f}",
                  flush=True)
    agg = aggregate(scored, lats)
    agg["rank_histogram"] = dict(sorted(
        rank_hist.items(), key=lambda kv: (kv[0] == "miss", kv[0])))
    return agg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--config", default=None,
                    help="deployed config.yaml to seed the replay data_dir")
    ap.add_argument("--label-source", default="logged-top1",
                    choices=("uses", "logged-top1", "logged-top3"))
    ap.add_argument("--also-uses", action="store_true", default=True,
                    help="always additionally score the real `uses` labels")
    ap.add_argument("--arms", default="shipped,bm25_off,rerank_on")
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--out", default=str(RESULTS / "retrieval-replay.json"))
    args = ap.parse_args(argv)
    guard_dsn(args.dsn)

    events, uses = fetch_events(args.dsn)
    svc = build_service(args.dsn, args.config)
    pool = pool_knob_status(svc.config)

    label_sets = [args.label_source]
    if args.also_uses and "uses" not in label_sets:
        label_sets.append("uses")

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        sys.exit(f"unknown arm(s): {unknown}; known: {sorted(ARMS)}")

    results: dict[str, Any] = {}
    for ls in label_sets:
        cases = sample(build_cases(events, uses, ls), args.limit)
        print(f"label source {ls}: {len(cases)} scorable events", flush=True)
        per_arm = {}
        for arm in arms:
            print(f"  arm {arm} ...", flush=True)
            cleared = clear_query_embedding_cache(svc)
            per_arm[arm] = run_arm(svc.search, cases, ARMS[arm],
                                   args.top_k, label=f"{ls}/{arm}")
            per_arm[arm]["query_embedding_cache_cleared"] = cleared
        per_arm["pool_fusion"] = {"skipped": True, **pool}
        results[ls] = {"n_cases": len(cases), "arms": per_arm}

    report = {
        "source_db": re.sub(r"\?.*$", "", args.dsn).rsplit("/", 1)[-1],
        # The NAME only, never the path: an absolute path on the
        # maintainer's machine embeds the OS username, which the tracked-
        # tree identifier guard rejects (tests/test_release_ux.py).
        "config_seed": (Path(args.config).name if args.config
                        else "(dataclass defaults)"),
        "top_k": args.top_k,
        "limit": args.limit,
        "n_logged_events": len(events),
        "n_use_rows": len(uses),
        "results": results,
        "caveat": (
            "the bank has GROWN since these events were logged, so absolute "
            "MRR/hit@k are indicative only; the valid read is the paired "
            "comparison across arms on the identical restored bank. The "
            "logged-top* label sources measure AGREEMENT WITH THE SHIPPED "
            "RANKER's own past head, not relevance."),
        "privacy": "aggregates and ids only; no query or entry text",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
