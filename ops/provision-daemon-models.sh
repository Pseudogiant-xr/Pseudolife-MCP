#!/bin/sh
# Install the pinned models once per persistent cache, then replace this
# process so the daemon receives the container's termination signals.
set -eu
marker="${TMPDIR:-/tmp}/pseudolife-model-provisioning"
if [ "$#" -eq 1 ] && [ "$1" = --healthcheck ]; then
    # Docker ends its start period at the first success. Wait inside this
    # check instead: downloads never mark the service healthy, and init gets
    # its own full budget after provisioning. Later checks are quick probes.
    while :; do
        body=$(curl --fail --silent --max-time 3 http://127.0.0.1:8765/health) || body=
        if printf '%s\n' "$body" | grep -Eq '"ready"[[:space:]]*:[[:space:]]*true'; then
            rm -f "$marker"
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
