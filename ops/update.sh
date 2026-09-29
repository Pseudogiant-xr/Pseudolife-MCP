#!/usr/bin/env bash
# Safely update ONLY the Pseudolife-MCP daemon to the current checkout code.
# Bash port of ops/update.ps1 for Linux/macOS hosts.
#
#   ops/update.sh                  # backup -> tag rollback -> daemon-only rebuild -> health
#   ops/update.sh --tag pre-x      # name the rollback image tag suffix
#   ops/update.sh --no-backup      # skip the pg_dump (NOT recommended)
#   ops/update.sh --keep-rollbacks 5  # rollback tags to retain (default 2)
#   ops/update.sh --keep-cache-hours 24  # build cache to retain, hours (default 168)
#   ops/update.sh --no-cache-prune    # skip build-cache retention entirely
#   ops/update.sh --force-rollback-tag # tag the rollback even when the version
#                                      # tag is not the running daemon's image
#   ops/update.sh --all                # after the daemon: shim, plugin cache,
#                                      # Codex hooks
#   ops/update.sh --allow-dirty        # deploy a tree with uncommitted or
#                                      # untracked files (stamped dirty=true),
#                                      # or one git cannot describe (unknown)
#
# HEALTH_RETRIES / HEALTH_DELAY_MS (environment) size the health wait.
#
# The deploy itself is pseudolife_memory/update_cli.py — the same code
# `pseudolife-mcp update` runs from an installed package with no checkout.
# This script only maps its flags and runs that code from THIS checkout
# (ops/update.py puts the checkout ahead of any installed package). It
# rebuilds + recreates ONLY the daemon container (`--no-deps`), so Postgres
# and the extractor are never touched; the bank lives in EXTERNAL volumes
# and nothing here ever runs `down -v`. Run after `git pull`; local edits
# must be committed first (or deployed with --allow-dirty).
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
step() {
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
        printf '\033[1;36m==>\033[0m %s\n' "$*"
    else
        echo "==> $*"
    fi
}

env_file="$repo/ops/.env"
# The Python deploy reads ops/.env itself; the rewrite happens here, before
# it starts, so ops/install.sh and this script keep one identical block.
# >>> env line endings >>>
# An ops/.env copied from a Windows host carries CRLF line endings, and a
# value read from it here then ends in a CR: `docker volume create` refused
# "pseudolife-mcp-bank-pg18\r" as an invalid name (Debian 13, 2026-09-29).
# Compose tolerates CRLF; the shell reads do not. The file is rewritten in
# place with LF endings, keeping its mode (owner-only once a token is
# minted) and a copy of the original beside it; a file already on LF is not
# touched. ops/install.sh and ops/update.sh carry this same block, and
# tests/test_installer_env_file.py keeps the two identical.
normalize_env_line_endings() {  # $1 = env file; says so when it rewrote it
    [ -f "$1" ] || return 0
    grep -q "$(printf '\r')" "$1" || return 0
    env_backup="$1.crlf-$(date +%Y%m%d-%H%M%S)"
    cp -p "$1" "$env_backup"
    tr -d '\r' < "$env_backup" > "$1"
    step "Rewrote $(basename "$1") with LF line endings (it had CRLF, which put a stray CR into every value read from it); the original is kept as $(basename "$env_backup")"
}
normalize_env_line_endings "$env_file"
# <<< env line endings <<<
args=(--checkout "$repo" --health-retries "${HEALTH_RETRIES:-30}" --health-delay-ms "${HEALTH_DELAY_MS:-1500}")

while [ $# -gt 0 ]; do
    case "$1" in
        --tag)            args+=(--rollback-tag "$2"); shift 2 ;;
        --no-backup)      args+=(--no-backup); shift ;;
        --keep-rollbacks) args+=(--keep-rollbacks "$2"); shift 2 ;;
        --keep-cache-hours) args+=(--keep-cache-hours "$2"); shift 2 ;;
        --no-cache-prune)   args+=(--no-cache-prune); shift ;;
        --force-rollback-tag) args+=(--force-rollback-tag); shift ;;
        --all)            args+=(--all); shift ;;
        --allow-dirty)    args+=(--allow-dirty); shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

python_cmd=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        python_cmd="$candidate"; break
    fi
done
if [ -z "$python_cmd" ]; then
    echo "WARNING: no Python >= 3.10 on PATH: the deploy is Python (ops/update.py). Install one and re-run." >&2
    exit 1
fi
exec "$python_cmd" "$repo/ops/update.py" "${args[@]}"
