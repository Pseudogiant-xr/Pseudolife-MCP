"""Compare native CPU embeddings with recorded vectors.

Invoked through run.py embedding. The offline fixture runs the native embedder
against committed vectors. Real-model checks take the raw report from a
historical three-arm run and compare its reference arms to a fresh native run.
"""
from __future__ import annotations

import argparse
import hashlib
import gzip
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PREFIX = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
# Delegate ruling, 2026-10-10: Qwen fp32, 78 replicated banks / 592 facts
# plus 12 canonical docs / 30 queries: Linux worst max abs 9.704e-7 and
# cosine loss 2.343e-11, rounded upward. ST5.3 / TF4.57.6 /
# ORT1.27; torch2.13 Linux / torch2.10 Windows. No token/cache/rank tolerance.
FP32_MAX_ABS = 1e-6
FP32_MIN_COSINE = 0.99999999997
QWEN_FP32_IDENTITIES = [
    {"onnx/model.onnx": "f5fdee679d0ffb98e1cb0bb178e8ee2b86739ab7ab035a3a9327761d5c6b1f0b",
     "onnx/model.onnx_data": "a585477de21c0a89e021dd64f4d3be34483eb4aaed7ae93fc047e3edf74545da",
     "tokenizer.json": "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"},
    # The provisioned public export is measured independently of the
    # historical export above; it uses the same bounds and exact checks.
    # Admission evidence (default corpus, ORT 1.27 image binary):
    # https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/1114
    {"onnx/model.onnx": "cec22565ec783289a5e51bd94950f70b8cb7ca6c0b7ced255b5e8dbf3c60536b",
     "onnx/model.onnx_data": "a585477de21c0a89e021dd64f4d3be34483eb4aaed7ae93fc047e3edf74545da",
     "tokenizer.json": "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"},
]
ACCEPTED_FP32_IDENTITIES = [
    *QWEN_FP32_IDENTITIES,
    {"onnx/model.onnx": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
     "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037"},
]
DOCUMENTS = [
    "The build uses one isolated worktree per change and runs targeted checks before opening a pull request.",
    "Deployment follows backup, rollback tag, daemon replacement and a health check, then verifies the changed path.",
    "The current value in a cortex slot supersedes its predecessor while retaining history.",
    "A contender is a competing fact awaiting resolution; it does not silently overwrite the current value.",
    "Queries carry an instruction prefix; stored documents preserve their original text.",
    "The hybrid logical clock supplies monotonic ordering for fact updates.",
    "The dream cursor advances after extraction so each memory is processed once.",
    "An episode groups work from one session and carries its title on stored entries.",
    "A shared-resource lease coordinates independent sessions. A status line does not reserve a resource.",
    "Unicode note: café, cafe\u0301, 日本語, Ελληνικά, 🧠, Straße and a nonbreaking\u00a0space.",
    "  Boundary whitespace is stripped during tokenization, while the original text remains the cache key.\n",
    "Truncation record: " + "memory cursor supersession cache query document " * 150 + " final marker",
]


