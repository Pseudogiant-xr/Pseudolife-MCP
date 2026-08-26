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
#
# Rebuilds + recreates ONLY the daemon container (`--no-deps`), so Postgres and
# the extractor are never touched. The bank lives in EXTERNAL volumes; this never
# runs `down -v`. Run after `git pull` (or local edits) to deploy daemon changes.
set -euo pipefail

TAG=""
NO_BACKUP=0
KEEP_ROLLBACKS=2
KEEP_CACHE_HOURS=168
NO_CACHE_PRUNE=0
# Override for the "a build already ran without a completed deploy" guard in
# step 2 — see the comment there before reaching for it.
FORCE_ROLLBACK_TAG=0

while [ $# -gt 0 ]; do
    case "$1" in
        --tag)            TAG="$2"; shift 2 ;;
        --no-backup)      NO_BACKUP=1; shift ;;
        --keep-rollbacks) KEEP_ROLLBACKS="$2"; shift 2 ;;
        --keep-cache-hours) KEEP_CACHE_HOURS="$2"; shift 2 ;;
        --no-cache-prune)   NO_CACHE_PRUNE=1; shift ;;
        --force-rollback-tag) FORCE_ROLLBACK_TAG=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

repo="$(cd "$(dirname "$0")/.." && pwd)"
compose_file="$repo/ops/docker-compose.yml"
env_file="$repo/ops/.env"
override_file="$repo/ops/docker-compose.override.yml"
compose=(-f "$compose_file")
# Scaffold the (gitignored) machine-local env from the example so its knobs
# are discoverable — every line ships commented, so this changes nothing.
if [ ! -f "$env_file" ] && [ -f "$repo/ops/.env.example" ]; then
    cp "$repo/ops/.env.example" "$env_file"
    echo "==> Scaffolded ops/.env from ops/.env.example (all values commented)."
fi
# Machine-local overrides (e.g. a fine-tuned GGUF mount) live in the gitignored
# override file; explicit -f disables compose's auto-merge, so add it here.
[ -f "$override_file" ] && compose+=(-f "$override_file")
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
daemon_container="pseudolife-mcp-daemon"
# The rollback tag is only worth anything if it points at the LAST-GOOD
# image, and the version tag alone is not proof of that: `docker compose up
# --build` below builds FIRST and the deploy is validated AFTER, so a run
# that aborts in between leaves the version tag on a freshly built, never
# validated image. Re-running update.sh — the obvious next move — then tagged
# THAT as the rollback and destroyed the only pointer to the last-good image
# (this happened live on the Windows side on 2026-08-13).
#
# The daemon container still holds the image that was actually deployed, so
# the two IDs disagreeing IS that situation. `inspect` answers for a STOPPED
# container too, so deploying from a stopped stack is still guarded; only a
# container that does not exist at all (fresh install, or it was removed)
# leaves nothing to compare and keeps the pre-guard behavior. Known blind
# spot: a build that recreated the container and then failed its health check
# leaves both IDs on the new image — that path exits with the rollback
# instructions already printed, and is meant to be acted on then.
tag_image_id="$(docker image inspect -f '{{.Id}}' "$image_tag" 2>/dev/null || true)"
running_image_id="$(docker inspect -f '{{.Image}}' "$daemon_container" 2>/dev/null || true)"
rollback_state=none
if [ -z "$tag_image_id" ]; then
    echo "WARNING: no current $image_tag image to tag (first build, or the version was bumped before this image was ever built)." >&2
    echo "WARNING: this deploy has NO rollback image. Rolling back means rebuilding the previous code." >&2
elif [ -n "$running_image_id" ] && [ "$running_image_id" != "$tag_image_id" ] \
    && [ "$FORCE_ROLLBACK_TAG" != "1" ]; then
    rollback_state=kept
    echo "WARNING: REFUSING to move the rollback tag: $image_tag is NOT the image the running daemon deployed ($running_image_id vs $tag_image_id)." >&2
    echo "WARNING: that means a build already ran without a completed deploy, so tagging it now would overwrite the last-good rollback with an unvalidated image." >&2
    echo "WARNING: existing rollback tags are untouched. Re-run with --force-rollback-tag once you are sure $image_tag IS the image you would want to roll back to." >&2
else
    docker tag "$image_tag" "$rollback"
    rollback_state=tagged
    echo "==> Tagged rollback image: $rollback"
    if [ "$FORCE_ROLLBACK_TAG" = "1" ] && [ -n "$running_image_id" ] \
        && [ "$running_image_id" != "$tag_image_id" ]; then
        echo "WARNING: --force-rollback-tag: tagged $image_tag even though the running daemon deployed a different image." >&2
    fi
fi

# Rollback instructions follow whether the tag actually exists. They used to
# print unconditionally, so a skipped tag produced a command that fails —
# worst on the unhealthy path below, where the operator reaches for it
# precisely because the deploy just broke.
print_rollback() {
    if [ "$rollback_state" = "tagged" ]; then
        echo "      docker tag $rollback $image_tag"
        echo "      docker compose -f \"$compose_file\" up -d --no-deps pseudolife-daemon"
    elif [ "$rollback_state" = "kept" ]; then
        echo "      (the rollback tag was NOT moved this run - see the warning above)"
        echo "      Pick the newest surviving rollback tag and redeploy it:"
        echo "      docker image ls ${image_tag%%:*}"
        echo "      docker tag <that tag> $image_tag"
        echo "      docker compose -f \"$compose_file\" up -d --no-deps pseudolife-daemon"
    else
        echo "      (no rollback image exists for this deploy - nothing was tagged)"
        echo "      Rebuild the last-good code instead, e.g.:"
        echo "      git checkout master && ops/update.sh"
    fi
}

# 2b. Retention: drop stale pre-* rollback tags beyond the newest N — one is
#     minted per deploy and they otherwise pile up without bound (~60 tags in
#     a 177GB docker_data.vhdx by 2026-07-14 on the Windows side). The script
#     never touches the deployed tag or an image a running container uses; a
#     retention hiccup must not abort the deploy.
if ! "$(dirname "$0")/prune-rollbacks.sh" --keep "$KEEP_ROLLBACKS" --repository "${image_tag%%:*}"; then
    echo "WARNING: rollback-tag retention failed (deploy continues)." >&2
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
    print_rollback
else
    echo "WARNING: daemon did not report healthy. Logs: docker logs pseudolife-mcp-daemon" >&2
    echo "WARNING: to roll back:" >&2
    print_rollback >&2
    exit 1
fi

# 5. Build-cache retention. Deliberately LAST, for two reasons: before the
#    build it would delete the cache the build reuses (cold-starting every
#    deploy), and on the unhealthy path above it would strip the cache an
#    operator's rollback rebuild wants — that branch exits, so this is
#    skipped for free. A retention hiccup must never fail a good deploy.
if [ "$NO_CACHE_PRUNE" != "1" ]; then
    if ! "$(dirname "$0")/prune-build-cache.sh" --max-age-hours "$KEEP_CACHE_HOURS"; then
        echo "WARNING: build-cache retention failed (deploy already succeeded)." >&2
    fi
fi
