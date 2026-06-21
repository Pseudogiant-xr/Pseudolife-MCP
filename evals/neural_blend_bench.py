"""F1 — does the MIRAS neural blend earn its keep vs plain cosine?

Isolates the band-level neural retrieval blend. Each band ranks by

    scores = w * cos(stored, MLP.predict(query)) + (1 - w) * cos(stored, query)

(``band.retrieve``), where ``w`` ramps to ``neural_blend_weight`` over the
band's first ``neural_warmup_updates`` updates. This harness ingests a
paraphrase-recall corpus ONCE (training the per-band MLPs through the real
``store`` path), then re-runs the SAME queries on the SAME trained state,
toggling only ``w``:

    OFF   w = 0.0   pure exact cosine (the neural-OFF baseline)
    ON    w = 0.6   shipped default (warmup ramp left intact)
    PURE  w = 1.0   MLP-only, warmup forced off (stress point)

The cortex is empty (auto_promote off, no dream), and reranker / BM25 /
recency-boost are disabled per query, so the ONLY thing that differs between
conditions is the MLP blend. We report recall@k + MRR overall, and split by
query↔gold lexical overlap — the low-overlap bucket is where exact cosine is
weakest and a useful associative memory should help if it helps anywhere.

Methodology note: the corpus is *generated* (templated facts + lexically
varied paraphrase queries, fixed seed) — reproducible and bias-controlled, but
synthetic. It is a decision-grade screen, not a LongMemEval-grade benchmark.

Standalone (not in the test suite). File-mode, CPU embedder, fixed seed; never
touches the live bank.

Run:
    PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python evals/neural_blend_bench.py
    PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python evals/neural_blend_bench.py --n 200 --seed 7
"""

from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
from pathlib import Path

# Force CPU + offline BEFORE importing torch/service (mirrors ladder_sweep).
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")

_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "is", "are", "was", "were",
    "what", "which", "who", "where", "when", "how", "does", "do", "did", "its",
    "it", "that", "this", "and", "or", "by", "with", "at", "as", "be", "has",
    "have", "had", "will", "can", "could", "their", "your", "you", "they",
}


def _toks(s: str) -> set[str]:
    return {w for w in "".join(
        c.lower() if c.isalnum() else " " for c in s).split() if w not in _STOP}


def _overlap(query: str, gold: str) -> float:
    """Jaccard token overlap (content words) between a query and its gold fact."""
    q, g = _toks(query), _toks(gold)
    return len(q & g) / len(q | g) if (q or g) else 0.0


# ── corpus generation ────────────────────────────────────────────────────
# Each template family yields (fact, query) pairs. Phrasings deliberately vary
# in how much vocabulary the query shares with the fact, so the auto-computed
# overlap buckets span easy (high-overlap) to hard (low-overlap) recall.

_ORGS = ["Northwind", "Acme", "Globex", "Initech", "Umbrella", "Hooli", "Stark",
         "Wayne", "Cyberdyne", "Tyrell", "Soylent", "Vandelay", "Massive",
         "Pied Piper", "Bluth", "Wonka"]
_PEOPLE = ["Mara", "Devi", "Olsen", "Park", "Ines", "Tau", "Quill", "Rao",
           "Bex", "Cho", "Nuri", "Volk", "Sage", "Ferro", "Lin", "Okafor",
           "Wren", "Idris", "Pax", "Yara", "Kade", "Soto", "Engel", "Maro",
           "Riva", "Toth", "Ng", "Adler", "Bisa", "Cruz"]
_ROLES = [("runs the billing team", "leads billing"),
          ("manages the data platform", "is in charge of the data platform"),
          ("owns the mobile app", "looks after the mobile app"),
          ("heads security", "is responsible for security"),
          ("oversees the warehouse", "is the warehouse boss")]
