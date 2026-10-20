"""Summarise tests/suite_profile_plugin.py output into one JSON artifact.

The plugin writes one JSON line per test (per xdist worker) with the phase
split and the counters that explain the time: real embedding-model forward
passes, model loads, service starts, ensure_schema runs, database creation,
drops and truncations, subprocess spawns and sleeps. This folds a run's files
into totals, the share of test time in tests that embed / spawn / touch
Postgres, and a per-file table:

    python evals/suite_profile_summary.py --profile-dir <dir> --tag <tag>

When the run also left a worker timeline (each phase's monotonic start and
end) and the controller's ``samples.jsonl``, the summary adds per-worker busy
and idle time, the straggler tail, the slowest setups and teardowns with the
test server's wait events and checkpoint progress over the same interval,
and the run's peak memory, pressure and checkpoint / fsync totals. The
manifest's run description is carried over as ``run``.

Records from an xdist controller (profile-main.jsonl next to gw* files) are
ignored: they repeat the workers' reports without the counters. Writes a
JSON artifact by default; a number without a committed artifact was never
really measured.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import glob
import json
import os
import statistics
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
COUNTERS = ("encode_s", "encode_texts", "model_load_s", "service_init_s",
            "service_init_n", "ensure_schema_s", "ensure_schema_n", "spawn_n",
            "sleep_main_s", "db_create_s", "db_create_n", "db_drop_s", "db_drop_n",
            "truncate_s", "truncate_n", "terminate_s", "terminate_n")
PHASES = ("setup", "call", "teardown")
# Checkpointer counters whose growth over an interval is reported (both the
# PostgreSQL 17+ and the older names; whichever the server had).
CHECKPOINT_DELTAS = ("num_requested", "num_timed", "num_done", "write_time",
                     "sync_time", "buffers_written", "checkpoints_req",
                     "checkpoints_timed", "checkpoint_write_time",
                     "checkpoint_sync_time", "buffers_checkpoint")
# A sample's checkpoint counters bracket a phase only this far outside it
# (seconds); farther ones (a reconnect gap) would credit it with unrelated
# checkpoints.
BRACKET_SLACK_S = 5.0


def _wall(r: dict) -> float:
    return r.get("setup", 0.0) + r.get("call", 0.0) + r.get("teardown", 0.0)


def _load_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a line cut short by a killed run
    return out


def _span(r: dict, when: str) -> tuple[float, float] | None:
    t = r.get(when + "_t")
    if isinstance(t, list) and len(t) == 2 and t[1] >= t[0]:
        return float(t[0]), float(t[1])
    return None


def timeline(records: list[dict]) -> dict | None:
    """Per-worker busy and idle time from the phases' monotonic spans."""
    by_worker: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    last_file: dict[str, tuple[float, str]] = {}
    for r in records:
        worker = r.get("worker", "main")
        for when in PHASES:
            span = _span(r, when)
            if span:
                by_worker[worker].append(span)
                if span[1] >= last_file.get(worker, (0.0, ""))[0]:
                    last_file[worker] = (span[1], r["id"].split("::")[0])
    if not by_worker:
        return None
    start = min(s for spans in by_worker.values() for s, _ in spans)
    end = max(e for spans in by_worker.values() for _, e in spans)
    workers = {}
    for worker, spans in sorted(by_worker.items()):
        busy = sum(e - s for s, e in spans)
        w_end = max(e for _, e in spans)
        workers[worker] = {
            "busy_s": round(busy, 1),
            "idle_s": round((w_end - start) - busy, 1),
            "finished_after_s": round(w_end - start, 1),
            "last_file": last_file[worker][1],
        }
    ends = [w["finished_after_s"] for w in workers.values()]
    return {
        "span_s": round(end - start, 1),
        "start": start,
        "end": end,
        "workers": workers,
        # How long the last worker ran on after the median one finished: the
        # tail a whole-file schedule could shorten.
        "tail_s": round(max(ends) - statistics.median(ends), 1),
        "busy_share_pct": round(100 * sum(w["busy_s"] for w in workers.values())
                                / (len(workers) * (end - start)), 1) if end > start else None,
    }


