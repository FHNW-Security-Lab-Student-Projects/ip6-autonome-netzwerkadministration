# Target Topology & Desired Configuration

This is the **desired end state** of the lab network — the healthy baseline that every
fault scenario is injected against. It is written so a human *or* the configuration agent
can bring the network to this state if the startup-configs in [../configs/](../configs/)
fail to apply cleanly.

> **For the agent:** each device section below lists the intent in plain English plus the
> exact SR Linux `set` commands to apply (validate first, then commit). Apply them per
> device. Devices are reachable as `clab-testlab-<node>` and over JSON-RPC. The goal state
> is verified by the checks at the bottom: **client1 and client3 must be able to ping each
> other, and the eBGP session between router1 and router2 must be Established.**

---

## 1. Physical topology

```
client1 (192.168.1.10/24) ─┐                                          ┌─ client3 (10.10.10.10/24)
                       switch1 ── router1 ════ eBGP ════ router2 ── switch2
client2 (192.168.10.10/24)─┘   AS 65001   10.0.0.0/30   AS 65002
       (L2 access)          (L3 gateways)            (L3 gateway)     (L2 access)
```

Cabling (from [../testlab.clab.yml](../testlab.clab.yml)):

| Link | A end | B end |
|---|---|---|
| client1 → switch1 | `client1:eth1` | `switch1:e1-1` |
| client2 → switch1 | `client2:eth1` | `switch1:e1-2` |
| switch1 → router1 | `switch1:e1-3` | `router1:e1-1` |
| router1 → router2 | `router1:e1-2` | `router2:e1-1` |
| router2 → switch2 | `router2:e1-2` | `switch2:e1-1` |
| switch2 → client3 | `switch2:e1-2` | `client3:eth1` |

The clients are fixed: their IPs and default routes are set by the topology file and must
not be changed. The network's job is to make those gateways exist and route between them.

| Client | IP | Default gateway (must exist on the network) |
|---|---|---|
| client1 | 192.168.1.10/24 | 192.168.1.1 |
| client2 | 192.168.10.10/24 | 192.168.10.1 |
| client3 | 10.10.10.10/24 | 10.10.10.1 |

---

## 2. Addressing & VLAN plan

| Segment | VLAN | Subnet | Gateway (lives on) |
|---|---|---|---|
| client1 | 10 | 192.168.1.0/24 | router1 `ethernet-1/1.10` = 192.168.1.1 |
| client2 | 20 | 192.168.10.0/24 | router1 `ethernet-1/1.20` = 192.168.10.1 |
| router1 ↔ router2 | — (routed p2p) | 10.0.0.0/30 | router1 = .1, router2 = .2 |
| client3 | 30 | 10.10.10.0/24 | router2 `ethernet-1/2.30` = 10.10.10.1 |

**BGP:** eBGP, router1 in AS 65001 and router2 in AS 65002, peering over the 10.0.0.0/30
link. Each router advertises its directly-connected LAN subnets to the other.

**Forwarding model:**
- **switches** are pure L2 — each VLAN is a `mac-vrf` network-instance bridging the client's
  untagged access port with the matching tagged sub-interface on the trunk to the router.
- **routers** do all L3 — VLAN sub-interfaces are `routed` and act as the LAN default gateways;
  the inter-router link is a routed p2p; eBGP carries reachability between the two sides.

---

## 3. Desired configuration per device

> Apply order does not matter between devices, but within a device apply all lines, then
> commit. The agent should VALIDATE first and only APPLY after the diff looks correct.
> The syslog + JSON-RPC lines are already present from the startup-config — re-applying
> them is harmless but they are omitted here to keep the intent focused on forwarding.

### 3.1 router1 — AS 65001 (LAN gateways for client1/client2 + eBGP)

Intent: e1-1 is an 802.1q trunk to switch1 with two **routed** sub-interfaces that are the
gateways for VLAN10 and VLAN20; e1-2 is the routed p2p link to router2; eBGP advertises the
two LAN subnets.

