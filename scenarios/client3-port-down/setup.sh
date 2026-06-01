#!/usr/bin/env bash
# EASY: switch2 access port to client3 admin-disabled
set -euo pipefail
sudo docker exec -i clab-testlab-switch2 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/2 admin-state disable
commit now
SRL
