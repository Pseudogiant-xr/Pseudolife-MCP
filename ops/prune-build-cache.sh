#!/usr/bin/env bash
# Retention for the BuildKit build cache. Bash port of ops/prune-build-cache.ps1.
#
#   ops/prune-build-cache.sh                     # age 168h, ceiling 20GB
#   ops/prune-build-cache.sh --dry-run           # report only; mutate nothing
#   ops/prune-build-cache.sh --max-age-hours 24  # tighter age policy
#
# Build cache is pure derived data: over-pruning costs rebuild time and
# nothing else. This script ONLY ever calls `docker builder prune`. It never
# removes images (ops/prune-rollbacks.sh owns rollback-tag retention), never
# touches containers, and never runs `docker system prune` or any volume
# command — the bank lives in the external ops_pseudolife_* volumes.
#
# The fstrim step is Windows/WSL-only; on Linux there is no vhdx and the
# prune alone is the whole job.
set -euo pipefail
# Belt-and-braces: without this (bash >= 4.4 only), a failing command inside
# a $(...) command substitution does not abort that substitution early —
# it only affects the substitution's own exit status, which callers can
# still discard. build_cache_bytes()/stale_estimate_bytes() are invoked via
# $(...), so this alone would NOT be sufficient; the explicit exit-status
# checks below are the real fix. This is defense in depth only.
shopt -s inherit_errexit 2>/dev/null || true

MAX_AGE_HOURS=168
MAX_USED_SPACE_GB=20
DRY_RUN=0
NO_TRIM=0

while [ $# -gt 0 ]; do
    case "$1" in
        --max-age-hours)      MAX_AGE_HOURS="$2"; shift 2 ;;
        --max-used-space-gb)  MAX_USED_SPACE_GB="$2"; shift 2 ;;
        --dry-run)            DRY_RUN=1; shift ;;
        --no-trim)            NO_TRIM=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
for v in "$MAX_AGE_HOURS" "$MAX_USED_SPACE_GB"; do
    case "$v" in
        ''|*[!0-9]*) echo "--max-age-hours/--max-used-space-gb must be non-negative integers" >&2; exit 2 ;;
    esac
done
# Matches the ps1's [ValidateRange(0, 876000)] / [ValidateRange(0, 100000)].
if [ "$MAX_AGE_HOURS" -gt 876000 ]; then
    echo "--max-age-hours must be between 0 and 876000" >&2
    exit 2
fi
if [ "$MAX_USED_SPACE_GB" -gt 100000 ]; then
    echo "--max-used-space-gb must be between 0 and 100000" >&2
    exit 2
fi

# docker humanizes with SI (1000-based) units and a lowercase k: "8.192kB".
docker_size_to_bytes() {
    printf '%s' "$1" | awk '
        {
            if (match($0, /^[ ]*[0-9.]+/) == 0) { exit 1 }
            num = substr($0, RSTART, RLENGTH) + 0
            unit = toupper(substr($0, RSTART + RLENGTH))
            gsub(/[ ]/, "", unit)
            m = 1
            if (unit == "KB") m = 1000
            else if (unit == "MB") m = 1000000
            else if (unit == "GB") m = 1000000000
            else if (unit == "TB") m = 1000000000000
            else if (unit == "PB") m = 1000000000000000
            else if (unit != "B") { exit 1 }
            printf "%d", num * m
        }'
}

format_bytes() {
    # LC_NUMERIC=C: matches the ps1's InvariantCulture formatting — without
    # it, a comma-decimal locale makes awk's %.2f emit "12,45GB".
    LC_NUMERIC=C awk -v b="$1" 'BEGIN {
        if (b >= 1000000000) printf "%.2fGB", b / 1000000000
        else if (b >= 1000000) printf "%.2fMB", b / 1000000
        else printf "%dB", b
    }'
}

build_cache_bytes() {
    local row type size out
    # Capture via a plain command substitution (not process substitution)
    # so `docker system df`'s own exit status is available directly — a
    # `while ... < <(cmd)` process substitution discards it.
    if ! out="$(docker system df --format '{{.Type}}|{{.Size}}')"; then
        echo "docker system df failed" >&2
        return 1
    fi
    while IFS= read -r row; do
        type="${row%%|*}"
        size="${row#*|}"
        if [ "$type" = "Build Cache" ]; then
            # Explicit status check, NOT the trailing `return 0` alone:
            # this call runs inside the $(...) that invokes
            # build_cache_bytes at the call site, where `set -e` does not
            # abort on a failing intermediate command (bash does not
            # inherit errexit into command substitutions unless
            # `inherit_errexit` is set). A bare `docker_size_to_bytes
            # "$size"; return 0` would silently discard a parse failure.
            if ! docker_size_to_bytes "$size"; then
                echo "unparseable docker size: '$size'" >&2
                return 1
            fi
            return 0
        fi
    done <<< "$out"
    echo "docker system df reported no Build Cache row" >&2
    return 1
}

