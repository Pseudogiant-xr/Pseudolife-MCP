#!/usr/bin/env bash
# Register the Codex extractor shim as a systemd --user service — the
# ChatGPT-plan twin of ops/install-shim-autostart.sh.
#
#   ops/install-codex-shim-autostart.sh                       # port 8086, terra
#   ops/install-codex-shim-autostart.sh --model gpt-5.6-sol   # pick the served model
#
# The shim wraps the ChatGPT-plan `codex` CLI as an OpenAI-compatible endpoint
# for the daemon's dream pass. Requires a signed-in CLI (`codex login`).
# Extraction quality is UNMEASURED for Codex-served models — run
# `evals/ladder_sweep.py --rung terra` before trusting one with
# consolidation. No --prompt-file on purpose: the v2 extraction-prompt
# override is Sonnet-tuned, so the codex shim runs the production prompt
# until a ladder run measures a Codex variant.
#
# --health-ttl default is 1800s (not the shim's own 300s): every /health
# refresh is a real CLI call — metered spend on a free ChatGPT tier
# (300s ≈ 288 calls/day; 1800s ≈ 48). A stale-ok window only costs one
# failed primary attempt before the dream falls back.
set -euo pipefail

PORT=8086
MODEL="gpt-5.6-terra"
HEALTH_TTL=1800
PYTHON_EXE=""
CODEX_CLI=""
LOG_FILE="$HOME/.pseudolife-mcp/codex-shim.log"

while [ $# -gt 0 ]; do
    case "$1" in
        --port)       PORT="$2"; shift 2 ;;
        --model)      MODEL="$2"; shift 2 ;;
        --health-ttl) HEALTH_TTL="$2"; shift 2 ;;
        --python)     PYTHON_EXE="$2"; shift 2 ;;
        --cli)        CODEX_CLI="$2"; shift 2 ;;
        --log-file)   LOG_FILE="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
# An empty --model (installer passthrough) keeps the default.
[ -n "$MODEL" ] || MODEL="gpt-5.6-terra"

repo="$(cd "$(dirname "$0")/.." && pwd)"
command -v systemctl >/dev/null 2>&1 || {
    echo "systemctl not found — run the shim manually instead:" >&2
    echo "  python $repo/evals/codex_shim.py --port $PORT --model $MODEL --health-ttl $HEALTH_TTL" >&2
    exit 1
}

if [ -z "$PYTHON_EXE" ]; then
    if [ -x "$repo/.venv/bin/python" ]; then
        PYTHON_EXE="$repo/.venv/bin/python"
    else
        PYTHON_EXE="$(command -v python3)"
    fi
fi

# The shim spawns `codex exec`; a systemd user unit gets a minimal PATH, so
# resolve the CLI now and pin it via --cli (a login shell's PATH additions
# like ~/.local/bin are not visible to the unit).
if [ -z "$CODEX_CLI" ]; then
    CODEX_CLI="$(command -v codex || true)"
fi
[ -n "$CODEX_CLI" ] || {
    echo "codex CLI not found on PATH — install + \`codex login\` first:" >&2
    echo "  https://developers.openai.com/codex/cli/" >&2
    exit 1
}

# host-gateway routes container->host traffic to the docker bridge IP, so a
# 127.0.0.1 bind is invisible to the daemon container. Bind the bridge IP —
# not 0.0.0.0, which would expose the unauthenticated shim to the LAN. From
# the host, verify with: curl http://$BIND_HOST:$PORT/health
bridge_ip="$(ip -4 addr show docker0 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -1)"
BIND_HOST="${bridge_ip:-172.17.0.1}"

mkdir -p "$(dirname "$LOG_FILE")"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
unit="$unit_dir/pseudolife-codex-shim.service"

cat > "$unit" <<EOF
[Unit]
Description=Codex extractor CLI shim (dream pass primary; E4B sidecar is fallback)
After=network-online.target

[Service]
ExecStart=$PYTHON_EXE $repo/evals/codex_shim.py --host $BIND_HOST --port $PORT --model $MODEL --health-ttl $HEALTH_TTL --cli $CODEX_CLI
WorkingDirectory=$repo
Restart=on-failure
RestartSec=60
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now pseudolife-codex-shim.service

echo "Registered + started pseudolife-codex-shim.service ($MODEL, $BIND_HOST:$PORT, health-ttl ${HEALTH_TTL}s, log $LOG_FILE)."
echo "Host-side check: curl http://$BIND_HOST:$PORT/health"
echo "User services start at login; to start at BOOT (before login) run:"
echo "  loginctl enable-linger $USER"
echo "Cutover env for the daemon (ops/.env):"
echo "  PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$PORT/v1"
echo "  PSEUDOLIFE_DREAM_MODEL=extractor"
echo "  PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1"
echo "  PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor"
echo "  PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto"
echo "Then redeploy (ops/update.sh) and verify: memory_dream(action=\"status\")"
echo "should show fallback_url set and primary_healthy: true."
