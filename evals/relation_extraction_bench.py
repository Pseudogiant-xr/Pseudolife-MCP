#!/usr/bin/env python
"""Relation-extraction benchmark (dev-only).

Scores the dream graph-from-text path: runs each extractor rung's
``extract_relations`` over a hand-labeled corpus and reports edge precision/
recall/F1 plus four defect-aligned diagnostics (naming consistency, type-
violation rate, related-to share, over-extraction). Unlike ladder_sweep this
needs NO database and NO embedder — extract_relations is a pure model call.

The opus-4.8 ceiling rung is produced in-session by subagents (see README):
  1. ``--emit-prompts`` writes results/relations_corpus_prompts.json
  2. dispatch subagents to extract over those prompts (the user's Claude usage)
  3. collect into results/relations-opus-4.8.json
  4. ``--report`` scores it as the absolute ceiling.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pseudolife_memory.graph import norm_name
from pseudolife_memory.memory.relation_quality import TYPE_CONSTRAINTS as RELATION_CONSTRAINTS
from pseudolife_memory.storage.postgres import _BUILTIN_RELATIONS

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ladder_sweep import RUNGS, make_extractor, probe  # noqa: E402

# Relation vocab handed to the model — same as service._dream_extract_relations
# (builtins minus the lesson-only prefers/avoids).
RELATION_REGISTRY = [(n, d) for (n, d, *_rest) in _BUILTIN_RELATIONS
                     if n not in ("prefers", "avoids")]

# canonical name -> {type, aliases}. Types: runtime/host, service/process,
# tool, file, datastore, component, concept, person.
ENTITIES: dict[str, dict] = {
    "pseudolife-daemon":   {"type": "service",   "aliases": ["the daemon", "pseudolife daemon"]},
    "postgres":            {"type": "datastore", "aliases": ["the postgres db", "pg", "postgres 16"]},
    "docker-desktop":      {"type": "runtime",   "aliases": ["docker"]},
    "windows 11":          {"type": "runtime",   "aliases": ["the windows host"]},
    "cortex console":      {"type": "component", "aliases": ["the web console", "the console ui"]},
    "chromadb":            {"type": "datastore", "aliases": ["the reference bank"]},
    "gemma 4 e2b sidecar": {"type": "service",   "aliases": ["the extractor sidecar", "gemma sidecar"]},
    "memory_recall":       {"type": "tool",      "aliases": ["the recall tool"]},
    "networkx":            {"type": "tool",      "aliases": ["the networkx read-model"]},
    "config.yaml":         {"type": "file",      "aliases": ["the config file"]},
    "schema":              {"type": "concept",   "aliases": []},
    "the user":            {"type": "person",    "aliases": ["i", "me"]},
}

# Each note: source text + gold closed-vocab edges (possibly empty).
CORPUS: list[dict] = [
    # --- Class 1: clean structural ---
    {"text": "The daemon runs in Docker and persists everything to Postgres.",
     "edges": [("pseudolife-daemon", "runs-on", "docker-desktop"),
               ("pseudolife-daemon", "stores-data-in", "postgres")]},
    {"text": "Cortex Console is part of the daemon and uses NetworkX for graph queries.",
     "edges": [("cortex console", "part-of", "pseudolife-daemon"),
               ("cortex console", "uses", "networkx")]},
    {"text": "The extractor sidecar runs on Docker, and the daemon depends on it for consolidation.",
     "edges": [("gemma 4 e2b sidecar", "runs-on", "docker-desktop"),
               ("pseudolife-daemon", "depends-on", "gemma 4 e2b sidecar")]},
    {"text": "ChromaDB is part of the daemon.",
     "edges": [("chromadb", "part-of", "pseudolife-daemon")]},
    {"text": "memory_recall uses NetworkX to walk the graph.",
     "edges": [("memory_recall", "uses", "networkx")]},
    {"text": "config.yaml configures the daemon.",
     "edges": [("config.yaml", "configures", "pseudolife-daemon")]},
    {"text": "The daemon runs on the Windows 11 host.",
     "edges": [("pseudolife-daemon", "runs-on", "windows 11")]},
    # --- Class 2: canonicalization probes (same entity, varied surface forms) ---
    {"text": "The gemma sidecar runs on Docker too.",
     "edges": [("gemma 4 e2b sidecar", "runs-on", "docker-desktop")]},
    {"text": "The daemon writes the entities table to pg.",
     "edges": [("pseudolife-daemon", "stores-data-in", "postgres")]},
    {"text": "The console ui is part of the daemon.",
     "edges": [("cortex console", "part-of", "pseudolife-daemon")]},
    # --- Class 3: null notes (no entity-to-entity relationship) ---
    {"text": "Honestly the new dashboard looks way cleaner than the old one.", "edges": []},
    {"text": "I spent way too long debugging this yesterday.", "edges": []},
    {"text": "We should probably write more tests at some point.", "edges": []},
    # --- Class 4: type traps ---
    {"text": "The migration touched schema v11 over the weekend.", "edges": []},
    {"text": "The user is on Windows 11.", "edges": []},
    {"text": "The daemon's data ends up in the nightly backup folder.", "edges": []},
]


def alias_index(entities: dict[str, dict]) -> dict[str, str]:
    """norm_name(surface) -> canonical, over canonical names + their aliases.
    Also registers an article-stripped variant (leading "the ") so model outputs
    that drop the article ("daemon" for "the daemon") still resolve — the lenient
    entity match is meant to credit correct relationships regardless of surface."""
    idx: dict[str, str] = {}
    for canon, meta in entities.items():
        for surface in [canon, *meta.get("aliases", [])]:
            s = surface.strip()
            idx[norm_name(s)] = canon
            if s.lower().startswith("the "):
                idx.setdefault(norm_name(s[4:]), canon)
    return idx


def resolve(name: str, idx: dict[str, str]) -> str | None:
    return idx.get(norm_name(name))


def _f1(p: float, r: float) -> float:
    return round(2 * p * r / (p + r), 3) if (p + r) else 0.0


def score(predicted: list[list[tuple]], corpus: list[dict] = CORPUS,
          entities: dict[str, dict] = ENTITIES) -> dict:
    if len(predicted) != len(corpus):
        raise ValueError(
            f"predicted has {len(predicted)} note-lists but corpus has {len(corpus)} notes")
    idx = alias_index(entities)
    tp = fp = fn = 0
    total_pred = related_to = 0
    struct_pred = struct_violation = 0
    null_edges = halluc = 0
    naming: dict[str, set] = {}          # canonical -> distinct normalized surfaces

    for note, preds in zip(corpus, predicted):
        gold = {(s, r, d) for (s, r, d) in note["edges"]}
        matched: set = set()
        is_null = not note["edges"]
        note_norm = norm_name(note["text"])
        for (s, r, d) in preds:
            total_pred += 1
            if is_null:
                null_edges += 1
            if r == "related-to":
                related_to += 1
            cs, cd = resolve(s, idx), resolve(d, idx)
            for raw, canon in ((s, cs), (d, cd)):
                if canon is not None:
                    naming.setdefault(canon, set()).add(norm_name(raw))
                # counts per unresolved endpoint (a fully-hallucinated edge contributes 2)
                elif norm_name(raw) not in note_norm:
                    halluc += 1
            if r in RELATION_CONSTRAINTS and cs and cd:
                struct_pred += 1
                src_ok, dst_ok = RELATION_CONSTRAINTS[r]
                if entities[cs]["type"] not in src_ok or entities[cd]["type"] not in dst_ok:
                    struct_violation += 1
            triple = (cs, r, cd)
            if cs and cd and triple in gold and triple not in matched:
                tp += 1
                matched.add(triple)
            else:
                fp += 1
        fn += len(gold) - len(matched)

    precision = round(tp / (tp + fp), 3) if (tp + fp) else 0.0
    recall = round(tp / (tp + fn), 3) if (tp + fn) else 0.0
    naming_consistency = (round(sum(len(v) for v in naming.values()) / len(naming), 3)
                          if naming else 1.0)
    n_notes = len(corpus) or 1
    return {
        "edge_precision": precision,
        "edge_recall": recall,
        "edge_f1": _f1(precision, recall),
        "naming_consistency": naming_consistency,
        "type_violation_rate": round(struct_violation / struct_pred, 3) if struct_pred else 0.0,
        "related_to_share": round(related_to / total_pred, 3) if total_pred else 0.0,
        "over_extraction_null_edges": null_edges,
        "over_extraction_halluc": halluc,
        "edges_per_note": round(total_pred / n_notes, 2),
    }


RESULTS_DIR = Path(__file__).resolve().parent / "results"


def predict_with(extractor, corpus: list[dict] = CORPUS) -> list[list[tuple]]:
    """Per-note: call extract_relations, return [(src, relation, dst), ...] strings."""
    out: list[list[tuple]] = []
    for note in corpus:
        triples = extractor.extract_relations([note["text"]], RELATION_REGISTRY)
        out.append([(t["src"], t["relation"], t["dst"]) for t in triples])
    return out


def run_rung(name: str) -> dict:
    rung = RUNGS[name]
    result = {"rung": name, "label": rung["label"]}
    if rung["kind"] == "floor":
        result["status"] = "n/a"   # RegexExtractor has no extract_relations
        return result
    # only llm rungs carry base_url; floor returned above, naive/other -> unreachable
    if rung["kind"] != "llm" or not probe(rung["base_url"]):
        result["status"] = "unreachable"
        result["base_url"] = rung.get("base_url")
        return result
    extractor = make_extractor(rung)        # OpenAICompatExtractor (max_tokens=4096, 600s)
    t0 = time.perf_counter()
    predicted = predict_with(extractor)
    secs = round(time.perf_counter() - t0, 1)
    result.update(score(predicted))
    result["extract_seconds"] = secs
    result["predicted"] = predicted          # raw triples = silver labels (§10)
    result["status"] = "ok"
    return result


def build_prompts() -> list[dict]:
    """Each corpus note paired with the EXACT system prompt + registry the
    headless LLM rungs receive — the single source for the in-session Opus rung."""
    from pseudolife_memory.memory.dream import _relations_prompt
    system = _relations_prompt(RELATION_REGISTRY)
    return [{"note_index": i, "system": system, "user": note["text"]}
            for i, note in enumerate(CORPUS)]


def emit_prompts(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_prompts(), indent=2), encoding="utf-8")
    return path


import argparse

LADDER_ORDER = ["floor", "gemma-e2b", "gemma-e4b", "qwen-27b", "opus-4.8"]


def _load_results() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if RESULTS_DIR.exists():
        for fp in RESULTS_DIR.glob("relations-*.json"):
            try:
                out[fp.stem.replace("relations-", "")] = json.loads(fp.read_text())
            except Exception:
                pass
    return out


def build_report(results: dict[str, dict]) -> list[dict]:
    ceiling = results.get("qwen-27b", {}).get("edge_f1")
    rows = []
    for name in LADDER_ORDER:
        r = results.get(name)
        if not r or r.get("status") != "ok":
            continue
        row = {k: r.get(k) for k in (
            "rung", "edge_f1", "type_violation_rate", "naming_consistency",
            "related_to_share", "over_extraction_null_edges", "over_extraction_halluc")}
        row["gap_to_27b"] = (round(r["edge_f1"] - ceiling, 3)
                             if ceiling is not None else None)
        rows.append(row)
    return rows


def report() -> None:
    rows = build_report(_load_results())
    hdr = (f"{'rung':<12}{'F1↑':>7}{'type_viol↓':>12}{'naming↓':>9}"
           f"{'rel-to↓':>9}{'null':>6}{'halluc':>8}{'gap_to_27b':>12}")
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for r in rows:
        print(f"{r['rung']:<12}{r['edge_f1']:>7}{r['type_violation_rate']:>12}"
              f"{r['naming_consistency']:>9}{r['related_to_share']:>9}"
              f"{r['over_extraction_null_edges']:>6}{r['over_extraction_halluc']:>8}"
              f"{str(r['gap_to_27b']):>12}")
    if not rows:
        print("(no results — run a rung, or add results/relations-opus-4.8.json)")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rung", choices=list(RUNGS), help="run one rung")
    ap.add_argument("--emit-prompts", action="store_true",
                    help="write results/relations_corpus_prompts.json for the opus rung")
    ap.add_argument("--report", action="store_true", help="aggregate results into the table")
    ap.add_argument("--list", action="store_true", help="list rungs + endpoints")
    args = ap.parse_args()

    if args.list:
        for n in LADDER_ORDER:
            r = RUNGS.get(n, {"label": "in-session subagents (see README)"})
            print(f"  {n:<12} {r.get('label', n):<34} {r.get('base_url', '—')}")
        return 0
    if args.report:
        report(); return 0
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.emit_prompts:
        p = emit_prompts(RESULTS_DIR / "relations_corpus_prompts.json")
        print(f"wrote {p}"); return 0
    if args.rung:
        out = run_rung(args.rung)
        (RESULTS_DIR / f"relations-{args.rung}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in out.items() if k != "predicted"}, indent=2))
        return 0
    ap.print_help(); return 1


if __name__ == "__main__":
    raise SystemExit(main())
