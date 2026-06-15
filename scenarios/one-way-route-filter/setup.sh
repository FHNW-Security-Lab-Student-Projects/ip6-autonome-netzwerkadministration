#!/usr/bin/env bash
# HARD: router1 import policy rejects 10.10.10.0/24 (router1 RIB missing client3 route; router2 RIB still has client1)
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
set / routing-policy prefix-set client3-subnet prefix 10.10.10.0/24 mask-length-range 24..24
set / routing-policy policy block-client3 statement 10 match prefix-set client3-subnet
set / routing-policy policy block-client3 statement 10 action policy-result reject
set / routing-policy policy block-client3 default-action policy-result accept
set / network-instance default protocols bgp group ebgp import-policy [ block-client3 ]
commit now
SRL
