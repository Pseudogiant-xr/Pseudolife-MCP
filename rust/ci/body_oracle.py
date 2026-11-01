"""Prepare offline Python models and bind provisioned native model roots.

The native `provision models` command owns artifact integrity. This step
only discovers its immutable layout and warms the Python oracle's cache.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def native_paths(models: Path, runtime: Path) -> dict[str, str]:
    versions = sorted(p for p in models.glob("qwen3-msmarco-*") if p.is_dir())
    if len(versions) != 1:
        raise ValueError("expected exactly one provisioned model version in this job's model directory")
    roots = {"PSEUDOLIFE_DAEMON_ONNX_DIR": versions[0] / "embedding",
             "PSEUDOLIFE_DAEMON_RERANK_DIR": versions[0] / "reranker"}
    for root in roots.values():
        if not (root / "onnx/model.onnx").is_file() or not (root / "tokenizer.json").is_file():
            raise ValueError("provisioned model root lacks its graph or tokenizer")
    libraries = sorted(runtime.glob("libonnxruntime.so.*"))
    if len(libraries) != 1:
        raise ValueError("expected one ONNX Runtime library from the pinned Python wheel")
    return {**{key: str(value.resolve()) for key, value in roots.items()},
            "ORT_DYLIB_PATH": str(libraries[0].resolve())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-env", type=Path, required=True)
    args = parser.parse_args()
    import onnxruntime
    from ops.provision_embedding_models import provision_embedding_models
    from pseudolife_memory.utils.config import RerankerConfig
    from sentence_transformers import CrossEncoder

    paths = native_paths(Path(os.environ["PSEUDOLIFE_DAEMON_MODELS_DIR"]),
                         Path(onnxruntime.__file__).parent / "capi")
    provision_embedding_models()
    CrossEncoder(RerankerConfig().model_name, device="cpu")
    with args.github_env.open("a", encoding="utf-8") as stream:
        for key, value in paths.items():
            if "\n" in value or "\r" in value:
                raise ValueError("a native artifact path cannot be written to GITHUB_ENV")
            stream.write(f"{key}={value}\n")
    print("Python embedding and reranker caches ready; native artifact roots bound.", flush=True)


if __name__ == "__main__":
    main()
