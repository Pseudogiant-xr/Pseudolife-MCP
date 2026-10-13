"""Linux fp32 daemon scaling, cache arms and same-head repeated controls."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import threading
import time

from .common import ROOT, controls, distribution, lease_gate, memory_tree, provenance, write_result
from .daemon import MCP, SEED, TOPICS, corpus, disposable_database, launched_daemon, private_directory, time_call

DIMENSION = 1024
CACHE_SIZE = 1024
POOL_SIZE = 1100
THREAD_POLICIES = ("one", "production")


def cold_pool(count=POOL_SIZE):
    return [{"query": f"Project {TOPICS[i % len(TOPICS)]} calibration probe {i:05d}", "top_k": 8}
            for i in range(count)]


def vector(seed):
    rng = random.Random(seed)
    values = [rng.uniform(-1, 1) for _ in range(DIMENSION)]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def configuration(bank_size):
    # Preserve the default flat band's cadence and retention. Its normal cap
    # is 5250; this declared stress arm raises only capacity to keep 20k rows.
    return ("embedding:\n  device: cpu\n  backend: torch\n  cpu_dtype: fp32\n"
            "memory:\n  dream:\n    enabled: false\n  miras:\n    preset: custom\n    bands:\n"
            f"      - name: flat\n        max_entries: {max(5250, bank_size)}\n"
            "        update_interval: 1000000000\n        promotion_access_count: 1000000000\n"
            "        promotion_surprise: 1.1\n        retention_policy: balanced\n"
            "updates:\n  check_releases: false\n")


def seed_bank(dsn, size):
    import psycopg
    from pseudolife_memory.storage.schema import assert_disposable_database
    # Schema was initialized by the real daemon. COPY does no DDL and never
    # invokes storage/model construction in the measurement parent.
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        assert_disposable_database(connection)
        if connection.execute("SELECT count(*) FROM entries").fetchone()[0]:
            raise RuntimeError("synthetic seed requires empty disposable bank")
        with connection.cursor().copy(
                "COPY entries (band,text,embedding,surprise,ts,access_count,source) FROM STDIN") as copy:
            for i, entry in enumerate(corpus(size)):
                embedding = "[" + ",".join(format(v, ".9g") for v in vector(SEED + i)) + "]"
                copy.write_row(("flat", entry["text"], embedding, 0.5, 1700000000 + i,
                                0, "synthetic-baseline"))
        if connection.execute("SELECT count(*) FROM entries").fetchone()[0] != size:
            raise RuntimeError("synthetic seed count mismatch")


def observe(client):
    response = client.client.get("/health")
    response.raise_for_status()
    health = response.json()
    if health.get("schema") != 53:
        raise RuntimeError("unexpected oracle schema")
    observation = health["baseline_observation"]
    if observation["parameter_dtypes"] != ["torch.float32"]:
        raise RuntimeError("baseline requires actual fp32 model parameters")
    return observation


def rss_samples(process, count=10):
    samples = []
    for _ in range(count):
        samples.append(memory_tree(process.verified_runtime_pid)["rss_bytes"])
        time.sleep(0.05)
    return samples


def search_arm(client, process, queries, expected_encodes):
    before = observe(client)
    samples, rss = [], []
    stop = threading.Event()

    def sample_memory():
        while not stop.is_set():
            rss.append(memory_tree(process.verified_runtime_pid)["rss_bytes"])
            stop.wait(0.02)

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    try:
        for query in queries:
            elapsed, result = time_call(lambda: client.call("memory_search", query))
            if not isinstance(result.get("entries"), list):
                raise RuntimeError("search response shape mismatch")
            samples.append(elapsed)
    finally:
        stop.set()
        sampler.join(timeout=5)
        if sampler.is_alive():
            raise RuntimeError("RSS sampler failed to stop")
    after = observe(client)
    actual_encodes = after["model_encode_calls"] - before["model_encode_calls"]
    if actual_encodes != expected_encodes:
        raise RuntimeError("cache arm did not take expected real embedding path")
    return {"latency_ms": samples, "load_rss_bytes": rss,
            "model_encode_calls": actual_encodes, "before": before, "after": after}


def run_repeat(args, size, threads, repeat):
    import psutil
    resource = lease_gate(args.board_checked_at,
                          offline_resource_checked_at=args.offline_resource_checked_at)
    cpu = psutil.cpu_percent(interval=1)
    launch = dict(source_root=args.source_root, configuration=configuration(size),
                  thread_policy=threads, child_module="evals.rust_baseline.scaling_child")
    with disposable_database() as dsn:
        with private_directory() as private:
            with launched_daemon(dsn, private, **launch) as (process, url, empty_cleanup):
                client = MCP(url)
                try:
                    client.initialize()
                    client.call("memory_search", {"query": "baseline model initialization", "top_k": 8})
                    empty_observation = observe(client)
                    if empty_observation["resident_entries"] != 0:
                        raise RuntimeError("idle baseline bank is not empty")
                    idle = rss_samples(process)
                finally:
                    client.client.close()
        seed_bank(dsn, size)
        print(json.dumps({"seeded_entries": size, "thread_policy": threads, "repeat": repeat}), flush=True)
        with private_directory() as private:
            with launched_daemon(dsn, private, **launch) as (process, url, cleanup):
                client = MCP(url)
                try:
                    initialized = client.initialize()
                    warm = [{"query": f"Project {topic} calibration", "top_k": 8} for topic in TOPICS]
                    for query in warm:
                        client.call("memory_search", query)
                    observation = observe(client)
                    if observation["resident_entries"] != size:
                        raise RuntimeError("hydration did not retain the requested bank size")
                    if observation["entry_vector_bytes"] != size * DIMENSION * 4:
                        raise RuntimeError("hydrated vector shape/dtype mismatch")
                    if threads == "one" and observation["torch_threads"] != 1:
                        raise RuntimeError("one-thread configuration mismatch")
                    hydrated = rss_samples(process)
                    arms = {"warm": search_arm(client, process,
                            [warm[i % len(warm)] for i in range(args.samples)], 0),
                            "cold": search_arm(client, process, cold_pool()[:args.samples], args.samples)}
                    result = {"repeat": repeat, "bank_size": size, "thread_policy": threads,
                              "resource_check": resource, "host_cpu_percent_before": cpu,
                              "protocol": initialized["protocolVersion"], "seeded_database_entries": size,
                              "hydrated_entries": observation["resident_entries"],
                              "configured_flat_capacity": max(5250, size), "idle_rss_bytes": idle,
                              "hydrated_rss_bytes": hydrated, "observation": observation,
                              "empty_observation": empty_observation, "arms": arms,
                              "actual_child_runtime": cleanup["actual_child_runtime"],
                              "cleanup": cleanup, "empty_cleanup": empty_cleanup}
                finally:
                    client.client.close()
        result["cleanup"]["database_dropped"] = True
    return result


def summarize(runs):
    cells, vectors = [], []
    for threads in sorted({run["thread_policy"] for run in runs}):
        selected = [run for run in runs if run["thread_policy"] == threads]
        sizes = sorted({run["bank_size"] for run in selected})
        for size in sizes:
            blocks = [run for run in selected if run["bank_size"] == size]
            for arm in ("warm", "cold"):
                cells.append({"bank_size": size, "thread_policy": threads, "arm": arm,
                    "search_ms": distribution([v for r in blocks for v in r["arms"][arm]["latency_ms"]]),
                    "search_control": controls([r["arms"][arm]["latency_ms"] for r in blocks]),
                    **{name: {"bytes": distribution([v for r in blocks for v in r[name]]),
                              "control": controls([r[name] for r in blocks])}
                       for name in ("idle_rss_bytes", "hydrated_rss_bytes")},
                    "load_rss_bytes": {"bytes": distribution([v for r in blocks for v in r["arms"][arm]["load_rss_bytes"]]),
                                       "control": controls([r["arms"][arm]["load_rss_bytes"] for r in blocks])}})
        if len(sizes) == 2:
            small, large = sizes
            deltas = []
            hydrated_deltas = []
            for repeat in sorted({run["repeat"] for run in selected}):
                pair = {r["bank_size"]: r for r in selected if r["repeat"] == repeat}
                hyd = {size: statistics.median(pair[size]["hydrated_rss_bytes"]) for size in sizes}
                idle = {size: statistics.median(pair[size]["idle_rss_bytes"]) for size in sizes}
                hydrated_deltas.append(hyd[large] - hyd[small])
                deltas.append((hyd[large] - idle[large]) - (hyd[small] - idle[small]))
            vectors.append({"thread_policy": threads, "small_bank_size": small, "large_bank_size": large,
                            "vector_lower_bound_delta_bytes": (large - small) * DIMENSION * 4,
                            "hydrated_rss_delta_bytes": distribution(hydrated_deltas),
                            "idle_adjusted_hydration_delta_bytes": distribution(deltas),
                            "idle_adjusted_delta_control": controls([[v] for v in deltas]),
                            "attribution": "resident scaling includes vectors, index, entry objects, text and allocator effects"})
    return {"cells": cells, "resident_scaling": vectors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--board-checked-at")
    parser.add_argument("--offline-resource-checked-at")
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("daemon scaling baseline requires Linux")
    if not 1 <= args.samples <= POOL_SIZE or args.repeats < 3 and not args.smoke:
        parser.error("require 1..1100 samples and at least three baseline repeats")
    identity = provenance(source_root=args.source_root)
    if identity["source_head"] != args.expected_head or identity["source_schema"] != 53 or identity["source_dirty"]:
        parser.error("clean pinned oracle at schema 53 required")
    sizes, policies = ((2,), ("one",)) if args.smoke else ((2000, 20000), THREAD_POLICIES)
    if args.smoke:
        args.repeats, args.samples = 1, 1
    runs = []
    for threads in policies:
        for repeat in range(args.repeats):
            for size in sizes:
                runs.append(run_repeat(args, size, threads, repeat))
                print(json.dumps({"completed_repeat": repeat, "bank_size": size, "thread_policy": threads}), flush=True)
    write_result(args.out, {"schema": 2, "status": "plumbing-smoke" if args.smoke else "linux-daemon-scaling-baseline",
        "provenance": identity, "seed": SEED, "runs": runs, "summary": summarize(runs),
        "method": {"samples_per_arm_repeat": args.samples, "repeats": args.repeats,
            "bank_sizes": list(sizes), "thread_policies": list(policies), "dtype": "fp32", "device": "cpu",
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B", "vector_dimension": DIMENSION, "vector_dtype_width": 4,
            "cold_pool_size": POOL_SIZE, "cold_pool_sha256": hashlib.sha256(json.dumps(cold_pool(), sort_keys=True).encode()).hexdigest(),
            "embedding_cache_size": CACHE_SIZE, "cold_selection": "first unique texts, disjoint from warmup; each encode verified",
            "rss_idle": "initialized real model on empty bank; separate owned daemon on same disposable database",
            "rss_hydrated": "all requested entries resident, cosine indexes warmed, before timed search",
            "rss_load_interval_s": 0.02, "seed_vectors": "deterministic seeded random normalized synthetic fp32 vectors",
            "production_threads": "OMP_NUM_THREADS and MKL_NUM_THREADS unset, as daemon image; resolved torch count recorded",
            "capacity": "default flat band properties; max_entries raised to 20000 for stress bank"},
        "limitations": ["Generated vectors measure storage and scoring scale, not semantic ranking quality.",
            "RSS includes all resident structures and allocator effects; vector tensor bytes are an exact lower bound, not exclusive attribution.",
            "CPU-only real query embeddings, fp32 in both thread arms; production dtype auto is outside this comparison.",
            "Sequential MCP requests; PostgreSQL RSS excluded; ambient host activity uncontrolled.",
            "Three same-head controls provide descriptive median ranges, not confidence intervals; no speedup claim.",
            "Owned process termination and fresh bank drop verified; no graceful-shutdown or durability assertion."]})


if __name__ == "__main__":
    main()
