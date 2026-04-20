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
| `LOGFIRE_TOKEN` | No | Logfire token for observability (skipped if absent) |

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

All agents (orchestrator, network agent, topology agent, syslog agent) and their MCP servers start together from a single entry point. You do **not** start sub-agents separately.

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

| Component | Started by | What it does |
|---|---|---|
| `network_lifespan()` | `network_agent.py` | Starts the `mcp_server.py` subprocess (netmiko show commands) |
| `syslog_lifespan()` | `syslog_agent.py` | Starts the `syslog_mcp_server.py` subprocess and the Loki polling loop (every 30 s) |
| `topology_lifespan()` | `topology_agent.py` | Starts the background LLDP topology refresh loop (every 60 s) |

All of these stop cleanly when you exit the CLI or stop the web server.

---

## Optional: Monitoring stack

If you want syslog visibility in Grafana, start the monitoring containers before deploying the lab (they are defined inside `testlab.clab.yml` alongside the SR Linux nodes and start automatically with `containerlab deploy`).

| Service | URL |
|---|---|
| Grafana | http://localhost:3000 (admin / admin) |
| Promtail | http://localhost:9080 |
