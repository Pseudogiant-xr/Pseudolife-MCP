"""Allocator probe — does glibc hand back the RSS the embedder's encodes
ratchet up? (2026-09-23)

The daemon OOM investigation (2026-09-23, ``results/embedder-cpu-bf16-probe-
20260923.json``) saw a single-thread process's resting RSS climb after
large encodes — 2,820 -> 3,422 -> 3,991 MB across 4/8/16 x ~512-token fp32
encodes, then back to 2,832 MB before the next step. The hypothesis under
test: glibc keeps freed tensor buffers in the heap (its dynamic mmap
threshold rises to 32 MiB after the first large free, so later buffers stop
being mmapped), interior free space is only returned by ``malloc_trim``,
and ``MALLOC_ARENA_MAX=2`` (fewer arenas, not less retention per arena)
does not address it.

This probe measures, per arm, in a THROWAWAY container from the daemon
image (never the production daemon), whether

* ``malloc_trim(0)`` (ctypes) after every encode burst, and/or
* a fixed ``MALLOC_MMAP_THRESHOLD_`` (which also disables the dynamic
  threshold and the dynamic trim threshold)

return the ratcheted memory, and what each costs in latency: encode burst
time, the trim call itself, and the short-query latency right after a trim
(pages it released fault back in). Every arm runs with
``MALLOC_ARENA_MAX=2`` (what the daemon ships with after the OOM fix)
except the ``defarena*`` arms, which drop it: they reproduce the original
observation's allocator (its launch did not record the env) and show how
trim interacts with per-thread arenas.

Workloads (both through the checked-out ``EmbeddingPipeline``, batch_size
16 = the daemon's effective embedding config, cache off):

* ``single``: one Python thread; encodes of 4, 8, 16, 16, 4 x ~512-token
  chunks, then a 48-chunk ingest in slices of 8 (``INGEST_ENCODE_BATCH``);
  one burst each.
* ``threads4``: three bursts of 4 threads x one 8 x ~256-token encode each
  (the OOM probe's concurrent burst), then two bursts of 4 threads x 8
  short queries each (a search burst). Its threads are new per burst, and
  the reading right after ``join()`` still counts what the exiting threads
  release a moment later (up to ~1 GB high), so read its retention at the
  next step's ``anon_before_mb``. ``pool4`` below is the daemon's shape.

Both then time 20 short queries, ``gc.collect()``, and one final
``malloc_trim(0)`` in every arm (what a single idle-time trim would still
release), followed by 5 more timed queries.

Their timings compare arms across containers, which a loaded host turns
to noise (the 2026-09-23 sweep ran at loadavg 6-9 on 6 vCPUs). Latency is
measured by a third workload, run only when named:

* ``pairs``: one process alternates "trim first" and "don't" in ABBA
  order before identical trials (5 short queries, then a 4 x ~512-token
  encode), so load drift cancels within each adjacent pair; the summary
  sets the paired deltas beside the gap between consecutive no-trim trials
  (the noise floor) and a sign test. Besides wall time it counts minor
  page faults and kernel CPU per trial (``getrusage``, all threads): what
  a trim costs the next request is re-faulting the pages it returned,
  which is kernel work that host load barely moves, while wall time under
  load swings several-fold. Its arms (``ctrl``, ``mmap128k``) differ only
  in allocator env, so the mmap cost is a cross-container comparison of
  the no-trim trials — on the same load-robust counters.
* ``pool4`` (only when named): the ``threads4`` bursts on four persistent
  workers (a thread pool created and warmed before the base reading, as the
  daemon's anyio threadpool is), with a settled reading one second after
  each burst (``anon_settled_mb``). Arms: ``ctrl``, ``trim``, ``defarena``,
  ``defarena_trim``.

Usage (repo root; needs Docker and the daemon image — the model is baked
into the image at /opt/hf, so the containers run with ``--network none``):

    python evals/allocator_trim_probe.py \
        --image pseudolife-daemon:0.15.0 --commit <sha> \
        --out evals/results/allocator-trim-probe-<date>.json
    python evals/allocator_trim_probe.py --workloads pairs --replicates 4 \
        --image pseudolife-daemon:0.15.0 --commit <sha> \
        --out evals/results/allocator-trim-latency-<date>.json
    python evals/allocator_trim_probe.py --workloads pool4 --replicates 2 \
        --image pseudolife-daemon:0.15.0 --commit <sha> \
        --out evals/results/allocator-trim-pool-<date>.json

``--commit`` is exported with ``git archive`` into a temp dir and mounted
read-only as /src ahead of the image's own copy (``-w /tmp`` keeps the
image's /app package from shadowing PYTHONPATH). The artifact is rewritten
after every run so an interrupted sweep keeps what it measured, and an
existing ``--out`` is refused — canonical results are never rewritten.

Inside the container the same file runs as ``python - inner`` and prints
one ``@@RESULT@@``-prefixed JSON line.
"""
from __future__ import annotations

