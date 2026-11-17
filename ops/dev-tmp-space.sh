#!/usr/bin/env bash
# Shared preflight for Linux dev-machine far halves. The 4 GiB ceiling
# implements the maintainer's 17 November 2026 tmpfs memory-use rule.
dev_tmp_preflight() {
    local prefix=$1 kind used
    kind=$(stat -f -c %T -- /tmp) || {
        printf '%s: cannot read /tmp filesystem type; install coreutils and retry\n' "$prefix"
        return 3
    }
    case "$kind" in tmpfs|ramfs)
        used=$(df -B1 --output=used -- /tmp | tail -1 | tr -d '[:space:]')
        if [[ ! "$used" =~ ^[0-9]+$ ]]; then
            printf '%s: cannot read memory-backed /tmp usage; inspect df -h /tmp and retry\n' "$prefix"
            return 3
        fi
        if (( used > 4294967296 )); then
            printf '%s: /tmp is memory-backed and holds %s bytes, above 4 GiB; inspect df -h /tmp, remove only your owned temporary files, use a disk-backed directory under home, and retry\n' "$prefix" "$used"
            return 3
        fi
    esac
}

dev_tmp_disk_path() {
    local prefix=$1 path=$2 resolved
    resolved=$(realpath -m -- "$path") || {
        printf '%s: cannot resolve an artifact path; fix its parent directory and retry\n' "$prefix"
        return 3
    }
    case "$resolved" in /tmp|/tmp/*)
        printf '%s: artifact path resolves under /tmp; configure the clone, environment, cache and saved output under home, then retry\n' "$prefix"
        return 3 ;;
    esac
}
