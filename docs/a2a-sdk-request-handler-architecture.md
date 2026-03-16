# A2A SDK Request Handler Architecture

## Layered Architecture Overview

```
HTTP Request (JSON-RPC)
        │
        ▼
  JSONRPCHandler          ← Transport layer: parses JSON-RPC, formats responses
        │
        ▼
  RequestHandler (ABC)    ← Interface: defines what operations exist
        │
        ▼
  DefaultRequestHandler   ← Implementation: orchestrates agent execution, tasks, queues
        │
        ▼
  AgentExecutor           ← Your code: the actual agent logic
```

## Classes

### `RequestHandler` (Abstract Base Class)

**Source:** `a2a.server.request_handlers.request_handler`

The interface contract. Defines abstract methods for every A2A operation:

- `on_message_send` / `on_message_send_stream` — handle incoming messages
- `on_get_task` / `on_cancel_task` — task lifecycle
- `on_set/get/list/delete_task_push_notification_config` — push notifications
- `on_resubscribe_to_task` — re-attach to a running stream

All methods work with **domain objects** (`Task`, `Message`, `MessageSendParams`) — no JSON-RPC awareness at this layer.

### `DefaultRequestHandler` (extends `RequestHandler`)

**Source:** `a2a.server.request_handlers.default_request_handler`

The SDK's ready-made implementation of `RequestHandler`. Orchestrates:

| Component | Role |
|---|---|
| `AgentExecutor` | Runs your agent logic |
| `TaskStore` | Persists tasks (e.g. `InMemoryTaskStore`) |
| `QueueManager` | Manages event queues for streaming |
| `PushNotificationConfigStore` | Optional push notification config storage |
| `PushNotificationSender` | Optional push notification delivery |
| `RequestContextBuilder` | Builds request context for agent execution |

Handles the complex orchestration internally:
- Creates `asyncio.Task`s for agent execution
- Consumes event queues via `ResultAggregator` + `EventConsumer`
- Manages task lifecycle and state transitions (terminal states: completed, canceled, failed, rejected)
- Cleans up producers and background tasks

### `JSONRPCHandler` (standalone class — NOT a subclass of `RequestHandler`)

**Source:** `a2a.server.request_handlers.jsonrpc_handler`

The transport adapter. Uses **composition** (holds a `RequestHandler` reference):

- Receives typed JSON-RPC request objects (e.g. `SendMessageRequest`)
- Delegates to `self.request_handler.on_message_send(request.params, ...)`
- Wraps results into JSON-RPC response objects (e.g. `SendMessageSuccessResponse`)
- Catches `ServerError` and converts them to `JSONRPCErrorResponse`
- Validates agent capabilities before allowing certain operations (e.g. checks `agent_card.capabilities.streaming`)
- Serves the agent card (including authenticated extended card support)

## How They Connect in Practice

In `A2AStarletteApplication`, the wiring looks like:

```python
request_handler = DefaultRequestHandler(
    agent_executor=your_executor,
    task_store=InMemoryTaskStore(),
)

jsonrpc_handler = JSONRPCHandler(
    agent_card=your_card,
    request_handler=request_handler,  # ← composition
)

# Starlette app routes HTTP → JSONRPCHandler → DefaultRequestHandler → AgentExecutor
```

## Key Design Decisions

- **Composition over inheritance:** `JSONRPCHandler` holds a `RequestHandler` reference rather than extending it. This cleanly separates JSON-RPC protocol concerns from business logic.
- **Swappable implementations:** You can replace `DefaultRequestHandler` with a custom `RequestHandler` implementation without touching the transport layer.
- **Transport agnostic:** The same `RequestHandler` can be used with `JSONRPCHandler`, `RESTHandler`, or `GrpcHandler` — each is a different transport adapter over the same interface.

## References

- [a2a-python SDK](https://github.com/a2aproject/a2a-python)
- [A2A Protocol](https://a2a-protocol.org/)
