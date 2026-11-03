#!/usr/bin/env bash
# Mock-only Linux autostart rollback regression; no service manager or CLI runs.
# Usage: bash rust/extractor_harness/autostart_rollback.sh [autostart.sh]
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
autostart=${1:-"$script_dir/../shim/src/extractor_shim/autostart.sh"}
test_root="$(mktemp -d "${TMPDIR:-/tmp}/extractor-autostart-rollback.XXXXXX")"
trap 'rm -rf -- "$test_root"' EXIT
failures=0
cases=0

systemctl() {
    printf '%s\n' "$*" >> "$fixture/commands"
    [ "$1" = --user ] || return 98
    shift
    case "$1" in
        show-environment|daemon-reload) ;;
        show)
            case "$*" in
                *LoadState*) printf 'loaded\n' ;;
                *ControlGroup*) printf '/mock/extractor.service\n' ;;
                *) return 98 ;;
            esac
            ;;
        is-active) [ -e "$fixture/active" ] ;;
        is-enabled) [ -e "$wants" ] ;;
        enable)
            mkdir -p -- "$(dirname -- "$wants")"
            # Model the enablement link with a marker, including on Git Bash.
            : > "$wants"
            ;;
        disable) rm -f -- "$wants" ;;
        stop) rm -f -- "$fixture/active" ;;
        restart) : > "$fixture/active" ;;
        *) return 98 ;;
    esac
}
ss() { printf 'ss\n' >> "$fixture/commands"; }
curl() { printf 'curl\n' >> "$fixture/commands"; return 97; }
sleep() { printf 'unexpected sleep\n' >&2; return 97; }
mock_native() {
    printf 'native %s\n' "$*" >> "$fixture/commands"
    printf 'new mock configuration\n' > "$config"
}

check() {
    if ! "$@"; then
        printf 'FAIL %s: %s\n' "$label" "$*" >&2
        failures=$((failures + 1))
    fi
}

run_case() {
    local had_unit=$1 enabled=$2 active=$3 had_config=$4
    local label="unit=$had_unit enabled=$enabled active=$active config=$had_config"
    local fixture="$test_root/case-$cases" config unit wants result before
    cases=$((cases + 1))
    mkdir -p -- "$fixture/home/.config/systemd/user/default.target.wants" "$fixture/tmp"
    config="$fixture/home/mock-shim.env"
    unit="$fixture/home/.config/systemd/user/mock-extractor.service"
    wants="$fixture/home/.config/systemd/user/default.target.wants/mock-extractor.service"
    if [ "$had_config" = 1 ]; then
        printf 'old mock configuration\n' > "$config"
        cp -- "$config" "$fixture/old-config"
    fi
    if [ "$had_unit" = 1 ]; then
        printf 'old mock unit\n' > "$unit"
        cp -- "$unit" "$fixture/old-unit"
    fi
    if [ "$enabled" = 1 ]; then : > "$wants"; fi
    if [ "$active" = 1 ]; then : > "$fixture/active"; fi
    before=$failures
    # This subprocess inherits only the mock functions, and all file mutations
    # target its disposable home. Empty ss output forces a health timeout.
    set +e
    (
        set -euo pipefail
        export HOME="$fixture/home" XDG_CONFIG_HOME="$fixture/home/.config" TMPDIR="$fixture/tmp"
        set -- install mock_native mock_native codex "$config" mock-extractor.service no \
            18765 127.0.0.1 0 1 serve mock-configure
        source "$autostart"
    ) > "$fixture/output" 2>&1
    result=$?
    set -e
    check test "$result" -eq 1
    check grep -q 'failed its health check' "$fixture/output"
    check grep -q '^native mock-configure$' "$fixture/commands"
    check grep -q '^--user enable mock-extractor.service$' "$fixture/commands"
    check grep -q '^--user restart mock-extractor.service$' "$fixture/commands"
    check grep -q '^ss$' "$fixture/commands"
    if [ "$had_config" = 1 ]; then check cmp -s "$fixture/old-config" "$config"
    else check test ! -e "$config"; fi
    if [ "$had_unit" = 1 ]; then check cmp -s "$fixture/old-unit" "$unit"
    else check test ! -e "$unit"; fi
    if [ "$enabled" = 1 ]; then check test -e "$wants"
    else check test ! -e "$wants"; fi
    if [ "$active" = 1 ]; then
        check test -e "$fixture/active"
        check test "$(grep -c '^--user restart mock-extractor.service$' "$fixture/commands")" -eq 2
    else check test ! -e "$fixture/active"; fi
    check test -z "$(find "$fixture/tmp" -type f -print)"
    if [ "$failures" = "$before" ]; then printf 'PASS %s\n' "$label"; fi
}

for enabled in 0 1; do
    for active in 0 1; do run_case 1 "$enabled" "$active" 1; done
done
run_case 0 0 0 0
run_case 0 0 0 1
printf '%s cases, %s failed assertions\n' "$cases" "$failures"
[ "$failures" = 0 ]
