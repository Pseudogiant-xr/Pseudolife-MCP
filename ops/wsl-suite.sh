#!/usr/bin/env bash
# Run a checkout's tests on Linux from a per-checkout uv environment.
#
# ops/wsl-suite.ps1 calls this inside WSL, so a Windows host runs its full
# suite where process creation never touches the Windows console subsystem
# (tests/suite_lock.py carries the 2026-10-02 measurement). It works the same
# from any Linux shell. With no arguments, or only display and worker
# options, it runs the full suite, under pytest-xdist when the machine has
# room (see plan_run below); any other arguments go to pytest unchanged.
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
#   PSEUDOLIFE_SUITE_PRUNE       off (or 0, false, no): remove nothing
#   PSEUDOLIFE_SUITE_WORKERS     xdist workers for the default full run
#                                (default: chosen from CPUs and memory;
#                                0 or 1 runs it in one process)
#   PSEUDOLIFE_SUITE_PROFILE     1: load tests/suite_profile_plugin.py (worker
#                                timeline, resource and test-server samples,
#                                run manifest) and fold its summary afterwards;
#                                the arguments and exit code are unchanged
#   PSEUDOLIFE_SUITE_PROFILE_DIR where the profile goes (default: a new
#                                directory under ~/.pseudolife-mcp/suite-profiles)
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

# Workers for a full run: up to 4, one per two CPUs, one per 6 GB of
# memory, the memory being the smaller of RAM and this run's cgroup limit
# (a dispatched run's systemd scope sets one, 16 GB by default). Measured
# 2026-10-19 in WSL (8 CPUs, 40 GB) at -n 4 --dist loadfile: 10:59 against
# 28-31 min single-process, peak 20.2 GB used, 1-minute load under 6.
# --dist loadfile keeps each file on one worker, as CI's -n 2 does, so
# module-scoped fixtures keep their meaning.
suite_workers() {
    if [[ -n "${PSEUDOLIFE_SUITE_WORKERS:-}" ]]; then
        echo "$PSEUDOLIFE_SUITE_WORKERS"
        return
    fi
    local cpus mem_kb limit n
    # GNU nproc answers OMP_NUM_THREADS / OMP_THREAD_LIMIT when set (CI's
    # Linux lanes export OMP_NUM_THREADS=2), which is a thread budget, not
    # the CPUs here (Codex review, 2026-10-19).
    cpus="$(env -u OMP_NUM_THREADS -u OMP_THREAD_LIMIT nproc 2>/dev/null || echo 1)"
    mem_kb="$(awk '/^MemTotal:/ { print $2 }' /proc/meminfo 2>/dev/null || true)"
    [[ "$mem_kb" =~ ^[0-9]+$ ]] || mem_kb=0
    # cgroup v2's memory.max, else v1's memory controller (a hybrid host
    # mounts v2 at /sys/fs/cgroup/unified and keeps memory on v1).
    for limit in \
        "/sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup 2>/dev/null || true)/memory.max" \
        "/sys/fs/cgroup/memory$(sed -n 's/^[0-9]*:\([^:]*,\)\{0,1\}memory\(,[^:]*\)\{0,1\}://p' /proc/self/cgroup 2>/dev/null || true)/memory.limit_in_bytes"; do
        [[ -r "$limit" ]] || continue
        limit="$(cat "$limit")"
        if [[ "$limit" =~ ^[0-9]+$ ]] && (( limit / 1024 < mem_kb )); then
            mem_kb=$(( limit / 1024 ))
        fi
        break
    done
    n=$(( cpus / 2 ))
    (( mem_kb / (6 * 1024 * 1024) < n )) && n=$(( mem_kb / (6 * 1024 * 1024) ))
    (( n > 4 )) && n=4
    echo "$n"
}