_SERVICES = ["vextra", "beacon", "relay", "atlas", "pylon", "drift", "kiln",
             "marlin", "quartz", "harbor", "ember", "spindle", "cobalt",
             "nimbus", "tidal", "girder", "lumen", "vault", "monsoon", "crux",
             "halo", "forge", "warden", "zephyr"]
_CITIES = ["Lisbon", "Osaka", "Calgary", "Nairobi", "Perth", "Tallinn", "Quito",
           "Bergen", "Hue", "Cusco", "Tromso", "Davao", "Riga", "Pune", "Accra",
           "Galway"]
_FOODS = ["miso ramen", "pad thai", "burek", "ceviche", "laksa", "pierogi",
          "bibimbap", "shakshuka", "khachapuri", "poke", "gyoza", "menudo",
          "dosa", "feijoada", "borscht", "tagine", "pho", "arepas", "okonomiyaki",
          "jollof rice", "goulash", "empanadas", "katsu", "injera"]
_BOOKS = ["Tidewall", "Glass Harvest", "The Ninth Gate", "Saltmarsh", "Ember & Ash",
          "Quiet Engines", "Northbound", "The Paper Cartographer", "Slow Lightning",
          "Granite Songs", "The Lantern Keeper", "Driftwood Almanac", "Hollow Tide",
          "Verdigris", "The Salt Path Home", "Cinder Lane", "Owllight", "Brackish",
          "The Tin Orchard", "Marrow Street"]
_AUTHORS = ["P. Vane", "L. Okonro", "S. Castellan", "D. Marsh", "T. Aalto",
            "R. Eberhardt", "M. Selvi", "C. Bramble", "H. Quist", "N. Adeyemi"]
_PROJECTS = ["Strata", "Bellwether", "Karst", "Foundry", "Mistral", "Cairn",
             "Loom", "Anvil", "Tessera", "Hearth", "Conduit", "Beacon-X",
             "Latch", "Mosaic", "Pennant", "Rivet", "Sable", "Trellis"]
_LANGS = ["Rust", "Go", "Elixir", "Kotlin", "Zig", "OCaml", "Clojure", "Swift",
          "Scala", "Crystal"]
_ANIMALS = [("octopus", "can change colour to hide from predators",
             "What lets it blend into its surroundings?"),
            ("peregrine falcon", "dives faster than any other bird",
             "Which one is the quickest in a dive?"),
            ("axolotl", "can regrow lost limbs",
             "What creature regenerates body parts?"),
            ("tardigrade", "survives the vacuum of space",
             "What animal endures outer space?"),
            ("mantis shrimp", "throws the fastest punch in the ocean",
             "Which sea animal strikes the hardest?")]


def build_corpus(n: int, seed: int) -> list[dict]:
    """Generate up to ``n`` unique-gold (fact, paraphrase-query) pairs. Every
    query is keyed to a unique subject so the gold fact is unambiguous; phrasings
    span high→low query/gold lexical overlap (auto-bucketed at scoring time)."""
    rng = random.Random(seed)
    pairs: list[tuple[str, str]] = []

    # person -> role (low overlap: query says "job", fact states the role verb).
    for p in _PEOPLE:
        fact_role, _ = rng.choice(_ROLES)
        org = rng.choice(_ORGS)
        pairs.append((f"{p} {fact_role} at {org}.", f"What is {p}'s job?"))
    # person -> favourite food (low overlap).
    for p in _PEOPLE:
        pairs.append((f"{p}'s favourite dish is {rng.choice(_FOODS)}.",
                      f"What does {p} most like to eat?"))
    # service -> port (high overlap).
    for s in _SERVICES:
        pairs.append((f"The {s} service listens on port {rng.randint(2000, 9999)}.",
                      f"Which port is {s} bound to?"))
    # service -> owner (hard negative vs port: same entity, other attribute).
    for s in _SERVICES:
        pairs.append((f"The {s} service is maintained by {rng.choice(_PEOPLE)}.",
                      f"Who keeps {s} running?"))
    # org -> city (medium overlap).
    for o in _ORGS:
        pairs.append((f"{o}'s regional office is located in {rng.choice(_CITIES)}.",
                      f"Where is {o} based for the region?"))
    # book -> author (medium-low overlap).
    for b in _BOOKS:
        pairs.append((f"{b} was written by {rng.choice(_AUTHORS)}.",
                      f"Who is the author of {b}?"))
    # project -> language (low overlap).
    for pr in _PROJECTS:
        pairs.append((f"The {pr} project is built in {rng.choice(_LANGS)}.",
                      f"What language is {pr} written in?"))
    # animals (very low overlap: no shared content words by design).
    for animal, ability, q in _ANIMALS:
        pairs.append((f"The {animal} {ability}.", q))

    seen, corpus = set(), []
    rng.shuffle(pairs)
    for fact, query in pairs:
        if fact in seen:
            continue
        seen.add(fact)
        corpus.append({"fact": fact, "query": query,
                       "overlap": round(_overlap(query, fact), 3)})
        if len(corpus) >= n:
            break
    return corpus


