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
#   ops/install.sh --daemon-url http://100.64.0.2:8765 \
#       --token-file ~/.pseudolife-mcp/claude-code.token --read-token --client claude
#
# Providers (--client, comma- or space-separated list):
#   claude    Claude Code    - MCP + SessionStart briefing + per-turn discipline
#   claude-desktop  Claude Desktop - MCP via claude_desktop_config.json (no hooks)
#   codex     OpenAI Codex   - MCP + hooks selected with --codex-hooks
#   gemini    Gemini CLI     - MCP + standing instructions (no hook system)
#   generic   any MCP agent  - prints paste-ready config + standing block
#   both = claude,codex      all = claude,codex,gemini
#
# Other flags:
#   --codex-hooks auto|manual|plugin|skip  hook owner (default: auto)
#   --codex-hook-trust ask|yes|no     approve PseudoLife hooks (default: ask)
#   --claude-plugin auto|skip        install the Claude Code plugin (hooks +
#                                    commands) when claude is a client (default: auto)
#   --claude-legacy-hooks ask|remove|keep  with the plugin installed, remove the
#                                    hooks an earlier install wrote to
#                                    ~/.claude/settings.json (default: ask -
#                                    prompts; unattended runs keep them)
#   --instructions append|skip|auto  standing memory block (default: auto -
#                                    prompts only where no briefing hook exists)
#   --claude-md append|skip          compatibility alias for --instructions
#   --agents-file <path>             standing-file target for generic agents
#   --no-art                         plain output (no banner, no color)
#   --no-token                       open-loopback install: mint no bearer
#                                    token, so the agent board stays off
#   --model / --shim-port / --transport   as before
#
# Client-only install (this machine runs no daemon; one runs elsewhere,
# typically reached over a tailnet): no Docker, volumes, ops/.env, token
# minting or local daemon. Shim, registrations, plugin, hooks and standing
# instructions are set up as usual, aimed at the remote daemon.
#   --daemon-url <url>               the daemon's URL (default: the
#                                    PSEUDOLIFE_MCP_DAEMON_URL environment
#                                    variable). A host other than 127.0.0.1,
#                                    localhost or ::1 implies --client-only
#   --token-file <path>              client-only: an owner-only file (chmod
#                                    600) holding the daemon's bearer token
#                                    (default: PSEUDOLIFE_MCP_TOKEN_FILE);
#                                    the installer never mints one here
#   --client-only                    wire clients to --daemon-url only; with
#                                    a loopback URL, for an SSH tunnel
#   --read-token                     client-only: create the --token-file (it
#                                    must not exist yet) from the token typed,
#                                    unechoed, or piped on stdin
# One token file is one principal: every client a run wires shares it. For
# per-client attribution on the board, run the installer once per client,
# each run with that client's own token file.
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
CODEX_HOOKS=auto
CODEX_HOOK_TRUST=ask
CLAUDE_PLUGIN=auto
CLAUDE_LEGACY_HOOKS=ask
CLAUDE_MD=""
INSTRUCTIONS=""
AGENTS_FILE=""
# 0 = auto: 8082 for the Claude shim modes, 8086 for the Codex ones.
SHIM_PORT=0
TRANSPORT=shim
NO_ART=""
NO_TOKEN=""
DAEMON_URL=""
TOKEN_FILE=""
CLIENT_ONLY=""
READ_TOKEN=""

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
        --codex-hooks) CODEX_HOOKS="$2"; shift 2 ;;
        --codex-hook-trust) CODEX_HOOK_TRUST="$2"; shift 2 ;;
        --claude-plugin) CLAUDE_PLUGIN="$2"; shift 2 ;;
        --claude-legacy-hooks) CLAUDE_LEGACY_HOOKS="$2"; shift 2 ;;
        --claude-md) CLAUDE_MD="$2"; shift 2 ;;
        --instructions) INSTRUCTIONS="$2"; shift 2 ;;
        --agents-file) AGENTS_FILE="$2"; shift 2 ;;
        --shim-port) SHIM_PORT="$2"; shift 2 ;;
        --transport) TRANSPORT="$2"; shift 2 ;;
        --no-art) NO_ART=1; shift ;;
        --no-token) NO_TOKEN=1; shift ;;
        --daemon-url) DAEMON_URL="$2"; shift 2 ;;
        --token-file) TOKEN_FILE="$2"; shift 2 ;;
        --client-only) CLIENT_ONLY=1; shift ;;
        --read-token) READ_TOKEN=1; shift ;;
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
case "$CODEX_HOOKS" in auto|manual|plugin|skip) ;; *)
    echo "invalid --codex-hooks '$CODEX_HOOKS' (auto|manual|plugin|skip)" >&2; exit 2 ;;
esac
case "$CODEX_HOOK_TRUST" in ask|yes|no) ;; *)
    echo "invalid --codex-hook-trust '$CODEX_HOOK_TRUST' (ask|yes|no)" >&2; exit 2 ;;
esac
case "$CLAUDE_PLUGIN" in auto|skip) ;; *)
    echo "invalid --claude-plugin '$CLAUDE_PLUGIN' (auto|skip)" >&2; exit 2 ;;
esac
case "$CLAUDE_LEGACY_HOOKS" in ask|remove|keep) ;; *)
    echo "invalid --claude-legacy-hooks '$CLAUDE_LEGACY_HOOKS' (ask|remove|keep)" >&2; exit 2 ;;
esac

# >>> client-only mode >>>
# A client-only install wires this machine's clients to a daemon that runs
# elsewhere (typically over a tailnet). The URL comes from --daemon-url,
# else from PSEUDOLIFE_MCP_DAEMON_URL in the environment; a host other than
# loopback implies --client-only, since no local daemon answers there.
daemon_url_host() {  # $1 = URL; echoes its host, lowercased, without brackets
    url_authority="${1#*://}"
    url_authority="${url_authority%%/*}"
    url_authority="${url_authority##*@}"
    case "$url_authority" in
        \[*) url_host="${url_authority#\[}"; url_host="${url_host%%]*}" ;;
        *) url_host="${url_authority%%:*}" ;;
    esac
    printf '%s' "$url_host" | tr '[:upper:]' '[:lower:]'
}
is_loopback_host() {  # the shim's rule (daemon_url._is_loopback_url)
    case "$1" in localhost|::1) return 0 ;; esac
    printf '%s' "$1" | grep -Eq '^127\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'
}
CLIENT_ONLY_VIA=""
daemon_url_from_env=""
if [ -z "$DAEMON_URL" ] && [ -n "${PSEUDOLIFE_MCP_DAEMON_URL:-}" ]; then
    DAEMON_URL="$PSEUDOLIFE_MCP_DAEMON_URL"
    daemon_url_from_env=1
fi
DAEMON_URL="${DAEMON_URL%/}"
if [ -n "$DAEMON_URL" ]; then
    # An origin only, as the shim requires (daemon_url._validated_daemon_url):
    # http(s), a host and an optional numeric port; no credentials, path,
    # query, fragment or whitespace. The URL is not echoed: it may hold a
    # password.
    url_authority="${DAEMON_URL#*://}"
    url_port=""
    case "$url_authority" in
        \[*\]:*) url_port="${url_authority##*]:}" ;;
        \[*) ;;
        *:*) url_port="${url_authority#*:}" ;;
    esac
    url_ok=1
    case "$DAEMON_URL" in http://?*|https://?*) ;; *) url_ok="" ;; esac
    case "$url_authority" in *[/?#@[:space:]]*) url_ok="" ;; esac
    case "$url_port" in *[!0-9]*) url_ok="" ;; esac
    [ -n "$(daemon_url_host "$DAEMON_URL")" ] || url_ok=""
    if [ -z "$url_ok" ]; then
        echo "invalid daemon URL: use an http(s) origin without credentials, a path, query, or fragment (http://<host>:<port> or https://<host>)" >&2
        exit 2
    fi
    if [ -z "$CLIENT_ONLY" ] && ! is_loopback_host "$(daemon_url_host "$DAEMON_URL")"; then
        CLIENT_ONLY=1
        [ -z "$daemon_url_from_env" ] || CLIENT_ONLY_VIA=" (implied by PSEUDOLIFE_MCP_DAEMON_URL in the environment; unset it for a local install)"
    fi
fi
if [ -n "$CLIENT_ONLY" ]; then
    if [ -z "$DAEMON_URL" ]; then
        echo "--client-only needs the daemon's URL: pass --daemon-url http://<host>:8765, or set PSEUDOLIFE_MCP_DAEMON_URL" >&2
        exit 2
    fi
    local_flags=""
    [ -z "$EXTRACTOR" ] || local_flags="$local_flags --extractor"
    [ -z "$MODEL" ] || local_flags="$local_flags --model"
    [ "$SHIM_PORT" = 0 ] || local_flags="$local_flags --shim-port"
    [ -z "$NO_TOKEN" ] || local_flags="$local_flags --no-token"
    [ "$TRANSPORT" = shim ] || local_flags="$local_flags --transport http"
    if [ -n "$local_flags" ]; then
        echo "client-only install: the daemon runs elsewhere, so these flags do not apply:$local_flags (a local daemon's settings, or an HTTP registration, which cannot carry the token file)$CLIENT_ONLY_VIA" >&2
        exit 2
    fi
elif [ -n "$TOKEN_FILE" ] || [ -n "$READ_TOKEN" ]; then
    echo "client-only install: --token-file and --read-token name a remote daemon's token, and a local install keeps its token in ops/.env. Add --client-only --daemon-url <url>, or drop them" >&2
    exit 2
fi
# <<< client-only mode <<<

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
  Agent           MCP          Briefing        Per-turn  Standing file
  --------------  -----------  --------------  --------  ---------------------
  Claude Code     shim / HTTP  hook or plugin  yes       ~/.claude/CLAUDE.md
  Claude Desktop  shim         none            no        none
  OpenAI Codex    shim / HTTP  hook (see *)    yes       ~/.codex/AGENTS.md
  Gemini CLI      shim / HTTP  none            no        ~/.gemini/GEMINI.md
  Other agent     stdio/HTTP   none            no        AGENTS.md (your path)

  Every agent also gets, with no files touched: the memory tools, and the
  MCP server `instructions` field - the memory loop delivered by the
  protocol itself.

  * Current Codex runtimes enable hooks by default, including Windows.
    Review and trust new or changed hooks in /hooks before they run.
    Support depends on the runtime and policy, not the selected model.
    Use a standing AGENTS.md block if hooks are disabled or unavailable.
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

# >>> client-only notes >>>
show_client_only_notes() {
    cat <<'PL_CLIENT_ONLY'
  This machine is a client only: the daemon, its bank and its dream
  extractor run on the other host, and nothing here starts, stops or
  upgrades them.
  - A new token there: write it into the token file here. The shims and
    the Claude Code hooks read that file on every call. Codex keeps its
    own copy in ~/.codex/pseudolife/token: write it there too.
  - A daemon upgrade there: bring this checkout to the same release and
    re-run this installer, so the shim matches the daemon.
PL_CLIENT_ONLY
}
# <<< client-only notes <<<