class Samples:
    """The controller's samples, searchable by monotonic time."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = sorted((r for r in rows if "t" in r), key=lambda r: r["t"])
        self.t = [r["t"] for r in self.rows]

    def between(self, start: float, end: float) -> list[dict]:
        return self.rows[bisect.bisect_left(self.t, start):bisect.bisect_right(self.t, end)]

    def bracket(self, start: float, end: float) -> tuple[dict, dict] | None:
        """The last sample at or before ``start`` and the first at or after
        ``end``, each carrying checkpointer counters and each within
        BRACKET_SLACK_S of the phase; None when either is missing."""
        i = bisect.bisect_right(self.t, start) - 1
        j = bisect.bisect_left(self.t, end)
        before = next((self.rows[k] for k in range(i, -1, -1)
                       if self.rows[k].get("pg", {}).get("checkpointer")), None)
        after = next((self.rows[k] for k in range(j, len(self.rows))
                      if self.rows[k].get("pg", {}).get("checkpointer")), None)
        if not (before and after):
            return None
        if start - before["t"] > BRACKET_SLACK_S or after["t"] - end > BRACKET_SLACK_S:
            return None
        return before, after


def _waits(rows: list[dict]) -> dict[str, int]:
    """Sample-weighted session counts per wait. Left out: a session not
    waiting (no wait type), a process idling in its main loop (Activity),
    and an idle client's read."""
    out: collections.Counter = collections.Counter()
    for row in rows:
        for backend, state, kind, event, n in row.get("pg", {}).get("sessions", []):
            if kind in ("", "Activity") or (kind, event) == ("Client", "ClientRead"):
                continue
            out[f"{backend}|{kind}:{event}"] += int(n)
    return dict(out.most_common(8))


def _checkpoint_delta(pair: tuple[dict, dict] | None) -> dict | None:
    if pair is None:
        return None
    a = pair[0]["pg"]["checkpointer"]
    b = pair[1]["pg"]["checkpointer"]
    out = {}
    for key in CHECKPOINT_DELTAS:
        if a.get(key) is not None and b.get(key) is not None:
            delta = b[key] - a[key]
            if delta:
                out[key] = round(delta, 1)
    return out


def _fsync_delta(first: dict, last: dict) -> dict:
    a = first.get("pg", {}).get("fsyncs", {})
    b = last.get("pg", {}).get("fsyncs", {})
    out = {}
    for backend, (n, ms) in b.items():
        n0, ms0 = a.get(backend, [0.0, 0.0])
        if n - n0:
            out[backend] = {"fsyncs": int(n - n0), "fsync_ms": round(ms - ms0, 1)}
    return out


def resources(samples: Samples) -> dict | None:
    rows = samples.rows
    if not rows:
        return None

    def peak(key, fn=max):
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return fn(values) if values else None

    with_pg = [r for r in rows if r.get("pg", {}).get("checkpointer")]
    errors = collections.Counter(r["pg"]["error"] for r in rows
                                 if r.get("pg", {}).get("error"))
    iowait = [r["cpu_iowait_pct"] for r in rows if "cpu_iowait_pct" in r]
    return {
        "samples": len(rows),
        "peak_tree_rss_sum": peak("tree_rss_sum"),
        "peak_tree_procs": peak("tree_procs"),
        "peak_postgres_rss_visible": peak("postgres_rss_visible"),
        "min_mem_available": peak("mem_available", min),
        "peak_swap_used": peak("swap_used"),
        "peak_load1": peak("load1"),
        "peak_pressure": {k: peak(k) for k in sorted({k for r in rows for k in r
                                                       if k.endswith("_avg10")})},
        "mean_cpu_iowait_pct": round(statistics.mean(iowait), 1) if iowait else None,
        "postgres_sampled": len(with_pg),
        "postgres_errors": dict(errors),
        "checkpoints": _checkpoint_delta((with_pg[0], with_pg[-1])) if len(with_pg) > 1 else None,
        "fsyncs": _fsync_delta(with_pg[0], with_pg[-1]) if len(with_pg) > 1 else None,
        "peak_databases": max((r["pg"]["databases"] for r in with_pg
                               if "databases" in r["pg"]), default=None),
        "waits": _waits(rows),
    }


