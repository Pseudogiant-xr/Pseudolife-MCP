"""Write the golden cases for the Rust port of the deep dream's pure half,
``memory/graph_consolidation.py`` (``src/graph_consolidation.rs``).

Every expected value comes from calling the real Python functions in this
checkout (CPython 3.11, the numpy it imports), so the Rust unit tests assert
against the oracle. Covered: CPython's int-set iteration order (the order
``entity_context_vectors`` sums embeddings in), the float32 context vectors
and cosines (bit patterns), ``candidate_pairs`` + ``partition_candidates``,
``slot_duplicate_candidates``, ``variant_tokens``, ``_name_contains``,
``_compound_halves``, ``junk_entities`` and ``exact_duplicate_pairs``.

The float32 goldens depend on which OpenBLAS ``sdot`` kernel numpy runs.
numpy's wheels bundle a DYNAMIC_ARCH OpenBLAS that picks a kernel per CPU
and honours ``OPENBLAS_CORETYPE``, so every float32-dependent case is
recorded once per distinct kernel in that build (``KERNELS``), each in a
child process with the core type forced, and filed under ``kernels``. The
Rust tests replay each kernel's cases against that kernel's port, whatever
the host. The host must be able to execute every kernel (AVX-512 for
SkylakeX).

Output: ``goldens/w2e-deep-dream.json``.

usage: python gen_w2e_deep_dream_goldens.py
"""
from __future__ import annotations

import json
import os
import random
import struct
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "w2e-deep-dream.json"

# Golden key -> (OPENBLAS_CORETYPE value, the core name OpenBLAS then reports).
# One entry per distinct sdot kernel in numpy's scipy-openblas build: its
# DYNAMIC_LIST builds Prescott (generic C dot, reported as "Katmai"), Nehalem,
# Sandybridge, Haswell and SkylakeX; every other core type aliases to one of
# these (``src/openblas_sdot.rs``).
KERNELS = {
    "prescott": ("Prescott", "Katmai"),
    "nehalem": ("Nehalem", "Nehalem"),
    "sandybridge": ("Sandybridge", "Sandybridge"),
    "haswell": ("Haswell", "Haswell"),
    "skylakex": ("SkylakeX", "SkylakeX"),
}

# Vector lengths for the raw sdot cases: every kernel's blocking boundary
# (4, 16, 32, 64) on both sides, plus the 1024-dim embedding width.
SDOT_LENGTHS = (0, 1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 31, 32, 33, 40, 47, 48, 63, 64,
                65, 95, 96, 97, 100, 127, 128, 129, 160, 191, 192, 255, 256, 257, 383,
                384, 1000, 1023, 1024, 1025, 1056, 1087, 1088, 2047, 2048)


def bits(v) -> list[str]:
    return [struct.pack("<f", float(x)).hex() for x in v]


def synthetic(np, i: int, dim: int):
    """A float32 vector both languages compute exactly: integer hashing,
    then a division by a power of two."""
    out = []
    for j in range(dim):
        h = (i * 2654435761 + j * 40503 + 12345) % 2**32
        out.append(np.float32((h % 20011) - 10005) / np.float32(4096.0))
    return np.asarray(out, dtype=np.float32)


def sdot_input(np, seed: int, n: int):
    """A float32 vector with a wide exponent spread (so the summation order
    shows in the result) that both languages build exactly: a 23-bit signed
    integer times a power of two."""
    out = []
    for j in range(n):
        h = (seed * 2654435761 + j * 40503 + 12345) % 2**32
        m = (h % (1 << 23)) - (1 << 22)
        e = (h >> 23) % 24
        out.append(np.float32(m) * np.float32(2.0 ** -(e + 10)))
    return np.asarray(out, dtype=np.float32)


def digest(v) -> str:
    import hashlib
    return hashlib.sha256(b"".join(struct.pack("<f", float(x)) for x in v)).hexdigest()


