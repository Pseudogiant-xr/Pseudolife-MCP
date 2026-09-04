"""Lever 6 — organic-edge graph ablation: does `memory_recall` earn its cost?

Two halves, both read-only against a RESTORED copy of the bank:

``shape``
    What the graph actually is: live edge counts by relation, the degree
    distribution and its top nodes, entities that are dead weight (no
    current facts and no non-``part-of`` edge), and comparator names the
    corpus talks about often but that have no node at all — the
    "``naive RAG`` is mentioned in nine entries and is not an entity"
    class, which is what an organic-edge graph is supposed to catch.

``ablate``
    ``memory_recall`` (search → graph-expand → re-query) vs plain
    ``memory_search`` on the same queries: served characters, wall time,
    whether the expected entity/entry surfaces at all, and — the point of
    the lever — how the extra entities ARRIVED. An entity reached only
    through a ``part-of`` edge or only through a hub node is expansion
    that a flat search would have missed for a structural reason; an
    entity reached through a domain relation (``depends-on``, ``uses``,
    ``runs-on``) is the graph doing the job it was built for.

Query sets: the labelled/logged retrieval events (what agents really
asked) plus ``RELATIONAL_QUESTIONS`` — thirty hand-written questions in
the bank's own domain whose answers require an edge, each with the entity
that should surface.

PRIVACY: the bank holds personal names and machine identifiers. Entity
names reach the artifact only when they also occur in the tracked repo
tree (``git grep``), which makes them already-public strings; everything
else is emitted as ``<redacted>``. Query and entry text never appear.

    python evals/graph_ablation.py \
        --dsn postgresql://.../pseudolife_memory_replay_YYYYMMDD \
        --config /path/to/deployed/config.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

REPO = Path(__file__).resolve().parents[1]
RESULTS = Path(__file__).resolve().parent / "results"
FORBIDDEN_DBS = {"pseudolife_memory", "pseudolife_memory_bench"}

# Thirty relational questions in the bank's own domain: each needs an edge
# to answer, not a single dense hit. ``expect`` is the canonical entity
# name that should surface; all thirty are strings that also occur in the
# tracked repo tree, so they are safe to commit.
RELATIONAL_QUESTIONS: list[dict[str, str]] = [
    {"q": "what does the pseudolife daemon depend on", "expect": "daemon"},
    {"q": "how does the shim reach the bank", "expect": "shim"},
    {"q": "what process owns the memory bank volumes", "expect": "pseudolife-daemon"},
    {"q": "which script deploys the daemon", "expect": "ops-update-ps1"},
    {"q": "what does ops/update.ps1 do before rebuilding", "expect": "ops-update-ps1"},
    {"q": "where does the backup script write its artifacts", "expect": "ops-backup-ps1"},
    {"q": "what stores the memory bank", "expect": "postgres"},
    {"q": "which service does the Cortex Console talk to", "expect": "cortex-console"},
    {"q": "what does the dream extractor run on", "expect": "dream-extractor"},
    {"q": "what is the deep dream pass part of", "expect": "memory-deep-dream"},
    {"q": "which component owns the cortex fact store", "expect": "cortex"},
    {"q": "how does Claude Code reach the pseudolife memory tools", "expect": "claude-code"},
    {"q": "what does the codex shim connect to", "expect": "codex-shim"},
    {"q": "which test pins the eval evidence", "expect": "tests-test-eval-evidence-py"},
    {"q": "what is graph_review.py part of", "expect": "graph-review-py"},
    {"q": "what does service.py implement", "expect": "service-py"},
    {"q": "which model does the extraction sidecar use", "expect": "dream-extractor"},
    {"q": "what hosts the postgres container", "expect": "postgres"},
    {"q": "what configures the daemon at startup", "expect": "daemon"},
    {"q": "which files are part of pseudolife-mcp", "expect": "pseudolife-mcp"},
    {"q": "what does memory_search depend on", "expect": "memory-search"},
    {"q": "what is chronicle_events part of", "expect": "chronicle-events"},
    {"q": "which tools does the MCP server expose", "expect": "tools"},
    {"q": "what does the Sonnet shim implement", "expect": "evals-sonnet-shim-py"},
    {"q": "what does the step-C judge decide", "expect": "step-c-judge"},
    {"q": "which eval run used chip12-b16", "expect": "chip12-b16"},
    {"q": "what is qwen3-8 used for in the bench", "expect": "qwen3-8"},
    {"q": "what does the BEAM 100k run measure", "expect": "beam-100k-run"},
    {"q": "what is claude-desktop configured with", "expect": "claude-desktop"},
    {"q": "what corpus does the e4b v3 lora train on", "expect": "e4b-v3-lora"},
]

# Names an organic-edge graph over this corpus ought to have promoted to
# nodes: comparators and systems the entries argue about.
COMPARATOR_TERMS = [
    "naive rag", "rag", "longmemeval", "mem0", "zep", "memgpt", "letta",
    "graphrag", "cognee", "hipporag", "a-mem", "memory bank", "chromadb",
    "pgvector", "bm25", "titans", "beam", "lme",
]


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
# privacy redaction
# ══════════════════════════════════════════════════════════════════════════

class NameRedactor:
    """Emit an entity name only when it also occurs in the tracked repo
    tree. The bank holds personal names and machine identifiers (the
    2026-07-12 scrub lesson); a name that is already in a public commit
    cannot leak by being repeated here, and everything else is dropped.
    """

    def __init__(self, repo: Path, enabled: bool = True) -> None:
        self.repo, self.enabled = repo, enabled
        self._cache: dict[str, bool] = {}

    def public(self, name: str) -> bool:
        if not self.enabled:
            return True
        if name in self._cache:
            return self._cache[name]
        ok = False
        try:
            r = subprocess.run(["git", "grep", "-qiF", "--", name],
                               cwd=str(self.repo), capture_output=True,
                               timeout=30)
            ok = r.returncode == 0
        except Exception:  # noqa: BLE001 — no git: redact everything
            ok = False
        self._cache[name] = ok
        return ok

    def __call__(self, name: str) -> str:
        return name if self.public(name) else "<redacted>"


# ══════════════════════════════════════════════════════════════════════════
# pure helpers (unit-tested on fixtures; no DB, no model)
# ══════════════════════════════════════════════════════════════════════════

def sample_evenly(items: list[Any], limit: int) -> list[Any]:
    """Deterministic even-stride subsample — a head slice of the question
    list would take only the infrastructure questions."""
    if not limit or limit >= len(items):
        return items
    step = len(items) / float(limit)
    return [items[int(i * step)] for i in range(limit)]


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; matches the recall hub gate's intent
    (``_hub_threshold``) closely enough for a degree cut-off."""
    if not values:
        return 0.0
    xs = sorted(values)
    idx = min(len(xs) - 1, max(0, int(round(pct / 100.0 * (len(xs) - 1)))))
    return float(xs[idx])


