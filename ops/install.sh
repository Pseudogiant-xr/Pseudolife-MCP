#!/usr/bin/env bash
# One-shot idempotent installer for the Pseudolife-MCP stack (issue #13
# tier 2). Everything downstream of Docker: preflight -> volumes -> extractor
# choice -> compose up -> client hooks -> standing instructions ->
# MCP registration -> health. Re-running is safe; re-running with a different
# --extractor is the supported way to switch modes.
#
#   ops/install.sh                                  # interactive
#   ops/install.sh --extractor sidecar --client codex
#   ops/install.sh --extractor sonnet-fallback --claude-md append
#   ops/install.sh --extractor sonnet-only --claude-md skip
#
# Extractor modes (spec: docs/superpowers/specs/
# 2026-07-14-installer-extractor-choice-design.md):
#   sonnet-only      Sonnet only — the 9.4 GB sidecar image is never built
#                    or pulled; dreams pause while the shim is down
#   sonnet-fallback  Claude Sonnet primary via the CLI shim, sidecar as
#                    automatic fallback (needs a logged-in Max-plan CLI)
#   sidecar          bundled local CPU extractor only (stock default; no
#                    Claude Max plan needed)
set -euo pipefail

EXTRACTOR=""
CLIENT=claude
CLAUDE_MD=""
INSTRUCTIONS=""
SHIM_PORT=8082
TRANSPORT=shim

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-2}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --extractor) EXTRACTOR="$2"; shift 2 ;;
        --client) CLIENT="$2"; shift 2 ;;
        --claude-md) CLAUDE_MD="$2"; shift 2 ;;
        --instructions) INSTRUCTIONS="$2"; shift 2 ;;
        --shim-port) SHIM_PORT="$2"; shift 2 ;;
        --transport) TRANSPORT="$2"; shift 2 ;;
        -h|--help)   usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done
case "$EXTRACTOR" in ""|sidecar|sonnet-fallback|sonnet-only) ;; *)
    echo "invalid --extractor '$EXTRACTOR' (sidecar|sonnet-fallback|sonnet-only)" >&2; exit 2 ;;
esac
case "$CLIENT" in claude|codex|both) ;; *)
    echo "invalid --client '$CLIENT' (claude|codex|both)" >&2; exit 2 ;;
esac
case "$CLAUDE_MD" in ""|append|skip) ;; *)
    echo "invalid --claude-md '$CLAUDE_MD' (append|skip)" >&2; exit 2 ;;
esac
case "$INSTRUCTIONS" in ""|append|skip) ;; *)
    echo "invalid --instructions '$INSTRUCTIONS' (append|skip)" >&2; exit 2 ;;
esac
case "$TRANSPORT" in shim|http) ;; *)
    echo "invalid --transport '$TRANSPORT' (shim|http)" >&2; exit 2 ;;
esac

repo="$(cd "$(dirname "$0")/.." && pwd)"
compose_file="$repo/ops/docker-compose.yml"
env_file="$repo/ops/.env"
override_file="$repo/ops/docker-compose.override.yml"
OVERRIDE_MARKER="# pseudolife-mcp install: managed override (sonnet-only) — do not edit; installer rewrites/removes this file"
ENV_BEGIN="# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"
ENV_END="# <<< pseudolife-mcp install <<<"

# ── 1. preflight ───────────────────────────────────────────────────────────
echo "==> Preflight..."
"$repo/ops/preflight.sh" --client "$CLIENT" || {
    echo "Preflight failed — fix the line(s) above and re-run." >&2; exit 1; }

