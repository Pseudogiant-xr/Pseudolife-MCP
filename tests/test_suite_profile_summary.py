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


def _sample(t, *, waits=(), ckpt=None, **host):
    row = {"t": t, "wall": 1000 + t, **host}
    pg = {"sessions": [list(w) for w in waits], "databases": 3}
    if ckpt is not None:
        pg["checkpointer"] = ckpt
        pg["fsyncs"] = {"checkpointer": [ckpt["num_done"] * 100, ckpt["sync_time"]]}
    row["pg"] = pg
    return row


def _timeline_run(tmp_path: Path) -> Path:
    # gw0 works 0-10 s with a 6 s module teardown at the end; gw1 stops at 4 s.
    _write(tmp_path / "profile-gw0.jsonl", [
        {"id": "tests/test_a.py::t1", "worker": "gw0", "setup": 1.0, "call": 2.0,
         "teardown": 0.0, "setup_t": [100.0, 101.0], "call_t": [101.0, 103.0],
         "teardown_t": [103.0, 103.0]},
        {"id": "tests/test_a.py::t2", "worker": "gw0", "setup": 0.0, "call": 1.0,
         "teardown": 6.0, "setup_t": [104.0, 104.0], "call_t": [104.0, 105.0],
         "teardown_t": [105.0, 110.0 + 1.0], "db_drop_s": 5.5, "db_drop_n": 1,
         "teardown_db_drop_s": 5.5, "setup_db_create_s": 0.0,
         "slow_teardowns": [["bank@module", 5.9]]},
    ])
    _write(tmp_path / "profile-gw1.jsonl", [
        {"id": "tests/test_b.py::t3", "worker": "gw1", "setup": 0.0, "call": 4.0,
         "teardown": 0.0, "setup_t": [100.0, 100.0], "call_t": [100.0, 104.0],
         "teardown_t": [104.0, 104.0]},
    ])
    ckpt0 = {"num_done": 1.0, "num_requested": 1.0, "sync_time": 10.0, "write_time": 5.0}
    ckpt1 = {"num_done": 2.0, "num_requested": 2.0, "sync_time": 4010.0, "write_time": 9.0}
    _write(tmp_path / "samples.jsonl", [
        _sample(100.5, ckpt=ckpt0, tree_rss_sum=100, mem_available=900, cpu_iowait_pct=1.0),
        # Not waits: an idle client's read, a running backend with no wait
        # type, and background processes idling in their main loops.
        _sample(106.0, waits=[("client backend", "active", "IPC", "CheckpointStart", 1),
                              ("client backend", "idle", "Client", "ClientRead", 4),
                              ("client backend", "active", "", "", 2),
                              ("checkpointer", "", "IO", "DataFileSync", 1),
                              ("walwriter", "", "Activity", "WalWriterMain", 1),
                              ("background writer", "", "Activity", "BgwriterHibernate", 1)],
                ckpt=ckpt0, tree_rss_sum=300, mem_available=500, cpu_iowait_pct=9.0),
        _sample(108.0, waits=[("client backend", "active", "IPC", "CheckpointStart", 1),
                              ("checkpointer", "", "Activity", "CheckpointerMain", 1)],
                ckpt=ckpt0, tree_rss_sum=250, mem_available=600),
        _sample(112.0, ckpt=ckpt1, tree_rss_sum=120, mem_available=880),
    ])
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"complete": True, "exitstatus": 0, "workers_requested": 2}), encoding="utf-8")
    return tmp_path


def test_the_timeline_separates_busy_from_idle_workers(tmp_path):
    out = sps.summarise(_timeline_run(tmp_path), top=10)

    line = out["timeline"]
    assert line["span_s"] == 11.0
    assert line["workers"]["gw0"] == {"busy_s": 10.0, "idle_s": 1.0,
                                      "finished_after_s": 11.0,
                                      "last_file": "tests/test_a.py"}
    assert line["workers"]["gw1"]["busy_s"] == 4.0
    assert line["workers"]["gw1"]["finished_after_s"] == 4.0
    # The last worker ran on 3.5 s past the median finish (7.5 s).
    assert line["tail_s"] == 3.5
    assert out["phase_s"] == {"setup": 1.0, "call": 7.0, "teardown": 6.0}
    assert out["totals"]["db_drop_s"] == 5.5
    assert out["run"]["exitstatus"] == 0


def test_a_slow_teardown_is_joined_with_the_servers_waits(tmp_path):
    out = sps.summarise(_timeline_run(tmp_path), top=10)

    (slow,) = [p for p in out["slow_phases"] if p["phase"] == "teardown"]
    assert slow["id"] == "tests/test_a.py::t2"
    assert slow["fixtures"] == [["bank@module", 5.9]]
    assert slow["sql_s"] == {"db_drop_s": 5.5}
    # Two samples fell inside it, both with a backend waiting on a
    # checkpoint; idle sessions are not waits.
    assert slow["waits"] == {"client backend|IPC:CheckpointStart": 2,
                             "checkpointer|IO:DataFileSync": 1}
    # The checkpoint it waited for completed between the bracketing samples.
    assert slow["checkpoint_delta"] == {"num_requested": 1.0, "num_done": 1.0,
                                        "write_time": 4.0, "sync_time": 4000.0}