import ctypes
import gc
import json
import os
import sys
import threading
import time

RESULT_MARK = "@@RESULT@@"
CHUNK = ("memory entry text about the daemon and its bank, a long operator "
         "manual paragraph with enough words to fill a chunk ") * 40
QUERIES = [
    "how do I deploy the daemon safely",
    "what reranker is deployed and why is it off",
    "which schema version does the bank run",
    "why did the daemon get OOM killed",
    "what is the flat band preset",
    "how are stale facts quarantined",
    "where are backups mirrored",
    "what does the dream cursor do",
    "which extractor does dream consolidation use",
    "how do I restore a bank from backup",
]


# ── in-container side ───────────────────────────────────────────────────
class _MallInfo2(ctypes.Structure):
    _fields_ = [(name, ctypes.c_size_t) for name in (
        "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
        "fsmblks", "uordblks", "fordblks", "keepcost")]


def _libc():
    libc = ctypes.CDLL("libc.so.6")
    libc.malloc_trim.argtypes = [ctypes.c_size_t]
    libc.malloc_trim.restype = ctypes.c_int
    libc.mallinfo2.restype = _MallInfo2
    libc.gnu_get_libc_version.restype = ctypes.c_char_p
    return libc


def _status() -> dict:
    out = {}
    with open("/proc/self/status") as fh:
        for line in fh:
            key, _, value = line.partition(":")
            if key in ("VmRSS", "VmHWM", "RssAnon", "RssFile"):
                out[key] = int(value.split()[0]) // 1024
    return {"rss_mb": out["VmRSS"], "anon_mb": out["RssAnon"],
            "file_mb": out["RssFile"], "hwm_mb": out["VmHWM"]}


def _reset_peak() -> None:
    with open("/proc/self/clear_refs", "w") as fh:
        fh.write("5")


def _usage() -> tuple[float, float, int]:
    """Process-wide (every thread) user CPU, kernel CPU, minor faults."""
    import resource

    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime, ru.ru_stime, ru.ru_minflt


def _usage_delta(before: tuple, after: tuple) -> dict:
    return {"cpu_user_s": round(after[0] - before[0], 3),
            "cpu_sys_s": round(after[1] - before[1], 3),
            "minflt": after[2] - before[2]}


def _fault_cost(mb: int = 256, repeats: int = 5) -> dict:
    """What one minor fault on fresh anonymous memory costs here: a single
    thread touches one byte per page of a new ``mb`` mapping (numpy's
    strided write, no OpenMP spin), so wall and kernel time are the fault
    path's. A plain anonymous mmap, not a numpy allocation: numpy madvises
    large arrays onto huge pages, and the heap the embedder refaults takes
    4 KiB faults (THP is "madvise" on the reference host)."""
    import mmap

    import numpy as np

    runs = []
    for _ in range(repeats):
        region = mmap.mmap(-1, mb * 1024 * 1024)
        buf = np.frombuffer(region, dtype=np.uint8)
        u0, t0 = _usage(), time.perf_counter()
        buf[::4096] = 1
        wall = time.perf_counter() - t0
        delta = _usage_delta(u0, _usage())
        del buf
        region.close()
        faults = max(delta["minflt"], 1)
        runs.append({"minflt": delta["minflt"],
                     "wall_us_per_fault": round(wall * 1e6 / faults, 3),
                     "sys_us_per_fault": round(delta["cpu_sys_s"] * 1e6 / faults, 3)})
    try:
        with open("/sys/kernel/mm/transparent_hugepage/enabled") as fh:
            thp = fh.read().strip()
    except OSError:
        thp = None
    return {"buffer_mb": mb, "transparent_hugepage": thp, "runs": runs,
            "wall_us_per_fault_min": min(r["wall_us_per_fault"] for r in runs)}