def degree_map(edges: list[tuple[str, str, str]]) -> dict[str, int]:
    """Undirected degree over (src, relation, dst) triples."""
    d: Counter[str] = Counter()
    for src, _rel, dst in edges:
        d[src] += 1
        d[dst] += 1
    return dict(d)


def classify_arrivals(seeds: list[str], entities: list[str],
                      edges: list[dict[str, Any]], degrees: dict[str, int],
                      hub_threshold: float) -> dict[str, Any]:
    """For every entity recall added BEYOND the seeds, how did it arrive?

    ``via_part_of``  every edge that touches it is ``part-of`` — pure
                     containment, the cheapest edge the extractor makes.
    ``via_hub``      it is itself a hub (degree >= p95), or the only edge
                     that reached it also touches a hub.
    ``via_domain``   at least one non-``part-of`` edge touches it.
    ``unlinked``     no edge in the returned set mentions it (it came from
                     the re-query's dense hits, not from an edge).
    """
    seed_set = set(seeds)
    added = [e for e in entities if e not in seed_set]
    touching: dict[str, list[dict[str, Any]]] = {n: [] for n in added}
    for ed in edges:
        for side in ("src", "dst"):
            n = ed.get(side)
            if n in touching:
                touching[n].append(ed)
    out = {"added": len(added), "via_part_of": 0, "via_domain": 0,
           "via_hub": 0, "unlinked": 0}
    for n in added:
        eds = touching[n]
        if not eds:
            out["unlinked"] += 1
            continue
        rels = {e.get("relation") for e in eds}
        if rels == {"part-of"}:
            out["via_part_of"] += 1
        else:
            out["via_domain"] += 1
        hub_side = any(
            degrees.get(e.get("src"), 0) >= hub_threshold
            or degrees.get(e.get("dst"), 0) >= hub_threshold for e in eds)
        if degrees.get(n, 0) >= hub_threshold or hub_side:
            out["via_hub"] += 1
    return out


