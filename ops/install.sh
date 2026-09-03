#!/usr/bin/env bash
# >>> usage >>>
# One-shot idempotent installer for the Pseudolife-MCP stack (issue #13
# tier 2). Everything downstream of Docker: provider selection -> preflight ->
# extractor choice -> compose up -> client hooks -> standing instructions ->
# MCP registration -> health. Re-running is safe; re-running with a different
# --extractor is the supported way to switch modes.
#
#   ops/install.sh                                  # interactive
#   ops/install.sh --extractor sidecar --client codex
#   ops/install.sh --extractor sonnet-only --client claude,gemini
#   ops/install.sh --extractor sonnet-fallback --instructions append
#   ops/install.sh --extractor codex-fallback --client codex
#
# Providers (--client, comma- or space-separated list):
#   claude    Claude Code    - MCP + SessionStart briefing + per-turn discipline
#   codex     OpenAI Codex   - MCP + SessionStart briefing (opt-in, not Windows)
#   gemini    Gemini CLI     - MCP + standing instructions (no hook system)
#   generic   any MCP agent  - prints paste-ready config + standing block
#   both = claude,codex      all = claude,codex,gemini
#
# Other flags:
#   --instructions append|skip|auto  standing memory block (default: auto -
#                                    prompts only where no briefing hook exists)
#   --claude-md append|skip          compatibility alias for --instructions
#   --agents-file <path>             standing-file target for generic agents
#   --no-art                         plain output (no banner, no color)
#   --model / --shim-port / --transport   as before
#
# Extractor modes (spec: docs/superpowers/specs/
# 2026-07-14-installer-extractor-choice-design.md):
#   sonnet-only      Claude shim only — the ~11.8 GB sidecar image is never built
#                    or pulled; dreams pause while the shim is down
#   sonnet-fallback  Claude Sonnet primary via the CLI shim, sidecar as
#                    automatic fallback (needs a logged-in Max-plan CLI)
#   codex-only       Codex (ChatGPT-plan) shim only — sidecar never built;
#                    extraction quality unmeasured (docs/guide/dreaming.md)
#   codex-fallback   Codex shim primary, sidecar as automatic fallback
#   sidecar          bundled local CPU extractor only (stock default; no
#                    Claude Max plan needed)
# <<< usage <<<
set -euo pipefail

EXTRACTOR=""
MODEL=""
CLIENT=""
CLAUDE_MD=""
INSTRUCTIONS=""
AGENTS_FILE=""
# 0 = auto: 8082 for the Claude shim modes, 8086 for the Codex ones.
SHIM_PORT=0
TRANSPORT=shim
NO_ART=""

usage() {
    sed -n '/^# >>> usage >>>$/,/^# <<< usage <<<$/p' "$0" \
        | sed '1d;$d' | sed 's/^# \{0,1\}//'
    exit "${1:-2}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --extractor) EXTRACTOR="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --client) CLIENT="$2"; shift 2 ;;
        --claude-md) CLAUDE_MD="$2"; shift 2 ;;
        --instructions) INSTRUCTIONS="$2"; shift 2 ;;
        --agents-file) AGENTS_FILE="$2"; shift 2 ;;
        --shim-port) SHIM_PORT="$2"; shift 2 ;;
        --transport) TRANSPORT="$2"; shift 2 ;;
        --no-art) NO_ART=1; shift ;;
        -h|--help)   usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done
case "$EXTRACTOR" in ""|sidecar|sonnet-fallback|sonnet-only|codex-fallback|codex-only) ;; *)
    echo "invalid --extractor '$EXTRACTOR' (sidecar|sonnet-fallback|sonnet-only|codex-fallback|codex-only)" >&2; exit 2 ;;
esac
case "$MODEL" in ""|claude-opus-5|claude-sonnet-5|claude-haiku-4-5|claude-fable-5|gpt-5.6-sol|gpt-5.6-terra|gpt-5.6-luna) ;; *)
    echo "invalid --model '$MODEL' (claude-opus-5|claude-sonnet-5|claude-haiku-4-5|claude-fable-5|gpt-5.6-sol|gpt-5.6-terra|gpt-5.6-luna)" >&2; exit 2 ;;
esac
case "$CLAUDE_MD" in ""|append|skip) ;; *)
    echo "invalid --claude-md '$CLAUDE_MD' (append|skip)" >&2; exit 2 ;;
esac
case "$INSTRUCTIONS" in ""|append|skip|auto) ;; *)
    echo "invalid --instructions '$INSTRUCTIONS' (append|skip|auto)" >&2; exit 2 ;;
esac
case "$TRANSPORT" in shim|http) ;; *)
    echo "invalid --transport '$TRANSPORT' (shim|http)" >&2; exit 2 ;;
esac

repo="$(cd "$(dirname "$0")/.." && pwd)"
compose_file="$repo/ops/docker-compose.yml"
env_file="$repo/ops/.env"
override_file="$repo/ops/docker-compose.override.yml"
OVERRIDE_MARKER="# pseudolife-mcp install: managed override (shim-only extractor) — do not edit; installer rewrites/removes this file"
# Pre-codex installs wrote the mode-specific text; keep recognizing it so a
# mode switch still removes/rewrites their override file.
LEGACY_OVERRIDE_MARKER="# pseudolife-mcp install: managed override (sonnet-only) — do not edit; installer rewrites/removes this file"
ENV_BEGIN="# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"
ENV_END="# <<< pseudolife-mcp install <<<"

# ── presentation helpers ───────────────────────────────────────────────────
# Art and color are interactive sugar only: a real TTY, NO_COLOR unset,
# TERM not dumb, and no --no-art. Escapes are generated (\033), never raw
# ESC bytes — the tracked-tree control-byte guard bans those.
art_ok() {
    [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ] \
        && [ -z "$NO_ART" ]
}
step() {
    if art_ok; then printf '\033[1;36m==>\033[0m %s\n' "$*"
    else echo "==> $*"; fi
}

