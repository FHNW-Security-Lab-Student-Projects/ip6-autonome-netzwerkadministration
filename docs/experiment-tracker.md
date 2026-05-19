# Experiment Tracker — LLM Comparison

## Purpose

The experiment tracker records per-session metrics for structured LLM comparison experiments. The goal is to compare different LLMs on network troubleshooting tasks — measuring cost, latency, tool usage, and token consumption across agents.

> **The web UI (`web_ui.py`) does not do any tracking** — it is kept clean for demos.
> All experiment data is collected exclusively via `experiment_runner.py`.
> See [experiment-runner.md](experiment-runner.md) for how to run experiments.

Each query submitted through `experiment_runner.py` triggers one **session**. A session records:
- Which model was active
- Which sub-agents were invoked (including the orchestrator)
- Tokens consumed (input / output) per agent
- Number of LLM round-trips and MCP tool calls per agent
- Wall-clock duration per agent and total
- Estimated USD cost derived from a static pricing table
- An optional scenario label for structured experiment batches

Data is written to two outputs:
- **`experiment_log.jsonl`** — one JSON record per session, append-only, for offline analysis
- **Logfire span** `experiment_session` — queryable via SQL in the Logfire dashboard

---

## Files

| File | Role |
|---|---|
| `experiment_tracker.py` | Core module: dataclasses, pricing table, `begin/record/finish_session` |
| `client_agent.py` | Sub-agent tool functions record their runs into the active session |
| `experiment_runner.py` | Entry point for experiments — calls orchestrator directly, records full usage |

---

## Running Experiments

See [experiment-runner.md](experiment-runner.md) for the full usage guide.

### Quick start

```bash
uv run python experiment_runner.py --model anthropic/claude-sonnet-4.6 --scenario bgp-01 --file scenarios/bgp-troubleshooting.txt
```

### Tagging a scenario

Set `EXPERIMENT_SCENARIO` before starting the runner to label all sessions in a batch. This is the primary way to group sessions by experiment name for comparison.

```bash
EXPERIMENT_SCENARIO=bgp-flap-01 uv run python experiment_runner.py --model z-ai/glm-5 --file scenarios/bgp-troubleshooting.txt
```

Run the same command with a different `--model` to produce a second batch with the same `scenario`. Repeat for each LLM you want to compare.

---

## Output Format (`experiment_log.jsonl`)

Each line is a complete JSON record:

```json
{
  "session_id": "a3f1c8...",
  "started_at": "2026-05-18T14:32:10.123456+00:00",
  "model": "anthropic/claude-sonnet-4.6",
  "scenario": "bgp-flap-01",
  "user_query": "why is router1 not reachable from router2?",
  "agent_runs": [
    {
      "agent_name": "network_agent",
      "model": "anthropic/claude-sonnet-4.6",
      "input_tokens": 4821,
      "output_tokens": 412,
      "llm_requests": 3,
      "tool_calls": 4,
      "duration_s": 9.241,
      "estimated_cost_usd": 0.00006231
    }
  ],
  "duration_s": 14.87,
  "total_input_tokens": 4821,
  "total_output_tokens": 412,
  "total_cost_usd": 0.00006231,
  "total_tool_calls": 4,
  "total_llm_requests": 3,
  "success": true,
  "error": ""
}
```

### Field reference

| Field | Description |
|---|---|
| `session_id` | UUID hex — unique per user turn |
| `model` | OpenRouter model ID active for this session |
| `scenario` | Label from `EXPERIMENT_SCENARIO` env var, or `""` |
| `user_query` | First 300 chars of the user's message |
| `agent_runs` | List of per-agent records (see below) |
| `duration_s` | Wall-clock time from first byte of request to end of SSE stream |
| `total_input_tokens` | Sum of `input_tokens` across all agent runs |
| `total_output_tokens` | Sum of `output_tokens` across all agent runs |
| `total_cost_usd` | Estimated USD cost from pricing table (0.0 if model not in table) |
| `total_tool_calls` | Total MCP tool invocations across all agents |
| `total_llm_requests` | Total LLM API round-trips (>1 per agent when tool loops occur) |
| `success` | `false` if any unhandled exception occurred |
| `error` | Exception message when `success` is `false` |

**Per-agent run fields:**

| Field | Description |
|---|---|
| `agent_name` | `"network_agent"`, `"config_agent"`, or `"snapshot_agent"` |
| `llm_requests` | Number of LLM API calls in this agent's run (1 + number of tool-call loops) |
| `tool_calls` | Number of MCP tools invoked (e.g. `execute_show_command` calls) |

> **Note:** The orchestrator's own LLM calls (deciding which sub-agent to call) are not tracked as a separate `agent_run` in web UI mode — they are captured in Logfire under the `chat <model>` span. Only sub-agents explicitly delegated via tool functions are recorded.

---

## Analysing the Data

### Python / pandas

```python
import json
import pandas as pd

records = []
with open('experiment_log.jsonl') as f:
    for line in f:
        records.append(json.loads(line))

df = pd.DataFrame(records)

# Compare models on the same scenario
summary = df.groupby(['scenario', 'model']).agg(
    turns=('session_id', 'count'),
    avg_duration_s=('duration_s', 'mean'),
    avg_cost_usd=('total_cost_usd', 'mean'),
    avg_tool_calls=('total_tool_calls', 'mean'),
    avg_input_tokens=('total_input_tokens', 'mean'),
    avg_output_tokens=('total_output_tokens', 'mean'),
).round(4)

print(summary)
```

### Per-agent breakdown

