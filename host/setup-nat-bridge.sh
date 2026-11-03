#!/usr/bin/env bash
# =============================================================================
#  OPTIONAL - Network Option B: private NAT bridge on the PROXMOX HOST.
#
#  Only needed on networks that block or "register" every new device
#  (many student halls, hotels, some workplaces). On a normal home router
#  you do NOT need this: use Network Option A (vmbr0 + DHCP) instead.
#
#  Run on the Proxmox HOST shell (prompt "root@<host>"), NOT inside a container:
#      bash setup-nat-bridge.sh
#
#  What it changes:
#    1. Backs up /etc/network/interfaces to /etc/network/interfaces.bak-<date>.
#    2. Appends a bridge "vmbr1" (10.10.10.1/24, no physical port) with:
#         - NAT (MASQUERADE) so containers on 10.10.10.0/24 reach the internet
#           through the host's own address,
#         - port forwarding of TCP 3000,5001,5678,8081 to 10.10.10.100.
#    3. Enables IP forwarding (/etc/sysctl.d/99-forward.conf).
#    4. Applies the network config with "ifreload -a".
#  To undo: copy the backup back over /etc/network/interfaces and reboot.
# =============================================================================
set -euo pipefail

IFACES="${IFACES:-/etc/network/interfaces}"
SYSCTL_FILE="${SYSCTL_FILE:-/etc/sysctl.d/99-forward.conf}"
SUBNET="${SUBNET:-10.10.10}"          # gives 10.10.10.1 (host) and 10.10.10.100 (container)
CT_IP="${CT_IP:-${SUBNET}.100}"
PORTS="${PORTS:-3000,5001,5678,8081}"
UPLINK="${UPLINK:-vmbr0}"
APPLY="${APPLY:-1}"                   # APPLY=0 writes files only (for testing)

die() { echo "ERROR: $*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
if [ "$APPLY" = 1 ]; then
  command -v pveversion >/dev/null 2>&1 || die "this is not a Proxmox host. Run it on the host shell, not inside a container."
fi
[ -f "$IFACES" ] || die "$IFACES not found"
grep -q "iface $UPLINK " "$IFACES" || die "uplink bridge $UPLINK not found in $IFACES"
if grep -q "^auto vmbr1" "$IFACES" || grep -q "iface vmbr1 " "$IFACES"; then
  die "vmbr1 already exists in $IFACES - nothing changed"
fi

echo "About to add vmbr1 (${SUBNET}.1/24) with NAT via $UPLINK and forward ports $PORTS to $CT_IP."
read -r -p "Continue? [y/N] " a
case "$a" in y|Y|yes) ;; *) echo "Cancelled."; exit 0 ;; esac

BACKUP="${IFACES}.bak-$(date +%Y%m%d-%H%M%S)"
cp "$IFACES" "$BACKUP"
echo "Backup saved: $BACKUP"

cat >> "$IFACES" <<EOF

auto vmbr1
iface vmbr1 inet static
        address ${SUBNET}.1/24
        bridge-ports none
        bridge-stp off
        bridge-fd 0
        post-up   iptables -t nat -A POSTROUTING -s ${SUBNET}.0/24 -o ${UPLINK} -j MASQUERADE
        post-down iptables -t nat -D POSTROUTING -s ${SUBNET}.0/24 -o ${UPLINK} -j MASQUERADE
        post-up   iptables -t nat -A PREROUTING -i ${UPLINK} -p tcp -m multiport --dports ${PORTS} -j DNAT --to-destination ${CT_IP}
        post-down iptables -t nat -D PREROUTING -i ${UPLINK} -p tcp -m multiport --dports ${PORTS} -j DNAT --to-destination ${CT_IP}
EOF
echo "net.ipv4.ip_forward=1" > "$SYSCTL_FILE"

if [ "$APPLY" = 1 ]; then
  sysctl -p "$SYSCTL_FILE"
  ifreload -a
  echo "Done. Check with:  ip addr show vmbr1   and   iptables -t nat -L -n"
else
  echo "APPLY=0: files written, nothing applied."
fi
