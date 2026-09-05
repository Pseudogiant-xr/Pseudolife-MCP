#!/usr/bin/env bash
# Register the Claude extractor shim as a systemd --user service (Linux
# parity for ops/install-shim-autostart.ps1 — issue #11).
#
#   ops/install-shim-autostart.sh                 # default port 8082, v4 prompt, opus
#   ops/install-shim-autostart.sh --model claude-sonnet-5   # pick the served model
#
# The shim wraps the Max-plan `claude` CLI as an OpenAI-compatible endpoint on
# 127.0.0.1 for the daemon's dream pass (primary extractor; the in-stack E4B
# container is the fallback — see docs/superpowers/specs/
# 2026-07-11-sonnet-sidecar-cutover-design.md). Requires a logged-in CLI.
# --model default is claude-opus-5 per the 2026-08-02 same-harness comparison
# (evals/results/dreamer-choice-verdict.json: cortex 0.885 vs 0.821, 5/0).
# --prompt-file default is sonnet_extractor_v4.md since 2026-09-05: v2 plus the
# assistant-facts blocks that shipped in dream.py that day. --system-prompt-file
# REPLACES the shipped prompt prefix, so on this path the daemon-side change
# alone never reached the model. Gated on the ladder opus-5 rung (v2 vs v4, two
# replicates per arm): evals/results/ladder-shimprompt-paired-verdict-threshold.json
# — gold 1.0, stale 0.0 on every run, tokens 14.0-16.1 across both arms.
set -euo pipefail

PORT=8082
MODEL="claude-opus-5"
PROMPT_FILE="evals/prompts/sonnet_extractor_v4.md"
PYTHON_EXE=""
LOG_FILE="$HOME/.pseudolife-mcp/claude-shim.log"

while [ $# -gt 0 ]; do
    case "$1" in
        --port)        PORT="$2"; shift 2 ;;
        --model)       MODEL="$2"; shift 2 ;;
        --prompt-file) PROMPT_FILE="$2"; shift 2 ;;
        --python)      PYTHON_EXE="$2"; shift 2 ;;
        --log-file)    LOG_FILE="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
# An empty --model (installer passthrough on sidecar mode) keeps the default.
[ -n "$MODEL" ] || MODEL="claude-opus-5"

repo="$(cd "$(dirname "$0")/.." && pwd)"
command -v systemctl >/dev/null 2>&1 || {
    echo "systemctl not found — run the shim manually instead:" >&2
    echo "  python $repo/evals/claude_shim.py --port $PORT --model $MODEL --system-prompt-file $repo/$PROMPT_FILE" >&2
    exit 1
}

if [ -z "$PYTHON_EXE" ]; then
    if [ -x "$repo/.venv/bin/python" ]; then
        PYTHON_EXE="$repo/.venv/bin/python"
    else
        PYTHON_EXE="$(command -v python3)"
    fi
fi
prompt_path="$repo/$PROMPT_FILE"
[ -f "$prompt_path" ] || { echo "prompt file not found: $prompt_path" >&2; exit 1; }

# The shim spawns `claude -p`; a systemd user unit gets a minimal PATH, so
# resolve the CLI now and pin it via --cli (a login shell's PATH additions
# like ~/.local/bin are not visible to the unit).
claude_cli="$(command -v claude || true)"
[ -n "$claude_cli" ] || { echo "claude CLI not found on PATH — install + log in first." >&2; exit 1; }

# host-gateway routes container->host traffic to the docker bridge IP, so a
# 127.0.0.1 bind is invisible to the daemon container. Bind the bridge IP —
# not 0.0.0.0, which would expose the unauthenticated shim to the LAN. From
# the host, verify with: curl http://$BIND_HOST:$PORT/health
bridge_ip="$(ip -4 addr show docker0 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -1)"
BIND_HOST="${bridge_ip:-172.17.0.1}"

mkdir -p "$(dirname "$LOG_FILE")"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
unit="$unit_dir/pseudolife-sonnet-shim.service"

cat > "$unit" <<EOF
[Unit]
Description=Claude extractor CLI shim (dream pass primary; E4B sidecar is fallback)
After=network-online.target

[Service]
ExecStart=$PYTHON_EXE $repo/evals/claude_shim.py --host $BIND_HOST --port $PORT --model $MODEL --system-prompt-file $prompt_path --cli $claude_cli
WorkingDirectory=$repo
Restart=on-failure
RestartSec=60
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now pseudolife-sonnet-shim.service

echo "Registered + started pseudolife-sonnet-shim.service ($MODEL, $BIND_HOST:$PORT, log $LOG_FILE)."
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
