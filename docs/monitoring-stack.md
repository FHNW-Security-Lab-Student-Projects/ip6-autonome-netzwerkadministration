# Monitoring Stack — Centralized Log Collection for Containerlab

## Overview

Every network node in the lab (SR Linux switches and routers, and any future Arista/Cisco nodes) forwards its syslog stream to a centralized collection stack that runs alongside the lab inside the same containerlab management network.

| Component | Image | Role |
|---|---|---|
| **Promtail** | `grafana/promtail:3.0.0` | Syslog TCP receiver; labels and ships log lines |
| **Loki** | `grafana/loki:3.0.0` | Log storage and query engine |
| **Grafana** | `grafana/grafana:11.0.0` | Visualization UI; Loki datasource is auto-provisioned |

All three containers are declared as `linux` nodes in `testlab.clab.yml` with fixed IPs on the `172.20.20.0/24` management network:

| Container | Fixed IP | Exposed port |
|---|---|---|
| promtail | `172.20.20.100` | `9080` (web UI), `1514` (syslog receiver) |
| loki | `172.20.20.101` | `3100` (HTTP API) |
| grafana | `172.20.20.102` | `3000` (UI) |

---

## Architecture

```
┌─────────────────────── 172.20.20.0/24 (clab-mgmt) ──────────────────────────┐
│                                                                               │
│  switch1 (.x)  ──syslog TCP──►                                                │
│  switch2 (.x)  ──syslog TCP──►  promtail (.100:1514)                          │
│  router1 (.x)  ──syslog TCP──►       │ push                                   │
│  router2 (.x)  ──syslog TCP──►       ▼                                        │
│                                  loki (.101:3100)                              │
│                                       │ query                                  │
│                                       ▼                                        │
│                                  grafana (.102:3000)  ◄── browser              │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
```

Every syslog message travels:
1. **Network node → Promtail** over TCP on port `1514`
2. **Promtail → Loki** via HTTP push to `172.20.20.101:3100`
3. **Loki → Grafana** via the pre-provisioned Loki datasource when you run a query

---

## How Each Component Is Configured

### Promtail — `monitoring/promtail/config.yml`

Promtail opens a TCP syslog listener on `0.0.0.0:1514`. For each incoming message it:

1. **Extracts labels** from the RFC 5424 syslog header using `relabel_configs`:
   - `host` — the hostname field from the syslog message (e.g. `clab-testlab-router1`)
   - `app` — the app-name field (process that generated the log)
   - `severity` — syslog severity level
   - `facility` — syslog facility

2. **Applies per-vendor pipeline stages** using `match` selectors on the `host` label. Each stage adds a `vendor` label and optionally parses the message body with a regex. This means all logs from different vendors land in the same Loki stream but are fully distinguishable by label.

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

## SR Linux Syslog Configuration — `configs/srl-syslog.cfg`

Each SR Linux node references this file via `startup-config` in the topology:

```yaml
switch1:
  kind: nokia_srlinux
  startup-config: configs/srl-syslog.cfg
```

The file contains three SR Linux CLI `set` commands that containerlab applies when the node boots:

```
set / system logging remote-server 172.20.20.100 transport tcp
set / system logging remote-server 172.20.20.100 port 1514
set / system logging remote-server 172.20.20.100 subsystem all severity informational
```

This configures a remote syslog destination at `172.20.20.100:1514` (the Promtail container). All subsystems at severity `informational` and above are forwarded. SR Linux management-plane traffic (including syslog) automatically uses the `mgmt` network instance, so no explicit network-instance binding is needed here.

---

## Adding a New Vendor

The steps are the same regardless of vendor. Promtail already has pipeline stages for Arista EOS, Cisco IOS-XR, and Cisco IOS-XE in `monitoring/promtail/config.yml`.

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

Each vendor has its own command to point syslog at the Promtail receiver (`172.20.20.100:1514`):

| Vendor | Command |
|---|---|
| **Arista EOS** | `logging host 172.20.20.100 1514 protocol tcp` |
| **Cisco IOS-XR** | `logging 172.20.20.100 vrf mgmt port 1514` |
| **Cisco IOS-XE** | `logging host 172.20.20.100 transport tcp port 1514` |
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
logging host 172.20.20.100 1514 protocol tcp
logging on
!
```

### Step 3 — Adjust the vendor detection regex in Promtail (if needed)

Open `monitoring/promtail/config.yml` and find the `match` block for your vendor. The selector matches on the `host` label, which is the hostname that appears in the syslog message — by default the container name assigned by containerlab (`clab-<lab>-<node-name>`).

For example, if you named your Arista node `leaf1` the container will be `clab-testlab-leaf1`. The existing Arista selector already covers this:

```yaml
- match:
    selector: '{host=~"clab-testlab-.*(eos|arista|leaf|spine).*"}'
```

If your node name doesn't match any existing pattern, add a new `match` block:

```yaml
- match:
    selector: '{host="clab-testlab-leaf1"}'
    stages:
      - static_labels:
          vendor: arista_eos
```

No restart of any component is needed — Promtail hot-reloads its config.

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
testlab.clab.yml                            ← mgmt network + monitoring nodes + SR Linux startup-config
configs/
  srl-syslog.cfg                            ← SR Linux: sets remote-server 172.20.20.100:1514
monitoring/
  promtail/config.yml                       ← syslog receiver + per-vendor pipeline stages
  loki/config.yml                           ← storage config (filesystem, single-node)
  grafana/
    provisioning/datasources/loki.yml       ← auto-provisions Loki datasource in Grafana
```