```
# e1-1: trunk to switch1, routed sub-interfaces = LAN gateways
set / interface ethernet-1/1 admin-state enable
set / interface ethernet-1/1 vlan-tagging true
set / interface ethernet-1/1 subinterface 10 type routed
set / interface ethernet-1/1 subinterface 10 admin-state enable
set / interface ethernet-1/1 subinterface 10 vlan encap single-tagged vlan-id 10
set / interface ethernet-1/1 subinterface 10 ipv4 admin-state enable
set / interface ethernet-1/1 subinterface 10 ipv4 address 192.168.1.1/24
set / interface ethernet-1/1 subinterface 20 type routed
set / interface ethernet-1/1 subinterface 20 admin-state enable
set / interface ethernet-1/1 subinterface 20 vlan encap single-tagged vlan-id 20
set / interface ethernet-1/1 subinterface 20 ipv4 admin-state enable
set / interface ethernet-1/1 subinterface 20 ipv4 address 192.168.10.1/24

# e1-2: routed p2p to router2
set / interface ethernet-1/2 admin-state enable
set / interface ethernet-1/2 subinterface 0 type routed
set / interface ethernet-1/2 subinterface 0 admin-state enable
set / interface ethernet-1/2 subinterface 0 ipv4 admin-state enable
set / interface ethernet-1/2 subinterface 0 ipv4 address 10.0.0.1/30

# default network-instance (L3)
set / network-instance default type default
set / network-instance default admin-state enable
set / network-instance default interface ethernet-1/1.10
set / network-instance default interface ethernet-1/1.20
set / network-instance default interface ethernet-1/2.0

# routing policy: advertise directly-connected LAN subnets into BGP
set / routing-policy policy export-lan statement 10 match protocol local
set / routing-policy policy export-lan statement 10 action policy-result accept
set / routing-policy policy export-lan default-action policy-result reject

# eBGP to router2
set / network-instance default protocols bgp admin-state enable
set / network-instance default protocols bgp autonomous-system 65001
set / network-instance default protocols bgp router-id 10.0.0.1
set / network-instance default protocols bgp afi-safi ipv4-unicast admin-state enable
set / network-instance default protocols bgp ebgp-default-policy import-reject-all false
set / network-instance default protocols bgp group ebgp peer-as 65002
set / network-instance default protocols bgp group ebgp export-policy [ export-lan ]
set / network-instance default protocols bgp neighbor 10.0.0.2 peer-group ebgp
```

### 3.2 router2 — AS 65002 (LAN gateway for client3 + eBGP)

Intent: mirror of router1. e1-1 is the routed p2p to router1; e1-2 is an 802.1q trunk to
switch2 with one **routed** sub-interface that is the gateway for VLAN30; eBGP advertises
10.10.10.0/24.

```
# e1-1: routed p2p to router1
set / interface ethernet-1/1 admin-state enable
set / interface ethernet-1/1 subinterface 0 type routed
set / interface ethernet-1/1 subinterface 0 admin-state enable
set / interface ethernet-1/1 subinterface 0 ipv4 admin-state enable
set / interface ethernet-1/1 subinterface 0 ipv4 address 10.0.0.2/30

# e1-2: trunk to switch2, routed sub-interface = client3 gateway
set / interface ethernet-1/2 admin-state enable
set / interface ethernet-1/2 vlan-tagging true
set / interface ethernet-1/2 subinterface 30 type routed
set / interface ethernet-1/2 subinterface 30 admin-state enable
set / interface ethernet-1/2 subinterface 30 vlan encap single-tagged vlan-id 30
set / interface ethernet-1/2 subinterface 30 ipv4 admin-state enable
set / interface ethernet-1/2 subinterface 30 ipv4 address 10.10.10.1/24

# default network-instance (L3)
set / network-instance default type default
set / network-instance default admin-state enable
set / network-instance default interface ethernet-1/1.0
set / network-instance default interface ethernet-1/2.30

# routing policy: advertise directly-connected LAN subnet into BGP
set / routing-policy policy export-lan statement 10 match protocol local
set / routing-policy policy export-lan statement 10 action policy-result accept
set / routing-policy policy export-lan default-action policy-result reject

# eBGP to router1
set / network-instance default protocols bgp admin-state enable
set / network-instance default protocols bgp autonomous-system 65002
set / network-instance default protocols bgp router-id 10.0.0.2
set / network-instance default protocols bgp afi-safi ipv4-unicast admin-state enable
set / network-instance default protocols bgp ebgp-default-policy import-reject-all false
set / network-instance default protocols bgp group ebgp peer-as 65001
set / network-instance default protocols bgp group ebgp export-policy [ export-lan ]
set / network-instance default protocols bgp neighbor 10.0.0.1 peer-group ebgp
```

### 3.3 switch1 — L2 access (VLAN10 client1, VLAN20 client2)

Intent: two broadcast domains. e1-1 (untagged) bridges client1 into VLAN10; e1-2 (untagged)
bridges client2 into VLAN20; e1-3 is the tagged trunk to router1 carrying both VLANs.

