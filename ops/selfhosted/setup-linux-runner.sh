#!/usr/bin/env bash
# Set up self-hosted parity runners on a Linux dev box
# (docs/specs/2026-10-19-self-hosted-parity-lane.md, "The sibling Linux dev box").
#
# Run as root on the box. Rerunning is safe: each step checks first.
#
#   setup-linux-runner.sh --runner-user <name> --suite-user <name> \
#     --lease full-suite@<machine> --runners <n> --version <x.y.z> --sha256 <hex> \
#     [--name-prefix parity-linux]
#
# RUNNER_TOKEN in the environment (a registration token from
# `gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token`)
# registers runners that are not configured yet. Without it the script
# prepares everything else and says which runners still need one.
#
# What it sets up:
# - one unprivileged account for every runner on the box, without sudo or a
#   docker group, and with no access to the suite user's files beyond the
#   lock directory;
# - rootless Docker for that account, so the w1c-connection shard's image
#   builds and containers stay out of the box's root Docker engine;
# - an nftables rule refusing that account's traffic to private, link-local
#   and CGNAT (Tailscale) addresses: jobs reach the internet, not the LAN;
# - <n> runners under that account, each with its own HOME, so one job's
#   start hook never removes another job's PostgreSQL directories;
# - Python 3.11 in a shared tool cache, because actions/setup-python ships
#   builds for Ubuntu only.
set -euo pipefail

REPO_URL=https://github.com/Pseudogiant-xr/Pseudolife-MCP
LABEL=pseudolife-parity-linux
# Subordinate IDs for rootless Docker: a full 65536, so an image build can
# switch to any ID a Debian image uses (apt drops to nogroup, 65534). The
# range lies past the 100000+65536 that useradd gives login accounts. An
# unprivileged Proxmox container maps only IDs 0-65535 by default; the
# script refuses until the container's ID map covers this range.
SUBID_START=165536
SUBID_COUNT=65536
LIB=/usr/local/lib/pseudolife-parity-runner
# Each runner's directory sits in a root-owned parent, so a job can change a
# runner directory's contents but never swap the directory itself for a
# link before root works on it.
RUNNERS=/srv/pseudolife-parity-runners

runner_user= suite_user= lease= runners= version= sha256= prefix=parity-linux
while [ $# -gt 0 ]; do
  case "$1" in
    --runner-user) runner_user=$2; shift 2 ;;
    --suite-user) suite_user=$2; shift 2 ;;
    --lease) lease=$2; shift 2 ;;
    --runners) runners=$2; shift 2 ;;
    --version) version=$2; shift 2 ;;
    --sha256) sha256=$2; shift 2 ;;
    --name-prefix) prefix=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
for setting in runner_user suite_user lease runners version sha256; do
  if [ -z "${!setting}" ]; then echo "missing --${setting//_/-}" >&2; exit 2; fi
done
if [[ ! "$lease" =~ ^full-suite@[A-Za-z0-9._-]{1,40}$ ]]; then
  echo "--lease must be full-suite@<machine>" >&2; exit 2
fi
if [[ ! "$runners" =~ ^[1-9]$ ]]; then echo "--runners must be 1-9" >&2; exit 2; fi
if [[ ! "$prefix" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]]; then echo "--name-prefix must be lowercase letters, digits and hyphens" >&2; exit 2; fi
if [[ ! "$runner_user" =~ ^[a-z][a-z0-9-]{0,30}$ ]]; then echo "--runner-user must be a plain account name" >&2; exit 2; fi
id "$suite_user" >/dev/null 2>&1 || { echo "--suite-user $suite_user is not an account on this machine" >&2; exit 2; }
[ "$runner_user" != "$suite_user" ] || { echo "--runner-user must be an account of its own, not the suite user" >&2; exit 2; }
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "--version must look like 2.338.1" >&2; exit 2; }
[[ "$sha256" =~ ^[0-9a-f]{64}$ ]] || { echo "--sha256 must be 64 lowercase hex digits" >&2; exit 2; }
if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 1; fi
command -v pwsh >/dev/null || { echo "install PowerShell 7 first (the shards' steps use shell: pwsh)" >&2; exit 1; }
for map in /proc/self/uid_map /proc/self/gid_map; do
  if ! awk -v s=$SUBID_START -v e=$((SUBID_START + SUBID_COUNT)) '$1 <= s && $1 + $3 >= e {found=1} END {exit !found}' "$map"; then
    echo "this machine's $map does not cover IDs $SUBID_START-$((SUBID_START + SUBID_COUNT - 1)), which rootless Docker needs;" \
      "on a Proxmox container, map IDs 0-$((SUBID_START + SUBID_COUNT - 1)) (lxc.idmap: u 0 100000 $((SUBID_START + SUBID_COUNT))" \
      "and the same for g, with root's range in the host's /etc/subuid and /etc/subgid widened to match), restart it, and rerun" >&2
    exit 1
  fi