# What pytest runs, decided before anything is fetched or installed, so a
# malformed request is refused at once:
#   - no arguments: the full suite, `tests/ -q -rs`, on automatic workers;
#   - only display options (-q..., -v..., --quiet, --verbose, -r<chars>,
#     --durations[=]N, --durations-min[=]S, --tb[=]STYLE), worker choices
#     (-n N, -nN, --numprocesses[=]N; N a whole number, auto or logical) and
#     a distribution (--dist[=]MODE): still the full suite, the display
#     options in place of -q -rs. A worker choice wins over the automatic
#     count and is never doubled, and a chosen distribution over loadfile;
#     --dist no alone runs it in one process (xdist would turn it into load
#     once it had workers);
#   - anything else (paths, node ids, -k, -m, --collect-only, --help, an
#     unknown option), or a `--` anywhere: every argument goes to pytest
#     unchanged and nothing is added; pytest then judges them all. An
#     unknown option is never parsed, since the launcher cannot know
#     whether it takes the next word.
# In a full-suite run, a worker or display option missing its value, a
# worker choice given twice, or an invalid count, mode or number exits 2.
run_args=()
run_note=""
plan_run() {
    local -a display=() chosen=() dist=()
    local arg name value problem="" durations=0 i=1 n=$#
    local -a argv=("$@")
    note_problem() { [[ -n "$problem" ]] || problem="$1"; }
    # The value of option $1: the attached one when $2 is 1 (`--x=v`; empty
    # is missing), else the next word. Sets `value`; returns 1 when missing.
    take_value() {
        if (( $2 )); then
            value="$3"
        elif (( i <= n )); then
            # i is the 1-based position of the next word, argv 0-based.
            value="${argv[i-1]}"
            i=$(( i + 1 ))
        else
            value=""
        fi
        [[ -n "$value" ]] && return 0
        note_problem "$1 needs a value"
        return 1
    }
    for arg in "$@"; do
        [[ "$arg" == "--" ]] && { run_args=("$@"); run_note="pass"; return; }
    done
    while (( i <= n )); do
        arg="${argv[i-1]}"
        i=$(( i + 1 ))
        case "$arg" in
            -q|-qq|-qqq|-qqqq|-v|-vv|-vvv|-vvvv|--quiet|--verbose) display+=("$arg") ;;
            -r?*)
                [[ "$arg" =~ ^-r[fEsxXpPaAwN]+$ ]] || { run_args=("$@"); run_note="pass"; return; }
                display+=("$arg") ;;
            --durations|--durations=*|--durations-min|--durations-min=*)
                name="${arg%%=*}"
                take_value "$name" "$( [[ "$arg" == *=* ]] && echo 1 || echo 0 )" "${arg#*=}" || continue
                if [[ "$name" == --durations ]]; then
                    durations=1
                    [[ "$value" =~ ^[0-9]+$ ]] \
                        || note_problem "--durations takes a number, not '$value'"
                else
                    # pytest reads it with float(): 1, 1., .5, 1e-3.
                    [[ "$value" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][-+]?[0-9]+)?$ ]] \
                        || note_problem "--durations-min takes a number, not '$value'"
                fi
                display+=("$name=$value") ;;
            --tb|--tb=*)
                take_value --tb "$( [[ "$arg" == *=* ]] && echo 1 || echo 0 )" "${arg#*=}" || continue
                [[ "$value" =~ ^(auto|long|short|line|native|no)$ ]] \
                    || note_problem "--tb takes auto, long, short, line, native or no, not '$value'"
                display+=("--tb=$value") ;;
            -n|-n?*|--numprocesses|--numprocesses=*)
                name="${arg%%=*}"
                if [[ "$arg" == -n?* ]]; then
                    name=-n
                    take_value -n 1 "${arg#-n}" || continue
                elif [[ "$arg" == --numprocesses=* ]]; then
                    take_value "$name" 1 "${arg#*=}" || continue
                else
                    take_value "$name" 0 "" || continue
                fi
                if (( ${#chosen[@]} )); then
                    note_problem "workers chosen twice (${chosen[0]} and $value)"
                elif [[ "$value" =~ ^([0-9]+|auto|logical)$ ]]; then
                    [[ "$value" =~ ^[0-9]+$ ]] && value=$(( 10#$value ))
                    chosen=("$value")
                else
                    note_problem "$name takes a whole number, auto or logical, not '$value'"
                fi ;;
            --dist|--dist=*)
                take_value --dist "$( [[ "$arg" == *=* ]] && echo 1 || echo 0 )" "${arg#*=}" || continue
                if (( ${#dist[@]} )); then
                    note_problem "--dist given twice"
                elif [[ "$value" =~ ^(each|load|loadscope|loadfile|loadgroup|worksteal|no)$ ]]; then
                    dist=("$value")
                else
                    note_problem "--dist takes an xdist mode, not '$value'"
                fi ;;
            *) run_args=("$@"); run_note="pass"; return ;;
        esac
    done
    if [[ -n "$problem" ]]; then
        echo "wsl-suite: $problem; refusing" >&2
        exit 2
    fi
    (( ${#display[@]} )) || display=(-q -rs)
    local workers source xdist=0
    if (( ${#chosen[@]} )); then
        workers="${chosen[0]}"
        source="-n"
        [[ "$workers" == 0 ]] || xdist=1
    elif [[ "${dist[0]:-}" == no ]]; then
        workers=0
        source="--dist no"
    else
        workers="$(suite_workers)"
        source="automatic"
        [[ -n "${PSEUDOLIFE_SUITE_WORKERS:-}" ]] && source="PSEUDOLIFE_SUITE_WORKERS"
        if ! [[ "$workers" =~ ^[0-9]+$ ]]; then
            echo "wsl-suite: PSEUDOLIFE_SUITE_WORKERS must be a whole number, not '$workers'" >&2
            exit 2
        fi
        workers=$(( 10#$workers ))  # "08" is 8, not an octal error
        (( workers >= 2 )) && xdist=1
    fi
    run_args=(tests/ "${display[@]}")
    if (( xdist )) || (( ${#chosen[@]} )); then
        run_args+=(-n "$workers")
    fi
    if (( ${#dist[@]} )); then
        run_args+=(--dist "${dist[0]}")
    elif (( xdist )); then
        run_args+=(--dist loadfile)
    fi
    (( xdist && ! durations )) && run_args+=(--durations=30)
    if (( xdist )); then
        run_note="full suite, $workers xdist worker$( [[ "$workers" == 1 ]] || echo s ) ($source)"
    else
        run_note="full suite, one process ($source)"
    fi
}
plan_run "$@"

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
    tries=0
    while :; do
        exec 9>"$root.lock"
        if ! flock -n 9; then
            echo "wsl-suite: another run is using the test copy of $name; waiting" >&2
            flock 9
        fi
        [[ "$root.lock" -ef /dev/fd/9 ]] && break
        (( ++tries < 5 )) || { echo "wsl-suite: $root.lock kept being replaced while this run waited; refusing" >&2; exit 2; }
    done
fi

key="$name-$(printf '%s' "$root" | sha256sum | cut -c1-8)"
venv_root="$HOME/.venvs/pseudolife"
venv="${PSEUDOLIFE_SUITE_VENV:-$venv_root/$key}"
if [[ "$venv" == "$venv_root"/* ]] && command -v flock >/dev/null 2>&1; then
    # Held shared for the whole run (two runs of one worktree may share an
    # environment, as before); the pruner deletes an environment only while
    # holding this lock exclusively. Opening it stamps the environment's
    # last use, as for the copy's lock.
    mkdir -p "$venv_root"
    tries=0
    while :; do
        exec 7>"$venv.lock"
        flock -s 7
        [[ "$venv.lock" -ef /dev/fd/7 ]] && break
        (( ++tries < 5 )) || { echo "wsl-suite: $venv.lock kept being replaced while this run waited; refusing" >&2; exit 2; }
    done
fi

# Removes the test copies and environments earlier runs left behind: those
# unused for PSEUDOLIFE_SUITE_PRUNE_DAYS days (default 3), then the least
# recently used past PSEUDOLIFE_SUITE_KEEP of each (default 8), and lock
# files whose directory is gone once they are as old.
# PSEUDOLIFE_SUITE_PRUNE=off turns it off. Measured 2026-10-05: nothing
# removed them, and at ~600 MB a copy and ~220 MB an environment the second
# machine's disk went from 62% to 88% in one day (13 copies, 7.8 GB, plus
# 2.9 GB of environments; it had filled once on 2026-10-04), while WSL here
# held 18 copies (11 GB) and 3.3 GB.
#
# It never fails the run, and it removes only a direct, non-symlink child of
# the two roots this script owns, checked after resolving it. It removes a
# copy or an environment only while holding its lock (taken without
# waiting: a held lock means a run is using it) and never this run's own.
# Either also stays while a process works in it or runs from it (its
# working directory or argv[0] lies inside: a shell cd'd into a copy by
# hand), and nothing is pruned while a root is itself a symlink, which
# could point it at something broad. An environment goes only
# while the lock of the copy it belongs to is held here too: a commit
# dispatched from before this pruner takes no environment lock, but it does
# take its copy's. A copy's lock file outlives the copy (a run from before
# the pruner may be waiting on it, and would not reopen a new one) and goes
# only once it is unused as long. A directory is renamed to .trash-<pid>-
# before it is deleted, so an interrupted delete never leaves a half copy a
# later run would take for whole; later runs finish such leftovers. Mirrors
# are shared by every copy of a repository and are left alone.
#
# Not covered: a run of an older commit, or a worktree-mode run, holds no
# environment lock, so while uv installs into its environment (argv[0] is
# uv, not the environment's python) only its copy's lock, if any, keeps
# that environment. Pruning it then fails that one run; nothing else.
prune_suite_cache() {
    set +eu
    case "${PSEUDOLIFE_SUITE_PRUNE,,}" in off|0|false|no) return 0 ;; esac
    local days="${PSEUDOLIFE_SUITE_PRUNE_DAYS:-3}" keep="${PSEUDOLIFE_SUITE_KEEP:-8}"
    if [[ ! "$days" =~ ^[0-9]+$ || ! "$keep" =~ ^[0-9]+$ ]]; then
        echo "wsl-suite: PSEUDOLIFE_SUITE_PRUNE_DAYS and PSEUDOLIFE_SUITE_KEEP take whole numbers; not pruning" >&2
        return 0
    fi
    [[ "$HOME" == /?* ]] || return 0
    command -v flock >/dev/null 2>&1 || return 0
    local work_dir="$HOME/.cache/pseudolife-suite/work"
    local work_root="" env_root="" now max_age
    if [[ -L "${work_dir%/work}" || -L "$work_dir" || -L "$venv_root" ]]; then
        echo "wsl-suite: not pruning: ${work_dir%/work}, $work_dir or $venv_root is a symlink; only real directories are pruned" >&2
        return 0
    fi
    [[ -d "$work_dir" ]] && work_root="$(realpath -e -- "$work_dir")"
    [[ -d "$venv_root" ]] && env_root="$(realpath -e -- "$venv_root")"
    [[ -n "$work_root$env_root" ]] || return 0
    now="$(date +%s)"
    max_age=$(( 10#$days * 86400 ))
    keep=$(( 10#$keep ))
    local own_copy own_env
    own_copy="$(realpath -m -- "$root")"
    own_env="$(realpath -m -- "$venv")"

    # Newest mtime among the paths given that exist; 0 when none could be
    # read, which may_go takes as "in use".
    last_use() {
        local t
        t="$(stat -c %Y -- "$@" 2>/dev/null | sort -n | tail -n 1)"
        printf '%s' "${t:-0}"
    }
    # Whether an entry last used at $1 may go: past the age limit, or past
    # the kept count; never when its last use is unknown.
    may_go() {
        (( $1 > 0 )) && { (( count > keep )) || (( now - $1 > max_age )); }
    }
    # Whether $1 is a directory directly inside root $2 (both resolved),
    # and not reached through a symlink.
    owned_child() {
        [[ -n "$2" && -d "$1" && ! -L "$1" ]] || return 1
        local real
        real="$(realpath -e -- "$1")" || return 1
        [[ "$real" == "$1" && "$(dirname -- "$real")" == "$2" ]]
    }
    # Deletes owned directory $1 of root $2, renamed out of the way first;
    # adds what that frees to `freed`: files with no other link, since uv
    # hardlinks an environment's packages from its cache. du counted those
    # too and reported 20.8 GB for a first prune in WSL that freed 7 GB
    # (2026-10-05).
    remove_owned() {
        owned_child "$1" "$2" || return 1
        local trash="$1" kib
        if [[ "${1##*/}" != .trash-* ]]; then
            trash="$2/.trash-$BASHPID-${1##*/}"
            [[ ! -e "$trash" ]] || return 1
            mv -- "$1" "$trash" || return 1
        fi
        kib="$(find "$trash" -links 1 -printf '%b\n' 2>/dev/null \
            | awk '{ s += $1 } END { printf "%d", s / 2 }')"
        rm -rf --one-file-system -- "$trash"
        freed=$(( freed + ${kib:-0} ))
    }
    # Opens lock file $1 on a new descriptor, stored in `fd`, and takes it
    # exclusively without waiting. Appending keeps its mtime; one created
    # here is dated 1970, so it never reads as a recent use. A symlink is
    # never followed.
    try_lock() {
        [[ -L "$1" ]] && return 1
        local made=0
        [[ -e "$1" ]] || made=1
        exec {fd}>>"$1" || return 1
        (( made )) && touch -m -d @0 -- "$1" 2>/dev/null
        flock -n "$fd" && return 0
        exec {fd}>&-
        return 1
    }
    # Whether any process uses directory $1 (resolved) or $2 (as a run names
    # it): its argv[0], or its working directory, lies inside either. One
    # find reads every working directory; nothing to read where there is
    # no /proc.
    in_use() {
        local dir arg0 cwd
        for dir in /proc/[0-9]*; do
            arg0=""
            IFS= read -r -d '' arg0 2>/dev/null < "$dir/cmdline"
            [[ "$arg0" == "$1"/* || "$arg0" == "$2"/* ]] && return 0
        done
        while IFS= read -r cwd; do
            [[ "$cwd" == "$1" || "$cwd" == "$1"/* || "$cwd" == "$2" || "$cwd" == "$2"/* ]] \
                && return 0
        done < <(find /proc/[0-9]*/cwd -maxdepth 0 -printf '%l\n' 2>/dev/null)
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

    local -i copies=0 envs=0 locks=0 freed=0 fd=0 copy_fd=0 count=0 removed=0
    local path t line lock_root pid
    local -a order=()
    local -A kept_env=() copy_lock=()

    # What an interrupted prune left, unless that pruner still runs.
    for lock_root in "$work_root" "$env_root"; do
        [[ -n "$lock_root" ]] || continue
        for path in "$lock_root"/.trash-*; do
            [[ -d "$path" ]] || continue
            pid="${path##*/.trash-}"
            pid="${pid%%-*}"
            [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && continue
            remove_owned "$path" "$lock_root"
        done
    done

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
            removed=0
            if may_go "${line%% *}" && try_lock "$path.lock"; then
                # A run may have finished with it since it was read.
                t="$(last_use "$path.lock" "$path" "$path/.git/HEAD" "$path/.git/index")"
                if may_go "$t" && ! in_use "$path" "$work_dir/${path##*/}" \
                        && remove_owned "$path" "$work_root"; then
                    copies+=1
                    count=$(( count - 1 ))
                    removed=1
                fi
                exec {fd}>&-
            fi
            (( removed )) || kept_env["$(env_of "$path")"]=1
        done < <(printf '%s\n' "${order[@]}" | sort -n)
        [[ "$own_copy" == "$work_root"/* ]] && kept_env["$(env_of "$own_copy")"]=1
        # Every copy lock, by the environment its run would use.
        for path in "$work_root"/*.lock; do
            [[ -f "$path" && ! -L "$path" ]] && copy_lock["$(env_of "${path%.lock}")"]="$path"
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
            may_go "${line%% *}" || continue
            copy_fd=0
            if [[ -n "${copy_lock[$path]:-}" ]]; then
                try_lock "${copy_lock[$path]}" || continue
                copy_fd=$fd
            fi
            if try_lock "$path.lock"; then
                t="$(last_use "$path.lock" "$path" "$path/pyvenv.cfg" "$path"/lib/python*/site-packages)"
                if may_go "$t" && ! in_use "$path" "$venv_root/${path##*/}" \
                        && remove_owned "$path" "$env_root"; then
                    rm -f -- "$path.lock"
                    envs+=1
                    count=$(( count - 1 ))
                fi
                exec {fd}>&-
            fi
            (( copy_fd )) && exec {copy_fd}>&-
        done < <(printf '%s\n' "${order[@]}" | sort -n)
    fi

    # Lock files whose copy or environment is gone (removed by hand, or kept
    # past their copy, as above), once they are unused as long.
    for lock_root in "$work_root" "$env_root"; do
        [[ -n "$lock_root" ]] || continue
        for path in "$lock_root"/*.lock; do
            [[ -f "$path" && ! -L "$path" && ! -e "${path%.lock}" ]] || continue
            [[ "${path%.lock}" == "$own_copy" || "${path%.lock}" == "$own_env" ]] && continue
            try_lock "$path" || continue
            t="$(last_use "$path")"
            if (( t > 0 && now - t > max_age )) && [[ ! -e "${path%.lock}" ]]; then
                rm -f -- "$path" && locks+=1
            fi
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
set -- "${run_args[@]}"
# stdout, beside "testing <sha>": through ops/wsl-suite.ps1 a redirected
# stdout and stderr can land in one file at separate offsets, and this
# line on stderr overwrote that one (2026-10-19).
if [[ "$run_note" == pass ]]; then
    echo "wsl-suite: chosen arguments, passed to pytest unchanged (no xdist workers added)"
else
    echo "wsl-suite: $run_note"
fi

# Opt-in profiling (tests/suite_profile_plugin.py): the plugin is added to
# whatever pytest runs, default or chosen, so a profiled run tests the same
# selection with the same workers as an unprofiled one.
profile_dir=""
case "${PSEUDOLIFE_SUITE_PROFILE:-}" in
    ""|0|off|false|no) ;;
    *)
        profile_dir="${PSEUDOLIFE_SUITE_PROFILE_DIR:-$HOME/.pseudolife-mcp/suite-profiles/$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$root" rev-parse --short=8 HEAD 2>/dev/null || echo worktree)}"
        mkdir -p "$profile_dir"
        export PSEUDOLIFE_SUITE_PROFILE_DIR="$profile_dir"
        set -- -p tests.suite_profile_plugin "$@"
        echo "wsl-suite: profiling to $profile_dir"
        ;;
esac
# The command a full-suite run builds, every word from the launcher's own
# allowlist; chosen arguments are not echoed (they may name local paths).
if [[ "$run_note" != pass ]]; then
    printf 'wsl-suite: pytest'
    printf ' %q' "$@"
    printf '\n'
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
# The summary never changes the run's exit code; it runs only when the
# plugin left its manifest (a run that never reached pytest leaves none).
if [[ -n "$profile_dir" && -f "$profile_dir/manifest.json" ]]; then
    "$venv/bin/python" evals/suite_profile_summary.py --profile-dir "$profile_dir" \
        --tag "${profile_dir##*/}" --out "$profile_dir/summary.json" >&2 \
        || echo "wsl-suite: the profile summary failed; the profile is in $profile_dir" >&2
fi
exit "$code"
