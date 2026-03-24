# A2A: Message & Context Retrieval — Where Does the Responsibility Reside?

## Key Question

If a server agent needs prior messages or tasks from the same `context_id`, who is responsible for providing that context — the client or the server?

## Answer: The Client Owns Context Retrieval

The A2A protocol places the responsibility for providing conversational context squarely on the **client**, not the server. The server has no built-in mechanism to query "give me everything related to this context."

## Evidence from the SDK

### 1. `Message.reference_task_ids` — Client Explicitly Lists Related Tasks

The `Message` type includes a field for the client to reference prior tasks:

```python
# a2a/types.py
class Message:
    reference_task_ids: list[str] | None = None
    """
    A list of other task IDs that this message references for additional context.
    """
```

There is **no** `reference_message_ids` or `reference_context_id` equivalent. The client must know and provide the specific task IDs.

### 2. `SimpleRequestContextBuilder` — Server-Side Task Fetching (Opt-In, Tasks Only)

The SDK provides `SimpleRequestContextBuilder` which can fetch referenced tasks on the server side:

```python
# a2a/server/agent_execution/simple_request_context_builder.py
class SimpleRequestContextBuilder(RequestContextBuilder):
    def __init__(self, should_populate_referred_tasks: bool = False, ...):
        ...

    async def build(self, params, task_id, context_id, task, context):
        if (self._should_populate_referred_tasks
            and params.message.reference_task_ids):
            tasks = await asyncio.gather(
                *[self._task_store.get(tid) for tid in params.message.reference_task_ids]
            )
            related_tasks = [x for x in tasks if x is not None]

        return RequestContext(..., related_tasks=related_tasks)
```

Key observations:
- **Off by default** (`should_populate_referred_tasks=False`)
- Only fetches **Tasks**, not standalone Messages
- Looks up tasks **by ID** (as provided by the client), not by `context_id`

### 3. Messages Are Not First-Class Stored Objects

Messages live **inside** Tasks as `task.history: list[Message]`. There is no `MessageStore` in the SDK.

```
TaskStore
  └── save/get/delete by task_id
        └── Task
              └── history: list[Message]   <-- messages live here
```

If the server responds with just a `Message` (without creating a Task), that message is **ephemeral** — it is not persisted by any store.

### 4. `TaskStore` Has No Context-Based Queries

The `TaskStore` ABC defines only three methods:

```python
class TaskStore(ABC):
    async def save(self, task, context) -> None
    async def get(self, task_id, context) -> Task | None    # by task_id only
    async def delete(self, task_id, context) -> None         # by task_id only
```

No `get_by_context_id()`, no `list_tasks()`, no `search()`. The `DatabaseTaskStore` does store `context_id` as an **indexed column**, but exposes no method to query by it.

## Design Philosophy

| Concern | Responsibility |
|---|---|
| Tracking which tasks belong to a conversation | **Client** |
| Providing related task IDs in messages | **Client** (`reference_task_ids`) |
| Storing tasks (with their message history) | **Server** (`TaskStore`) |
| Looking up tasks by ID | **Server** (`TaskStore.get`) |
| Looking up tasks by context_id | **Neither** (not in protocol/SDK) |
| Storing standalone messages | **Neither** (messages are ephemeral unless inside a Task) |

## Options If the Server Needs Context Awareness

If a server agent needs to be aware of prior messages within the same context:

1. **Client provides `reference_task_ids`** — The intended A2A approach. The client tracks task IDs per context and sends them along with each new message. The server can then fetch those tasks (via `SimpleRequestContextBuilder` with `should_populate_referred_tasks=True`).

2. **Custom `RequestContextBuilder`** — Subclass `RequestContextBuilder` and add a query that fetches all tasks by `context_id` from the store. The `DatabaseTaskStore` already has `context_id` indexed, so you'd only need to add the query method (e.g., subclass `DatabaseTaskStore` with a `get_by_context_id()` method). This goes beyond what the protocol envisions.

## Implications for Our Project

- The client agent should maintain a mapping of `context_id -> list[task_id]` and include relevant `reference_task_ids` when sending follow-up messages.
- Relying on the server to reconstruct conversation history from `context_id` alone is not supported out-of-the-box and would require custom extensions to both the `TaskStore` and `RequestContextBuilder`.