# >>> banner >>>
show_banner() {
    art_ok || return 0
    printf '\033[36m'
    cat <<'PL_BANNER'
                        :=-:--:=====             -=:
                   ==: #%##@%-*@@@*#%:                :==
                :*@*:=-#%+**%+*:+#+*%+                   @*:
             :+#*@%#%%=*+++%#%%#+--*%*.                     #+:
            +@%%+#%**#%%#*#@@+***%*+%*                       %@+
         -*@#%*%%@*#*=%@+=**@@*+#@@:%%=                []       @*-
        =%*+#*%#*+@@=+@*:-*@%*+**@%#**@                 |        *%=
        @%-*%##*%:%%--#@#@#%#-*=*%*#%%*          []     |        -%@
      :*#@*#%%@@%=+%*@##%*#===***-+#@*   .--o     |     |    []    #*:
     :%+*****%++*%-#%%%##%#=+=+%*#==@%:           |     |     |     +%:
    =#*%%***+***+*%%%++=-#%@==--@%:-#%@           |     |     |    []*#=
   =%@+%*#%%#@%*#@@%*%%: +%%:-*%%%=+#%@   o--.    |     |     |     | @%=
  .@%###=+#%%@@.+*+*@@%: *@%#+= :+%#%@*           |     |     |     |  %@.
  ##%=%@%%%==%*%+=::=*@#%+=-+#%@@+:+%%%        .---------------.       %##
 .%%*###%@@*==+***%%*..*@@=--.*#%++:#*@ o------|  .---------.  |-----o  %%.
 **%#=:+#@@##*#@*:-#@= *=% :#*.%##:-%@%        |  | # # # # |  |        %**
 %+@%-#%%===**#%*--%@+:*%- .@@=.%#==%%= o------|  | # # # # |  |-----o  @+%
 @%%%#*#%%*#::=#%#*+#%**+=*%@%*@%#-:%@@        |  '---------'  |        %%@
 *%%%*+=:#@%#--%@@%##******=::*@@+:=%@+ o------|  o   o   o  o |-----o  %%*
:@@##==**@@*%*%%#.=%*%%%%*##*######%*@.        '---------------'         @@:
%#%+##%%%%=%=*#%%..:*@%@%%*+*%%%%#%%*%            |     |     |     |    %#%
-%###%***+%#=:#%@#*+*#***%###%*+-:=+%%            |     |     |     |    #%-
 %%%*%=**:*%@@#*=********=**-=%*%+:.%%-           |     |     |     |   %%%
 =%@#%#**#*#@@%#*%%#**###%@%=.=@@%=:%%%   o--[]--.|     |     |    []   @%=
  +%###%#*@%%*+#*#*++*#**#%@##*%%###%@%           |     |     |        #%+
    *%*#*#:#**#****+*@%%%@+:*@%***=*#%.          [] .---|     |      *%*
    =%*=+%%##=**#%+==***@%#-:%%%+=:*%#                  |    []      *%=
     %#***:-**+#%%*%%@%:==-=#@@%#++=@@+                []           *#%
     :*%+*##=@@###+++#%*+#+%@**=%#@-*%@                             %*:
       :#%%%#**#-*#%*+*%**=%%***%#*.+@@                           %#:
         :=###*==+#*#%+**%#*%+=+*+::%@%                         #=:
            +@%==***==****=:+****=:*@*:                      %@+
             .=*%@%*+-==*+*++===++=*%=                      *=.
                   =*@@%%%@@@#+#**%@*                 @*=
                       =====+***+==.              ===

                         P S E U D O L I F E   M C P
PL_BANNER
    printf '\033[0m\n'
}
# <<< banner <<<

# >>> capability-matrix >>>
show_matrix() {
    cat <<'PL_MATRIX'
  Agent         MCP          Briefing        Per-turn  Standing file
  ------------  -----------  --------------  --------  ---------------------
  Claude Code   shim / HTTP  hook or plugin  yes       ~/.claude/CLAUDE.md
  OpenAI Codex  shim / HTTP  hook (see *)    no        ~/.codex/AGENTS.md
  Gemini CLI    shim / HTTP  none            no        ~/.gemini/GEMINI.md
  Other agent   stdio/HTTP   none            no        AGENTS.md (your path)

  Every agent also gets, with no files touched: the memory tools, and the
  MCP server `instructions` field - the memory loop delivered by the
  protocol itself.

  * Codex hooks are EXPERIMENTAL and off by default: set
        [features]
        codex_hooks = true
    in ~/.codex/config.toml, then review and trust the hook in /hooks.
    Codex hooks are NOT available on Windows - there the standing AGENTS.md
    block is the briefing, which is why appending it is recommended.
PL_MATRIX
}
# <<< capability-matrix <<<

# >>> generic-snippets >>>
show_generic_snippets() {
    cat <<'PL_SNIPPETS'
  Add pseudolife-memory to your agent's MCP config. Two ready-to-paste
  shapes (pick ONE):

  stdio shim (recommended - per-session identity; needs
  `pip install pseudolife-mcp` or pipx):
    { "mcpServers": { "pseudolife-memory": {
        "command": "pseudolife-mcp",
        "env": { "PSEUDOLIFE_WRITER_ID": "mcp-client",
                 "PSEUDOLIFE_MCP_NO_SPAWN": "1" } } } }

  HTTP (no local install; concurrent sessions share one identity):
    { "mcpServers": { "pseudolife-memory": {
        "type": "http", "url": "http://127.0.0.1:8765/mcp" } } }

  Common config homes: Cursor ~/.cursor/mcp.json - Windsurf
  ~/.codeium/windsurf/mcp_config.json - Zed settings.json
  (context_servers) - Copilot CLI / others: see the tool's MCP docs.
PL_SNIPPETS
}
# <<< generic-snippets <<<

