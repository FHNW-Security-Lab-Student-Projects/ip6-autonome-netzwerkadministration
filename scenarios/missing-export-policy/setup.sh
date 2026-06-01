#!/usr/bin/env bash
# MEDIUM: remove router1 eBGP export-policy (BGP up, no routes advertised)
set -euo pipefail
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
delete / network-instance default protocols bgp group ebgp export-policy
commit now
SRL
