#!/usr/bin/env bash
# EASY: router1 e1-2 (link to router2) admin-disabled
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/2 admin-state disable
commit now
SRL