def served_chars_search(res: dict[str, Any]) -> int:
    return sum(len(e.get("text") or "") for e in res.get("entries", []))


def served_chars_recall(res: dict[str, Any]) -> int:
    """Everything recall puts in front of the model: the texts plus the
    rendered facts on each entity plus the edge triples."""
    n = sum(len(t if isinstance(t, str) else (t.get("text") or ""))
            for t in res.get("texts", []))
    for ent in res.get("entities", []):
        n += len(ent.get("entity") or "")
        for f in ent.get("facts", []):
            n += len(str(f.get("attribute", ""))) + len(str(f.get("value", "")))
    for ed in res.get("edges", []):
        n += sum(len(str(ed.get(k, ""))) for k in ("src", "relation", "dst"))
    return n


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def expected_hit_search(res: dict[str, Any], expect: str) -> bool:
    needle = _norm(expect.replace("-", " "))
    return any(needle in _norm(e.get("text") or "")
               for e in res.get("entries", []))


def expected_hit_recall(res: dict[str, Any], expect: str) -> bool:
    if any(_norm(expect) == _norm(e.get("entity") or "")
           for e in res.get("entities", [])):
        return True
    return expected_hit_search({"entries": [
        {"text": t if isinstance(t, str) else (t.get("text") or "")}
        for t in res.get("texts", [])]}, expect)