# Expand aliases, validate, dedupe, and emit the canonical provider order.
normalize_clients() {
    raw="$(printf '%s' "$1" | tr ',' ' ')"
    expanded=""
    for tok in $raw; do
        case "$tok" in
            both) expanded="$expanded claude codex" ;;
            all) expanded="$expanded claude codex gemini" ;;
            claude|codex|gemini|generic) expanded="$expanded $tok" ;;
            *) echo "invalid --client '$tok' (claude|codex|gemini|generic|both|all)" >&2
               exit 2 ;;
        esac
    done
    canon=""
    for tok in claude codex gemini generic; do
        case " $expanded " in *" $tok "*) canon="$canon $tok" ;; esac
    done
    printf '%s' "${canon# }"
}

show_banner

# ── 1. provider selection (before preflight, so it checks what you picked) ─
if [ -z "$CLIENT" ]; then
    if [ ! -t 0 ]; then
        CLIENT=claude
    else
        echo ""
        echo "Which coding agents should this install wire up?"
        echo ""
        echo "  1) Claude Code    full parity: MCP + SessionStart briefing + per-turn discipline"
        echo "  2) OpenAI Codex   MCP + SessionStart briefing (opt-in, trust review, not on Windows)"
        echo "  3) Gemini CLI     MCP + standing instructions (Gemini CLI has no hook system)"
        echo "  4) Other MCP agent  Cursor / Windsurf / Zed / Copilot CLI / anything else:"
        echo "                      prints ready-to-paste config, offers the standing block"
        echo ""
        while [ -z "$CLIENT" ]; do
            printf 'Select one or more - e.g. "1 2" or "1,3" (Enter = 1): '
            read -r selection
            [ -z "$selection" ] && selection=1
            picked=""
            bad=""
            for tok in $(printf '%s' "$selection" | tr ',' ' '); do
                case "$tok" in
                    1) picked="$picked claude" ;;
                    2) picked="$picked codex" ;;
                    3) picked="$picked gemini" ;;
                    4) picked="$picked generic" ;;
                    *) bad=1 ;;
                esac
            done
            if [ -n "$bad" ] || [ -z "$picked" ]; then
                echo "  please answer with numbers 1-4 (e.g. \"1 3\")"
            else
                CLIENT="$(printf '%s' "$picked" | tr ' ' ',')"
            fi
        done
    fi
fi
CLIENTS="$(normalize_clients "$CLIENT")"
CLIENT_LIST="$(printf '%s' "$CLIENTS" | tr ' ' ',')"
step "Providers: $CLIENTS"
if [ -t 0 ] && [ -t 1 ]; then
    echo ""
    echo "What each agent gets:"
    echo ""
    show_matrix
    echo ""
fi

# ── 2. preflight ───────────────────────────────────────────────────────────
step "Preflight..."
"$repo/ops/preflight.sh" --client "$CLIENT_LIST" || {
    echo "Preflight failed — fix the line(s) above and re-run." >&2; exit 1; }

# ── 3. extractor choice (explicit, no default) ─────────────────────────────
if [ -z "$EXTRACTOR" ]; then
    if [ ! -t 0 ]; then
        echo "Non-interactive run: --extractor sidecar|sonnet-fallback|sonnet-only|codex-fallback|codex-only is required." >&2
        exit 2
    fi
    echo ""
    echo "Which dream extractor should consolidate memories?"
    echo "  1) sonnet-only      — lightest: Claude shim only; sidecar never built (~11.8 GB lighter; needs logged-in Max-plan CLI; dreams pause when the shim is down)"
    echo "  2) sonnet-fallback  — Claude shim primary, sidecar auto-fallback (Max-plan CLI plus the ~11.8 GB image)"
    echo "  3) sidecar          — bundled local CPU model (no Claude plan needed, works for everyone; ~11.8 GB image)"
    echo "  4) codex-fallback   — Codex (ChatGPT-plan) shim primary, sidecar auto-fallback (ladder-measured at parity with the Claude ceiling — see docs/guide/dreaming.md)"
    echo "  5) codex-only       — Codex shim only; sidecar never built (ladder-measured; dreams pause when the shim is down)"
    while [ -z "$EXTRACTOR" ]; do
        printf "Choose 1/2/3/4/5: "
        read -r choice
        case "$choice" in
            1) EXTRACTOR=sonnet-only ;;
            2) EXTRACTOR=sonnet-fallback ;;
            3) EXTRACTOR=sidecar ;;
            4) EXTRACTOR=codex-fallback ;;
            5) EXTRACTOR=codex-only ;;
            *) echo "  please answer 1-5" ;;
        esac
    done
fi
step "Extractor mode: $EXTRACTOR"
claude_shim_mode=""; codex_shim_mode=""
case "$EXTRACTOR" in sonnet-only|sonnet-fallback) claude_shim_mode=1 ;; esac
case "$EXTRACTOR" in codex-only|codex-fallback) codex_shim_mode=1 ;; esac
if [ "$SHIM_PORT" = 0 ]; then
    if [ -n "$codex_shim_mode" ]; then SHIM_PORT=8086; else SHIM_PORT=8082; fi
fi
# A model from the wrong family would silently serve the shim's launch
# default (the per-request override only honours its own prefixes).
case "$MODEL" in
    claude-*) [ -z "$codex_shim_mode" ] || {
        echo "--model $MODEL does not match extractor mode $EXTRACTOR" >&2; exit 2; } ;;
    gpt-*) [ -z "$claude_shim_mode" ] || {
        echo "--model $MODEL does not match extractor mode $EXTRACTOR" >&2; exit 2; } ;;
esac
# Fail fast on a missing shim CLI: preflight only knows --client, so e.g.
# --extractor codex-fallback --client claude would otherwise sail through
# and die at the autostart stage with the stack already up.
if [ -n "$claude_shim_mode" ] && ! command -v claude >/dev/null 2>&1; then
    echo "claude CLI not found (needed by extractor mode $EXTRACTOR) —" >&2
    echo "  npm install -g @anthropic-ai/claude-code, then log in" >&2
    exit 1
