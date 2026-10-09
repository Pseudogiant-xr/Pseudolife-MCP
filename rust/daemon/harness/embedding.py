"""Sequential CPU-only embedding arms; exact comparisons remain the default.

Invoked through run.py embedding. Model loading is isolated in child processes:
the Python process exits before the Rust process loads its graph. Measurements
write an artifact even when bit parity fails or the shipped ONNX wrapper fails.
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
# Delegate ruling, 2026-10-10: Qwen fp32 Windows, 78 replicated banks /
# 592 facts plus 12 canonical docs / 30 queries: observed worst max abs
# 9.69e-7, cosine loss 2.343e-11 (Linux), rounded upward. ST5.3 / TF4.57.6 /
# ORT1.27; torch2.13 Linux / torch2.10 Windows. No token/cache/rank tolerance.
FP32_MAX_ABS = 1e-6
FP32_MIN_COSINE = 0.99999999997
ACCEPTED_FP32_IDENTITIES = [
    {"onnx/model.onnx": "f5fdee679d0ffb98e1cb0bb178e8ee2b86739ab7ab035a3a9327761d5c6b1f0b",
     "onnx/model.onnx_data": "a585477de21c0a89e021dd64f4d3be34483eb4aaed7ae93fc047e3edf74545da",
     "tokenizer.json": "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"},
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


def oracle(request, backend):
    import numpy as np
    import torch
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "evals"))
    import embedder_stamp
    from pseudolife_memory.memory.embedding import EmbeddingPipeline, _configured_onnx_source
    from pseudolife_memory.utils.config import EmbeddingConfig
    torch.set_num_threads(3)
    torch.set_num_interop_threads(1)
    cfg = EmbeddingConfig(**request["config"], backend=backend, device="cpu")
    file_name, root = _configured_onnx_source(cfg)
    if root is None:
        raise RuntimeError("configured graph unavailable; no torch-only result substitutes for ONNX identity")
    pipe = EmbeddingPipeline(cfg)
    stamp = embedder_stamp.describe(pipe)
    forwards = 0
    original = pipe.model.encode

    def counted(texts, **kwargs):
        nonlocal forwards
        forwards += (len(texts) + cfg.batch_size - 1) // cfg.batch_size
        return original(texts, **kwargs)

    pipe.model.encode = counted
    results = []
    for op in request["operations"]:
        texts, norm = op["texts"], op.get("normalize", True)
        query = op.get("query", False)
        if query:
            rows = torch.stack([pipe.encode_query(t, normalize=norm) for t in texts])
        else:
            rows = pipe.encode(texts, normalize=norm)
        token_texts = [cfg.query_prefix + t if query else t for t in texts]
        tokenized = pipe.model.tokenize(token_texts) if texts else None
        tokens = [{"ids": ids, "mask": mask} for ids, mask in zip(
            tokenized["input_ids"].tolist(), tokenized["attention_mask"].tolist())] if texts else []
        bits = rows.detach().cpu().numpy().astype("<f4").view("<u4").tolist()
        results.append({"bits": bits, "tokens": tokens, "forwards": forwards})
    first = pipe.encode_single("cache copy control")
    expected = first.clone()
    first.fill_(99)
    copy_safe = torch.equal(pipe.encode_single("cache copy control"), expected)
    root = Path(root)
    modules = json.loads((root / "modules.json").read_text()) if (root / "modules.json").exists() else []
    transformer_path = next((m["path"] for m in modules if m["type"].endswith(".Transformer")), "")
    graph = root / transformer_path / file_name
    identities = {p.relative_to(root).as_posix(): digest(p) for p in [graph, root / transformer_path / "tokenizer.json"]}
    external = graph.with_name(graph.name + "_data")
    if external.exists():
        identities[external.relative_to(root).as_posix()] = digest(external)
    for p in [root / "modules.json", root / transformer_path / "config.json",
              root / transformer_path / "tokenizer_config.json", root / transformer_path / "model.safetensors",
              root / transformer_path / "sentence_bert_config.json"] + [
              root / m["path"] / "config.json" for m in modules if m["type"].endswith(".Pooling")]:
        if p.exists():
            identities[p.relative_to(root).as_posix()] = digest(p)
    return {"dim": pipe.embedding_dim, "operations": results, "copy_safe": bool(copy_safe),
            "embedder": stamp, "identities": identities,
            "versions": {n: importlib.metadata.version(n) for n in
                         ["torch", "sentence-transformers", "transformers", "tokenizers", "onnxruntime"]}}


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


def rankings(py, rs, document_op=0, query_op=1):
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
    return {"queries": len(a), "identical_full": sum(x == y for x, y in zip(a, b)),
            "identical_top8": sum(x[:8] == y[:8] for x, y in zip(a, b)),
            "python_order": a, "rust_order": b}


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
         REPO / "rust/daemon/src/onnx_artifacts.rs", REPO / "rust/daemon/src/onnx_runtime.rs", Path(__file__)]}
    report["ranking_pairs"] = ranking_pairs
    with tempfile.TemporaryDirectory(prefix="pl_cf_emb_") as tmp:
        tmp = Path(tmp)
        req_path = tmp / "request.json"
        req_path.write_text(json.dumps(request), encoding="utf-8")
        cfg_path = tmp / "config.yaml"
        cfg_path.write_text(json.dumps({"embedding": dict(config, backend="onnx", device="cpu")}), encoding="utf-8")
        for backend in ["torch", "onnx"]:
            print(f"embedding Python {backend}: starting CPU arm", flush=True)
            out = tmp / f"{backend}.json"
            try:
                proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--oracle", backend,
                                       "--request", str(req_path), "--out", str(out)],
                                      env=env, capture_output=True, timeout=600)
            except subprocess.TimeoutExpired:
                report["arms"][backend] = {"error": f"TimeoutExpired: Python {backend} arm exceeded 600 seconds"}
                print(f"embedding Python {backend}: timeout", flush=True)
                continue
            data = json.loads(out.read_text()) if out.exists() else {"error": "oracle produced no artifact"}
            report["arms"][backend] = data
            data["exit"] = proc.returncode
            if proc.returncode and "error" not in data:
                report["arms"][backend] = {"error": f"oracle exited {proc.returncode}", "partial": data}
            print(f"embedding Python {backend}: exit {proc.returncode}", flush=True)
        rs_env = dict(env, PSEUDOLIFE_DAEMON_EMBED_PROBE="1")
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
                and all(rs.get("identities", {}).get(k) == v for k, v in ACCEPTED_FP32_IDENTITIES[0].items()))
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
        rank = rankings(py, rs)
        report[backend + "_comparison"] = {"diffs": diffs, "metrics": metrics, "ranking": rank}
        slice_ranks = [rankings(py, rs, pair["documents"], pair["query"]) for pair in report["ranking_pairs"]]
        report[backend + "_comparison"]["replicated_slice"] = slice_ranks
        if diffs or rank["identical_full"] != rank["queries"]:
            errors.append(f"{backend}: differs")
        if any(r["identical_full"] != r["queries"] for r in slice_ranks):
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
    ap.add_argument("--oracle", choices=["torch", "onnx"])
    ap.add_argument("--request", type=Path)
    ap.add_argument("--recompare", type=Path)
    ap.add_argument("--rust-bin", type=Path)
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--onnx-file", default="onnx/model.onnx")
    ap.add_argument("--prefix", default=PREFIX)
    ap.add_argument("--cap", type=int, default=512)
    ap.add_argument("--mutant")
    ap.add_argument("--ranking-banks", type=Path)
    ap.add_argument("--fixture", action="store_true")
    ap.add_argument("--golden", type=Path)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--accepted-fp32", action="store_true",
                    help="apply the declared fp32 bounds; exact token/cache/rank checks remain")
    ap.add_argument("--qwen-position-ids-deferred", action="store_true",
                    help="record only the identified Qwen wrapper failure as a declared divergence")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.fixture:
        return fixture(args)
    if args.recompare:
        if args.out.resolve() == args.recompare.resolve():
            raise ValueError("recomparison must preserve the original artifact")
        report = json.loads(args.recompare.read_text(encoding="utf-8"))
        report["recomparison_of_sha256"] = digest(args.recompare)
        return finish_report(report, args)
    if args.oracle:
        try:
            result = oracle(json.loads(args.request.read_text()), args.oracle)
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        args.out.write_text(json.dumps(result), encoding="utf-8")
        return int("error" in result)
    return run(args)


def fixture(args):
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
               "embedding-no-truncation", "embedding-cache-disabled", "embedding-cache-key"]
    report = {"platform": sys.platform, "mutants": {}, "profiles": [], "refusals": []}
    with tempfile.TemporaryDirectory(prefix="pl_cf_emb_fixture_") as tmp:
        root = Path(tmp)
        create(root / "model")
        config = {"model_name": str(root / "model"), "batch_size": 3, "cache_size": 2,
                  "max_seq_length": 32, "query_prefix": "query ", "cpu_dtype": "fp32"}
        ops = [{"texts": ["memory cursor", "cache", "memory cursor " * 100, "日本語 café 🧠"]},
               {"texts": ["memory cursor"], "query": True},
               {"texts": ["normalize cache partition"], "normalize": False},
               {"texts": ["normalize cache partition"]},
               {"texts": ["normalize cache partition"], "normalize": False},
               {"texts": ["memory cursor", "cache", "memory cursor"]},
               {"texts": []}]
        goldens = []
        profiles = [(2, "query "), (0, ""), (-2, "")]
        for cache_size, prefix in profiles:
            config.update(cache_size=cache_size, query_prefix=prefix)
            req = {"config": dict(config), "operations": ops}
            req_file = root / "request.json"
            req_file.write_text(json.dumps(req), encoding="utf-8")
            cfg_file = root / "config.yaml"
            cfg_file.write_text(json.dumps({"embedding": dict(config, backend="onnx", device="cpu")}), encoding="utf-8")
            if args.record:
                py_file = root / "python.json"
                proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--oracle", "onnx",
                                       "--request", str(req_file), "--out", str(py_file)],
                                      env=env, capture_output=True, timeout=120)
                if proc.returncode:
                    raise RuntimeError(py_file.read_text())
                expected = json.loads(py_file.read_text())
                for key in ["identities", "versions", "embedder"]:
                    expected.pop(key, None)
                goldens.append(expected)
            else:
                expected = json.loads(args.golden.read_text())["profiles"][len(report["profiles"])]
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
            report["profiles"].append({"cache_size": cache_size, "prefix": prefix,
                                       "diffs": diffs, "metrics": metrics})
            if diffs:
                break
            if cache_size == 2:
                for mutant in mutants:
                    differences, _ = compare(expected, candidate(mutant))
                    report["mutants"][mutant] = differences
                for case, patch, extra_env in [
                    ("invalid backend", {"backend": "other"}, {}),
                    ("invalid dtype override", {}, {"PSEUDOLIFE_EMBEDDING_CPU_DTYPE": "invalid"}),
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
                    report["refusals"].append({"case": case, "refused": proc.returncode != 0 and not proc.stdout})
                saved_prompt_file = root / "model/config_sentence_transformers.json"
                saved_prompt_file.write_text(json.dumps({"prompts": {"query": "query "}, "default_prompt_name": "query"}), encoding="utf-8")
                cfg_file.write_text(json.dumps({"embedding": dict(config, backend="onnx", device="cpu")}), encoding="utf-8")
                proc = subprocess.run([str(args.rust_bin.resolve())], input=request_bytes,
                    env=dict(env, PSEUDOLIFE_DAEMON_EMBED_PROBE="1"), capture_output=True, timeout=120)
                report["refusals"].append({"case": "saved default prompt", "refused": proc.returncode != 0 and not proc.stdout})
                saved_prompt_file.unlink()
        if args.record and not any(p["diffs"] for p in report["profiles"]):
            args.golden.write_text(json.dumps({"fixture": "embedding_fixture.py", "profiles": goldens}, indent=1) + "\n", encoding="utf-8")
    failures = [p for p in report["profiles"] if p["diffs"]]
    survivors = [m for m, diffs in report["mutants"].items() if not diffs]
    report["survivors"] = survivors
    args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(f"embedding fixture: {len(report['profiles'])} profiles, {len(failures)} failed, survivors={survivors}", flush=True)
    return int(bool(failures or survivors or any(not r["refused"] for r in report["refusals"])))


if __name__ == "__main__":
    raise SystemExit(main())
