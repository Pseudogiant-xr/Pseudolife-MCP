#!/usr/bin/env bash
set -euo pipefail
action=$1 owner_exe=$2
shift 2
native=$1 kind=$2 env_file=$3 name=$4 replace=$5 port=$6 host=$7 timeout=$8 count=$9
shift 9
serve=("${@:1:count}")
shift "$count"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/$name"
if [ "$action" = remove ]; then
    [ -e "$unit" ] || [ -L "$unit" ] || exit 0
    if ! command -v systemctl >/dev/null; then
        echo "systemctl unavailable; skipping removal of $name"
        exit 0
    fi
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        echo "systemd user manager unavailable; skipping removal of $name"
        exit 0
    fi
    systemctl --user disable --now "$name" 2>/dev/null || true
    rm -f -- "$unit"
    systemctl --user daemon-reload
    echo "Removed $name"
    exit 0
fi
for tool in systemctl ss curl cmp mktemp cp sha256sum; do
    command -v "$tool" >/dev/null || { echo "native autostart requires $tool" >&2; exit 1; }
done
systemctl --user show-environment >/dev/null || { echo 'systemd user manager unavailable' >&2; exit 1; }
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
healthy_listener() {
    local expected headers
    expected="$(sha256sum < "$env_file")"
    expected="${expected%% *}"
    unit_group="$(systemctl --user show "$name" --property ControlGroup --value)"
    for pid in $(socket_pids "$(ss -H -ltnp "sport = :$port")"); do
        if owned_by_unit "$pid" && [ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" = "$owner_exe" ] &&
            headers="$(curl --silent --fail --max-time 2 --dump-header - --output /dev/null "http://$host:$port/health")"; then
            headers="${headers//$'\r'/}"
            if grep -qi "^x-pseudolife-config-sha256: $expected$" <<< "$headers"; then return 0; fi
        fi
    done
    return 1
}
umask 077
config_backup="$(mktemp)" unit_backup="$(mktemp)" desired="$(mktemp)"
had_config=0 had_unit=0 was_active=0 mutated=0 committed=0
if [ -e "$env_file" ]; then cp -- "$env_file" "$config_backup"; had_config=1; fi
if [ -e "$unit" ]; then cp -- "$unit" "$unit_backup"; had_unit=1; fi
systemctl --user is-active --quiet "$name" && was_active=1
finish() {
    result=$?
    trap - EXIT
    if [ "$result" != 0 ] && [ "$committed" = 0 ]; then
        if [ "$mutated" = 1 ]; then systemctl --user stop "$name" >/dev/null 2>&1 || true; fi
        if [ "$had_config" = 1 ]; then cp -- "$config_backup" "$env_file"; else rm -f -- "$env_file"; fi
        if [ "$mutated" = 1 ]; then
            if [ "$had_unit" = 1 ]; then cp -- "$unit_backup" "$unit";
            else systemctl --user disable "$name" >/dev/null 2>&1 || true; rm -f -- "$unit"; fi
            systemctl --user daemon-reload || true
            if [ "$was_active" = 1 ]; then systemctl --user restart "$name" || true; fi
        fi
    fi
    rm -f -- "$config_backup" "$unit_backup" "$desired"
    exit "$result"
}
trap finish EXIT
"$native" "$@" >/dev/null
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
} > "$desired"
unchanged=0
if [ "$had_config" = 1 ] && [ "$had_unit" = 1 ] && cmp -s "$config_backup" "$env_file" && cmp -s "$desired" "$unit" && healthy_listener; then
    unchanged=1
    if ! systemctl --user is-enabled --quiet "$name"; then
        systemctl --user enable "$name" >/dev/null
    fi
else
    mkdir -p "$unit_dir"
    mutated=1
    cp -- "$desired" "$unit"
    systemctl --user daemon-reload
    systemctl --user enable "$name"
    systemctl --user restart "$name"
    deadline=$((SECONDS + timeout))
    until healthy_listener; do
        [ "$SECONDS" -lt "$deadline" ] || { echo "native $kind shim failed its health check at $host:$port; inspect journalctl --user -u $name" >&2; exit 1; }
        sleep 1
    done
fi
committed=1
if [ "$replace" = yes ]; then
    if [ "$kind" = claude ]; then sibling=pseudolife-codex-shim.service; else sibling=pseudolife-sonnet-shim.service; fi
    if [ "$sibling" != "$name" ]; then
        systemctl --user disable --now "$sibling" 2>/dev/null || true
        rm -f -- "$unit_dir/$sibling"
        systemctl --user daemon-reload
    fi
fi
if [ "$unchanged" = 0 ]; then
    echo "Registered + started native $kind shim ($host:$port, pid $pid)."
    echo "To reload $env_file: systemctl --user restart $name"
fi
