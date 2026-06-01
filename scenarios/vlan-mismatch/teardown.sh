#!/usr/bin/env bash
# restore client1 access port to VLAN10
set -euo pipefail
sudo docker exec -i clab-testlab-switch1 sr_cli <<'SRL'
enter candidate
delete / network-instance vlan20 interface ethernet-1/1.0
set / network-instance vlan10 interface ethernet-1/1.0
commit now
SRL
