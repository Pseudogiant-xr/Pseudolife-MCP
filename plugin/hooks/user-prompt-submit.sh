#!/usr/bin/env bash
# Pseudolife-MCP UserPromptSubmit hook — stdout becomes turn context.
# Static by design: this fires on EVERY user turn, so no daemon round-trip
# and no network dependency. The one-shot SessionStart briefing loses
# salience over a long session (2026-08-25 finding); a per-turn line keeps
# the memory loop mechanical, and codifies recall-before-review
# (2026-08-28): search the bank first, then compare it against the files.
# Must never block a turn (always exits 0).
#
# Coordination digest (2026-09-21): the shim's adapter keeps a small file
# per host session under PSEUDOLIFE_DIGEST_DIR (default
# ~/.pseudolife-mcp/digests), named by the SHA-256 of the session id this
# hook receives on stdin. Line 1 is a watermark that moves only when the
# digest text changed; the rest is the text. It prints here once, when the
# watermark is past the shared .seen marker (the tool-result hint advances
# the same marker), so a quiet turn adds nothing. Still file reads only.

echo "Memory (PseudoLife) mid-session discipline: before reviewing code, docs, or a PR -> memory_search + memory_lesson_search the target area FIRST, then compare memory against the files and correct drift both ways (fix stale memory via memory_fact_set + memory_outcome; treat memory-vs-file mismatches as review findings). Status or in-progress questions -> memory_search (include sources: status) before or alongside git. Starting work in a new area -> memory_search + memory_lesson_search first. Launching or finishing long-running work -> memory_store a status entry. Outcome landed -> memory_outcome with used_ids."

INPUT=$(cat 2>/dev/null)
# The payload also carries the user's prompt, which may quote the literal
# "session_id": take the first occurrence and accept only an id-shaped value.
SID=$(printf '%s' "$INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null |
      head -1 | sed 's/.*:[[:space:]]*"\([^"]*\)"$/\1/')
case "$SID" in ''|*[!A-Za-z0-9._-]*) SID="" ;; esac
[ "${#SID}" -le 128 ] || SID=""
DIGEST_DIR="${PSEUDOLIFE_DIGEST_DIR:-${HOME}/.pseudolife-mcp/digests}"
if [ -n "$SID" ] && [ -d "$DIGEST_DIR" ]; then
    # A pipeline's status is its last command's, so the fallback lives
    # inside the group, not after it.
    KEY=$(printf '%s' "$SID" | { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | cut -c1-64)
    FILE="$DIGEST_DIR/$KEY.txt"
    if [ -n "$KEY" ] && [ -f "$FILE" ] && [ ! -L "$FILE" ]; then
        SEEN="$DIGEST_DIR/$KEY.seen"
        # One read, one snapshot: the writer replaces the file atomically.
        CONTENT=$(tr -d '\r' < "$FILE")
        WATERMARK=${CONTENT%%$'\n'*}
        LAST=$(cat "$SEEN" 2>/dev/null | tr -d '\r\n ')
        case "$WATERMARK" in ''|*[!0-9]*) WATERMARK="" ;; esac
        case "$LAST" in ''|*[!0-9]*) LAST=0 ;; esac
        PRINTED=0
        if [ -n "$WATERMARK" ] && [ "$WATERMARK" -gt "$LAST" ]; then
            BODY=""
            case "$CONTENT" in *$'\n'*) BODY=${CONTENT#*$'\n'} ;; esac
            if [ -n "$BODY" ]; then
                printf '%s\n' "$BODY"
                PRINTED=$(( ${#BODY} + 1 ))
            fi
            printf '%s\n' "$WATERMARK" > "$SEEN" 2>/dev/null
        fi
        # Measurement ledger: when, which session, which watermark, bytes added.
        LEDGER="$DIGEST_DIR/ledger.log"
        if [ -f "$LEDGER" ] && [ "$(wc -c < "$LEDGER" 2>/dev/null || echo 0)" -gt 1048576 ]; then
            tail -n 2000 "$LEDGER" > "$LEDGER.tmp" 2>/dev/null && mv "$LEDGER.tmp" "$LEDGER" 2>/dev/null
        fi
        printf '%s\thook\t%s\t%s\t%s\n' "$(date +%s)" "${KEY:0:8}" "${WATERMARK:-0}" "$PRINTED" >> "$LEDGER" 2>/dev/null
    fi
fi

exit 0