def kernel_payload() -> dict:
    """Every case whose value depends on the sdot kernel, computed under the
    core type this process was started with."""
    sys.path.insert(0, str(REPO))
    import numpy as np

    from pseudolife_memory.memory import graph_consolidation as gc

    rng = random.Random(20261021)
    out: dict = {}

    # Raw np.dot results (OpenBLAS cblas_sdot) over every blocking boundary.
    sdot = []
    for n in SDOT_LENGTHS:
        x, y = sdot_input(np, 2 * n + 1, n), sdot_input(np, 2 * n + 2, n)
        sdot.append({"n": n, "xy": bits([np.dot(x, y)])[0], "xx": bits([np.dot(x, x)])[0],
                     "norm": bits([np.float32(np.linalg.norm(y))])[0]})
    out["sdot"] = sdot

    # Context vectors and cosines over synthetic float32 embeddings both
    # sides generate (``synthetic``), pinned by digest and bit pattern.
    numeric = []
    for dim in (1024, 96, 1000, 77):
        entries = [{"id": i * 7 % 61 + 1, "text": f"note {i}",
                    "embedding": synthetic(np, i, dim)} for i in range(1, 41)]
        entities = [{"id": k, "canonical": f"e{k}", "display": f"Entity {k}"} for k in range(1, 9)]
        traces = {}
        for k in range(1, 9):
            traces[f"e{k}"] = [entries[rng.randrange(len(entries))]["id"]
                               for _ in range(rng.randint(1, 12))]
        vectors, mentions = gc.entity_context_vectors(entities, entries, traces, min_mentions=2)
        near = gc.candidate_pairs(vectors, [], entities, {}, mentions,
                                  min_similarity=-1.0, top_k=12, max_support_overlap=2.0)
        cos = {f"{a}-{b}": struct.pack("<f", np.float32(np.dot(vectors[a], vectors[b]))).hex()
               for a in sorted(vectors) for b in sorted(vectors) if a < b}
        numeric.append({
            "dim": dim,
            "entry_ids": [e["id"] for e in entries],
            "traces": traces,
            "vectors": {str(k): digest(v) for k, v in sorted(vectors.items())},
            "mentions": {str(k): sorted(v) for k, v in sorted(mentions.items())},
            "cosines": cos,
            "candidates": near,
        })
    out["numeric"] = numeric

    # candidate_pairs filters + partition_candidates on a small graph.
    nrng = np.random.default_rng(7)
    ents = [
        {"id": 1, "canonical": "kafka-broker", "display": "Kafka Broker"},
        {"id": 2, "canonical": "kafka-broker-cluster", "display": "Kafka Broker Cluster"},
        {"id": 3, "canonical": "model-e4b", "display": "Model E4B"},
        {"id": 4, "canonical": "model-e2b", "display": "Model E2B"},
        {"id": 5, "canonical": "tide-gauge", "display": "Tide Gauge"},
        {"id": 6, "canonical": "kelp-farm", "display": "Kelp Farm"},
        {"id": 7, "canonical": "harbor", "display": "Harbor"},
    ]
    base = nrng.standard_normal(64).astype(np.float32)
    vecs = {}
    for e in ents:
        v = (base + 0.15 * nrng.standard_normal(64)).astype(np.float32)
        vecs[e["id"]] = v / np.float32(np.linalg.norm(v))
    mentions = {1: frozenset({1, 2}), 2: frozenset({1, 2, 3}), 3: frozenset({4}),
                4: frozenset({5}), 5: frozenset({6, 7}), 6: frozenset({6, 8}),
                7: frozenset({9})}
    edges = [{"src_id": 5, "dst_id": 7, "relation": "uses"}]
    scope = {5: ["proj-a"], 6: ["proj-b"], 7: ["proj-a"]}
    pairs = gc.candidate_pairs(vecs, edges, ents, scope, mentions, min_similarity=0.0,
                               top_k=50, max_support_overlap=0.8,
                               dismissed={("harbor", "kelp-farm")}, pending_pairs=set(),
                               excluded_ids=set())
    merges, links = gc.partition_candidates(pairs, ents, edges, merge_min_similarity=0.0,
                                            fact_counts={2: 3})
    # Junk-owned / lesson-only exclusion and pending proposals: dropped
    # before the ranking, so a later pair takes the freed slot.
    excluded = []
    for excl, pending, top_k in (({2}, set(), 50), ({1, 5}, {frozenset((3, 4))}, 50),
                                 ({3}, {frozenset((1, 2))}, 2), ({1, 2, 3, 4, 5, 6, 7}, set(), 50)):
        got = gc.candidate_pairs(vecs, edges, ents, scope, mentions, min_similarity=0.0,
                                 top_k=top_k, max_support_overlap=0.8,
                                 dismissed={("harbor", "kelp-farm")}, pending_pairs=pending,
                                 excluded_ids=excl)
        excluded.append({"excluded_ids": sorted(excl),
                         "pending_pairs": sorted(sorted(p) for p in pending),
                         "top_k": top_k, "pairs": got})
    out["partition"] = {
        "entities": ents, "vectors": {str(k): bits(v) for k, v in vecs.items()},
        "mentions": {str(k): sorted(v) for k, v in mentions.items()}, "edges": edges,
        "scope": {str(k): v for k, v in scope.items()}, "pairs": pairs,
        "merges": merges, "links": links, "excluded": excluded,
    }

    # slot_duplicate_candidates.
    recs = []
    for i, (ent, attr) in enumerate([("Host A", "os"), ("Host B", "os"), ("Host A", "distro"),
                                     ("arxiv:2602-05665", "title"), ("arxiv:2602-05666", "title"),
                                     ("", "orphan"), ("Host C", "os")]):
        emb = (base + 0.05 * nrng.standard_normal(64)).astype(np.float32)
        key = f"{ent.lower().replace(' ', '-')}|{attr}"
        recs.append({"key": key, "entity": ent, "attribute": attr, "value": f"v{i}",
                     "embedding": None if i == 6 else emb})
    dups = gc.slot_duplicate_candidates(recs, min_similarity=0.0, top_k=20,
                                        dismissed={("host-a|os", "host-b|os")})
    out["slot_duplicates"] = {
        "records": [{**{k: v for k, v in r.items() if k != "embedding"},
                     "embedding": None if r["embedding"] is None else bits(r["embedding"])}
                    for r in recs],
        "expected": dups,
    }
    return out


