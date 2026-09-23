#!/usr/bin/env bash
# pg_dump the Pseudolife-MCP database to data/backups/ with 7-day rotation,
# plus a tar of the daemon state volume (ChromaDB reference documents, cortex
# snapshot, graph snapshots) — the bank alone does not cover document_ingest.
# Bash port of ops/backup.ps1 for Linux/macOS hosts.
#
#   ops/backup.sh                  # dump into <repo>/data/backups
#   ops/backup.sh --keep-days 30
#   ops/backup.sh --mirror-keep 2  # cap the mirror at the newest 2 per kind
#   ops/backup.sh --accept-row-drop  # rotate despite a held row-count gate
#
# Runs pg_dump INSIDE the container (no local postgres client needed).
# Each dump gets a pseudolife_manifest-<stamp>.json of its per-table row
# counts; a copy goes into the daemon, where /health reports it as
# last_backup. Row-count gate (see ops/backup.ps1): when entries, facts or
# lessons fell by more than --max-row-drop-percent (default 25) against the
# last manifest that was not itself held, the new dump is kept and nothing
# older is deleted, locally or on the mirror, until --accept-row-drop.
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
MAX_ROW_DROP_PERCENT=25
ACCEPT_ROW_DROP=0

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
        --max-row-drop-percent) MAX_ROW_DROP_PERCENT="$2"; shift 2 ;;
        --accept-row-drop) ACCEPT_ROW_DROP=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
case "$MIRROR_KEEP" in
    ''|*[!0-9]*) echo "--mirror-keep must be a non-negative integer" >&2; exit 2 ;;
esac
case "$MAX_ROW_DROP_PERCENT" in
    ''|*[!0-9]*) echo "--max-row-drop-percent must be an integer 0-100" >&2; exit 2 ;;
esac
[ "$MAX_ROW_DROP_PERCENT" -le 100 ] \
    || { echo "--max-row-drop-percent must be an integer 0-100" >&2; exit 2; }

repo="$(cd "$(dirname "$0")/.." && pwd)"
[ -n "$OUT_DIR" ] || OUT_DIR="$repo/data/backups"
mkdir -p "$OUT_DIR"

stamp="$(date +%Y%m%d-%H%M%S)"
out="$OUT_DIR/pseudolife_memory-$stamp.sql.gz"

echo "Dumping $DB from container $CONTAINER -> $out"
# Dump + compress INSIDE the container, then copy the artifact out (mirrors
# the .ps1: no binary piping through the host shell). -Fc would be smaller
# but plain+gzip is trivially restorable with psql.
#
# pg_dump writes the gzip ITSELF (-Z9, plain format) rather than being piped
# into a separate gzip: the container's POSIX sh has no `pipefail`, so a
# pipeline hands `docker exec` the LAST command's status, and a pg_dump that
# died partway still produced a non-empty, perfectly valid gzip of a
# truncated dump — which passed the only other guard here, a zero-length
# check. One process means the status really is pg_dump's, and (unlike
# dumping to a temp file first) it costs no uncompressed scratch space
# inside the container.
tmp="/tmp/pl_backup-$stamp.sql.gz"
if ! docker exec "$CONTAINER" sh -c "pg_dump -U $DB_USER -d $DB -Z9 > $tmp"; then
    echo "pg_dump failed inside container $CONTAINER" >&2
    exit 1
fi
# Land on a .part name and promote only once the artifact verifies: a
# rejected dump must never sit in the backup folder looking like the newest
# good backup (restore.sh and the rotation below both glob *.sql.gz).
part="$out.part"
docker cp "$CONTAINER:$tmp" "$part"
docker exec "$CONTAINER" rm -f "$tmp"
if [ ! -s "$part" ]; then
    echo "backup artifact missing or empty: $part" >&2
    exit 1
