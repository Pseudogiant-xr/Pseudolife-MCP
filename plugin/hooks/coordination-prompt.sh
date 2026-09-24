#!/usr/bin/env bash
# Local, change-only coordination alert. Never calls the daemon.
INPUT=$(cat 2>/dev/null)
# The payload also carries the user's prompt, which may quote the literal
# "session_id" — and Codex serialises the prompt field before session_id.
# Only a top-level key counts: preceded by { or , (plus whitespace), never
# by a backslash, which is how the same characters look inside the escaped
# prompt string. Then accept only an id-shaped value.
SID=$(printf '%s' "$INPUT" | grep -o '[{,][[:space:]]*"session_id"[[:space:]]*:[[:space:]]*"[^"\\]*"' 2>/dev/null |
      head -1 | sed 's/.*:[[:space:]]*"\([^"]*\)"$/\1/')
case "$SID" in ''|*[!A-Za-z0-9._-]*) SID="" ;; esac
[ "${#SID}" -le 128 ] || SID=""
# Codex desktop can start hooks without HOME in the environment.
DIGEST_DIR="${PSEUDOLIFE_DIGEST_DIR:-${HOME:-${USERPROFILE:-~}}/.pseudolife-mcp/digests}"
if [ -n "$SID" ] && [ -d "$DIGEST_DIR" ]; then
    # A pipeline's status is its last command's, so the fallback lives
    # inside the group, not after it.
    KEY=$(printf '%s' "$SID" | { sha256sum 2>/dev/null || shasum -a 256 2>/dev/null; } | cut -c1-64)
    # The record is followed only while it is confirmed for this very
    # session (line 2). The env id equals the stdin id only in a hook Claude
    # Code started for this session; a host run from a Claude Bash command
    # inherits both variables from the outer session and must not follow it.
    case "${CLAUDE_PID:-}" in
        ''|*[!0-9]*) ;;
        *)
            RECORD="$DIGEST_DIR/claude-$CLAUDE_PID.host"
            if [ -n "$KEY" ] && [ "${CLAUDE_CODE_SESSION_ID:-}" = "$SID" ] &&
                    [ -f "$RECORD" ] && [ ! -L "$RECORD" ]; then
                SHIM="" FOR=""
                { IFS= read -r SHIM; IFS= read -r FOR; } 2>/dev/null < "$RECORD"
                case "$SHIM" in ''|*[!0-9a-f]*) SHIM="" ;; esac
                if [ "${#SHIM}" -eq 64 ] && [ "$FOR" = "$KEY" ]; then
                    KEY="$SHIM"
                fi
            fi
            ;;
    esac
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
