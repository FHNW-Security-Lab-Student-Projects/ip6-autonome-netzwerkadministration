#!/usr/bin/env bash
# MEDIUM: remove VLAN10 from switch1's trunk to router1 (e1-3) so the vlan10 bridge
# domain loses its uplink. client1's access port stays correctly in vlan10, but VLAN10
# can no longer reach the router. client2 (VLAN20) is unaffected.
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"

HERE="$(cd "$(dirname "$0")" && pwd)"
CAPTURE=(uv run python "$HERE/../../state_snapshot_agent.py" --capture-once)

# Capture the healthy baseline BEFORE applying the fault. The background snapshot loop only
# starts with the agents (after the fault is in place), so this is the only way to give the
# snapshot agent a clean good->bad diff.
echo "[vlan-trunk] capturing healthy baseline"
"${CAPTURE[@]}"
sudo docker exec -i clab-testlab-switch1 sr_cli <<'SRL'
enter candidate
delete / network-instance vlan10 interface ethernet-1/3.10
delete / interface ethernet-1/3 subinterface 10
commit now
SRL

# Capture the faulted state so the good->bad change is in snapshot history immediately.
sleep 3
echo "[vlan-trunk] capturing faulted state"
"${CAPTURE[@]}"
