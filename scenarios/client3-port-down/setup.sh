#!/usr/bin/env bash
# EASY: switch2 access port to client3 admin-disabled
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"

HERE="$(cd "$(dirname "$0")" && pwd)"
CAPTURE=(uv run python "$HERE/../../state_snapshot_agent.py" --capture-once)

# Capture the healthy baseline BEFORE applying the fault. The background snapshot loop only
# starts with the agents (after the fault is in place), so this is the only way to give the
# snapshot agent a clean good->bad diff.
echo "[client3-port] capturing healthy baseline"
"${CAPTURE[@]}"
sudo docker exec -i clab-testlab-switch2 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/2 admin-state disable
commit now
SRL

# Capture the faulted state so the good->bad change is in snapshot history immediately.
sleep 3
echo "[client3-port] capturing faulted state"
"${CAPTURE[@]}"
