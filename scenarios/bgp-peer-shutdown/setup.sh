#!/usr/bin/env bash
# MEDIUM: router1 BGP neighbor 10.0.0.2 admin-shutdown (links stay up)
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
set / network-instance default protocols bgp neighbor 10.0.0.2 admin-state disable
commit now
SRL