done
for tool in docker dockerd-rootless-setuptool.sh; do
  command -v "$tool" >/dev/null || { echo "install Docker Engine with its rootless extras first ($tool is missing)" >&2; exit 1; }
done

# Programs run as the account may be ones a job replaced, so they get only
# the variables named here: root's environment (a registration token
# included) is dropped while still root, before runuser starts anything.
run_as() { env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 runuser -u "$runner_user" -- env -C "$runner_home" \
  HOME="$runner_home" XDG_RUNTIME_DIR="/run/user/$runner_uid" LANG=C.UTF-8 \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$runner_uid/bus" PATH="$runner_home/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }

echo "== packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq uidmap slirp4netns dbus-user-session acl nftables curl >/dev/null
locales=$(locale -a)
grep -qi '^en_US.utf8$' <<<"$locales" || { sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen; locale-gen; }

echo "== account"
if ! id "$runner_user" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$runner_user"
fi
runner_uid=$(id -u "$runner_user")
runner_home=$(getent passwd "$runner_user" | cut -d: -f6)
uid_min=$(awk '$1 == "UID_MIN" {print $2}' /etc/login.defs 2>/dev/null)
if [ "$runner_uid" -lt "${uid_min:-1000}" ] || [ "$(passwd -S "$runner_user" | awk '{print $2}')" != L ] \
   || [ "$(id -nG "$runner_user")" != "$runner_user" ] || [ "$(id -g "$runner_user")" -lt "${uid_min:-1000}" ]; then
  echo "$runner_user must be an ordinary account with a locked password and only its own group;" \
    "pick a new name and the script creates it" >&2
  exit 1
fi
chmod 0700 "$runner_home"
# Nothing of the account's runs outside the runner units and its user
# manager, both of which wait for the egress table.
for deny in /etc/cron.deny /etc/at.deny; do
  grep -qx "$runner_user" "$deny" 2>/dev/null || echo "$runner_user" >> "$deny"
done
if awk -F: -v s=$SUBID_START -v e=$((SUBID_START + SUBID_COUNT)) '$3>=s && $3<e {found=1} END {exit !found}' /etc/passwd; then
  echo "an account's UID falls in the subordinate range $SUBID_START+$SUBID_COUNT" >&2; exit 1
fi
# useradd's default range may not be this one, so the account's range is
# replaced rather than kept. A changed range leaves the rootless Docker
# storage owned by IDs the account no longer maps; it is rebuilt below.
old_subids=$(grep "^$runner_user:" /etc/subuid /etc/subgid 2>/dev/null | cut -d: -f2- | sort | paste -sd' ' -)
for file in /etc/subuid /etc/subgid; do
  if awk -F: -v me="$runner_user" -v s=$SUBID_START -v e=$((SUBID_START + SUBID_COUNT)) \
       '$1 != me && $2 < e && $2 + $3 > s {found=1} END {exit !found}' "$file"; then
    echo "another account's range in $file overlaps $SUBID_START+$SUBID_COUNT" >&2; exit 1
  fi
done
for file in /etc/subuid /etc/subgid; do
  sed -i "/^$runner_user:/d" "$file"
  echo "$runner_user:$SUBID_START:$SUBID_COUNT" >> "$file"
