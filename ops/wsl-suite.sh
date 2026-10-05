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
#   PSEUDOLIFE_SUITE_ENV_FILE    an ops/.env to copy into the test copy; the
#                                wrapper sends one only without a test login
#                                (PSEUDOLIFE_TEST_PG_LOGIN_FILE, from
#                                `pseudolife-mcp test-login create`), which
#                                the run reads in place
#   PSEUDOLIFE_SUITE_NAME        the checkout's name (keys the copy and venv)
#   PSEUDOLIFE_SUITE_VENV        the environment to use (default: one per
#                                checkout under ~/.venvs/pseudolife)
#   PSEUDOLIFE_SUITE_PYTHON      the interpreter version (default 3.11, as CI)
#   PSEUDOLIFE_SUITE_PRUNE_DAYS  remove other checkouts' test copies and
#                                environments unused this many days (default 3)
#   PSEUDOLIFE_SUITE_KEEP        and keep at most this many of each (default 8)
#   PSEUDOLIFE_SUITE_PRUNE       off: remove nothing
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
    # Opening it truncates the file, which stamps its mtime: the pruner below
    # reads that as the copy's last use. The pruner deletes a lock file only
    # while holding it, so one deleted while this run waited is opened again.
    while :; do
        exec 9>"$root.lock"
        if ! flock -n 9; then
            echo "wsl-suite: another run is using the test copy of $name; waiting" >&2
            flock 9
        fi
        [[ "$root.lock" -ef /dev/fd/9 ]] && break
    done
fi

key="$name-$(printf '%s' "$root" | sha256sum | cut -c1-8)"
venv_root="$HOME/.venvs/pseudolife"
venv="${PSEUDOLIFE_SUITE_VENV:-$venv_root/$key}"
if [[ -z "${PSEUDOLIFE_SUITE_VENV:-}" ]] && command -v flock >/dev/null 2>&1; then
    # Held shared for the whole run (two runs of one worktree may share an
    # environment, as before); the pruner deletes an environment only while
    # holding this lock exclusively. Its mtime is the environment's last use.
    mkdir -p "$venv_root"
    while :; do
        exec 7>"$venv.lock"
        flock -s 7
        [[ "$venv.lock" -ef /dev/fd/7 ]] && break
    done
fi

