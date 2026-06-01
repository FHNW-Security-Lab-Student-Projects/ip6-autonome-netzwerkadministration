#!/usr/bin/env bash
# restore router1 export-policy
set -euo pipefail
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
set / network-instance default protocols bgp group ebgp export-policy [ export-lan ]
commit now
SRL
