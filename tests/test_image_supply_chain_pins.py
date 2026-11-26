"""Every network input of the extractor and Rust daemon images is pinned and checked.

A floating base tag or an unverified download lets the bytes a user builds
change under the same Dockerfile. Each guard has a near-miss test that feeds
it the unpinned form and expects a refusal.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = ROOT / "ops" / "Dockerfile.extractor"
DAEMON_RUST = ROOT / "ops" / "Dockerfile.daemon-rust"

DIGEST_FROM = re.compile(r"^FROM [^\s@:]+(?:/[^\s@:]+)*:[^\s@]+@sha256:[0-9a-f]{64}(?: AS \S+)?$")
HF_REVISION_URL = re.compile(r"^https://huggingface\.co/[^/]+/[^/]+/resolve/[0-9a-f]{40}/[^/]+\.gguf$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _logical_lines(text):
    """Dockerfile instructions with line continuations joined."""
    return [" ".join(line.split()) for line in re.sub(r"\\\n", " ", text).splitlines()]


def _arg(text, name):
    match = re.search(rf"^ARG {name}=(\S+)$", text, re.MULTILINE)
    assert match, f"ARG {name} has no default"
    return match.group(1)


def assert_extractor_pinned(text):
    lines = _logical_lines(text)
    froms = [line for line in lines if line.startswith("FROM ")]
    assert froms, "no FROM"
    for line in froms:
        assert DIGEST_FROM.match(line), f"base image not pinned by digest: {line}"
    assert HF_REVISION_URL.match(_arg(text, "MODEL_URL")), "MODEL_URL must name a revision, not a branch"
    assert SHA256.match(_arg(text, "MODEL_SHA256")), "MODEL_SHA256"
    fetch = next(line for line in lines if line.startswith("RUN ") and "$MODEL_URL" in line)
    download, _, check = fetch.partition(" && ")
    assert "-o /tmp/extractor.gguf" in download, "download target"
    assert check.startswith('{ echo "${MODEL_SHA256} /tmp/extractor.gguf" | sha256sum -c - || {'), "sha256sum check"
    # A MODEL_URL override without its hash is refused with the fix named.
    assert "MODEL_SHA256=<its sha256>" in check and check.endswith("exit 1; }; }"), "mismatch message"
    # The documented smaller model is pinned the same way, with its own hash.
    alt_url = re.search(r"--build-arg MODEL_URL=(\S+)", text)
    alt_sha = re.search(r"--build-arg MODEL_SHA256=(\S+)", text)
    assert alt_url and HF_REVISION_URL.match(alt_url.group(1)), "alternative MODEL_URL"
    assert alt_sha and SHA256.match(alt_sha.group(1)), "alternative MODEL_SHA256"


def assert_daemon_rust_ort_pinned(text):
    lines = _logical_lines(text)
    assert not any(line.startswith("FROM python") for line in lines), "Python build stage"
    assert "pip install" not in text, "pip install"
    versions = set()
    for arch, wheel_arch in (("AMD64", "x86_64"), ("ARM64", "aarch64")):
        url = _arg(text, f"ORT_WHEEL_URL_{arch}")
        assert url.startswith("https://files.pythonhosted.org/"), f"ORT wheel source {arch}"
        assert url.endswith(f"_{wheel_arch}.whl"), f"ORT wheel platform {arch}"
        versions.add(re.search(r"/onnxruntime-(\d+\.\d+\.\d+)-", url).group(1))
        assert SHA256.match(_arg(text, f"ORT_WHEEL_SHA256_{arch}")), f"ORT_WHEEL_SHA256_{arch}"
    assert len(versions) == 1, "both wheels name one ONNX Runtime version"
    run = next(line for line in lines if line.startswith("RUN ") and "$ORT_WHEEL_URL_AMD64" in line)
    assert 'amd64) url="$ORT_WHEEL_URL_AMD64" sha="$ORT_WHEEL_SHA256_AMD64" ;;' in run, "amd64 selection"
    assert 'arm64) url="$ORT_WHEEL_URL_ARM64" sha="$ORT_WHEEL_SHA256_ARM64" ;;' in run, "arm64 selection"
    assert re.search(r'\*\) echo "ONNX Runtime: no pinned wheel for [^"]*" >&2; exit 1 ;;', run), "other platforms refused"
    steps = run.split(" && ")
    check = 'echo "${sha} /tmp/onnxruntime.whl" | sha256sum -c -'
    assert check in steps, "sha256sum check"
    unzip = next(i for i, step in enumerate(steps) if step.startswith("unzip "))
    assert steps.index(check) < unzip, "check before unpack"
    (version,) = versions
    assert f"ORT_DYLIB_PATH=/opt/ort/libonnxruntime.so.{version}" in text, "ORT_DYLIB_PATH version"


def test_extractor_image_inputs_are_pinned():
    assert_extractor_pinned(EXTRACTOR.read_text(encoding="utf-8"))


def test_daemon_rust_onnx_runtime_is_hash_checked_without_python():
    assert_daemon_rust_ort_pinned(DAEMON_RUST.read_text(encoding="utf-8"))


def test_daemon_rust_drops_python_era_variables():
    # Nothing in the Rust daemon or the native client reads these; the
    # offline ones misled, since the model provisioner downloads over HTTPS.
    text = DAEMON_RUST.read_text(encoding="utf-8")
    for name in ("CUDA_VISIBLE_DEVICES", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        assert name not in text, name


EXTRACTOR_MUTATIONS = {
    "floating-curl": (r"curlimages/curl:\S+", "curlimages/curl:latest AS fetch"),
    "floating-server": (r"ghcr\.io/ggml-org/llama\.cpp:\S+", "ghcr.io/ggml-org/llama.cpp:server"),
    "branch-model": (r"/resolve/[0-9a-f]{40}/pseudolife", "/resolve/main/pseudolife"),
    "no-check": (r" \\\n    && \{ echo \"\$\{MODEL_SHA256\}[^\n]*\n[^\n]*", ""),
    "silent-mismatch": (r" \\\n         \|\| \{ echo [^\n]*", "; }"),
    "short-hash": (r"ARG MODEL_SHA256=[0-9a-f]{64}", "ARG MODEL_SHA256=3d5930dd"),
    "branch-alternative": (r"/resolve/[0-9a-f]{40}/gemma", "/resolve/main/gemma"),
}


@pytest.mark.parametrize("mutation", EXTRACTOR_MUTATIONS)
def test_extractor_pin_guard_rejects_unpinned_inputs(mutation):
    pattern, replacement = EXTRACTOR_MUTATIONS[mutation]
    text = EXTRACTOR.read_text(encoding="utf-8")
    mutated, count = re.subn(pattern, replacement, text, count=1)
    assert count == 1, mutation
    with pytest.raises(AssertionError):
        assert_extractor_pinned(mutated)


DAEMON_MUTATIONS = {
    "pip-stage": (
        r"FROM debian:bookworm-slim AS runtime-inputs",
        "FROM python:3.12-slim-bookworm AS runtime-inputs\nRUN pip install onnxruntime==1.27.0",
    ),
    "no-check": (r"\n    && echo \"\$\{sha\}[^\n]*", ""),
    "short-hash": (r"ARG ORT_WHEEL_SHA256_AMD64=[0-9a-f]{64}", "ARG ORT_WHEEL_SHA256_AMD64=7c65a743"),
    "arm64-gets-x86-wheel": (r"_aarch64\.manylinux_2_28_aarch64\.whl", "_x86_64.manylinux_2_28_x86_64.whl"),
    "other-arch-accepted": (r"exit 1 ;; \\", r";; \\"),
    "version-drift": (r"ORT_DYLIB_PATH=/opt/ort/libonnxruntime\.so\.\S+", "ORT_DYLIB_PATH=/opt/ort/libonnxruntime.so.1.26.0"),
}


@pytest.mark.parametrize("mutation", DAEMON_MUTATIONS)
def test_daemon_rust_pin_guard_rejects_unverified_runtime(mutation):
    pattern, replacement = DAEMON_MUTATIONS[mutation]
    text = DAEMON_RUST.read_text(encoding="utf-8")
    mutated, count = re.subn(pattern, replacement, text, count=1)
    assert count == 1, mutation
    with pytest.raises(AssertionError):
        assert_daemon_rust_ort_pinned(mutated)
