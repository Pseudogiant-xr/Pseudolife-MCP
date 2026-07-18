#!/usr/bin/env bash
# Preflight / doctor: CHECK-ONLY prerequisite audit for a fresh install
# (issue #13 — converts "mysterious failure" into "run this line"). Verifies
# each prerequisite and prints the exact remediation for anything missing;
# never installs or changes anything. Exit 0 = ready to install.
#
#   ops/preflight.sh --client claude|codex|both
set -u

CLIENT=claude
if [ "${1:-}" = "--client" ]; then
    CLIENT="${2:-}"
    shift 2
fi
case "$CLIENT" in claude|codex|both) ;; *)
    echo "invalid --client '$CLIENT' (claude|codex|both)" >&2; exit 2 ;;
esac
[ "$#" -eq 0 ] || { echo "unknown argument: $1" >&2; exit 2; }

fails=0

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; printf '        fix: %s\n' "$2"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; printf '        fix: %s\n' "$2"; fails=$((fails+1)); }

echo "Pseudolife-MCP preflight (checks only — nothing is installed or changed)"

# ── docker: installed, daemon reachable, permission to use the socket ──────
if ! command -v docker >/dev/null 2>&1; then
    case "$(uname -s)" in
        Darwin) fix="install Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/" ;;
        *)      fix="install Docker Engine for your distro: https://docs.docker.com/engine/install/ (Arch: sudo pacman -S docker && sudo systemctl enable --now docker)" ;;
    esac
    fail "docker is not installed" "$fix"
elif ! docker info >/dev/null 2>&1; then
    err="$(docker info 2>&1 || true)"
    if printf '%s' "$err" | grep -qi 'permission denied'; then
        fail "docker daemon reachable only as root (socket permission denied)" \
             "sudo usermod -aG docker \$USER   # then log out and back in (group changes need a re-login)"
    else
        fail "docker daemon is not running" \
             "start Docker Desktop, or: sudo systemctl enable --now docker"
    fi
else
    ok "docker installed, daemon reachable"
fi

# ── docker compose v2 ──────────────────────────────────────────────────────
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "docker compose v2"
else
    fail "docker compose v2 plugin missing" \
         "Docker Desktop bundles it; on Linux: https://docs.docker.com/compose/install/linux/ (Arch: sudo pacman -S docker-compose)"
fi

# ── ports 8765 (daemon) / 5433 (postgres): free, or held by our own stack ──
# Warn-only: a taken port turns into a cryptic "port is already allocated" at
# compose up; held-by-us means an existing install (idempotent re-run is fine).
port_listening() { # $1 = port; rc 0 = listening, 1 = free, 2 = cannot tell
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | grep -qE "[:.]$1([[:space:]]|$)"
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
    else
        return 2
    fi
}
running="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
for spec in "8765 daemon pseudolife-mcp-daemon" "5433 postgres pseudolife-mcp-postgres"; do
    set -- $spec
    port="$1"; svc="$2"; cont="$3"
    if printf '%s\n' "$running" | grep -qx "$cont"; then
        ok "port $port held by $cont (existing install)"
    else
        port_listening "$port" && rc=0 || rc=$?
        if [ "$rc" -eq 0 ]; then
            warn "port $port is already in use (needed for the $svc)" \
                 "free the port (e.g. a native Postgres on 5433), then re-run — compose up will otherwise fail with 'port is already allocated'"
        elif [ "$rc" -eq 1 ]; then
            ok "port $port free ($svc)"
        fi
    fi
done

# ── git ────────────────────────────────────────────────────────────────────
if command -v git >/dev/null 2>&1; then
    ok "git"
else
    fail "git is not installed" \
         "https://git-scm.com/downloads (Arch: sudo pacman -S git; Debian/Ubuntu: sudo apt install git)"
fi

# ── python 3 (only needed for the optional Sonnet shim + eval tooling) ─────
if command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then
    ok "python 3 (optional Sonnet shim)"
else
    fail "python 3 not found (optional — needed only for the Sonnet shim)" \
         "https://www.python.org/downloads/ (Arch: sudo pacman -S python)"
fi

# ── claude CLI: installed + logged in ──────────────────────────────────────
if [ "$CLIENT" = claude ] || [ "$CLIENT" = both ]; then
    if ! command -v claude >/dev/null 2>&1; then
        fail "claude CLI not found" \
             "npm install -g @anthropic-ai/claude-code   (needs Node; see https://docs.anthropic.com/en/docs/claude-code)"
    else
        ok "claude CLI"
    fi
fi
if [ "$CLIENT" = codex ] || [ "$CLIENT" = both ]; then
    if ! command -v codex >/dev/null 2>&1; then
        fail "codex CLI not found" \
             "install Codex: https://developers.openai.com/codex/cli/"
    else
        ok "codex CLI"
    fi
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "All checks passed — follow the README Quickstart."
else
    echo "$fails check(s) failed — run the fix line(s) above, then re-run ops/preflight.sh."
    exit 1
fi