# Expand aliases, validate, dedupe, and emit the canonical provider order.
normalize_clients() {
    raw="$(printf '%s' "$1" | tr ',' ' ')"
    expanded=""
    for tok in $raw; do
        case "$tok" in
            both) expanded="$expanded claude codex" ;;
            all) expanded="$expanded claude codex gemini" ;;
            claude|claude-desktop|codex|gemini|generic) expanded="$expanded $tok" ;;
            *) echo "invalid --client '$tok' (claude|claude-desktop|codex|gemini|generic|both|all)" >&2
               exit 2 ;;
        esac
    done
    canon=""
    for tok in claude claude-desktop codex gemini generic; do
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
        echo "  2) OpenAI Codex   MCP + session and per-turn hooks (trust review)"
        echo "  3) Gemini CLI     MCP + standing instructions (Gemini CLI has no hook system)"
        echo "  4) Other MCP agent  Cursor / Windsurf / Zed / Copilot CLI / anything else:"
        echo "                      prints ready-to-paste config, offers the standing block"
        echo "  5) Claude Desktop   MCP entry written to claude_desktop_config.json (no hook system)"
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
                    5) picked="$picked claude-desktop" ;;
                    *) bad=1 ;;
                esac
            done
            if [ -n "$bad" ] || [ -z "$picked" ]; then
                echo "  please answer with numbers 1-5 (e.g. \"1 3\")"
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
installer_python() {  # echoes an interpreter >= 3.10, or nothing
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    return 0
}
# >>> client-only preflight >>>
# Nothing Docker-shaped to check: a client-only install depends on the
# token file and on the remote daemon answering. The token is never read
# into output.
client_only_preflight() {
    step "Client-only install${CLIENT_ONLY_VIA:-}: this machine's clients will use the daemon at $DAEMON_URL (no Docker, volumes or local daemon here)."
    [ -n "$TOKEN_FILE" ] || TOKEN_FILE="${PSEUDOLIFE_MCP_TOKEN_FILE:-}"
    if [ -z "$TOKEN_FILE" ]; then
        echo "client-only install: pass --token-file <path> (or set PSEUDOLIFE_MCP_TOKEN_FILE) naming the file that holds the remote daemon's bearer token, and --read-token to create it. The installer never mints one for a remote daemon${CLIENT_ONLY_VIA:-}" >&2
        exit 2
    fi
    token_py="$(installer_python)"
    if [ -z "$token_py" ]; then
        echo "client-only install: Python 3.10 or newer is needed to check the token file and to install the shim. Install it, then re-run" >&2
        exit 1
    fi
    if [ -n "${READ_TOKEN:-}" ]; then
        if [ -e "$TOKEN_FILE" ] || [ -L "$TOKEN_FILE" ]; then
            echo "client-only install: --read-token creates the token file, and it already exists: $TOKEN_FILE. Drop --read-token to use it, or remove it first" >&2
            exit 1
        fi
        # Read unechoed, and handed to the helper on stdin through a shell
        # builtin: the token never reaches an argument list or the output.
        [ ! -t 0 ] || printf "Paste the daemon's bearer token (not shown), then Enter: " >&2
        token_value=""
        IFS= read -rs token_value || true
        [ ! -t 0 ] || echo "" >&2
        token_report=$(printf '%s\n' "$token_value" |
            "$token_py" "$repo/ops/client_credentials.py" write-token-file --path "$TOKEN_FILE") || true
        token_value=""
        case "$token_report" in
            *'"written"'*) step "Wrote the token file $TOKEN_FILE (owner-only; the token is not shown)." ;;
            *)
                token_problem=$(printf '%s' "$token_report" | sed -n 's/.*"recovery": "\([^"]*\)".*/\1/p')
                echo "client-only install: could not write the token file (${token_problem:-no result}): $TOKEN_FILE" >&2
                exit 1 ;;
        esac
    fi
    if [ ! -f "$TOKEN_FILE" ] || [ ! -s "$TOKEN_FILE" ]; then
        echo "client-only install: the token file is missing or empty: $TOKEN_FILE. Create it with --read-token, or write the daemon's token into it owner-only, then re-run" >&2
        exit 1
    fi
    # The shim's own check, on every OS: an owner-only regular file (mode
    # bits on POSIX, the ACL on Windows), no link, one well-formed token.
    token_report=$("$token_py" "$repo/ops/client_credentials.py" check-token-file --path "$TOKEN_FILE") || true
    case "$token_report" in *'"ready"'*) ;; *)
        token_problem=$(printf '%s' "$token_report" | sed -n 's/.*"recovery": "\([^"]*\)".*/\1/p')
        echo "client-only install: the shim cannot use the token file (${token_problem:-no result}): $TOKEN_FILE. It reads only an owner-only regular file: chmod 600 it on Linux or macOS, or create it with --read-token, then re-run" >&2
        exit 1 ;;
    esac
    TOKEN_FILE="$(cd "$(dirname "$TOKEN_FILE")" && pwd)/$(basename "$TOKEN_FILE")"
    for selected_client in $CLIENTS; do
        case "$selected_client" in claude|codex|gemini)
            command -v "$selected_client" >/dev/null 2>&1 || {
                echo "client-only install: the $selected_client CLI is not on PATH. Install it, or drop it from --client, then re-run" >&2
                exit 1; } ;;
        esac
    done
    if ! command -v curl >/dev/null 2>&1; then
        echo "curl is needed to check the daemon at $DAEMON_URL; install it, then re-run." >&2
        exit 1
    fi
    daemon_health=""
    if ! daemon_health="$(curl -fsS --max-time 5 "$DAEMON_URL/health" 2>/dev/null)" ||
            ! printf '%s' "$daemon_health" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
        echo "client-only install: no healthy daemon answered at $DAEMON_URL/health.${CLIENT_ONLY_VIA:-}" >&2
        echo "  The daemon must be exposed to this machine, for example through the tailnet: check the host and port, that the daemon listens beyond loopback on its host, and that this machine reaches it (curl $DAEMON_URL/health)." >&2
        exit 1
    fi
    if ! is_loopback_host "$(daemon_url_host "$DAEMON_URL")"; then
        if printf '%s' "$daemon_health" | grep -q '"auth"[[:space:]]*:[[:space:]]*false'; then
            echo "client-only install: the daemon runs without a bearer token (auth: false) at $DAEMON_URL. An unauthenticated bank must never be reached over a network: set PSEUDOLIFE_MCP_TOKEN for the daemon on its host, restart it, and re-run" >&2
            exit 1
        fi
        case "$DAEMON_URL" in http://*)
            echo "WARNING: $DAEMON_URL is plain HTTP: the link itself is unencrypted, so it must be a private network such as a tailnet, or a TLS reverse proxy must front the daemon." >&2 ;;
        esac
    fi
    step "Daemon answered at $DAEMON_URL/health."
}
# <<< client-only preflight <<<
step "Preflight..."
if [ -n "$CLIENT_ONLY" ]; then
    client_only_preflight
else
    "$repo/ops/preflight.sh" --client "$CLIENT_LIST" || {
        echo "Preflight failed — fix the line(s) above and re-run." >&2; exit 1; }
fi

# ── 3. extractor choice (explicit, no default) ─────────────────────────────
# A client-only install has none: the remote daemon runs its own extractor.
if [ -z "$EXTRACTOR" ] && [ -z "$CLIENT_ONLY" ]; then
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
[ -n "$CLIENT_ONLY" ] || step "Extractor mode: $EXTRACTOR"
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
if [ -z "$CLIENT_ONLY" ]; then
    docker volume create "$bank_vol" >/dev/null
    docker volume create "$state_vol" >/dev/null
    step "Volumes ready: $bank_vol, $state_vol"
fi

# ── 5. managed env block ───────────────────────────────────────────────────
# Daemon-side writer default: a single first-class provider gets its own id;
# any multi-provider or generic install falls back to the neutral id — the
# per-provider ids then ride each MCP registration's env instead (stage 10).
case "$CLIENTS" in
    claude) WRITER_ID=claude-code ;;
    claude-desktop) WRITER_ID=claude-desktop ;;
    codex)  WRITER_ID=codex ;;
    gemini) WRITER_ID=gemini ;;
    *)      WRITER_ID=mcp-client ;;
esac
if [ -z "$CLIENT_ONLY" ]; then
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
fi

# ── 5b. bearer token (the agent board needs one) ──────────────────────────
# A default install mints one, so the board is on; every client wired below
# then carries it in an owner-only token file. The value is never printed.
# Open loopback (docs/guide/configuration.md) stays available: --no-token,
# and --transport http, whose registrations cannot carry a token file. A
# host that cannot install the shim falls back to HTTP, so it mints none.
# >>> mint token >>>
# Whether stage 11 can be expected to install the shim: pipx, or a Python
# >= 3.10 with pip outside a PEP 668 externally managed environment (where
# `pip install --user` refuses). The helper needs that Python either way.
shim_tooling_ready() {
    [ -n "$(installer_python)" ] || return 1
    command -v pipx >/dev/null 2>&1 && return 0
    "$(installer_python)" -c 'import os, sys, sysconfig
import pip
marker = os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")
sys.exit(1 if sys.prefix == sys.base_prefix and os.path.exists(marker) else 0)' >/dev/null 2>&1
}
TOKEN_STATE=""
token_py="$(installer_python)"
# A client-only install never mints: the remote daemon's token is the file
# the operator supplied. A token already set gates the daemon whatever this
# run may mint, and the HTTP-registration warning and the board hint depend
# on knowing that.
if [ -n "${CLIENT_ONLY:-}" ]; then
    TOKEN_STATE=remote
elif [ -n "${PSEUDOLIFE_MCP_TOKEN:-}${PSEUDOLIFE_MCP_TOKENS:-}" ] ||
        [ -n "$(get_env PSEUDOLIFE_MCP_TOKEN)$(get_env PSEUDOLIFE_MCP_TOKENS)" ]; then
    TOKEN_STATE=present
elif [ -n "$NO_TOKEN" ]; then
    TOKEN_STATE=opted-out
elif [ "$TRANSPORT" != shim ]; then
    TOKEN_STATE=http
elif ! shim_tooling_ready; then
    TOKEN_STATE=no-shim
else
    mint_output=$("$token_py" "$repo/ops/client_credentials.py" mint --env-file "$env_file") || true
    case "$mint_output" in
        *'"minted"'*)
            TOKEN_STATE=minted
            step "Minted a bearer token in ops/.env (owner-only, never printed) - the agent board needs one." ;;
        *'"present"'*) TOKEN_STATE=present ;;
        *)
            TOKEN_STATE=failed
            echo "WARNING: could not mint a bearer token in ops/.env, so the agent board stays off. Set PSEUDOLIFE_MCP_TOKEN there by hand and re-run." >&2 ;;
    esac
fi
# <<< mint token <<<

# ── 6. sidecar enable/disable via the compose override ────────────────────
installer_owns_override() {
    [ -f "$override_file" ] || return 1
    local first
    first="$(head -1 "$override_file")"
    [ "$first" = "$OVERRIDE_MARKER" ] || [ "$first" = "$LEGACY_OVERRIDE_MARKER" ]
}
if [ -n "$CLIENT_ONLY" ]; then
    :  # no local daemon here, so its compose override is not this run's to change
elif [ "$EXTRACTOR" = "sonnet-only" ] || [ "$EXTRACTOR" = "codex-only" ]; then
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
if [ -z "$CLIENT_ONLY" ]; then
    compose=(--env-file "$env_file" -f "$compose_file")
    [ -f "$override_file" ] && compose+=(-f "$override_file")
    step "docker compose up -d --build (first build downloads images — grab a coffee)..."
    docker compose "${compose[@]}" up -d --build
fi

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
# A client-only install leaves any local daemon's extractor shims alone.
[ -n "$codex_shim_mode" ] || [ -n "$CLIENT_ONLY" ] || remove_shim_unit pseudolife-codex-shim.service
[ -n "$claude_shim_mode" ] || [ -n "$CLIENT_ONLY" ] || remove_shim_unit pseudolife-sonnet-shim.service
if [ -n "$claude_shim_mode" ]; then
    step "Registering the Claude shim autostart (systemd --user)..."
    if ! "$repo/ops/install-shim-autostart.sh" --port "$SHIM_PORT" --model "$MODEL"; then
        echo "WARNING: shim autostart registration failed (no systemd --user on this host?)" >&2
        echo "  Re-run later: ops/install-shim-autostart.sh --port $SHIM_PORT --model $MODEL" >&2
        echo "  Or start it manually: python evals/claude_shim.py --port $SHIM_PORT --model $MODEL --system-prompt-file evals/prompts/sonnet_extractor_v5.md" >&2
    fi
elif [ -n "$codex_shim_mode" ]; then
    step "Registering the Codex shim autostart (systemd --user)..."
    if ! "$repo/ops/install-codex-shim-autostart.sh" --port "$SHIM_PORT" --model "$MODEL"; then
        echo "WARNING: shim autostart registration failed (no systemd --user on this host?)" >&2
        echo "  Re-run later: ops/install-codex-shim-autostart.sh --port $SHIM_PORT --model $MODEL" >&2
        echo "  Or start it manually: python evals/codex_shim.py --port $SHIM_PORT --model $MODEL" >&2
    fi
fi

