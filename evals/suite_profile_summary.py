"""Summarise tests/suite_profile_plugin.py output into one JSON artifact.

The plugin writes one JSON line per test (per xdist worker) with the phase
split and the counters that explain the time: real embedding-model forward
passes, model loads, service starts, ensure_schema runs, subprocess spawns
and sleeps. This folds a run's files into totals, the share of test time in
tests that embed / spawn / touch Postgres, and a per-file table:

    python evals/suite_profile_summary.py --profile-dir <dir> --tag <tag>

Records from an xdist controller (profile-main.jsonl next to gw* files) are
ignored: they repeat the workers' reports without the counters. Writes a
JSON artifact by default; a number without a committed artifact was never
really measured.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
COUNTERS = ("encode_s", "encode_texts", "model_load_s", "service_init_s",
            "service_init_n", "ensure_schema_s", "ensure_schema_n", "spawn_n",
            "sleep_main_s")


def _wall(r: dict) -> float:
    return r.get("setup", 0.0) + r.get("call", 0.0) + r.get("teardown", 0.0)


def summarise(profile_dir: Path, top: int) -> dict:
    paths = sorted(glob.glob(str(profile_dir / "profile-*.jsonl")))
    workers = [p for p in paths if not p.endswith("profile-main.jsonl")]
    records = [json.loads(line) for p in (workers or paths)
               for line in open(p, encoding="utf-8")]
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
    return {
        "tests": len(records),
        "workers": len(workers) or 1,
        "test_wall_s": round(wall, 1),
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
