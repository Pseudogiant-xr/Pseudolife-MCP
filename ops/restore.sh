#!/usr/bin/env bash
# Restore a Pseudolife-MCP pg_dump backup (the inverse of ops/backup.sh).
# Bash port of ops/restore.ps1 for Linux/macOS hosts.
#
#   ops/restore.sh                        # REHEARSAL (default, safe):
#                                         #   newest backup -> scratch db,
#                                         #   row-count report, drop scratch.
#   ops/restore.sh --backup-file <path>   # rehearse a specific backup
#   ops/restore.sh --apply                # REAL RESTORE into the live db:
#                                         #   safety-dump current bank first,
#                                         #   stop daemon, drop+recreate db,
#                                         #   restore, start daemon, health.
#   ops/restore.sh --apply --state-archive <pseudolife_state-*.tgz>
#                                         # ALSO replace the daemon state
#                                         # volume (ingested documents +
#                                         # cortex/graph snapshots) from a
#                                         # backup.sh state tar. Opt-in: a
#                                         # DB-only restore must not clobber
#                                         # current state.
#
# The rehearsal NEVER touches the live database — it exists so the restore
# path is a rehearsed procedure, not a hope.
set -euo pipefail

BACKUP_FILE=""
STATE_ARCHIVE=""
CONTAINER="pseudolife-mcp-postgres"
DAEMON_CONTAINER="pseudolife-mcp-daemon"
DB="pseudolife_memory"
DB_USER="pseudolife"
APPLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --backup-file)      BACKUP_FILE="$2"; shift 2 ;;
        --state-archive)    STATE_ARCHIVE="$2"; shift 2 ;;
        --container)        CONTAINER="$2"; shift 2 ;;
        --daemon-container) DAEMON_CONTAINER="$2"; shift 2 ;;
        --db)               DB="$2"; shift 2 ;;
        --user)             DB_USER="$2"; shift 2 ;;
        --apply)            APPLY=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

repo="$(cd "$(dirname "$0")/.." && pwd)"

# 1. Resolve + validate the backup artifact.
if [ -z "$BACKUP_FILE" ]; then
    BACKUP_FILE="$(ls -1t "$repo/data/backups"/pseudolife_memory-*.sql.gz 2>/dev/null | head -1 || true)"
    [ -n "$BACKUP_FILE" ] || { echo "no backups found under data/backups" >&2; exit 1; }
fi
[ -s "$BACKUP_FILE" ] || { echo "backup artifact missing or empty: $BACKUP_FILE" >&2; exit 1; }
echo "==> Backup: $BACKUP_FILE ($(( $(wc -c < "$BACKUP_FILE") / 1024 )) KB)"

if [ -n "$STATE_ARCHIVE" ]; then
    [ -s "$STATE_ARCHIVE" ] || { echo "state archive missing or empty: $STATE_ARCHIVE" >&2; exit 1; }
    echo "==> State archive: $STATE_ARCHIVE ($(( $(wc -c < "$STATE_ARCHIVE") / 1024 )) KB)"
else
    newest_state="$(ls -1t "$repo/data/backups"/pseudolife_state-*.tgz 2>/dev/null | head -1 || true)"
    if [ -n "$newest_state" ]; then
        echo "==> Note: state archives exist (newest: $(basename "$newest_state")); pass --state-archive to restore ingested documents + cortex/graph snapshots too."
    fi
fi

stamp="$(date +%Y%m%d-%H%M%S)"
tmp="/tmp/pl_restore-$stamp.sql.gz"
docker cp "$BACKUP_FILE" "$CONTAINER:$tmp"

tables="entries facts world_facts lessons entities edges episodes"

count_rows() { # $1 = database, $2 = table; prints -1 when the query fails
    docker exec "$CONTAINER" psql -tA -U "$DB_USER" -d "$1" \
        -c "SELECT count(*) FROM $2" 2>/dev/null || echo "-1"
}

cleanup() { docker exec "$CONTAINER" rm -f "$tmp" >/dev/null 2>&1 || true; }
trap cleanup EXIT