# ── 8b. Claude Code plugin (hooks + commands layer) ────────────────────────
# >>> claude plugin >>>
# The plugin is the third install beside the daemon and the shim, and the
# only one that needed two commands typed inside Claude Code. Installing it
# here gives a fresh machine the session briefing and per-turn hooks from
# the plugin (section 9 then skips the settings.json hooks). Idempotent: an
# installed plugin is left alone — its cache moves through /plugin update,
# and the daemon's session briefing says when it is behind.
CLAUDE_PLUGIN_ID="pseudolife-memory@pseudolife-mcp"
CLAUDE_PLUGIN_MARKETPLACE="pseudolife-mcp"
CLAUDE_PLUGIN_MARKETPLACE_SOURCE="Pseudogiant-xr/Pseudolife-MCP"
PLUGIN_CLAUDE=""
PLUGIN_CLAUDE_RECOVERY=""
claude_plugin_manual() {
    echo "inside Claude Code run /plugin marketplace add $CLAUDE_PLUGIN_MARKETPLACE_SOURCE then /plugin install $CLAUDE_PLUGIN_ID"
}
claude_plugin_recorded() {
    grep -q "\"$CLAUDE_PLUGIN_ID\"" "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null
}
claude_plugin_installed_version() {
    # The record is pretty-printed JSON: the plugin's entry follows its key
    # within a few lines. curl+sed hosts only — no jq or python assumed.
    { grep -A 12 "\"$CLAUDE_PLUGIN_ID\"" "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null \
        | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1; } || true
}
install_claude_plugin() {
    case " $CLIENTS " in *" claude "*) ;; *) return 0 ;; esac
    # An installed plugin is reported as such even under skip: the ladder
    # line must agree with the hook-ownership lines section 9 derives from
    # the same record.
    if claude_plugin_recorded; then
        PLUGIN_CLAUDE="present:$(claude_plugin_installed_version)"
        return 0
    fi
    if [ "$CLAUDE_PLUGIN" = skip ]; then PLUGIN_CLAUDE=skipped; return 0; fi
    if ! command -v claude >/dev/null 2>&1; then
        PLUGIN_CLAUDE=no-cli
        PLUGIN_CLAUDE_RECOVERY="the claude CLI is not on PATH; $(claude_plugin_manual)"
        return 0
    fi
    if ! grep -q "\"$CLAUDE_PLUGIN_MARKETPLACE\"" "$HOME/.claude/plugins/known_marketplaces.json" 2>/dev/null; then
        step "Adding the $CLAUDE_PLUGIN_MARKETPLACE plugin marketplace to Claude Code..."
        if ! claude plugin marketplace add "$CLAUDE_PLUGIN_MARKETPLACE_SOURCE"; then
            PLUGIN_CLAUDE=failed
            PLUGIN_CLAUDE_RECOVERY="'claude plugin marketplace add $CLAUDE_PLUGIN_MARKETPLACE_SOURCE' failed (GitHub reachable?); retry it, or $(claude_plugin_manual)"
            echo "WARNING: $PLUGIN_CLAUDE_RECOVERY" >&2
            return 0
        fi
    fi
    step "Installing the $CLAUDE_PLUGIN_ID plugin into Claude Code..."
    # --yes skips a confirmation newer CLIs require without a TTY; older
    # ones lack the flag, so probe rather than assume. Captured, not piped:
    # under pipefail a `grep -q` that exits early makes the CLI's SIGPIPE
    # read as "no such flag", and some CLIs print help on stderr.
    yes_flag=""
    install_help=$(claude plugin install --help 2>&1 || true)
    case "$install_help" in *--yes*) yes_flag=--yes ;; esac
    if ! claude plugin install ${yes_flag:+"$yes_flag"} "$CLAUDE_PLUGIN_ID"; then
        PLUGIN_CLAUDE=failed
        PLUGIN_CLAUDE_RECOVERY="'claude plugin install $CLAUDE_PLUGIN_ID' failed (see above); retry it, or $(claude_plugin_manual)"
        echo "WARNING: $PLUGIN_CLAUDE_RECOVERY" >&2
        return 0
    fi
    if claude_plugin_recorded; then
        PLUGIN_CLAUDE="installed:$(claude_plugin_installed_version)"
    else
        PLUGIN_CLAUDE=failed
        PLUGIN_CLAUDE_RECOVERY="'claude plugin install' returned success but ~/.claude/plugins/installed_plugins.json does not list $CLAUDE_PLUGIN_ID; check 'claude plugin list', or $(claude_plugin_manual)"
        echo "WARNING: $PLUGIN_CLAUDE_RECOVERY" >&2
    fi
}
describe_plugin() {  # $1 = state
    case "$1" in
        installed:*) echo "[x] Plugin               installed (v${1#*:}) - restart Claude Code or /reload-plugins to load it" ;;
        present:*)   echo "[x] Plugin               already installed (v${1#*:})" ;;
        skipped)     echo "[-] Plugin               skipped (--claude-plugin skip) - hooks come from settings.json instead" ;;
        no-cli|failed) echo "[!] Plugin               not installed - $PLUGIN_CLAUDE_RECOVERY" ;;
        *)           echo "[-] Plugin               not installed" ;;
    esac
}
# <<< claude plugin <<<
install_claude_plugin

# >>> claude legacy hooks >>>
# Installs from before the plugin wrote the briefing, coordination and
# discipline hooks (and, before 2026-07-14, episode hooks) into
# ~/.claude/settings.json. The plugin provides them now, so an upgraded user
# runs each one twice. install-hook removes only the exact entries the
# installers wrote, only while the plugin runs for every project, after a
# backup. Section 9 calls this for a plugin-owned Claude; it asks first
# unless --claude-legacy-hooks says otherwise.
LEGACY_CLAUDE=""
LEGACY_CLAUDE_BACKUP=""
claude_legacy_answer() {  # prints the reply; fails when there is no terminal
    [ -t 0 ] || return 1
    printf 'Remove them from ~/.claude/settings.json (a timestamped backup is taken first)? [y/N] ' >&2
    read -r legacy_reply || legacy_reply=""
    printf '%s' "$legacy_reply"
}
cleanup_claude_legacy_hooks() {
    legacy_rc=0
    legacy_report=$("$repo/ops/install-hook.sh" --client claude --remove-legacy --dry-run \
        </dev/null 2>&1) || legacy_rc=$?
    case "$legacy_rc" in
        0) ;;
        3)  # Nothing to remove; a lookalike the user should review still shows.
            case "$legacy_report" in *"by hand"*) printf '%s\n' "$legacy_report" ;; esac
            return 0 ;;
        4) LEGACY_CLAUDE=inactive; printf '%s\n' "$legacy_report"; return 0 ;;
        *) LEGACY_CLAUDE=error; printf '%s\n' "$legacy_report" >&2; return 0 ;;
    esac
    step "Found hooks an earlier install wrote to ~/.claude/settings.json; the plugin provides them now:"
    printf '%s\n' "$legacy_report"
    legacy_choice="$CLAUDE_LEGACY_HOOKS"
    if [ "$legacy_choice" = ask ]; then
        if legacy_reply=$(claude_legacy_answer); then
            case "$legacy_reply" in y|Y|yes|YES|Yes) legacy_choice=remove ;; *) legacy_choice=keep ;; esac
        else
            legacy_choice=keep
            echo "    No terminal to ask: left them in place. Rerun with --claude-legacy-hooks remove to remove them."
        fi
    fi
    if [ "$legacy_choice" != remove ]; then
        LEGACY_CLAUDE=kept
        return 0
    fi
    legacy_rc=0
    legacy_report=$("$repo/ops/install-hook.sh" --client claude --remove-legacy \
        </dev/null 2>&1) || legacy_rc=$?
    printf '%s\n' "$legacy_report"
    if [ "$legacy_rc" = 0 ]; then
        LEGACY_CLAUDE=removed
        LEGACY_CLAUDE_BACKUP=$(printf '%s\n' "$legacy_report" | sed -n 's/^Backed up -> //p')
    else
        LEGACY_CLAUDE=error
    fi
}
describe_legacy_hooks() {  # $1 = state
    case "$1" in
        removed)  echo "[x] Old hooks            removed from ~/.claude/settings.json (backup: $LEGACY_CLAUDE_BACKUP)" ;;
        kept)     echo "[!] Old hooks            still in ~/.claude/settings.json, so they run twice - rerun with --claude-legacy-hooks remove" ;;
        inactive) echo "[-] Old hooks            kept in ~/.claude/settings.json - the plugin is not enabled for all projects" ;;
        error)    echo "[!] Old hooks            not removed (see the error above) - remove them by hand (plugin/README.md, Migrating from installer hook wiring)" ;;
    esac
}
# <<< claude legacy hooks <<<

# ── 9. session lifecycle hooks (hook-capable providers only) ───────────────
# Claude skips hooks owned by its plugin. Codex resolves ownership, consent,
# exact hook trust, runtime verification, and instruction fallback together.
# Gemini/generic have no hook system.
instruction_choice="${INSTRUCTIONS:-${CLAUDE_MD:-auto}}"
if grep -q "pseudolife-memory@pseudolife-mcp" \
        "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null; then
    CLAUDE_PLUGIN_INSTALLED=1
    case " $CLIENTS " in *" claude "*)
        step "pseudolife-memory Claude Code plugin detected — skipping Claude"
        if [ "$instruction_choice" = append ]; then
            echo "    hook (the plugin provides it, serving a compact memory core); the full"
            echo "    CLAUDE.md block is still appended, as requested. The plugin no longer"
            echo "    bundles an MCP server, so the transport is still wired below."
        else
            echo "    hook and CLAUDE.md block (the plugin provides the hook, which serves a"
            echo "    compact memory core; the full block stays optional). The plugin no"
            echo "    longer bundles an MCP server, so the transport is still wired below."
        fi ;;
    esac
else
    CLAUDE_PLUGIN_INSTALLED=""
fi