```
# access ports (untagged)
set / interface ethernet-1/1 admin-state enable
set / interface ethernet-1/1 subinterface 0 type bridged
set / interface ethernet-1/1 subinterface 0 admin-state enable
set / interface ethernet-1/2 admin-state enable
set / interface ethernet-1/2 subinterface 0 type bridged
set / interface ethernet-1/2 subinterface 0 admin-state enable

# trunk to router1 (tagged VLAN10 + VLAN20)
set / interface ethernet-1/3 admin-state enable
set / interface ethernet-1/3 vlan-tagging true
set / interface ethernet-1/3 subinterface 10 type bridged
set / interface ethernet-1/3 subinterface 10 vlan encap single-tagged vlan-id 10
set / interface ethernet-1/3 subinterface 20 type bridged
set / interface ethernet-1/3 subinterface 20 vlan encap single-tagged vlan-id 20

# broadcast domains
set / network-instance vlan10 type mac-vrf
set / network-instance vlan10 admin-state enable
set / network-instance vlan10 interface ethernet-1/1.0
set / network-instance vlan10 interface ethernet-1/3.10
set / network-instance vlan20 type mac-vrf
set / network-instance vlan20 admin-state enable
set / network-instance vlan20 interface ethernet-1/2.0
set / network-instance vlan20 interface ethernet-1/3.20
```

### 3.4 switch2 — L2 access (VLAN30 client3)

Intent: single broadcast domain. e1-2 (untagged) bridges client3 into VLAN30; e1-1 is the
tagged trunk to router2.

```
# access port (untagged) to client3
set / interface ethernet-1/2 admin-state enable
set / interface ethernet-1/2 subinterface 0 type bridged
set / interface ethernet-1/2 subinterface 0 admin-state enable

# trunk to router2 (tagged VLAN30)
set / interface ethernet-1/1 admin-state enable
set / interface ethernet-1/1 vlan-tagging true
set / interface ethernet-1/1 subinterface 30 type bridged
set / interface ethernet-1/1 subinterface 30 vlan encap single-tagged vlan-id 30

# broadcast domain
set / network-instance vlan30 type mac-vrf
set / network-instance vlan30 admin-state enable
set / network-instance vlan30 interface ethernet-1/2.0
set / network-instance vlan30 interface ethernet-1/1.30
```

---

## 4. Verification — the network is correctly configured when all hold

1. **End-to-end reachability**

   ```bash
   sudo docker exec clab-testlab-client1 ping -c2 10.10.10.10    # client1 → client3
   sudo docker exec clab-testlab-client3 ping -c2 192.168.1.10   # client3 → client1
   sudo docker exec clab-testlab-client2 ping -c2 10.10.10.10    # client2 → client3
   ```

2. **Gateways answer** — each client can ping its own gateway (.1 of its subnet).

3. **eBGP Established and routes exchanged**

   ```bash
   sudo docker exec clab-testlab-router1 sr_cli "show network-instance default protocols bgp neighbor"
   # neighbor 10.0.0.2 state = established
   sudo docker exec clab-testlab-router1 sr_cli "show network-instance default route-table ipv4-unicast prefix 10.10.10.0/24"
   # router1 has a BGP route to 10.10.10.0/24; router2 has BGP routes to 192.168.1.0/24 and 192.168.10.0/24
   ```

4. **L2 segments correct** — on switch1, client1's port is in `mac-vrf vlan10` and client2's
   in `mac-vrf vlan20`; on switch2, client3's port is in `mac-vrf vlan30`.

---

## 5. Notes for whoever applies this

- This config was **deployed and verified** on the `srlinux:24.10.1` lab: all four startup
  configs apply at boot, eBGP comes up Established, and client1/client2 ↔ client3 ping
  successfully.
- The clients' IPs/gateways are fixed by [../testlab.clab.yml](../testlab.clab.yml); the
  gateway IPs above (`.1` of each subnet) must match them exactly.
- **Two SR Linux gotchas these configs already account for** (watch for them if you hand-edit):
  - `export-policy` / `import-policy` on a BGP group is a leaf-list — it needs bracket
    syntax: `export-policy [ export-lan ]`, not `export-policy export-lan`.
  - In a config file applied via `sr_cli source` (which is how containerlab loads the
    startup-config), comments must be **ASCII with no quote characters**. An apostrophe,
    single/double quote, or backtick in a comment is treated as a string delimiter and
    silently aborts the whole commit — nothing applies, with no error shown.
- Applying via `sr_cli` non-interactively: pipe commands on stdin
  (`sudo docker exec -i clab-testlab-router1 sr_cli <<'SRL' … SRL`) or
  `sr_cli "source /path/file.cfg"`. `sr_cli -c "cmd1" -c "cmd2"` does **not** run multiple
  commands (`-c` only means "commit at end").
- This document describes the **healthy baseline only**. Faults are applied on top of it by
  the scenarios in [../scenarios/](../scenarios/) (see [../scenarios/README.md](../scenarios/README.md)).
- A full reset to this state is always available with
  `sudo containerlab redeploy --cleanup -t testlab.clab.yml` (re-applies the startup-configs).
```
