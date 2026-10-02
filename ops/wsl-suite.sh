#!/usr/bin/env bash
# Run a checkout's tests on Linux from a per-checkout uv environment.
#
# ops/wsl-suite.ps1 calls this inside WSL, so a Windows host runs its full
# suite where process creation never touches the Windows console subsystem
# (tests/suite_lock.py carries the 2026-10-02 measurement). It works the same
# from any Linux shell. With no arguments it runs the full suite; any
# arguments replace that and go to pytest unchanged.
#
# With PSEUDOLIFE_SUITE_COMMIT set (the wrapper sets it), the run does not
# read the checkout over /mnt/c: it fetches that commit from the repository
# at PSEUDOLIFE_SUITE_GIT_COMMON into a mirror on the Linux filesystem and
# tests a copy checked out there. Two reasons, both seen 2026-10-02:
# Linux git cannot read a Windows worktree (its .git file names a C:/ path),
# so git-dependent tests failed or skipped; and a run over /mnt/c roughly
# doubled DPC work on the host's CPU 0 (11.2% vs 6.3%) through file traffic.
# Only new objects cross /mnt/c after the first fetch.
#
# Environment:
#   PSEUDOLIFE_SUITE_COMMIT      the commit to test (a full SHA)
#   PSEUDOLIFE_SUITE_GIT_COMMON  its repository's common git directory
#   PSEUDOLIFE_SUITE_ENV_FILE    an ops/.env to copy into the test copy
#   PSEUDOLIFE_SUITE_NAME        the checkout's name (keys the copy and venv)
#   PSEUDOLIFE_SUITE_VENV        the environment to use (default: one per
#                                checkout under ~/.venvs/pseudolife)
#   PSEUDOLIFE_SUITE_PYTHON      the interpreter version (default 3.11, as CI)
#
# The run is offline (HF_HUB_OFFLINE=1) and WSL has its own Hugging Face
# cache, so a fresh distribution needs the embedders CI warms
# (Qwen/Qwen3-Embedding-0.6B, sentence-transformers/all-MiniLM-L6-v2) and the
# cross-encoders the config names, once: copy them from the Windows cache
# into ~/.cache/huggingface/hub, or run once with HF_HUB_OFFLINE=0.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name="${PSEUDOLIFE_SUITE_NAME:-$(basename "$root")}"
# wsl.exe -e starts no login shell, so uv's installer's PATH line never ran.
export PATH="$PATH:$HOME/.local/bin:$HOME/.cargo/bin"
if ! command -v uv >/dev/null 2>&1; then
    echo "wsl-suite: uv is required: https://docs.astral.sh/uv/" >&2
    exit 2
fi

if [[ -n "${PSEUDOLIFE_SUITE_COMMIT:-}" ]]; then
    common="${PSEUDOLIFE_SUITE_GIT_COMMON:?PSEUDOLIFE_SUITE_GIT_COMMON is required with PSEUDOLIFE_SUITE_COMMIT}"
    base="$HOME/.cache/pseudolife-suite"
    mirror="$base/mirror-$(printf '%s' "$common" | sha256sum | cut -c1-8).git"
    if [[ ! -d "$mirror" ]]; then
        git clone --quiet --bare --no-hardlinks "$common" "$mirror"
    fi
    git -C "$mirror" fetch --quiet --prune "$common" \
        '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'
    if ! git -C "$mirror" cat-file -e "${PSEUDOLIFE_SUITE_COMMIT}^{commit}" 2>/dev/null; then
        echo "wsl-suite: $PSEUDOLIFE_SUITE_COMMIT is on no branch or tag; commit it to a branch" >&2
        exit 2
    fi
    root="$base/work/$name"
    if [[ ! -d "$root/.git" ]]; then
        mkdir -p "$base/work"
        git clone --quiet --shared --no-checkout "$mirror" "$root"
    fi
    git -C "$root" fetch --quiet origin
    git -C "$root" checkout --quiet --force --detach "$PSEUDOLIFE_SUITE_COMMIT"
    # Untracked files from the last run (caches, build output) go; the copy
    # holds nothing else.
    git -C "$root" clean --quiet -fdx
    if [[ -n "${PSEUDOLIFE_SUITE_ENV_FILE:-}" && -f "$PSEUDOLIFE_SUITE_ENV_FILE" ]]; then
        cp "$PSEUDOLIFE_SUITE_ENV_FILE" "$root/ops/.env"
    fi
    echo "wsl-suite: testing $(git -C "$root" rev-parse --short HEAD) in $root"
fi

key="$name-$(printf '%s' "$root" | sha256sum | cut -c1-8)"
venv="${PSEUDOLIFE_SUITE_VENV:-$HOME/.venvs/pseudolife/$key}"
if [[ ! -x "$venv/bin/python" ]]; then
    # --seed: the installer tests ask the run's Python for pip, which a bare
    # uv environment lacks.
    uv venv --quiet --seed --python "${PSEUDOLIFE_SUITE_PYTHON:-3.11}" "$venv"
fi
# The CPU torch wheels, as CI and the daemon image install them; re-run each
# time so the environment follows the checkout's pyproject.toml.
uv pip install --quiet --python "$venv/bin/python" --torch-backend cpu -e "$root[dev]"

cd "$root"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
# uv marks its managed Pythons externally managed (PEP 668); CI's setup-python
# does not, and a test installs into a disposable user base through the base
# interpreter. Scoped to this run.
export PIP_BREAK_SYSTEM_PACKAGES=1
if [[ $# -eq 0 ]]; then
    set -- tests/ -q -rs
fi
exec "$venv/bin/python" -m pytest "$@"
