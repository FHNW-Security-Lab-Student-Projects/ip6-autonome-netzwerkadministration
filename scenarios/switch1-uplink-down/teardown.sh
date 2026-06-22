#!/usr/bin/env bash
# restore switch1 e1-3 (trunk uplink to router1)
set -euo pipefail
sudo docker exec -i clab-testlab-switch1 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/3 admin-state enable
commit now
SRL
