#!/usr/bin/env bash
# HARD: lower router1 e1-2 ip-mtu to 1280 (small pings pass, >1252B pings/transfers blackholed)
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/2 subinterface 0 ip-mtu 1280
commit now
SRL
