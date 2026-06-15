#!/usr/bin/env bash
# HARD: a wrong / duplicate-IP ARP binding that PERSISTS through the whole investigation.
#
# router2 is client3's gateway (ethernet-1/2.30 = 10.10.10.1). client3 (10.10.10.10) and
# client4 (10.10.10.11) are both normal hosts on the VLAN30 segment. We stage a *real*
# duplicate-IP event: client4 briefly also claims 10.10.10.10 and announces it (gratuitous
# ARP) while client3's switch port is down, so router2 relearns 10.10.10.10 -> client4's
# REAL MAC. client3 is then brought back, but client4 KEEPS the duplicate address. router2's
# neighbor cache stays poisoned: it frames all client3-bound traffic to client4's MAC, and
# 10.10.10.10 remains a live duplicate on the segment -- client1 <-> client3 fails and
# STAYS failed.
#
# Why this is hard:
#   * The fault lives in runtime ARP state -- it is NOT in any device config the agent would
#     read as "wrong" (no static ARP entry, no VLAN/IP misconfig) and NOT in syslog.
#   * The poisoned MAC is client4's genuine, live MAC on the same segment, so a live
#     `show arpnd arp-entries` shows an entry that looks perfectly valid in isolation.
#   * The ONLY record that 10.10.10.10 used to resolve to client3's real MAC is the snapshot
#     baseline captured here BEFORE the fault. That is what the snapshot agent uniquely
#     provides.
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"

HERE="$(cd "$(dirname "$0")" && pwd)"
CAPTURE=(uv run python "$HERE/../../state_snapshot_agent.py" --capture-once)
DUP_IP="10.10.10.10"      # client3's address, which client4 will transiently steal
DUP_CIDR="10.10.10.10/24"

# 0. start the real service on client3 (port 1111). client4 will steal the IP but NOT run
#    this service, so ping to 10.10.10.10 still succeeds (client4 answers) while the actual
#    service is unreachable -- that mismatch is the reportable symptom.
echo "[dup-ip-arp] starting http.server :1111 on client3 (the real service)"
sudo docker exec -d clab-testlab-client3 python3 -m http.server 1111

# 1. make sure router2 has dynamically learned client3's REAL MAC, then snapshot the
#    healthy baseline (10.10.10.10 -> client3's MAC, origin dynamic)
sudo docker exec clab-testlab-client1 ping -c2 -W1 "$DUP_IP" >/dev/null 2>&1 || true
sleep 2
echo "[dup-ip-arp] capturing healthy baseline (correct ARP for $DUP_IP -> client3)"
"${CAPTURE[@]}"

# 2. take client3 offline at the switch so it cannot defend the address during the grab
echo "[dup-ip-arp] bringing client3's access port (switch2 e1-2) down"
sudo docker exec -i clab-testlab-switch2 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/2 admin-state disable
commit now
SRL
sleep 2

# 3. client4 transiently claims 10.10.10.10 and announces it, so router2 (and the segment)
#    relearn 10.10.10.10 -> client4's REAL MAC
echo "[dup-ip-arp] client4 claims $DUP_IP and announces it (gratuitous ARP)"
sudo docker exec clab-testlab-client4 ip addr add "$DUP_CIDR" dev eth1
sudo docker exec clab-testlab-client4 arping -c 3 -U -I eth1 "$DUP_IP" >/dev/null 2>&1 || true
# force an ARP exchange sourced from the duplicate address so router2 overwrites its entry
sudo docker exec clab-testlab-client4 ping -c 2 -W1 -I "$DUP_IP" 10.10.10.1 >/dev/null 2>&1 || true
sleep 3

# 4. bring client3 back up, but client4 KEEPS the duplicate address. The conflicting
#    10.10.10.10 stays live on client4, and router2's poisoned 10.10.10.10 -> client4-MAC
#    entry remains (a valid, non-expired dynamic neighbor), so client3 stays unreachable.
echo "[dup-ip-arp] client4 keeps $DUP_IP; bringing client3's port back up"
sudo docker exec -i clab-testlab-switch2 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/2 admin-state enable
commit now
SRL
sleep 3

# 5. snapshot the broken state so the good->bad MAC change is in history immediately
#    (the agent can also rely on the background loop, but this guarantees a clean diff)
echo "[dup-ip-arp] capturing faulted state (router2 ARP for $DUP_IP now points at client4)"
"${CAPTURE[@]}"
echo "[dup-ip-arp] done -- poisoned ARP binding persists; client3 unreachable"
