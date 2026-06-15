#!/usr/bin/env bash
# MEDIUM: move client1 access port from VLAN10 to VLAN20 mac-vrf
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"
sudo docker exec -i clab-testlab-switch1 sr_cli <<'SRL'
enter candidate
delete / network-instance vlan10 interface ethernet-1/1.0
set / network-instance vlan20 interface ethernet-1/1.0
commit now
SRL
