#!/bin/sh
# Started as root: transfer root-owned state to the daemon account, then
# drop to it for good. Install the pinned models once per persistent cache,
# then replace this process so the daemon receives the container's
# termination signals.
#
# The native client also runs this file, byte for byte (its copy is
# rust/shim/src/cli/ownership/provision-daemon-models.sh), through
# `/bin/sh -c` in short-lived helper containers from the daemon's image:
#   --probe-access DIR...     as uid 10001 with no capabilities: what that
#                             account cannot use and cannot fix (U owner
#                             path), and what it owns but cannot use (O path);
#   --list-root-owned DIR...  as uid 0 without CHOWN: every uid-0 entry with
#                             its group (L group path), the undo list the
#                             client writes before anything changes;
#   --repair-ownership DIR... as uid 0 with CHOWN: gives every uid-0 entry to
#                             10001:10001 (C group path changed, F failed).
# Each answer is NUL-separated fields (the mode, 1, the records, end),
# hex-encoded by od so that no capture can alter a name. Exit 0 means every
# walk finished; 64 is a usage error, 70 an unreadable mount table, 71 a
# walk that failed and 72 a missing DIR. A DIR may also be a file (a file
# bind mount): it is checked by itself.
set -eu
marker="${TMPDIR:-/tmp}/pseudolife-model-provisioning"
if [ "$#" -eq 1 ] && [ "$1" = --healthcheck ]; then
    # Docker ends its start period at the first success. Wait inside this
    # check instead: downloads never mark the service healthy, and init gets
    # its own full budget after provisioning. Later checks are quick probes.
    while :; do
        body=$(curl --fail --silent --max-time 3 http://127.0.0.1:8765/health) || body=
        if printf '%s\n' "$body" | grep -Eq '"ready"[[:space:]]*:[[:space:]]*true'; then
            # The daemon account owns the marker in sticky /tmp, which a root
            # check without CAP_FOWNER cannot unlink; a kept marker is inert
            # once ready (its phase budget has already elapsed).
            rm -f "$marker" 2>/dev/null || :
            exit 0
        fi
        [ -f "$marker" ] || exit 1
        read -r phase started < "$marker" || exit 1
        case "$started" in ''|*[!0-9]*) exit 1 ;; esac
        case "$phase" in provision) budget=1800 ;; init) budget=90 ;; *) exit 1 ;; esac
        age=$(( $(date +%s) - started ))
        if [ "$age" -lt 0 ] || [ "$age" -ge "$budget" ]; then
            echo "model startup phase $phase exceeded its ${budget}s readiness budget" >&2
            exit 1
        fi
        sleep 1
    done