# ── 2. extractor choice (explicit, no default) ─────────────────────────────
if [ -z "$EXTRACTOR" ]; then
    if [ ! -t 0 ]; then
        echo "Non-interactive run: --extractor sidecar|sonnet-fallback|sonnet-only is required." >&2
        exit 2
    fi
    echo ""
    echo "Which dream extractor should consolidate memories?"
    echo "  1) sonnet-only      — lightest: Sonnet only; sidecar never built (~9 GB lighter; needs logged-in Max-plan CLI; dreams pause when the shim is down)"
    echo "  2) sonnet-fallback  — Claude Sonnet primary, sidecar auto-fallback (Max-plan CLI plus the ~9 GB image)"
    echo "  3) sidecar          — bundled local CPU model (no Claude plan needed, works for everyone; ~9 GB image)"
    while [ -z "$EXTRACTOR" ]; do
        printf "Choose 1/2/3: "
        read -r choice
        case "$choice" in
            1) EXTRACTOR=sonnet-only ;;
            2) EXTRACTOR=sonnet-fallback ;;
            3) EXTRACTOR=sidecar ;;
            *) echo "  please answer 1, 2 or 3" ;;
        esac
    done
fi
echo "==> Extractor mode: $EXTRACTOR"

# ── 3. volumes (respect names overridden in an existing ops/.env) ─────────
get_env() { [ -f "$env_file" ] && sed -n "s/^$1=//p" "$env_file" | tail -1 || true; }
bank_vol="$(get_env PSEUDOLIFE_BANK_VOLUME)"; bank_vol="${bank_vol:-pseudolife-mcp-bank}"
state_vol="$(get_env PSEUDOLIFE_STATE_VOLUME)"; state_vol="${state_vol:-pseudolife-mcp-state}"
docker volume create "$bank_vol" >/dev/null
docker volume create "$state_vol" >/dev/null
echo "==> Volumes ready: $bank_vol, $state_vol"

# ── 4. managed env block ───────────────────────────────────────────────────
[ -f "$env_file" ] || cp "$repo/ops/.env.example" "$env_file"
# Drop any previous managed block, then append the new one.
tmp="$(mktemp)"
awk -v b="$ENV_BEGIN" -v e="$ENV_END" '
    $0 == b {skip=1; next} $0 == e {skip=0; next} !skip {print}' \
    "$env_file" > "$tmp" && mv "$tmp" "$env_file"
{
    echo "$ENV_BEGIN"
    case "$EXTRACTOR" in
        sidecar)
            echo "# extractor: sidecar (stock defaults — nothing to set)" ;;
        sonnet-fallback)
            echo "PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$SHIM_PORT/v1"
            echo "PSEUDOLIFE_DREAM_MODEL=extractor"
            echo "PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1"
            echo "PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor"
            echo "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto" ;;
        sonnet-only)
            echo "PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$SHIM_PORT/v1"
            echo "PSEUDOLIFE_DREAM_MODEL=extractor"
            # `primary` (not `auto`): states the single-extractor intent and
            # keeps the auto-without-fallback startup warning silent.
            echo "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=primary" ;;
    esac
    case "$CLIENT" in
        claude) echo "PSEUDOLIFE_WRITER_ID=claude-code" ;;
        codex)  echo "PSEUDOLIFE_WRITER_ID=codex" ;;
        both)   echo "PSEUDOLIFE_WRITER_ID=mcp-client" ;;
    esac
    echo "$ENV_END"
} >> "$env_file"
echo "==> Wrote managed block in ops/.env"

# ── 5. sidecar enable/disable via the compose override ────────────────────
installer_owns_override() {
    [ -f "$override_file" ] && [ "$(head -1 "$override_file")" = "$OVERRIDE_MARKER" ]
}
if [ "$EXTRACTOR" = "sonnet-only" ]; then
    if [ ! -f "$override_file" ] || installer_owns_override; then
        cat > "$override_file" <<EOF
$OVERRIDE_MARKER
# A profiled service is skipped by \`up\` entirely: the extractor image is
# never built or pulled. Re-run ops/install.sh with a sidecar mode to remove.
services:
  pseudolife-extractor:
    profiles: ["disabled"]
