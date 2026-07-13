#!/usr/bin/env bash
# Preflight / doctor: CHECK-ONLY prerequisite audit for a fresh install
# (issue #13 — converts "mysterious failure" into "run this line"). Verifies
# each prerequisite and prints the exact remediation for anything missing;
# never installs or changes anything. Exit 0 = ready to install.
#
#   ops/preflight.sh
set -u

fails=0

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; printf '        fix: %s\n' "$2"; fails=$((fails+1)); }

echo "PseudoLife-MCP preflight (checks only — nothing is installed or changed)"

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
if ! command -v claude >/dev/null 2>&1; then
    fail "claude CLI not found" \
         "npm install -g @anthropic-ai/claude-code   (needs Node; see https://docs.anthropic.com/en/docs/claude-code)"
else
    ok "claude CLI"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "All checks passed — follow the README Quickstart."
else
    echo "$fails check(s) failed — run the fix line(s) above, then re-run ops/preflight.sh."
    exit 1
fi