HOOK_CLAUDE=""
HOOK_CODEX=""
INSTR_CODEX=""
CODEX_HOOK_SOURCE=skip
CODEX_HOOK_RECOVERY=""
CODEX_SETUP_VALID=""
CODEX_CREDENTIAL_FILE=""
CODEX_CREDENTIAL_URL=""
CODEX_CONNECTION_CONFIGURED=""
CODEX_CREDENTIAL_BOOTSTRAP_FAILED=""
CODEX_RUNTIME_DEFAULTS=""
CODEX_RUNTIME_RECOVERY=""
briefing_command="docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"
for selected_client in $CLIENTS; do
    case "$selected_client" in claude|codex) ;; *) continue ;; esac
    if [ "$selected_client" = claude ] && [ -n "$CLAUDE_PLUGIN_INSTALLED" ]; then
        HOOK_CLAUDE=plugin
        cleanup_claude_legacy_hooks
        continue
    fi
    if [ "$selected_client" = codex ]; then
        HOOK_CODEX=unavailable
        INSTR_CODEX=skipped
        CODEX_HOOK_RECOVERY="Install Python 3.10 or newer, then rerun this installer to finish Codex memory setup."
        codex_python=""
        for candidate in python3 python; do
            if command -v "$candidate" >/dev/null 2>&1 &&
                "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
                codex_python="$candidate"
                break
            fi
        done
        if [ -n "$codex_python" ]; then
            credential_exit=0
            installer_token="$(get_env PSEUDOLIFE_MCP_TOKEN)"
            installer_url="${DAEMON_URL:-$(get_env PSEUDOLIFE_MCP_DAEMON_URL)}"
            installer_url="${installer_url:-http://127.0.0.1:8765}"
            credential_args=("$repo/ops/setup-codex-coordination.py" --credentials
                --installer-daemon-url "$installer_url")
            if [ -n "${CLIENT_ONLY:-}" ]; then
                # The remote daemon's token, from the operator's file on stdin.
                credential_output=$("$codex_python" "$repo/ops/setup-codex-coordination.py" \
                    --credentials --installer-token-stdin \
                    --installer-daemon-url "$installer_url" < "$TOKEN_FILE") || credential_exit=$?
            elif [ -n "$installer_token" ]; then
                credential_output=$("$codex_python" "$repo/ops/setup-codex-coordination.py" \
                    --credentials --installer-token-stdin \
                    --installer-daemon-url "$installer_url" <<< "$installer_token") || credential_exit=$?
            else
                credential_output=$("$codex_python" "${credential_args[@]}") || credential_exit=$?
            fi
            if [ "$credential_exit" -eq 0 ] && credential_fields=$("$codex_python" -c '
import json, sys
sys.stdout.reconfigure(encoding="utf-8", newline="\n")
r = json.load(sys.stdin)
assert r["status"] in ("ready", "tokenless")
assert isinstance(r.get("credential_file_configured"), bool)
assert isinstance(r.get("connection_configured"), bool)
print("1" if r["credential_file_configured"] else "0")
print("1" if r["connection_configured"] else "0")
print(r.get("credential_file_path") or "")
print(r.get("daemon_url") or "")
' <<< "$credential_output" 2>/dev/null); then
                {
                    IFS= read -r credential_configured
                    IFS= read -r CODEX_CONNECTION_CONFIGURED
                    IFS= read -r CODEX_CREDENTIAL_FILE || true
                    IFS= read -r CODEX_CREDENTIAL_URL || true
                } <<< "$credential_fields"
                if [ "$CODEX_CONNECTION_CONFIGURED" != 1 ]; then
                    CODEX_CONNECTION_CONFIGURED=""
                fi
                if [ "$credential_configured" = 1 ]; then
                    step "Codex credential file ready; future rotations are picked up by new requests."
                fi
                # An existing Codex registration keeps its own daemon URL.
                if [ -n "${CLIENT_ONLY:-}" ] && [ "${CODEX_CREDENTIAL_URL%/}" != "$DAEMON_URL" ]; then
                    CODEX_CREDENTIAL_BOOTSTRAP_FAILED=1
                    CODEX_HOOK_RECOVERY="The existing Codex registration names the daemon at ${CODEX_CREDENTIAL_URL:-(none)}, not the daemon at $DAEMON_URL. Edit it in place in the Codex config.toml: set PSEUDOLIFE_MCP_DAEMON_URL=$DAEMON_URL in [mcp_servers.pseudolife-memory.env], then re-run."
                fi
            else
                CODEX_CREDENTIAL_BOOTSTRAP_FAILED=1
                CODEX_HOOK_RECOVERY="Codex credential setup failed. Repair the configured token file or Codex configuration, then rerun the installer."
            fi
            if [ -z "$CODEX_CREDENTIAL_BOOTSTRAP_FAILED" ]; then
                setup_args=("$repo/ops/setup-codex-hooks.py" --source "$CODEX_HOOKS"
                    --trust "$CODEX_HOOK_TRUST" --instructions "$instruction_choice")
                if ! [ -t 0 ]; then setup_args+=(--non-interactive); fi
                setup_exit=0
                if [ -n "$CODEX_CONNECTION_CONFIGURED" ]; then
                    if [ -n "$CODEX_CREDENTIAL_FILE" ]; then
                        setup_output=$(env -u PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKEN_FILE="$CODEX_CREDENTIAL_FILE" PSEUDOLIFE_MCP_DAEMON_URL="$CODEX_CREDENTIAL_URL" "$codex_python" "${setup_args[@]}") || setup_exit=$?
                    else
                        setup_output=$(env -u PSEUDOLIFE_MCP_TOKEN -u PSEUDOLIFE_MCP_TOKEN_FILE PSEUDOLIFE_MCP_DAEMON_URL="$CODEX_CREDENTIAL_URL" "$codex_python" "${setup_args[@]}") || setup_exit=$?
                    fi
                elif [ -n "$CODEX_CREDENTIAL_FILE" ]; then
                    setup_output=$(env -u PSEUDOLIFE_MCP_TOKEN PSEUDOLIFE_MCP_TOKEN_FILE="$CODEX_CREDENTIAL_FILE" PSEUDOLIFE_MCP_DAEMON_URL="$CODEX_CREDENTIAL_URL" "$codex_python" "${setup_args[@]}") || setup_exit=$?
                else
                    setup_output=$("$codex_python" "${setup_args[@]}") || setup_exit=$?
                fi
                # Parse only data, never shell code. A failed helper must leave the
                # remaining provider setup available and must never imply readiness.
                if [ "$setup_exit" -le 1 ] && setup_fields=$("$codex_python" -c '
import json, sys
sys.stdout.reconfigure(encoding="utf-8", newline="\n")
r = json.load(sys.stdin)
assert r["status"] in ("ready", "pending", "unavailable", "skipped")
assert r["source"] in ("manual", "plugin", "skip")
assert r["instructions"] in ("present", "appended", "skipped", "covered-by-hooks")
for key in ("status", "source", "instructions", "recovery"):
    print(" ".join(str(r.get(key) or "").splitlines()))
' <<< "$setup_output" 2>/dev/null); then
                    {
                        IFS= read -r HOOK_CODEX
                        IFS= read -r CODEX_HOOK_SOURCE
                        IFS= read -r INSTR_CODEX
                        IFS= read -r CODEX_HOOK_RECOVERY || true
                    } <<< "$setup_fields"
                    CODEX_SETUP_VALID=1
                else
                    CODEX_HOOK_RECOVERY="Codex setup did not return a valid result. Run python3 ops/setup-codex-hooks.py to retry."
                fi
            fi
        fi
        step "Codex hooks: $HOOK_CODEX; standing instructions: $INSTR_CODEX."
        if [ -n "$CODEX_HOOK_RECOVERY" ]; then echo "    $CODEX_HOOK_RECOVERY"; fi
        continue
    fi
    if [ -n "${CLIENT_ONLY:-}" ]; then
        # No daemon container here: the hook runs the installed shim by its
        # path, known once section 11 installed it (after section 12's check).
        HOOK_CLAUDE=deferred
        continue
    fi
    step "Installing $selected_client session hook..."
    "$repo/ops/install-hook.sh" --client "$selected_client" "" "$briefing_command"
    if [ "$selected_client" = claude ]; then HOOK_CLAUDE=hook; else HOOK_CODEX=hook; fi
done

# ── 10. standing memory instructions (consent; never edited without it) ────
# Codex instructions are resolved by the setup helper after hook verification.
# Other clients retain the existing auto/append/skip behavior and prompts.
INSTR_CLAUDE=""
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
    if [ "$selected_client" = codex ]; then
        # A valid helper result owns the fallback. Otherwise preserve explicit
        # append consent even when Python is missing or setup cannot run.
        if [ -z "$CODEX_SETUP_VALID" ] && { [ "$instruction_choice" = append ] ||
            { [ "$instruction_choice" = auto ] && [ "$CODEX_HOOK_TRUST" = yes ]; }; }; then
            codex_home="${CODEX_HOME:-$HOME/.codex}"
            fallback_path="$codex_home/AGENTS.md"
            if [ -f "$codex_home/AGENTS.override.md" ] &&
                grep -q '[^[:space:]]' "$codex_home/AGENTS.override.md"; then
                fallback_path="$codex_home/AGENTS.override.md"
            fi
            if grep -Eq '^## Memory([[:space:]]|$)' "$fallback_path" 2>/dev/null &&
                grep -q 'pseudolife-memory' "$fallback_path" &&
                grep -q 'RECALL' "$fallback_path" && grep -q 'CAPTURE' "$fallback_path" &&
                grep -q 'REFLECT' "$fallback_path"; then
                INSTR_CODEX=present
            else
                fallback_ok=1
                mkdir -p "$codex_home" || fallback_ok=""
                if [ -n "$fallback_ok" ] && [ -f "$fallback_path" ]; then
                    saved=$(mktemp "$fallback_path.bak-pseudolife-XXXXXXXX") &&
                        cp "$fallback_path" "$saved" || fallback_ok=""
                    if [ -n "$fallback_ok" ]; then step "Backed up Codex standing instructions to $saved"; fi
                fi
                if [ -n "$fallback_ok" ] && [ -r "$repo/examples/CLAUDE.memory.md" ] &&
                    { printf '\n\n'; cat "$repo/examples/CLAUDE.memory.md"; } >> "$fallback_path"; then
                    INSTR_CODEX=appended
                else
                    CODEX_HOOK_RECOVERY="$CODEX_HOOK_RECOVERY Could not write standing instructions; check the Codex home and file permissions."
                    step "$CODEX_HOOK_RECOVERY"
                fi
            fi
            step "Codex standing instructions: $INSTR_CODEX ($fallback_path). Hooks still require setup."
        fi
        continue
    fi
    if [ "$selected_client" = claude-desktop ]; then
        # Desktop reads no standing file; the MCP instructions field is its
        # only briefing channel.
        continue
    fi
    if [ "$selected_client" = claude ] && [ -n "$CLAUDE_PLUGIN_INSTALLED" ]; then
        # The plugin's SessionStart hook serves a compact memory core, not
        # this block: auto and skip leave CLAUDE.md alone (the summary says
        # so), and an explicit append still writes it.
        if [ "$instruction_choice" != append ]; then
            record_instr claude "covered-by-plugin"
            continue
        fi
    fi
    case "$selected_client" in
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
            claude)
                # Skipped by default. The settings.json SessionStart hook
                # serves the compact memory core and the live briefing, not
                # this block; the summary names the file to append it to.
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
SHIM_PATH=""
SHIM_HELD=""
resolve_installed_shim() {  # optional $1 = pipx, python3, or python
    shim_manager="${1:-}"
    if [ -z "$shim_manager" ]; then
        if command -v pipx >/dev/null 2>&1; then
            shim_manager=pipx
        elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            shim_manager=python3
        elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            shim_manager=python
        else
            return 1
        fi
    fi
    if [ "$shim_manager" = pipx ]; then
        shim_bin_dir="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
    else
        shim_bin_dir="$("$shim_manager" -c "import sysconfig; print(sysconfig.get_path('scripts', scheme=sysconfig.get_preferred_scheme('user')))" 2>/dev/null || true)"
    fi
    if [ -n "$shim_bin_dir" ] && command -v cygpath >/dev/null 2>&1; then
        case "$shim_bin_dir" in [A-Za-z]:\\*) shim_bin_dir="$(cygpath -u "$shim_bin_dir")" ;; esac
    fi
    for shim_candidate in "$shim_bin_dir/pseudolife-mcp" "$shim_bin_dir/pseudolife-mcp.exe"; do
        if [ -n "$shim_bin_dir" ] && [ -f "$shim_candidate" ] && [ -x "$shim_candidate" ]; then
            SHIM_PATH="$shim_candidate"
            return 0
        fi
    done
    return 1
}
# On Windows neither pipx nor pip can replace a shim that sessions are running,
# and neither puts back what it removed first. pip 24.0 stashes an uninstall in
# sorted order, so site-packages is already renamed to `~` siblings when the
# running launcher raises WinError 32, from uninstall(), outside the try that
# rolls back. `pipx install --force` deletes the venv with
# rmtree(ignore_errors=True), taking everything that is not locked. On
# 2026-09-25 this left a runtime without its own package. So an installed shim
# that a session is running is left in place, still usable, and the rerun named.
shim_process_table() {  # prints pid|parent pid|image rows; status 2 off Windows, 1 when unreadable
    case "$(uname -s 2>/dev/null || true)" in
        MINGW*|MSYS*|CYGWIN*) ;;
        *) return 2 ;;  # an open or running file does not stop pip or pipx here
    esac
    shim_ps="$(command -v powershell.exe 2>/dev/null || true)"
    shim_root="${SYSTEMROOT:-${SystemRoot:-${WINDIR:-}}}"
    if [ -z "$shim_ps" ] && [ -n "$shim_root" ]; then
        shim_ps="$(cygpath -u "$shim_root" 2>/dev/null || printf '%s' "$shim_root")/System32/WindowsPowerShell/v1.0/powershell.exe"
    fi
    if [ -z "$shim_ps" ] || [ ! -f "$shim_ps" ]; then return 1; fi
    shim_rows="$("$shim_ps" -NoProfile -NonInteractive -Command '[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-CimInstance -ClassName Win32_Process -Property ProcessId, ParentProcessId, ExecutablePath -ErrorAction Stop | ForEach-Object { "{0}|{1}|{2}" -f $_.ProcessId, $_.ParentProcessId, $_.ExecutablePath }' 2>/dev/null)" || return 1
    if [ -z "$shim_rows" ]; then return 1; fi
    printf '%s\n' "$shim_rows"
}
shim_held_paths() {  # $1 = pipx, python3 or python; prints what a running session of the installed shim executes from
    # The whole pipx venv (its launcher and the venv's python.exe redirector
    # both run there) and the installed launcher. A --user scripts directory
    # holds other tools' launchers too, so only the shim's own launcher counts.
    # Runs in a command substitution: resolve_installed_shim's SHIM_PATH stays put.
    if [ "$1" = pipx ]; then
        shim_pipx_home="$(pipx environment --value PIPX_HOME 2>/dev/null || true)"
        if [ -n "$shim_pipx_home" ]; then
            shim_pipx_home="$(cygpath -u "$shim_pipx_home" 2>/dev/null || printf '%s' "$shim_pipx_home")"
            if [ -d "$shim_pipx_home/venvs/pseudolife-mcp" ]; then
                cygpath -w "$shim_pipx_home/venvs/pseudolife-mcp" 2>/dev/null || printf '%s\n' "$shim_pipx_home/venvs/pseudolife-mcp"
            fi
        fi
    fi
    if resolve_installed_shim "$1"; then
        cygpath -w "$SHIM_PATH" 2>/dev/null || printf '%s\n' "$SHIM_PATH"
    fi
}
shim_upgrade_held() {  # $1 = pipx, python3 or python; status 0 = leave the installed shim alone (SHIM_HELD says why)
    shim_table_status=0
    shim_table="$(shim_process_table)" || shim_table_status=$?
    if [ "$shim_table_status" -eq 2 ]; then return 1; fi
    shim_held="$(shim_held_paths "$1")"
    # Nothing installed yet: nothing a session could be running.
    if [ -z "$shim_held" ]; then return 1; fi
    shim_held_list="$(printf '%s\n' "$shim_held" | awk 'NR > 1 { printf ", " } { printf "%s", $0 }')"
    shim_rerun="\"$repo/ops/install.sh\" (with the options you used)"
    if [ "$shim_table_status" -ne 0 ]; then
        SHIM_HELD="Could not read the Windows process table to see whether a session is running the pseudolife-mcp shim from $shim_held_list; it was left as it is rather than upgraded. Close every Claude Code / Codex session using it, then rerun: $shim_rerun"
        echo "WARNING: $SHIM_HELD" >&2
        return 0
    fi
    # A Claude session is the launcher plus the venv redirector it starts, a
    # Codex one the redirector alone: each process tree counts once.
    # powershell.exe writes CRLF rows. Git for Windows' awk drops the CR
    # itself; the sub() below is for awks that keep it.
    shim_counts="$(printf '%s\n' "$shim_table" | SHIM_HELD_ROOTS="$shim_held" awk -F'|' '
        BEGIN {
            n = split(ENVIRON["SHIM_HELD_ROOTS"], roots, "\n")
            for (i = 1; i <= n; i++) { roots[i] = tolower(roots[i]); gsub(/\\/, "/", roots[i]); sub(/\/+$/, "", roots[i]) }
        }
        {
            image = tolower($3); sub(/\r$/, "", image); gsub(/\\/, "/", image)
            if (image == "") next
            for (i = 1; i <= n; i++)
                if (roots[i] != "" && (image == roots[i] || index(image, roots[i] "/") == 1)) { parent[$1] = $2; break }
        }
        END {
            for (pid in parent) { processes++; if (!(parent[pid] in parent)) sessions++ }
            printf "%d %d\n", sessions, processes
        }')"
    shim_sessions="${shim_counts% *}"
    shim_processes="${shim_counts#* }"
    if [ "$shim_processes" -eq 0 ]; then return 1; fi
    if [ "$shim_sessions" -eq 1 ]; then shim_who="1 session is"; else shim_who="$shim_sessions sessions are"; fi
    if [ "$shim_processes" -eq 1 ]; then shim_what="1 process"; else shim_what="$shim_processes processes"; fi
    if [ "$1" = pipx ]; then shim_tool=pipx; else shim_tool=pip; fi
    SHIM_HELD="$shim_who running the pseudolife-mcp shim from $shim_held_list ($shim_what); it was not upgraded, because on Windows $shim_tool cannot replace a running shim and would leave it half-removed. The installed shim stays in place and registered. Close every Claude Code / Codex session using it, then rerun: $shim_rerun"
    echo "WARNING: $SHIM_HELD" >&2
    return 0
}
ensure_shim() {
    if [ -n "$SHIM_TRIED" ]; then return 0; fi
    SHIM_TRIED=1
    shim_install_succeeded=""
    shim_manager=""
    # A held shim counts as installed: the one in place stays usable.
    if command -v pipx >/dev/null 2>&1; then
        if shim_upgrade_held pipx; then
            shim_install_succeeded=1
            shim_manager=pipx
        # --force replaces a stale same-version environment as well as
        # installing fresh, and the local path keeps shim and daemon aligned.
        elif pipx install --force "$repo"; then
            shim_install_succeeded=1
            shim_manager=pipx
        fi
    elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        if shim_upgrade_held python3; then
            shim_install_succeeded=1
            shim_manager=python3
        elif python3 -m pip install --user --upgrade "$repo"; then
            shim_install_succeeded=1
            shim_manager=python3
        fi
    elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        if shim_upgrade_held python; then
            shim_install_succeeded=1
            shim_manager=python
        elif python -m pip install --user --upgrade "$repo"; then
            shim_install_succeeded=1
            shim_manager=python
        fi
    fi
    if [ -n "$shim_install_succeeded" ] && resolve_installed_shim "$shim_manager"; then
        SHIM_OK=1
    elif [ -n "$shim_install_succeeded" ]; then
        echo "WARNING: shim installation completed, but its installed executable was not found in the manager's scripts directory." >&2
    fi
    return 0
}

