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
    if command -v "$candidate" >/dev/null 2>&1; then python_cmd="$candidate"; break; fi
done
if [ -z "$python_cmd" ]; then
    echo "WARNING: no python on PATH: the deploy is Python (ops/update.py). Install Python >= 3.10 and re-run." >&2
    exit 1
fi
exec "$python_cmd" "$repo/ops/update.py" "${args[@]}"