EOF
        echo "==> Sidecar disabled via ops/docker-compose.override.yml"
    else
        echo "NOTE: ops/docker-compose.override.yml exists and is not installer-managed."
        echo "      Add this to it yourself to disable the sidecar:"
        echo "        services:"
        echo "          pseudolife-extractor:"
        echo "            profiles: [\"disabled\"]"
    fi
    # Remove a leftover running extractor container (container only — it has
    # no volumes; the image is kept for an easy switch back).
    if docker ps -a --format '{{.Names}}' | grep -qx pseudolife-mcp-extractor; then
        docker rm -f pseudolife-mcp-extractor >/dev/null
        echo "==> Removed the running extractor container"
    fi
else
    if installer_owns_override; then
        rm "$override_file"
        echo "==> Removed installer-managed override (sidecar re-enabled)"
    fi
fi

# ── 6. bring the stack up ──────────────────────────────────────────────────
compose=(--env-file "$env_file" -f "$compose_file")
[ -f "$override_file" ] && compose+=(-f "$override_file")
echo "==> docker compose up -d --build (first build downloads images — grab a coffee)..."
docker compose "${compose[@]}" up -d --build

# ── 7. Sonnet shim autostart (Sonnet modes) ────────────────────────────────
# Best-effort, like the .ps1: a host without systemd --user (macOS, some WSL)
# must not abort the install between `compose up` and the hooks/mcp-add/health
# steps — that strands a running stack that was never wired into Claude Code.
if [ "$EXTRACTOR" != "sidecar" ]; then
    echo "==> Registering the Sonnet shim autostart (systemd --user)..."
    if ! "$repo/ops/install-shim-autostart.sh" --port "$SHIM_PORT"; then
        echo "WARNING: shim autostart registration failed (no systemd --user on this host?)" >&2
        echo "  Re-run later: ops/install-shim-autostart.sh --port $SHIM_PORT" >&2
        echo "  Or start it manually: python evals/sonnet_shim.py --port $SHIM_PORT --system-prompt-file evals/prompts/sonnet_extractor_v1.md" >&2
    fi
fi

# ── 8. session lifecycle hooks ─────────────────────────────────────────────
if [ "$CLIENT" = both ]; then clients="claude codex"; else clients="$CLIENT"; fi
if grep -q "pseudolife-memory@pseudolife-mcp" \
        "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null; then
    CLAUDE_PLUGIN_INSTALLED=1
    if [ "$CLIENT" = claude ] || [ "$CLIENT" = both ]; then
        echo "==> pseudolife-memory Claude Code plugin detected — skipping Claude"
        echo "    hook, CLAUDE.md block, and mcp add (the plugin provides all three)."
    fi
else
    CLAUDE_PLUGIN_INSTALLED=""
fi

briefing_command="docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"
for selected_client in $clients; do
    if [ "$selected_client" = claude ] && [ -n "$CLAUDE_PLUGIN_INSTALLED" ]; then continue; fi
    echo "==> Installing $selected_client session hook..."
    "$repo/ops/install-hook.sh" --client "$selected_client" "" "$briefing_command"
done

# ── 9. CLAUDE.md memory block (consent; never edited without it) ──────────
instruction_choice="${INSTRUCTIONS:-$CLAUDE_MD}"
for selected_client in $clients; do
    if [ "$selected_client" = claude ] && [ -n "$CLAUDE_PLUGIN_INSTALLED" ]; then continue; fi
    if [ "$selected_client" = codex ]; then
        instruction_path="$HOME/.codex/AGENTS.md"
    else
        instruction_path="$HOME/.claude/CLAUDE.md"
    fi
    if grep -q "pseudolife-memory" "$instruction_path" 2>/dev/null; then
        echo "==> Memory block already present in $instruction_path — skipping."
        continue
    fi
    choice="$instruction_choice"
    if [ -z "$choice" ]; then
        if [ -t 0 ]; then
            printf "Append the memory-loop block to %s? [Y/n] " "$instruction_path"
            read -r yn
            case "$yn" in [Nn]*) choice=skip ;; *) choice=append ;; esac
        else
            choice=skip
        fi
    fi
    if [ "$choice" = "append" ]; then
        mkdir -p "$(dirname "$instruction_path")"
        cat "$repo/examples/CLAUDE.memory.md" >> "$instruction_path"
        echo "==> Appended memory block to $instruction_path"
    else
        echo "SKIPPED: MCP server instructions still provide the core memory loop. Optional stronger guidance:"
        echo "  cat $repo/examples/CLAUDE.memory.md >> $instruction_path"
    fi
