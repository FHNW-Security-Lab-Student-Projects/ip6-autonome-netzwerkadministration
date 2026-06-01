#!/usr/bin/env bash
# remove router2 block-icmp ACL
set -euo pipefail
sudo docker exec -i clab-testlab-router2 sr_cli <<'SRL'
enter candidate
delete / acl interface ethernet-1/1.0 input acl-filter block-icmp type ipv4
delete / acl acl-filter block-icmp type ipv4
commit now
SRL
