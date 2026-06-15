# Fault-injection scenario catalog

Each folder is one experiment for the multi-agent troubleshooter, in the format the
runner expects (`queries.txt`, `ground_truth.yaml`, and — for fault scenarios —
`setup.sh` / `teardown.sh`) — see
[../docs/running-experiments.md](../docs/running-experiments.md).

There are **9 fault scenarios** (table below) plus **2 no-fault baselines**
(`basic-client-communication`, `bgp-troubleshooting`) that check the agent doesn't
invent problems in a healthy network. The baselines have no `setup.sh` / `teardown.sh`.
For what each scenario tests and why it sits in its tier, see
[scenario-guide.md](scenario-guide.md).

All faults are injected against a **working baseline** where client1 ↔ client3
communication succeeds. The baseline is the per-node SR Linux startup configs in
[../configs/](../configs/) (`router1.cfg`, `router2.cfg`, `switch1.cfg`, `switch2.cfg`),
wired up in [../testlab.clab.yml](../testlab.clab.yml). Full intent + the exact config
commands are in [../docs/target-topology.md](../docs/target-topology.md).

```
client1 (192.168.1.10/24) ─┐                                   ┌─ client3 (10.10.10.10/24)
                       switch1 ─ router1 ═eBGP═ router2 ─ switch2
client2 (192.168.10.10/24)─┘   AS65001  10.0.0.0/30  AS65002
   VLAN10 / VLAN20                                        VLAN30
```

> **Status:** the baseline and all 9 scenarios below were deployed and verified on the
> `srlinux:24.10.1` lab — each fault was confirmed to break connectivity and each
> teardown to restore it.

## Difficulty tiers

Difficulty tracks **what the agent must observe to find the fault**. "In syslog?"
means the fault raises an error event the agent can spot via Loki without probing.

| Scenario | Tier | Fault | In syslog? |
|---|---|---|---|
| `intf-down` | easy | router1 e1-2 (link to router2) admin-disabled | ✅ |
| `client3-port-down` | easy | switch2 access port to client3 admin-disabled | ✅ |
| `bgp-peer-shutdown` | medium | router1 BGP neighbor admin-shutdown (links stay up) | ✅ |
| `missing-export-policy` | medium | router1 advertises nothing (BGP up, RIB-out empty) | ❌ |
| `vlan-mismatch` | medium | switch1 puts client1's port in the wrong VLAN | ❌ |
| `wrong-gateway-ip` | hard | router1 VLAN10 gateway IP typo (.2 not .1) | ❌ |
| `acl-silent-drop` | hard | router2 ACL silently drops client1's ICMP | ❌ |
| `one-way-route-filter` | hard | router1 import policy drops 10.10.10.0/24 (route-table asymmetry) | ❌ |
| `mtu-blackhole` | hard | router1↔router2 IP MTU lowered to 1280 → PMTU blackhole | ❌ |

- **easy** — one device, one show command, and it shows up in syslog.
- **medium** — the obvious signal lies: links are up but BGP is down, or BGP is up but
  no routes are advertised, or L2 membership is wrong. The agent must look one layer deeper.
- **hard** — silent (no syslog), the config *looks* complete, and the symptom needs active,
  multi-signal probing (route-table comparison, ACL counters, packet-size testing).

## How the scripts inject faults

`setup.sh` / `teardown.sh` drive `sr_cli` over `docker exec` using a stdin heredoc
(the form that actually works non-interactively on this image):

```bash
sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL'
enter candidate
set / interface ethernet-1/2 admin-state disable
commit now
SRL
```

> Note: `sr_cli -c "cmd1" -c "cmd2"` does **not** work (`-c` just means "commit at end").
> Use the stdin heredoc above, or `sr_cli "source /path/file.cfg"`.

## Validate / reset

Run a scenario by hand once (in the devcontainer where the lab lives):

```bash
bash scenarios/<name>/setup.sh
sudo docker exec clab-testlab-client1 ping -c2 10.10.10.10   # now fails
# (mtu-blackhole: default ping still passes; the big one fails:)
#   sudo docker exec clab-testlab-client1 ping -c2 -s 1320 -M do 10.10.10.10
bash scenarios/<name>/teardown.sh
sudo docker exec clab-testlab-client1 ping -c2 10.10.10.10   # recovers
```

> **BGP reconvergence:** the session-flap scenarios (`intf-down`, `bgp-peer-shutdown`)
> drop the eBGP TCP session. After teardown, BGP can take up to ~1–2 min to re-establish
> (the ConnectRetry timer) before client1↔client3 works again — the config is restored
> immediately, the dataplane just needs to reconverge. Policy-only faults
> (`missing-export-policy`, `one-way-route-filter`) recover in seconds. When in doubt
> between runs, `sudo containerlab redeploy --cleanup -t testlab.clab.yml` resets everything.