# Removes the test copies and environments earlier runs left behind: those
# unused for PSEUDOLIFE_SUITE_PRUNE_DAYS days (default 3), then the least
# recently used past PSEUDOLIFE_SUITE_KEEP of each (default 8), and lock
# files whose directory is gone. PSEUDOLIFE_SUITE_PRUNE=off turns it off.
# Measured 2026-10-05: nothing removed them, and at ~600 MB a copy and
# ~220 MB an environment the second machine's disk went from 62% to 88% in
# one day (13 copies, 7.8 GB, plus 2.9 GB of environments; it had filled
# once on 2026-10-04), while WSL here held 18 copies (11 GB) and 3.3 GB.
#
# It never fails the run, and it removes only a direct, non-symlink child of
# the two roots this script owns, checked after resolving it. It removes a
# copy or an environment only while holding its lock (taken without
# waiting: a held lock means a run is using it) and never this run's own.
# An environment also stays while any process runs from it, or while the
# lock of the copy it belongs to is held: a commit dispatched from before
# this pruner takes no environment lock, but it does hold its copy's.
# Mirrors are shared by every copy of a repository and are left alone.
prune_suite_cache() {
    set +eu
    [[ "${PSEUDOLIFE_SUITE_PRUNE:-}" == off ]] && return 0
    local days="${PSEUDOLIFE_SUITE_PRUNE_DAYS:-3}" keep="${PSEUDOLIFE_SUITE_KEEP:-8}"
    if [[ ! "$days" =~ ^[0-9]+$ || ! "$keep" =~ ^[0-9]+$ ]]; then
        echo "wsl-suite: PSEUDOLIFE_SUITE_PRUNE_DAYS and PSEUDOLIFE_SUITE_KEEP take whole numbers; not pruning" >&2
        return 0
    fi
    [[ "$HOME" == /?* ]] || return 0
    local work_dir="$HOME/.cache/pseudolife-suite/work"
    local work_root="" env_root="" now max_age
    [[ -d "$work_dir" ]] && work_root="$(realpath -e -- "$work_dir")"
    [[ -d "$venv_root" ]] && env_root="$(realpath -e -- "$venv_root")"
    [[ -n "$work_root$env_root" ]] || return 0
    now="$(date +%s)"
    max_age=$(( 10#$days * 86400 ))
    keep=$(( 10#$keep ))
    local own_copy own_env
    own_copy="$(realpath -m -- "$root")"
    own_env="$(realpath -m -- "$venv")"

    # Newest mtime among the paths given that exist, else 0.
    last_use() {
        local t
        t="$(stat -c %Y -- "$@" 2>/dev/null | sort -n | tail -n 1)"
        printf '%s' "${t:-0}"
    }
    # Whether $1 is a directory directly inside root $2 (both resolved),
    # and not reached through a symlink.
    owned_child() {
        [[ -n "$2" && -d "$1" && ! -L "$1" ]] || return 1
        local real
        real="$(realpath -e -- "$1")" || return 1
        [[ "$real" == "$1" && "$(dirname -- "$real")" == "$2" ]]
    }
    # Removes owned directory $1 of root $2; adds its size to `freed`.
    remove_owned() {
        owned_child "$1" "$2" || return 1
        local kib
        kib="$(du -sk -- "$1" 2>/dev/null | cut -f1)"
        rm -rf --one-file-system -- "$1" || return 1
        freed=$(( freed + ${kib:-0} ))
    }
    # Opens lock file $1 on a new descriptor, stored in `fd`, and takes it
    # exclusively without waiting. Appending keeps its mtime; one created
    # here is dated 1970, so it never reads as a recent use.
    try_lock() {
        local made=0
        [[ -e "$1" ]] || made=1
        exec {fd}>>"$1" || return 1
        (( made )) && touch -m -d @0 -- "$1" 2>/dev/null
        flock -n "$fd" && return 0
        exec {fd}>&-
        return 1
    }
    # Whether any process runs from directory $1 (resolved) or $2 (as a run
    # names it): its argv[0] is inside either. Read without forking; nothing
    # to read where there is no /proc.
    runs_from() {
        local dir arg0
        for dir in /proc/[0-9]*; do
            arg0=""
            IFS= read -r -d '' arg0 2>/dev/null < "$dir/cmdline"
            [[ "$arg0" == "$1"/* || "$arg0" == "$2"/* ]] && return 0
        done
        return 1
    }
    # The environment a copy's run uses: keyed, as above, by the copy's
    # unresolved path and the checkout name the copy is named after.
    env_of() {
        local entry="${1##*/}"
        local copy_name="$entry"
        [[ "$entry" =~ ^[0-9a-f]{8}-(.+)$ ]] && copy_name="${BASH_REMATCH[1]}"
        printf '%s/%s-%s' "$env_root" "$copy_name" \
            "$(printf '%s' "$work_dir/$entry" | sha256sum | cut -c1-8)"
    }

    local -i copies=0 envs=0 locks=0 freed=0 fd=0
    local path t line
    local -i count=0
    local -a order=()
    local -A kept_env=()

    # Test copies, oldest first. A copy holds a .git; anything else here is
    # not one this script made and stays.
    if [[ -n "$work_root" ]]; then
        order=()
        for path in "$work_root"/*; do
            [[ -d "$path/.git" ]] || continue
            [[ "$path" == "$own_copy" ]] && continue
            order+=("$(last_use "$path.lock" "$path" "$path/.git/HEAD" "$path/.git/index") $path")
        done
        count=${#order[@]}
        [[ "$own_copy" == "$work_root"/* ]] && count+=1  # kept, and counted
        while IFS= read -r line; do
            [[ -n "$line" ]] || continue
            path="${line#* }"
            if (( count <= keep )) && (( now - ${line%% *} <= max_age )); then
                kept_env["$(env_of "$path")"]=1
                continue
            fi
            if ! try_lock "$path.lock"; then
                kept_env["$(env_of "$path")"]=1
                continue
            fi
            # A run may have finished with it since it was read.
            t="$(last_use "$path.lock" "$path" "$path/.git/HEAD" "$path/.git/index")"
            if (( count <= keep )) && (( now - t <= max_age )); then
                exec {fd}>&-
                kept_env["$(env_of "$path")"]=1
                continue
            fi
            if remove_owned "$path" "$work_root"; then
                rm -f -- "$path.lock"
                copies+=1
                count=$(( count - 1 ))
            else
                kept_env["$(env_of "$path")"]=1
            fi
            exec {fd}>&-
        done < <(printf '%s\n' "${order[@]}" | sort -n)
        [[ "$own_copy" == "$work_root"/* ]] && kept_env["$(env_of "$own_copy")"]=1
        # A held lock with no copy yet is a run about to clone one: its
        # environment stays too. A free one is removed below.
        for path in "$work_root"/*.lock; do
            [[ -f "$path" && ! -e "${path%.lock}" ]] || continue
            if try_lock "$path"; then exec {fd}>&-; else kept_env["$(env_of "${path%.lock}")"]=1; fi
        done
    fi

    # Environments, oldest first; those of kept copies stay with them.
    if [[ -n "$env_root" ]]; then
        order=()
        for path in "$env_root"/*; do
            [[ -f "$path/pyvenv.cfg" ]] || continue
            [[ "$path" == "$own_env" ]] && continue
            order+=("$(last_use "$path.lock" "$path" "$path/pyvenv.cfg" "$path"/lib/python*/site-packages) $path")
        done
        count=${#order[@]}
        [[ "$own_env" == "$env_root"/* ]] && count+=1
        while IFS= read -r line; do
            [[ -n "$line" ]] || continue
            path="${line#* }"
            [[ -n "${kept_env[$path]:-}" ]] && continue
            (( count <= keep )) && (( now - ${line%% *} <= max_age )) && continue
            try_lock "$path.lock" || continue
            t="$(last_use "$path.lock" "$path" "$path/pyvenv.cfg" "$path"/lib/python*/site-packages)"
            if { (( count <= keep )) && (( now - t <= max_age )); } \
                    || runs_from "$path" "$venv_root/${path##*/}"; then
                exec {fd}>&-
                continue
            fi
            if remove_owned "$path" "$env_root"; then
                rm -f -- "$path.lock"
                envs+=1
                count=$(( count - 1 ))
            fi
            exec {fd}>&-
        done < <(printf '%s\n' "${order[@]}" | sort -n)
    fi

    # Lock files whose copy or environment is gone (removed by hand, or by
    # a run that stopped between the two removals).
    local lock_root
    for lock_root in "$work_root" "$env_root"; do
        [[ -n "$lock_root" ]] || continue
        for path in "$lock_root"/*.lock; do
            [[ -f "$path" && ! -L "$path" && ! -e "${path%.lock}" ]] || continue
            [[ "${path%.lock}" == "$own_copy" || "${path%.lock}" == "$own_env" ]] && continue
            try_lock "$path" || continue
            [[ -e "${path%.lock}" ]] || { rm -f -- "$path" && locks+=1; }
            exec {fd}>&-
        done
    done

    (( copies + envs + locks )) || return 0
    printf 'wsl-suite: pruned %d test cop%s and %d environment%s (unused %d+ days, or past the %d kept), freeing %s; removed %d orphaned lock file%s\n' \
        "$copies" "$( (( copies == 1 )) && echo y || echo ies)" \
        "$envs" "$( (( envs == 1 )) || echo s)" "$((10#$days))" "$keep" \
        "$(awk -v k="$freed" 'BEGIN { if (k >= 1048576) printf "%.1f GB", k / 1048576; else printf "%.1f MB", k / 1024 }')" \
        "$locks" "$( (( locks == 1 )) || echo s)" >&2
}
( prune_suite_cache ) || true

if [[ -n "${PSEUDOLIFE_SUITE_COMMIT:-}" ]]; then
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
# or a future test, leaves behind. A child started with a scrubbed
# environment drops the marker and escapes the sweep: the Codex doorbell's
# CLI and the tunnel's runtime do, on purpose (bearer hygiene). Linux only:
# the sweep reads /proc.
PSEUDOLIFE_SUITE_RUN_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null \
    || printf '%s-%s-%s' "$$" "$RANDOM" "$(date +%s%N)")"
export PSEUDOLIFE_SUITE_RUN_ID

# Whether process $1 carries this run's marker. Reads /proc without
# forking, so no helper of the sweep's own carries the marker while it scans.
carries_marker() {
    local entry marker="PSEUDOLIFE_SUITE_RUN_ID=$PSEUDOLIFE_SUITE_RUN_ID"
    while IFS= read -r -d '' entry; do
        [[ "$entry" == "$marker" ]] && return 0
    done 2>/dev/null < "/proc/$1/environ"
    return 1
}

# Stops every other process carrying this run's marker; does nothing where
# there is no /proc (macOS). It runs as the EXIT trap, so neither a failed
# command nor a closed stderr (the run's reader gone) nor a second signal
# may cut it short: the leftovers are signalled before anything is printed.
# A pid is checked for the marker again before SIGKILL, in case it exited
# and the pid was reused (zombies, whose environment reads empty, drop out
# the same way).
stop_leftovers() {
    set +e
    trap '' HUP INT TERM PIPE
    [[ -r /proc/self/environ ]] || return 0
    local dir pid
    local -a left=() alive=()
    for dir in /proc/[0-9]*; do
        pid="${dir#/proc/}"
        [[ "$pid" != "$$" && -r "$dir/environ" ]] || continue
        carries_marker "$pid" && left+=("$pid")
    done
    (( ${#left[@]} )) || return 0
    kill -TERM "${left[@]}" 2>/dev/null
    echo "wsl-suite: the run left ${#left[@]} process(es) behind; stopping them:" >&2
    for pid in "${left[@]}"; do
        echo "  $pid $(tr '\0' ' ' 2>/dev/null < "/proc/$pid/cmdline" | cut -c1-200)" >&2
    done
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        alive=()
        for pid in "${left[@]}"; do
            carries_marker "$pid" && alive+=("$pid")
        done
        (( ${#alive[@]} )) || return 0
        sleep 1
    done
    kill -KILL "${alive[@]}" 2>/dev/null
}

# pytest runs as a background child, not through exec, so the sweep runs
# after it exits, and so a signal to this shell acts at once (bash runs a
# trap only between commands, and `wait` returns when one arrives) and is
# passed on: an interrupt, a hangup or TERM exits 130, 129 or 143 once
# pytest has stopped; otherwise the exit code is pytest's. pytest gets a
# session of its own, so the terminal's Ctrl+C (sent to its whole
# foreground group) reaches it only through this shell, once: a second
# interrupt aborted its session-finish cleanup (private banks undropped,
# the suite lease unreleased; review of #541, 2026-10-03). The subshell is
# not a group leader, so setsid execs pytest in place and $! stays its pid.
# A background job starts with SIGINT ignored and Python keeps an ignored
# SIGINT, so the subshell resets it: the interrupt still reaches pytest as
# KeyboardInterrupt and its teardown runs. The test copy's lock
# (descriptor 9) is held until the sweep is done.
session=()
command -v setsid >/dev/null 2>&1 && session=(setsid)
trap stop_leftovers EXIT
( trap - INT; exec ${session[@]+"${session[@]}"} "$venv/bin/python" -m pytest "$@" ) &
pytest_pid=$!
pass_on() {
    kill "-$1" "$pytest_pid" 2>/dev/null
    wait "$pytest_pid" || true
    exit "$2"
}
trap 'pass_on HUP 129' HUP
trap 'pass_on INT 130' INT
trap 'pass_on TERM 143' TERM
code=0
wait "$pytest_pid" || code=$?
exit "$code"