# Dry-run only. BuildKit has no --dry-run, so this sums entries whose
# CreatedAt precedes the cutoff. It is an ESTIMATE: BuildKit's own until=
# filter considers last-used, and shared entries may not free fully.
stale_estimate_bytes() {
    local cutoff row created size total=0 when out bytes
    cutoff="$(date -u -d "-${MAX_AGE_HOURS} hours" +%s)"
    # Plain command substitution (not process substitution) so
    # `docker builder du`'s own exit status is available directly.
    if ! out="$(docker builder du --format '{{.CreatedAt}}|{{.Size}}')"; then
        echo "docker builder du failed" >&2
        return 1
    fi
    while IFS= read -r row; do
        [ -n "$row" ] || continue
        created="${row%%|*}"
        size="${row#*|}"
        # "2026-07-28 05:31:09.884 +0000 UTC" -> strip fractional secs + zone
        # name, which GNU date will not parse together.
        created="$(printf '%s' "$created" | sed -E 's/\.[0-9]+//; s/ UTC$//')"
        when="$(date -u -d "$created" +%s 2>/dev/null || echo 0)"
        if [ "$when" -ne 0 ] && [ "$when" -lt "$cutoff" ]; then
            if ! bytes="$(docker_size_to_bytes "$size")"; then
                echo "unparseable docker size: '$size'" >&2
                return 1
            fi
            total=$(( total + bytes ))
        fi
    done <<< "$out"
    printf '%d' "$total"
}

cap_bytes=$(( MAX_USED_SPACE_GB * 1000000000 ))
before="$(build_cache_bytes)"
echo "==> Build-cache retention: $(format_bytes "$before") in cache (age policy ${MAX_AGE_HOURS}h, ceiling $(format_bytes "$cap_bytes"))."

if [ "$DRY_RUN" = "1" ]; then
    stale="$(stale_estimate_bytes)"
    echo "==> DRY RUN: nothing below is executed."
    echo "    age pass    : docker builder prune --force --filter until=${MAX_AGE_HOURS}h"
    echo "                  estimated reclaim $(format_bytes "$stale")."
    echo "                  (Estimate: sums CreatedAt; BuildKit's until= uses last-used,"
    echo "                   and shared entries may not free fully.)"
    if [ $(( before - stale )) -gt "$cap_bytes" ]; then
        echo "    ceiling pass: docker builder prune --force --max-used-space $cap_bytes"
    else
        echo "    ceiling pass: skipped (post-age size would be within the ceiling)."
    fi
    if [ "$NO_TRIM" != "1" ]; then
        echo "    trim        : wsl -d docker-desktop -e fstrim -v /mnt/docker-desktop-disk"
    fi
    echo "==> DRY RUN complete: nothing was changed."
    exit 0
fi

# Age pass — the normal policy, always runs.
docker builder prune --force --filter "until=${MAX_AGE_HOURS}h" > /dev/null

# Ceiling pass — backstop only.
after_age="$(build_cache_bytes)"
if [ "$after_age" -gt "$cap_bytes" ]; then
    echo "==> Build-cache retention: $(format_bytes "$after_age") still over the $(format_bytes "$cap_bytes") ceiling; enforcing."
    docker builder prune --force --max-used-space "$cap_bytes" > /dev/null
fi

after="$(build_cache_bytes)"
echo "==> Build-cache retention: $(format_bytes "$before") -> $(format_bytes "$after") (reclaimed $(format_bytes $(( before - after ))))."

# fstrim returns freed blocks to the vhdx free list; it does NOT shrink the
# host file. Windows/WSL only, never fatal.
[ "$NO_TRIM" = "1" ] && exit 0
if ! command -v wsl > /dev/null 2>&1; then
    echo "==> Build-cache retention: no wsl on PATH; skipping fstrim."
    exit 0
fi
if ! wsl -d docker-desktop -e true 2>/dev/null; then
    echo "==> Build-cache retention: no docker-desktop WSL distro; skipping fstrim."
    exit 0
fi
if ! wsl -d docker-desktop -e fstrim -v /mnt/docker-desktop-disk; then
    echo "WARNING: fstrim failed (retention otherwise succeeded)." >&2
fi