fi
if [ -n "$codex_shim_mode" ] && ! command -v codex >/dev/null 2>&1; then
    echo "codex CLI not found (needed by extractor mode $EXTRACTOR) —" >&2
    echo "  install Codex and run \`codex login\`: https://developers.openai.com/codex/cli/" >&2
    exit 1
fi

# ── 3b. dreamer model choice (Claude-shim modes only) ──────────────────────
# Opus is the recommended default per the 2026-08-02 same-harness comparison
# (evals/results/dreamer-choice-verdict.json). The shim honours per-request
# claude-* names, so this is only the launch default — switchable later from
# the Console's Extractor panel without a reinstall.
if [ -n "$claude_shim_mode" ] && [ -z "$MODEL" ]; then
    if [ -t 0 ]; then
        echo ""
        echo "Which Claude model should extract memories (the 'dreamer')?"
        echo "  1) claude-opus-5    — recommended: best measured extraction quality"
        echo "  2) claude-sonnet-5  — balanced"
        echo "  3) claude-haiku-4-5 — fastest / lightest on plan usage"
        echo "  4) claude-fable-5   — most capable tier"
        while [ -z "$MODEL" ]; do
            printf "Choose 1/2/3/4 (Enter = 1): "
            read -r choice
            case "$choice" in
                ""|1) MODEL=claude-opus-5 ;;
                2) MODEL=claude-sonnet-5 ;;
                3) MODEL=claude-haiku-4-5 ;;
                4) MODEL=claude-fable-5 ;;
                *) echo "  please answer 1, 2, 3 or 4" ;;
            esac
        done
    else
        MODEL=claude-opus-5
    fi
    step "Dreamer model: $MODEL"
fi
# GPT-5.6 menu: no 'recommended' — extraction quality is unmeasured for all
# three (the ladder's terra rung exists to measure it); Terra is only the
# shim's balanced default.
if [ -n "$codex_shim_mode" ] && [ -z "$MODEL" ]; then
    if [ -t 0 ]; then
        echo ""
        echo "Which GPT-5.6 model should extract memories (the 'dreamer')?"
        echo "  1) gpt-5.6-terra — balanced default (extraction quality unmeasured)"
        echo "  2) gpt-5.6-sol   — flagship (unmeasured)"
        echo "  3) gpt-5.6-luna  — fastest / lightest on plan usage (unmeasured)"
        while [ -z "$MODEL" ]; do
            printf "Choose 1/2/3 (Enter = 1): "
            read -r choice
            case "$choice" in
                ""|1) MODEL=gpt-5.6-terra ;;
                2) MODEL=gpt-5.6-sol ;;
                3) MODEL=gpt-5.6-luna ;;
                *) echo "  please answer 1, 2 or 3" ;;
            esac
        done
    else
        MODEL=gpt-5.6-terra
    fi
    step "Dreamer model: $MODEL"
fi

# ── 4. volumes (respect names overridden in an existing ops/.env) ─────────
get_env() { [ -f "$env_file" ] && sed -n "s/^$1=//p" "$env_file" | tail -1 || true; }
bank_vol="$(get_env PSEUDOLIFE_BANK_VOLUME)"; bank_vol="${bank_vol:-pseudolife-mcp-bank}"
state_vol="$(get_env PSEUDOLIFE_STATE_VOLUME)"; state_vol="${state_vol:-pseudolife-mcp-state}"
docker volume create "$bank_vol" >/dev/null
docker volume create "$state_vol" >/dev/null
step "Volumes ready: $bank_vol, $state_vol"

# ── 5. managed env block ───────────────────────────────────────────────────
# Daemon-side writer default: a single first-class provider gets its own id;
# any multi-provider or generic install falls back to the neutral id — the
# per-provider ids then ride each MCP registration's env instead (stage 10).
case "$CLIENTS" in
    claude) WRITER_ID=claude-code ;;
    codex)  WRITER_ID=codex ;;
    gemini) WRITER_ID=gemini ;;
    *)      WRITER_ID=mcp-client ;;
esac
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
        sonnet-fallback|codex-fallback)
            echo "PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$SHIM_PORT/v1"
            echo "PSEUDOLIFE_DREAM_MODEL=extractor"
            echo "PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1"
            echo "PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor"
            echo "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto" ;;
        sonnet-only|codex-only)
            echo "PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$SHIM_PORT/v1"
            echo "PSEUDOLIFE_DREAM_MODEL=extractor"
            # `primary` (not `auto`): states the single-extractor intent and
            # keeps the auto-without-fallback startup warning silent.
            echo "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=primary" ;;
    esac
    echo "PSEUDOLIFE_WRITER_ID=$WRITER_ID"
    echo "$ENV_END"
} >> "$env_file"
step "Wrote managed block in ops/.env"

# ── 6. sidecar enable/disable via the compose override ────────────────────
installer_owns_override() {
    [ -f "$override_file" ] || return 1
    local first
    first="$(head -1 "$override_file")"
    [ "$first" = "$OVERRIDE_MARKER" ] || [ "$first" = "$LEGACY_OVERRIDE_MARKER" ]
}
if [ "$EXTRACTOR" = "sonnet-only" ] || [ "$EXTRACTOR" = "codex-only" ]; then
    if [ ! -f "$override_file" ] || installer_owns_override; then
        cat > "$override_file" <<EOF
$OVERRIDE_MARKER
# A profiled service is skipped by \`up\` entirely: the extractor image is
# never built or pulled. Re-run ops/install.sh with a sidecar mode to remove.
services:
  pseudolife-extractor:
    profiles: ["disabled"]
EOF
        step "Sidecar disabled via ops/docker-compose.override.yml"
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
        step "Removed the running extractor container"
    fi
else
    if installer_owns_override; then
        rm "$override_file"
        step "Removed installer-managed override (sidecar re-enabled)"
    fi
fi

# ── 7. bring the stack up ──────────────────────────────────────────────────
compose=(--env-file "$env_file" -f "$compose_file")
[ -f "$override_file" ] && compose+=(-f "$override_file")
step "docker compose up -d --build (first build downloads images — grab a coffee)..."
docker compose "${compose[@]}" up -d --build

