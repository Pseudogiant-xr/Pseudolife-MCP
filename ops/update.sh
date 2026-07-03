#!/usr/bin/env bash
# Safely update ONLY the PseudoLife-MCP daemon to the current checkout code.
# Bash port of ops/update.ps1 for Linux/macOS hosts.
#
#   ops/update.sh                  # backup -> tag rollback -> daemon-only rebuild -> health
#   ops/update.sh --tag pre-x      # name the rollback image tag suffix
#   ops/update.sh --no-backup      # skip the pg_dump (NOT recommended)
#
# Rebuilds + recreates ONLY the daemon container (`--no-deps`), so Postgres and
# the extractor are never touched. The bank lives in EXTERNAL volumes; this never
# runs `down -v`. Run after `git pull` (or local edits) to deploy daemon changes.
set -euo pipefail

TAG=""
NO_BACKUP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --tag)       TAG="$2"; shift 2 ;;
        --no-backup) NO_BACKUP=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

repo="$(cd "$(dirname "$0")/.." && pwd)"
compose_file="$repo/ops/docker-compose.yml"
env_file="$repo/ops/.env"
compose=(-f "$compose_file")
[ -f "$env_file" ] && compose=(--env-file "$env_file" "${compose[@]}")

# 1. Backup the bank (pg_dump inside the container) — the always-first rule.
if [ "$NO_BACKUP" -eq 0 ]; then
    echo "==> Backing up the bank (pg_dump)..."
    "$(dirname "$0")/backup.sh"
else
    echo "WARNING: skipping backup (--no-backup)." >&2
fi

# 2. Tag the current daemon image so a bad build can be rolled back. The tag
#    is read from the compose file so this script never drifts from it.
image_tag="$(sed -n 's/^[[:space:]]*image:[[:space:]]*\(pseudolife-daemon:[^[:space:]]*\).*/\1/p' "$compose_file" | head -1)"
[ -n "$image_tag" ] || { echo "could not find the pseudolife-daemon image tag in $compose_file" >&2; exit 1; }
stamp="$(date +%Y%m%d-%H%M%S)"
[ -n "$TAG" ] || TAG="pre-update-$stamp"
rollback="$image_tag-$TAG"
if docker image inspect "$image_tag" >/dev/null 2>&1; then
    docker tag "$image_tag" "$rollback"
    echo "==> Tagged rollback image: $rollback"
else
    echo "WARNING: no current $image_tag image to tag (first build?)." >&2
fi

# 3. Rebuild + recreate ONLY the daemon. `--no-deps` is what keeps Postgres and
#    the extractor untouched (without it, `up --build <svc>` recreates all three).
echo "==> Rebuilding the daemon only (Postgres + extractor untouched)..."
docker compose "${compose[@]}" up -d --no-deps --build pseudolife-daemon

# 4. Wait for health.
echo "==> Waiting for the daemon to report healthy..."
healthy=""
for _ in $(seq 1 30); do
    if curl -fsS --max-time 3 http://127.0.0.1:8765/health 2>/dev/null \
        | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        healthy=1
        break
    fi
    sleep 1.5
done
if [ -n "$healthy" ]; then
    echo "==> Healthy."
    echo "    Rolled-back deploy if ever needed:"
    echo "      docker tag $rollback $image_tag"
    echo "      docker compose -f \"$compose_file\" up -d --no-deps pseudolife-daemon"
else
    echo "WARNING: daemon did not report healthy. Logs: docker logs pseudolife-mcp-daemon" >&2
    echo "WARNING: rollback: docker tag $rollback $image_tag; then re-run the up line above." >&2
    exit 1
fi
