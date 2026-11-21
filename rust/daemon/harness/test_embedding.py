"""Comparator controls, independent of model inference or artifact downloads."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
import pytest

import embedding
from embedding import compare, rankings
from embedding_fixture import fixture_weights


def observation(rows):
    rows = np.array(rows, dtype="<f4")
    return {"dim": rows.shape[1], "copy_safe": True, "operations": [
        {"bits": rows.view("<u4").tolist(), "tokens": [], "forwards": 1}]}


def test_fixture_vectors_vary_after_normalization_and_have_mixed_signs():
    weights = fixture_weights(9)
    normalized = weights / np.linalg.norm(weights, axis=1, keepdims=True)
    assert np.any(weights < 0)
    assert np.count_nonzero(np.any(weights != 0, axis=0)) >= 2
    assert len({row.tobytes() for row in normalized}) > 1


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


def test_fixture_requires_recorded_vectors_and_keeps_them_read_only(tmp_path, monkeypatch):
    golden = tmp_path / "embedding-fixture.json"
    golden.write_text("committed vectors", encoding="utf-8")
    args = ["--fixture", "--golden", str(golden), "--rust-bin", str(tmp_path / "daemon")]
    calls = []
    monkeypatch.setattr(embedding, "fixture", lambda args: calls.append(args))
    for invalid in (args + ["--record", "--out", str(tmp_path / "report.json")],
                    args + ["--out", str(golden)]):
        with pytest.raises(SystemExit) as error:
            embedding.main(invalid)
        assert error.value.code == 2
    assert calls == []
    assert golden.read_text(encoding="utf-8") == "committed vectors"


def test_recorded_reference_checks_fresh_candidate_and_survives_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(embedding.os, "environ", {"CUDA_VISIBLE_DEVICES": "-1"})
    monkeypatch.setattr(embedding.subprocess, "check_output", lambda *a, **kw: "fixture-head")
    monkeypatch.setattr(embedding, "digest", lambda p: "0" * 64)
    monkeypatch.setattr(embedding, "operations", lambda: [
        {"texts": ["doc 1", "doc 2"]}, {"texts": ["query"], "query": True}])
    reference = tmp_path / "reference.json"
    config = {"model_name": "fixture-model", "onnx_file_name": "onnx/model.onnx",
              "query_prefix": "query ", "max_seq_length": 32, "batch_size": 3,
              "cache_size": 64, "cpu_dtype": "fp32"}
    corpus = json.dumps({"config": config, "operations": embedding.operations()}, sort_keys=True)
    recorded = observation([[1., 0.], [0., 1.]])
    recorded["operations"].append(observation([[1., 0.]])["operations"][0])
    recorded["identities"] = {"onnx/model.onnx": "recorded graph"}
    arms = {"torch": recorded, "onnx": recorded}
    orders = {"ranking": {"python_order": [[0, 1]]}, "replicated_slice": []}
    artifact = {"corpus_sha256": hashlib.sha256(corpus.encode()).hexdigest(),
                "ranking_pairs": [], "arms": arms, "torch_comparison": orders,
                "onnx_comparison": orders}
    reference.write_text(json.dumps(artifact), encoding="utf-8")

    args = argparse.Namespace(model="fixture-model", onnx_file="onnx/model.onnx", prefix="query ",
        cap=32, ranking_banks=None, rust_bin=tmp_path / "candidate", mutant=None,
        accepted_fp32=False, qwen_position_ids_deferred=False, reference=reference,
        model_root=tmp_path / "relocated", out=tmp_path / "result.json")

    def run_arm(argv, *, env, **kwargs):
        assert argv == [str(args.rust_bin)]
        assert env["HF_HUB_OFFLINE"] == "1"
        assert env["PSEUDOLIFE_DAEMON_ONNX_DIR"] == str(args.model_root)
        config_path = Path(json.loads(kwargs["input"])["config_path"])
        assert json.loads(config_path.read_text(encoding="utf-8"))["embedding"]["model_name"] == "fixture-model"
        if args.mutant == "timeout":
            raise subprocess.TimeoutExpired(argv, 600)
        candidate = json.loads(json.dumps(recorded))
        if args.mutant == "wrong-vectors":
            candidate["operations"][0]["bits"].reverse()
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(candidate).encode())

    monkeypatch.setattr(embedding.subprocess, "run", run_arm)
    assert embedding.run(args) == 0
    orders["ranking"]["python_order"] = [[1, 0]]
    reference.write_text(json.dumps(artifact), encoding="utf-8")
    assert embedding.run(args) == 1
    archived = json.loads(args.out.read_text())["torch_comparison"]
    assert archived["diffs"] == []
    assert archived["ranking"]["matches_recorded_order"] is False
    orders["ranking"]["python_order"] = [[0, 1]]
    reference.write_text(json.dumps(artifact), encoding="utf-8")
    args.mutant = "wrong-vectors"
    assert embedding.run(args) == 1
    assert json.loads(args.out.read_text())["torch_comparison"]["diffs"] == ["operation 0: vectors"]
    args.mutant = "timeout"
    assert embedding.run(args) == 1
    report = json.loads(args.out.read_text())
    assert {name: report["arms"][name] for name in arms} == arms
    assert report["arms"]["rust"]["error"].startswith("TimeoutExpired")
    assert report["reference_sha256"] == hashlib.sha256(reference.read_bytes()).hexdigest()

    args.cap = 16
    with pytest.raises(ValueError, match="does not match the requested corpus"):
        embedding.run(args)
