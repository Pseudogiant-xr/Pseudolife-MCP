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
link_mode=symlink
if ! ln -s missing "$test_root/link-probe" 2>/dev/null || [ ! -L "$test_root/link-probe" ]; then
    link_mode=marker
fi
rm -f -- "$test_root/link-probe"
printf 'Mock enablement entries: %s\n' "$link_mode"

make_link() {
    local target=$1 path=$2
    mkdir -p -- "$(dirname -- "$path")"
    if [ "$link_mode" = symlink ]; then ln -s -- "$target" "$path"
    else printf 'mock symlink -> %s\n' "$target" > "$path"; fi
}

same_entry() {
    if [ -L "$1" ]; then
        [ -L "$2" ] && [ "$(readlink -- "$1")" = "$(readlink -- "$2")" ]
    else
        [ ! -L "$2" ] && cmp -s "$1" "$2"
    fi
}

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
        is-enabled)
            # systemctl also returns success for static and indirect units.
            case "$enabled" in enabled|static|indirect) return 0 ;; *) return 1 ;; esac
            ;;
        enable)
            # Restoring an older unit enables its own target, leaving any link
            # previously created for the generated unit's default target intact.
            if grep -q '^WantedBy=other.target$' "$unit"; then
                [ -e "$other_wants" ] || [ -L "$other_wants" ] || make_link "../mock-extractor.service" "$other_wants"
            elif grep -q '^WantedBy=default.target$' "$unit"; then
                [ -e "$wants" ] || [ -L "$wants" ] || make_link "../mock-extractor.service" "$wants"
            fi
            ;;
        # A name-wide disable also removes links from other user directories.
        disable) rm -f -- "$wants" "$other_wants" "$runtime_wants" ;;
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
    local had_unit=$1 enabled=$2 active=$3 had_config=$4 local_link=${5:-none}
    local label="unit=$had_unit enabled=$enabled active=$active config=$had_config link=$local_link"
    local fixture="$test_root/case-$cases" config unit wants other_wants runtime_wants vendor_wants result before
    cases=$((cases + 1))
    mkdir -p -- "$fixture/home/.config/systemd/user/default.target.wants" "$fixture/tmp"
    config="$fixture/home/mock-shim.env"
    unit="$fixture/home/.config/systemd/user/mock-extractor.service"
    wants="$fixture/home/.config/systemd/user/default.target.wants/mock-extractor.service"
    other_wants="$fixture/home/.config/systemd/user/other.target.wants/mock-extractor.service"
    runtime_wants="$fixture/runtime/systemd/user/other.target.wants/mock-extractor.service"
    vendor_wants="$fixture/vendor/systemd/user/other.target.wants/mock-extractor.service"
    if [ "$had_config" = 1 ]; then
        printf 'old mock configuration\n' > "$config"
        cp -- "$config" "$fixture/old-config"
    fi
    if [ "$had_unit" = 1 ]; then
        case "$enabled" in
            static) printf 'old mock unit\n' > "$unit" ;;
            indirect) printf 'old mock unit\n[Install]\nAlso=helper.service\n' > "$unit" ;;
            *) printf 'old mock unit\n[Install]\nWantedBy=other.target\n' > "$unit" ;;
        esac
        cp -- "$unit" "$fixture/old-unit"
    fi
    case "$local_link" in
        relative) make_link '../mock-extractor.service' "$wants" ;;
        absolute) make_link "$unit" "$wants" ;;
        dangling) make_link '../old-missing.service' "$wants" ;;
    esac
    if [ "$local_link" != none ]; then cp -P -- "$wants" "$fixture/old-wants"; fi
    # An absent local unit may still have enablement links to a vendor unit;
    # disabled/static/indirect fixtures have only the vendor-provided link.
    if [ "$enabled" = enabled ] || [ "$had_unit" = 0 ]; then
        mkdir -p -- "$fixture/vendor/systemd/user"
        printf 'vendor mock unit\n' > "$fixture/vendor/systemd/user/mock-extractor.service"
        make_link "$fixture/vendor/systemd/user/mock-extractor.service" "$other_wants"
        make_link "$fixture/vendor/systemd/user/mock-extractor.service" "$runtime_wants"
        cp -P -- "$other_wants" "$fixture/old-other-wants"
        cp -P -- "$runtime_wants" "$fixture/old-runtime-wants"
    fi
    make_link '../mock-extractor.service' "$vendor_wants"
    cp -P -- "$vendor_wants" "$fixture/old-vendor-wants"
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
    if [ "$local_link" != none ]; then check same_entry "$fixture/old-wants" "$wants"
    else check test ! -e "$wants"; check test ! -L "$wants"; fi
    if [ "$enabled" = enabled ] || [ "$had_unit" = 0 ]; then
        check same_entry "$fixture/old-other-wants" "$other_wants"
        check same_entry "$fixture/old-runtime-wants" "$runtime_wants"
    fi
    check same_entry "$fixture/old-vendor-wants" "$vendor_wants"
    check test "$(grep -c '^--user disable ' "$fixture/commands" || true)" -eq 0
    check test "$(grep -c '^--user enable mock-extractor.service$' "$fixture/commands")" -eq 1
    if [ "$active" = 1 ]; then
        check test -e "$fixture/active"
        check test "$(grep -c '^--user restart mock-extractor.service$' "$fixture/commands")" -eq 2
    else check test ! -e "$fixture/active"; fi
    check test -z "$(find "$fixture/tmp" -type f -print)"
    if [ "$failures" = "$before" ]; then printf 'PASS %s\n' "$label"; fi
}

for enabled in enabled disabled static indirect; do
    for active in 0 1; do run_case 1 "$enabled" "$active" 1; done
done
for local_link in relative absolute dangling; do
    for active in 0 1; do run_case 1 enabled "$active" 1 "$local_link"; done
done
run_case 0 absent 0 0
run_case 0 absent 0 1
printf '%s cases, %s failed assertions\n' "$cases" "$failures"
[ "$failures" = 0 ]
