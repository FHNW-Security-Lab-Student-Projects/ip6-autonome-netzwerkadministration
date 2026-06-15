#!/usr/bin/env bash
# HARD: router2 ingress ACL silently drops ICMP from 192.168.1.0/24 (BGP+routes stay healthy)
set -euo pipefail

# Clear snapshot history so the evaluation starts from a fresh state.
rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"
sudo docker exec -i clab-testlab-router2 sr_cli <<'SRL'
enter candidate
set / acl acl-filter block-icmp type ipv4 entry 10 match ipv4 protocol icmp
set / acl acl-filter block-icmp type ipv4 entry 10 match ipv4 source-ip prefix 192.168.1.0/24
set / acl acl-filter block-icmp type ipv4 entry 10 action drop
set / acl interface ethernet-1/1.0 input acl-filter block-icmp type ipv4
commit now
SRL