def test_resources_report_peaks_and_run_totals(tmp_path):
    res = sps.summarise(_timeline_run(tmp_path), top=10)["resources"]

    assert res["samples"] == 4
    assert res["peak_tree_rss_sum"] == 300
    assert res["min_mem_available"] == 500
    assert res["mean_cpu_iowait_pct"] == 5.0
    assert res["checkpoints"]["num_done"] == 1.0
    assert res["fsyncs"] == {"checkpointer": {"fsyncs": 100, "fsync_ms": 4000.0}}
    assert res["peak_databases"] == 3
    assert res["waits"] == {"client backend|IPC:CheckpointStart": 2,
                            "checkpointer|IO:DataFileSync": 1}


def test_a_line_cut_short_by_a_killed_run_is_skipped(tmp_path):
    (tmp_path / "profile-gw0.jsonl").write_text(
        json.dumps({"id": "tests/test_a.py::t1", "call": 1.0}) + "\n{\"id\": \"tests/te",
        encoding="utf-8")

    out = sps.summarise(tmp_path, top=10)

    assert out["tests"] == 1
    assert "timeline" not in out and "resources" not in out


def test_a_phase_gets_only_its_own_statements(tmp_path):
    _write(tmp_path / "profile-gw0.jsonl", [
        {"id": "tests/test_a.py::t1", "worker": "gw0", "setup": 2.0, "call": 0.0,
         "teardown": 3.0, "setup_t": [10.0, 12.0], "call_t": [12.0, 12.0],
         "teardown_t": [12.0, 15.0], "db_create_s": 1.5, "db_drop_s": 2.5,
         "setup_db_create_s": 1.5, "teardown_db_drop_s": 2.5},
    ])

    phases = {p["phase"]: p for p in sps.summarise(tmp_path, top=10)["slow_phases"]}

    assert phases["setup"]["sql_s"] == {"db_create_s": 1.5}
    assert phases["teardown"]["sql_s"] == {"db_drop_s": 2.5}


def test_checkpoints_far_outside_a_phase_are_not_credited_to_it(tmp_path):
    _write(tmp_path / "profile-gw0.jsonl", [
        {"id": "tests/test_a.py::t1", "worker": "gw0", "setup": 0.0, "call": 0.0,
         "teardown": 2.0, "setup_t": [100.0, 100.0], "call_t": [100.0, 100.0],
         "teardown_t": [100.0, 102.0]},
    ])
    # The server was unreachable for a minute around the teardown.
    _write(tmp_path / "samples.jsonl", [
        _sample(40.0, ckpt={"num_done": 1.0, "num_requested": 1.0, "sync_time": 0.0,
                            "write_time": 0.0}),
        _sample(101.0),
        _sample(160.0, ckpt={"num_done": 9.0, "num_requested": 9.0, "sync_time": 9.0,
                             "write_time": 9.0}),
    ])

    (slow,) = sps.summarise(tmp_path, top=10)["slow_phases"]

    assert "checkpoint_delta" not in slow


def test_the_test_a_worker_died_in_is_named(tmp_path):
    _write(tmp_path / "profile-gw0.jsonl", [
        {"start": "tests/test_a.py::t1", "worker": "gw0", "t": 1.0},
        {"id": "tests/test_a.py::t1", "worker": "gw0", "call": 1.0},
        {"start": "tests/test_a.py::t2", "worker": "gw0", "t": 2.0},
    ])
    _write(tmp_path / "profile-gw1.jsonl", [
        {"start": "tests/test_b.py::t3", "worker": "gw1", "t": 1.0},
        {"id": "tests/test_b.py::t3", "worker": "gw1", "call": 1.0},
    ])

    out = sps.summarise(tmp_path, top=10)

    assert out["tests"] == 2
    assert out["unfinished"] == ["tests/test_a.py::t2"]


def test_a_worker_that_ran_nothing_counts_as_idle(tmp_path):
    _write(tmp_path / "profile-gw0.jsonl", [
        {"id": "tests/test_a.py::t1", "worker": "gw0", "setup": 0.0, "call": 10.0,
         "teardown": 0.0, "setup_t": [0.0, 0.0], "call_t": [0.0, 10.0],
         "teardown_t": [10.0, 10.0]},
    ])
    (tmp_path / "profile-gw1.jsonl").write_text("", encoding="utf-8")

    line = sps.summarise(tmp_path, top=10)["timeline"]

    assert line["busy_share_pct"] == 50.0
    assert line["workers"]["gw1"] == {"busy_s": 0.0, "idle_s": 10.0,
                                      "finished_after_s": None, "last_file": None}
    assert line["tail_s"] == 0.0