cli_env_flag() {  # $1 = cli; echoes the supported env flag, or nothing
    if "$1" mcp add --help 2>/dev/null | grep -q -- '--env'; then
        echo "--env"
    fi
}
registration_is_http() {
    printf '%s\n' "$1" | grep -Eqi '(^|[[:space:]])"?(transport|type)"?[[:space:]]*:[[:space:]]*"?(streamable_)?http"?([,}]|[[:space:]]|$)|\(http\)'
}
no_spawn_guard_is_enabled() {
    printf '%s\n' "$1" | grep -Eqi '(^|[,{}])[[:space:]]*"?PSEUDOLIFE_MCP_NO_SPAWN"?[[:space:]]*[:=][[:space:]]*"?(1|true|yes|on)"?[[:space:]]*([,}]|$)'
}
registered_stdio_command() {
    printf '%s\n' "$1" | sed -nE \
        -e 's/^[[:space:]]*"?[Cc]ommand"?[[:space:]]*:[[:space:]]*"([^"]+)"[,]?[[:space:]]*$/\1/p' \
        -e 's/^[[:space:]]*[Cc]ommand:[[:space:]]*(.+)[[:space:]]*$/\1/p' | sed -n '1p'
}
warn_unverified_no_spawn_guard() {
    echo "WARNING: the existing $1 stdio registration's no-spawn guard is missing or cannot be verified; the registration was preserved and Docker-tier setup is incomplete." >&2
    echo "  Edit the existing registration in place and set PSEUDOLIFE_MCP_NO_SPAWN=1; preserve its command, arguments, daemon URL, token file, and all other environment values." >&2
    echo "  Re-run this installer after verifying the effective value with $2." >&2
}

# Two env pairs ride each shim registration: PSEUDOLIFE_WRITER_ID (the shim
# forwards it as the X-PL-Writer header — per-provider write attribution)
# and PSEUDOLIFE_MCP_NO_SPAWN=1 (Docker-tier no-spawn guard, 2026-08-29
# incident). CLI env-flag support is probed, never assumed: a missing flag
# fails closed before registration. HTTP transport cannot carry env, so there
# the daemon default (ops/.env) applies and no shim exists to spawn anything.
MCP_CLAUDE=""
MCP_CLAUDE_DESKTOP=""
MCP_CODEX=""
MCP_GEMINI=""

# Claude Desktop launches MCP servers with a sanitized environment, so a
# token-gated daemon needs a token FILE path on the entry — never the value,
# and never an OS env var, which Desktop cannot see (2026-09-19 incident).
# Honour an explicit PSEUDOLIFE_MCP_TOKEN_FILE; otherwise, when any token is
# configured for the daemon, use a private default path. The registrar
# WRITES that file (owner-only) from the token source below, or migrates a
# literal already in the entry, and never points the entry at a file it did
# not write or validate.
desktop_token_file() {
    if [ -n "${PSEUDOLIFE_MCP_TOKEN_FILE:-}" ]; then
        echo "$PSEUDOLIFE_MCP_TOKEN_FILE"
        return 0
    fi
    if [ -z "${PSEUDOLIFE_MCP_TOKEN:-}" ] && [ -z "${PSEUDOLIFE_MCP_TOKENS:-}" ] &&
        [ -z "$(get_env PSEUDOLIFE_MCP_TOKEN)" ] && [ -z "$(get_env PSEUDOLIFE_MCP_TOKENS)" ]; then
        return 0
    fi
    echo "$HOME/.pseudolife-mcp/claude-desktop.token"
}
# The singular daemon token, from the installer's environment or ops/.env
# (a per-principal PSEUDOLIFE_MCP_TOKENS map names no single value to copy).
desktop_token_source() {
    if [ -n "${PSEUDOLIFE_MCP_TOKEN:-}" ]; then
        echo "$PSEUDOLIFE_MCP_TOKEN"
        return 0
    fi
    get_env PSEUDOLIFE_MCP_TOKEN
}
desktop_tokens_source() {
    if [ -n "${PSEUDOLIFE_MCP_TOKENS:-}" ]; then
        echo "$PSEUDOLIFE_MCP_TOKENS"
        return 0
    fi
    get_env PSEUDOLIFE_MCP_TOKENS
}
configure_codex_runtime_defaults() {
    if [ -z "$codex_python" ]; then
        MCP_CODEX=failed
        CODEX_RUNTIME_DEFAULTS=failed
        CODEX_RUNTIME_RECOVERY="Install Python 3.10 or newer, then run python3 ops/setup-codex-coordination.py --runtime-defaults."
        echo "WARNING: Codex was registered, but its runtime defaults could not be configured because Python 3.10 or newer was not found." >&2
        return 0
    fi
    runtime_exit=0
    runtime_output=$("$codex_python" "$repo/ops/setup-codex-coordination.py" --runtime-defaults) || runtime_exit=$?
    if [ "$runtime_exit" -eq 0 ] && runtime_state=$("$codex_python" -c '
import json, sys
r = json.load(sys.stdin)
assert r["status"] == "ready"
assert r["runtime_defaults"] in ("configured", "preserved")
print(r["runtime_defaults"])
' <<< "$runtime_output" 2>/dev/null); then
        CODEX_RUNTIME_DEFAULTS="$runtime_state"
        step "Codex runtime defaults ready (startup 240s, tools 240s, required)."
    else
        MCP_CODEX=failed
        CODEX_RUNTIME_DEFAULTS=failed
        CODEX_RUNTIME_RECOVERY="Run python3 ops/setup-codex-coordination.py --runtime-defaults, then retry the Codex task."
        echo "WARNING: Codex was registered, but its runtime defaults were not confirmed. $CODEX_RUNTIME_RECOVERY" >&2
    fi
    return 0
}
# Board credentials for the Claude Code and Gemini shims: an owner-only token
# file per client holding its principal's own PSEUDOLIFE_MCP_TOKENS entry,
# else the singular token; the shim re-reads it per call. Claude Code also
# gets a private directory for its board address, keyed by session id so
# `claude --resume <id>` keeps it. The plugin's hooks read the Claude Code
# process environment, not the registration, so they get the file through
# settings.json unless the user's own environment supplies a credential.
# >>> client token files >>>
CLIENT_DAEMON_URL="${DAEMON_URL:-$(get_env PSEUDOLIFE_MCP_DAEMON_URL)}"
CLIENT_DAEMON_URL="${CLIENT_DAEMON_URL:-http://127.0.0.1:8765}"
CLAUDE_AGENT_STATE_DIR="$HOME/.pseudolife-mcp/claude-code-agents"
CLAUDE_JSON="${CLAUDE_CONFIG_DIR:-$HOME}/.claude.json"
client_token_file() {  # $1 = principal; echoes the token file it wrote or kept, or nothing
    [ "$TRANSPORT" = shim ] || return 0
    client_py="$(installer_python)"
    [ -n "$client_py" ] || return 0
    client_target="$HOME/.pseudolife-mcp/$1.token"
    # The value rides process-scoped env vars, never an argument.
    client_output=$(PSEUDOLIFE_INSTALLER_TOKEN="$(desktop_token_source)" \
        PSEUDOLIFE_INSTALLER_TOKENS="$(desktop_tokens_source)" \
        "$client_py" "$repo/ops/client_credentials.py" token-file \
        --principal "$1" --path "$client_target") || true
    case "$client_output" in
        *'"ready"'*) printf '%s\n' "$client_target" ;;
        *'"tokenless"'*) ;;
        *) echo "WARNING: could not write the $1 token file ($client_output); its registration gets no board credential." >&2 ;;
    esac
}
CLAUDE_TOKEN_FILE=""
GEMINI_TOKEN_FILE=""
if [ -n "${CLIENT_ONLY:-}" ]; then
    # A remote daemon's token stays in the file the operator supplied.
    case " $CLIENTS " in *" claude "*) CLAUDE_TOKEN_FILE="$TOKEN_FILE" ;; esac
    case " $CLIENTS " in *" gemini "*) GEMINI_TOKEN_FILE="$TOKEN_FILE" ;; esac
