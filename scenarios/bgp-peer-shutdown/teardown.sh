#!/usr/bin/env bash
# restore router1 BGP neighbor 10.0.0.2
set -euo pipefail
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
set / network-instance default protocols bgp neighbor 10.0.0.2 admin-state enable
commit now
SRL