def slow_phases(records: list[dict], samples: Samples | None, top: int) -> list[dict]:
    """The slowest setups and teardowns, with what the server did meanwhile."""
    phases = []
    for r in records:
        for when in ("setup", "teardown"):
            seconds = r.get(when, 0.0)
            if seconds >= 1.0:
                phases.append((seconds, when, r))
    phases.sort(key=lambda p: -p[0])
    out = []
    for seconds, when, r in phases[:top]:
        row = {"id": r["id"], "phase": when, "s": round(seconds, 1),
               "worker": r.get("worker")}
        if when == "teardown" and r.get("slow_teardowns"):
            row["fixtures"] = r["slow_teardowns"]
        # This phase's own statements (the plugin splits them by phase).
        sql = {k: r[f"{when}_{k}"] for k in ("db_create_s", "db_drop_s", "truncate_s",
                                             "terminate_s") if r.get(f"{when}_{k}")}
        if sql:
            row["sql_s"] = sql
        span = _span(r, when)
        if span and samples is not None and samples.rows:
            row["waits"] = _waits(samples.between(*span))
            delta = _checkpoint_delta(samples.bracket(*span))
            if delta:
                row["checkpoint_delta"] = delta
        out.append(row)
    return out


def summarise(profile_dir: Path, top: int) -> dict:
    paths = sorted(glob.glob(str(profile_dir / "profile-*.jsonl")))
    workers = [p for p in paths if not p.endswith("profile-main.jsonl")]
    lines = [r for p in (workers or paths) for r in _load_jsonl(Path(p))]
    records = [r for r in lines if "id" in r]
    # A start line with no record after it on its worker: the test a killed
    # or hung worker was in.
    finished = {(r.get("worker"), r["id"]) for r in records}
    unfinished = sorted({r["start"] for r in lines if "start" in r
                         and (r.get("worker"), r["start"]) not in finished})
    wall = sum(_wall(r) for r in records)
    totals = {k: round(sum(r.get(k, 0) for r in records), 1) for k in COUNTERS}

    def share(pred) -> dict:
        rows = [r for r in records if pred(r)]
        w = sum(_wall(r) for r in rows)
        return {"tests": len(rows), "wall_s": round(w, 1),
                "pct": round(100 * w / wall, 1) if wall else None}

    spawns = collections.Counter(s for r in records for s in r.get("spawns", []))
    per_file: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in records:
        c = per_file[r["id"].split("::")[0]]
        c["tests"] += 1
        c["wall_s"] += _wall(r)
        for k in COUNTERS:
            c[k] += r.get(k, 0)
    files = sorted(per_file.items(), key=lambda kv: -kv[1]["wall_s"])[:top]
    samples_path = profile_dir / "samples.jsonl"
    samples = Samples(_load_jsonl(samples_path)) if samples_path.exists() else None
    manifest_path = profile_dir / "manifest.json"
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists() else None)
    result = {
        "tests": len(records),
        "workers": len(workers) or 1,
        "test_wall_s": round(wall, 1),
        "phase_s": {when: round(sum(r.get(when, 0.0) for r in records), 1)
                    for when in PHASES},
        "outcomes": dict(collections.Counter(r.get("outcome", "passed") for r in records)),
        "unfinished": unfinished,
        "totals": totals,
        "shares": {
            "real_embedder_tests": share(lambda r: r.get("encode_n", 0) > 0),
            "subprocess_tests": share(lambda r: r.get("spawn_n", 0) > 0),
            "postgres_schema_tests": share(lambda r: r.get("ensure_schema_n", 0) > 0),
            "under_10ms_tests": share(lambda r: _wall(r) < 0.01),
        },
        "spawns_by_command": dict(spawns.most_common(15)),
        "top_files": [{"file": f, **{k: round(v, 1) for k, v in c.items()}}
                      for f, c in files],
    }
    line = timeline(records)
    if line is not None:
        result["timeline"] = {k: v for k, v in line.items() if k not in ("start", "end")}
    if samples is not None:
        result["resources"] = resources(samples)
    if line is not None or samples is not None:
        result["slow_phases"] = slow_phases(records, samples, min(top, 25))
    if manifest is not None:
        result["run"] = manifest
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--tag", required=True, help="names the artifact")
    parser.add_argument("--note", default="", help="where and how the run was made")
    parser.add_argument("--top", type=int, default=40, help="files in the table")
    parser.add_argument("--out", type=Path,
                        help="default evals/results/suite-profile-<tag>.json")
    args = parser.parse_args()
    out = args.out or RESULTS / f"suite-profile-{args.tag}.json"
    result = {"tag": args.tag, "note": args.note,
              **summarise(args.profile_dir, args.top)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"suite-profile: {result['tests']} tests, {result['test_wall_s']} s "
          f"-> {os.fspath(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