def _run_threads(target, args_list) -> None:
    """One fresh thread per args tuple, all started, then all joined. The
    first worker error is re-raised once every thread has finished: a raw
    thread's exception only reaches threading.excepthook, and a burst that
    lost a worker would otherwise be measured as if it had run (PR #347
    review, 2026-09-24)."""
    errors: list[BaseException] = []

    def run(*args) -> None:
        try:
            target(*args)
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=a) for a in args_list]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    if errors:
        raise errors[0]


def _loadavg() -> list[float]:
    with open("/proc/loadavg") as fh:
        return [float(x) for x in fh.read().split()[:3]]


def _inner() -> int:
    libc = _libc()

    def mallinfo() -> dict:
        info = libc.mallinfo2()
        mb = 1024 * 1024
        return {"arena_mb": round(info.arena / mb, 1),
                "mmapped_mb": round(info.hblkhd / mb, 1),
                "mmapped_chunks": info.hblks,
                "in_use_mb": round(info.uordblks / mb, 1),
                "free_mb": round(info.fordblks / mb, 1),
                "top_releasable_mb": round(info.keepcost / mb, 1)}

    def trim() -> dict:
        before = _status()
        t0 = time.perf_counter()
        released = libc.malloc_trim(0)
        ms = (time.perf_counter() - t0) * 1000
        after = _status()
        return {"ms": round(ms, 2), "released": bool(released),
                "anon_before_mb": before["anon_mb"],
                "anon_after_mb": after["anon_mb"],
                "rss_after_mb": after["rss_mb"],
                "freed_mb": before["anon_mb"] - after["anon_mb"],
                "mallinfo_after": mallinfo()}

    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    import torch

    torch.set_num_threads(int(os.environ.get("THREADS", "3")))
    import pseudolife_memory
    from pseudolife_memory.memory import embedding
    from pseudolife_memory.utils.config import EmbeddingConfig

    workload = os.environ["PROBE_WORKLOAD"]
    do_trim = os.environ.get("PROBE_TRIM") == "1"
    result = {
        "package": pseudolife_memory.__file__,
        "glibc": libc.gnu_get_libc_version().decode(),
        "torch": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "env": {k: os.environ[k] for k in sorted(os.environ)
                if k.startswith("MALLOC_") or k in (
                    "PSEUDOLIFE_EMBEDDING_CPU_DTYPE", "PROBE_WORKLOAD",
                    "PROBE_TRIM", "THREADS")},
        "loadavg_start": _loadavg(),
    }
    t0 = time.perf_counter()
    pipe = embedding.EmbeddingPipeline(
        EmbeddingConfig(device="cpu", batch_size=16, cache_size=0))
    result["describe"] = pipe.describe()
    result["load_s"] = round(time.perf_counter() - t0, 2)
    result["after_load"] = _status()
    log(f"loaded {pipe.describe()} {result['after_load']}")

    def timed_queries(n: int) -> list[float]:
        out = []
        for i in range(n):
            t = time.perf_counter()
            pipe.encode_query(QUERIES[i % len(QUERIES)])
            out.append(round((time.perf_counter() - t) * 1000, 1))
        return out

    pool = None
    if workload == "pool4":
        # Persistent workers, as the daemon's anyio threadpool has: created
        # and warmed (each its own thread, via the barrier) before the base
        # reading, so no burst pays thread start-up or sheds its teardown.
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=4)
        gate = threading.Barrier(4)

        def warm(k: int) -> None:
            gate.wait()
            pipe.encode_query(QUERIES[k])

        list(pool.map(warm, range(4)))

    result["warm_query_ms"] = timed_queries(10)
    result["base"] = _status()
    result["base_mallinfo"] = mallinfo()
    log(f"base {result['base']} {result['base_mallinfo']}")

    def enc_threads() -> None:
        def work(k: int) -> None:
            pipe.encode([(f"concurrent request text {k}-{i} ") * 40
                         for i in range(8)])
        _run_threads(work, [(k,) for k in range(4)])

    def query_threads() -> None:
        def work(k: int) -> None:
            for i in range(8):
                pipe.encode_query(f"{QUERIES[(k + i) % len(QUERIES)]} {k}")
        _run_threads(work, [(k,) for k in range(4)])

    def encode_n(n: int):
        return lambda: pipe.encode([f"{i} {CHUNK}" for i in range(n)])

    def ingest(n: int):
        def go() -> None:
            chunks = [f"paragraph {i}: {CHUNK}" for i in range(n)]
            for start in range(0, n, 8):
                pipe.encode(chunks[start:start + 8])
        return go

    if workload == "pairs":
        # Paired latency, all in one process so host-load drift cancels:
        # before each trial either trim or not, in ABBA order, then time 5
        # short queries and a 4 x ~512-token encode. Each trial's encode
        # leaves the freed buffers that the next trial's trim returns, so a
        # "trim" trial pays the re-faulting a real trim would cause.
        encode_4 = encode_n(4)
        # Three warm-up encodes: the first ones still page in bf16 weights
        # (~665k minor faults per trial in the smoke run).
        for _ in range(3):
            encode_4()
        order = []
        for r in range(int(os.environ.get("PROBE_ROUNDS", "8"))):
            order += ["none", "trim"] if r % 2 == 0 else ["trim", "none"]
        trials = []
        for condition in order:
            trial = {"condition": condition, "load1": _loadavg()[0]}
            if condition == "trim":
                u0 = _usage()
                trial["trim"] = trim()
                trial["trim"].update(_usage_delta(u0, _usage()))
            u0 = _usage()
            trial["query_ms"] = timed_queries(5)
            t = time.perf_counter()
            encode_4()
            trial["encode_s"] = round(time.perf_counter() - t, 3)
            # Wall time swings with host load; page faults and kernel CPU
            # (where re-faulting released pages and mmap/munmap are paid)
            # barely do.
            trial.update(_usage_delta(u0, _usage()))
            trial["anon_after_mb"] = _status()["anon_mb"]
            trials.append(trial)
            log(f"{condition}: encode {trial['encode_s']}s queries "
                f"{trial['query_ms']} anon {trial['anon_after_mb']}")
        result["pairs"] = trials
        result["fault_cost"] = _fault_cost()
        result["final"] = _status()
        result["loadavg_end"] = _loadavg()
        print(RESULT_MARK + json.dumps(result), flush=True)
        return 0

    if workload == "single":
        bursts = [(f"encode_{n}x512", encode_n(n)) for n in (4, 8, 16, 16, 4)]
        bursts.append(("ingest_48x512_slices_of_8", ingest(48)))
    elif workload == "threads4":
        bursts = [("threads4_encode_8x256", enc_threads)] * 3
        bursts += [("threads4_queries_8", query_threads)] * 2
    elif workload == "pool4":
        def pool_encode() -> None:
            list(pool.map(lambda k: pipe.encode(
                [(f"concurrent request text {k}-{i} ") * 40 for i in range(8)]),
                range(4)))

        def pool_queries() -> None:
            list(pool.map(lambda k: [
                pipe.encode_query(f"{QUERIES[(k + i) % len(QUERIES)]} {k}")
                for i in range(8)], range(4)))

        bursts = [("pool4_encode_8x256", pool_encode)] * 3
        bursts += [("pool4_queries_8", pool_queries)] * 2
    else:
        raise SystemExit(f"unknown PROBE_WORKLOAD {workload!r}")

    steps = []
    for name, fn in bursts:
        before = _status()
        _reset_peak()
        t = time.perf_counter()
        fn()
        seconds = time.perf_counter() - t
        after = _status()
        step = {"name": name, "seconds": round(seconds, 2),
                "anon_before_mb": before["anon_mb"],
                "peak_hwm_mb": after["hwm_mb"],
                "anon_after_mb": after["anon_mb"],
                "rss_after_mb": after["rss_mb"]}
        if pool is not None:
            # What the burst kept, once anything released asynchronously
            # has landed (the immediate reading after joined threads was
            # up to ~1 GB high in the threads4 sweep).
            time.sleep(1)
            step["anon_settled_mb"] = _status()["anon_mb"]
        step["mallinfo_after"] = mallinfo()
        if do_trim:
            step["trim"] = trim()
        steps.append(step)
        kept = _kept_mb(step)
        log(f"{name}: {seconds:.1f}s anon {before['anon_mb']} -> "
            f"{after['anon_mb']} (peak {after['hwm_mb']}) kept {kept}")
    result["steps"] = steps
    result["post_query_ms"] = timed_queries(20)
    gc.collect()
    time.sleep(1)
    result["idle_after_gc"] = _status()
    result["idle_mallinfo"] = mallinfo()
    result["end_trim"] = trim()
    result["after_end_trim_query_ms"] = timed_queries(5)
    result["final"] = _status()
    result["loadavg_end"] = _loadavg()
    log(f"idle {result['idle_after_gc']} end_trim {result['end_trim']}")
    print(RESULT_MARK + json.dumps(result), flush=True)
    return 0


