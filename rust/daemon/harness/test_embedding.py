"""Comparator controls, independent of model inference or artifact downloads."""
import numpy as np
import argparse
import json
from pathlib import Path
import subprocess
import embedding

from embedding import compare, rankings


def observation(rows):
    rows = np.array(rows, dtype="<f4")
    return {"dim": rows.shape[1], "copy_safe": True, "operations": [
        {"bits": rows.view("<u4").tolist(), "tokens": [], "forwards": 1}]}


def test_exact_self_comparison_uses_bits_when_cosine_rounds_below_one():
    data = observation([[1., 2.]])
    diffs, metrics = compare(data, data)
    assert not diffs
    assert metrics[0]["exact"]


def test_exact_mode_preserves_signed_zero_and_rejects_nonfinite():
    assert compare(observation([[0., 1.]]), observation([[-0., 1.]]))[0]
    assert compare(observation([[1., 2.]]), observation([[float("nan"), 2.]]))[0]


def test_token_and_cache_counts_stay_exact_under_numeric_tolerance():
    data = observation([[1., 2.]])
    candidate = observation([[1., 2.]])
    candidate["operations"][0]["forwards"] += 1
    candidate["operations"][0]["tokens"] = [{"ids": [1], "mask": [1]}]
    diffs, _ = compare(data, candidate, max_abs=1e-6, cosine_floor=.99)
    assert set(diffs) == {"operation 0: tokens", "operation 0: forwards"}


def test_ranking_swap_remains_visible():
    a = observation([[1., 0.], [0., 1.]])
    a["operations"].append(observation([[1., 0.]])["operations"][0])
    b = observation([[0., 1.], [1., 0.]])
    b["operations"].append(a["operations"][1])
    result = rankings(a, b)
    assert result["identical_full"] == result["identical_top8"] == 0


def test_fp32_near_tie_is_not_hidden_by_float64_scoring():
    q = np.zeros(384, dtype="f4")
    q[:2] = np.float32(1 / np.sqrt(2))
    docs = np.zeros((2, 384), dtype="f4")
    docs[:, 0] = 1.
    docs[1, 1] = np.float32(2**-25)
    a = observation(docs)
    a["operations"].append(observation([q])["operations"][0])
    docs[1, 1] = np.float32(2**-24)
    b = observation(docs)
    b["operations"].append(a["operations"][1])
    assert not compare(a, b, max_abs=1e-6, cosine_floor=.99999999997)[0]
    assert rankings(a, b)["identical_full"] == 0


def test_timeout_keeps_completed_arm_and_writes_failure_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(embedding.os, "environ", {"CUDA_VISIBLE_DEVICES": "-1"})
    monkeypatch.setattr(embedding.subprocess, "check_output", lambda *a, **kw: "fixture-head")
    monkeypatch.setattr(embedding, "digest", lambda p: "0" * 64)

    def run_arm(argv, **kwargs):
        if "--oracle" in argv:
            backend = argv[argv.index("--oracle") + 1]
            if backend == "onnx":
                raise subprocess.TimeoutExpired(argv, 600)
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps({"completed_marker": "retained", "dim": 2}))
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"fixture refusal")

    monkeypatch.setattr(embedding.subprocess, "run", run_arm)
    args = argparse.Namespace(model="fixture-model", onnx_file="onnx/model.onnx", prefix="query ",
        cap=32, ranking_banks=None, rust_bin=tmp_path / "candidate", mutant=None,
        accepted_fp32=False, qwen_position_ids_deferred=False, out=tmp_path / "result.json")
    assert embedding.run(args) == 1
    report = json.loads(args.out.read_text())
    assert report["arms"]["torch"]["completed_marker"] == "retained"
    assert report["arms"]["onnx"]["error"].startswith("TimeoutExpired")
