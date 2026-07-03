#!/usr/bin/env bash
# pg_dump the PseudoLife-MCP database to data/backups/ with 7-day rotation.
# Bash port of ops/backup.ps1 for Linux/macOS hosts.
#
#   ops/backup.sh                  # dump into <repo>/data/backups
#   ops/backup.sh --keep-days 30
#
# Runs pg_dump INSIDE the container (no local postgres client needed).
# Off-disk mirror: point --mirror-dir (or PSEUDOLIFE_BACKUP_MIRROR) at a
# folder on ANOTHER disk / synced share. Mirror failure warns, never throws —
# the primary backup already succeeded and deploys must not abort because a
# mirror drive is unplugged.
set -euo pipefail

CONTAINER="pseudolife-mcp-postgres"
DB="pseudolife_memory"
DB_USER="pseudolife"
OUT_DIR=""
KEEP_DAYS=7
MIRROR_DIR="${PSEUDOLIFE_BACKUP_MIRROR:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --container)  CONTAINER="$2"; shift 2 ;;
        --db)         DB="$2"; shift 2 ;;
        --user)       DB_USER="$2"; shift 2 ;;
        --out-dir)    OUT_DIR="$2"; shift 2 ;;
        --keep-days)  KEEP_DAYS="$2"; shift 2 ;;
        --mirror-dir) MIRROR_DIR="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

repo="$(cd "$(dirname "$0")/.." && pwd)"
[ -n "$OUT_DIR" ] || OUT_DIR="$repo/data/backups"
mkdir -p "$OUT_DIR"

stamp="$(date +%Y%m%d-%H%M%S)"
out="$OUT_DIR/pseudolife_memory-$stamp.sql.gz"

echo "Dumping $DB from container $CONTAINER -> $out"
# Dump + gzip INSIDE the container, then copy the artifact out (mirrors the
# .ps1: no binary piping through the host shell). -Fc would be smaller but
# plain+gzip is trivially restorable with psql.
tmp="/tmp/pl_backup-$stamp.sql.gz"
docker exec "$CONTAINER" sh -c "pg_dump -U $DB_USER -d $DB | gzip -9 > $tmp"
docker cp "$CONTAINER:$tmp" "$out"
docker exec "$CONTAINER" rm -f "$tmp"
if [ ! -s "$out" ]; then
    echo "backup artifact missing or empty: $out" >&2
    exit 1
fi

# Rotation.
find "$OUT_DIR" -maxdepth 1 -name 'pseudolife_memory-*.sql.gz' -mtime +"$KEEP_DAYS" -delete

# Off-disk mirror (opt-in; same retention).
if [ -n "$MIRROR_DIR" ]; then
    if mkdir -p "$MIRROR_DIR" 2>/dev/null && cp "$out" "$MIRROR_DIR/" 2>/dev/null \
        && [ "$(wc -c < "$MIRROR_DIR/$(basename "$out")")" -eq "$(wc -c < "$out")" ]; then
        find "$MIRROR_DIR" -maxdepth 1 -name 'pseudolife_memory-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
        echo "Mirrored to $MIRROR_DIR/$(basename "$out")"
    else
        echo "WARNING: backup mirror failed (primary backup is safe)" >&2
    fi
fi

echo "Backup complete. Retained last $KEEP_DAYS days in $OUT_DIR"
