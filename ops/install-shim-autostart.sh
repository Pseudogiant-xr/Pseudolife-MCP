#!/usr/bin/env bash
# Register the Claude extractor shim as a systemd --user service (Linux
# parity for ops/install-shim-autostart.ps1 — issue #11).
#
#   ops/install-shim-autostart.sh                 # default port 8082, v5 prompt, opus
#   ops/install-shim-autostart.sh --model claude-sonnet-5   # pick the served model
#
# The unit runs `ops/shim_autostart.py run claude --foreground`, which reads
# the model, prompt file, port, host, CLI path and interpreter from ops/.env
# (PSEUDOLIFE_CLAUDE_SHIM_*) at every start; the flags here are written into
# that file. Changing a value later is an edit plus
# `python ops/shim_autostart.py restart claude`.
#   Claude models: claude-opus-5-5 (default), claude-opus-5, claude-sonnet-5,
#   claude-haiku-4-5, claude-fable-5 (the list ops/install.sh offers; any other
#   claude-* id passes to the shim unchanged)
#
# The shim wraps the Max-plan `claude` CLI as an OpenAI-compatible endpoint on
# 127.0.0.1 for the daemon's dream pass (primary extractor; the in-stack E4B
# container is the fallback — see docs/superpowers/specs/
# 2026-07-11-sonnet-sidecar-cutover-design.md). Requires a logged-in CLI.
# --model default is claude-opus-5-5 since 2026-09-29: it clears the
# paired extraction-ladder gate with no regression against claude-opus-5
# (evals/results/ladder-opus55-paired-verdict-threshold.json, 2026-09-28).
# claude-opus-5 was the default from the 2026-08-02 same-harness comparison
# (evals/results/dreamer-choice-verdict.json: cortex 0.885 vs 0.821, 5/0), the
# judged comparison that chose Opus over Sonnet (best measured extraction quality);
# it ran on claude-opus-5.
# --prompt-file default is sonnet_extractor_v5.md since 2026-09-07: the
# v2 body with its two pre-rule worked examples re-cut on invented names (the
# same re-cut the daemon's v12 base took on 2026-09-07), plus the
# assistant-facts blocks that shipped in dream.py on 2026-09-05.
# --system-prompt-file REPLACES the shipped prompt prefix, so on this path a
# daemon-side change alone never reaches the model: this file is what the
# shim actually sends. Gated on the ladder opus-5 rung (v4 vs v5, two
# replicates per arm): evals/results/ladder-shimv5-paired-verdict-threshold.json
# — gold 1.0, stale 0.0 and 16/16 claims on every run, tokens 14.1-15.7
# across both arms. The v2 -> v4 step (the assistant-facts blocks) rests on
# the earlier evals/results/ladder-shimprompt-rule2-paired-verdict-threshold.json
# (v2 vs v4, tokens 14.0-15.5 across both arms); its rule-v1 predecessor
# (ladder-shimprompt-paired-verdict-threshold.json, tokens 14.0-16.1) is
# superseded evidence and stays in the tree.
set -euo pipefail

PORT=8082
MODEL="claude-opus-5-5"
PROMPT_FILE="evals/prompts/sonnet_extractor_v5.md"
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
[ -n "$MODEL" ] || MODEL="claude-opus-5-5"

repo="$(cd "$(dirname "$0")/.." && pwd)"
command -v systemctl >/dev/null 2>&1 || {
    echo "systemctl not found — run the shim manually instead:" >&2
    echo "  python $repo/evals/claude_shim.py --port $PORT --model $MODEL --system-prompt-file $repo/$PROMPT_FILE" >&2
    exit 1
}

prompt_path="$repo/$PROMPT_FILE"
[ -f "$prompt_path" ] || { echo "prompt file not found: $prompt_path" >&2; exit 1; }

# The shim spawns `claude -p`; a systemd user unit gets a minimal PATH, so
# resolve the CLI now and pin it via --cli (a login shell's PATH additions
# like ~/.local/bin are not visible to the unit).
claude_cli="$(command -v claude || true)"
[ -n "$claude_cli" ] || { echo "claude CLI not found on PATH — install + log in first." >&2; exit 1; }

# >>> shim python >>>
# The unit's interpreter must import pseudolife_memory with its
# dependencies (the shim imports the dream system prompt from it): a bare
# python3 registered a unit that exited 1 in a restart loop (Debian 13,
# 2026-09-29). ops/shim_python.py picks one (the checkout's .venv, pipx's
# pseudolife-mcp venv, a venv it made earlier, a PATH python that imports
# the package, else a venv it creates from the checkout), verifies it the
# way the unit will use it, and says which and why; --python names one to
# verify instead. Last among the checks: a missing CLI fails faster than a
# venv build.
helper_python="$(command -v python3 || command -v python || true)"
[ -n "$helper_python" ] || { echo "no python3 on PATH to run ops/shim_python.py, which picks the shim's interpreter; install Python 3.10+ and re-run." >&2; exit 1; }
picker=("$helper_python" "$repo/ops/shim_python.py" --repo "$repo")
[ -z "$PYTHON_EXE" ] || picker+=(--python "$PYTHON_EXE")
PYTHON_EXE="$("${picker[@]}")" || { echo "shim autostart not registered: no interpreter for the unit (see above)." >&2; exit 1; }
# <<< shim python <<<

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

# The values live in ops/.env; the unit's command line names only the runner.
"$PYTHON_EXE" "$repo/ops/shim_autostart.py" config claude --model "$MODEL" --port "$PORT" \
    --prompt-file "$PROMPT_FILE" --host "$BIND_HOST" --cli "$claude_cli" --python "$PYTHON_EXE" --log "$LOG_FILE"
cat > "$unit" <<EOF
[Unit]
Description=Claude extractor CLI shim (dream pass primary; E4B sidecar is fallback)
After=network-online.target

[Service]
ExecStart="$PYTHON_EXE" "$repo/ops/shim_autostart.py" run claude --foreground
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

echo "Registered + started pseudolife-sonnet-shim.service ($MODEL, $BIND_HOST:$PORT, python $PYTHON_EXE, log $LOG_FILE)."
echo "To change the model, prompt file, port or CLI later: edit the PSEUDOLIFE_CLAUDE_SHIM_* lines in ops/.env, then run: python ops/shim_autostart.py restart claude"
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