# ── 8. CLI shim autostart (Claude / Codex modes) ───────────────────────────
# Best-effort, like the .ps1: a host without systemd --user (macOS, some WSL)
# must not abort the install between `compose up` and the hooks/mcp-add/health
# steps — that strands a running stack that was never wired into Claude Code.
# A mode switch must tear down the OTHER family's autostart: an abandoned
# shim unit keeps making real CLI calls at every /health refresh, forever,
# on a plan whose owner believes it is turned off.
remove_shim_unit() {
    command -v systemctl >/dev/null 2>&1 || return 0
    if systemctl --user is-enabled "$1" >/dev/null 2>&1 \
            || systemctl --user is-active "$1" >/dev/null 2>&1; then
        systemctl --user disable --now "$1" >/dev/null 2>&1 || true
        step "Removed autostart unit $1"
    fi
}
[ -n "$codex_shim_mode" ] || remove_shim_unit pseudolife-codex-shim.service
[ -n "$claude_shim_mode" ] || remove_shim_unit pseudolife-sonnet-shim.service
if [ -n "$claude_shim_mode" ]; then
    step "Registering the Claude shim autostart (systemd --user)..."
    if ! "$repo/ops/install-shim-autostart.sh" --port "$SHIM_PORT" --model "$MODEL"; then
        echo "WARNING: shim autostart registration failed (no systemd --user on this host?)" >&2
        echo "  Re-run later: ops/install-shim-autostart.sh --port $SHIM_PORT --model $MODEL" >&2
        echo "  Or start it manually: python evals/claude_shim.py --port $SHIM_PORT --model $MODEL --system-prompt-file evals/prompts/sonnet_extractor_v2.md" >&2
    fi
elif [ -n "$codex_shim_mode" ]; then
    step "Registering the Codex shim autostart (systemd --user)..."
    if ! "$repo/ops/install-codex-shim-autostart.sh" --port "$SHIM_PORT" --model "$MODEL"; then
        echo "WARNING: shim autostart registration failed (no systemd --user on this host?)" >&2
        echo "  Re-run later: ops/install-codex-shim-autostart.sh --port $SHIM_PORT --model $MODEL" >&2
        echo "  Or start it manually: python evals/codex_shim.py --port $SHIM_PORT --model $MODEL" >&2
    fi
fi

# ── 9. session lifecycle hooks (hook-capable providers only) ───────────────
# claude: unless the plugin already owns the hooks. codex: hooks exist but
# are experimental and opt-in — install-hook prints the trust-review and
# [features] codex_hooks guidance. gemini/generic: no hook system.
if grep -q "pseudolife-memory@pseudolife-mcp" \
        "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null; then
    CLAUDE_PLUGIN_INSTALLED=1
    case " $CLIENTS " in *" claude "*)
        step "pseudolife-memory Claude Code plugin detected — skipping Claude"
        echo "    hook and CLAUDE.md block (the plugin provides both). The plugin no"
        echo "    longer bundles an MCP server, so the transport is still wired below." ;;
    esac
else
    CLAUDE_PLUGIN_INSTALLED=""
fi

HOOK_CLAUDE=""
HOOK_CODEX=""
briefing_command="docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"
for selected_client in $CLIENTS; do
    case "$selected_client" in claude|codex) ;; *) continue ;; esac
    if [ "$selected_client" = claude ] && [ -n "$CLAUDE_PLUGIN_INSTALLED" ]; then
        HOOK_CLAUDE=plugin
        continue
    fi
    step "Installing $selected_client session hook..."
    "$repo/ops/install-hook.sh" --client "$selected_client" "" "$briefing_command"
    if [ "$selected_client" = claude ]; then HOOK_CLAUDE=hook; else HOOK_CODEX=hook; fi
done

# ── 10. standing memory instructions (consent; never edited without it) ────
# Default is `auto`: skip wherever a session-start briefing already delivers
# the block (claude hook/plugin, codex hook), and offer an interactive append
# where none exists (gemini, generic). `auto` never writes a standing file in
# a non-interactive run; --instructions append behaves exactly as before.
instruction_choice="${INSTRUCTIONS:-${CLAUDE_MD:-auto}}"
INSTR_CLAUDE=""
INSTR_CODEX=""
INSTR_GEMINI=""
INSTR_GENERIC=""

record_instr() {
    case "$1" in
        claude)  INSTR_CLAUDE="$2" ;;
        codex)   INSTR_CODEX="$2" ;;
        gemini)  INSTR_GEMINI="$2" ;;
        generic) INSTR_GENERIC="$2" ;;
    esac
}

append_block() {  # $1 = target path, $2 = provider
    # Presence check HERE, not only at the loop top: the generic prompt
    # resolves its target path after that check ran against an empty
    # --agents-file, and a re-run must never double-append.
    if grep -q "pseudolife-memory" "$1" 2>/dev/null; then
        step "Memory block already present in $1 — skipping."
        record_instr "$2" "present:$1"
        return 0
    fi
    mkdir -p "$(dirname "$1")"
    cat "$repo/examples/CLAUDE.memory.md" >> "$1"
    step "Appended memory block to $1"
    record_instr "$2" "appended:$1"
}

