#!/usr/bin/env bash
# Heal the poisoned binding: make sure client3 is online and let it reclaim 10.10.10.10 by
# announcing its real MAC (gratuitous ARP), so router2 relearns the correct neighbor and
# connectivity recovers. Best-effort (the runner ignores teardown failures).
set -uo pipefail

# client4 should already have released the duplicate in setup; drop it again just in case
# setup aborted mid-grab.
sudo docker exec clab-testlab-client4 ip addr del 10.10.10.10/24 dev eth1 >/dev/null 2>&1 || true

# ensure client3's access port is up (setup disables it transiently)
sudo docker exec -i clab-testlab-switch2 sr_cli <<'SRL' || true
enter candidate
set / interface ethernet-1/2 admin-state enable
commit now
SRL

# client3 reclaims 10.10.10.10 with its real MAC so router2 overwrites the poisoned entry
sudo docker exec clab-testlab-client3 arping -c 3 -U -I eth1 10.10.10.10 >/dev/null 2>&1 || true
sudo docker exec clab-testlab-client3 ping -c 2 -W1 10.10.10.1 >/dev/null 2>&1 || true
true
