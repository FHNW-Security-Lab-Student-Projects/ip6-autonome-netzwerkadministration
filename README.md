# IP6 — AI Multi-Agent Network Troubleshooting

An AI-driven multi-agent system for troubleshooting network problems, built with
[Pydantic AI](https://ai.pydantic.dev/) at FHNW. An orchestrator agent delegates to
specialised sub-agents that inspect a live [ContainerLab](https://containerlab.dev/)
network of Nokia SR Linux devices over MCP tools.

The project evaluates **what kinds of network problems can be troubleshooted by an agent
network**, measuring time saved, the difficulty levels that can be diagnosed, the
success rate per difficulty level, and the potential cost savings.

## Agents

A single orchestrator (`client_agent.py`) routes each request to the right sub-agent:

| Agent | Module | Responsibility |
|---|---|---|
| **Orchestrator** | `client_agent.py` | Routes requests, owns the conversation, coordinates delegation |
| **Network** | `network_agent.py` + `mcp_server.py` | Read-only SR Linux show commands (JSON-RPC via `srl_jsonrpc.py`) |
| **Topology** | `topology_agent.py` | LLDP-based topology discovery (no LLM, background refresh) |
| **Config** | `config_agent.py` + `config_mcp_server.py` | Validate-then-apply configuration changes |
| **Snapshot** | `state_snapshot_agent.py` | Capture / compare device state snapshots |
| **Syslog** | `syslog_agent.py` + `syslog_mcp_server.py` | Syslog incident investigation + background Loki poller |
| **Investigation orchestrator** | `investigation_orchestrator.py` | Dedicated orchestrator for autonomous syslog-event analysis, read-only sub-agent roster, config agent excluded |

## Prerequisites

ContainerLab needs Linux, so the easiest path on any OS is the included **devcontainer**:
open the repo in VS Code and choose *Reopen in Container*. The image ships Docker-in-Docker
and containerlab, and the post-create hook installs uv, Python 3.13, and all dependencies,
only Docker and VS Code are needed on the host. Alternatively, on a native Linux host,
install [Docker](https://docs.docker.com/engine/install/),
[containerlab](https://containerlab.dev/install/), and [uv](https://docs.astral.sh/uv/)
yourself.

All lab images (Nokia SR Linux 24.10.1, network-multitool, Grafana/Loki/Alloy) are publicly
pullable no vendor account or license required and are downloaded on first deploy. Give
Docker roughly 8 GB of RAM for the four SR Linux nodes.

## Setup

```bash
cp .env.example .env        # set OPENROUTER_API_KEY (LOGFIRE_TOKEN optional)
uv sync                     # install dependencies (already done inside the devcontainer)
```

The API key comes from [openrouter.ai](https://openrouter.ai/) a few dollars of credit is
enough to try the system out. The default model is `z-ai/glm-5`, configured per agent
module.

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter key (used by all LLM agents) |
| `LOGFIRE_TOKEN` | No | Logfire observability (skipped if absent) |
| `LOGFIRE_READ_TOKEN` | No | Logfire read token for `fetch_logfire_exports.py` |
| `AUTO_INVESTIGATIONS_ENABLED` | No | Pause automatic Loki-driven investigations (`true`/`false`) |
| `INVESTIGATION_STATUS_PORT` | No | Port for the `investigation_status.py` web UI (default 7933) |
| `AGENT_API_PORT` | No | Port for the syslog agent's internal HTTP API (default 7934) |

Two additional code flags, `ENABLE_CONFIG_AGENT` and `ENABLE_INVESTIGATIONS` (default on,
set to `0` to disable), let `experiment_runner.py` run a reduced orchestrator without the
config agent or the syslog investigation tools.

### Start the network lab

The agents talk to real SR Linux devices, so the lab must be running first:

```bash
sudo containerlab deploy -t testlab.clab.yml      # destroy with: ... destroy ...
```

Topology (see [testlab.clab.yml](testlab.clab.yml)):

```
client1 ─ switch1 ─ router1 ═BGP═ router2 ─ switch2 ─ client3
client2 ─┘                                         └─ client4
```

client3 (10.10.10.10) and client4 (10.10.10.11) share VLAN30 behind switch2 (ports e1-2
and e1-3). Containers are named `clab-testlab-<node>` (e.g. `clab-testlab-router1`). The
monitoring stack (Grafana, Loki, and Grafana Alloy) defined in the same file starts
automatically.

## Running

All sub-agents and their MCP servers start from a single entry point, you do **not**
launch them separately.

```bash
uv run python client_agent.py     # interactive terminal REPL
uv run python web_ui.py           # chat UI at http://127.0.0.1:7932
```

See [docs/running-the-system.md](docs/running-the-system.md) for the full startup guide.

### Quick test drive

Inject a known fault, ask the orchestrator to find it, then restore the baseline:

```bash
./scenarios/intf-down/setup.sh      # captures a healthy baseline, then disables a router link
uv run python client_agent.py
# You: Client1 reports it can no longer reach client3. Investigate the root cause.
./scenarios/intf-down/teardown.sh   # restores the healthy state
```

The agent should trace the outage to `ethernet-1/2` being admin-disabled on router1.

## Automatic syslog investigations

While the agents are running, a background poller checks Loki every 30 seconds for syslog
events of severity `error` and above from the lab devices. Each new event automatically
opens an **investigation**: a dedicated read-only orchestrator
(`investigation_orchestrator.py`, config agent excluded) queries the logs around the
event, inspects the affected devices, and writes its root-cause analysis to
`investigations.json`.

Watch it live in the status UI (standalone process, run alongside either entry point):

```bash
uv run python investigation_status.py     # → http://127.0.0.1:7933
```

The page lists every investigation with its status `investigating` (agent working) →
`waiting` (analysis done, awaiting review) → `resolved`. Click a row to see the
triggering event, the full investigation log, and the agent's summary, the detail panel
has a button to mark the investigation resolved.

Nothing needs to be forced to see it work: the SR Linux nodes emit error-level events on
their own (license notices at startup, memory-utilization criticals), so investigations
appear shortly after lab and agents are up. From the chat you can also list
investigations, ask follow-up questions on one, or open one manually ("open an
investigation for router1").

Set `AUTO_INVESTIGATIONS_ENABLED=false` in `.env` to pause automatic openings.

## Documentation

Architecture and how-to notes live in [docs/](docs/):

- [running-the-system.md](docs/running-the-system.md): start the lab, agents, and web UI
- [target-topology.md](docs/target-topology.md) · [containerlab-commands.md](docs/containerlab-commands.md) · [monitoring-stack.md](docs/monitoring-stack.md)

The written report lives in [IP6-Bericht/](IP6-Bericht/).
