#!/usr/bin/env bash
# restore router1 e1-2
set -euo pipefail
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/2 admin-state enable
commit now
SRL
