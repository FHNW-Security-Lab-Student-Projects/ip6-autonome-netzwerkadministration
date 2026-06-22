#!/usr/bin/env bash
# MEDIUM: remove router1 eBGP export-policy (BGP up, no routes advertised)
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"

HERE="$(cd "$(dirname "$0")" && pwd)"
CAPTURE=(uv run python "$HERE/../../state_snapshot_agent.py" --capture-once)

# Capture the healthy baseline BEFORE applying the fault. The background snapshot loop only
# starts with the agents (after the fault is in place), so this is the only way to give the
# snapshot agent a clean good->bad diff.
echo "[export-policy] capturing healthy baseline"
"${CAPTURE[@]}"
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
delete / network-instance default protocols bgp group ebgp export-policy
commit now
SRL

# Capture the faulted state so the good->bad change is in snapshot history immediately.
# Give BGP a few seconds to withdraw the now-unexported routes first.
sleep 5
echo "[export-policy] capturing faulted state"
"${CAPTURE[@]}"
