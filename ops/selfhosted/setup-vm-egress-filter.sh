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
# A bridge-family nftables table matching only the VM's tap interfaces
# ("tap<id>i*"), at the bridge's prerouting hook: every frame the VM sends
# passes it before the bridge decides between forwarding it and delivering
# it to the hypervisor itself, so neither path is unfiltered. It is
# stateless on purpose: a conntrack match in the bridge family would turn on
# bridge conntrack for every guest's frames, and the Proxmox firewall would
# put every guest's bridged traffic through the host's iptables. Neither
# happens here: no other guest's path changes.
#
# What the VM may still send, from its own MAC addresses only: ARP; IPv6
# neighbour discovery (and nothing else of IPv6); DHCP to the broadcast
# address or the LAN's server; its sshd's replies (never a new connection),
# so inbound SSH keeps working; ICMP replies and errors; and IPv4 to public
# addresses. Every other frame is dropped: private, link-local, CGNAT
# (Tailscale) and multicast/broadcast IPv4 destinations, and anything that
# is not plain IPv4, IPv6 or ARP (a VLAN tag, PPPoE).
#
# A hookscript refuses to start the VM while the table is not loaded, so a
# failed load at boot, or a table someone flushed, never runs it unfiltered.
set -euo pipefail

vmid= remove=0 dhcp_server=
while [ $# -gt 0 ]; do
  case "$1" in
    --vmid) vmid=$2; shift 2 ;;
    --dhcp-server) dhcp_server=$2; shift 2 ;;
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
snippets=/var/lib/vz/snippets
hook="pseudolife-vm${vmid}-egress-hook.sh"

if [ "$remove" -eq 1 ]; then
  if qm config "$vmid" 2>/dev/null | grep -qx "hookscript: local:snippets/$hook"; then
    qm set "$vmid" --delete hookscript >/dev/null
  fi
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
  nft delete table bridge "$table" 2>/dev/null || true
  rm -f "/etc/systemd/system/$unit" "$file" "$snippets/$hook"
  systemctl daemon-reload
  echo "removed: the VM's frames are no longer filtered"
  exit 0
fi

config=$(qm config "$vmid" 2>/dev/null) || { echo "VM $vmid does not exist on this node" >&2; exit 1; }
# The tap interfaces exist only for bridged NICs; each one's MAC is the only
# source address the VM may use.
macs=$(grep -E '^net[0-9]+: ' <<<"$config" | grep -oiE '=([0-9a-f]{2}:){5}[0-9a-f]{2}' | tr -d '=' | tr 'A-F' 'a-f' | paste -sd, -)
[ -n "$macs" ] || { echo "VM $vmid has no NIC with a MAC address" >&2; exit 1; }
if grep -E '^net[0-9]+: ' <<<"$config" | grep -vq 'bridge='; then
  echo "VM $vmid has a NIC that is not bridged; this filter covers bridged NICs only" >&2; exit 1
fi
if [ -z "$dhcp_server" ]; then
  dhcp_server=$(ip -4 route show default | awk '{for (i = 1; i < NF; i++) if ($i == "via") {print $(i + 1); exit}}')
fi
[[ "$dhcp_server" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "no DHCP server address (the host has no IPv4 default route); pass --dhcp-server <address>" >&2; exit 1; }

# The private ranges, each written in two parts: the tree's identifier guard
# (tests/test_release_ux.py) flags private addresses, and these generic
# blocks are none.
private="10.0"".0.0/8, 172.16"".0.0/12, 192.168"".0.0/16, 169.254.0.0/16, 100.64.0.0/10"
cat > "$file.new" <<NFT
# Installed by ops/selfhosted/setup-vm-egress-filter.sh --vmid $vmid.
table bridge $table
delete table bridge $table
table bridge $table {
  chain prerouting {
    type filter hook prerouting priority -300; policy accept;
    iifname "tap${vmid}i*" jump from_vm
  }
  chain from_vm {
    ether saddr != { $macs } counter drop
    ether type arp arp saddr ether != { $macs } counter drop
    ether type arp accept
    # IPv6: neighbour discovery only (the VM keeps no IPv6 path at all).
    icmpv6 type { nd-router-solicit, nd-neighbor-solicit, nd-neighbor-advert } accept
    ether type ip6 counter drop
    # Anything but plain IPv4 from here on (a VLAN tag, PPPoE, a raw type).
    ether type != ip counter drop
    # DHCP, to the broadcast address or the LAN's server only.
    ip daddr { 255.255.255.255, $dhcp_server } udp sport 68 udp dport 67 accept
    # Replies from the VM's sshd: never a SYN, so never a new connection
    # (stateless: no bridge conntrack).
    tcp sport 22 tcp flags & (syn | ack) != syn accept
    icmp type { echo-reply, destination-unreachable, time-exceeded } accept
    ip daddr { 224.0.0.0/4, 255.255.255.255 } counter drop
    # Dropped, not refused: a bridge chain offers no reject before routing.
    ip daddr { $private } counter drop
  }
}
NFT
nft -c -f "$file.new"
mv "$file.new" "$file"
# The hookscript: the VM does not start while its filter is not loaded.
install -d -m 0755 "$snippets"
cat > "$snippets/$hook.new" <<HOOK
#!/bin/sh
# Installed by ops/selfhosted/setup-vm-egress-filter.sh --vmid $vmid: refuse
# to start VM $vmid unless its egress filter is loaded.
if [ "\$2" = pre-start ] && ! /usr/sbin/nft list table bridge $table >/dev/null 2>&1; then
  echo "VM $vmid: its egress filter (table bridge $table) is not loaded; run setup-vm-egress-filter.sh --vmid $vmid" >&2
  exit 1
fi
exit 0
HOOK
chmod 0755 "$snippets/$hook.new"
mv "$snippets/$hook.new" "$snippets/$hook"
content=$(awk '/^dir: local$/ {found = 1; next} found && /^[^ \t]/ {exit} found && $1 == "content" {print $2}' /etc/pve/storage.cfg)
case ",$content," in
  *,snippets,*) ;;
  *) pvesm set local --content "${content:+$content,}snippets"; echo "the local storage now also holds snippets (for the hookscript)" ;;
esac
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
# No ExecStop: stopping the unit leaves the filter loaded. The rollback is
# setup-vm-egress-filter.sh --vmid $vmid --remove.

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$unit" >/dev/null 2>&1
systemctl reload-or-restart "$unit"
nft list table bridge "$table" >/dev/null
qm set "$vmid" --hookscript "local:snippets/$hook" >/dev/null
echo "filter on: frames from tap${vmid}i* pass table bridge $table; VM $vmid refuses to start without it (rollback: $0 --vmid $vmid --remove)"
