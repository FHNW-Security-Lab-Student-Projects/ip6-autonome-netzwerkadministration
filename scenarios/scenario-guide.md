# Scenario guide — what each one tests and why it's that difficulty

This explains every scenario in [scenarios/](.), the fault it injects, and **why it
sits in its difficulty tier**. For the runner format and reset procedure see
[HOW-TO-RUN.md](HOW-TO-RUN.md); for the lab itself see [../docs/target-topology.md](../docs/target-topology.md).

## The baseline topology

Every fault is injected against a working network where client1 ↔ client3 succeeds:

```
client1 (192.168.1.10/24) ─┐                                   ┌─ client3 (10.10.10.10/24)
                       switch1 ─ router1 ═eBGP═ router2 ─ switch2
client2 (192.168.10.10/24)─┘   AS65001  10.0.0.0/30  AS65002
   VLAN10 / VLAN20                                        VLAN30
```

The path crosses four layers, and that is what makes difficulty meaningful: a symptom
("client1 can't reach client3") can originate at L1 (port down), L2 (VLAN/ARP), L3
(routing/BGP), or the data plane (ACL, MTU). The agent's job is to localize the fault to
the right device, layer, and config line.

---

## What makes a scenario easy vs. hard

Difficulty is driven by **how far the agent has to travel from the obvious first check to
reach the truth** — not by whether the fault happens to be logged. Three properties, in
order of impact:

| Property | Easy direction | Hard direction |
|---|---|---|
| **Honesty of the obvious signal** | The first thing you check *is* the fault | The obvious signal lies: link up but BGP down; BGP up but no routes; ping succeeds but the service is dead |
| **How far the fault hides from the symptom** | On the exact device and layer the symptom points at | Displaced — a shared uplink, a lower layer, or another host — somewhere the agent has no reason to look until it rules everything else out |
| **Number of signals to correlate** | One device, one `show` command | Must compare two outputs (two RIBs, ACL counter vs. traffic) or vary an input (packet size) |


A secondary factor is **how many plausible wrong answers exist**. Easy faults have one
obvious culprit. Hard faults look healthy at every layer the agent normally checks, so
"routing looks fine, no problem found" is a tempting — and wrong — conclusion. Each
`ground_truth.yaml` states the single true root cause; the queries are deliberately
open-ended ("investigate the root cause"), giving no hints about where to look, so the
giving-up answer counts as a MISS when scored against that root cause.

---

## Baseline scenario (no fault — sanity check)

This verifies the agent doesn't *invent* problems in a healthy network. It has a `setup.sh`
that **injects no fault and has no teardown** — it only clears `state_snapshots.json` so the
run starts from a fresh snapshot history, exactly like the fault scenarios do (there is
deliberately no before/after `--capture-once`: with no fault there is no good→bad diff to
record). The runner otherwise just sends `queries.txt` against the unbroken topology.

> Run it **without** `--no-fault` so the `setup.sh` actually executes — `--no-fault` skips
> `setup.sh`/`teardown.sh`, which would leave a stale `state_snapshots.json` in place.

### `basic-client-communication`
- **Fault:** none. The network is healthy; client1, client2 and client3 can all
  communicate with each other.
- **Tests:** the agent confirms full reachability between clients and does not fabricate a down link,
  VLAN, or BGP fault. 

---

## Easy tier

> One device, one show command, and the fault sits **right where the symptom points.** The obvious signal is the truth.

### `intf-down`
- **Fault:** router1 `ethernet-1/2` (the link to router2) is admin-disabled.
- **Why easy:** oper-state goes down and the interface-down event hits syslog. A single
  `show interface` on router1 reveals it. (It *also* tears down BGP and drops cross-router
  routes, but those are downstream effects, not the root cause.)
- **Distractors to reject:** router2, switches/VLANs, BGP policy, the clients.

### `client3-port-down`
- **Fault:** switch2 `ethernet-1/2` (access port to client3) is admin-disabled.
- **Why easy:** client3 is isolated at L2; oper-state down is visible in one `show` and in
  syslog, and LLDP/topology pinpoints the break.
- **Distractors to reject:** the routers, BGP, client3's own config, router2's gateway.

