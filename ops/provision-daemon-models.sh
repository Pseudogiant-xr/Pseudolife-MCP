#!/bin/sh
# Started as root: transfer root-owned state to the daemon account, then
# drop to it for good. Install the pinned models once per persistent cache,
# then replace this process so the daemon receives the container's
# termination signals.
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
if [ "$(id -u)" = 0 ]; then
    # Earlier root-run daemons, restores and update's model provisioning
    # leave root-owned files (the model tree is owner-only). Transfer only
    # root-owned entries, never through a symbolic link or into another
    # mount, and refuse to start rather than run with state it cannot use.
    for dir in ${PSEUDOLIFE_ENTRYPOINT_STATE_DIRS:-/data /models}; do
        [ -d "$dir" ] || continue
        if ! find "$dir" -xdev -uid 0 -exec chown -h 10001:10001 {} +; then
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
