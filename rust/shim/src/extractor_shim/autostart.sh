#!/usr/bin/env bash
set -euo pipefail
action=$1 owner_exe=$2
shift 2
native=$1 kind=$2 env_file=$3 name=$4 replace=$5 port=$6 host=$7 timeout=$8 count=$9
shift 9
serve=("${@:1:count}")
shift "$count"
command -v systemctl >/dev/null
command -v ss >/dev/null
command -v curl >/dev/null
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/$name"
if [ "$action" = remove ]; then
    systemctl --user disable --now "$name" 2>/dev/null || true
    rm -f -- "$unit"
    systemctl --user daemon-reload
    echo "Removed $name"
    exit 0
fi
# A waiting launcher and its runtime child belong to the same service cgroup.
owned_by_unit() {
    local process_id=$1 hierarchy controllers group
    [ -n "$unit_group" ] && [ "$unit_group" != / ] || return 1
    [ -r "/proc/$process_id/cgroup" ] || return 1
    while IFS=: read -r hierarchy controllers group; do
        if [[ "$group" = "$unit_group" || "$group" = "$unit_group/"* ]]; then return 0; fi
    done < "/proc/$process_id/cgroup"
    return 1
}
socket_pids() { grep -oE 'pid=[0-9]+' <<< "$1" | cut -d= -f2 | sort -u || true; }
unit_group="$(systemctl --user show "$name" --property ControlGroup --value 2>/dev/null || true)"
listeners="$(ss -H -ltnp "sport = :$port")"
if [ -n "$listeners" ]; then
    pids="$(socket_pids "$listeners")"
    owned=1
    [ -n "$pids" ] || owned=0
    for pid in $pids; do owned_by_unit "$pid" || owned=0; done
    if [ "$owned" != 1 ]; then
        echo "port $port belongs to another process; choose --port or stop its owner" >&2
        exit 1
    fi
fi
"$native" "$@"
mkdir -p "$unit_dir"
unit_quote() {
    local value="$1"
    value="${value//\\/\\\\}"; value="${value//\"/\\\"}"
    value="${value//%/%%}"; value="${value//$/\$\$}"
    printf '"%s"' "$value"
}
{
    printf '[Unit]\nDescription=Native %s extractor CLI shim\nAfter=network-online.target\n\n[Service]\nExecStart=' "$kind"
    unit_quote "$native"
    for arg in "${serve[@]}"; do printf ' '; unit_quote "$arg"; done
    printf '\nRestart=on-failure\nRestartSec=60\nStandardOutput=null\nStandardError=journal\n\n[Install]\nWantedBy=default.target\n'
} > "$unit"
systemctl --user daemon-reload
systemctl --user enable "$name"
systemctl --user restart "$name"
deadline=$((SECONDS + timeout))
verified=0
while [ "$SECONDS" -lt "$deadline" ]; do
    unit_group="$(systemctl --user show "$name" --property ControlGroup --value)"
    for pid in $(socket_pids "$(ss -H -ltnp "sport = :$port")"); do
        if owned_by_unit "$pid" && [ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" = "$owner_exe" ] &&
            curl --silent --fail --max-time 2 "http://$host:$port/models" >/dev/null; then
            verified=1; break
        fi
    done
    [ "$verified" = 0 ] || break
    sleep 1
done
[ "$verified" = 1 ] || { echo "native $kind shim did not bind $host:$port; inspect journalctl --user -u $name" >&2; exit 1; }
if [ "$replace" = yes ]; then
    if [ "$kind" = claude ]; then sibling=pseudolife-codex-shim.service; else sibling=pseudolife-sonnet-shim.service; fi
    if [ "$sibling" != "$name" ]; then
        systemctl --user disable --now "$sibling" 2>/dev/null || true
        rm -f -- "$unit_dir/$sibling"
        systemctl --user daemon-reload
    fi
fi
echo "Registered + started native $kind shim ($host:$port, pid $pid)."
echo "To reload $env_file: systemctl --user restart $name"
