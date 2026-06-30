# IP6 — AI Multi-Agent Network Troubleshooting

An AI-driven multi-agent system for troubleshooting network problems, built with
[Pydantic AI](https://ai.pydantic.dev/) at FHNW. An orchestrator agent delegates to
specialised sub-agents that inspect a live [ContainerLab](https://containerlab.dev/)
network of Nokia SR Linux devices over MCP tools.

The project evaluates **what kinds of network problems can be troubleshooted by an agent
network** — measuring time saved, the difficulty levels that can be diagnosed, the
success rate per difficulty level, and the potential cost savings.

> **Architecture note:** all agents run **in-process** using Pydantic AI's
> [agent delegation](https://ai.pydantic.dev/multi-agent-applications/) pattern. An
> earlier A2A-protocol design has been removed — there are no separate server processes
> or per-agent ports to start.

## Agents

A single orchestrator (`client_agent.py`) routes each request to the right sub-agent:

| Agent | Module | Responsibility |
|---|---|---|
| **Orchestrator** | `client_agent.py` | Routes requests, owns the conversation, coordinates delegation |
| **Network** | `network_agent.py` + `mcp_server.py` | Read-only SR Linux show commands (netmiko) |
| **Topology** | `topology_agent.py` | LLDP-based topology discovery (no LLM, background refresh) |
| **Config** | `config_agent.py` | Validate-then-apply configuration changes |
| **Snapshot** | `state_snapshot_agent.py` | Capture / compare device state snapshots |
| **Syslog** | `syslog_agent.py` + `syslog_mcp_server.py` | Syslog incident investigation + background Loki poller |

## Setup

```bash
cp .env.example .env        # set OPENROUTER_API_KEY (LOGFIRE_TOKEN optional)
uv sync                     # install dependencies (Python 3.13+, uv)
```

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes | OpenRouter key — used by all LLM agents |
| `LOGFIRE_TOKEN` | No | Logfire observability (skipped if absent) |
| `AUTO_INVESTIGATIONS_ENABLED` | No | Pause automatic Loki-driven investigations (`true`/`false`) |

### Start the network lab

The agents talk to real SR Linux devices, so the lab must be running first:

```bash
sudo containerlab deploy -t testlab.clab.yml      # destroy with: ... destroy ...
```

Topology (see [testlab.clab.yml](testlab.clab.yml)):

```
client1 ─ switch1 ─ router1 ═BGP═ router2 ─ switch2 ─ client3
client2 ─┘
```

Containers are named `clab-testlab-<node>` (e.g. `clab-testlab-router1`). The monitoring
stack (Grafana, Promtail) defined in the same file starts automatically.

## Running

All sub-agents and their MCP servers start from a single entry point — you do **not**
launch them separately.

```bash
uv run python client_agent.py     # interactive terminal REPL
uv run python web_ui.py           # chat UI at http://127.0.0.1:7932
```

See [docs/running-the-system.md](docs/running-the-system.md) for the full startup guide.

## Experiments

The fault-injection workflow: define a broken scenario, run the agents against it, score
whether they found the real root cause, then aggregate.

```bash
# ① author a scenario folder under scenarios/<name>/
#    (queries.txt, ground_truth.yaml, setup.sh, teardown.sh)

# ② run — injects fault → queries → restores baseline; appends to experiment_log.jsonl
uv run python experiment_runner.py -m anthropic/claude-sonnet-4.6 -s intf-down --multi-turn

# ③ evaluate by hand (press f/m/s per answer) → evaluation_log.jsonl
uv run python evaluate_experiments.py

# ④ aggregate / visualize
uv run python analyze_experiments.py
uv run python visualize_experiments.py --out figures
```

Full runbook: [docs/running-experiments.md](docs/running-experiments.md) ·
flag reference: [docs/experiment-runner.md](docs/experiment-runner.md).

## Documentation

Architecture and how-to notes live in [docs/](docs/):

- [running-the-system.md](docs/running-the-system.md) — start the lab, agents, and web UI
- [running-experiments.md](docs/running-experiments.md) — end-to-end experiment workflow
- [experiment-runner.md](docs/experiment-runner.md) · [analyze-experiments.md](docs/analyze-experiments.md) · [visualize-experiments.md](docs/visualize-experiments.md)
- [target-topology.md](docs/target-topology.md) · [adding-arista-ceos.md](docs/adding-arista-ceos.md) · [containerlab-commands.md](docs/containerlab-commands.md)
- [syslog-incident-agent.md](docs/syslog-incident-agent.md) · [monitoring-stack.md](docs/monitoring-stack.md) · [multi-model-web-ui.md](docs/multi-model-web-ui.md)

The written report lives in [IP6-Bericht/](IP6-Bericht/).