def child_main(kernel: str) -> int:
    """Print this kernel's payload as JSON, after checking OpenBLAS really
    runs the forced core type."""
    import numpy  # noqa: F401  (loads OpenBLAS for threadpoolctl)
    import threadpoolctl

    want = KERNELS[kernel][1]
    info = [i for i in threadpoolctl.threadpool_info() if i.get("internal_api") == "openblas"]
    got = info[0]["architecture"] if info else None
    if got != want:
        print(f"OPENBLAS_CORETYPE={KERNELS[kernel][0]} gave core {got!r}, not {want!r}",
              file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps({"core": got, "version": info[0]["version"],
                                 "payload": kernel_payload()}, default=list))
    return 0


def main() -> int:
    sys.path.insert(0, str(REPO))
    import numpy  # noqa: F401  (loads OpenBLAS for threadpoolctl)
    import threadpoolctl

    from pseudolife_memory.memory import graph_consolidation as gc

    rng = random.Random(20261020)
    out: dict = {}

    # CPython set iteration order over ints.
    orders = []
    for n in (0, 1, 5, 9, 20, 47, 130):
        for scale in (60, 5000, 10**9):
            items = [rng.randint(1, scale) for _ in range(n)]
            orders.append([items, list({i for i in items})])
    out["set_order"] = orders

    info = [i for i in threadpoolctl.threadpool_info() if i.get("internal_api") == "openblas"]
    out["openblas"] = {"version": info[0]["version"], "host_core": info[0]["architecture"]}
    kernels = {}
    for name, (coretype, _) in KERNELS.items():
        env = {**os.environ, "OPENBLAS_CORETYPE": coretype}
        res = subprocess.run([sys.executable, __file__, "--kernel", name], env=env,
                             capture_output=True, text=True, check=False)
        if res.returncode != 0:
            sys.stderr.write(res.stderr)
            return res.returncode
        child = json.loads(res.stdout)
        if child["version"] != out["openblas"]["version"]:
            print(f"{name}: OpenBLAS {child['version']} != {out['openblas']['version']}",
                  file=sys.stderr)
            return 2
        kernels[name] = child["payload"]
    out["kernels"] = kernels

    names = ["Gemma 3 E4B", "gemma-3-e2b", "Qwen3-8B", "qwen3 8b", "Q4_K_M", "q4_k_xl",
             "UD-Q4_K_XL", "model q4_0", "v0.2.0", "release 3.6", "26B", "4.5MB", "1.2KB",
             "E4Bx", "xE4B", "Q4-K", "Q4_K_XS", "llama.cpp", "", "plain", "７B", "KKB",
             "q4_k_s+q4_k_l"]
    out["variant_tokens"] = [[n, sorted(gc.variant_tokens(n))] for n in names]
    pairs = [("Kafka Broker", "Kafka Broker Cluster"), ("kafka", "kafka broker"),
             ("update.ps1", "ops/update.ps1"), ("a<->b", "a b"), ("Tide Gauge", "Kelp Farm"),
             ("gpt-5.6-luna", "gpt 5.6 luna max"), ("abc def", "abcdef"),
             ("Straße Map", "strasse map"), ("x y", "x-y-z")]
    out["name_contains"] = [[a, b, gc._name_contains(a, b)] for a, b in pairs]
    comp = ["pg+extractor", "codex-cli / installer", "origin/master", "a + ", "ops/backup.ps1 + x",
            "x+y.py", "+", "a / b", "a / b / c", "-- + !!", "a+b+c", "foo /bar"]
    out["compound_halves"] = [[c, gc._compound_halves(c)] for c in comp]

    jents = [
        {"id": 1, "display": "42"}, {"id": 2, "display": "QQ"}, {"id": 3, "display": " ok "},
        {"id": 4, "display": "stale 0.8"}, {"id": 5, "display": "a<->b"},
        {"id": 6, "display": "alpha, beta"}, {"id": 7, "display": "pg+extractor"},
        {"id": 8, "display": "llama.cpp"}, {"id": 9, "display": "llama.attribute"},
        {"id": 10, "display": "Payments DB"}, {"id": 11, "display": "Z"},
        {"id": 12, "display": "pg"}, {"id": 13, "display": "extractor"},
        {"id": 14, "display": "llama"}, {"id": 15, "display": "cortex._norm_key"},
        {"id": 16, "display": "gpt-5.6"}, {"id": 17, "display": "gpt-5"},
        {"id": 18, "display": "llama.Pending"}, {"id": 19, "display": "٤٢"},
        {"id": 20, "display": "host.docker.internal"}, {"id": 21, "display": "None"},
    ]
    jedges = [{"src_id": 2, "dst_id": 10}, {"src_id": 2, "dst_id": 13},
              {"src_id": 11, "dst_id": 10}, {"src_id": 6, "dst_id": 10},
              {"src_id": 6, "dst_id": 13}, {"src_id": 6, "dst_id": 14}]
    known = frozenset({"pg", "extractor", "llama", "payments-db", "gpt-5", "host"})
    junk_cases = []
    for max_degree, kn in ((1, known), (0, known), (2, frozenset())):
        for e in jents:
            e.setdefault("canonical", e["display"])
        junk_cases.append({"max_degree": max_degree, "known_norms": sorted(kn),
                           "expected": gc.junk_entities(jents, jedges, max_degree=max_degree,
                                                        known_norms=kn)})
    out["junk"] = {"entities": jents, "edges": jedges, "cases": junk_cases}

    dents = [{"id": 1, "canonical": "kafka-broker", "display": "Kafka Broker"},
             {"id": 2, "canonical": "broker-kafka", "display": "Broker Kafka"},
             {"id": 3, "canonical": "kafka-broker-x", "display": "kafka_broker"},
             {"id": 4, "canonical": "a-b", "display": "a<->b"},
             {"id": 5, "canonical": "b-a", "display": "b<->a"},
             {"id": 6, "canonical": "x", "display": "!!"},
             {"id": 7, "canonical": "y", "display": "??"},
             {"id": 8, "canonical": "z", "display": "Tide Gauge"},
             {"id": 9, "canonical": "w", "display": "tide-GAUGE"}]
    dedges = [{"src_id": 2, "dst_id": 8}, {"src_id": 9, "dst_id": 8}, {"src_id": 9, "dst_id": 1}]
    out["exact_dups"] = {"entities": dents, "edges": dedges,
                         "expected": gc.exact_duplicate_pairs(dents, dedges)}

    TARGET.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=list) + "\n",
                      encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--kernel":
        raise SystemExit(child_main(sys.argv[2]))
    raise SystemExit(main())
