# Experiment Runner

## Purpose

`experiment_runner.py` is a terminal-based tool for running structured LLM comparison experiments. Unlike the web UI path, it calls `orchestrator.run()` directly, which means it can capture the orchestrator's own token usage in addition to all sub-agent usage — giving you a complete cost and latency breakdown for every query.

Results are appended to the same `experiment_log.jsonl` file used by the web UI tracker.

---

## When to use this vs. the web UI

| | Web UI | Experiment Runner |
|---|---|---|
| Orchestrator tokens tracked | No (Logfire only) | **Yes** |
| Sub-agent tokens tracked | Yes | Yes |
| Scriptable / batch mode | No | Yes |
| Reproducible identical queries | No | Yes |
| Good for demos | Yes | No |

Use the experiment runner for any run where you want to compare models on identical inputs. Use the web UI for interactive exploration and demos.

---

## Usage

```bash
# Interactive — type queries one at a time, "exit" to stop
uv run python experiment_runner.py --model anthropic/claude-sonnet-4.6 --scenario bgp-01

# Batch — run all queries in a file
uv run python experiment_runner.py --model z-ai/glm-5 --scenario bgp-01 --file scenarios/bgp-troubleshooting.txt

# Multi-turn — carry conversation history between queries in the same run
uv run python experiment_runner.py --model z-ai/glm-5 --scenario bgp-multi --file scenarios/bgp-troubleshooting.txt --multi-turn

# Use the EXPERIMENT_SCENARIO env var instead of --scenario
EXPERIMENT_SCENARIO=bgp-01 uv run python experiment_runner.py --model z-ai/glm-5 --file scenarios/bgp-troubleshooting.txt
```

### CLI options

| Flag | Default | Description |
|---|---|---|
| `--model` / `-m` | `z-ai/glm-5` | OpenRouter model ID for all agents |
| `--scenario` / `-s` | `$EXPERIMENT_SCENARIO` or `""` | Label written to every session record |
| `--file` / `-f` | *(interactive)* | Path to query file (one query per line) |
| `--multi-turn` | off | Carry message history between queries |

---

## Comparing models on the same scenario

Run the same query file with each model you want to compare:

```bash
uv run python experiment_runner.py -m z-ai/glm-5                    -s bgp-01 -f scenarios/bgp-troubleshooting.txt
uv run python experiment_runner.py -m anthropic/claude-sonnet-4.6   -s bgp-01 -f scenarios/bgp-troubleshooting.txt
uv run python experiment_runner.py -m google/gemini-2.0-flash-001   -s bgp-01 -f scenarios/bgp-troubleshooting.txt
```

All three runs share the same `scenario` label. The `model` field differs.

---

## Analysing results with Python and pandas

### Load the log

```python
import json
import pandas as pd

records = []
with open('experiment_log.jsonl') as f:
    for line in f:
        records.append(json.loads(line))

df = pd.DataFrame(records)
```

### Top-level comparison — one row per (scenario, model)

```python
summary = (
    df.groupby(['scenario', 'model'])
    .agg(
        turns            = ('session_id',          'count'),
        avg_duration_s   = ('duration_s',           'mean'),
        avg_input_tokens = ('total_input_tokens',   'mean'),
        avg_output_tokens= ('total_output_tokens',  'mean'),
        avg_tool_calls   = ('total_tool_calls',     'mean'),
        total_cost_usd   = ('total_cost_usd',       'sum'),
        success_rate     = ('success',              'mean'),
    )
    .round(3)
)
print(summary)
```

### Per-agent breakdown — orchestrator vs. sub-agents

Each session record has an `agent_runs` list. Explode it into a flat table:

```python
agent_rows = []
for rec in records:
    for run in rec['agent_runs']:
        agent_rows.append({
            'session_id'   : rec['session_id'],
            'scenario'     : rec['scenario'],
            'model'        : rec['model'],
            'agent_name'   : run['agent_name'],
            'input_tokens' : run['input_tokens'],
            'output_tokens': run['output_tokens'],
            'tool_calls'   : run['tool_calls'],
            'duration_s'   : run['duration_s'],
            'cost_usd'     : run['estimated_cost_usd'],
        })

agents_df = pd.DataFrame(agent_rows)

# Average tokens and duration split by agent, per model
print(
    agents_df.groupby(['model', 'agent_name'])[
        ['input_tokens', 'output_tokens', 'tool_calls', 'duration_s']
    ].mean().round(1)
)
```