else
    case " $CLIENTS " in *" claude "*) CLAUDE_TOKEN_FILE="$(client_token_file claude-code)" ;; esac
    case " $CLIENTS " in *" gemini "*) GEMINI_TOKEN_FILE="$(client_token_file gemini)" ;; esac
fi
# A client-only install's settings.json hooks run the host shim, which needs
# the file and the URL there as much as the plugin's hooks do.
if { [ "${HOOK_CLAUDE:-}" = plugin ] || [ -n "${CLIENT_ONLY:-}" ]; } && [ -n "$CLAUDE_TOKEN_FILE" ]; then
    settings_args=("$repo/ops/client_credentials.py" claude-settings-env
        --settings "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
        --token-file "$CLAUDE_TOKEN_FILE" --daemon-url "$CLIENT_DAEMON_URL")
    # A client-only install's hooks must reach the remote daemon with the
    # operator's file, whatever an earlier local install left there.
    [ -z "${CLIENT_ONLY:-}" ] || settings_args+=(--replace)
    settings_output=$("$(installer_python)" "${settings_args[@]}") || true
    case "$settings_output" in
        *'"updated"'*)
            if [ -n "${CLIENT_ONLY:-}" ]; then
                settings_changed=$(printf '%s' "$settings_output" | sed -n 's/.*"changed": \[\([^]]*\)\].*/\1/p' | tr -d '"')
                settings_backup=$(printf '%s' "$settings_output" | sed -n 's/.*"backup": "\([^"]*\)".*/\1/p')
                step "Claude Code hooks: set ${settings_changed:-the daemon URL and token file} in ~/.claude/settings.json (env) for the daemon at $CLIENT_DAEMON_URL${settings_backup:+ (backup: $settings_backup)}."
            else
                step "Claude Code plugin hooks: PSEUDOLIFE_MCP_TOKEN_FILE set in ~/.claude/settings.json (env)."
            fi ;;
        *'"kept"'*|*'"unchanged"'*) ;;
        *) echo "WARNING: could not give the Claude Code hooks the token file ($settings_output). Set PSEUDOLIFE_MCP_TOKEN_FILE=$CLAUDE_TOKEN_FILE and PSEUDOLIFE_MCP_DAEMON_URL=$CLIENT_DAEMON_URL in the env block of ~/.claude/settings.json." >&2 ;;
    esac
