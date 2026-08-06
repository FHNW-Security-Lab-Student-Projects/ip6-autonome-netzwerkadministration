# Monitoring Stack — Centralized Log Collection for Containerlab

## Overview

Every network node in the lab (SR Linux switches and routers, and any future Arista/Cisco nodes) forwards its syslog stream to a centralized collection stack that runs alongside the lab inside the same containerlab management network.

| Component | Image | Role |
|---|---|---|
| **Grafana Alloy** | `grafana/alloy:latest` | Syslog UDP receiver; labels and ships log lines |
| **Loki** | `grafana/loki:3.0.0` | Log storage and query engine |
| **Grafana** | `grafana/grafana:11.0.0` | Visualization UI; Loki datasource is auto-provisioned |

All three containers are declared as `linux` nodes in `testlab.clab.yml` with fixed IPs on the `172.20.20.0/24` management network:

| Container | Fixed IP | Exposed port |
|---|---|---|
| alloy | `172.20.20.100` | `12345` (web UI), `1514` (syslog receiver, UDP) |
| loki | `172.20.20.101` | `3100` (HTTP API) |
| grafana | `172.20.20.102` | `3000` (UI) |

---

## Architecture

```
┌─────────────────────── 172.20.20.0/24 (clab-mgmt) ──────────────────────────┐
│                                                                               │
│  switch1 (.x)  ──syslog UDP──►                                                │
│  switch2 (.x)  ──syslog UDP──►  alloy (.100:1514)                             │
│  router1 (.x)  ──syslog UDP──►       │ push                                   │
│  router2 (.x)  ──syslog UDP──►       ▼                                        │
│                                  loki (.101:3100)                              │
│                                       │ query                                  │
│                                       ▼                                        │
│                                  grafana (.102:3000)  ◄── browser              │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
```

Every syslog message travels:
1. **Network node → Alloy** over UDP on port `1514`
2. **Alloy → Loki** via HTTP push to `172.20.20.101:3100`
3. **Loki → Grafana** via the pre-provisioned Loki datasource when you run a query

---

## How Each Component Is Configured

### Grafana Alloy — `monitoring/alloy/config.alloy`

Alloy uses its own River-based config language (`.alloy` files). The pipeline is wired as three components:

**`loki.source.syslog "network_syslog"`** — opens a UDP syslog listener on `0.0.0.0:1514`. For each incoming message it applies `relabel_rules` from `loki.relabel.syslog_meta` before forwarding to the process stage.

**`loki.relabel "syslog_meta"`** — extracts labels from the RFC 5424 syslog header:
- `host` — the hostname field (e.g. `clab-testlab-router1`)
- `app` — the app-name field (process that generated the log)
- `severity` — syslog severity level
- `facility` — syslog facility

**`loki.process "vendor_detection"`** — applies per-vendor `stage.match` blocks that add a `vendor` label and optionally parse the message body with a regex. All logs from different vendors land in the same Loki stream but are fully distinguishable by label.

Example: after processing a log from `clab-testlab-router1`, the resulting label set looks like:

```
{job="network-syslog", host="clab-testlab-router1", vendor="nokia_srlinux",
 severity="informational", facility="local6", app="sr_linux_mgmt"}
```

### Loki — `monitoring/loki/config.yml`

Minimal single-node configuration:
- Stores chunks and indexes on the local filesystem under `/loki/` inside the container
- Uses the `tsdb` index and schema `v13`
- No authentication (`auth_enabled: false`) — appropriate for a local lab
- Rejects samples older than 7 days (`reject_old_samples_max_age: 168h`)

Loki does **not** re-index the log content itself; it indexes only the labels. Full-text search is done by scanning stored chunks at query time.

### Grafana — `monitoring/grafana/provisioning/datasources/loki.yml`

The Loki datasource is provisioned automatically on startup — no manual setup required. When you open Grafana the datasource is already wired to `http://172.20.20.101:3100`.

Anonymous access is enabled so you can open the UI immediately without logging in. The default admin credentials (if login is needed) are `admin / admin`.

---

## SR Linux Syslog Configuration — `configs/<node>.cfg`

Each SR Linux node references its own per-device startup config via `startup-config` in the topology:

```yaml
switch1:
  kind: nokia_srlinux
  startup-config: configs/switch1.cfg
```

All four files (`configs/switch1.cfg`, `switch2.cfg`, `router1.cfg`, `router2.cfg`) inline the same syslog block alongside the node's forwarding config, applied by containerlab when the node boots (from `configs/switch1.cfg`):

