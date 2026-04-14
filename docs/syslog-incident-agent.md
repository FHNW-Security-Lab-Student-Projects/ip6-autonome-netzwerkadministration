# Syslog Incident Agent

Added: 2026-04-14

## What it does

The Syslog Incident Agent (`syslog_agent.py`, port 8003) does two things:

1. **Event-driven**: watches Loki every 30 s for important Nokia SR Linux syslog events. When one is detected, it automatically opens an incident and starts a background LLM investigation. The LLM queries Loki for surrounding log context and runs show commands to find the root cause.

2. **On-demand**: the user can talk to it directly via the client agent for ad-hoc log queries — no incident needed. Any natural language question that isn't a structured incident command goes straight to the LLM, which uses the same tools.

---

## Files changed

### New file: `syslog_agent.py`

A full A2A server agent following the same patterns as the existing agents.

**Key constants**
```python
LOKI_URL = 'http://172.20.20.101:3100'
LOKI_QUERY = '{vendor="nokia_srlinux", severity=~"error|critical|alert|emergency"}'
LOKI_POLL_INTERVAL = 30  # seconds
```
Change `LOKI_QUERY` to adjust which events trigger incidents (e.g. add/remove severities, filter by `host` or `app` label).

**`Incident` dataclass**

Each detected event becomes an `Incident` object stored in the module-level `_incidents` dict:

| Field | Purpose |
|---|---|
| `incident_id` | `uuid4().hex` — used to look up the incident |
| `device` | Syslog `host` label (e.g. `clab-testlab-router1`) |
| `triggering_event` | Raw log line from Loki |
| `triggering_timestamp_ns` | Unix nanosecond timestamp of the event from Loki — used as anchor for `query_loki` calls |
| `status` | `investigating` → `waiting` → `resolved` |
| `investigation_log` | List of `(step_name, text)` tuples recording what was done |
| `message_history` | Pydantic AI native message list — the full LLM conversation so far |
| `summary` | Latest LLM output, shown in the incident list |
| `bg_task` | `asyncio.Task` handle for the background investigation |

**Module-level state**

```python
_incidents: dict[str, Incident] = {}
_last_checked_ns: int = 0       # tracks last Loki poll time for incremental fetching
_http_client: httpx.AsyncClient | None = None  # set by lifespan, used by query_loki tool
```

**Loki polling (`_loki_poll_loop` / `_poll_loki`)**

