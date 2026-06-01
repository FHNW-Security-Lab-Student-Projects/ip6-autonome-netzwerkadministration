#!/usr/bin/env bash
# HARD: router1 VLAN10 gateway IP typo .1 -> .2 (silent)
set -euo pipefail
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
delete / interface ethernet-1/1 subinterface 10 ipv4 address 192.168.1.1/24
set / interface ethernet-1/1 subinterface 10 ipv4 address 192.168.1.2/24
commit now
SRL