### `switch1-uplink-down`
- **Fault:** switch1 `ethernet-1/3` (the trunk uplink to router1) is admin-disabled.
- **Why easy:** oper-state down is visible in one `show` on switch1 and in syslog, and
  LLDP/topology pinpoints the break. The distinctive tell is the symptom signature itself:
  client1 **and** client2 both go offline while client3 stays up, so the shared uplink — the one
  link both VLAN10 and VLAN20 traverse — is the obvious and correct suspect. The fault sits
  exactly where the symptom points.
- **Distractors to reject:** the routers, BGP, the individual client access ports (all up),
  router1's gateways, and client3's side of the network (healthy).

---

## Medium tier

> The obvious signal **lies** — the fault is one step displaced from the obvious suspect, so
> you have to look one layer deeper or one hop away.

### `missing-export-policy`
- **Fault:** router1's eBGP `export-policy` is removed. Session is Established, but router1
  advertises none of its LAN prefixes, so router2 has no return route.
- **Why medium / silent:** "BGP Established" is the lie — checking session state and stopping
  is an explicit MISS. The agent must inspect **RIB-in / RIB-out** (what's actually
  advertised/received), one layer below session state. Nothing is logged.

### `missing-vlan-on-trunk`
- **Fault:** on switch1, the trunk uplink to router1 (`ethernet-1/3`) stops carrying VLAN10 —
  the tagged `ethernet-1/3.10` sub-interface is removed from the trunk and from `mac-vrf vlan10`.
  client1's access port stays correctly in VLAN10, so `mac-vrf vlan10` still exists but now has
  **no uplink member** — an isolated L2 island. The port stays **up** (still carrying VLAN20).
- **Why medium / silent:** the trunk port is up and client1's own access port is clean, so the
  two obvious places to look both pass — that's the lie. The fault is on the shared uplink, one
  hop removed from the obvious suspect. The tell is the same client1-fails / client2-works
  asymmetry plus inspecting **which VLANs the trunk actually carries**. No syslog event. Sits at
  the harder edge of medium: the fault is *not* on the port directly
  associated with the failing client, so it distinguishes agents that check "is the access port
  in the right VLAN?" from agents that check "does that VLAN's path to the router actually exist?"
- **Distractors to reject:** client1's access port (correct), `mac-vrf vlan10` existence (present),
  routing/BGP (healthy), client2's segment, and "the trunk port is up, looks fine".

