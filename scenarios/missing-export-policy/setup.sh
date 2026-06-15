#!/usr/bin/env bash
# MEDIUM: remove router1 eBGP export-policy (BGP up, no routes advertised)
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
delete / network-instance default protocols bgp group ebgp export-policy
commit now
SRL
