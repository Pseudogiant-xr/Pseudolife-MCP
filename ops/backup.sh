#!/usr/bin/env bash
# pg_dump the PseudoLife-MCP database to data/backups/ with 7-day rotation,
# plus a tar of the daemon state volume (ChromaDB reference documents, cortex
# snapshot, graph snapshots) — the bank alone does not cover document_ingest.
# Bash port of ops/backup.ps1 for Linux/macOS hosts.
#
#   ops/backup.sh                  # dump into <repo>/data/backups
#   ops/backup.sh --keep-days 30
#   ops/backup.sh --mirror-keep 2  # cap the mirror at the newest 2 per kind
#
# Runs pg_dump INSIDE the container (no local postgres client needed).
# Off-disk mirror: point --mirror-dir (or PSEUDOLIFE_BACKUP_MIRROR) at a
# folder on ANOTHER disk / synced share. Mirror failure warns, never throws —
# the primary backup already succeeded and deploys must not abort because a
# mirror drive is unplugged. --mirror-keep (or PSEUDOLIFE_BACKUP_MIRROR_KEEP)
# caps the mirror at the newest N files by filename stamp — cloud-synced
# folders have untrustworthy mtimes and metered space; 0 = age-based.
set -euo pipefail

CONTAINER="pseudolife-mcp-postgres"
DAEMON_CONTAINER="pseudolife-mcp-daemon"
DB="pseudolife_memory"
DB_USER="pseudolife"
OUT_DIR=""
KEEP_DAYS=7
MIRROR_DIR="${PSEUDOLIFE_BACKUP_MIRROR:-}"
MIRROR_KEEP="${PSEUDOLIFE_BACKUP_MIRROR_KEEP:-0}"

while [ $# -gt 0 ]; do
    case "$1" in
        --container)   CONTAINER="$2"; shift 2 ;;
        --daemon-container) DAEMON_CONTAINER="$2"; shift 2 ;;
        --db)          DB="$2"; shift 2 ;;
        --user)        DB_USER="$2"; shift 2 ;;
        --out-dir)     OUT_DIR="$2"; shift 2 ;;
        --keep-days)   KEEP_DAYS="$2"; shift 2 ;;
        --mirror-dir)  MIRROR_DIR="$2"; shift 2 ;;
        --mirror-keep) MIRROR_KEEP="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
case "$MIRROR_KEEP" in
    ''|*[!0-9]*) echo "--mirror-keep must be a non-negative integer" >&2; exit 2 ;;
esac

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

# State volume (ChromaDB reference documents + cortex snapshot + graph
# snapshots), tarred from inside the daemon container — the only place those
# live; a pg_dump alone loses every document_ingest on restore. Warn, never
# fail: the DB dump above is the critical artifact and deploys must not
# abort because the daemon happens to be stopped (the skip is loud).
state_out="$OUT_DIR/pseudolife_state-$stamp.tgz"
state_tmp="/tmp/pl_state-$stamp.tgz"
if docker exec "$DAEMON_CONTAINER" sh -c "tar czf $state_tmp -C /data ." \
    && docker cp "$DAEMON_CONTAINER:$state_tmp" "$state_out" \
    && docker exec "$DAEMON_CONTAINER" rm -f "$state_tmp" \
    && [ -s "$state_out" ]; then
    echo "State volume -> $state_out"
else
    echo "WARNING: STATE VOLUME NOT BACKED UP (ingested documents + cortex/graph snapshots live there; is the daemon running?)" >&2
fi

# Rotation.
find "$OUT_DIR" -maxdepth 1 \( -name 'pseudolife_memory-*.sql.gz' -o -name 'pseudolife_state-*.tgz' \) -mtime +"$KEEP_DAYS" -delete

# Off-disk mirror (opt-in; --keep-days retention, or newest-N per kind with
# --mirror-keep). Mirrors both artifacts: the DB dump and the state tar.
if [ -n "$MIRROR_DIR" ]; then
    mirror_one() { # $1 = artifact path; returns non-zero on copy/verify failure
        cp "$1" "$MIRROR_DIR/" 2>/dev/null \
            && [ "$(wc -c < "$MIRROR_DIR/$(basename "$1")")" -eq "$(wc -c < "$1")" ] \
            && echo "Mirrored to $MIRROR_DIR/$(basename "$1")"
    }
    mirror_ok=1
    if mkdir -p "$MIRROR_DIR" 2>/dev/null; then
        mirror_one "$out" || mirror_ok=0
        if [ -s "$state_out" ]; then
            mirror_one "$state_out" || mirror_ok=0
        fi
        for pat in 'pseudolife_memory-.*\.sql\.gz' 'pseudolife_state-.*\.tgz'; do
            if [ "$MIRROR_KEEP" -gt 0 ]; then
                ls -1 "$MIRROR_DIR" | grep -E "^$pat$" | sort -r \
                    | tail -n +$((MIRROR_KEEP + 1)) | while IFS= read -r f; do
                        rm -f "$MIRROR_DIR/$f"
                    done || true
            fi
        done
        if [ "$MIRROR_KEEP" -eq 0 ]; then
            find "$MIRROR_DIR" -maxdepth 1 \( -name 'pseudolife_memory-*.sql.gz' -o -name 'pseudolife_state-*.tgz' \) -mtime +"$KEEP_DAYS" -delete
        fi
    else
        mirror_ok=0
    fi
    [ "$mirror_ok" -eq 1 ] || echo "WARNING: backup mirror failed (primary backup is safe)" >&2
fi

echo "Backup complete. Retained last $KEEP_DAYS days in $OUT_DIR"