```
set / system logging network-instance mgmt
set / system logging remote-server 172.20.20.100 transport udp
set / system logging remote-server 172.20.20.100 remote-port 1514
set / system logging remote-server 172.20.20.100 facility local6 priority match-above informational
```

This configures a remote syslog destination at `172.20.20.100:1514` (the Alloy container) over UDP, explicitly bound to the `mgmt` network instance. Messages of facility `local6` at priority `informational` and above are forwarded.

---

## Adding a New Vendor

The steps are the same regardless of vendor. Alloy already has pipeline stages for Arista EOS, Cisco IOS-XR, and Cisco IOS-XE in `monitoring/alloy/config.alloy` — this vendor path is prepared in the pipeline but was **never implemented in the lab**, which contains only Nokia SR Linux devices.

For Arista cEOS the image must be obtained manually first: register a free account at [arista.com](https://www.arista.com/en/user-registration), download the cEOS-lab tarball from **Software Downloads → cEOS-lab** (e.g. `cEOS-lab-4.32.0F.tar.xz`), and load it with `docker import cEOS-lab-4.32.0F.tar.xz ceos:4.32.0`.

### Step 1 — Add the node to the topology

```yaml
# testlab.clab.yml
leaf1:
  kind: ceos          # Arista cEOS
  image: ceos:4.32.0
  labels:
    role: switch
    layer: access
```

### Step 2 — Configure syslog forwarding on the device

Each vendor has its own command to point syslog at the Alloy receiver (`172.20.20.100:1514`):

| Vendor | Command |
|---|---|
| **Arista EOS** | `logging host 172.20.20.100 1514 protocol udp` |
| **Cisco IOS-XR** | `logging 172.20.20.100 vrf mgmt port 1514` |
| **Cisco IOS-XE** | `logging host 172.20.20.100 transport udp port 1514` |
| **FRR / Linux** | Add a forward rule to `/etc/rsyslog.conf` |

For Arista cEOS you can apply this via a startup config file:

```yaml
leaf1:
  kind: ceos
  image: ceos:4.32.0
  startup-config: configs/eos-syslog.cfg
```

`configs/eos-syslog.cfg`:
```
!
logging host 172.20.20.100 1514 protocol udp
logging on
!
```

### Step 3 — Adjust the vendor detection regex in Alloy (if needed)

Open `monitoring/alloy/config.alloy` and find the `stage.match` block for your vendor. The selector matches on the `host` label, which is the hostname that appears in the syslog message — by default the container name assigned by containerlab (`clab-<lab>-<node-name>`).

For example, if you named your Arista node `leaf1` the container will be `clab-testlab-leaf1`. The existing Arista selector already covers this:

```alloy
stage.match {
  selector = "{host=~\"clab-testlab-.*(eos|arista|leaf|spine).*\"}"
  ...
}
```

If your node name doesn't match any existing pattern, add a new `stage.match` block:

```alloy
stage.match {
  selector = "{host=\"clab-testlab-leaf1\"}"
  stage.static_labels {
    values = { vendor = "arista_eos" }
  }
}
```

---

## Querying Logs in Grafana

Open `http://localhost:3000` in your browser. Navigate to **Explore** and select the **Loki** datasource.

### Useful LogQL queries

```logql
# All logs from all network nodes
{job="network-syslog"}

# Logs from a specific node
{host="clab-testlab-router1"}

# All Nokia SR Linux logs
{vendor="nokia_srlinux"}

# Only errors and above across all vendors
{job="network-syslog", severity=~"error|critical|alert|emergency"}

# Arista logs containing "BGP"
{vendor="arista_eos"} |= "BGP"

# Logs from all routers (role label is not on syslog labels — filter by host name)
{host=~"clab-testlab-router.*"}
```

### Tip — correlate with topology labels

The `host` label value is always the containerlab container name (`clab-testlab-<node>`). Since containerlab node names encode the role (`switch`, `router`) you can filter by regex without any extra label configuration:

```logql
{host=~"clab-testlab-switch.*"}   # all switch logs
{host=~"clab-testlab-router.*"}   # all router logs
```

---

## File Reference

```
testlab.clab.yml                            ← mgmt network + monitoring nodes + SR Linux startup-configs
configs/
  switch1.cfg  switch2.cfg
  router1.cfg  router2.cfg                  ← per-device startup configs, each inlines the syslog block
monitoring/
  alloy/config.alloy                        ← syslog receiver + per-vendor pipeline stages
  loki/config.yml                           ← storage config (filesystem, single-node)
  grafana/
    provisioning/datasources/loki.yml       ← auto-provisions Loki datasource in Grafana
```
