#!/bin/sh
# Install the pinned models once per persistent cache, then replace this
# process so the daemon receives the container's termination signals.
set -eu
if [ "$#" -eq 0 ]; then
    set -- pseudolife-daemon
fi
if [ "$#" -eq 1 ] && [ "$1" = pseudolife-daemon ] && [ -z "${PSEUDOLIFE_DAEMON_ONNX_DIR:-}" ]; then
    pseudolife-daemon provision models --if-missing
fi
exec "$@"
