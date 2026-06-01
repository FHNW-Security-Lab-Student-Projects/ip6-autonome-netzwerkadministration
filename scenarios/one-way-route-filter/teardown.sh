#!/usr/bin/env bash
# remove router1 import policy + objects
set -euo pipefail
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
delete / network-instance default protocols bgp group ebgp import-policy
delete / routing-policy policy block-client3
delete / routing-policy prefix-set client3-subnet
commit now
SRL
