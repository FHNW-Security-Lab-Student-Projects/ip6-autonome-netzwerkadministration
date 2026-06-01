#!/usr/bin/env bash
# restore router1 e1-2 ip-mtu + flush client path-MTU caches
set -euo pipefail
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
delete / interface ethernet-1/2 subinterface 0 ip-mtu
commit now
SRL
for c in client1 client2 client3; do
  sudo docker exec clab-testlab-$c ip route flush cache 2>/dev/null || true
done