def operations():
    queries = json.loads((HERE / "queries.json").read_text(encoding="utf-8"))
    return [
        {"texts": DOCUMENTS},
        {"texts": queries, "query": True},
        {"texts": [DOCUMENTS[0], "fresh cache miss", DOCUMENTS[0]]},
        {"texts": ["normalize cache partition"], "normalize": False},
        {"texts": ["normalize cache partition"]},
        {"texts": ["normalize cache partition"], "normalize": False},
        {"texts": ["duplicate miss", "duplicate miss", "different length duplicate control"]},
        {"texts": []},
    ]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def compare(oracle_data, candidate, max_abs=0., cosine_floor=1.):
    import numpy as np
    diffs, metrics = [], []
    if oracle_data["dim"] != candidate["dim"] or not candidate["copy_safe"]:
        diffs.append("dimension or cache copy control")
    if len(oracle_data["operations"]) != len(candidate["operations"]):
        return ["incomplete operation count"], []
    for i, (py, rs) in enumerate(zip(oracle_data["operations"], candidate["operations"])):
        for field in ("tokens", "forwards"):
            if py[field] != rs[field]:
                diffs.append(f"operation {i}: {field}")
        a = np.array(py["bits"], dtype="<u4").view("<f4")
        b = np.array(rs["bits"], dtype="<u4").view("<f4")
        if a.shape != b.shape or not np.isfinite(a).all() or not np.isfinite(b).all():
            diffs.append(f"operation {i}: vector shape/non-finite")
            continue
        if not a.size:
            continue
        abs_diff = float(np.max(np.abs(a.astype("f8") - b.astype("f8"))))
        af, bf = a.astype("f8"), b.astype("f8")
        an, bn = np.linalg.norm(af, axis=1), np.linalg.norm(bf, axis=1)
        denom = an * bn
        cos = np.divide(np.sum(af * bf, axis=1), denom, out=np.zeros_like(denom), where=denom != 0)
        cos[(an == 0) & (bn == 0)] = 1.
        # Map IEEE sign/magnitude to monotonic integers; both signed zeroes
        # occupy the same point. Near-zero ULP counts can be large.
        def ordered(v):
            bits = v.view("<u4").astype("i8")
            return np.where(bits & 0x80000000, 0x80000000 - (bits & 0x7fffffff), bits + 0x80000000)
        ulp = int(np.max(np.abs(ordered(a) - ordered(b))))
        exact = bool(np.array_equal(a.view("u4"), b.view("u4")))
        metrics.append({"operation": i, "exact": exact, "max_abs": abs_diff,
                        "max_ulp": ulp, "min_cosine": float(np.min(cos))})
        differs = (not exact if max_abs == 0. else
                   abs_diff > max_abs or float(np.min(cos)) < cosine_floor)
        if differs:
            diffs.append(f"operation {i}: vectors")
    return diffs, metrics


def rankings(py, rs, document_op=0, query_op=1, *, recorded_order=None):
    import numpy as np
    import torch
    torch.set_num_threads(3)
    def arrays(data):
        docs = torch.from_numpy(np.array(data["operations"][document_op]["bits"], dtype="<u4").view("<f4"))
        queries = torch.from_numpy(np.array(data["operations"][query_op]["bits"], dtype="<u4").view("<f4"))
        # The replicated slice's shared dense consumer, cortex.py:1519-1524
        # and rebuild_contexts.py:114-120: CPU fp32 normalization then mat@q.
        # This isolates embedding replacement; it does not certify the Rust
        # search scorer, assistant demotion, filters, fusion or floor policy.
        docs = docs / (docs.norm(dim=1, keepdim=True) + 1e-12)
        queries = queries / (queries.norm(dim=1, keepdim=True) + 1e-12)
        return [sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
                for scores in [(docs @ q).tolist() for q in queries]]
    a, b = arrays(py), arrays(rs)
    result = {"queries": len(a), "identical_full": sum(x == y for x, y in zip(a, b)),
              "identical_top8": sum(x[:8] == y[:8] for x, y in zip(a, b)),
              "python_order": a, "rust_order": b}
    if recorded_order is not None:
        result["matches_recorded_order"] = b == recorded_order
    return result


