#!/usr/bin/env bash
# Per-turn memory-change note: prints only when memory changed since this
# session's last note (new lessons, other sessions' status notes). The daemon
# request, its cursor and the connection checks live in session-start.sh;
# sourcing it keeps one copy of those checks and costs no second bash
# process per turn.
set -- memory-changes
. "$(dirname "${BASH_SOURCE[0]}")/session-start.sh"
exit 0