done
new_subids=$(grep "^$runner_user:" /etc/subuid /etc/subgid | cut -d: -f2- | sort | paste -sd' ' -)
loginctl enable-linger "$runner_user"
for _ in $(seq 30); do [ -S "/run/user/$runner_uid/bus" ] && break; sleep 1; done

echo "== suite lock directory"
suite_home=$(getent passwd "$suite_user" | cut -d: -f6)
[[ "$suite_home" =~ ^/[A-Za-z0-9/._-]+$ ]] || { echo "the suite user's home path has characters this script does not quote" >&2; exit 1; }
locks="$suite_home/.pseudolife-mcp/locks"
runuser -u "$suite_user" -- mkdir -p "$locks"
[ -s "$locks/full-suite.lease" ] || runuser -u "$suite_user" -- sh -c "echo '$lease' > '$locks/full-suite.lease'"
named=$(head -n 1 "$locks/full-suite.lease")
[ "$named" = "$lease" ] || echo "note: $locks/full-suite.lease names $named, not $lease; the runners use $lease"
[ -e "$locks/full-suite.lock" ] || runuser -u "$suite_user" -- touch "$locks/full-suite.lock"
# The account reads the lock directory through a read-only bind mount, so it
# needs no search right on the suite user's home, where it could otherwise
# read any file whose path it guessed.
setfacl -x "u:$runner_user" "$suite_home" "$suite_home/.pseudolife-mcp" 2>/dev/null || true
setfacl -m "u:$runner_user:rx" "$locks"
setfacl -d -m "u:$runner_user:r" "$locks"
find "$locks" -maxdepth 1 -type f -exec setfacl -m "u:$runner_user:r" {} +
runner_locks=$LIB/locks
install -d -m 0755 "$LIB"
mountpoint -q "$runner_locks" || install -d -m 0755 "$runner_locks"
cat > /etc/systemd/system/pseudolife-parity-locks.service <<UNIT
[Unit]
Description=The suite lock directory, read-only, for the parity runners

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'mountpoint -q $runner_locks || { mount --bind $locks $runner_locks && mount -o remount,bind,ro $runner_locks; }'
ExecStop=/bin/umount $runner_locks

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable pseudolife-parity-locks.service >/dev/null 2>&1
systemctl start pseudolife-parity-locks.service
[ -e "$runner_locks/full-suite.lock" ] || { echo "the lock directory bind mount failed" >&2; exit 1; }