# ── host-side driver ────────────────────────────────────────────────────
ARENA2 = {"MALLOC_ARENA_MAX": "2"}
MMAP128K = {"MALLOC_MMAP_THRESHOLD_": "131072"}
ARMS = {
    # name: (extra env, trim after every burst)
    "ctrl": (ARENA2, False),
    "trim": (ARENA2, True),
    "mmap128k": ({**ARENA2, **MMAP128K}, False),
    "mmap128k_trim": ({**ARENA2, **MMAP128K}, True),
    "defarena": ({}, False),
    "defarena_trim": ({}, True),
}
# The single-thread workload runs the default-arena arms too: torch's
# intra-op worker threads allocate as well, so one Python thread does not
# mean one arena.
WORKLOAD_ARMS = {workload: list(ARMS) for workload in ("single", "threads4")}
# The paired-latency workload trims inside the process, so its arms differ
# only by allocator env; the mmap arm is compared across containers.
WORKLOAD_ARMS["pairs"] = ["ctrl", "mmap128k"]
# Persistent-worker concurrency (the daemon's shape); the trim question only.
WORKLOAD_ARMS["pool4"] = ["ctrl", "trim", "defarena", "defarena_trim"]
DEFAULT_WORKLOADS = ["single", "threads4"]
DTYPES = ["fp32", "auto"]


