# A2A: Client-Side State Management for Multi-Server Conversations

## The Problem

When a client agent talks to multiple server agents during a single user conversation, it accumulates:

- Multiple `context_id`s (one per server, server-generated)
- Multiple `task_id`s (potentially several per server)
- Message history and artifacts from each task

If the client doesn't store this content locally, it would have to call `get_task()` on every server for every prior task just to figure out what's relevant before sending a new message. This is wasteful and impractical.

## The Solution: Client Keeps Conversation State Locally

The client should keep the responses it receives during the conversation in local memory (or a local database) — not just the task IDs, but the actual content (messages, artifacts). The A2A `get_task()` exists as a **fallback/refresh** mechanism (e.g., polling a long-running task, or resuming a session after a client restart), not as the primary way to access conversation content.

### Conceptual client session state

```
Client Session State (for one user conversation)
├── Server A (url: ...)
│   ├── context_id: "ctx-abc"
│   ├── task t1: { messages: [...], artifacts: [...], status: completed }
│   └── task t2: { messages: [...], artifacts: [...], status: completed }
├── Server B (url: ...)
│   ├── context_id: "ctx-xyz"
│   └── task t3: { messages: [...], artifacts: [...], status: working }
├── Server C (url: ...)
│   └── ...
```

The client LLM reasons over this local state to decide what context to include in the next request — no round-trips to servers needed.

## How Context Gets Passed (Recap)

When the client sends a new message, it has two mechanisms depending on the situation:

| Scenario | Mechanism | Client needs to re-fetch? |
|---|---|---|
| Referencing tasks on the **same server** | `reference_task_ids` — server fetches them internally from its `TaskStore` | No |
| Bridging context from **Server A to Server B** | Extract content from local state, include as message text/parts | No (already in local memory) |
| Refreshing stale task state (e.g., long-running task) | `get_task(TaskQueryParams(id=...))` on the relevant server | Yes (intentional refresh) |
| Resuming after client restart | `get_task()` to reload task state from servers | Yes (state was lost) |

## What the SDK Provides (and Doesn't)

The A2A SDK's `ClientTaskManager` keeps only the **current task** in memory — it does not maintain a history of all tasks across servers. Anything beyond single-task tracking is the client developer's responsibility.

```python
# a2a/client/client_task_manager.py
class ClientTaskManager:
    _current_task: Task | None  # only tracks ONE task at a time
```

The protocol does **not** prescribe how the client stores multi-server state. That's an implementation detail left to whoever builds the client agent.

## Implications for Client Design

1. **During a session**: The client should cache every task response (messages, artifacts, status) it receives, organized by server and task ID.
2. **For context decisions**: The client's LLM uses the cached local state to decide which prior results are relevant to a new goal — this is where the intelligence lives.
3. **For same-server context**: Use `reference_task_ids` to let the server fetch related tasks efficiently.
4. **For cross-server context**: Extract relevant content from local cache and include it as message parts in the new request.
5. **For persistence across restarts**: If the client needs to survive restarts, it must persist this state (e.g., to a database) and use `get_task()` to refresh on resume.

## Related Docs

- [a2a-client-task-retrieval.md](a2a-client-task-retrieval.md) — How `tasks/get` works and the cross-server limitation
- [a2a-message-context-responsibility.md](a2a-message-context-responsibility.md) — Why context retrieval is the client's responsibility
- [a2a-task-and-context-id-lifecycle.md](a2a-task-and-context-id-lifecycle.md) — How task IDs and context IDs are generated and used