echo "== egress: no LAN for $runner_user"
while read -r keyword server _ || [ -n "${keyword:-}" ]; do
  [ "$keyword" = nameserver ] || continue
  if [[ "$server" == *:* || "$server" =~ ^(127\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|169\.254\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]; then
    echo "resolver $server is IPv6, local, private or on the tailnet, which the runner account may not reach;" \
      "point /etc/resolv.conf at a public IPv4 resolver first" >&2
    exit 1
  fi
done < /etc/resolv.conf
# The account's subordinate IDs count too: a job can map itself onto one in
# a user namespace that keeps this network namespace.
ids="$runner_uid, $SUBID_START-$((SUBID_START + SUBID_COUNT - 1))"
# This box's own services (any socket listening now that another account
# owns) are refused on every local address. A port in use cannot be one a
# job's own server binds; rerun this script after adding a service.
# Prints "any <port>" for a wildcard listener, "4 <address> <port>" or
# "6 <address> <port>" for one bound to a loopback address. Listeners on
# other addresses are already covered by the ranges above. A listener bound
# to one address is refused on that address only, so a job's own server on
# another loopback address and the same port still works.
listening() {
  ss "-Hl${1}ne" | awk -v me="$runner_uid" -v sub_low="$SUBID_START" -v sub_high="$((SUBID_START + SUBID_COUNT - 1))" '{
    owner = 0
    for (i = 1; i <= NF; i++) if ($i ~ /^uid:/) owner = substr($i, 5) + 0
    if (owner == me || (owner >= sub_low && owner <= sub_high)) next
    local = $4; port = local; sub(/.*:/, "", port); address = substr(local, 1, length(local) - length(port) - 1)
    sub(/%.*/, "", address); gsub(/[][]/, "", address)
    if (port !~ /^[0-9]+$/) next
    if (address == "*" || address == "0.0.0.0" || address == "::") { print "any", port; next }
    if (address ~ /^127\./) print 4, address, port
    else if (address == "::1") print 6, address, port
  }' | sort -u
}
local_rules=""
for proto in t u; do
  name=$([ "$proto" = t ] && echo tcp || echo udp)
  any=$(listening "$proto" | awk '$1 == "any" {print $2}' | sort -nu | paste -sd, -)
  [ -z "$any" ] || local_rules+="    meta skuid { $ids } fib daddr type local $name dport { $any } counter reject
"
  while read -r family address port; do
    match=$([ "$family" = 4 ] && echo "ip daddr $address" || echo "ip6 daddr $address")
    local_rules+="    meta skuid { $ids } $match $name dport $port counter reject
"
  done < <(listening "$proto" | awk '$1 != "any"')
done
# The private ranges, each written in two parts: the tree's identifier
# guard (tests/test_release_ux.py) flags private addresses, and these
# generic blocks are none.
private="10.0"".0.0/8, 172.16"".0.0/12, 192.168"".0.0/16"
cat > /etc/pseudolife-runner-egress.nft <<NFT
table inet pseudolife_runner_egress
delete table inet pseudolife_runner_egress
table inet pseudolife_runner_egress {
  chain output {
    type filter hook output priority 0; policy accept;
    meta skuid { $ids } ip daddr { $private, 169.254.0.0/16, 100.64.0.0/10, 224.0.0.0/4, 255.255.255.255 } counter reject
    meta skuid { $ids } meta nfproto ipv6 ip6 daddr != ::1 counter reject
$local_rules  }
}
NFT
nft -c -f /etc/pseudolife-runner-egress.nft
# The stock /etc/nftables.conf starts with "flush ruleset", so a reload of
# nftables.service would drop the table until this unit reloaded it after.
# Including the table there loads it in the same transaction as the flush,
# so the account is never unfiltered.
nft_conf=/etc/nftables.conf
include_line='include "/etc/pseudolife-runner-egress.nft"'
if [ -f "$nft_conf" ] && ! grep -qxF "$include_line" "$nft_conf"; then
  cp -p "$nft_conf" "$nft_conf.before-parity-runner"
  printf '\n# The parity runner account'\''s egress table (ops/selfhosted/setup-linux-runner.sh).\n%s\n' \
    "$include_line" >> "$nft_conf"
  if ! nft -c -f "$nft_conf"; then
    cp -p "$nft_conf.before-parity-runner" "$nft_conf"
    echo "$nft_conf does not check with the egress table included; it is restored unchanged" >&2
    exit 1
  fi
fi
cat > /etc/systemd/system/pseudolife-runner-egress.service <<UNIT
[Unit]
Description=Keep the parity runner account off the local network
# Loaded before any network comes up. /etc/nftables.conf includes the table
# (above), so a reload or restart of nftables.service reloads it with the
# rest of the ruleset; a restart stops the runners (PartOf=), so rerun this
# script after one.
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target
After=nftables.service
ReloadPropagatedFrom=nftables.service
PartOf=nftables.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/pseudolife-runner-egress.nft
# The file replaces the table in one transaction, so a reload never leaves
# the account unfiltered. There is no ExecStop: stopping the unit leaves the
# table in place, because processes outside the runner units (the account's
# rootless Docker and its containers) would otherwise lose the filter.
# Removing it is a deliberate `nft delete table inet pseudolife_runner_egress`.
ExecReload=/usr/sbin/nft -f /etc/pseudolife-runner-egress.nft

[Install]
WantedBy=multi-user.target
UNIT
# The account's user manager starts its rootless Docker; at boot it waits
# for the table, and does not start if the table fails to load.
install -d -m 0755 "/etc/systemd/system/user@$runner_uid.service.d"
cat > "/etc/systemd/system/user@$runner_uid.service.d/pseudolife-runner-egress.conf" <<UNIT
[Unit]
Requires=pseudolife-runner-egress.service
After=pseudolife-runner-egress.service
UNIT
systemctl daemon-reload
systemctl enable pseudolife-runner-egress.service >/dev/null 2>&1
systemctl reload-or-restart pseudolife-runner-egress.service

echo "== tools for $runner_user"
if [ ! -x "$runner_home/.local/bin/uv" ]; then
  run_as sh -c 'curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh' >/dev/null
fi
run_as uv tool install --quiet --upgrade pseudolife-mcp >/dev/null
run_as pseudolife-mcp lease check --help >/dev/null

echo "== rootless Docker"
docker_data="$runner_home/.local/share/docker"
if [ -n "$old_subids" ] && [ "$old_subids" != "$new_subids" ] && [ -e "$docker_data" ]; then
  echo "the account's subordinate IDs changed; rebuilding its rootless Docker storage (images and build cache)"
  run_as systemctl --user stop docker.service || true
  # Its files belong to IDs the account no longer maps, so only root can
  # remove them; rm does not follow symbolic links inside the tree.
  rm -rf --one-file-system "$docker_data"
fi
if ! run_as systemctl --user is-active --quiet docker; then
  run_as dockerd-rootless-setuptool.sh install
fi
run_as env DOCKER_HOST="unix:///run/user/$runner_uid/docker.sock" docker info --format '{{.SecurityOptions}}' | grep -q rootless \
  || { echo "rootless Docker did not come up" >&2; exit 1; }

echo "== Python 3.11 tool cache"
toolcache="$runner_home/toolcache"
if ! ls "$toolcache"/Python/3.11.*/x64.complete >/dev/null 2>&1; then
  run_as uv python install --quiet 3.11
  source_dir=$(dirname "$(dirname "$(readlink -f "$(run_as uv python find 3.11 --managed-python)")")")
  full=$(run_as "$source_dir/bin/python3" -c 'import platform; print(platform.python_version())')
  run_as mkdir -p "$toolcache/Python/$full"
  run_as cp -a "$source_dir" "$toolcache/Python/$full/x64"
  run_as rm -f "$toolcache/Python/$full/x64/lib/python3.11/EXTERNALLY-MANAGED"
  run_as touch "$toolcache/Python/$full/x64.complete"
fi
# The shards' steps call `python`, which Debian does not provide.
for bin in "$toolcache"/Python/3.11.*/x64/bin; do
  [ -e "$bin/python" ] || run_as ln -s python3 "$bin/python"
done

echo "== runners"
# Root never runs, or writes into, a file a job could have replaced: the
# release is root's, registration runs from a fresh root-owned extraction,
# the hook and the service units are root's, and .env and .path are written
# as the account.
tarball="$LIB/actions-runner-linux-x64-$version.tar.gz"
if [ ! -f "$tarball" ]; then
  curl -fsSL -o "$tarball.part" "https://github.com/actions/runner/releases/download/v$version/actions-runner-linux-x64-$version.tar.gz"
  echo "$sha256  $tarball.part" | sha256sum -c --quiet - || { rm -f "$tarball.part"; echo "runner checksum mismatch" >&2; exit 1; }
  mv "$tarball.part" "$tarball"
fi
hook=$LIB/job-started.sh
install -d -m 0755 -o root -g root "$RUNNERS"
cat > "$hook.part" <<'HOOK'
#!/usr/bin/env bash
# Remove the previous job's PostgreSQL data directories under this runner's
# own HOME. It stops only a postgres owned by this account whose working
# directory is the cluster's own data directory (a postmaster runs in its
# data directory), so a reused PID belonging to another runner's cluster
# is left alone.
set -u
me=$(id -un)
for dir in "$HOME"/pl-cli-lease-postgres-*/; do
  [ -d "$dir" ] || continue
  while IFS= read -r pidfile; do
    pid=$(head -n 1 "$pidfile")
    data=$(dirname "$(readlink -f "$pidfile")")
    if [[ "$pid" =~ ^[0-9]+$ ]] && [ "$(stat -c %U "/proc/$pid" 2>/dev/null)" = "$me" ] \
       && [ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" = "$data" ]; then
      kill -INT "$pid"
      for _ in $(seq 30); do [ -d "/proc/$pid" ] || break; sleep 1; done
    fi
  done < <(find "$dir" -maxdepth 4 -name postmaster.pid)
  rm -rf -- "$dir"
done
exit 0
HOOK
chmod 0755 "$hook.part"
mv "$hook.part" "$hook"
missing=()
for index in $(seq "$runners"); do
  dir="$RUNNERS/$index"
  home="$runner_home/homes/$index"
  service="pseudolife-$prefix-$index.service"
  if [ ! -f "$dir/.runner" ]; then
    if [ -z "${RUNNER_TOKEN:-}" ]; then missing+=("$prefix-$index"); continue; fi
    # A fresh extraction, owned by root until registration is done, so no
    # file a job wrote ever sees the token. config.sh reads the token from
    # its environment, keeping it off every command line.
    systemctl stop "$service" 2>/dev/null || true
    rm -rf -- "$dir"
    install -d -m 0700 "$dir"
    tar xzf "$tarball" -C "$dir"
    (
      cd "$dir"
      export ACTIONS_RUNNER_INPUT_TOKEN="$RUNNER_TOKEN" RUNNER_ALLOW_RUNASROOT=1
      ./config.sh --unattended --replace --url "$REPO_URL" --name "$prefix-$index" \
        --labels "$LABEL" --work _work </dev/null
    )
    chown -R "$runner_user:" "$dir"
  fi
  run_as mkdir -p "$home"
  # The job's HOME is the runner's own, so lease_ci.py's data directories
  # and the start hook stay per runner.
  before=$(run_as sh -c 'cat "$1/.env" "$1/.path" 2>/dev/null' sh "$dir" ; cat "/etc/systemd/system/pseudolife-$prefix-$index.service" 2>/dev/null || true)
  run_as tee "$dir/.env" >/dev/null <<ENV
LANG=en_US.UTF-8
HOME=$home
PSEUDOLIFE_PARITY_LEASE=$lease
PSEUDOLIFE_PARITY_LOCK_DIR=$runner_locks
ACTIONS_RUNNER_HOOK_JOB_STARTED=$hook
AGENT_TOOLSDIRECTORY=$toolcache
RUNNER_TOOL_CACHE=$toolcache
DOCKER_HOST=unix:///run/user/$runner_uid/docker.sock
XDG_RUNTIME_DIR=/run/user/$runner_uid
ENV
  echo "$runner_home/.local/bin:/usr/local/bin:/usr/bin:/bin" | run_as tee "$dir/.path" >/dev/null
  run_as chmod 0600 "$dir/.credentials" "$dir/.credentials_rsaparams" "$dir/.runner"
  # The unit is written here rather than by the runner's svc.sh, which root
  # would otherwise run from the account's directory. An earlier svc.sh unit
  # for this runner is replaced.
  legacy="actions.runner.$(basename "$(dirname "$REPO_URL")")-$(basename "$REPO_URL").$prefix-$index.service"
  if [ -f "/etc/systemd/system/$legacy" ]; then
    systemctl disable --now "$legacy" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$legacy"
  fi
  cat > "/etc/systemd/system/$service" <<UNIT
[Unit]
Description=GitHub Actions parity runner $prefix-$index
After=network-online.target pseudolife-runner-egress.service pseudolife-parity-locks.service
Wants=network-online.target
BindsTo=pseudolife-runner-egress.service
Requires=pseudolife-parity-locks.service

[Service]
# Refuse to start unless the egress table is loaded.
ExecStartPre=+/usr/sbin/nft list table inet pseudolife_runner_egress
ExecStart=$dir/bin/runsvc.sh
User=$runner_user
WorkingDirectory=$dir
KillMode=process
KillSignal=SIGTERM
TimeoutStopSec=5min

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable "$service" >/dev/null 2>&1
  after=$(run_as sh -c 'cat "$1/.env" "$1/.path" 2>/dev/null' sh "$dir" ; cat "/etc/systemd/system/$service")
  if [ "$before" != "$after" ] || ! systemctl is-active --quiet "$service"; then
    systemctl restart "$service"
    echo "runner $prefix-$index: $(systemctl is-active "$service") (restarted)"
  else
    echo "runner $prefix-$index: unchanged"
  fi
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "not registered yet: ${missing[*]}; rerun with RUNNER_TOKEN set"
fi
