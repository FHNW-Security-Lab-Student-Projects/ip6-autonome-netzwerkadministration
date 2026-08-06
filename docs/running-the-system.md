# Running the System

This guide explains how to start all agents and the web UI.

## Prerequisites

### 1. Environment variables

Copy `.env.example` to `.env` and set the required values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key — used by all LLM agents |
| `LOGFIRE_TOKEN` | No | Logfire write token for observability (skipped if absent) |
| `LOGFIRE_READ_TOKEN` | No | Logfire read token for `fetch_logfire_exports.py` |
| `AUTO_INVESTIGATIONS_ENABLED` | No | Set to `false` to pause automatic investigation creation from Loki polling |
| `INVESTIGATION_STATUS_PORT` | No | Port for the `investigation_status.py` web UI (default: 7933) |
| `AGENT_API_PORT` | No | Port for the syslog agent's internal HTTP API (default: 7934) |

Two feature flags exist in `client_agent.py` (not in `.env.example`): `ENABLE_CONFIG_AGENT`
and `ENABLE_INVESTIGATIONS`, both on by default. Set them to `0` to disable — this is how
`experiment_runner.py` runs a reduced orchestrator without the config agent and the syslog
investigation tools.

### 2. Install dependencies

```bash
uv sync
```

### 3. Start the ContainerLab network

The agents connect to real SR Linux devices. The lab must be running before you start any agent.

```bash
sudo containerlab deploy -t testlab.clab.yml
```

To stop the lab later:

```bash
sudo containerlab destroy -t testlab.clab.yml
```

---

## Starting the agents

All agents (orchestrator, network, topology, snapshot, syslog, and config agents) and their MCP servers start together from a single entry point. You do **not** start sub-agents separately.

### Option A — Terminal REPL (CLI)

```bash
uv run python client_agent.py
```

This starts all sub-agent lifespans (MCP servers, topology refresh loop, Loki poller) and opens an interactive prompt:

```
Type your message and press Enter. Press Ctrl+C or type "exit" to quit.

You:
```

Type `exit` or press `Ctrl+C` to stop.

### Option B — Web UI

```bash
uv run python web_ui.py
```

This starts the same sub-agent lifespans as the CLI and serves a chat interface at:

```
http://127.0.0.1:7932
```

Open that URL in a browser to interact with the orchestrator.

---

## What starts automatically

Both entry points compose the following background services via `main_lifespan()`:

| Component | Defined in | What it does |
|---|---|---|
| `network_lifespan()` | `network_agent.py` | Starts the `mcp_server.py` subprocess (JSON-RPC show commands via `srl_jsonrpc.py`) |
| `syslog_lifespan()` | `syslog_investigations.py` | Starts the `syslog_mcp_server.py` subprocess, the Loki polling loop (every 30 s), and the internal agent API on port 7934 |
| `topology_lifespan()` | `topology_agent.py` | Starts the background LLDP topology refresh loop (every 60 s) |
| `snapshot_lifespan()` | `state_snapshot_agent.py` | Loads persisted device state snapshots and runs the background snapshot refresh loop (every 120 s) |
| `config_lifespan()` | `config_agent.py` | Starts the `config_mcp_server.py` subprocess — only entered when `ENABLE_CONFIG_AGENT` is on (experiments disable it) |

All of these stop cleanly when you exit the CLI or stop the web server.

---

## Monitoring stack and web endpoints

The monitoring containers (Loki, Alloy, Grafana) are nodes inside `testlab.clab.yml` alongside the SR Linux devices, so they start automatically with `containerlab deploy` — nothing to launch separately.

| Service | URL |
|---|---|
| Grafana | http://localhost:3000 (admin / admin) |
| Loki | http://localhost:3100 (HTTP API) |
| Alloy web UI | http://localhost:12345 |
| Investigation status UI | http://127.0.0.1:7933 (start separately: `uv run python investigation_status.py`) |

In addition, the syslog agent serves an internal HTTP API on `127.0.0.1:7934` (started by `syslog_lifespan()`, used by the investigation status UI — not meant for direct use).
