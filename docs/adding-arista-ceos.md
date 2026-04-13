# Adding Arista cEOS as a Second Vendor

## What is already done

The Promtail pipeline stage for Arista EOS is already written in
`monitoring/promtail/config.yml`. No changes to the monitoring stack are needed.
The only work is:

1. Loading the cEOS Docker image onto the host (one-time, manual step)
2. Creating `configs/eos-syslog.cfg` (startup config)
3. Adding the cEOS node(s) to `testlab.clab.yml`

---

## Step 1 — Get the cEOS image (manual, one-time)

1. Create a free account at https://www.arista.com/en/user-registration
2. Go to **Software Downloads → cEOS-lab** and download the latest `.tar.xz`
   (e.g. `cEOS-lab-4.32.0F.tar.xz`)
3. Load it into Docker:
   ```bash
   docker import cEOS-lab-4.32.0F.tar.xz ceos:4.32.0
   ```
4. Verify:
   ```bash
   docker images | grep ceos
   ```

---

## Step 2 — Create the syslog startup config

Create `configs/eos-syslog.cfg` with this content:

```
!
logging host 172.20.20.100 1514 protocol tcp
logging on
!
```

This points syslog at the Promtail receiver (`172.20.20.100:1514 TCP`).

---

## Step 3 — Add the node to testlab.clab.yml

Add one or more cEOS nodes in the `nodes:` section. Example — a leaf switch:

```yaml
leaf1:
  kind: ceos
  image: ceos:4.32.0
  startup-config: configs/eos-syslog.cfg
  labels:
    role: switch
    layer: access
```

Then add links connecting it to the existing topology, for example:

```yaml
links:
  - endpoints: ["router2:e1-3", "leaf1:eth1"]
  - endpoints: ["leaf1:eth2", "client3:eth1"]   # if moving client3 to leaf1
```

---

## Step 4 — Verify logs appear in Grafana

After `containerlab deploy`, open `http://localhost:3000` and run this LogQL query
in **Explore**:

```logql
{vendor="arista_eos"}
```

You should see log lines from `clab-testlab-leaf1` (or whatever you named the node).

---

## How the Promtail vendor detection works for cEOS

The existing `match` selector in `monitoring/promtail/config.yml` is:

```yaml
- match:
    selector: '{host=~"clab-testlab-.*(eos|arista|leaf|spine).*"}'
    stages:
      - static_labels:
          vendor: arista_eos
```

This matches any node whose containerlab-assigned hostname contains `eos`, `arista`,
`leaf`, or `spine`. A node named `leaf1` becomes `clab-testlab-leaf1` — matched
automatically, no Promtail config change needed.

If you name your node something that doesn't match (e.g. `access1`), either:
- Rename it to include `leaf`/`arista`/`eos`, or
- Add it explicitly: change the selector to `'{host=~"clab-testlab-.*(eos|arista|leaf|spine|access).*"}'`

---

## Summary of files to create/change

| File | Action |
|---|---|
| `configs/eos-syslog.cfg` | Create (content above) |
| `testlab.clab.yml` | Add `leaf1` node + links |
| `monitoring/promtail/config.yml` | No change needed (unless node name doesn't match) |
