#!/usr/bin/env bash
# Install ops/selfhosted/runner_watch.py as a systemd timer on a Linux box
# that is not itself a parity runner (a machine that goes down cannot take
# itself out of the variable).
#
# Run as root from a checkout, or with runner_watch.py beside this script:
#
#   setup-runner-watch.sh            # install or update the watcher
#   setup-runner-watch.sh --token    # then paste the token on stdin
#   setup-runner-watch.sh --machines linux,windows
#                                    # the machines nightly jobs may route to
#   setup-runner-watch.sh --machines linux,windows:on-demand
#                                    # windows's runners are VMs started per job
#
# The allowlist (/etc/pseudolife-runner-watch/machines) keeps a machine whose
# runner is online for a trial dispatch out of the variable until the trial
# passes. Without the file every online machine is listed. A machine marked
# :on-demand (written as "<machine> on-demand") also stays listed while its
# orchestrator's heartbeat variable, PARITY_HEARTBEAT_<MACHINE>, is fresh.
#
# The token is a fine-grained token for this repository alone with
# "Administration: read" and "Variables: read and write" (which also reads
# the heartbeats). It is stored
# readable by the watcher's own system account only. Until it exists the
# timer's runs are skipped. Rerunning is safe.
set -euo pipefail

ACCOUNT=pseudolife-runner-watch
LIB=/usr/local/lib/pseudolife-runner-watch
CONFIG=/etc/pseudolife-runner-watch
TOKEN=$CONFIG/token
MACHINES=$CONFIG/machines

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 1; fi
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

if [ "${1:-}" = "--token" ]; then
  [ -d "$CONFIG" ] || { echo "install the watcher first (run without --token)" >&2; exit 1; }
  token=
  read -r token || true
  # A token piped from Windows ends in a carriage return.
  token=${token//[$'\r\t ']/}
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

if [ "${1:-}" = "--machines" ]; then
  [ -d "$CONFIG" ] || { echo "install the watcher first (run without --machines)" >&2; exit 1; }
  # The list is required, so a forgotten argument never empties it; '' is
  # the explicit way to allow no machine.
  [ $# -ge 2 ] || { echo "--machines needs a list such as linux,windows (or '' for none); nothing changed" >&2; exit 2; }
  list=$2
  if [[ -n "$list" && ! "$list" =~ ^[a-z0-9-]+(:on-demand)?(,[a-z0-9-]+(:on-demand)?)*$ ]]; then
    echo "--machines takes a comma-separated list of machine names, each optionally ending in :on-demand, such as linux,windows:on-demand (or '' for none); nothing changed" >&2
    exit 2
  fi
  # The watcher refuses a machine named both ways, which would freeze the
  # variable on every run, so refuse it here before the allowlist changes.
  both=$(tr ',' '\n' <<<"$list" | sort -u | sed 's/:on-demand$//' | uniq -d | paste -sd, -)
  if [ -n "$both" ]; then
    echo "--machines names $both both with and without :on-demand; keep one; nothing changed" >&2
    exit 2
  fi
  # An older watcher would read "windows on-demand" as one machine's name.
  if [[ "$list" == *:on-demand* ]] && ! grep -qs '^ON_DEMAND = "on-demand"$' "$LIB/runner_watch.py"; then
    echo "the installed watcher does not know :on-demand; run $0 without arguments to update it, then rerun this; nothing changed" >&2
    exit 1
  fi
  { echo "# Machines nightly parity jobs may route to (setup-runner-watch.sh --machines).";
    echo "# '<machine> on-demand' also stays listed while PARITY_HEARTBEAT_<MACHINE> is fresh.";
    [ -z "$list" ] || tr ',' '\n' <<<"$list" | sed 's/:on-demand$/ on-demand/'; } > "$MACHINES.part"
  chown "root:$ACCOUNT" "$MACHINES.part"
  chmod 0640 "$MACHINES.part"
  mv "$MACHINES.part" "$MACHINES"
  echo "allowlist: ${list:-none}"
  systemctl start pseudolife-runner-watch.service || true
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
ExecStart=/usr/bin/python3 -I $LIB/runner_watch.py --token-file $TOKEN --state-file /var/lib/pseudolife-runner-watch/last-seen.json --machines-file $MACHINES
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