fi
# PostgreSQL's own end-of-dump marker, read back from the HOST artifact, so
# one test covers three failure modes: the gzip decompresses, pg_dump ran to
# completion, and `docker cp` did not truncate it. The same pass counts each
# table's COPY rows for the manifest: plain-format COPY data holds one row
# per line (embedded newlines are escaped), ending at a line that is exactly
# "\.". The marker only counts outside COPY data, where a stored memory that
# happens to quote it cannot pass for the end of the dump; pg_dump writes it
# last, so a dump cut off inside COPY data never reaches it. Output: one
# "table <name> <rows>" line per table, then "complete" last if it was.
# The `|| true` keeps the script's `pipefail` from turning gzip's expected
# error on a corrupt artifact into a bare exit with no explanation.
dump_scan="$(gzip -dc "$part" 2>/dev/null | awk '
    table != "" { if ($0 == "\\.") { print "table", table, rows; table = "" } else rows++; next }
    /^COPY [^ ]+ .*FROM stdin;$/ { table = $2; rows = 0; next }
    $0 == "-- PostgreSQL database dump complete" { complete = 1 }
    END { if (complete) print "complete" }
' || true)"
if [ "${dump_scan##*$'\n'}" != "complete" ]; then
    # Kept, not deleted: a truncated dump is the evidence for whatever
    # went wrong, and the previous good backups sit untouched beside it.
    echo "backup is INCOMPLETE - the dump is truncated (end-of-dump marker missing). Nothing was promoted; the rejected artifact is at $part" >&2
    exit 1
fi
mv "$part" "$out"

# Row-count gate: compare against the newest manifest that was not itself
# held, in the out-dir OR the mirror (see ops/backup.ps1 for why both, and
# why held runs are skipped). Manifests are read with grep, not a JSON
# parser: both scripts write "key": value pairs this tolerates.
manifest_out="$OUT_DIR/pseudolife_manifest-$stamp.json"
manifest_rotation() { # $1 = manifest file
    grep -o -m1 '"rotation": *"[a-z]*"' "$1" | sed 's/.*"\([a-z]*\)"$/\1/' || true
}
manifest_count() { # $1 = manifest file, $2 = table; prints nothing if absent
    grep -o -m1 "\"$2\": *[0-9]*" "$1" | grep -o '[0-9]*$' || true
}
baseline=""
while IFS=$'\t' read -r _ f; do
    if [ "$(manifest_rotation "$f")" != "held" ]; then baseline="$f"; break; fi
done < <(for d in "$OUT_DIR" ${MIRROR_DIR:+"$MIRROR_DIR"}; do
             [ -d "$d" ] || continue
             for f in "$d"/pseudolife_manifest-*.json; do
                 if [ -f "$f" ]; then printf '%s\t%s\n' "$(basename "$f")" "$f"; fi
             done
         done | sort -r)
drops=""
if [ -n "$baseline" ]; then
    for t in public.entries public.facts public.lessons; do
        before="$(manifest_count "$baseline" "$t")"
        [ -n "$before" ] || continue
        # A gated table missing from the dump is a drop to zero.
        after="$(printf '%s\n' "$dump_scan" \
            | awk -v t="$t" '$1 == "table" && $2 == t { n = $3 } END { print n + 0 }')"
        if [ "$before" -gt 0 ] \
            && [ $(( (before - after) * 100 )) -gt $(( before * MAX_ROW_DROP_PERCENT )) ]; then
            drops="${drops:+$drops; }$t $before -> $after (-$(( (before - after) * 100 / before ))%)"
        fi
    done
fi
if [ -z "$drops" ]; then rotation=ok
elif [ "$ACCEPT_ROW_DROP" -eq 1 ]; then rotation=accepted
else rotation=held; fi
note=""
baseline_json=null
if [ -n "$baseline" ]; then baseline_json="\"$(basename "$baseline")\""; fi
if [ -n "$drops" ]; then
    note="fell by more than $MAX_ROW_DROP_PERCENT% since $(basename "$baseline"): $drops"