fi
# <<< client token files <<<
for selected_client in $CLIENTS; do
    if [ "$selected_client" = claude-desktop ]; then
        # No `mcp add` CLI: the entry is merged into claude_desktop_config.json
        # by ops/register_claude_desktop.py (absolute shim path — Desktop's
        # sanitized PATH omits pipx/venv bin dirs; token FILE when gated). It is
        # named pseudolife-desktop so Code-tab sessions keep their own
        # per-session pseudolife-memory server.
        if [ "$TRANSPORT" != "shim" ]; then
            echo "WARNING: Claude Desktop needs the stdio shim (its connector dialog rejects plain-http URLs) — ignoring --transport http for it." >&2
        fi
        ensure_shim
        desktop_py="$(installer_python)"
        if [ -z "$SHIM_OK" ] || [ -z "$SHIM_PATH" ]; then
            echo "WARNING: pseudolife-mcp shim installation did not yield a usable executable — Claude Desktop not wired. Re-run after fixing pipx/Python, or register by hand: python3 ops/register_claude_desktop.py --command <absolute path to pseudolife-mcp>" >&2
            MCP_CLAUDE_DESKTOP=failed
            continue
        fi
        if [ -z "$desktop_py" ]; then
            echo "WARNING: no python >= 3.10 found to write claude_desktop_config.json — Claude Desktop not wired." >&2
            MCP_CLAUDE_DESKTOP=failed
            continue
        fi
        token_file="$(desktop_token_file)"
        token_source=""
        tokens_source=""
        desktop_args=(--command "$SHIM_PATH" --writer-id claude-desktop)
        if [ -n "${CLIENT_ONLY:-}" ]; then
            # The operator's file as it is: a remote daemon's token is never copied.
            desktop_args+=(--daemon-url "$CLIENT_DAEMON_URL" --token-file "$TOKEN_FILE")
        elif [ -n "$token_file" ]; then
            if [ -n "${PSEUDOLIFE_MCP_TOKEN_FILE:-}" ]; then
                desktop_args+=(--token-file "$token_file")
            else
                desktop_args+=(--default-token-file "$token_file")
            fi
            # The token value rides a process-scoped env var the registrar
            # reads by NAME — never a command-line argument, never printed.
            token_source="$(desktop_token_source)"
            tokens_source="$(desktop_tokens_source)"
            if [ -n "$token_source" ]; then
                desktop_args+=(--token-from-env PSEUDOLIFE_DESKTOP_TOKEN_SOURCE)
            elif [ -n "$tokens_source" ]; then
                desktop_args+=(--tokens-from-env PSEUDOLIFE_DESKTOP_TOKENS_SOURCE)
            else
                echo "WARNING: the daemon is token-gated but no token source is set (environment or ops/.env) — the registrar can only reuse a credential already in the Desktop entry. If registration fails, configure a singular token or exactly one claude-desktop principal in PSEUDOLIFE_MCP_TOKENS." >&2
            fi
        fi
        if PSEUDOLIFE_DESKTOP_TOKEN_SOURCE="$token_source" \
                PSEUDOLIFE_DESKTOP_TOKENS_SOURCE="$tokens_source" \
                "$desktop_py" "$repo/ops/register_claude_desktop.py" "${desktop_args[@]}"; then
            MCP_CLAUDE_DESKTOP=shim-env
        else
            MCP_CLAUDE_DESKTOP=failed
        fi
        if [ "$MCP_CLAUDE_DESKTOP" = shim-env ]; then
            step "Wired into Claude Desktop as pseudolife-desktop via the pseudolife-mcp shim (claude_desktop_config.json) — fully quit and relaunch Desktop to load it."
        else
            echo "WARNING: Claude Desktop registration failed — see the error above and re-run." >&2
        fi
        continue
    fi
    if [ "$selected_client" = generic ]; then
        echo ""
        step "Other MCP-capable agents — paste-ready config:"
        echo ""
        show_generic_snippets
        echo ""
        if [ -n "${CLIENT_ONLY:-}" ]; then
            echo "  This daemon is remote: use the stdio shape, with"
            echo "  \"PSEUDOLIFE_MCP_DAEMON_URL\": \"$CLIENT_DAEMON_URL\" and"
            echo "  \"PSEUDOLIFE_MCP_TOKEN_FILE\": \"$TOKEN_FILE\" added to its env"
            echo "  (the HTTP shape cannot carry the token)."
            echo ""
        fi
        continue
    fi
    if [ "$selected_client" = codex ]; then
        if [ -n "$CODEX_CREDENTIAL_BOOTSTRAP_FAILED" ]; then
            MCP_CODEX=failed
            echo "WARNING: Codex MCP registration was skipped because credential setup failed. Rerun the installer after repairing the reported credential problem." >&2
            continue
        elif [ -n "${CLIENT_ONLY:-}" ] && [ -z "$CODEX_CREDENTIAL_FILE" ]; then
            # Without it Codex would reach whatever daemon its defaults name.
            MCP_CODEX=failed
            echo "WARNING: Codex was not registered: a client-only install points it at $CLIENT_DAEMON_URL through the Codex credential setup, which did not run (it needs Python 3.10 or newer; see above). Fix that, then re-run." >&2
            continue
        elif existing_codex=$(codex mcp get pseudolife-memory 2>/dev/null); then
            existing_codex_guard=$(codex mcp get pseudolife-memory --json 2>/dev/null || printf '%s' "$existing_codex")
            codex_guard_unverified=""
            if [ "$TRANSPORT" = "shim" ] && ! registration_is_http "$existing_codex_guard" && ! registration_is_http "$existing_codex"; then
                if ! no_spawn_guard_is_enabled "$existing_codex_guard"; then
                    warn_unverified_no_spawn_guard "Codex" "codex mcp get pseudolife-memory --json"
                    codex_guard_unverified=1
                fi
                managed_registered_shim=""
                bare_registered_shim=""
                registered_shim=$(registered_stdio_command "$existing_codex")
                if printf '%s' "$registered_shim" | grep -Eqi '^pseudolife-mcp(\.exe)?$'; then
                    managed_registered_shim=1; bare_registered_shim=1
                elif resolve_installed_shim; then
                    if [ -n "$registered_shim" ] && command -v cygpath >/dev/null 2>&1; then
                        registered_shim=$(cygpath -aw "$registered_shim" 2>/dev/null || printf '%s' "$registered_shim")
                        managed_shim=$(cygpath -aw "$SHIM_PATH" 2>/dev/null || printf '%s' "$SHIM_PATH")
                    else
                        managed_shim="$SHIM_PATH"
                    fi
                    if [ -n "$registered_shim" ] && [ "$registered_shim" = "$managed_shim" ]; then managed_registered_shim=1; fi
                fi
                if [ -n "$managed_registered_shim" ]; then
                    ensure_shim
                    if [ -n "$SHIM_OK" ]; then
                        if [ -n "${SHIM_HELD:-}" ]; then
                            echo "WARNING: the existing Codex registration was preserved, but its pseudolife-mcp shim was not upgraded — see the warning above." >&2
                            MCP_CODEX=failed
                        elif [ -z "$bare_registered_shim" ]; then
                            step "Codex registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                            MCP_CODEX=present-upgraded
                        else
                        resolved_shim=$(command -v pseudolife-mcp 2>/dev/null || true)
                        installed_shim="$SHIM_PATH"
                        if command -v cygpath >/dev/null 2>&1; then
                            resolved_shim=$(cygpath -aw "$resolved_shim" 2>/dev/null || printf '%s' "$resolved_shim")
                            installed_shim=$(cygpath -aw "$installed_shim" 2>/dev/null || printf '%s' "$installed_shim")
                        elif command -v readlink >/dev/null 2>&1; then
                            resolved_shim=$(readlink -f "$resolved_shim" 2>/dev/null || printf '%s' "$resolved_shim")
                            installed_shim=$(readlink -f "$installed_shim" 2>/dev/null || printf '%s' "$installed_shim")
                        fi
                        if [ -n "$resolved_shim" ] && [ "$resolved_shim" = "$installed_shim" ]; then
                            step "Codex registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                            MCP_CODEX=present-upgraded
                        else
                            echo "WARNING: the existing Codex registration was preserved and the checkout shim installed, but bare pseudolife-mcp still resolves to a different executable. Remove the earlier pseudolife-mcp from PATH or put the installed scripts directory first, then re-run." >&2
                            MCP_CODEX=failed
                        fi
                        fi
                    else
                        echo "WARNING: the existing Codex registration was preserved, but its pseudolife-mcp shim upgrade failed — see the pip/pipx output above and re-run." >&2
                        MCP_CODEX=failed
                    fi
                else
                    echo "WARNING: the existing Codex stdio registration uses a custom registered command or interpreter; it was preserved and may need a separate update." >&2
                    MCP_CODEX=present-custom
                fi
                if [ -n "$codex_guard_unverified" ]; then MCP_CODEX=failed; fi
            else
                step "MCP server already wired into Codex — registration preserved."
                MCP_CODEX=present
            fi
            CODEX_RUNTIME_DEFAULTS=preserved
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
                    if [ -n "$CODEX_CONNECTION_CONFIGURED" ] && [ -n "$CODEX_CREDENTIAL_FILE" ]; then
                        codex mcp add pseudolife-memory "$env_flag" PSEUDOLIFE_WRITER_ID=codex "$env_flag" PSEUDOLIFE_MCP_NO_SPAWN=1 "$env_flag" "PSEUDOLIFE_MCP_DAEMON_URL=$CODEX_CREDENTIAL_URL" "$env_flag" "PSEUDOLIFE_MCP_TOKEN_FILE=$CODEX_CREDENTIAL_FILE" -- "$SHIM_PATH"
                    elif [ -n "$CODEX_CONNECTION_CONFIGURED" ]; then
                        codex mcp add pseudolife-memory "$env_flag" PSEUDOLIFE_WRITER_ID=codex "$env_flag" PSEUDOLIFE_MCP_NO_SPAWN=1 "$env_flag" "PSEUDOLIFE_MCP_DAEMON_URL=$CODEX_CREDENTIAL_URL" -- "$SHIM_PATH"
                    else
                        codex mcp add pseudolife-memory "$env_flag" PSEUDOLIFE_WRITER_ID=codex "$env_flag" PSEUDOLIFE_MCP_NO_SPAWN=1 -- "$SHIM_PATH"
                    fi
                    MCP_CODEX=shim-env
                    configure_codex_runtime_defaults
                else
                    echo "WARNING: this Codex CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed." >&2
                    MCP_CODEX=failed
                fi
                if [ "$MCP_CODEX" = shim-env ]; then
                    step "Wired into Codex via the pseudolife-mcp shim — per-session identity (a Codex session no longer inherits a concurrent Claude session's episode)."
                fi
            else
                echo "WARNING: shim unavailable for Codex (see warnings above) — falling back to HTTP." >&2
                echo "  Without the shim, a Codex session running beside a Claude Code session shares its episode identity." >&2
                if [ -n "$CODEX_CREDENTIAL_FILE" ] || [ -n "${CLIENT_ONLY:-}" ]; then
                    echo "WARNING: Codex authentication requires the stdio shim; HTTP fallback was not registered." >&2
                    MCP_CODEX=failed
                else
                    codex_http_url="${CODEX_CREDENTIAL_URL:-http://127.0.0.1:8765}/mcp"
                    codex mcp add pseudolife-memory --url "$codex_http_url"
                    MCP_CODEX=http
                    step "Wired into Codex (codex mcp add, HTTP fallback)."
                    configure_codex_runtime_defaults
                fi
            fi
        else
            if [ -n "$CODEX_CREDENTIAL_FILE" ] || [ -n "${CLIENT_ONLY:-}" ]; then
                echo "WARNING: Codex authentication requires the stdio shim; HTTP transport was not registered." >&2
                MCP_CODEX=failed
            else
                codex mcp add pseudolife-memory --url http://127.0.0.1:8765/mcp
                step "Wired into Codex (codex mcp add, HTTP)."
                MCP_CODEX=http
            fi
        fi
    elif [ "$selected_client" = gemini ]; then
        if existing_gemini=$(gemini mcp list 2>/dev/null) && printf '%s' "$existing_gemini" | grep -q pseudolife-memory; then
            gemini_registration=$(printf '%s\n' "$existing_gemini" | grep -E 'pseudolife-memory:' | sed -n '1p')
            gemini_guard_unverified=""
            if [ "$TRANSPORT" = "shim" ] && ! registration_is_http "$gemini_registration"; then
                # `gemini mcp list` does not expose env values, so an existing
                # stdio guard cannot be verified without inspecting settings.
                warn_unverified_no_spawn_guard "Gemini CLI" "the pseudolife-memory entry in ~/.gemini/settings.json"
                gemini_guard_unverified=1
                managed_registered_shim=""
                bare_registered_shim=""
                if printf '%s' "$gemini_registration" | grep -Eqi 'pseudolife-memory:[[:space:]]*pseudolife-mcp(\.exe)?[[:space:]]*\(stdio\)'; then
                    managed_registered_shim=1; bare_registered_shim=1
                elif resolve_installed_shim && printf '%s' "$gemini_registration" | grep -Fq "pseudolife-memory: $SHIM_PATH (stdio)"; then
                    managed_registered_shim=1
                fi
                if [ -n "$managed_registered_shim" ]; then
                    ensure_shim
                    if [ -n "$SHIM_OK" ]; then
                        if [ -n "${SHIM_HELD:-}" ]; then
                            echo "WARNING: the existing Gemini CLI registration was preserved, but its pseudolife-mcp shim was not upgraded — see the warning above." >&2
                            MCP_GEMINI=failed
                        elif [ -z "$bare_registered_shim" ]; then
                            step "Gemini CLI registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                            MCP_GEMINI=present-upgraded
                        else
                        resolved_shim=$(command -v pseudolife-mcp 2>/dev/null || true)
                        installed_shim="$SHIM_PATH"
                        if command -v cygpath >/dev/null 2>&1; then
                            resolved_shim=$(cygpath -aw "$resolved_shim" 2>/dev/null || printf '%s' "$resolved_shim")
                            installed_shim=$(cygpath -aw "$installed_shim" 2>/dev/null || printf '%s' "$installed_shim")
                        elif command -v readlink >/dev/null 2>&1; then
                            resolved_shim=$(readlink -f "$resolved_shim" 2>/dev/null || printf '%s' "$resolved_shim")
                            installed_shim=$(readlink -f "$installed_shim" 2>/dev/null || printf '%s' "$installed_shim")
                        fi
                        if [ -n "$resolved_shim" ] && [ "$resolved_shim" = "$installed_shim" ]; then
                            step "Gemini CLI registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                            MCP_GEMINI=present-upgraded
                        else
                            echo "WARNING: the existing Gemini CLI registration was preserved and the checkout shim installed, but bare pseudolife-mcp still resolves to a different executable. Remove the earlier pseudolife-mcp from PATH or put the installed scripts directory first, then re-run." >&2
                            MCP_GEMINI=failed
                        fi
                        fi
                    else
                        echo "WARNING: the existing Gemini CLI registration was preserved, but its pseudolife-mcp shim upgrade failed — see the pip/pipx output above and re-run." >&2
                        MCP_GEMINI=failed
                    fi
                else
                    echo "WARNING: the existing Gemini CLI stdio registration uses a custom registered command or interpreter; it was preserved and may need a separate update." >&2
                    MCP_GEMINI=present-custom
                fi
                if [ -n "$gemini_guard_unverified" ]; then MCP_GEMINI=failed; fi
            else
                step "MCP server already wired into Gemini CLI — registration preserved."
                MCP_GEMINI=present
            fi
            # `gemini mcp list` shows no env, so the installer cannot tell
            # whether this registration carries a token; say how to add one.
            if [ -n "${GEMINI_TOKEN_FILE:-}" ]; then
                echo "WARNING: the daemon requires a bearer token; unless the existing Gemini CLI registration already carries one, its memory calls will be refused. Edit it in place in ~/.gemini/settings.json and set PSEUDOLIFE_MCP_TOKEN_FILE=$GEMINI_TOKEN_FILE (an HTTP registration cannot carry it: register the stdio shim instead), or re-run with --no-token." >&2
            fi
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
                    if [ -n "${GEMINI_TOKEN_FILE:-}" ]; then
                        gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini -e PSEUDOLIFE_MCP_NO_SPAWN=1 -e "PSEUDOLIFE_MCP_TOKEN_FILE=$GEMINI_TOKEN_FILE" -e "PSEUDOLIFE_MCP_DAEMON_URL=$CLIENT_DAEMON_URL" pseudolife-memory "$SHIM_PATH"
                    else
                        gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini -e PSEUDOLIFE_MCP_NO_SPAWN=1 pseudolife-memory "$SHIM_PATH"
                    fi
                    MCP_GEMINI=shim-env
                else
                    echo "WARNING: this Gemini CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed." >&2
                    MCP_GEMINI=failed
                fi
                if [ "$MCP_GEMINI" = shim-env ]; then
                    step "Wired into Gemini CLI via the pseudolife-mcp shim — per-session identity."
                fi
            elif [ -n "${CLIENT_ONLY:-}" ]; then
                echo "WARNING: shim unavailable for Gemini CLI (see warnings above), and a remote daemon needs it: an HTTP registration cannot carry the token file. Install pipx and re-run." >&2
                MCP_GEMINI=failed
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
        claude_guard_unverified=""
        if [ "$TRANSPORT" = "shim" ] && ! registration_is_http "$existing_claude"; then
            if ! no_spawn_guard_is_enabled "$existing_claude"; then
                warn_unverified_no_spawn_guard "Claude Code" "claude mcp get pseudolife-memory"
                claude_guard_unverified=1
            fi
            managed_registered_shim=""
            bare_registered_shim=""
            registered_shim=$(registered_stdio_command "$existing_claude")
            if printf '%s' "$registered_shim" | grep -Eqi '^pseudolife-mcp(\.exe)?$'; then
                managed_registered_shim=1; bare_registered_shim=1
            elif resolve_installed_shim; then
                if [ -n "$registered_shim" ] && command -v cygpath >/dev/null 2>&1; then
                    registered_shim=$(cygpath -aw "$registered_shim" 2>/dev/null || printf '%s' "$registered_shim")
                    managed_shim=$(cygpath -aw "$SHIM_PATH" 2>/dev/null || printf '%s' "$SHIM_PATH")
                else
                    managed_shim="$SHIM_PATH"
                fi
                if [ -n "$registered_shim" ] && [ "$registered_shim" = "$managed_shim" ]; then managed_registered_shim=1; fi
            fi
            if [ -n "$managed_registered_shim" ]; then
                ensure_shim
                if [ -n "$SHIM_OK" ]; then
                    if [ -n "${SHIM_HELD:-}" ]; then
                        echo "WARNING: the existing Claude Code registration was preserved, but its pseudolife-mcp shim was not upgraded — see the warning above." >&2
                        MCP_CLAUDE=failed
                    elif [ -z "$bare_registered_shim" ]; then
                        step "Claude Code registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                        MCP_CLAUDE=present-upgraded
                    else
                    resolved_shim=$(command -v pseudolife-mcp 2>/dev/null || true)
                    installed_shim="$SHIM_PATH"
                    if command -v cygpath >/dev/null 2>&1; then
                        resolved_shim=$(cygpath -aw "$resolved_shim" 2>/dev/null || printf '%s' "$resolved_shim")
                        installed_shim=$(cygpath -aw "$installed_shim" 2>/dev/null || printf '%s' "$installed_shim")
                    elif command -v readlink >/dev/null 2>&1; then
                        resolved_shim=$(readlink -f "$resolved_shim" 2>/dev/null || printf '%s' "$resolved_shim")
                        installed_shim=$(readlink -f "$installed_shim" 2>/dev/null || printf '%s' "$installed_shim")
                    fi
                    if [ -n "$resolved_shim" ] && [ "$resolved_shim" = "$installed_shim" ]; then
                        step "Claude Code registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                        MCP_CLAUDE=present-upgraded
                    else
                        echo "WARNING: the existing Claude Code registration was preserved and the checkout shim installed, but bare pseudolife-mcp still resolves to a different executable. Remove the earlier pseudolife-mcp from PATH or put the installed scripts directory first, then re-run." >&2
                        MCP_CLAUDE=failed
                    fi
                    fi
                else
                    echo "WARNING: the existing Claude Code registration was preserved, but its pseudolife-mcp shim upgrade failed — see the pip/pipx output above and re-run." >&2
                    MCP_CLAUDE=failed
                fi
            else
                echo "WARNING: the existing Claude Code stdio registration uses a custom registered command or interpreter; it was preserved and may need a separate update." >&2
                MCP_CLAUDE=present-custom
            fi
            if [ -n "$claude_guard_unverified" ]; then MCP_CLAUDE=failed; fi
        else
            step "MCP server already wired into Claude Code — registration preserved."
            MCP_CLAUDE=present
        fi
        # An install from before the board token: the upgraded shim reads a
        # token file, so add it, the daemon URL and the state directory to
        # the registration in place (never a remove and re-add), keeping any
        # credential it already has. Anything else gets the manual fix.
        claude_credential_ok=""
        if [ "$MCP_CLAUDE" = present-upgraded ] && [ -n "${CLAUDE_TOKEN_FILE:-}" ]; then
            claude_env_output=$("$(installer_python)" "$repo/ops/client_credentials.py" registration-env \
                --config "$CLAUDE_JSON" \
                --set "PSEUDOLIFE_MCP_TOKEN_FILE=$CLAUDE_TOKEN_FILE" \
                --set "PSEUDOLIFE_MCP_DAEMON_URL=$CLIENT_DAEMON_URL" \
                --set "PSEUDOLIFE_AGENT_STATE_DIR=$CLAUDE_AGENT_STATE_DIR") || true
            case "$claude_env_output" in
                *'"updated"'*)
                    claude_credential_ok=1
                    step "Claude Code registration: added the board token file, daemon URL and state directory in place. Restart Claude Code sessions to load them." ;;
                *'"unchanged"'*) claude_credential_ok=1 ;;
            esac
        fi
        if [ -z "$claude_credential_ok" ] && [ -n "${CLAUDE_TOKEN_FILE:-}" ] &&
                ! printf '%s\n' "$existing_claude" | grep -q 'PSEUDOLIFE_MCP_TOKEN'; then
            echo "WARNING: the daemon requires a bearer token, but the existing Claude Code registration carries none, so its memory calls will be refused. Edit the registration in place and set PSEUDOLIFE_MCP_TOKEN_FILE=$CLAUDE_TOKEN_FILE (an HTTP registration cannot carry it: register the stdio shim instead), or re-run with --no-token." >&2
        fi
        # The in-place edit above only adds what is missing, so a registration
        # from an earlier local install may still name that daemon.
        # Captured, then matched: a `grep -q` that exits early under
        # pipefail would read the CLI's SIGPIPE as a mismatch.
        if [ -n "${CLIENT_ONLY:-}" ]; then
            claude_registration_now=$(claude mcp get pseudolife-memory 2>/dev/null || true)
            case "$claude_registration_now" in *"$CLIENT_DAEMON_URL"*) ;; *)
                echo "WARNING: the existing Claude Code registration does not name the daemon at $CLIENT_DAEMON_URL. Edit it in place and set PSEUDOLIFE_MCP_DAEMON_URL=$CLIENT_DAEMON_URL and PSEUDOLIFE_MCP_TOKEN_FILE=$CLAUDE_TOKEN_FILE." >&2 ;;
            esac
        fi
    elif [ "$TRANSPORT" = "shim" ]; then
        ensure_shim
        if [ -n "$SHIM_OK" ]; then
            env_flag="$(cli_env_flag claude)"
            if [ -n "$env_flag" ]; then
                # --env is variadic and must come AFTER the server name:
                # placed earlier it swallows the name as another KEY=value
                # pair and the whole add fails (verified against the claude
                # CLI 2026-08-29; the `--` separator ends the value list).
                # PSEUDOLIFE_MCP_NO_SPAWN: Docker-tier shims wait for the
                # compose daemon instead of spawning a fallback that can
                # shadow the real bank (see the Codex registration above).
                # With a token, the board credential rides along: the token
                # file (re-read per call), the daemon URL, and the state
                # directory that keeps a resumed session's board address.
                if [ -n "${CLAUDE_TOKEN_FILE:-}" ]; then
                    claude mcp add --scope user pseudolife-memory "$env_flag" PSEUDOLIFE_WRITER_ID=claude-code PSEUDOLIFE_MCP_NO_SPAWN=1 "PSEUDOLIFE_MCP_TOKEN_FILE=$CLAUDE_TOKEN_FILE" "PSEUDOLIFE_MCP_DAEMON_URL=$CLIENT_DAEMON_URL" "PSEUDOLIFE_AGENT_STATE_DIR=$CLAUDE_AGENT_STATE_DIR" -- "$SHIM_PATH"
                else
                    claude mcp add --scope user pseudolife-memory "$env_flag" PSEUDOLIFE_WRITER_ID=claude-code PSEUDOLIFE_MCP_NO_SPAWN=1 -- "$SHIM_PATH"
                fi
                MCP_CLAUDE=shim-env
            else
                echo "WARNING: this Claude CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed." >&2
                MCP_CLAUDE=failed
            fi
            if [ "$MCP_CLAUDE" = shim-env ]; then
                step "Wired into Claude Code via the pseudolife-mcp shim — per-session identity (required for correct episodes with concurrent sessions)."
            fi
        elif [ -n "${CLIENT_ONLY:-}" ]; then
            echo "WARNING: the pseudolife-mcp shim is unavailable (see the pip/pipx output above), and a remote daemon needs it: an HTTP registration cannot carry the token file. Install pipx and re-run." >&2
            MCP_CLAUDE=failed
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
# A client-only install checked the remote daemon at preflight, and nothing
# starts locally, so there is nothing to wait for.
if [ -z "$CLIENT_ONLY" ]; then
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
fi