Runs as a background `asyncio.Task` started in the lifespan (same pattern as topology agent's refresh loop). Polls `GET /loki/api/v1/query_range` every 30 s, tracking `_last_checked_ns` so only new events are fetched. On the first poll it looks back 30 s to catch events that arrived just before startup.

**Incident deduplication (`_maybe_open_incident`)**

Before opening a new incident, checks whether an existing incident for the same device is already in `investigating` status. If so, the new event is silently dropped — this prevents flooding when a device logs a burst of errors.

**`query_loki` tool (`@syslog_investigator.tool_plain`)**

A Pydantic AI tool registered directly on `syslog_investigator`. The LLM calls this to fetch syslog lines from Loki itself, rather than receiving a pre-fetched bulk dump. Parameters:

| Parameter | Default | Purpose |
|---|---|---|
| `device` | required | Short name (`router1`) or `"all"`. Tool prepends `clab-testlab-` automatically. |
| `time_anchor` | required | ISO 8601 string (e.g. `"2026-04-14T14:30:00Z"`). Center of the time window. |
| `minutes_before` | `5` | How far before the anchor to look |
| `minutes_after` | `2` | How far after the anchor to look |
| `severities` | all except debug | Comma-separated list of syslog severity levels to include |
| `text_filter` | `''` | Optional substring filter on log content |
| `limit` | `100` (max 500) | Max log lines returned |

The tool builds a LogQL stream selector from these parameters, calls `GET /loki/api/v1/query_range`, and returns formatted log lines: `[HH:MM:SS] [host] [severity] [app] message`.

The LLM uses this iteratively — first querying the triggering device, then deciding which neighboring devices to check based on what it finds.

**Background troubleshooting (`_run_troubleshooting`)**

Launched as `asyncio.create_task(...)` when an incident is opened. The prompt explicitly tells the LLM the triggering event timestamp and instructs it to start with `query_loki`:

```
Triggering event timestamp: 2026-04-14T14:30:05Z
Triggering syslog event: <raw log line>

Start by calling query_loki with device='router1' and time_anchor='2026-04-14T14:30:05Z'
to see surrounding log context, then check neighboring devices and run show commands
to confirm the issue and identify the root cause.
```

The LLM has access to both `query_loki` (log context) and the MCP network tools (`network_execute_show_command`, etc.) and interleaves them freely. After it finishes:
- Sets `inc.status = waiting`
- Stores the full Pydantic AI `message_history` in the incident — enables seamless user continuation later

**`SyslogAgentExecutor` (A2A executor)**

Handles user requests via string-based intent dispatch. Four paths:

| User input | Path |
|---|---|
| `list` / `incidents` / `active` | Return formatted incident table — no LLM |
| `detail` / `info` + hex ID fragment | Return full incident detail — no LLM |
| `continue` / `join` + hex ID fragment | Load `inc.message_history`, run LLM continuation |
| Anything else | Route directly to `syslog_investigator.run()` for ad-hoc queries |

The ad-hoc path (fallback) uses `self._context_history` (from `BaseAgentExecutor`) keyed on `context_id`, so multi-turn ad-hoc sessions maintain conversation history — same pattern as `NetworkAgentExecutor`.

If the user tries to continue an incident while the background task is still running (`not inc.bg_task.done()`), it returns the partial summary without touching `message_history` — race condition guard.

**Lifespan**

```python
async with syslog_investigator:          # starts mcp_server.py subprocess
    async with httpx.AsyncClient(timeout=30) as http_client:
        _http_client = http_client       # exposed at module level for query_loki tool
        poll_task = asyncio.create_task(_loki_poll_loop(http_client))
        yield
        poll_task.cancel()
        _http_client = None
```

The `httpx.AsyncClient` (timeout: 30 s) is shared between the Loki poller and `query_loki` tool calls. It is exposed as the module-level `_http_client` so the tool function can access it. The MCP subprocess lifecycle is tied to the A2A server — same as `network_A2A_server_agent.py`.

---

### Modified file: `client_agent.py`

Six targeted changes, all following existing patterns:

1. **`OrchestratorDeps`** — added `syslog_server: ServerState` field
2. **`_call_syslog_agent()`** — new tool function, identical structure to `_call_network_agent` / `_call_topology_agent`
3. **`build_orchestrator()`** — added `syslog_card` parameter; syslog agent description and skills added to orchestrator instructions; `agent.tool(_call_syslog_agent)` registered
4. **`main()`** — connects to `http://localhost:8003`, fetches agent card
5. **`main()`** — instantiates `syslog_server = ServerState(...)`, passes it to `OrchestratorDeps` and `build_orchestrator`
6. **`/state`, `/mode`, post-turn loop** — `syslog_server` added alongside the other servers

---

## How the LLM investigates

The investigation strategy the agent is instructed to follow:

```
1. query_loki(device="router1", time_anchor="T")     ← surrounding logs on triggering device
       ↓ finds: interface flap 30s before the error
2. query_loki(device="switch1", time_anchor="T")     ← check neighboring device
       ↓ finds: port-down event on switch1 at T-31s
3. network_execute_show_command("router1", "show interface")   ← current live state
       ↓ confirms: interface still down
4. Summary: root cause is switch1 port failure, not a BGP misconfiguration
```

The LLM drives the investigation — each tool call is informed by what the previous one returned, the same way a human engineer would work.

---

## How the message history flows

This is the core mechanism that makes "continue troubleshooting" work:

```
1. Background task:
   syslog_investigator.run(initial_prompt, message_history=[])
   → result.all_messages() stored as inc.message_history
   (contains: prompt + all query_loki calls + show commands + LLM responses)

2. User continues:
   syslog_investigator.run(follow_up, message_history=inc.message_history)
   → LLM sees the entire background conversation as context
   → result.all_messages() replaces inc.message_history for future continuations
```

Each call to `result.all_messages()` returns the complete conversation so far. Passing it as `message_history=` on the next call makes the LLM continue as if it's one uninterrupted conversation — even though the first half happened in a background task hours earlier.

---

## Running

```bash
# Terminal 3 (or alongside the others)
uv run uvicorn syslog_agent:app --port 8003

# Client agent now connects to all three
uv run python client_agent.py
```

## Example interactions

```
# Automatic incident (triggered by syslog event):
You: are there any active incidents?
Orchestrator: [calls call_syslog_agent] → incident table with status

You: show me details for incident abc12345
Orchestrator: [calls call_syslog_agent] → triggering event + full investigation log

You: continue abc12345 — also check switch1 logs
Orchestrator: [calls call_syslog_agent] → LLM resumes with full context, calls query_loki on switch1

# Ad-hoc query (no incident needed):
You: what happened on router1 around 14:30 today?
Orchestrator: [calls call_syslog_agent] → LLM calls query_loki directly and summarises findings
```

---

## Things to adjust later

- **Event filter**: Change `LOKI_QUERY` to tune which events trigger incidents. Could add `app` label filter to focus on specific processes (e.g. only BGP events).
- **Poll interval**: `LOKI_POLL_INTERVAL = 30` seconds. Reduce for faster response, increase to reduce Loki load.
- **Deduplication strategy**: Currently drops subsequent events for a device with an active investigation. Could instead queue them or append to the existing incident's log.
- **Persistence**: `_incidents` is in-memory and dies on restart. For production, replace with a database or file-backed store.
- **LLM model**: `syslog_investigator` uses `z-ai/glm-5` via OpenRouter (same as the other agents). Swap the model string to use a different one for incident investigation.
- **Mark resolved**: Currently no way to mark incidents resolved. Could add a `resolve <id>` intent to `SyslogAgentExecutor`.
- **`query_loki` time window**: defaults are `minutes_before=5, minutes_after=2`. These can be widened for slow-developing issues or narrowed for burst-event correlation.
