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

# A run dispatched to a second machine (ops/remote-suite.ps1 sets
# PSEUDOLIFE_SUITE_DISPATCHED=1) starts only against a test server named
# off 5433 — on the maintainer's homelab box 5433 is the live bank's
# server, and a fixed PSEUDOLIFE_TEST_DATABASE_URL leaves default paths
# there (review of #528, 2026-10-03) — and only under that machine's own
# suite lease, so the first machine's gates do not hold off for it.
if [[ -n "${PSEUDOLIFE_SUITE_DISPATCHED:-}" ]]; then
    # Stripped and compared as a number, as pg_defaults reads it: '05433'
    # and '5433 ' both reach 5433 (re-review of #528, 2026-10-03).
    server="$(printf '%s' "${PSEUDOLIFE_TEST_PG_HOST_PORT:-}" | tr -d '[:space:]')"
    port="${server##*:}"
    if [[ "$server" != *:* || ! "$port" =~ ^[0-9]+$ ]] || (( 10#$port == 5433 )); then
        echo "wsl-suite: a dispatched run needs PSEUDOLIFE_TEST_PG_HOST_PORT set to a test server other than port 5433 (got '${server}'); refusing" >&2
        exit 2
    fi
    lock_dir="${PSEUDOLIFE_SUITE_LOCK_DIR:-$HOME/.pseudolife-mcp/locks}"
    lease="${PSEUDOLIFE_SUITE_LEASE:-$(cat "$lock_dir/full-suite.lease" 2>/dev/null || true)}"
    lease="$(printf '%s' "$lease" | tr -d '[:space:]')"
    if [[ "$lease" != full-suite@?* ]]; then
        echo "wsl-suite: a dispatched run needs this machine's own suite lease (full-suite@<host> in $lock_dir/full-suite.lease or PSEUDOLIFE_SUITE_LEASE; got '${lease:-full-suite}'); refusing" >&2
        exit 2
    fi
fi

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
    mirror_key="$(printf '%s' "$common" | sha256sum | cut -c1-8)"
    mirror="$base/mirror-$mirror_key.git"
    mkdir -p "$base/work"
    # The mirror is cloned and fetched under its own lock: two launches at
    # once raced here and one failed (review of #527, 2026-10-02).
    exec 8>"$mirror.lock"
    flock 8
    if [[ ! -d "$mirror" ]]; then
        git clone --quiet --bare --no-hardlinks "$common" "$mirror"
    fi
    git -C "$mirror" fetch --quiet --prune "$common" \
        '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'
    exec 8>&-
    if ! git -C "$mirror" cat-file -e "${PSEUDOLIFE_SUITE_COMMIT}^{commit}" 2>/dev/null; then
        echo "wsl-suite: $PSEUDOLIFE_SUITE_COMMIT is on no branch or tag; commit it to a branch" >&2
        exit 2
    fi
    # Keyed by mirror as well as name: checkouts of different repositories
    # share a folder name (every Codex worktree is "Pseudolife-MCP"), and a
    # copy keyed by name alone stayed bound to the first one's mirror, which
    # lacked the next one's commit (exit 128 before pytest, 2026-10-02).
    root="$base/work/$mirror_key-$name"
    # One run at a time per test copy: a second run of the same checkout
    # would check out and clean under the first. This shell holds the
    # descriptor until pytest has exited and its leftovers are stopped;
    # Python's subprocesses close it, so a leaked test daemon cannot keep it.
    exec 9>"$root.lock"
    if ! flock -n 9; then
        echo "wsl-suite: another run is using the test copy of $name; waiting" >&2
        flock 9
    fi
    if [[ ! -d "$root/.git" ]]; then
        git clone --quiet --shared --no-checkout "$mirror" "$root"
    fi
    # Always this mirror, objects included (a --shared clone borrows them).
    git -C "$root" remote set-url origin "$mirror"
    printf '%s\n' "$mirror/objects" > "$root/.git/objects/info/alternates"
    git -C "$root" fetch --quiet origin
    git -C "$root" checkout --quiet --force --detach "$PSEUDOLIFE_SUITE_COMMIT"
    # Untracked files from the last run (caches, build output) go; the copy
    # holds nothing else.
    git -C "$root" clean --quiet -fdx
    if [[ -n "${PSEUDOLIFE_SUITE_ENV_FILE:-}" && -f "$PSEUDOLIFE_SUITE_ENV_FILE" ]]; then
        install -m 600 "$PSEUDOLIFE_SUITE_ENV_FILE" "$root/ops/.env"  # it holds secrets
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

# Every process the run starts inherits this marker, so whatever outlives
# pytest can be found by it and stopped, and nothing without it is touched
# (the live daemon, another run). A detached process escapes the run's
# process group: the shim's autostarted daemon starts its own session, and
# one outlived every Linux run until 2026-10-03, when 14 had piled up in
# WSL (28 of its 39 GB) and 5 on the second machine had exhausted its
# memory and swap. On that machine each also held its run's systemd scope
# open. The test reaps its daemon itself; this catches what a killed run,
# or a future test, leaves behind.
PSEUDOLIFE_SUITE_RUN_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null \
    || printf '%s-%s-%s' "$$" "$RANDOM" "$(date +%s%N)")"
export PSEUDOLIFE_SUITE_RUN_ID

# Stops every other process carrying this run's marker. Reads /proc without
# forking, so no helper of its own carries the marker while it scans; does
# nothing where there is no /proc (macOS).
stop_leftovers() {
    [[ -r /proc/self/environ ]] || return 0
    local marker="PSEUDOLIFE_SUITE_RUN_ID=$PSEUDOLIFE_SUITE_RUN_ID"
    local dir pid entry
    local -a left=() alive=()
    for dir in /proc/[0-9]*; do
        pid="${dir#/proc/}"
        [[ "$pid" != "$$" && -r "$dir/environ" ]] || continue
        while IFS= read -r -d '' entry; do
            if [[ "$entry" == "$marker" ]]; then left+=("$pid"); break; fi
        done 2>/dev/null < "$dir/environ" || true
    done
    (( ${#left[@]} )) || return 0
    echo "wsl-suite: the run left ${#left[@]} process(es) behind; stopping them:" >&2
    for pid in "${left[@]}"; do
        echo "  $pid $(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-200)" >&2
    done
    kill -TERM "${left[@]}" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        alive=()
        for pid in "${left[@]}"; do
            [[ -d "/proc/$pid" ]] && alive+=("$pid")
        done
        (( ${#alive[@]} )) || return 0
        sleep 1
    done
    kill -KILL "${alive[@]}" 2>/dev/null || true
}

# pytest runs as a child, not through exec, so the sweep runs after it
# exits, also after Ctrl+C or a hangup. The test copy's lock (descriptor 9)
# is held until then.
trap stop_leftovers EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
code=0
"$venv/bin/python" -m pytest "$@" || code=$?
exit "$code"
