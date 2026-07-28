#!/usr/bin/env bash
# Prune stale pre-* rollback tags of the daemon image, keeping the newest N.
# Bash port of ops/prune-rollbacks.ps1 for Linux/macOS hosts.
#
#   ops/prune-rollbacks.sh               # keep the 2 newest rollback tags
#   ops/prune-rollbacks.sh --keep 5      # keep more
#
# update.sh mints one rollback tag per deploy and, before this script, never
# garbage-collected them (~60 stale tags inside a 177GB docker_data.vhdx by
# 2026-07-14 on the Windows side). Only ever removes image TAGS of the
# repository whose tag suffix follows the pre-* rollback naming; never the
# deployed tag (doesn't match the pattern), never an image a running
# container uses, never volumes.
set -euo pipefail

KEEP=2
REPOSITORY="pseudolife-daemon"

while [ $# -gt 0 ]; do
    case "$1" in
        --keep)       KEEP="$2"; shift 2 ;;
        --repository) REPOSITORY="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
case "$KEEP" in
    ''|*[!0-9]*) echo "--keep must be a non-negative integer" >&2; exit 2 ;;
esac

# Image IDs in use by running containers are never removed, even when stale.
# Mid-deploy this also protects the just-minted rollback tag: it shares the
# running daemon's image until the rebuild swaps it.
containers="$(docker ps -q)"
in_use=""
if [ -n "$containers" ]; then
    # shellcheck disable=SC2086  # container ids are word-split on purpose
    in_use="$(docker inspect --format '{{.Image}}' $containers)"
fi

# Rollback candidates: repository tags whose suffix follows the pre-* naming
# update.sh uses ("<version>-pre-update-<stamp>", or "--tag pre-x" named ones).
refs="$(docker image ls "$REPOSITORY" --format '{{.Repository}}:{{.Tag}}' \
    | grep -E ':.+-pre-' || true)"

# "created<TAB>ref<TAB>id" lines. `sort -r` puts the newest first; a timestamp
# tie (two tags on one image) breaks toward the lexicographically later tag,
# which for pre-update-<stamp> is the newer one.
candidates=""
if [ -n "$refs" ]; then
    while IFS= read -r ref; do
        line="$(docker image inspect --format '{{.Created}}|{{.Id}}' "$ref")"
        created="${line%%|*}"
        id="${line##*|}"
        candidates+="${created}"$'\t'"${ref}"$'\t'"${id}"$'\n'
    done <<< "$refs"
fi

stale=""
if [ -n "$candidates" ]; then
    stale="$(printf '%s' "$candidates" | sort -r | tail -n +$((KEEP + 1)))"
fi

removed=0
if [ -n "$stale" ]; then
    while IFS=$'\t' read -r created ref id; do
        [ -n "$ref" ] || continue
        if printf '%s\n' "$in_use" | grep -qxF "$id"; then
            echo "==> Rollback retention: keeping $ref (image in use by a running container)."
            continue
        fi
        if docker rmi "$ref" > /dev/null; then
            echo "==> Rollback retention: removed stale tag $ref."
            removed=$((removed + 1))
        else
            echo "WARNING: rollback retention: docker rmi $ref failed; leaving it." >&2
        fi
    done <<< "$stale"
fi

total="$(printf '%s' "$candidates" | grep -c . || true)"
kept=$(( KEEP < total ? KEEP : total ))
echo "==> Rollback retention: kept the newest $kept rollback tag(s), removed $removed stale."