# ── eval ───────────────────────────────────────────────────────────────────

def _set_blend(svc, weight: float, warmup: int | None = None) -> None:
    for b in svc._cms.bands:
        b.neural_blend_weight = weight
        if warmup is not None:
            b.neural_warmup_updates = warmup


def _rank_of_gold(svc, query: str, gold: str, top_k: int) -> int | None:
    res = svc.search(query, top_k=top_k, min_score=0.0,
                     disable_recency_boost=True, rerank=False, bm25=False)
    for i, e in enumerate(res.get("entries", [])):
        if e.get("text") == gold:
            return i + 1
    return None


def _score(svc, corpus, top_k, ks) -> dict:
    ranks = []
    for item in corpus:
        ranks.append((_rank_of_gold(svc, item["query"], item["fact"], top_k),
                      item["overlap"]))

    def metrics(subset):
        if not subset:
            return None
        out = {f"recall@{k}": round(
            sum(1 for r, _ in subset if r is not None and r <= k) / len(subset), 3)
            for k in ks}
        out["mrr"] = round(
            sum((1.0 / r) for r, _ in subset if r is not None) / len(subset), 3)
        out["n"] = len(subset)
        return out

    med = sorted(o for _, o in ranks)[len(ranks) // 2]
    return {
        "all": metrics(ranks),
        "low_overlap": metrics([x for x in ranks if x[1] <= med]),
        "high_overlap": metrics([x for x in ranks if x[1] > med]),
        "overlap_median": round(med, 3),
    }


def _diagnose(svc) -> None:
    """Is the memory module M useful, identity (redundant with cosine), or noise?

    For each trained band, report mean cos(M(x), x) over its stored embeddings.
    ~1.0 => M learned (approx) identity, so M(query) ~= query and the neural
    score is redundant with the exact cosine score. Far from 1.0 => M(query) is
    off the data manifold (noise), which actively degrades the cosine ranking.
    """
    import torch
    import torch.nn.functional as F

    print("\n-- diagnostic: cos(M(x), x) per trained band "
          "(~1.0 = identity/redundant; low = noise) --")
    for b in svc._cms.bands:
        if not b.entries:
            continue
        b.memory.eval()
        sims = []
        with torch.no_grad():
            for e in b.entries:
                x = F.normalize(e.embedding.to(b.device).unsqueeze(0), p=2, dim=1)
                mx = F.normalize(b.memory(x.squeeze(0)).unsqueeze(0), p=2, dim=1)
                sims.append(F.cosine_similarity(x, mx).item())
        n = len(sims)
        mean = sum(sims) / n
        lo = min(sims)
        print(f"  {b.name:10} n={n:3d}  updates={b.update_count:3d}  "
              f"mean cos(M(x),x)={mean:+.3f}  min={lo:+.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=160, help="corpus size")
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--objective", default=None,
                    help="override every band's objective (l2|kv|neg_sim|huber) "
                         "to test whether a different in-regime objective helps")
    ap.add_argument("--diagnose", action="store_true",
                    help="also report per-band cos(M(x), x) identity-ness")
    args = ap.parse_args()
    ks = (1, 3, 5, 10)

    from pseudolife_memory.service import MemoryService

    corpus = build_corpus(args.n, args.seed)
    print(f"corpus: {len(corpus)} facts (seed={args.seed}); "
          f"overlap min/median/max = "
          f"{min(c['overlap'] for c in corpus):.2f}/"
          f"{sorted(c['overlap'] for c in corpus)[len(corpus)//2]:.2f}/"
          f"{max(c['overlap'] for c in corpus):.2f}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)  # file mode (no PG); cortex stays empty
        # Ingest everything: drop the surprise gate so the haystack is complete,
        # and keep auto_promote off so the cortex never shadows band retrieval.
        svc.config.memory.surprise_threshold = -1.0
        svc.config.memory.cortex.auto_promote = False
        svc._ensure_init()
        if args.objective:
            from pseudolife_memory.memory.miras.objectives import build_objective
            for b in svc._cms.bands:
                b.objective = build_objective(args.objective)
            print(f"objective override: every band -> {args.objective!r}")
        for c in corpus:
            svc.store(c["fact"], source="neural-bench")
        n_band = sum(len(b.entries) for b in svc._cms.bands)
        print(f"ingested {n_band} entries across "
              f"{len(svc._cms.bands)} bands; MLPs trained via store path\n")

        conditions = [
            ("OFF  (w=0.0, cosine)", 0.0, None),
            ("ON   (w=0.6, shipped)", 0.6, None),       # ramp left intact
            ("PURE (w=1.0, MLP-only)", 1.0, 0),         # warmup off -> full weight
        ]
        results = {"corpus_size": len(corpus), "seed": args.seed,
                   "top_k": args.top_k, "conditions": {}}
        for label, w, warmup in conditions:
            _set_blend(svc, w, warmup)
            r = _score(svc, corpus, args.top_k, ks)
            results["conditions"][label] = r
            a = r["all"]
            print(f"{label:26}  R@1 {a['recall@1']:.3f}  R@3 {a['recall@3']:.3f}  "
                  f"R@5 {a['recall@5']:.3f}  R@10 {a['recall@10']:.3f}  "
                  f"MRR {a['mrr']:.3f}")

        print(f"\n-- low-overlap subset (query~gold Jaccard <= "
              f"{results['conditions'][conditions[0][0]]['overlap_median']}, "
              f"where cosine is weakest) --")
        for label, _, _ in conditions:
            lo = results["conditions"][label]["low_overlap"]
            print(f"{label:26}  R@1 {lo['recall@1']:.3f}  R@5 {lo['recall@5']:.3f}  "
                  f"MRR {lo['mrr']:.3f}  (n={lo['n']})")

        off = results["conditions"]["OFF  (w=0.0, cosine)"]["all"]
        on = results["conditions"]["ON   (w=0.6, shipped)"]["all"]
        d_mrr = round(on["mrr"] - off["mrr"], 3)
        d_r5 = round(on["recall@5"] - off["recall@5"], 3)
        results["verdict"] = {"delta_mrr": d_mrr, "delta_recall@5": d_r5}
        print(f"\nVERDICT  shipped-ON minus OFF:  dMRR {d_mrr:+.3f}  "
              f"dRecall@5 {d_r5:+.3f}")
        print("  >0 => the neural blend helps ranking; ~0 => redundant with "
              "cosine; <0 => it hurts.")

        if args.diagnose:
            _set_blend(svc, 0.6, None)  # restore shipped weight for the probe
            _diagnose(svc)

        out_path = Path(__file__).parent / "results" / "neural_blend.json"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2))
        print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