fi
# `find ROOT EXPRESSION...` that never enters another mount below ROOT.
# -xdev alone is not enough: two named volumes on one host filesystem share
# a device number, so every mount point the kernel lists below ROOT is
# pruned as well. Without the mount table nothing is walked. An entry the
# running daemon removes during the walk is not an error.
ownership_walk() {
    walk_root=$1
    shift
    if [ ! -r /proc/self/mountinfo ]; then
        echo "cannot read /proc/self/mountinfo, so the mounts below $walk_root cannot be skipped; nothing was walked" >&2
        return 70
    fi
    walk_count=$#
    set -- "$@" -xdev -ignore_readdir_race "(" -false
    while read -r walk_id walk_parent walk_device walk_source walk_point walk_rest; do
        # Field 5, with mountinfo's octal escapes (space, tab, newline,
        # backslash) decoded; the dots keep trailing newlines.
        walk_point=$(printf '%s.' "$walk_point" | sed 's/\\\([0-7][0-7][0-7]\)/\\0\1/g')
        walk_point=$(printf "%b" "${walk_point%.}."); walk_point=${walk_point%.}
        case $walk_point in
            "${walk_root%/}"/*)
                walk_glob=$(printf '%s.' "$walk_point" | sed 's/[][*?\\]/\\&/g')
                set -- "$@" -o -path "${walk_glob%.}" ;;
        esac
    done < /proc/self/mountinfo
    set -- "$@" ")" -prune -o
    # Move the caller's expression behind the pruned mounts.
    while [ "$walk_count" -gt 0 ]; do
        walk_word=$1
        shift
        set -- "$@" "$walk_word"
        walk_count=$((walk_count - 1))
    done
    find "$walk_root" "$@"
}
ownership_mode() {
    mode=$1
    shift
    if [ "$#" -eq 0 ]; then
        echo "usage: provision-daemon-models $mode DIR..." >&2
        return 64
    fi
    printf '%s\000' "${mode#--}" 1
    for root do
        if [ ! -e "$root" ] && [ ! -L "$root" ]; then
            if [ "$mode" = --probe-access ]; then
                # Absent, or below a directory 10001 cannot search: either
                # way that account cannot use it, and its owner is unknown.
                printf 'U\000?\000%s\000' "$root"
                continue
            fi
            echo "$root does not exist in this container" >&2
            return 72
        fi
        status=0
        case $mode in
            --probe-access)
                # What 10001 cannot use and cannot fix itself: another
                # owner's entry it cannot read, or directory it cannot enter
                # or write (U, with its owner). What it owns but cannot use
                # is detail only (O): the owner can change its own mode.
                # access(2) decides, so emulated ownership (Desktop bind
                # mounts, network filesystems) holds.
                ownership_walk "$root" ! -type l "(" \
                    -type d ! -user 10001 "(" ! -readable -o ! -executable -o ! -writable ")" -printf 'U\0%U\0%p\0' -prune \
                    -o -type d "(" ! -readable -o ! -executable ")" -printf 'O\0%p\0' -prune \
                    -o -type d ! -writable -printf 'O\0%p\0' \
                    -o ! -type d ! -user 10001 ! -readable -printf 'U\0%U\0%p\0' \
                    -o ! -type d ! -readable -printf 'O\0%p\0' ")" || status=$? ;;
            --list-root-owned)
                ownership_walk "$root" -uid 0 -printf 'L\0%G\0%p\0' || status=$? ;;
            *)
                # One chown per entry, never through a link, each reported
                # with the group it had (its owner was 0).
                ownership_walk "$root" -uid 0 "(" -exec chown -h 10001:10001 {} ";" -printf 'C\0%G\0%p\0' \
                    -o -printf 'F\0%G\0%p\0' ")" || status=$? ;;
        esac
        case $status in
            0) ;;
            70) return 70 ;;
            *) echo "the walk of $root failed (find exited $status)" >&2; return 71 ;;
        esac
    done
    printf '%s\000' end
}
case ${1:-} in
    --probe-access|--list-root-owned|--repair-ownership)
        if [ "$1" != --probe-access ] && [ "$(id -u)" != 0 ]; then
            echo "$1 runs only in the native client's ownership helper" >&2
            exit 64
        fi
        # The answer goes through od as hex; its exit status comes back on
        # fd 4, because a pipeline reports only od's.
        exec 3>&1
        code=$( { { mode_status=0; ownership_mode "$@" || mode_status=$?; echo "$mode_status" >&4; } | od -An -v -tx1 >&3; } 4>&1 )
        exit "${code:-71}" ;;
esac
if [ "$(id -u)" = 0 ]; then
    # Earlier root-run daemons, restores and update's model provisioning
    # leave root-owned files (the model tree is owner-only). Transfer only
    # root-owned entries, never through a symbolic link or into another
    # mount, and refuse to start rather than run with state it cannot use.
    for dir in ${PSEUDOLIFE_ENTRYPOINT_STATE_DIRS:-/data /models}; do
        [ -d "$dir" ] || continue
        if ! ownership_walk "$dir" -uid 0 -exec chown -h 10001:10001 {} +; then
            echo "Pseudolife container startup refused: cannot give $dir to UID 10001; make it writable by root or owned by 10001:10001" >&2
            exit 1
        fi
    done
    # Docker gives root HOME=/root, which the daemon account cannot read.
    if [ "${HOME:-}" = /root ]; then
        export HOME=/
    fi
    # setuid from root clears the permitted and effective capabilities;
    # no-new-privileges keeps them from coming back.
    exec setpriv --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all -- /bin/sh "$0" "$@"
fi
# Container restart must not inherit a marker left by a killed provisioner.
rm -f "$marker" "$marker.next"
mark_phase() {
    (umask 077; printf '%s %s\n' "$1" "$(date +%s)" > "$marker.next")
    mv -f "$marker.next" "$marker"
}
if [ "$#" -eq 0 ]; then
    set -- pseudolife-daemon
fi
if [ "$#" -eq 1 ] && [ "$1" = pseudolife-daemon ]; then
    trap 'rm -f "$marker" "$marker.next"' EXIT
    trap 'exit 1' HUP INT TERM
    if [ -z "${PSEUDOLIFE_DAEMON_ONNX_DIR:-}" ]; then
        mark_phase provision
        pseudolife-daemon provision models --if-missing
    fi
    mark_phase init
    trap - EXIT HUP INT TERM
fi
exec "$@"
