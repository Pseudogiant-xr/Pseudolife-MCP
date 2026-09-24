"""evals/suite_profile_summary.py folds per-worker profiler output."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import suite_profile_summary as sps  # noqa: E402


def _write(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_worker_records_are_summed_and_the_controller_copy_ignored(tmp_path):
    _write(tmp_path / "profile-gw0.jsonl", [
        {"id": "tests/test_a.py::t1", "setup": 0.5, "call": 1.5, "teardown": 0.0,
         "encode_n": 2, "encode_s": 1.0, "encode_texts": 2},
        {"id": "tests/test_b.py::t2", "setup": 0.0, "call": 0.005, "teardown": 0.0},
    ])
    _write(tmp_path / "profile-gw1.jsonl", [
        {"id": "tests/test_c.py::t3", "setup": 0.0, "call": 3.0, "teardown": 0.0,
         "spawn_n": 1, "spawns": ["pwsh.exe"]},
    ])
    # An xdist controller repeats the reports without counters; it must not
    # double the totals.
    _write(tmp_path / "profile-main.jsonl", [
        {"id": "tests/test_a.py::t1", "setup": 0.5, "call": 1.5, "teardown": 0.0},
    ])

    out = sps.summarise(tmp_path, top=10)

    assert out["tests"] == 3 and out["workers"] == 2
    assert out["test_wall_s"] == 5.0
    assert out["totals"]["encode_s"] == 1.0
    assert out["shares"]["real_embedder_tests"] == {"tests": 1, "wall_s": 2.0, "pct": 40.0}
    assert out["shares"]["subprocess_tests"]["tests"] == 1
    assert out["shares"]["under_10ms_tests"]["tests"] == 1
    assert out["spawns_by_command"] == {"pwsh.exe": 1}
    assert [f["file"] for f in out["top_files"]] == [
        "tests/test_c.py", "tests/test_a.py", "tests/test_b.py"]