for selected_client in $CLIENTS; do
    if [ "$selected_client" = claude ] && [ -n "$CLAUDE_PLUGIN_INSTALLED" ]; then
        record_instr claude "covered-by-plugin"
        continue
    fi
    case "$selected_client" in
        codex)   instruction_path="$HOME/.codex/AGENTS.md" ;;
        gemini)  instruction_path="$HOME/.gemini/GEMINI.md" ;;
        generic) instruction_path="$AGENTS_FILE" ;;
        *)       instruction_path="$HOME/.claude/CLAUDE.md" ;;
    esac
    if [ -n "$instruction_path" ] \
            && grep -q "pseudolife-memory" "$instruction_path" 2>/dev/null; then
        step "Memory block already present in $instruction_path — skipping."
        record_instr "$selected_client" "present:$instruction_path"
        continue
    fi
    choice="$instruction_choice"
    if [ "$choice" = auto ]; then
        case "$selected_client" in
            claude|codex)
                # A session-start briefing hook already delivers the block —
                # a standing-file copy would double-inject.
                choice=skip ;;
            gemini)
                if [ -t 0 ]; then
                    printf 'Gemini CLI has no hook system - append the standing memory block to %s? [Y/n] ' "$instruction_path"
                    read -r yn
                    case "$yn" in n|N|no|NO) choice=skip ;; *) choice=append ;; esac
                else
                    choice=skip
                fi ;;
            generic)
                if [ -n "$AGENTS_FILE" ]; then
                    choice=append
                elif [ -t 0 ]; then
                    printf 'Append the standing memory block to which file? (Enter = %s, "-" to skip) ' "$HOME/AGENTS.md"
                    read -r answer
                    if [ "$answer" = "-" ]; then
                        choice=skip
                    else
                        instruction_path="${answer:-$HOME/AGENTS.md}"
                        choice=append
                    fi
                else
                    choice=skip
                fi ;;
        esac
    fi
    if [ "$choice" = append ] && [ "$selected_client" = generic ] \
            && [ -z "$instruction_path" ]; then
        echo "NOTE: generic append needs a target — pass --agents-file <path>." >&2
        choice=skip
    fi
    if [ "$choice" = append ]; then
        append_block "$instruction_path" "$selected_client"
    else
        hint_path="${instruction_path:-<your AGENTS.md>}"
        step "Standing memory block not written for $selected_client. To add it:"
        echo "  cat $repo/examples/CLAUDE.memory.md >> $hint_path"
        record_instr "$selected_client" "skipped:$hint_path"
    fi
done

# ── 11. wire into selected MCP clients ─────────────────────────────────────
# Runs even with the plugin installed: the plugin is the hooks/commands layer
# only, so the MCP transport (shim by default) always comes from here.
# The shim install itself is client-agnostic; memoize one attempt so
# multi-provider runs don't run pipx/pip twice. Every install command runs as
# an `if` condition so `set -e` is suspended around it: a failed pipx/pip —
# PEP 668 externally-managed-environment on Ubuntu 24.04 / Debian 12 /
# Fedora 40 / Arch is the common case — leaves SHIM_OK unset so the
# per-client HTTP fallback and its remediation text fire, instead of
# aborting the run after the images are already built (issue #176;
# install.ps1 has always exit-checked these same paths).
SHIM_TRIED=""
SHIM_OK=""
ensure_shim() {
    if [ -n "$SHIM_TRIED" ]; then return 0; fi
    SHIM_TRIED=1
    if command -v pipx >/dev/null 2>&1; then
        if pipx list 2>/dev/null | grep -q "package pseudolife-mcp "; then
            if pipx upgrade pseudolife-mcp; then SHIM_OK=1; fi
        else
            if pipx install pseudolife-mcp; then SHIM_OK=1; fi
        fi
    elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        if python3 -m pip install --user pseudolife-mcp; then SHIM_OK=1; fi
    elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        if python -m pip install --user pseudolife-mcp; then SHIM_OK=1; fi
    fi
    return 0
}

# Two env pairs ride each shim registration: PSEUDOLIFE_WRITER_ID (the shim
# forwards it as the X-PL-Writer header — per-provider write attribution)
# and PSEUDOLIFE_MCP_NO_SPAWN=1 (Docker-tier no-spawn guard, 2026-08-29
# incident). CLI env-flag support is probed, never assumed: a missing flag
# degrades to the flagless form plus a printed manual config edit — never
# to a failed install. HTTP transport cannot carry env, so there the daemon
# default (ops/.env) applies and no shim exists to spawn anything.
cli_env_flag() {  # $1 = cli; echoes the supported env flag, or nothing
    if "$1" mcp add --help 2>/dev/null | grep -q -- '--env'; then
        echo "--env"
    fi
}