done

# ── 10. wire into selected MCP clients ─────────────────────────────────────
for selected_client in $clients; do
    if [ "$selected_client" = claude ] && [ -n "$CLAUDE_PLUGIN_INSTALLED" ]; then continue; fi
    if [ "$selected_client" = codex ]; then
        if codex mcp get pseudolife-memory >/dev/null 2>&1; then
            echo "==> MCP server already wired into Codex — skipping."
        else
            codex mcp add pseudolife-memory --url http://127.0.0.1:8765/mcp
            echo "==> Wired into Codex (codex mcp add)."
        fi
    elif claude mcp get pseudolife-memory >/dev/null 2>&1; then
        echo "==> MCP server already wired into Claude Code — skipping."
    elif [ "$TRANSPORT" = "shim" ]; then
        shim_installed=""
        if command -v pipx >/dev/null 2>&1; then
            if pipx list 2>/dev/null | grep -q "package pseudolife-mcp "; then
                pipx upgrade pseudolife-mcp
            else
                pipx install pseudolife-mcp
            fi
            shim_installed=1
        elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            python3 -m pip install --user pseudolife-mcp
            shim_installed=1
        elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            python -m pip install --user pseudolife-mcp
            shim_installed=1
        fi
        if [ -n "$shim_installed" ]; then
            claude mcp remove pseudolife-memory 2>/dev/null || true
            claude mcp add --scope user pseudolife-memory -- pseudolife-mcp
            echo "==> Wired into Claude Code via the pseudolife-mcp shim — per-session identity (required for correct episodes with concurrent sessions)."
        else
            echo "WARNING: neither pipx nor a suitable python3 (>=3.10) was found — cannot install the pseudolife-mcp shim." >&2
            echo "  Without the shim, concurrent Claude Code sessions share one episode identity." >&2
            echo "  Install pipx or python3 >=3.10 and re-run, or pass --transport http to silence this." >&2
            claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
            echo "==> Wired into Claude Code via HTTP (fallback — shim tooling not found)."
        fi
    else
        claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
        echo "==> Wired into Claude Code via HTTP (--transport http)."
    fi
done

# ── 11. health ─────────────────────────────────────────────────────────────
echo "==> Waiting for the daemon to report healthy..."
healthy=""
for _ in $(seq 1 40); do
    if curl -fsS --max-time 3 http://127.0.0.1:8765/health 2>/dev/null \
        | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        healthy=1; break
    fi
    sleep 1.5
done
[ -n "$healthy" ] || {
    echo "WARNING: daemon not healthy yet. Logs: docker logs pseudolife-mcp-daemon" >&2
    exit 1
}
echo "==> Healthy: http://127.0.0.1:8765/health (Console: http://127.0.0.1:8765/ui/)"

# ── 12. per-mode verify hints ──────────────────────────────────────────────
case "$EXTRACTOR" in
    sidecar)
        echo "Verify: memory_dream(action=\"status\") — primary_url should point at pseudolife-extractor:8081." ;;
    sonnet-fallback)
        echo "Verify: memory_dream(action=\"status\") — fallback_url set and primary_healthy: true (shim up)." ;;
    sonnet-only)
        echo "Verify: memory_dream(action=\"status\") — primary_url on :$SHIM_PORT, extractor_mode: primary."
        echo "Note: dreams pause (and retry next sweep) whenever the shim is down or the CLI is logged out." ;;
esac
echo "Done. First session: tell your coding agent to remember something."
