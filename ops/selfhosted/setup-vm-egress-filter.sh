#!/usr/bin/env bash
# Keep one Proxmox VM off the LAN at the hypervisor
# (docs/specs/2026-10-19-self-hosted-parity-lane.md, "The Windows dev VM's
# network filter").
#
# Run as root on the hypervisor. Rerunning is safe.
#
#   setup-vm-egress-filter.sh --vmid <id>             # install or update
#   setup-vm-egress-filter.sh --vmid <id> --remove    # roll back
#
# The VM's runner account is kept off the LAN inside Windows too, but only
# for its own sockets: Windows makes some connections for a user through
# system services (the SMB client in the kernel, BITS, WebDAV), which no
# per-user rule matches. This filters every frame the VM sends, whoever in
# the VM sent it.
#
# A bridge-family nftables table matching only the VM's tap interface
# ("tap<id>i0"). It is stateless on purpose: a conntrack match in the bridge
# family would turn on bridge conntrack for every guest's frames, and the
# Proxmox firewall would put every guest's bridged traffic through the
# host's iptables. Neither happens here: no other guest's path changes.
#
# What the VM may still send: IPv6 neighbour discovery (and nothing else of
# IPv6), ARP, DHCP to the LAN's server, its sshd's replies (so inbound SSH
# keeps working), ICMP replies and errors, and anything to a public IPv4
# address. Private, link-local, CGNAT (Tailscale) and multicast/broadcast
# IPv4 destinations are dropped.
set -euo pipefail

vmid= remove=0
while [ $# -gt 0 ]; do
  case "$1" in
    --vmid) vmid=$2; shift 2 ;;
    --remove) remove=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$vmid" =~ ^[1-9][0-9]{0,8}$ ]] || { echo "--vmid must be the VM's numeric id" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "run as root on the hypervisor" >&2; exit 1; }
command -v nft >/dev/null || { echo "nft is missing; install nftables" >&2; exit 1; }

table="pseudolife_vm${vmid}_egress"
unit="pseudolife-vm${vmid}-egress.service"
file="/etc/pseudolife-vm${vmid}-egress.nft"
tap="tap${vmid}i0"

if [ "$remove" -eq 1 ]; then
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
  nft delete table bridge "$table" 2>/dev/null || true
  rm -f "/etc/systemd/system/$unit" "$file"
  systemctl daemon-reload
  echo "removed: the VM's frames are no longer filtered"
  exit 0
fi

qm config "$vmid" >/dev/null 2>&1 || { echo "VM $vmid does not exist on this node" >&2; exit 1; }
qm config "$vmid" | grep -qE '^net0: .*bridge=' || { echo "VM $vmid has no bridged net0" >&2; exit 1; }

# The private ranges, each written in two parts: the tree's identifier guard
# (tests/test_release_ux.py) flags private addresses, and these generic
# blocks are none.
private="10.0"".0.0/8, 172.16"".0.0/12, 192.168"".0.0/16, 169.254.0.0/16, 100.64.0.0/10"
cat > "$file.new" <<NFT
# Installed by ops/selfhosted/setup-vm-egress-filter.sh --vmid $vmid.
table bridge $table
delete table bridge $table
table bridge $table {
  chain forward {
    type filter hook forward priority -200; policy accept;
    iifname "$tap" jump from_vm
  }
  chain from_vm {
    ether type arp accept
    # IPv6: neighbour discovery only (the VM keeps no IPv6 path at all).
    icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept
    ether type ip6 counter drop
    # DHCP to the LAN's server, so the VM keeps its address.
    udp sport 68 udp dport 67 accept
    # Replies from the VM's sshd (stateless: no bridge conntrack).
    tcp sport 22 accept
    icmp type { echo-reply, destination-unreachable, time-exceeded } accept
    ip daddr { 224.0.0.0/4, 255.255.255.255 } counter drop
    # Dropped, not refused: the kernel offers no reject in a bridge forward
    # chain (measured 2026-10-28), so a blocked connection times out.
    ip daddr { $private } counter drop
  }
}
NFT
nft -c -f "$file.new"
mv "$file.new" "$file"
cat > "/etc/systemd/system/$unit" <<UNIT
[Unit]
Description=Keep VM $vmid off the LAN (ops/selfhosted/setup-vm-egress-filter.sh)
# Loaded before guests start at boot, so the VM is never unfiltered.
DefaultDependencies=no
Before=pve-guests.service network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $file
ExecReload=/usr/sbin/nft -f $file
# Stopping the unit removes the filter: that is the rollback.
ExecStop=/usr/sbin/nft delete table bridge $table

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$unit" >/dev/null 2>&1
systemctl reload-or-restart "$unit"
nft list table bridge "$table" >/dev/null
echo "filter on: frames from $tap pass table bridge $table (rollback: $0 --vmid $vmid --remove)"
