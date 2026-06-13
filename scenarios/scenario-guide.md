# Scenario guide — what each one tests and why it's that difficulty

This explains every scenario in [scenarios/](.), the fault it injects, and **why it
sits in its difficulty tier**. For the runner format and reset procedure see
[README.md](README.md); for the lab itself see [../docs/target-topology.md](../docs/target-topology.md).

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

Three properties drive the difficulty rating, in order of impact:

| Property | Easy direction | Hard direction |
|---|---|---|
| **Syslog visibility** | Fault raises an error event the agent can find in Loki without probing | Silent — nothing logged; the agent must actively probe to see it |
| **Honesty of the obvious signal** | The first thing you check *is* the fault | The obvious signal lies: link up but BGP down; BGP up but no routes; port up but wrong VLAN |
| **Number of signals to correlate** | One device, one `show` command | Must compare two outputs (route tables, ACL counters) or vary an input (packet size) |

A secondary factor is **how many plausible wrong answers exist**. Easy faults have one
obvious culprit. Hard faults look healthy at every layer the agent normally checks, so
"routing looks fine, no problem found" is a tempting — and wrong — conclusion. Each
hard `ground_truth.yaml` explicitly marks that giving-up answer as a MISS.

---

## Baseline scenarios (no fault — sanity checks)

These verify the agent doesn't *invent* problems in a healthy network.

### `basic-reachability`
- **Fault:** none. All router1 interfaces are admin-enabled and operationally up.
- **Tests:** the agent reports interfaces as up and does not fabricate a down link or BGP fault.

### `bgp-troubleshooting`
- **Fault:** none. eBGP is Established, no flaps, router1 advertises normally.
- **Tests:** the agent reports BGP healthy and doesn't claim a session is down or flapping.

> Note: `client1-client3-communication` is a third baseline-style probe, but its
> `ground_truth.yaml` currently states there is **no** L2/L3 config and communication is
> not possible — which contradicts the "healthy baseline" framing of the other two and
> the README. This looks like a stale ground-truth file; flag it before relying on it.

> The top-level `*.txt` files (`basic-reachability.txt`, `bgp-troubleshooting.txt`,
> `client1-client3-communication.txt`) are the older flat-file query format. The
> per-folder versions (`queries.txt` + `ground_truth.yaml` + `setup.sh`/`teardown.sh`)
> are the current runner format.

---

## Easy tier

> One device, one show command, **and it shows up in syslog.** The obvious signal is the truth.

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

---

## Medium tier

> The obvious signal **lies** — you have to look one layer deeper. Some are in syslog, some aren't.

### `bgp-peer-shutdown`
- **Fault:** router1's eBGP neighbor `10.0.0.2` is admin-disabled. The interface and the
  `10.0.0.0/30` link stay **up**.
- **Why medium:** the interface check (the easy reflex) says "all up" — that's the lie. The
  agent must check BGP *session state*, not link state. It is in syslog (session down), but
  only if the agent looks past the healthy-looking interfaces.

### `missing-export-policy`
- **Fault:** router1's eBGP `export-policy` is removed. Session is Established, but router1
  advertises none of its LAN prefixes, so router2 has no return route.
- **Why medium / silent:** "BGP Established" is the lie — checking session state and stopping
  is an explicit MISS. The agent must inspect **RIB-in / RIB-out** (what's actually
  advertised/received), one layer below session state. Nothing is logged.

### `vlan-mismatch`
- **Fault:** on switch1, client1's access port is moved into VLAN20 (client2's segment)
  instead of VLAN10. The port stays **up**.
- **Why medium / silent:** the port is up and configured, so L1 checks pass — that's the lie.
  The fault lives in L2 membership; the tell is that client1 can't ARP its gateway while
  client2 (same switch) is fine. No syslog event.

---

## Hard tier

> **Silent** (nothing in syslog), the config *looks* complete at every layer the agent
> normally checks, and the symptom needs **active, multi-signal probing**. "No problem
> found" is the trap.

### `wrong-gateway-ip`
- **Fault:** router1's VLAN10 sub-interface (`ethernet-1/1.10`) has IP `192.168.1.2/24`
  instead of `.1/24` — a one-digit typo. client1's gateway (`.1`) simply doesn't exist, so
  ARP fails.
- **Why hard:** every interface is up, BGP is fine, routing is fine — nothing is "broken,"
  the address is just *wrong*. The agent must compare client1's **configured gateway**
  against the **actual IP** on the sub-interface and spot a value mismatch. Silent.

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

## Quick reference

| Scenario | Tier | Device | Layer | In syslog? | What localizes it |
|---|---|---|---|---|---|
| `intf-down` | easy | router1 e1-2 | L1 | ✅ | one `show interface` |
| `client3-port-down` | easy | switch2 e1-2 | L1 | ✅ | one `show interface` / LLDP |
| `bgp-peer-shutdown` | medium | router1 neighbor | L3 | ✅ | BGP session state (links lie) |
| `missing-export-policy` | medium | router1 export-policy | L3 | ❌ | RIB-out (session state lies) |
| `vlan-mismatch` | medium | switch1 e1-1 | L2 | ❌ | VLAN membership + ARP |
| `wrong-gateway-ip` | hard | router1 e1-1.10 | L3 | ❌ | configured vs. actual gateway IP |
| `acl-silent-drop` | hard | router2 ACL | data plane | ❌ | ACL drop counters |
| `one-way-route-filter` | hard | router1 import-policy | L3 | ❌ | route-table asymmetry (2 RIBs) |
| `mtu-blackhole` | hard | router1 e1-2 | data plane | ❌ | packet-size probing + MTU |
