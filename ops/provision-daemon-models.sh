#!/bin/sh
# Install the pinned models once per persistent cache, then replace this
# process so the daemon receives the container's termination signals.
set -eu
marker="${TMPDIR:-/tmp}/pseudolife-model-provisioning"
if [ "$#" -eq 1 ] && [ "$1" = --healthcheck ]; then
    if [ -f "$marker" ]; then
        started=$(cat "$marker")
        case "$started" in ''|*[!0-9]*) exit 1 ;; esac
        age=$(( $(date +%s) - started ))
        if [ "$age" -ge 0 ] && [ "$age" -lt 1800 ]; then
            echo "starting: model provisioning"
            exit 0
        fi
        echo "model provisioning exceeded its 30-minute health grace" >&2
        exit 1
    fi
    body=$(curl --fail --silent --max-time 3 http://127.0.0.1:8765/health) || exit 1
    printf '%s\n' "$body" | grep -Eq '"ready"[[:space:]]*:[[:space:]]*true'
    exit $?
fi
# Container restart must not inherit a marker left by a killed provisioner.
rm -f "$marker"
if [ "$#" -eq 0 ]; then
    set -- pseudolife-daemon
fi
if [ "$#" -eq 1 ] && [ "$1" = pseudolife-daemon ] && [ -z "${PSEUDOLIFE_DAEMON_ONNX_DIR:-}" ]; then
    trap 'rm -f "$marker"' EXIT
    trap 'exit 1' HUP INT TERM
    (umask 077; date +%s > "$marker")
    pseudolife-daemon provision models --if-missing
    rm -f "$marker"
    trap - EXIT HUP INT TERM
fi
exec "$@"