# >>> client-only claude hook >>>
# A client-only install's Claude Code session hook (deferred from section
# 9): the installed shim by its path, since a hook's PATH need not include
# pipx's directory. It reads the daemon's address and the token file from
# the settings.json env block section 11 wrote.
if [ "${HOOK_CLAUDE:-}" = deferred ]; then
    ensure_shim
    if [ -n "$SHIM_OK" ] && [ -n "$SHIM_PATH" ]; then
        shim_command="${SHIM_PATH%.exe}"
        case "$shim_command" in *[[:space:]]*)
            echo "WARNING: the shim's path holds a space, so the session hook runs pseudolife-mcp from PATH instead: $shim_command" >&2
            shim_command=pseudolife-mcp ;;
        esac
        step "Installing claude session hook..."
        "$repo/ops/install-hook.sh" --client claude "" "$shim_command briefing --hook-json"
        HOOK_CLAUDE=hook
    else
        echo "WARNING: the Claude Code session hook was not installed: it runs the pseudolife-mcp shim, which is unavailable (see above)." >&2
        HOOK_CLAUDE=failed
    fi
fi
# <<< client-only claude hook <<<

# ── 13. per-provider wiring ladder + per-mode verify hints ─────────────────
# [x] wired · [-] deliberately skipped · [!] unavailable, with remediation.
mcp_marker() {  # $1 = state
    if [ "$1" = failed ]; then echo "[!]"; else echo "[x]"; fi
}
describe_mcp() {  # $1 = state
    case "$1" in
        shim-env) echo "stdio shim (per-provider writer id set)" ;;
        shim)     echo "stdio shim (writer id: daemon default in ops/.env)" ;;
        http)     echo "HTTP (writer id: daemon default in ops/.env)" ;;
        present)  echo "already wired (unchanged)" ;;
        present-upgraded) echo "already wired; checkout shim upgraded" ;;
        present-custom) echo "already wired with custom command (unchanged)" ;;
        failed)   echo "registration or shim upgrade FAILED - see warning above and re-run" ;;
        *)        echo "not wired" ;;
    esac
}
describe_instr() {  # $1 = state
    case "$1" in
        appended:*|present:*) echo "[x] Standing file        ${1#*:}" ;;
        covered-by-plugin)    echo "[-] Standing file        skipped - the plugin serves a compact memory core; append examples/CLAUDE.memory.md for the full guide" ;;
        covered-by-hooks)     echo "[-] Standing file        skipped - verified hooks serve a compact memory core; append examples/CLAUDE.memory.md for the full guide" ;;
        present)             echo "[x] Standing file        existing Codex memory fallback" ;;
        appended)            echo "[x] Standing file        Codex memory fallback appended" ;;
        skipped:*) echo "[-] Standing file        skipped - append later: cat examples/CLAUDE.memory.md >> ${1#*:}" ;;
        *)         echo "[-] Standing file        skipped" ;;
    esac
}
echo ""
step "What got wired, per agent:"
if [ -n "$CLIENT_ONLY" ]; then
    echo ""
    echo "  Daemon: remote at $CLIENT_DAEMON_URL - this machine runs none (client-only install)"
fi
for selected_client in $CLIENTS; do
    echo ""
    case "$selected_client" in
        claude)
            echo "  Claude Code"
            echo "    $(mcp_marker "$MCP_CLAUDE") MCP transport        $(describe_mcp "$MCP_CLAUDE")"
            echo "    [x] Server instructions  automatic (MCP instructions field)"
            if [ "$HOOK_CLAUDE" = plugin ]; then
                echo "    [x] Session briefing     Claude Code plugin"
                echo "    [x] Per-turn discipline  Claude Code plugin"
            elif [ "$HOOK_CLAUDE" = failed ]; then
                echo "    [!] Session briefing     not installed - the shim is unavailable"
                echo "    [!] Per-turn discipline  not installed"
            else
                echo "    [x] Session briefing     SessionStart hook -> ~/.claude/settings.json"
                echo "    [x] Per-turn discipline  UserPromptSubmit hook"
            fi
            echo "    $(describe_plugin "$PLUGIN_CLAUDE")"
            [ -z "$LEGACY_CLAUDE" ] || echo "    $(describe_legacy_hooks "$LEGACY_CLAUDE")"
            describe_instr "$INSTR_CLAUDE" | sed 's/^/    /' ;;
        claude-desktop)
            echo "  Claude Desktop"
            if [ "$MCP_CLAUDE_DESKTOP" = shim-env ]; then
                echo "    [x] MCP transport        $(describe_mcp "$MCP_CLAUDE_DESKTOP")"
            else
                echo "    [!] MCP transport        registration FAILED - see the warning above and re-run"
            fi
            echo "    [x] Server instructions  automatic (MCP instructions field)"
            echo "    [!] Session briefing     unavailable - Claude Desktop has no hook system"
            echo "    [!] Per-turn discipline  unavailable"
            echo "    [-] Standing file        none - Desktop reads no CLAUDE.md"
            echo "    Restart: fully quit Claude Desktop (tray / menu-bar icon) and relaunch to load the entry." ;;
        codex)
            echo "  OpenAI Codex"
            echo "    $(mcp_marker "$MCP_CODEX") MCP transport        $(describe_mcp "$MCP_CODEX")"
            echo "    [x] Server instructions  automatic (MCP instructions field)"
            if [ "$HOOK_CODEX" = ready ]; then
                echo "    [x] Memory hooks         trusted and verified ($CODEX_HOOK_SOURCE)"
            else
                echo "    [!] Memory hooks         $HOOK_CODEX"
                if [ -n "$CODEX_HOOK_RECOVERY" ]; then echo "    $CODEX_HOOK_RECOVERY"; fi
            fi
            echo "    Verify runtime: codex mcp get pseudolife-memory; run doctor from that command's environment."
            case "$CODEX_RUNTIME_DEFAULTS" in
                configured) echo "    [x] Runtime defaults     startup 240s; tools 240s; required" ;;
                preserved)  echo "    [-] Runtime settings     existing registration unchanged" ;;
                failed)     echo "    [!] Runtime defaults     not confirmed - $CODEX_RUNTIME_RECOVERY" ;;
                *)          echo "    [!] Runtime defaults     unavailable" ;;
            esac
            describe_instr "$INSTR_CODEX" | sed 's/^/    /' ;;
        gemini)
            echo "  Gemini CLI"
            echo "    $(mcp_marker "$MCP_GEMINI") MCP transport        $(describe_mcp "$MCP_GEMINI")"
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
# One line for the agent board, as the daemon answers it for the first client
# token file (else the daemon's singular token): on, or off and why.
# >>> board line >>>
board_hint=""
case "$TOKEN_STATE" in
    opted-out) board_hint=" (installed with --no-token; re-run without it to turn the board on)" ;;
    http)      board_hint=" (--transport http registrations cannot carry a token file)" ;;
    no-shim)   board_hint=" (no pipx or pip-capable Python >= 3.10 to install the shim, so no token was minted; install one and re-run)" ;;
esac
board_file="${CLAUDE_TOKEN_FILE:-${GEMINI_TOKEN_FILE:-${CODEX_CREDENTIAL_FILE:-${TOKEN_FILE:-}}}}"
board_py="$(installer_python)"
if [ -z "$board_py" ]; then
    board_line="unknown - no Python >= 3.10 to ask the daemon"
elif [ -n "$board_file" ]; then
    board_line=$("$board_py" "$repo/ops/client_credentials.py" board \
        --daemon-url "$CLIENT_DAEMON_URL" --token-file "$board_file") || board_line="unknown"
else
    board_line=$(PSEUDOLIFE_INSTALLER_TOKEN="$(desktop_token_source)" \
        "$board_py" "$repo/ops/client_credentials.py" board \
        --daemon-url "$CLIENT_DAEMON_URL") || board_line="unknown"
fi
case "$board_line" in
    on*) echo "  [x] Agent board          $board_line" ;;
    *)   echo "  [!] Agent board          $board_line$board_hint" ;;
esac
case "$TOKEN_STATE" in minted|present)
    for http_state in "${MCP_CLAUDE:-}" "${MCP_GEMINI:-}" "${MCP_CODEX:-}"; do
        if [ "$http_state" = http ]; then
            echo "WARNING: an HTTP registration sends no bearer token, and this daemon requires one, so its memory calls will be refused. Register the stdio shim (fix its install and re-run without --transport http), or remove the token from ops/.env for an open-loopback install." >&2
            break
        fi
    done ;;
esac
echo ""
# <<< board line <<<
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
if [ -n "$CLIENT_ONLY" ]; then show_client_only_notes; echo ""; fi
if [ -n "$SHIM_HELD" ]; then echo "WARNING: $SHIM_HELD" >&2; fi
echo "Done. First session: tell your coding agent to remember something."
