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
    awk -v b="$1" 'BEGIN {
        if (b >= 1000000000) printf "%.2fGB", b / 1000000000
        else if (b >= 1000000) printf "%.2fMB", b / 1000000
        else printf "%dB", b
    }'
}

build_cache_bytes() {
    local row type size
    while IFS= read -r row; do
        type="${row%%|*}"
        size="${row#*|}"
        if [ "$type" = "Build Cache" ]; then
            docker_size_to_bytes "$size"
            return 0
        fi
    done < <(docker system df --format '{{.Type}}|{{.Size}}')
    echo "docker system df reported no Build Cache row" >&2
    return 1
}

# Dry-run only. BuildKit has no --dry-run, so this sums entries whose
# CreatedAt precedes the cutoff. It is an ESTIMATE: BuildKit's own until=
# filter considers last-used, and shared entries may not free fully.
stale_estimate_bytes() {
    local cutoff row created size total=0 when
    cutoff="$(date -u -d "-${MAX_AGE_HOURS} hours" +%s)"
    while IFS= read -r row; do
        [ -n "$row" ] || continue
        created="${row%%|*}"
        size="${row#*|}"
        # "2026-07-28 05:31:09.884 +0000 UTC" -> strip fractional secs + zone
        # name, which GNU date will not parse together.
        created="$(printf '%s' "$created" | sed -E 's/\.[0-9]+//; s/ UTC$//')"
        when="$(date -u -d "$created" +%s 2>/dev/null || echo 0)"
        if [ "$when" -ne 0 ] && [ "$when" -lt "$cutoff" ]; then
            total=$(( total + $(docker_size_to_bytes "$size") ))
        fi
    done < <(docker builder du --format '{{.CreatedAt}}|{{.Size}}')
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