### `duplicate-ip-arp`
- **Fault:** a real duplicate-IP event on VLAN30. client4 (normally `10.10.10.11`) also claims
  `10.10.10.10` and announces it (gratuitous ARP) while client3's switch port is down, so router2
  (client3's gateway, `ethernet-1/2.30` = 10.10.10.1) relearns `10.10.10.10` → **client4's real
  MAC**. client3 is brought back but client4 **keeps** the duplicate address, so router2's neighbor
  cache stays poisoned: frames to client3 go to client4's MAC.
- **Why medium / the obvious signal lies:** a ping to `10.10.10.10` still **succeeds** (client4
  answers), so the reachability reflex says "fine" — that's the lie. The tell is the mismatch:
  pingable IP, but client3's real service (`http.server` on TCP 1111) is dead. The root cause is
  **named in syslog** — SR Linux logs `AddNbr Duplicate add 10.10.10.10,<mac>` with both competing
  MACs — so an agent that checks Loki is pointed straight at the duplicate. (Snapshot history still
  corroborates it — `10.10.10.10` used to resolve to client3's MAC — but it is **not required**: the
  syslog event reveals the fault, which is why this is medium, not history-required.)
- **Distractors to reject:** routing/BGP (healthy), interface/port down (all up in the final state),
  VLAN-membership or gateway-IP misconfig (clean), and "an ARP entry exists, looks fine".

---

## Hard tier

> The config *looks* complete at **every layer the agent normally checks**, and the fault hides
> somewhere it has no reason to look — so the symptom needs **active, multi-signal probing**
> (vary an input, or correlate two outputs). "No problem found" is the trap.

### `acl-silent-drop`
- **Fault:** an ingress ACL on router2 (`block-icmp` on `ethernet-1/1.0`) silently drops
  ICMP from `192.168.1.0/24`. BGP and all routes are healthy.
- **Why hard:** reachability *and* routing both look perfect — the drop happens in the data
  plane below them. The agent has to think to check **traffic filters and their drop
  counters**, a place it has no reason to look unless it rules out everything else. Silent.

### `one-way-route-filter`
- **Fault:** router1 has a BGP import policy (`block-client3`) that rejects `10.10.10.0/24`.
  Session is Established; router2 still learns client1's prefix, but router1 never installs
  client3's.
- **Why hard:** the tell is an **asymmetry between two route tables**, not in any single
  output. The agent must compare router1's RIB against router2's and notice one prefix
  missing on one side — even though BGP is up and every other check passes. Silent.

### `mtu-blackhole`
- **Fault:** router1's `ethernet-1/2` IP MTU is lowered to 1280. Small packets pass; large
  packets with DF set are silently dropped → path-MTU blackhole.
- **Why hard:** the symptom is **intermittent and size-dependent** — a normal ping *succeeds*,
  so the agent can wrongly conclude the network is fine. Finding it requires **varying an
  input** (ping with a large size + DF set) and then inspecting interface MTU on the
  router-to-router link. Silent, and the most counter-intuitive symptom of the set.

---

## History-required tier — *currently unfilled*

> This tier was meant for a fault in **dynamic runtime state** that is wrong *only* relative to a
> recorded baseline — nothing in current state or syslog flags it, so it would be the one tier that
> genuinely requires `state_snapshot_agent`.
>
> Its intended occupant, `duplicate-ip-arp`, turned out to be **syslog-visible on real SR Linux gear**
> (`AddNbr Duplicate add 10.10.10.10,<mac>` names the conflicting IP and both MACs), so it was
> reclassified to **medium** rather than filtered to fake silence — filtering a warning real gear emits
> would measure an artificial puzzle, not real troubleshooting. The honest takeaway: duplicate-IP / ARP
> conflicts surface in syslog and don't require history reasoning.
>
> An honest occupant would have to be **drift that never trips a log** — e.g. a working-but-suboptimal
> route whose next-hop / AS-path changed from a recorded baseline — and would need validating against the
> lab to confirm it actually stays silent (the same check that caught `duplicate-ip-arp`). **None is
> defined yet.**

### Snapshot mechanism (still recorded by `duplicate-ip-arp`'s `setup.sh`)

The snapshot agent's background refresh loop only runs *while the orchestrator is alive*, i.e.
**after** `setup.sh`. So `setup.sh` records the healthy baseline itself, by calling the snapshot
agent's one-shot capture *before* injecting the persistent fault:

```
state_snapshot_agent.py --capture-once   # 1. healthy baseline  (the correct value)
<inject persistent fault>                # 2. fault stays in place for the whole run
state_snapshot_agent.py --capture-once   # 3. faulted state      (clean good->bad diff)
```

`--capture-once` loads the existing history, appends one snapshot of every device, and saves.
When the experiment starts, `snapshot_lifespan` reloads that file (and keeps appending the
still-broken live state every 2 min). The agent then has both the recorded healthy value and
the current broken value, and localizes the fault by diffing them: `snapshot_status` for the
timestamps, then `state_before` / `state_diff`. `teardown.sh` removes the persistent fault.

---

## Quick reference

| Scenario | Tier | Device | Layer | In syslog? | What localizes it |
|---|---|---|---|---|---|
| `intf-down` | easy | router1 e1-2 | L1 | ✅ | one `show interface` |
| `client3-port-down` | easy | switch2 e1-2 | L1 | ✅ | one `show interface` / LLDP |
| `switch1-uplink-down` | easy | switch1 e1-3 | L1 | ✅ | one `show interface` / LLDP (client1+client2 both down) |
| `missing-export-policy` | medium | router1 export-policy | L3 | ❌ | RIB-out (session state lies) |
| `missing-vlan-on-trunk` | medium | switch1 e1-3 | L2 | ❌ | trunk VLAN membership (port up + clean access port both lie) |
| `duplicate-ip-arp` | medium | router2 ARP | L2/L3 | ✅ | syslog `Duplicate add` event (ping succeeds but service dead) |
| `acl-silent-drop` | hard | router2 ACL | data plane | ❌ | ACL drop counters |
| `one-way-route-filter` | hard | router1 import-policy | L3 | ❌ | route-table asymmetry (2 RIBs) |
| `mtu-blackhole` | hard | router1 e1-2 | data plane | ❌ | packet-size probing + MTU |
