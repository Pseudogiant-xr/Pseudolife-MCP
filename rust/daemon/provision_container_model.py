"""Bake a pinned public fp32 Qwen export; downloads happen only at build time.

The tensors and tokenizer match the historical CPU prerequisite receipt.
The public graph differs, so this image makes no embedding parity claim.
Source: https://huggingface.co/onnx-community/ONNX_Qwen3-Embedding-0.6B
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import urllib.request

REVISION = "462e5a71e724575c710975d9b79309b690fd22ce"
BASE = "https://huggingface.co/onnx-community/ONNX_Qwen3-Embedding-0.6B/resolve/" + REVISION
FILES = {
    "model.onnx": "cec22565ec783289a5e51bd94950f70b8cb7ca6c0b7ced255b5e8dbf3c60536b",
    "model.onnx_data": "a585477de21c0a89e021dd64f4d3be34483eb4aaed7ae93fc047e3edf74545da",
    "tokenizer.json": "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a",
    "config.json": "726b4b1650dff2607b9e19bf28cc7d1cecb7a90e4a88c348741fcffc6cf58a29",
    "tokenizer_config.json": "b2a0d89cf5c89d5d844c56d82e9c3731f862d720a6ce221c31955b628960cde7",
}


def download(url: str, destination: Path, expected: str) -> None:
    temporary = destination.with_suffix(destination.suffix + ".partial")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError("SHA256 mismatch for " + destination.name)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    for name, digest in FILES.items():
        print("provisioning " + name, flush=True)
        # The loader uses the configured onnx/model.onnx relative path;
        # its external tensors must stay beside the graph.
        relative = Path("onnx") / name if name.startswith("model.onnx") else Path(name)
        destination = args.destination / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        download(BASE + "/" + name, destination, digest)
        print("verified " + name + " SHA256=" + digest, flush=True)


if __name__ == "__main__":
    main()