if [ "$APPLY" -eq 0 ]; then
    # ── REHEARSAL: restore into a scratch db, compare, drop ──────────────
    scratch="pseudolife_restore_rehearsal"
    echo "==> Rehearsal: restoring into scratch db '$scratch' (live bank untouched)"
    docker exec "$CONTAINER" psql -q -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $scratch"
    docker exec "$CONTAINER" psql -q -U "$DB_USER" -d postgres -c "CREATE DATABASE $scratch"
    if ! docker exec "$CONTAINER" sh -c "gunzip -c $tmp | psql -q -v ON_ERROR_STOP=1 -U $DB_USER -d $scratch > /dev/null"; then
        echo "restore into scratch db FAILED - the backup may be unusable" >&2
        exit 1
    fi

    printf '%-14s %10s %10s\n' "table" "live" "restored"
    any_lost=0
    for t in $tables; do
        live="$(count_rows "$DB" "$t")"
        restored="$(count_rows "$scratch" "$t")"
        printf '%-14s %10s %10s\n' "$t" "$live" "$restored"
        # Alarm only when the LIVE bank has rows the backup lost — an
        # absolute-zero check false-fails on a young bank (no dreams run
        # yet = legitimately 0 facts) and teaches users to distrust a
        # perfectly good backup.
        case "$t" in entries|facts)
            if [ "$live" -gt 0 ] && [ "$restored" -le 0 ]; then any_lost=1; fi ;;
        esac
    done
    docker exec "$CONTAINER" psql -q -U "$DB_USER" -d postgres -c "DROP DATABASE $scratch"
    if [ "$any_lost" -eq 1 ]; then
        echo "live bank has entries/facts but the restored copy has none - investigate before trusting this backup" >&2
        exit 1
    fi

    if [ -n "$STATE_ARCHIVE" ]; then
        # Integrity-check the state tar (listing decompresses everything).
        echo "==> Rehearsing state archive (list-only, nothing written)..."
        state_tmp="/tmp/pl_state_rehearse.tgz"
        docker cp "$STATE_ARCHIVE" "$DAEMON_CONTAINER:$state_tmp"
        if n="$(docker exec "$DAEMON_CONTAINER" sh -c "tar tzf $state_tmp | wc -l")"; then
            docker exec "$DAEMON_CONTAINER" rm -f "$state_tmp" || true
            echo "==> State archive OK ($n entries)."
        else
            docker exec "$DAEMON_CONTAINER" rm -f "$state_tmp" || true
            echo "state archive is unreadable - investigate before trusting it" >&2
            exit 1
        fi
    fi
    echo "==> Rehearsal PASSED: the backup restores cleanly. (Counts differ from live only by writes since the dump.)"
else
    # ── REAL RESTORE ─────────────────────────────────────────────────────
    echo "WARNING: REAL RESTORE: this REPLACES the live bank '$DB' with $BACKUP_FILE" >&2
    echo "==> Safety-dumping the current bank first..."
    "$(dirname "$0")/backup.sh"

    echo "==> Stopping the daemon..."
    docker stop "$DAEMON_CONTAINER" >/dev/null

    echo "==> Dropping + recreating $DB..."
    docker exec "$CONTAINER" psql -q -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $DB"
    docker exec "$CONTAINER" psql -q -U "$DB_USER" -d postgres -c "CREATE DATABASE $DB"
    if ! docker exec "$CONTAINER" sh -c "gunzip -c $tmp | psql -q -v ON_ERROR_STOP=1 -U $DB_USER -d $DB > /dev/null"; then
        echo "RESTORE FAILED mid-way; daemon left stopped. The pre-restore safety dump is in data/backups." >&2
        exit 1
    fi

    if [ -n "$STATE_ARCHIVE" ]; then
        # Replace /data (the state volume) while the daemon is stopped.
        # Runs the daemon's own image (already local, has tar) with
        # --volumes-from, so no volume-name resolution or image pull is
        # needed. The safety backup above already tarred the current state.
        echo "==> Restoring the state volume from $STATE_ARCHIVE..."
        img="$(docker inspect -f '{{.Config.Image}}' "$DAEMON_CONTAINER")"
        dir="$(cd "$(dirname "$STATE_ARCHIVE")" && pwd)"
        name="$(basename "$STATE_ARCHIVE")"
        if ! docker run --rm --entrypoint sh --volumes-from "$DAEMON_CONTAINER" \
            -v "$dir:/pl_backup:ro" "$img" \
            -c "find /data -mindepth 1 -delete && tar xzf /pl_backup/$name -C /data"; then
            echo "STATE RESTORE FAILED; daemon left stopped. The pre-restore safety state tar is in data/backups." >&2
            exit 1
        fi
    fi

    echo "==> Restarting the daemon..."
    docker start "$DAEMON_CONTAINER" >/dev/null
    healthy=""
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 3 http://127.0.0.1:8765/health 2>/dev/null \
            | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
            healthy=1
            break
        fi
        sleep 1.5
    done
    if [ -z "$healthy" ]; then
        echo "daemon did not report healthy after restore - check docker logs $DAEMON_CONTAINER" >&2
        exit 1
    fi
    echo "==> Restore complete. /health reports ok."
fi
