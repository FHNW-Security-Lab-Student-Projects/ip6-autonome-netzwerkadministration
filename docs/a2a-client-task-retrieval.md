# A2A: Client-Side Task Retrieval via `tasks/get`

## The Question

Can a client retrieve the full details (status, history, artifacts) of a task it previously interacted with?

## Answer: Yes — via `tasks/get`

The A2A protocol defines a `tasks/get` JSON-RPC method, and the SDK exposes it on the client transport as `get_task()`.

### `TaskQueryParams`

```python
# a2a/types.py
class TaskQueryParams(A2ABaseModel):
    id: str                        # The task ID to look up
    history_length: int | None     # Optional: limit how many messages to return
    metadata: dict[str, Any] | None
```

### Client usage

```python
from a2a.types import TaskQueryParams

# Retrieve a task's full state from the server
task = await client.get_task(TaskQueryParams(id="some-task-id"))

# Or limit history to the last 5 messages
task = await client.get_task(TaskQueryParams(id="some-task-id", history_length=5))

# Access the task's conversation history
for message in task.history:
    ...
```

All three transport implementations (JSON-RPC, REST, gRPC) implement this method:
- `JsonRpcTransport.get_task()` — calls `tasks/get` JSON-RPC method
- `RestTransport.get_task()` — calls the REST endpoint
- `GrpcTransport.get_task()` — calls the gRPC equivalent

## How This Fits into Context Passing

### Within a single server

The `reference_task_ids` mechanism relies on this capability server-side. When a client sends:

```python
Message(
    parts=[...],
    reference_task_ids=["t1", "t2"],  # "these tasks are relevant"
)
```

The server's `SimpleRequestContextBuilder` (if configured with `should_populate_referred_tasks=True`) calls `task_store.get(t1)` and `task_store.get(t2)` internally and passes the full task objects (with their histories) to the `AgentExecutor` as `related_tasks`.

So the flow is:

```
Client                                  Server A
  │                                        │
  │  send_message(task_id=None)           │
  │ ─────────────────────────────────────► │  creates task t1
  │  ◄── Task(id=t1, context_id=ctx1)     │
  │                                        │
  │  send_message(task_id=None)           │
  │ ─────────────────────────────────────► │  creates task t2
  │  ◄── Task(id=t2, context_id=ctx1)     │
  │                                        │
  │  send_message(                         │
  │    reference_task_ids=[t1, t2])        │
  │ ─────────────────────────────────────► │  fetches t1, t2 from TaskStore
  │                                        │  passes them as related_tasks
  │                                        │  to AgentExecutor
```

The client can also call `get_task()` itself at any time to check the latest state of a task — useful for polling long-running background tasks or refreshing task history before deciding what context to include.

### Across multiple servers — the limitation

`reference_task_ids` only works **within the server that owns those tasks**. Each server has its own `TaskStore`, and task IDs are meaningless to other servers.

```
Client
  ├── Server A  →  TaskStore: {t1, t2}
  ├── Server B  →  TaskStore: {t3}
  └── Server C  →  TaskStore: {t4}
```

If the client needs to give Server B context from Server A's task `t1`:

1. **Cannot** send `reference_task_ids=[t1]` to Server B — Server B's `TaskStore` doesn't have `t1`
2. **Must** extract the relevant content from `t1` (via `get_task()` on Server A, or from the response it already has) and include it as **message content** in a new message to Server B

```
Client                     Server A              Server B
  │                           │                      │
  │  get_task(id=t1)         │                      │
  │ ────────────────────────► │                      │
  │  ◄── Task(history=[...]) │                      │
  │                           │                      │
  │  send_message(                                   │
  │    parts=[TextPart(       │                      │
  │      text="Context from Server A: ...")])        │
  │ ────────────────────────────────────────────────► │
  │                                                   │
```

### Summary

| Scenario | Mechanism | Works? |
|---|---|---|
| Client retrieves task from the same server | `get_task(TaskQueryParams(id=...))` | Yes |
| Server retrieves related tasks internally | `reference_task_ids` + `SimpleRequestContextBuilder` | Yes (same server only) |
| Client passes context from Server A to Server B | Extract content, re-send as message parts | Yes (manual, client's job) |
| Client passes `reference_task_ids` from Server A to Server B | `reference_task_ids=[t1]` on Server B | No — Server B doesn't have `t1` |

## SDK Source References

- `TaskQueryParams`: `a2a-python/src/a2a/types.py` (line 947)
- `GetTaskRequest` (JSON-RPC): `a2a-python/src/a2a/types.py` (line 1199)
- `ClientTransport.get_task()` (abstract): `a2a-python/src/a2a/client/transports/base.py` (line 63)
- `JsonRpcTransport.get_task()`: `a2a-python/src/a2a/client/transports/jsonrpc.py` (line 224)
- `SimpleRequestContextBuilder` with `reference_task_ids` lookup: `a2a-python/src/a2a/server/agent_execution/simple_request_context_builder.py` (line 61-75)
