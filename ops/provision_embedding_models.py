"""Provision the daemon's embedding models without exporting ONNX files."""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from typing import Callable

from pseudolife_memory.onnx_artifacts import onnx_layout_available


QWEN_MODEL = "Qwen/Qwen3-Embedding-0.6B"
MINILM_MODEL = "all-MiniLM-L6-v2"
MINILM_REPO = "sentence-transformers/all-MiniLM-L6-v2"
MINILM_ONNX_FILE = "onnx/model.onnx"


def _native_windows() -> bool:
    return sys.platform == "win32"


def _snapshot_root(artifact: Path, file_name: str) -> Path:
    parts = PurePosixPath(file_name).parts
    if tuple(artifact.parts[-len(parts):]) != parts:
        raise RuntimeError(
            f"downloaded ONNX artifact {artifact} does not end with {file_name}",
        )
    root = artifact
    for _ in parts:
        root = root.parent
    return root


def provision_embedding_models(
    *,
    sentence_transformer: Callable | None = None,
    hf_download: Callable | None = None,
) -> None:
    """Download torch models and verify MiniLM's published ONNX artifact.

    The explicit file download fails the image build when the upstream model
    no longer publishes the configured artifact.  The final load receives the
    local snapshot root and ``export=False``; its exception is intentionally
    allowed to fail the build if the artifact's snapshot lacks model metadata.
    """
    if _native_windows():
        raise RuntimeError(
            "ONNX provisioning is not supported on native Windows with the "
            "pinned Optimum stack because it can mis-detect existing nested "
            "ONNX paths and enable export",
        )
    if sentence_transformer is None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        sentence_transformer = SentenceTransformer
    if hf_download is None:
        from huggingface_hub import hf_hub_download  # noqa: PLC0415

        hf_download = hf_hub_download

    sentence_transformer(QWEN_MODEL)
    sentence_transformer(MINILM_MODEL)
    artifact = Path(hf_download(
        repo_id=MINILM_REPO,
        filename=MINILM_ONNX_FILE,
    ))
    if not artifact.is_file():
        raise RuntimeError(f"downloaded ONNX artifact is missing: {artifact}")
    snapshot = _snapshot_root(artifact, MINILM_ONNX_FILE)
    if not onnx_layout_available(
        snapshot,
        MINILM_ONNX_FILE,
        allow_hub_blob_links=True,
    ):
        raise RuntimeError("ONNX module layout has no verified artifact for each Transformer")
    sentence_transformer(
        str(snapshot),
        backend="onnx",
        model_kwargs={"file_name": MINILM_ONNX_FILE, "export": False},
    )


if __name__ == "__main__":
    provision_embedding_models()
