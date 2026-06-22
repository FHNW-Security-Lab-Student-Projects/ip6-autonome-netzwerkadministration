#!/usr/bin/env bash
# HARD: router2 ingress ACL silently drops ICMP from 192.168.1.0/24 (BGP+routes stay healthy)
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"

HERE="$(cd "$(dirname "$0")" && pwd)"
CAPTURE=(uv run python "$HERE/../../state_snapshot_agent.py" --capture-once)

# Capture the healthy baseline BEFORE applying the fault. The background snapshot loop only
# starts with the agents (after the fault is in place), so this is the only way to give the
# snapshot agent a clean good->bad diff.
echo "[acl-drop] capturing healthy baseline"
"${CAPTURE[@]}"
sudo docker exec -i clab-testlab-router2 sr_cli <<'SRL'
enter candidate
set / acl acl-filter block-icmp type ipv4 entry 10 match ipv4 protocol icmp
set / acl acl-filter block-icmp type ipv4 entry 10 match ipv4 source-ip prefix 192.168.1.0/24
set / acl acl-filter block-icmp type ipv4 entry 10 action drop
set / acl interface ethernet-1/1.0 input acl-filter block-icmp type ipv4
commit now
SRL

# Capture the faulted state so the good->bad change is in snapshot history immediately.
sleep 3
echo "[acl-drop] capturing faulted state"
"${CAPTURE[@]}"