```python
agent_rows = []
for rec in records:
    for run in rec['agent_runs']:
        agent_rows.append({
            'session_id': rec['session_id'],
            'scenario': rec['scenario'],
            'session_model': rec['model'],
            **run,
        })

agents_df = pd.DataFrame(agent_rows)
print(agents_df.groupby(['session_model', 'agent_name'])[['duration_s', 'tool_calls']].mean())
```

---

## Querying via Logfire

Every session emits a Logfire span with the aggregated fields as attributes. Query example:

```sql
SELECT
    attributes->>'model'                              AS model,
    attributes->>'scenario'                          AS scenario,
    COUNT(*)                                          AS sessions,
    AVG((attributes->>'duration_s')::float)           AS avg_duration_s,
    SUM((attributes->>'total_cost_usd')::float)       AS total_cost_usd,
    AVG((attributes->>'total_tool_calls')::float)     AS avg_tool_calls,
    AVG((attributes->>'total_input_tokens')::float)   AS avg_input_tokens
FROM records
WHERE span_name = 'experiment_session'
  AND start_timestamp >= now() - interval '7 days'
GROUP BY model, scenario
ORDER BY model, scenario
LIMIT 100
```

To inspect individual sessions including which agents were invoked:

```sql
SELECT
    start_timestamp,
    attributes->>'model'                AS model,
    attributes->>'scenario'             AS scenario,
    attributes->>'user_query'           AS query,
    (attributes->>'duration_s')::float  AS duration_s,
    (attributes->>'total_cost_usd')::float AS cost_usd,
    attributes->>'agents_invoked'       AS agents
FROM records
WHERE span_name = 'experiment_session'
  AND start_timestamp >= now() - interval '1 day'
ORDER BY start_timestamp DESC
LIMIT 50
```

---

## Keeping Costs Accurate

Cost estimates are computed from the `MODEL_PRICING` table in `experiment_tracker.py`. Prices change — update the table periodically from [openrouter.ai/models](https://openrouter.ai/models).

```python
# experiment_tracker.py
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # model_id: (input_usd_per_1M_tokens, output_usd_per_1M_tokens)
    'z-ai/glm-5': (0.0, 0.0),
    'anthropic/claude-sonnet-4.6': (3.0, 15.0),
    # add new models here
}
```

If a model is not in the table, `estimated_cost_usd` is recorded as `0.0`.

To cross-check total spend against actual OpenRouter billing:

```bash
curl -s https://openrouter.ai/api/v1/auth/key \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | jq '.data.usage'
```

This returns total credits used (in USD) since account creation — compare against the sum of `total_cost_usd` in `experiment_log.jsonl` to validate the pricing table.

---

## How It Works Internally

### Session lifecycle (web UI)

```
1. Browser sends POST /chat with { "model": "openrouter:...", "messages": [...] }

2. _ModelContextMiddleware.dispatch():
   - Parses model name and user query from the request body
   - Calls begin_session(user_query, model_name)
   - Sets _active_session ContextVar in the current asyncio task context
   - Calls call_next(request) → returns a StreamingResponse (SSE)
   - Attaches a BackgroundTask(finish_session, session) to the response

3. Pydantic AI's to_web() runs the orchestrator, producing SSE events.
   As the LLM calls sub-agent tools:
     call_network_agent() / call_config_agent() / call_snapshot_agent()
     → each times the sub-agent.run() call
     → reads _active_session from ContextVar
     → calls record_agent_run(session, agent_name, model, result, duration)

4. SSE stream ends. Starlette runs BackgroundTask:
   → finish_session(session) aggregates totals, writes to JSONL, emits Logfire span.
```

The `BackgroundTask` runs after the full response body has been sent to the client — by that point, all sub-agent tool calls have already completed and been recorded in the session.

### ContextVar propagation

`_active_session` is a `ContextVar`. Starlette's `BaseHTTPMiddleware` spawns the inner ASGI app in a child task that inherits the current asyncio context (including all ContextVar values set before `call_next`). This means the session set in step 2 is visible to tool functions called anywhere within the same request, including deeply nested sub-agent calls.

Multiple concurrent web UI requests each run in their own asyncio task context, so sessions are isolated — one request's `_active_session` cannot leak into another's.

---

## Adding a New Sub-Agent

If a new sub-agent tool is added to `client_agent.py`, wrap its `agent.run()` call to record the run:

```python
@orchestrator.tool_plain
async def call_my_new_agent(request: str) -> str:
    try:
        t0 = time.monotonic()
        result = await my_new_agent.run(request, model=_get_agent_model())
        session = _active_session.get()
        if session is not None:
            record_agent_run(session, 'my_new_agent', _effective_model(), result, time.monotonic() - t0)
        return result.output
    except APITimeoutError:
        return 'Timed out.'
```

No changes to `experiment_tracker.py` or `web_ui.py` are needed.

---

## Limitations

- **Orchestrator LLM calls not in `agent_runs`**: In web UI mode, the orchestrator's own token usage (deciding which tool to call) is not captured as an `agent_run` because `to_web()` manages the run internally. These calls are still visible in Logfire under `chat <model>` spans but are not included in `total_cost_usd`.
- **Syslog investigator not tracked**: The `syslog_investigator` runs in background asyncio tasks independent of the request lifecycle. Its LLM calls are captured in Logfire but not in `experiment_log.jsonl`.
- **Estimated costs only**: `total_cost_usd` is computed from the static pricing table, not from OpenRouter billing data. Prices may differ if OpenRouter applies credits, volume discounts, or routing to cheaper providers.