MCP_CLAUDE=""
MCP_CODEX=""
MCP_GEMINI=""
for selected_client in $CLIENTS; do
    if [ "$selected_client" = generic ]; then
        echo ""
        step "Other MCP-capable agents — paste-ready config:"
        echo ""
        show_generic_snippets
        echo ""
        continue
    fi
    if [ "$selected_client" = codex ]; then
        if existing_codex=$(codex mcp get pseudolife-memory 2>/dev/null); then
            if [ "$TRANSPORT" = "shim" ] && ! printf '%s' "$existing_codex" | grep -q PSEUDOLIFE_MCP_NO_SPAWN; then
                echo "WARNING: the existing Codex registration lacks PSEUDOLIFE_MCP_NO_SPAWN=1 — its shim can still spawn a fallback daemon that shadows the Docker bank after a reboot." >&2
                echo "  Upgrade it (re-check any custom command first: codex mcp get pseudolife-memory):" >&2
                echo "    codex mcp remove pseudolife-memory" >&2
                echo "    codex mcp add pseudolife-memory --env PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp" >&2
            fi
            step "MCP server already wired into Codex — skipping."
            MCP_CODEX=present
        elif [ "$TRANSPORT" = "shim" ]; then
            ensure_shim
            if [ -n "$SHIM_OK" ]; then
                env_flag="$(cli_env_flag codex)"
                if [ -n "$env_flag" ]; then
                    # Name first, env after — the documented codex form
                    # (an env flag directly before the name risks the
                    # variadic-option parse that breaks claude's CLI).
                    # PSEUDOLIFE_MCP_NO_SPAWN: Docker-tier install — the
                    # shim must wait for the compose container, never spawn
                    # a host-side fallback that can win the port-bind race
                    # against a still-booting Docker and shadow the real
                    # bank (2026-08-29 incident). Flag repeated per pair:
                    # codex's --env takes one KEY=VALUE per occurrence.
                    codex mcp add pseudolife-memory "$env_flag" PSEUDOLIFE_WRITER_ID=codex "$env_flag" PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp
                    MCP_CODEX=shim-env
                else
                    codex mcp add pseudolife-memory -- pseudolife-mcp
                    echo "  (this codex CLI takes no env flag — for per-provider write attribution"
                    echo "   and the Docker-tier no-spawn guard, add to the server's entry in"
                    echo "   ~/.codex/config.toml:"
                    echo "     env = { PSEUDOLIFE_WRITER_ID = \"codex\", PSEUDOLIFE_MCP_NO_SPAWN = \"1\" })"
                    MCP_CODEX=shim
                fi
                step "Wired into Codex via the pseudolife-mcp shim — per-session identity (a Codex session no longer inherits a concurrent Claude session's episode)."
            else
                echo "WARNING: shim unavailable for Codex (see warnings above) — falling back to HTTP." >&2
                echo "  Without the shim, a Codex session running beside a Claude Code session shares its episode identity." >&2
                codex mcp add pseudolife-memory --url http://127.0.0.1:8765/mcp
                step "Wired into Codex (codex mcp add, HTTP fallback)."
                MCP_CODEX=http
            fi
        else
            codex mcp add pseudolife-memory --url http://127.0.0.1:8765/mcp
            step "Wired into Codex (codex mcp add, HTTP)."
            MCP_CODEX=http
        fi
    elif [ "$selected_client" = gemini ]; then
        if gemini mcp list 2>/dev/null | grep -q pseudolife-memory; then
            if [ "$TRANSPORT" = "shim" ]; then
                # `gemini mcp list` cannot show env, so unlike claude/codex
                # there is no way to detect a registration that predates the
                # no-spawn guard — say so instead of staying silent.
                echo "  (if this Gemini registration predates the Docker-tier no-spawn guard, re-add it:" >&2
                echo "   gemini mcp remove pseudolife-memory, then" >&2
                echo "   gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini -e PSEUDOLIFE_MCP_NO_SPAWN=1 pseudolife-memory pseudolife-mcp)" >&2
            fi
            step "MCP server already wired into Gemini CLI — skipping."
            MCP_GEMINI=present
        elif [ "$TRANSPORT" = "shim" ]; then
            ensure_shim
            if [ -n "$SHIM_OK" ]; then
                # Probe gemini's own spelling (`-e, --env`): the command
                # below emits the short form, so a help listing only `-e`
                # must still count as env support (cli_env_flag greps
                # `--env` alone, which is claude/codex's spelling).
                env_flag=""
                if gemini mcp add --help 2>/dev/null | grep -q -- '--env\|-e,'; then
                    env_flag="-e"
                fi
                if [ -n "$env_flag" ]; then
                    # -e repeated per pair (one KEY=VALUE each, verified on
                    # gemini CLI 0.57.0); PSEUDOLIFE_MCP_NO_SPAWN carries
                    # the same Docker-tier no-spawn guard as the claude and
                    # codex registrations (2026-08-29 incident).
                    gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini -e PSEUDOLIFE_MCP_NO_SPAWN=1 pseudolife-memory pseudolife-mcp
                    MCP_GEMINI=shim-env
                else
                    gemini mcp add -s user pseudolife-memory pseudolife-mcp
                    echo "  (this gemini CLI takes no env flag — for per-provider write attribution"
                    echo "   and the Docker-tier no-spawn guard, add \"env\": {\"PSEUDOLIFE_WRITER_ID\":"
                    echo "   \"gemini\", \"PSEUDOLIFE_MCP_NO_SPAWN\": \"1\"} to the server's entry in"
                    echo "   ~/.gemini/settings.json)"
                    MCP_GEMINI=shim
                fi
                step "Wired into Gemini CLI via the pseudolife-mcp shim — per-session identity."
            else
                echo "WARNING: shim unavailable for Gemini CLI (see warnings above) — falling back to HTTP." >&2
                gemini mcp add -s user -t http pseudolife-memory http://127.0.0.1:8765/mcp
                step "Wired into Gemini CLI (gemini mcp add, HTTP fallback)."
                MCP_GEMINI=http
            fi
        else
            gemini mcp add -s user -t http pseudolife-memory http://127.0.0.1:8765/mcp
            step "Wired into Gemini CLI (gemini mcp add, HTTP)."
            MCP_GEMINI=http
        fi
    elif existing_claude=$(claude mcp get pseudolife-memory 2>/dev/null); then
        if [ "$TRANSPORT" = "shim" ] && ! printf '%s' "$existing_claude" | grep -q PSEUDOLIFE_MCP_NO_SPAWN; then
            echo "WARNING: the existing Claude Code registration lacks PSEUDOLIFE_MCP_NO_SPAWN=1 — its shim can still spawn a fallback daemon that shadows the Docker bank after a reboot (2026-08-29 incident)." >&2
            echo "  Upgrade it (re-check any custom command first: claude mcp get pseudolife-memory):" >&2
            echo "    claude mcp remove pseudolife-memory" >&2
            echo "    claude mcp add --scope user pseudolife-memory --env PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp" >&2
        fi
        step "MCP server already wired into Claude Code — skipping."
        MCP_CLAUDE=present
    elif [ "$TRANSPORT" = "shim" ]; then
        ensure_shim
        if [ -n "$SHIM_OK" ]; then
            claude mcp remove pseudolife-memory 2>/dev/null || true
            env_flag="$(cli_env_flag claude)"
            if [ -n "$env_flag" ]; then
                # --env is variadic and must come AFTER the server name:
                # placed earlier it swallows the name as another KEY=value
                # pair and the whole add fails (verified against the claude
                # CLI 2026-08-29; the `--` separator ends the value list).
                # PSEUDOLIFE_MCP_NO_SPAWN: Docker-tier shims wait for the
                # compose daemon instead of spawning a fallback that can
                # shadow the real bank (see the Codex registration above).
                claude mcp add --scope user pseudolife-memory "$env_flag" PSEUDOLIFE_WRITER_ID=claude-code PSEUDOLIFE_MCP_NO_SPAWN=1 -- pseudolife-mcp
                MCP_CLAUDE=shim-env
            else
                claude mcp add --scope user pseudolife-memory -- pseudolife-mcp
                echo "  (this claude CLI takes no env flag — for per-provider write attribution"
                echo "   and the Docker-tier no-spawn guard, add \"env\": {\"PSEUDOLIFE_WRITER_ID\":"
                echo "   \"claude-code\", \"PSEUDOLIFE_MCP_NO_SPAWN\": \"1\"} to the server's entry"
                echo "   in ~/.claude.json)"
                MCP_CLAUDE=shim
            fi
            step "Wired into Claude Code via the pseudolife-mcp shim — per-session identity (required for correct episodes with concurrent sessions)."
        else
            echo "WARNING: the pseudolife-mcp shim is unavailable — tooling missing (pipx / python3 >=3.10) or the install failed (see the pip/pipx output above; on PEP 668 distros 'pip install --user' refuses with externally-managed-environment)." >&2
            echo "  Without the shim, concurrent Claude Code sessions share one episode identity." >&2
            echo "  Install pipx and re-run (pipx sidesteps externally-managed distros), or pass --transport http to silence this." >&2
            claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
            step "Wired into Claude Code via HTTP (fallback — shim tooling not found)."
            MCP_CLAUDE=http
        fi
    else
        claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
        step "Wired into Claude Code via HTTP (--transport http)."
        MCP_CLAUDE=http
    fi
