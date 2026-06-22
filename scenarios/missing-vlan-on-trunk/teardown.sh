#!/usr/bin/env bash
# restore VLAN10 on switch1's trunk to router1 (e1-3)
set -euo pipefail
sudo docker exec -i clab-testlab-switch1 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/3 subinterface 10 type bridged
set / interface ethernet-1/3 subinterface 10 vlan encap single-tagged vlan-id 10
set / network-instance vlan10 interface ethernet-1/3.10
commit now
SRL
