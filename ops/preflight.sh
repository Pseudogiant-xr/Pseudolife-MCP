#!/usr/bin/env bash
# Preflight / doctor: CHECK-ONLY prerequisite audit for a fresh install
# (issue #13 — converts "mysterious failure" into "run this line"). Verifies
# each prerequisite and prints the exact remediation for anything missing;
# never installs or changes anything. Exit 0 = ready to install.
#
#   ops/preflight.sh --client claude|claude-desktop|codex|gemini|generic
#   (comma/space list; aliases: both = claude,codex — all = claude,codex,gemini)
set -u

CLIENT=claude
if [ "${1:-}" = "--client" ]; then
    CLIENT="${2:-}"
    shift 2
fi
CHECKS=""
for tok in $(printf '%s' "$CLIENT" | tr ',' ' '); do
    case "$tok" in
        both) CHECKS="$CHECKS claude codex" ;;
        all) CHECKS="$CHECKS claude codex gemini" ;;
        claude|codex|gemini) CHECKS="$CHECKS $tok" ;;
        generic) ;;  # no CLI to probe — its MCP config is pasted by hand
        claude-desktop) ;;  # no CLI to probe: the native client writes its config
        *) echo "invalid --client '$tok' (claude|claude-desktop|codex|gemini|generic|both|all)" >&2
           exit 2 ;;
    esac
done
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

# ── docker buildx (BuildKit, which the image build needs) ──────────────────
# The Dockerfiles use RUN --mount caches, which only BuildKit runs; without
# the buildx plugin compose falls back to the classic builder and the build
# dies with "the --mount option requires BuildKit".
if command -v docker >/dev/null 2>&1 && docker buildx version >/dev/null 2>&1; then
    ok "docker buildx (BuildKit image builds)"
else
    case "$(uname -s)" in
        Darwin) fix="Docker Desktop bundles it - update Docker Desktop; colima or Homebrew docker: brew install docker-buildx, then link it into ~/.docker/cli-plugins as its caveat says" ;;
        *)      fix="Arch: sudo pacman -S docker-buildx; Debian/Ubuntu with Docker's repo: sudo apt install docker-buildx-plugin; Fedora: sudo dnf install docker-buildx-plugin; other: https://github.com/docker/buildx#installing" ;;
    esac
    fail "docker buildx plugin missing - the image build needs BuildKit for its RUN --mount caches" "$fix"
fi

# ── ports, volumes and Compose ownership: fail before any installer writes ──
preflight_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The native client's check (`installer preflight-check`): the binary named by
# PSEUDOLIFE_NATIVE_CLI (the installer passes its verified copy), else
# PSEUDOLIFE_SHIM_LAUNCHER, else the stable launcher, the order ops/update.sh
# uses.
native_cli="${PSEUDOLIFE_NATIVE_CLI:-${PSEUDOLIFE_SHIM_LAUNCHER:-}}"
if [ -z "$native_cli" ]; then
    case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*)
            native_cli="$(cygpath -u "${LOCALAPPDATA:-$HOME/AppData/Local}" 2>/dev/null || printf '%s' "${LOCALAPPDATA:-$HOME/AppData/Local}")/pseudolife-mcp/bin/pseudolife-mcp.exe" ;;
        *) native_cli="${XDG_DATA_HOME:-$HOME/.local/share}/pseudolife-mcp/bin/pseudolife-mcp" ;;
    esac
fi
if [ ! -f "$native_cli" ]; then
    fail "the native pseudolife-mcp client is needed to verify Docker ownership safely, and it is not at $native_cli" \
         "run ops/install.sh, which downloads and verifies it before this check, or set PSEUDOLIFE_NATIVE_CLI to its binary"
    exit 1
fi
"$native_cli" installer preflight-check --repo "$(dirname "$preflight_dir")" --env-file "$preflight_dir/.env" || exit 1

# ── git ────────────────────────────────────────────────────────────────────
if command -v git >/dev/null 2>&1; then
    ok "git"
else
    fail "git is not installed" \
         "https://git-scm.com/downloads (Arch: sudo pacman -S git; Debian/Ubuntu: sudo apt install git)"
fi

# ── selected MCP client CLI(s) ─────────────────────────────────────────────
case " $CHECKS " in *" claude "*)
    if ! command -v claude >/dev/null 2>&1; then
        fail "claude CLI not found" \
             "npm install -g @anthropic-ai/claude-code   (needs Node; see https://docs.anthropic.com/en/docs/claude-code)"
    else
        ok "claude CLI"
    fi ;;
esac
case " $CHECKS " in *" codex "*)
    if ! command -v codex >/dev/null 2>&1; then
        fail "codex CLI not found" \
             "install Codex: https://developers.openai.com/codex/cli/"
    else
        ok "codex CLI"
    fi ;;
esac
case " $CHECKS " in *" gemini "*)
    if ! command -v gemini >/dev/null 2>&1; then
        fail "gemini CLI not found" \
             "npm install -g @google/gemini-cli   (needs Node; see https://geminicli.com/docs/)"
    else
        ok "gemini CLI"
    fi ;;
esac

echo
if [ "$fails" -eq 0 ]; then
    echo "All checks passed — follow the README Quickstart."
else
    echo "$fails check(s) failed — run the fix line(s) above, then re-run ops/preflight.sh."
    exit 1
fi