def summarize_pairs(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Arm-level aggregate over per-question rows."""
    n = len(pairs) or 1
    def med(key, arm):
        xs = sorted(p[arm][key] for p in pairs)
        return xs[len(xs) // 2] if xs else 0
    out: dict[str, Any] = {"n": len(pairs)}
    for arm in ("search", "recall"):
        out[arm] = {
            "mean_served_chars": round(
                sum(p[arm]["served_chars"] for p in pairs) / n, 1),
            "median_served_chars": med("served_chars", arm),
            "mean_wall_s": round(sum(p[arm]["wall_s"] for p in pairs) / n, 4),
            "median_wall_s": round(med("wall_s", arm), 4),
            "expected_hit_rate": round(
                sum(1 for p in pairs if p[arm]["expected_hit"]) / n, 4),
        }
    out["recall_only_hits"] = sum(
        1 for p in pairs
        if p["recall"]["expected_hit"] and not p["search"]["expected_hit"])
    out["search_only_hits"] = sum(
        1 for p in pairs
        if p["search"]["expected_hit"] and not p["recall"]["expected_hit"])
    out["low_confidence_recalls"] = sum(
        1 for p in pairs if p["recall"].get("low_confidence"))
    arrivals: Counter[str] = Counter()
    for p in pairs:
        for k, v in (p["recall"].get("arrivals") or {}).items():
            arrivals[k] += v
    out["arrivals_total"] = dict(arrivals)
    out["chars_ratio_recall_over_search"] = (
        round(out["recall"]["mean_served_chars"]
              / max(1.0, out["search"]["mean_served_chars"]), 3))
    out["time_ratio_recall_over_search"] = (
        round(out["recall"]["mean_wall_s"]
              / max(1e-9, out["search"]["mean_wall_s"]), 3))
    return out


# ══════════════════════════════════════════════════════════════════════════
# graph shape
# ══════════════════════════════════════════════════════════════════════════

def graph_shape(dsn: str, redact: NameRedactor,
                top_n: int = 20) -> dict[str, Any]:
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT s.canonical, e.relation, d.canonical FROM edges e "
            "JOIN entities s ON s.id = e.src_id "
            "JOIN entities d ON d.id = e.dst_id "
            "WHERE e.superseded_at IS NULL")
        live = [(r[0], r[1], r[2]) for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM edges")
        n_all_edges = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM entities")
        n_entities = cur.fetchone()[0]
        cur.execute("SELECT DISTINCT entity_norm FROM facts "
                    "WHERE status = 'current'")
        with_facts = {r[0] for r in cur.fetchall()}
        # Comparator coverage: how often is a term written into entries,
        # and does it have a node? Counted in SQL to avoid pulling text.
        comparators = []
        for term in COMPARATOR_TERMS:
            cur.execute("SELECT count(*) FROM entries WHERE text ILIKE %s",
                        (f"%{term}%",))
            mentions = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM entities WHERE canonical = %s "
                        "OR canonical = %s",
                        (term.replace(" ", "-"), term))
            has_node = int(cur.fetchone()[0]) > 0
            comparators.append({"term": term, "entries_mentioning": mentions,
                                "has_node": has_node})

    deg = degree_map(live)
    by_rel = Counter(r for _s, r, _d in live)
    non_part_of = {n for s, r, d in live if r != "part-of" for n in (s, d)}
    linked = {n for s, _r, d in live for n in (s, d)}
    orphans = [n for n in linked - non_part_of if n not in with_facts]
    isolated = n_entities - len(linked)
    p95 = percentile([float(v) for v in deg.values()], 95.0)
    top = sorted(deg.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return {
        "entities": n_entities,
        "edges_all_versions": n_all_edges,
        "edges_live": len(live),
        "edges_by_relation": dict(by_rel.most_common()),
        "part_of_share": round(by_rel.get("part-of", 0) / max(1, len(live)), 4),
        "degree_p50": percentile([float(v) for v in deg.values()], 50.0),
        "degree_p95": p95,
        "degree_max": max(deg.values()) if deg else 0,
        "top_degree_nodes": [{"entity": redact(n), "degree": d}
                             for n, d in top],
        "entities_with_no_live_edge": isolated,
        "dead_weight_entities": {
            "count": len(orphans),
            "definition": ("linked only by part-of edges AND holding no "
                           "current fact"),
        },
        "comparator_coverage": {
            "threshold_entries": 5,
            "mentioned_5plus_without_a_node": [
                c["term"] for c in comparators
                if c["entries_mentioning"] >= 5 and not c["has_node"]],
            "detail": comparators,
        },
    }


# ══════════════════════════════════════════════════════════════════════════
# ablation
# ══════════════════════════════════════════════════════════════════════════

def build_service(dsn: str, config_path: str | None):
    from pseudolife_memory.service import MemoryService  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="gabl_"))
    if config_path:
        shutil.copyfile(config_path, tmp / "config.yaml")
    svc = MemoryService(data_dir=str(tmp), database_url=dsn)
    svc.config.embedding.device = "cpu"
    svc.config.memory.retrieval_log.enabled = False
    return svc


def run_pairs(svc, cases: list[dict[str, Any]], degrees: dict[str, int],
              hub_threshold: float, top_k: int) -> list[dict[str, Any]]:
    rows = []
    for i, c in enumerate(cases):
        q, expect = c["q"], c["expect"]
        t0 = time.perf_counter()
        s = svc.search(q, top_k=top_k)
        t1 = time.perf_counter()
        r = svc.recall(q, top_k=top_k)
        t2 = time.perf_counter()
        ents = [e.get("entity") for e in r.get("entities", [])]
        rows.append({
            "case": c.get("label", "relational"),
            "expect": expect if c.get("expect_public", True) else "<redacted>",
            "search": {"wall_s": t1 - t0,
                       "served_chars": served_chars_search(s),
                       "n_entries": len(s.get("entries", [])),
                       "expected_hit": (c["hit_search"](s) if "hit_search" in c
                                        else expected_hit_search(s, expect))},
            "recall": {"wall_s": t2 - t1,
                       "served_chars": served_chars_recall(r),
                       "n_entities": len(ents),
                       "n_edges": len(r.get("edges", [])),
                       "n_texts": len(r.get("texts", [])),
                       "low_confidence": bool(r.get("low_confidence")),
                       "expected_hit": (c["hit_recall"](r) if "hit_recall" in c
                                        else expected_hit_recall(r, expect)),
                       "arrivals": classify_arrivals(
                           r.get("seeds", []), ents, r.get("edges", []),
                           degrees, hub_threshold)},
        })
        if (i + 1) % 10 == 0:
            print(f"  [{i + 1}/{len(cases)}]", flush=True)
    return rows


def labelled_cases(dsn: str, limit: int) -> list[dict[str, Any]]:
    """Logged queries with a served head, scored on whether the entry the
    daemon served at rank 0 comes back.

    ``search`` returns entry ids, so its side matches on the id. ``recall``
    returns ``texts`` as PLAIN STRINGS with no ids at all (``RecallState.
    texts: list[str]``), so matching it on an id would silently score
    every logged case a miss. The target's text is loaded here and matched
    verbatim — recall appends exactly what search handed it. The text is
    used in memory only; it never reaches the artifact.
    """
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, query_text, served FROM retrieval_events "
                    "WHERE jsonb_array_length(served) > 0 ORDER BY id")
        rows = cur.fetchall()
        cur.execute("SELECT id, text FROM entries")
        texts = {int(r[0]): (r[1] or "") for r in cur.fetchall()}
    cases = []
    for eid, q, served in rows:
        top = sorted(served, key=lambda s: int(s.get("rank", 0)))
        ids = [int(s["entry_id"]) for s in top if s.get("entry_id") is not None]
        if not ids or not texts.get(ids[0]):
            continue
        target, ttext = ids[0], texts[ids[0]]
        cases.append({
            "q": q, "expect": f"entry:{target}", "expect_public": False,
            "label": "logged", "event_id": int(eid),
            "hit_search": (lambda res, t=target: any(
                int(e.get("id", -1)) == t for e in res.get("entries", []))),
            "hit_recall": (lambda res, s=ttext: any(
                (x if isinstance(x, str) else (x or {}).get("text", "")) == s
                for x in res.get("texts", []))),
        })
    return sample_evenly(cases, limit)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--logged-limit", type=int, default=40)
    # `recall` at the shipped defaults (3 hops, max_entities=50,
    # expand_budget=0) issues one search per newly-discovered entity per
    # hop — measured 2026-09-04 against the live-bank copy (1296 entries,
    # 5504 entities) at a mean of 32.4 s per call on the relational set and
    # 44.3 s on the logged set, worst case 73.0 s
    # (graph-ablation-20260904.json, ablation.*.summary.recall.mean_wall_s).
    # A full 30-question sweep still runs to tens of minutes; these caps
    # make a smaller, honest run possible and the artifact records the n.
    ap.add_argument("--rel-limit", type=int, default=0,
                    help="cap the relational-question set (0 = all 30)")
    ap.add_argument("--no-redact", action="store_true",
                    help="emit raw entity names (NEVER for a committed "
                         "artifact — the bank holds personal names)")
    ap.add_argument("--out", default=str(RESULTS / "graph-ablation.json"))
    args = ap.parse_args(argv)
    guard_dsn(args.dsn)

    redact = NameRedactor(REPO, enabled=not args.no_redact)
    print("graph shape ...", flush=True)
    shape = graph_shape(args.dsn, redact)

    svc = build_service(args.dsn, args.config)
    degrees = svc._graph_degrees()  # noqa: SLF001 — eval reads the same map
    hub_threshold = percentile([float(v) for v in degrees.values()], 95.0)

    rel_cases = [dict(c) for c in RELATIONAL_QUESTIONS]
    if args.rel_limit:
        rel_cases = sample_evenly(rel_cases, args.rel_limit)
    print(f"relational questions ({len(rel_cases)} of "
          f"{len(RELATIONAL_QUESTIONS)}) ...", flush=True)
    rel_rows = run_pairs(svc, rel_cases, degrees, hub_threshold, args.top_k)
    logged = labelled_cases(args.dsn, args.logged_limit)
    print(f"logged queries ({len(logged)}) ...", flush=True)
    log_rows = run_pairs(svc, logged, degrees, hub_threshold, args.top_k)

    report = {
        "source_db": re.sub(r"\?.*$", "", args.dsn).rsplit("/", 1)[-1],
        # The NAME only, never the path — an absolute path on the
        # maintainer's machine embeds the OS username, which the tracked-
        # tree identifier guard rejects (tests/test_release_ux.py).
        "config_seed": (Path(args.config).name if args.config
                        else "(dataclass defaults)"),
        "top_k": args.top_k,
        "hub_degree_p95": hub_threshold,
        "graph_shape": shape,
        "ablation": {
            "relational_questions": {
                "n_asked": len(rel_cases),
                "n_available": len(RELATIONAL_QUESTIONS),
                "summary": summarize_pairs(rel_rows),
                "per_question": [
                    {k: v for k, v in r.items() if k != "hit_search"}
                    for r in rel_rows],
            },
            "logged_queries": {
                "n_sampled": len(logged),
                "summary": summarize_pairs(log_rows),
            },
        },
        "caveat": ("the bank has grown since the logged events were served, "
                   "so the logged arm's absolute hit rates are indicative; "
                   "search and recall see the identical restored bank, so "
                   "the paired comparison is the valid read"),
        "privacy": ("entity names appear only when they also occur in the "
                    "tracked repo tree; no query or entry text"),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"shape": {k: shape[k] for k in
                                ("entities", "edges_live", "part_of_share",
                                 "degree_p95", "dead_weight_entities")},
                      "relational": report["ablation"]
                      ["relational_questions"]["summary"],
                      "logged": report["ablation"]["logged_queries"]
                      ["summary"]}, indent=2, default=str))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