def _export(commit: str, dest: str) -> None:
    import io
    import subprocess
    import tarfile

    blob = subprocess.run(
        ["git", "archive", "--format=tar", commit, "pseudolife_memory"],
        check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        tar.extractall(dest, filter="data")


def _run_one(image: str, src: str, dtype: str, workload: str, arm: str,
             log_path) -> dict:
    import subprocess
    from pathlib import Path

    env, do_trim = ARMS[arm]
    env = {**env, "PSEUDOLIFE_EMBEDDING_CPU_DTYPE": dtype,
           "PROBE_WORKLOAD": workload, "PROBE_TRIM": "1" if do_trim else "0",
           "THREADS": "3", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
           "HF_HOME": "/opt/hf", "PYTHONPATH": "/src"}
    # --no-healthcheck: the image's HEALTHCHECK would start a python
    # process every 15 s inside the arm being timed.
    cmd = ["docker", "run", "--rm", "-i", "--network", "none",
           "--no-healthcheck", "--label", "pseudolife.probe=allocator-trim",
           "--memory", "7g", "--memory-swap", "7g", "--cpus", "3",
           "-w", "/tmp", "-v", f"{Path(src).as_posix()}:/src:ro"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    cmd += ["--entrypoint", "python", image, "-", "inner"]
    with open(__file__, "rb") as fh:
        proc = subprocess.run(cmd, stdin=fh, capture_output=True, timeout=3600)
    stdout = proc.stdout.decode(errors="replace")
    stderr = proc.stderr.decode(errors="replace")
    if log_path is not None:
        log_path.write_text(f"$ {' '.join(cmd)}\n--- stderr\n{stderr}"
                            f"\n--- stdout\n{stdout}", encoding="utf-8")
    if proc.returncode != 0:
        # A result line from a process that then failed is not a result.
        raise RuntimeError(f"{dtype}/{workload}/{arm}: container failed "
                           f"(exit {proc.returncode}): {stderr[-2000:]}")
    for line in stdout.splitlines():
        if line.startswith(RESULT_MARK):
            return json.loads(line[len(RESULT_MARK):])
    raise RuntimeError(f"{dtype}/{workload}/{arm}: no result "
                       f"(exit {proc.returncode}): {stderr[-2000:]}")


def _kept_mb(step: dict, key: str = "anon") -> int:
    """What a burst left resident: after its trim when the arm trims, else
    the settled reading where the workload takes one."""
    if "trim" in step:
        return step["trim"][f"{key}_after_mb"]
    if key == "anon" and "anon_settled_mb" in step:
        return step["anon_settled_mb"]
    return step[f"{key}_after_mb"]


def _median(values):
    values = sorted(values)
    n = len(values)
    if not n:
        return None
    mid = n // 2
    return values[mid] if n % 2 else round((values[mid - 1] + values[mid]) / 2, 2)


def summarize(runs: list[dict]) -> dict:
    """Per (dtype, workload, arm): the replicate medians the verdict reads."""
    groups: dict = {}
    for run in runs:
        if run["workload"] == "pairs":
            continue
        key = f"{run['dtype']}/{run['workload']}/{run['arm']}"
        groups.setdefault(key, []).append(run["result"])
    out = {}
    for key, results in sorted(groups.items()):
        n_steps = len(results[0]["steps"])
        trims = [s["trim"]["ms"] for r in results for s in r["steps"] if "trim" in s]
        out[key] = {
            "replicates": len(results),
            "resolved_dtype": results[0]["describe"]["dtype"],
            "base_anon_mb": _median([r["base"]["anon_mb"] for r in results]),
            "kept_anon_mb_per_step": [
                _median([_kept_mb(r["steps"][i]) for r in results])
                for i in range(n_steps)],
            "kept_rss_mb_per_step": [
                _median([_kept_mb(r["steps"][i], "rss") for r in results])
                for i in range(n_steps)],
            "step_names": [s["name"] for s in results[0]["steps"]],
            "base_rss_mb": _median([r["base"]["rss_mb"] for r in results]),
            "idle_after_gc_rss_mb": _median(
                [r["idle_after_gc"]["rss_mb"] for r in results]),
            "max_peak_hwm_mb": _median(
                [max(s["peak_hwm_mb"] for s in r["steps"]) for r in results]),
            "idle_after_gc_anon_mb": _median(
                [r["idle_after_gc"]["anon_mb"] for r in results]),
            "end_trim_freed_mb": _median(
                [r["end_trim"]["freed_mb"] for r in results]),
            "final_anon_mb": _median([r["final"]["anon_mb"] for r in results]),
            "burst_seconds_total": _median(
                [round(sum(s["seconds"] for s in r["steps"]), 2) for r in results]),
            "burst_seconds_per_step": [
                _median([r["steps"][i]["seconds"] for r in results])
                for i in range(n_steps)],
            "trim_ms_median": _median(trims),
            "trim_ms_max": max(trims) if trims else None,
            "end_trim_ms": _median([r["end_trim"]["ms"] for r in results]),
            # The first warm queries pay page-in and kernel JIT (seconds on
            # bf16); the last five are the steady state.
            "warm_query_ms_median": _median(
                [q for r in results for q in r["warm_query_ms"][-5:]]),
            "post_query_ms_median": _median(
                [q for r in results for q in r["post_query_ms"]]),
            "post_query_first_ms": [r["post_query_ms"][0] for r in results],
            "after_end_trim_first_query_ms": [
                r["after_end_trim_query_ms"][0] for r in results],
        }
    return out


def _sign_test_p(wins: int, n: int) -> float | None:
    """Two-sided exact binomial p for ``wins`` of ``n`` under p=0.5."""
    from math import comb

    if not n:
        return None
    probs = [comb(n, k) / 2 ** n for k in range(n + 1)]
    return round(min(1.0, sum(p for p in probs if p <= probs[wins] + 1e-12)), 4)


def _paired(pairs: list[tuple[float, float]]) -> dict:
    """``(trim, none)`` pairs -> median delta, how often trim was slower,
    and the sign-test p against "trim changes nothing"."""
    deltas = [round(t - n, 3) for t, n in pairs]
    slower = sum(d > 0 for d in deltas)
    ties = sum(d == 0 for d in deltas)
    return {"n": len(deltas), "median_delta": _median(deltas),
            "trim_slower": slower, "ties": ties,
            "sign_test_p": _sign_test_p(slower, len(deltas) - ties),
            "deltas": deltas}


def summarize_pairs(runs: list[dict]) -> dict:
    """Per (dtype, arm) of the paired workload: trim-minus-none deltas over
    adjacent ABBA pairs, beside the noise floor — the median gap between
    consecutive no-trim trials, i.e. what the same condition moves by on
    its own. A delta inside that floor is not a finding."""
    groups: dict = {}
    for run in runs:
        if run["workload"] == "pairs":
            groups.setdefault(f"{run['dtype']}/pairs/{run['arm']}", []).append(
                run["result"])
    # Per-trial metrics: wall-clock (load-sensitive) first, then the
    # load-robust counters.
    metrics = {
        "encode_4x512_s": lambda x: x["encode_s"],
        "first_query_ms": lambda x: x["query_ms"][0],
        "query_median_ms": lambda x: _median(x["query_ms"]),
        "cpu_sys_s": lambda x: x["cpu_sys_s"],
        "cpu_user_s": lambda x: x["cpu_user_s"],
        "minflt": lambda x: x["minflt"],
    }
    out = {}
    for key, results in sorted(groups.items()):
        pairs = {name: [] for name in metrics}
        noise = {name: [] for name in metrics}
        per_container = []
        for res in results:
            trials = res["pairs"]
            for a, b in zip(trials[0::2], trials[1::2]):
                t, n = (a, b) if a["condition"] == "trim" else (b, a)
                for name, get in metrics.items():
                    pairs[name].append((get(t), get(n)))
            nones = [x for x in trials if x["condition"] == "none"]
            for name, get in metrics.items():
                noise[name] += [abs(get(x) - get(y))
                                for x, y in zip(nones, nones[1:])]
            per_container.append({
                f"none_{name}_median": _median([get(x) for x in nones])
                for name, get in metrics.items()})
            per_container[-1]["load1_median"] = _median(
                [x["load1"] for x in trials])
        trims = [x["trim"] for r in results for x in r["pairs"] if "trim" in x]
        summary = {"containers": len(results),
                   "resolved_dtype": results[0]["describe"]["dtype"]}
        for name in metrics:
            paired = _paired(pairs[name])
            none_median = _median([n for _, n in pairs[name]])
            paired["none_median"] = none_median
            paired["median_delta_pct_of_none"] = (
                round(100 * paired["median_delta"] / none_median, 2)
                if none_median else None)
            # What the no-trim condition moves by on its own between
            # consecutive trials; a delta inside it is not a finding.
            paired["noise_floor"] = _median(noise[name])
            summary[name] = paired
        summary["trim_call"] = {
            "ms_median": _median([t["ms"] for t in trims]),
            "ms_max": max((t["ms"] for t in trims), default=None),
            "cpu_sys_s_median": _median([t["cpu_sys_s"] for t in trims]),
            "freed_mb_median": _median([t["freed_mb"] for t in trims]),
        }
        costs = [r["fault_cost"]["wall_us_per_fault_min"] for r in results]
        summary["fault_cost_wall_us_min"] = min(costs)
        summary["fault_cost_wall_us_median"] = _median(costs)
        # Re-faulting cost a trim puts on the next request: its extra
        # faults at the fastest fault cost measured on this host.
        summary["est_refault_ms"] = round(
            summary["minflt"]["median_delta"] * min(costs) / 1000, 1)
        summary["per_container"] = per_container
        out[key] = summary
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import hashlib
    import platform
    import subprocess
    import tempfile
    from pathlib import Path

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", required=True)
    ap.add_argument("--commit", required=True,
                    help="commit whose pseudolife_memory/ is mounted as /src")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--replicates", type=int, default=2)
    ap.add_argument("--dtypes", nargs="+", default=DTYPES)
    ap.add_argument("--workloads", nargs="+", default=DEFAULT_WORKLOADS,
                    choices=list(WORKLOAD_ARMS),
                    help="'pairs' (paired trim latency) and 'pool4' "
                         "(persistent workers) run only when named")
    ap.add_argument("--log-dir", type=Path, default=None,
                    help="per-run container logs (not committed)")
    args = ap.parse_args(argv)
    if args.out.exists():
        raise SystemExit(f"{args.out} exists; canonical results are never "
                         "rewritten — pick a new --out")
    if args.log_dir is not None:
        args.log_dir.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(["git", "rev-parse", args.commit], check=True,
                            capture_output=True, text=True).stdout.strip()
    image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.image],
        check=True, capture_output=True, text=True).stdout.strip()
    plan = []
    for rep in range(args.replicates):
        for dtype in args.dtypes:
            for workload in args.workloads:
                arms = WORKLOAD_ARMS[workload]
                # Alternate the arm order per replicate so host-load drift
                # does not line up with one arm.
                for arm in (arms if rep % 2 == 0 else arms[::-1]):
                    plan.append((rep, dtype, workload, arm))
    artifact = {
        "title": "Allocator probe: malloc_trim(0) and a fixed "
                 "MALLOC_MMAP_THRESHOLD_ vs the embedder's RSS ratchet",
        "date": time.strftime("%Y-%m-%d"),
        "harness": "evals/allocator_trim_probe.py",
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_commit": commit,
        "image": args.image,
        "image_id": image_id,
        "host": platform.platform(),
        "container": "docker run --rm -i --network none --no-healthcheck "
                     "--memory 7g --memory-swap 7g --cpus 3 -w /tmp, "
                     "/src read-only",
        "arms": {name: {"env": env, "trim_after_every_burst": t}
                 for name, (env, t) in ARMS.items()},
        "workload_arms": WORKLOAD_ARMS,
        "runs": [],
    }
    with tempfile.TemporaryDirectory(prefix="alloc-probe-") as src:
        _export(commit, src)
        for i, (rep, dtype, workload, arm) in enumerate(plan, 1):
            t0 = time.time()
            log_path = (args.log_dir / f"{i:02d}-{dtype}-{workload}-{arm}-r{rep}.log"
                        if args.log_dir else None)
            result = _run_one(args.image, src, dtype, workload, arm, log_path)
            artifact["runs"].append({"replicate": rep, "dtype": dtype,
                                     "workload": workload, "arm": arm,
                                     "result": result})
            artifact["summary"] = summarize(artifact["runs"])
            if any(run["workload"] == "pairs" for run in artifact["runs"]):
                artifact["pairs_summary"] = summarize_pairs(artifact["runs"])
            args.out.write_text(json.dumps(artifact, indent=1) + "\n",
                                encoding="utf-8")
            if workload == "pairs":
                detail = " ".join(f"{x['condition'][0]}{x['encode_s']}"
                                  for x in result["pairs"])
            else:
                kept = [_kept_mb(s) for s in result["steps"]]
                detail = (f"kept {kept} idle {result['idle_after_gc']['anon_mb']} "
                          f"end-trim -{result['end_trim']['freed_mb']}")
            print(f"[{i}/{len(plan)}] {dtype}/{workload}/{arm} r{rep}: "
                  f"{time.time() - t0:.0f}s base {result['base']['anon_mb']} "
                  f"{detail} "
                  f"load {result['loadavg_start'][0]}->{result['loadavg_end'][0]}",
                  flush=True)
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["inner"]:
        sys.exit(_inner())
    sys.exit(main())
