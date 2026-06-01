#!/usr/bin/env bash
# restore switch2 e1-2
set -euo pipefail
sudo docker exec -i clab-testlab-switch2 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/2 admin-state enable
commit now
SRL