done

# ── 12. health ─────────────────────────────────────────────────────────────
step "Waiting for the daemon to report healthy..."
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
step "Healthy: http://127.0.0.1:8765/health (Console: http://127.0.0.1:8765/ui/)"

# ── 13. per-provider wiring ladder + per-mode verify hints ─────────────────
# [x] wired · [-] deliberately skipped · [!] unavailable, with remediation.
describe_mcp() {  # $1 = state
    case "$1" in
        shim-env) echo "stdio shim (per-provider writer id set)" ;;
        shim)     echo "stdio shim (writer id: daemon default in ops/.env)" ;;
        http)     echo "HTTP (writer id: daemon default in ops/.env)" ;;
        present)  echo "already wired (unchanged)" ;;
        *)        echo "not wired" ;;
    esac
}
describe_instr() {  # $1 = state
    case "$1" in
        appended:*|present:*) echo "[x] Standing file        ${1#*:}" ;;
        covered-by-plugin)    echo "[-] Standing file        plugin briefing covers it" ;;
        skipped:*) echo "[-] Standing file        skipped - append later: cat examples/CLAUDE.memory.md >> ${1#*:}" ;;
        *)         echo "[-] Standing file        skipped" ;;
    esac
}
echo ""
step "What got wired, per agent:"
for selected_client in $CLIENTS; do
    echo ""
    case "$selected_client" in
        claude)
            echo "  Claude Code"
            echo "    [x] MCP transport        $(describe_mcp "$MCP_CLAUDE")"
            echo "    [x] Server instructions  automatic (MCP instructions field)"
            if [ "$HOOK_CLAUDE" = plugin ]; then
                echo "    [x] Session briefing     Claude Code plugin"
                echo "    [x] Per-turn discipline  Claude Code plugin"
            else
                echo "    [x] Session briefing     SessionStart hook -> ~/.claude/settings.json"
                echo "    [x] Per-turn discipline  UserPromptSubmit hook"
            fi
            describe_instr "$INSTR_CLAUDE" | sed 's/^/    /' ;;
        codex)
            echo "  OpenAI Codex"
            echo "    [x] MCP transport        $(describe_mcp "$MCP_CODEX")"
            echo "    [x] Server instructions  automatic (MCP instructions field)"
            if [ "$HOOK_CODEX" = hook ]; then
                echo "    [x] Session briefing     hook written - enable codex_hooks = true, then trust it in /hooks"
            else
                echo "    [!] Session briefing     not installed - re-run: ops/install-hook.sh --client codex"
            fi
            echo "    [!] Per-turn discipline  unavailable - Codex has no per-prompt hook"
            describe_instr "$INSTR_CODEX" | sed 's/^/    /' ;;
        gemini)
            echo "  Gemini CLI"
            echo "    [x] MCP transport        $(describe_mcp "$MCP_GEMINI")"
            echo "    [x] Server instructions  automatic (MCP instructions field)"
            echo "    [!] Session briefing     unavailable - Gemini CLI has no hook system"
            echo "    [!] Per-turn discipline  unavailable"
            describe_instr "$INSTR_GEMINI" | sed 's/^/    /' ;;
        generic)
            echo "  Other MCP agent"
            echo "    [-] MCP transport        paste the printed config into your agent"
            echo "    [x] Server instructions  automatic once connected (MCP instructions field)"
            echo "    [!] Session briefing     unavailable - no hook system to wire"
            echo "    [!] Per-turn discipline  unavailable"
            describe_instr "$INSTR_GENERIC" | sed 's/^/    /' ;;
    esac
done
echo ""
case "$EXTRACTOR" in
    sidecar)
        echo "Verify: memory_dream(action=\"status\") — primary_url should point at pseudolife-extractor:8081." ;;
    sonnet-fallback|codex-fallback)
        echo "Verify: memory_dream(action=\"status\") — fallback_url set and primary_healthy: true (shim up)." ;;
    sonnet-only|codex-only)
        echo "Verify: memory_dream(action=\"status\") — primary_url on :$SHIM_PORT, extractor_mode: primary."
        echo "Note: dreams pause (and retry next sweep) whenever the shim is down or the CLI is logged out." ;;
esac
if [ -n "$codex_shim_mode" ]; then
    echo "Note: Codex-served extraction quality is unmeasured — see the 'OpenAI primary' section of docs/guide/dreaming.md."
fi
echo "Done. First session: tell your coding agent to remember something."
