#!/usr/bin/env bash
# Publish Linux availability with an existing controller account's gh login.
# Run as root: setup-linux-pool-health.sh --account <controller-account>
# Does not replace its credential or change the pool's kill switch.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 1; fi
if [ "$#" -ne 2 ] || [ "$1" != --account ] || ! [[ "$2" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  echo "usage: setup-linux-pool-health.sh --account <existing-controller-account>" >&2
  exit 1
fi
account=$2
id "$account" >/dev/null
if [ "$(id -u "$account")" -eq 0 ]; then echo "choose an existing unprivileged controller account" >&2; exit 1; fi
command -v python3 >/dev/null
service_path=/usr/local/bin:/usr/bin:/bin
account_home=$(getent passwd "$account" | cut -d: -f6)
if ! runuser -u "$account" -- env -i HOME="$account_home" USER="$account" LOGNAME="$account" PATH="$service_path" gh auth token >/dev/null 2>&1; then
  echo "the selected account's existing gh credential is unavailable; choose its owning account; nothing changed" >&2
  exit 1
fi
here=$(cd "$(dirname "$0")" && pwd)
lib=/usr/local/lib/pseudolife-linux-pool-health
install -d -m 0755 "$lib"
install -m 0644 "$here/linux_pool_health.py" "$here/runner_watch.py" "$lib/"

cat > /etc/systemd/system/pseudolife-linux-pool-health.service <<UNIT
[Unit]
Description=Publish Linux parity pool availability
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$account
Environment="HOME=$account_home" "PATH=$service_path"
UnsetEnvironment=GH_TOKEN GITHUB_TOKEN GH_CONFIG_DIR GH_HOST XDG_CONFIG_HOME
WorkingDirectory=/
ExecStart=/usr/bin/python3 -I $lib/linux_pool_health.py --gh-auth
TimeoutStartSec=90s
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
UNIT
cat > /etc/systemd/system/pseudolife-linux-pool-health.timer <<UNIT
[Unit]
Description=Refresh Linux parity pool availability every minute

[Timer]
OnBootSec=15s
OnUnitInactiveSec=60s

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now pseudolife-linux-pool-health.timer >/dev/null
echo "Linux health publisher installed; the pool remains controlled by PARITY_BATCH_LINUX_POOL"