def run(args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDOLIFE_")}
    env.update(CUDA_VISIBLE_DEVICES="-1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               PSEUDOLIFE_EMBEDDING_CPU_DTYPE="fp32", OMP_NUM_THREADS="3", MKL_NUM_THREADS="3",
               TOKENIZERS_PARALLELISM="false", PYTHONUTF8="1")
    config = {"model_name": args.model, "onnx_file_name": args.onnx_file,
              "query_prefix": args.prefix, "max_seq_length": args.cap,
              "batch_size": 3, "cache_size": 64, "cpu_dtype": "fp32"}
    request = {"config": config, "operations": operations()}
    ranking_pairs = []
    if args.ranking_banks:
        # The pinned replicated eval's disposable bank dumps, read-only.
        # No current/live bank or answer/judge endpoint is consulted.
        for path in sorted(args.ranking_banks.glob("*.json.gz")):
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                bank = json.load(stream)
            texts = [f"{f['entity']} {f['attribute']} {f['value']}".strip() for f in bank["facts"]]
            if not texts:
                raise ValueError("ranking bank has no facts")
            i = len(request["operations"])
            request["operations"].extend([{"texts": texts}, {"texts": [bank["question"]], "query": True}])
            ranking_pairs.append({"documents": i, "query": i+1, "bank_sha256": digest(path)})
        if len(ranking_pairs) != 78:
            raise ValueError("replicated eval slice requires all 78 banks")
    report = {"status": "exact-comparison", "platform": sys.platform,
              "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
              "corpus_sha256": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
              "config": config, "arms": {}, "declared": []}
    report["policy"] = {"max_abs": FP32_MAX_ABS if args.accepted_fp32 else 0.,
                        "min_cosine": FP32_MIN_COSINE if args.accepted_fp32 else 1.,
                        "tokens_cache_ranks": "exact"}
    report["dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO, text=True).strip())
    report["binary_sha256"] = digest(args.rust_bin)
    report["source_sha256"] = {p.relative_to(REPO).as_posix(): digest(p) for p in
        [REPO / "rust/daemon/src/embed.rs", REPO / "rust/daemon/src/embedding_math.rs",
         REPO / "rust/daemon/src/onnx_artifacts.rs", REPO / "rust/daemon/src/model_provision.rs",
         REPO / "rust/daemon/src/onnx_runtime.rs", Path(__file__)]}
    report["ranking_pairs"] = ranking_pairs
    reference_bytes = args.reference.read_bytes()
    reference = json.loads(reference_bytes)
    if (reference["corpus_sha256"] != report["corpus_sha256"]
            or reference["ranking_pairs"] != ranking_pairs):
        raise ValueError("recorded embedding reference does not match the requested corpus and config")
    report["reference_sha256"] = hashlib.sha256(reference_bytes).hexdigest()
    report["arms"] = {backend: reference["arms"][backend] for backend in ("torch", "onnx")}
    report["reference_rankings"] = {}
    for backend, arm in report["arms"].items():
        if "error" not in arm:
            recorded = reference[backend + "_comparison"]
            report["reference_rankings"][backend] = {
                "canonical": recorded["ranking"]["python_order"],
                "replicated": [row["python_order"] for row in recorded["replicated_slice"]],
            }
    with tempfile.TemporaryDirectory(prefix="pl_cf_emb_") as tmp:
        tmp = Path(tmp)
        cfg_path = tmp / "config.yaml"
        cfg_path.write_text(json.dumps({"embedding": dict(config, backend="onnx", device="cpu")}), encoding="utf-8")
        rs_env = dict(env, PSEUDOLIFE_DAEMON_EMBED_PROBE="1")
        if args.model_root is not None:
            rs_env["PSEUDOLIFE_DAEMON_ONNX_DIR"] = str(args.model_root.resolve())
        if args.mutant:
            rs_env["PSEUDOLIFE_DAEMON_MUTANT"] = args.mutant
        try:
            proc = subprocess.run([str(args.rust_bin.resolve())], env=rs_env, capture_output=True,
                                  input=json.dumps({"config_path": str(cfg_path), "operations": request["operations"]}).encode(),
                                  timeout=600)
        except subprocess.TimeoutExpired:
            proc = None
            report["arms"]["rust"] = {"error": "TimeoutExpired: Rust arm exceeded 600 seconds"}
        if proc is not None and proc.returncode:
            report["arms"]["rust"] = {"error": proc.stderr.decode(errors="replace"), "exit": proc.returncode}
        elif proc is not None:
            candidate = json.loads(proc.stdout)
            candidate.pop("model_root", None)
            candidate.pop("graph", None)
            report["arms"]["rust"] = candidate
    return finish_report(report, args)


def finish_report(report, args):
    errors = []
    rs = report["arms"]["rust"]
    for backend in ["torch", "onnx"]:
        py = report["arms"][backend]
        if "error" in py or "error" in rs:
            known_qwen_failure = (backend == "onnx" and args.qwen_position_ids_deferred
                and py.get("error") == "ValueError: Input position_ids is required by model but not provided."
                and "error" not in rs
                and any(all(rs.get("identities", {}).get(k) == v for k, v in allowed.items())
                        for allowed in QWEN_FP32_IDENTITIES))
            if known_qwen_failure:
                report["declared"].append("Qwen Python ONNX wrapper fails missing position_ids; shipped torch fp32 is the oracle, ONNX comparison unavailable")
                continue
            errors.append(f"{backend}: arm unavailable")
            continue
        identities = rs.get("identities", {})
        if not identities or any(py["identities"].get(key) != value for key, value in identities.items()):
            errors.append(f"{backend}: graph/tokenizer identity differs")
        if args.accepted_fp32 and not any(all(identities.get(k) == v for k, v in allowed.items())
                                         for allowed in ACCEPTED_FP32_IDENTITIES):
            errors.append(f"{backend}: artifact has no accepted fp32 tolerance")
        diffs, metrics = compare(py, rs,
            FP32_MAX_ABS if args.accepted_fp32 else 0.,
            FP32_MIN_COSINE if args.accepted_fp32 else 1.)
        recorded = report.get("reference_rankings")
        reference = recorded[backend] if recorded is not None else None
        expected = reference["canonical"] if reference is not None else None
        rank = rankings(py, rs, recorded_order=expected)
        report[backend + "_comparison"] = {"diffs": diffs, "metrics": metrics, "ranking": rank}
        pairs = report["ranking_pairs"]
        expected_slices = reference["replicated"] if reference is not None else [None] * len(pairs)
        slice_ranks = [rankings(py, rs, pair["documents"], pair["query"], recorded_order=order)
                       for pair, order in zip(pairs, expected_slices, strict=True)]
        report[backend + "_comparison"]["replicated_slice"] = slice_ranks
        if diffs or rank["identical_full"] != rank["queries"] or not rank.get("matches_recorded_order", True):
            errors.append(f"{backend}: differs")
        if any(r["identical_full"] != r["queries"] or not r.get("matches_recorded_order", True)
               for r in slice_ranks):
            errors.append(f"{backend}: replicated slice rank differs")
    report["errors"] = errors
    report["rank_consumer"] = {"name": "cortex/rebuild_contexts dense cosine",
        "dtype": "float32", "threads": 3, "torch": importlib.metadata.version("torch"),
        "excluded": ["assistant demotion", "filters", "score floor", "fusion", "Rust search arithmetic"],
        "comparator_sha256": digest(Path(__file__))}
    args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"embedding artifact: {args.out}; exact diffs: {errors}", flush=True)
    return int(bool(errors))


def main(argv=None):
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true")
    mode.add_argument("--reference", type=Path, help="raw historical three-arm report for a fresh native comparison")
    mode.add_argument("--recompare", type=Path, help="recheck the observations in an existing report")
    ap.add_argument("--rust-bin", type=Path)
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--model-root", type=Path,
                    help="use relocated local artifacts without changing the recorded --model setting")
    ap.add_argument("--onnx-file", default="onnx/model.onnx")
    ap.add_argument("--prefix", default=PREFIX)
    ap.add_argument("--cap", type=int, default=512)
    ap.add_argument("--mutant")
    ap.add_argument("--ranking-banks", type=Path)
    ap.add_argument("--golden", type=Path)
    ap.add_argument("--accepted-fp32", action="store_true",
                    help="apply the declared fp32 bounds; exact token/cache/rank checks remain")
    ap.add_argument("--qwen-position-ids-deferred", action="store_true",
                    help="record only the identified Qwen wrapper failure as a declared divergence")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if not args.recompare and args.rust_bin is None:
        ap.error("--rust-bin is required to run the native embedder")
    if args.fixture != (args.golden is not None):
        ap.error("--fixture and --golden must be used together")
    if args.model_root is not None and args.reference is None:
        ap.error("--model-root requires --reference")
    source = args.golden or args.recompare or args.reference
    if (args.out.resolve() == source.resolve()
            or (args.out.exists() and source.exists() and args.out.samefile(source))):
        ap.error("--out must preserve the input artifact")
    if args.fixture:
        return fixture(args)
    if args.recompare:
        source_bytes = args.recompare.read_bytes()
        report = json.loads(source_bytes)
        report["recomparison_of_sha256"] = hashlib.sha256(source_bytes).hexdigest()
        return finish_report(report, args)
    return run(args)