fi
{
    printf '{\n'
    printf '  "dump": "%s",\n' "$(basename "$out")"
    printf '  "created_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "rotation": "%s",\n' "$rotation"
    printf '  "baseline": %s,\n' "$baseline_json"
    printf '  "note": "%s",\n' "$note"
    printf '  "tables": {\n'
    printf '%s\n' "$dump_scan" | awk '$1 == "table" {
        printf "%s    \"%s\": %s", sep, $2, $3; sep = ",\n" } END { if (sep != "") printf "\n" }'
    printf '  }\n}\n'
} > "$manifest_out"
if [ "$rotation" = held ]; then
    echo "WARNING: ROW-COUNT GATE: $note. Rotation and mirror pruning are HELD: nothing older is deleted and the new dump is kept. If the drop is intended, re-run with --accept-row-drop; until then every backup holds." >&2
elif [ "$rotation" = accepted ]; then
    echo "WARNING: ROW-COUNT GATE: $note. Accepted (--accept-row-drop): rotating, and this backup is the new baseline." >&2
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

# Rotation. Rejected .part artifacts age out on the same window: they are
# kept as evidence (see the completeness check above), but a dump that keeps
# failing must not pile up full-size files forever on a machine that is
# already having a bad day. A held row-count gate skips it all.
if [ "$rotation" != held ]; then
    find "$OUT_DIR" -maxdepth 1 \( -name 'pseudolife_memory-*.sql.gz' -o -name 'pseudolife_memory-*.sql.gz.part' -o -name 'pseudolife_state-*.tgz' -o -name 'pseudolife_manifest-*.json' \) -mtime +"$KEEP_DAYS" -delete
fi

# Off-disk mirror (opt-in; --keep-days retention, or newest-N per kind with
# --mirror-keep). Mirrors every artifact: the DB dump, the state tar, and the
# manifest (last, so a manifest never lands without its dump). A held gate
# still copies — it only skips the pruning.
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
        mirror_one "$manifest_out" || mirror_ok=0
        if [ "$rotation" != held ]; then
            for pat in 'pseudolife_memory-.*\.sql\.gz' 'pseudolife_state-.*\.tgz' 'pseudolife_manifest-.*\.json'; do
                if [ "$MIRROR_KEEP" -gt 0 ]; then
                    ls -1 "$MIRROR_DIR" | grep -E "^$pat$" | sort -r \
                        | tail -n +$((MIRROR_KEEP + 1)) | while IFS= read -r f; do
                            rm -f "$MIRROR_DIR/$f"
                        done || true
                fi
            done
            if [ "$MIRROR_KEEP" -eq 0 ]; then
                find "$MIRROR_DIR" -maxdepth 1 \( -name 'pseudolife_memory-*.sql.gz' -o -name 'pseudolife_state-*.tgz' -o -name 'pseudolife_manifest-*.json' \) -mtime +"$KEEP_DAYS" -delete
            fi
        fi
    else
        mirror_ok=0
    fi
    [ "$mirror_ok" -eq 1 ] || echo "WARNING: backup mirror failed (primary backup is safe)" >&2
fi

# Record this backup inside the daemon, where /health reports it as
# last_backup (see ops/backup.ps1). Warn, never fail — same reasoning as
# the state tar.
if docker cp "$manifest_out" "$DAEMON_CONTAINER:/data/last-backup.json"; then
    echo "Recorded in the daemon (/health last_backup)"
else
    echo "WARNING: could not record the backup in the daemon's /data/last-backup.json (/health last_backup will read older than it is)" >&2
fi

if [ "$rotation" = held ]; then
    echo "WARNING: Backup complete, but rotation is HELD by the row-count gate (see above). Nothing was deleted." >&2
else
    echo "Backup complete. Retained last $KEEP_DAYS days in $OUT_DIR"
fi
