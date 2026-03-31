# client_agent.py — State Tracking Refactor

## What changed

Replaced two module-level scalar variables (`task_id`, `context_id`) with a structured data model that tracks every context ID and task ID returned by the server, enforces task ID scoping within contexts, and is ready for future multi-server scenarios.

---

## New data model

Three dataclasses were added (placed after the imports, before `extract_text`):

### `TaskRecord`
Stores the result of a single completed or in-flight task.

| Field | Type | Description |
|---|---|---|
| `task_id` | `str` | Server-assigned task ID |
| `context_id` | `str` | Denormalised copy of parent context ID |
| `response_text` | `str \| None` | Last text extracted from this task |
| `final_state` | `str` | Last known state, e.g. `"completed"`, `"input_required"` |
| `created_at` | `datetime` | UTC timestamp of record creation |

### `ContextState`
Groups all tasks belonging to one A2A context (one "session" on a server).

| Field / Method | Description |
|---|---|
| `context_id` | The A2A context ID |
| `tasks: dict[task_id, TaskRecord]` | All tasks seen in this context, in insertion order |
| `active_task_id` | The task ID the server is currently waiting on (`None` when no task is in-flight) |
| `record_task(record)` | Upserts a `TaskRecord` into `tasks` |
| `all_task_ids()` | Returns task IDs in chronological order |
| `completed_responses()` | Returns all non-None `response_text` values, oldest first |

**Scoping rule:** `TaskRecord` objects in one `ContextState` must never be used as `reference_task_ids` in a different context or on a different server. The data structure enforces this — there is no way to reach a `task_id` across `ContextState` boundaries without explicitly extracting it.

### `ServerState`
Holds everything the client knows about one server agent.

| Field / Method | Description |
|---|---|
| `url` | Server URL |
| `agent_name` | Name from the agent card |
| `client` | The `a2a.client.Client` instance |
| `contexts: dict[context_id, ContextState]` | All contexts seen on this server |
| `active_context_id` | The context ID used for the next outgoing message |
| `active_context()` | Returns the currently active `ContextState` or `None` |
| `active_task_id()` | Returns the active task ID or `None` |
| `apply_response(...)` | **Core update method** — called after every server response; creates/updates context and task records; resets `active_task_id` to `None` on task completion, keeps it set when server requests more input |
| `build_message(user_text)` | Constructs an A2A `Message` scoped to the active context and task |
| `summarise_for_injection()` | Cross-server context hook — see below |
| `debug_summary()` | Returns a formatted string of the full context/task tree |

---

## `send_and_process` — signature change

**Before:**
```python
async def send_and_process(client, message, logger) -> tuple[str | None, str | None, str | None, bool]:
    # returns (response_text, task_id, context_id, needs_input)
```

**After:**
```python
async def send_and_process(server: ServerState, message: Message, logger) -> str | None:
    # updates server state in-place, returns only response_text
```

The function now calls `server.apply_response(...)` at the end of the event loop instead of returning a 4-tuple. All event-processing logic and logfire instrumentation is unchanged. `final_state` is now captured from `task.status.state`.

---

## `main()` — interaction loop changes

| Before | After |
|---|---|
| `task_id = None` / `context_id = None` scalars | `server = ServerState(url=..., agent_name=..., client=...)` |
| Manual `Message(task_id=task_id, context_id=context_id, ...)` | `server.build_message(user_text)` |
| `response_text, task_id, context_id, needs_input = await send_and_process(client, ...)` | `response_text = await send_and_process(server, ...)` |
| `task_id = None` reset after task completion | Handled internally by `server.apply_response()` |
| — | `/state` command: prints `server.debug_summary()` |
| — | `logger.debug(...)` tracking line after each successful response |

---

## Cross-server context injection hook

`ServerState.summarise_for_injection()` flattens all known response texts from a server into a single string, e.g.:

```
[JokeAgent / ctx a3f2b1c0 / task 9d8e7f6a]: Why don't scientists trust atoms? Because they make up everything.
```

This string is meant to be prepended to a user message when sending to a *different* server agent. This satisfies the A2A constraint that task IDs cannot be referenced across servers — context is shared via the message prompt, not via `reference_task_ids`.

Usage pattern (not yet wired, hook is ready):
```python
context_from_server_a = servers['http://localhost:8000'].summarise_for_injection()
if context_from_server_a:
    user_text = f'Context from prior agent:\n{context_from_server_a}\n\nUser request: {user_text}'
message = servers['http://localhost:8001'].build_message(user_text)
```

---

## Multi-server expansion path

No interaction loop changes are needed. Add more `ServerState` instances:

```python
servers: dict[str, ServerState] = {
    'http://localhost:8000': ServerState(...),
    'http://localhost:8001': ServerState(...),
}
active_server = servers['http://localhost:8000']
```

Routing between servers is a future concern; the data model already supports it.

---

## `/state` debug command

Typing `/state` in the REPL prints the full context/task tree for the current server, e.g.:

```
ServerState(JokeAgent @ http://localhost:8000)
  context a3f2b1c0 [ACTIVE]  (3 task(s))
    task 9d8e7f6a  state=completed  created=14:02:11
    task 1b2c3d4e  state=completed  created=14:03:45
    task 5f6a7b8c [active task]  state=input_required  created=14:05:02
```
