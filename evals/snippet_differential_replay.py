"""Replay a bank's pending merge proposals through snippet attachment and
measure evidence quality — the harness behind
``evals/results/snippet-differential-live-20260830.json``.

Two arms over the same bank state:

* ``before`` — the pre-fix algorithm frozen below (``_legacy_snippets``:
  trace-id prefix, else sorted-mention prefix, mentions-map-only fallback,
  no pair-level diversification), the code shape the 2026-08-21 shadow
  comparison (``evals/results/judge-shadow-live-20260821.json``) measured
  at 40/109 (37%) low-differential;
* ``after`` — the live ``DreamOps._enrich_merge_proposals`` /
  ``_attach_candidate_snippets`` path, imported from the tree.

Both arms count the shadow comparison's two defect classes on the SHOWN
snippets: a proposal with an empty side, and a proposal whose sides share
at least half of their snippets. The ``after`` arm additionally reports how
many rows the code itself stamps ``low_differential`` (a superset: the
pool-containment criterion fires on pairs whose shown snippets diverge but
whose evidence pools coincide).

Read-only by construction (``default_transaction_read_only=on``); safe to
point at a live bank. Persists its result by default (``--out``).

Usage:
    python evals/snippet_differential_replay.py \
        [--dsn postgresql://...] [--out evals/results/<name>.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pseudolife_memory.memory import graph_consolidation as gc  # noqa: E402
from pseudolife_memory.service_dream import DreamOps  # noqa: E402
from pseudolife_memory.utils.config import DeepDreamConfig  # noqa: E402

DEFAULT_DSN = "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory"


class _Svc(DreamOps):
    """Bind the enrichment methods without a full service. _fold_direction
    is copied verbatim from MemoryService (one line; the heavy service
    import would drag in the embedding stack)."""

    @staticmethod
    def _fold_direction(frm, into, evidence):
        return (into, frm) if evidence(frm) > evidence(into) else (frm, into)


def _legacy_snippets(rows, entities, entries, traces, mentions, k, max_chars):
    """The pre-fix per-side selection, frozen for the ``before`` arm."""
    by_id = {e["id"]: e for e in entries}
    canon = {e["id"]: e["canonical"] for e in entities}

    def snippets(eid):
        ids = traces.get(canon.get(eid, ""), [])[:k]
        if not ids and mentions:
            ids = sorted(mentions.get(eid, ()))[:k]
        texts = [by_id[i]["text"] for i in ids if i in by_id][:k]
        return [t[:max_chars] for t in texts] if max_chars else texts

    return [(snippets(a), snippets(b)) for a, b in rows]


def _measure(pairs):
    """Count the two defect classes over (src_snippets, dst_snippets) pairs."""
    empty = shared50 = identical = 0
    for src, dst in pairs:
        if not src or not dst:
            empty += 1
            continue
        sh = len(set(src) & set(dst))
        if set(src) == set(dst):
            identical += 1
        if sh / min(len(src), len(dst)) >= 0.5:
            shared50 += 1
    n = len(pairs)
    low = empty + shared50          # the classes are mutually exclusive
    return {"pending_merge_proposals": n,
            "empty_side_proposals": empty,
            "shared_ge_50pct_proposals": shared50,
            "identical_snippet_sets": identical,
            "low_differential_total": low,
            "low_differential_share": round(low / n, 3) if n else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--out", default=None,
                    help="result path (default: evals/results/"
                         "snippet-differential-live-<yyyymmdd>.json)")
    args = ap.parse_args()
    out_path = Path(args.out) if args.out else (
        Path(__file__).resolve().parent / "results" /
        f"snippet-differential-live-{time.strftime('%Y%m%d')}.json")

    conn = psycopg.connect(args.dsn,
                           options="-c default_transaction_read_only=on")
    cur = conn.cursor()
    ent_cols = ("id", "canonical", "display", "etype", "created_at")
    cur.execute("SELECT id, canonical, display, etype, created_at "
                "FROM entities ORDER BY id")
    entities = [dict(zip(ent_cols, r)) for r in cur.fetchall()]
    edge_cols = ("id", "src_id", "relation", "dst_id", "confidence",
                 "origin", "asserted_at")
    cur.execute(f"SELECT {', '.join(edge_cols)} FROM edges "
                "WHERE superseded_at IS NULL ORDER BY id")
    edges = [dict(zip(edge_cols, r)) for r in cur.fetchall()]
    # Embeddings are irrelevant here (vectors are discarded); a dummy keeps
    # entity_context_vectors' shape without parsing pgvector payloads.
    cur.execute("SELECT id, text FROM entries ORDER BY id")
    entries = [{"id": i, "text": t or "", "embedding": np.ones(4)}
               for i, t in cur.fetchall()]
    cur.execute("SELECT entity_norm, entry_id FROM memory_traces "
                "ORDER BY entity_norm, entry_id")
    traces: dict[str, list[int]] = {}
    for norm, eid in cur.fetchall():
        traces.setdefault(norm, []).append(eid)
    cur.execute("SELECT entity_id, COUNT(*) FROM facts "
                "WHERE status='current' AND entity_id IS NOT NULL "
                "GROUP BY entity_id")
    fact_counts = {eid: int(n) for eid, n in cur.fetchall()}
    cur.execute("SELECT entity_id, source FROM entity_sources "
                "ORDER BY entity_id, source")
    scope_map: dict[int, list[str]] = {}
    for eid, s in cur.fetchall():
        scope_map.setdefault(eid, []).append(s)
    pcols = ("id", "kind", "entity_id", "into_id", "score", "reason",
             "status", "created_at", "judge_verdict", "judge_confidence",
             "judge_note", "judge_model", "judged_at")
    cur.execute(
        "SELECT p.id, p.kind, p.entity_id, p.into_id, p.score, p.reason, "
        "p.status, p.created_at, p.judge_verdict, p.judge_confidence, "
        "p.judge_note, p.judge_model, p.judged_at, e.display, i.display "
        "FROM entity_proposals p JOIN entities e ON e.id=p.entity_id "
        "LEFT JOIN entities i ON i.id=p.into_id "
        "WHERE p.status='pending' AND p.kind='merge' "
        "ORDER BY p.score DESC NULLS LAST, p.id")
    merges = []
    for r in cur.fetchall():
        d = dict(zip(pcols, r[:13]))
        d["entity"], d["into"] = r[13], r[14]
        merges.append(d)
    conn.close()

    cfg = DeepDreamConfig()
    _, mentions = gc.entity_context_vectors(
        entities, entries, traces, min_mentions=cfg.min_entity_mentions,
        max_fallback_mentions=cfg.max_fallback_mentions or None)

    svc = _Svc()
    enriched = svc._enrich_merge_proposals(
        merges, entities, edges, entries, traces, mentions, scope_map,
        cfg.max_context_snippets, cfg.snippet_max_chars, True,
        fact_counts=fact_counts)
    after_pairs = [(r["from"]["snippets"], r["into"]["snippets"])
                   for r in enriched]
    after = _measure(after_pairs)
    after["flagged_low_differential"] = sum(
        1 for r in enriched if r.get("low_differential"))

    from pseudolife_memory.graph import degree_counts
    deg = degree_counts(edges)
    oriented = [svc._fold_direction(
        p["entity_id"], p["into_id"],
        lambda eid: deg.get(eid, 0) + fact_counts.get(eid, 0))
        for p in merges]
    before = _measure(_legacy_snippets(
        oriented, entities, entries, traces, mentions,
        cfg.max_context_snippets, cfg.snippet_max_chars))

    out = {
        "generated_for": ("merge-proposal snippet attachment quality: "
                          "before/after the differential-evidence fix"),
        "date": time.strftime("%Y-%m-%d"),
        "harness": "evals/snippet_differential_replay.py",
        "method": (
            "read-only replay of the bank's pending merge proposals; 'before' "
            "re-runs the frozen pre-fix per-side selection, 'after' runs the "
            "live _enrich_merge_proposals path; both count the two defect "
            "classes of evals/results/judge-shadow-live-20260821.json on the "
            "shown snippets (empty side; shared texts / min(side counts) >= "
            "0.5), with deployed-default DeepDreamConfig"),
        "bank_snapshot": {"entities": len(entities), "edges": len(edges),
                          "entries": len(entries),
                          "trace_norms": len(traces)},
        "before": before,
        "after": after,
        "notes": (
            "after.flagged_low_differential counts rows the code stamps, a "
            "superset of after.low_differential_total: the pool-containment "
            "criterion also fires on pairs whose shown snippets diverge only "
            "because exclusive-first selection diversified a one-sided pool. "
            "identical_snippet_sets can RISE from before to after when a "
            "previously-empty side gains the same scan evidence as its "
            "partner."),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")
    print(json.dumps({"before": before, "after": after}, indent=2))


if __name__ == "__main__":
    main()
