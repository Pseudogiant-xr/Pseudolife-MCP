#!/usr/bin/env bash
# Run this checkout's tests on Linux from a per-checkout uv environment.
#
# ops/wsl-suite.ps1 calls this inside WSL, so a Windows host runs its full
# suite where process creation never touches the Windows console subsystem
# (tests/suite_lock.py carries the 2026-10-02 measurement). It works the same
# from any Linux shell. With no arguments it runs the full suite; any
# arguments replace that and go to pytest unchanged.
#
# Environment:
#   PSEUDOLIFE_SUITE_VENV    the environment to use (default: one per checkout
#                            under ~/.venvs/pseudolife, kept on the Linux
#                            filesystem so uv can hard-link from its cache)
#   PSEUDOLIFE_SUITE_PYTHON  the interpreter version (default 3.11, as CI)
#
# The run is offline (HF_HUB_OFFLINE=1) and WSL has its own Hugging Face
# cache, so a fresh distribution needs the embedders CI warms
# (Qwen/Qwen3-Embedding-0.6B, sentence-transformers/all-MiniLM-L6-v2) and the
# cross-encoders the config names, once: copy them from the Windows cache
# into ~/.cache/huggingface/hub, or run once with HF_HUB_OFFLINE=0.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# wsl.exe -e starts no login shell, so uv's installer's PATH line never ran.
export PATH="$PATH:$HOME/.local/bin:$HOME/.cargo/bin"
if ! command -v uv >/dev/null 2>&1; then
    echo "wsl-suite: uv is required: https://docs.astral.sh/uv/" >&2
    exit 2
fi
key="$(basename "$root")-$(printf '%s' "$root" | sha256sum | cut -c1-8)"
venv="${PSEUDOLIFE_SUITE_VENV:-$HOME/.venvs/pseudolife/$key}"
if [[ ! -x "$venv/bin/python" ]]; then
    uv venv --quiet --python "${PSEUDOLIFE_SUITE_PYTHON:-3.11}" "$venv"
fi
# The CPU torch wheels, as CI and the daemon image install them; re-run each
# time so the environment follows this checkout's pyproject.toml.
uv pip install --quiet --python "$venv/bin/python" --torch-backend cpu -e "$root[dev]"

cd "$root"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
if [[ $# -eq 0 ]]; then
    set -- tests/ -q -rs
fi
exec "$venv/bin/python" -m pytest "$@"