def fixture(args):
    profiles = [(2, "query ", "mean"), (0, "", "mean"), (-2, "", "mean"), (2, "query ", "last-left-normalize")]
    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    if len(golden["profiles"]) != len(profiles):
        raise ValueError("embedding golden must contain one observation for each fixture profile")

    import onnxruntime
    from embedding_fixture import create
    env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDOLIFE_")}
    env.update(CUDA_VISIBLE_DEVICES="-1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               PSEUDOLIFE_EMBEDDING_CPU_DTYPE="fp32", TOKENIZERS_PARALLELISM="false", PYTHONUTF8="1")
    runtime_dir = Path(onnxruntime.__file__).parent / "capi"
    runtime = (runtime_dir / "onnxruntime.dll" if os.name == "nt"
               else next(runtime_dir.glob("libonnxruntime.so.*")))
    env["ORT_DYLIB_PATH"] = str(runtime)
    mutants = ["embedding-no-prefix", "embedding-wrong-pool", "embedding-no-normalize",
               "embedding-no-truncation", "embedding-cache-disabled", "embedding-cache-key",
               "embedding-mean-mask-blind", "embedding-batch-row-offset"]
    report = {"platform": sys.platform, "mutants": {}, "profiles": [], "refusals": [],
              "oracle_adjustment": golden["oracle_adjustment"]}
    with tempfile.TemporaryDirectory(prefix="pl_cf_emb_fixture_") as tmp:
        root = Path(tmp)
        create(root / "model")
        create(root / "last-model", last_token=True)
        config = {"model_name": str(root / "model"), "batch_size": 3, "cache_size": 2,
                  "max_seq_length": 32, "query_prefix": "query ", "cpu_dtype": "fp32"}
        ops = [{"texts": ["memory cursor", "cache", "memory cursor " * 100, "日本語 café 🧠"]},
               {"texts": ["memory cursor"], "query": True},
               {"texts": ["normalize cache partition"], "normalize": False},
               {"texts": ["normalize cache partition"]},
               {"texts": ["normalize cache partition"], "normalize": False},
               {"texts": ["memory cursor", "cache", "memory cursor"]},
               {"texts": ["document cache cursor", "cursor"], "normalize": False},
               {"texts": []}]
        for (cache_size, prefix, pooling_profile), expected in zip(profiles, golden["profiles"], strict=True):
            is_last = pooling_profile == "last-left-normalize"
            config.update(cache_size=cache_size, query_prefix=prefix,
                          model_name=str(root / ("last-model" if is_last else "model")))
            cfg_file = root / "config.yaml"
            cfg_file.write_text(json.dumps({"embedding": dict(config, backend="onnx", device="cpu")}), encoding="utf-8")
            request_bytes = json.dumps({"config_path": str(cfg_file), "operations": ops}).encode()

            def candidate(mutant=None):
                child_env = dict(env, PSEUDOLIFE_DAEMON_EMBED_PROBE="1")
                if mutant:
                    child_env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
                proc = subprocess.run([str(args.rust_bin.resolve())], input=request_bytes,
                                      env=child_env, capture_output=True, timeout=120)
                if proc.returncode:
                    raise RuntimeError(proc.stderr.decode(errors="replace"))
                result = json.loads(proc.stdout)
                result.pop("model_root", None)
                result.pop("graph", None)
                result.pop("identities", None)
                return result

            diffs, metrics = compare(expected, candidate())
            report["profiles"].append({"cache_size": cache_size, "prefix": prefix, "pooling": pooling_profile,
                                       "diffs": diffs, "metrics": metrics})
            if diffs:
                break
            if cache_size == 2 and not is_last:
                for mutant in mutants:
                    differences, _ = compare(expected, candidate(mutant))
                    report["mutants"][mutant] = differences
                for case, patch, extra_env in [
                    ("invalid backend", {"backend": "other"}, {}),
                    ("invalid dtype override", {}, {"PSEUDOLIFE_EMBEDDING_CPU_DTYPE": "invalid"}),
                    ("bf16 ONNX", {}, {"PSEUDOLIFE_EMBEDDING_CPU_DTYPE": "bf16"}),
                    ("zero batch", {"batch_size": 0}, {}),
                    ("escaping artifact", {"onnx_file_name": "../model.onnx"}, {}),
                    ("missing artifact", {"onnx_file_name": "onnx/missing.onnx"}, {}),
                    ("Qwen dimension", {"model_name": "Qwen/Qwen3-Embedding-0.6B"},
                        {"PSEUDOLIFE_DAEMON_ONNX_DIR": str(root / "model")}),
                    ("MiniLM dimension", {"model_name": "sentence-transformers/all-MiniLM-L6-v2"},
                        {"PSEUDOLIFE_DAEMON_ONNX_DIR": str(root / "model")}),
                ]:
                    cfg_file.write_text(json.dumps({"embedding": {**config, "backend": "onnx", "device": "cpu", **patch}}), encoding="utf-8")
                    proc = subprocess.run([str(args.rust_bin.resolve())], input=request_bytes,
                        env=dict(env, PSEUDOLIFE_DAEMON_EMBED_PROBE="1", **extra_env),
                        capture_output=True, timeout=120)
                    refused = proc.returncode != 0 and not proc.stdout
                    if case == "bf16 ONNX":
                        refused = refused and all(part in proc.stderr for part in (
                            b"embedding.cpu_dtype", b"PSEUDOLIFE_EMBEDDING_CPU_DTYPE",
                            b"fp32", b"auto", b"Python engine"))
                    report["refusals"].append({"case": case, "refused": bool(refused)})
                saved_prompt_file = root / "model/config_sentence_transformers.json"
                saved_prompt_file.write_text(json.dumps({"prompts": {"query": "query "}, "default_prompt_name": "query"}), encoding="utf-8")
                cfg_file.write_text(json.dumps({"embedding": dict(config, backend="onnx", device="cpu")}), encoding="utf-8")
                proc = subprocess.run([str(args.rust_bin.resolve())], input=request_bytes,
                    env=dict(env, PSEUDOLIFE_DAEMON_EMBED_PROBE="1"), capture_output=True, timeout=120)
                report["refusals"].append({"case": "saved default prompt", "refused": proc.returncode != 0 and not proc.stdout})
                saved_prompt_file.unlink()
    failures = [p for p in report["profiles"] if p["diffs"]]
    survivors = [m for m, diffs in report["mutants"].items() if not diffs]
    report["survivors"] = survivors
    args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(f"embedding fixture: {len(report['profiles'])} profiles, {len(failures)} failed, survivors={survivors}", flush=True)
    return int(bool(failures or survivors or any(not r["refused"] for r in report["refusals"])))


if __name__ == "__main__":
    raise SystemExit(main())
