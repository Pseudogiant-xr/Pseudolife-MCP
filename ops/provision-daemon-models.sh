#!/bin/sh
# Install the pinned models once per persistent cache, then replace this
# process so the daemon receives the container's termination signals.
set -eu
if [ "$#" -eq 0 ]; then
    set -- pseudolife-daemon
fi
if [ "$#" -eq 1 ] && [ "$1" = pseudolife-daemon ]; then
    pseudolife-daemon provision models
fi
exec "$@"