### Orchestrator cost share

How much of the total cost comes from the orchestrator itself vs. the sub-agents?

```python
orch = agents_df[agents_df['agent_name'] == 'orchestrator']
subs = agents_df[agents_df['agent_name'] != 'orchestrator']

print("Orchestrator avg cost per turn:")
print(orch.groupby('model')['cost_usd'].mean())

print("\nSub-agents avg cost per turn:")
print(subs.groupby('model')['cost_usd'].mean())
```

### Slowest turns

```python
print(
    df.nlargest(10, 'duration_s')[
        ['scenario', 'model', 'duration_s', 'total_tool_calls', 'success']
    ]
)
```

### Failed turns

```python
failed = df[~df['success']]
if failed.empty:
    print('No failures.')
else:
    print(failed[['scenario', 'model', 'user_query', 'error']])
```

---

## Query files

Plain text, one query per line. Lines starting with `#` are treated as comments.

```text
# scenarios/bgp-troubleshooting.txt
Check the BGP session state on all routers and report any sessions that are not established.
Are there any recent BGP-related syslog events? Summarise what happened and on which device.
What routes is router1 advertising to its BGP peers?
```

Provided scenario files:

| File | Description |
|---|---|
| `scenarios/basic-reachability.txt` | Interface status, route lookup, device inventory |
| `scenarios/bgp-troubleshooting.txt` | BGP session state, syslog events, route advertisement |

---

## Per-turn output

Each query produces a summary table printed to the terminal:

```
────────────────────────────────────────────────────────────────────────
  Turn 1/3  |  Check the BGP session state on all routers...
────────────────────────────────────────────────────────────────────────
  Agent                Model                             In tok  Out tok  Tools      s
  ──────────────────── ────────────────────────────────  ───────  ───────  ─────  ──────
  network_agent        anthropic/claude-sonnet-4.6         4,821      412      4     9.2
  orchestrator         anthropic/claude-sonnet-4.6         1,204      183      2    14.8
────────────────────────────────────────────────────────────────────────
  Total: 6,025 in / 595 out  |  6 tool calls  |  14.8s  |  $0.000224  |  OK
```

Followed by a session summary at the end of all queries:

```
════════════════════════════════════════════════════════════════════════
  EXPERIMENT SUMMARY
  Model:    anthropic/claude-sonnet-4.6
  Scenario: bgp-01
  Turns:    3  (3 successful)
════════════════════════════════════════════════════════════════════════
  Avg duration:     12.4s
  Total tokens:    18,120 in  /  1,840 out
  Total tool calls: 14
  Estimated cost:  $0.000731
════════════════════════════════════════════════════════════════════════
  Results appended to: experiment_log.jsonl
```

---

## What is tracked vs. the web UI

In the runner, `agent_runs` in the session record includes an entry for `"orchestrator"` in addition to the sub-agents:

```json
{
  "agent_runs": [
    { "agent_name": "network_agent",  "input_tokens": 4821, "tool_calls": 4, ... },
    { "agent_name": "orchestrator",   "input_tokens": 1204, "tool_calls": 2, ... }
  ]
}
```

The orchestrator entry captures the LLM calls made by the orchestrator itself — deciding which tool to call, routing between sub-agents, and composing the final answer. These are the calls that cannot be captured via the web UI path.

---

## Limitations

- **Syslog investigator not tracked**: Background LLM investigation tasks (`syslog_investigator`) run independently and are not part of a query's session record. Their usage is captured in Logfire.
- **All sub-agents start**: `main_lifespan()` starts MCP servers for all agents (network, config, syslog, topology, snapshot). There is currently no way to start only a subset.
- **No streaming output**: The orchestrator's answer is printed only after the full run completes, unlike the web UI which streams tokens live.