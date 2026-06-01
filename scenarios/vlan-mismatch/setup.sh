#!/usr/bin/env bash
# MEDIUM: move client1 access port from VLAN10 to VLAN20 mac-vrf
set -euo pipefail
sudo docker exec -i clab-testlab-switch1 sr_cli <<'SRL'
enter candidate
delete / network-instance vlan10 interface ethernet-1/1.0
set / network-instance vlan20 interface ethernet-1/1.0
commit now
SRL
