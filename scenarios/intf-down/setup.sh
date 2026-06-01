#!/usr/bin/env bash
# EASY: router1 e1-2 (link to router2) admin-disabled
set -euo pipefail
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/2 admin-state disable
commit now
SRL
