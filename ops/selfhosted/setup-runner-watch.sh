#!/usr/bin/env bash
# Install ops/selfhosted/runner_watch.py as a systemd timer on a Linux box
# that is not itself a parity runner (a machine that goes down cannot take
# itself out of the variable).
#
# Run as root from a checkout, or with runner_watch.py beside this script:
#
#   setup-runner-watch.sh            # install or update the watcher
#   setup-runner-watch.sh --token    # then paste the token on stdin
#
# The token is a fine-grained token for this repository alone with
# "Administration: read" and "Variables: read and write". It is stored
# readable by the watcher's own system account only. Until it exists the
# timer's runs are skipped. Rerunning is safe.
set -euo pipefail

ACCOUNT=pseudolife-runner-watch
LIB=/usr/local/lib/pseudolife-runner-watch
CONFIG=/etc/pseudolife-runner-watch
TOKEN=$CONFIG/token

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 1; fi
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

if [ "${1:-}" = "--token" ]; then
  [ -d "$CONFIG" ] || { echo "install the watcher first (run without --token)" >&2; exit 1; }
  token=
  read -r token || true
  [ -n "$token" ] || { echo "no token on stdin; nothing changed" >&2; exit 1; }
  umask 077
  printf '%s\n' "$token" > "$TOKEN.part"
  chown "$ACCOUNT:" "$TOKEN.part"
  chmod 0400 "$TOKEN.part"
  mv "$TOKEN.part" "$TOKEN"
  if systemctl start pseudolife-runner-watch.service; then
    echo "token stored; the watcher ran: journalctl -u pseudolife-runner-watch.service shows its changes"
  else
    echo "token stored, but the watcher's run failed:" >&2
    journalctl -u pseudolife-runner-watch.service -n 6 --no-pager >&2 || true
    exit 1
  fi
  exit 0
fi

here=$(cd "$(dirname "$0")" && pwd)
id "$ACCOUNT" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$ACCOUNT"
install -d -m 0755 "$LIB"
install -m 0644 "$here/runner_watch.py" "$LIB/runner_watch.py"
install -d -m 0750 -o root -g "$ACCOUNT" "$CONFIG"

cat > /etc/systemd/system/pseudolife-runner-watch.service <<UNIT
[Unit]
Description=Keep PARITY_SELFHOSTED_RUNNERS on the online parity runners
ConditionPathExists=$TOKEN
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$ACCOUNT
StateDirectory=pseudolife-runner-watch
ExecStart=/usr/bin/python3 -I $LIB/runner_watch.py --token-file $TOKEN --state-file /var/lib/pseudolife-runner-watch/last-seen.json
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
UNIT
cat > /etc/systemd/system/pseudolife-runner-watch.timer <<UNIT
[Unit]
Description=Check the parity runners every two minutes

[Timer]
OnCalendar=*:0/2

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now pseudolife-runner-watch.timer >/dev/null
if [ -s "$TOKEN" ]; then
  echo "watcher installed and running"
else
  echo "watcher installed; it starts once the token is stored: $0 --token < token-file"
fi
