# A2A Server: Full Request Chain

The complete path a client request takes through the A2A server, from HTTP to your agent code and back. This combines the architecture overview, object instantiation, and runtime flow into one reference.

## Object Instantiation (Startup)

When your server starts (`A2A_server_agent.py`), objects are created in this order:

```
1. JokeAgentExecutor()                    ← your AgentExecutor implementation
2. InMemoryTaskStore()                    ← task persistence
3. DefaultRequestHandler(                 ← orchestration layer
       agent_executor=...,
       task_store=...,
   )
4. A2AStarletteApplication(              ← HTTP app builder
       agent_card=...,
       http_handler=request_handler,
   )
   └── super().__init__() calls JSONRPCApplication.__init__()
       └── self.handler = JSONRPCHandler(     ← ⚡ created internally, never by you
               agent_card=agent_card,
               request_handler=http_handler,  ← your DefaultRequestHandler
           )
5. server.build()                         ← creates the Starlette app with routes
   └── Starlette(routes=[
           POST /          → _handle_requests      (JSON-RPC endpoint)
           GET  /.well-known/agent.json → _handle_get_agent_card
       ])
6. logfire.instrument_starlette(app)      ← wraps the Starlette app with tracing
```

**Key point:** `JSONRPCHandler` is never instantiated by you. It's an internal detail created inside `JSONRPCApplication.__init__()` (the parent class of `A2AStarletteApplication`). It's stored as `self.handler` on the application instance.

## The Object Graph at Runtime

```
A2AStarletteApplication
├── .agent_card              → AgentCard (capabilities, skills, URL)
├── .handler                 → JSONRPCHandler          (created internally)
│   ├── .agent_card          → same AgentCard
│   └── .request_handler     → DefaultRequestHandler   (your http_handler)
│       ├── .agent_executor  → JokeAgentExecutor       (your code)
│       ├── .task_store      → InMemoryTaskStore
│       └── .queue_manager   → InMemoryQueueManager    (created internally)
└── ._context_builder        → DefaultCallContextBuilder (created internally)
```

## Full Request Chain (Streaming Path)

This is the path for a `message/stream` request (the most common in our setup since `capabilities.streaming=True`).

### 1. Uvicorn receives the HTTP request

```
Client POST / with JSON-RPC body:
{
  "jsonrpc": "2.0",
  "method": "message/stream",
  "id": "abc-123",
  "params": { "message": { "role": "user", "parts": [...] } }
}
```

### 2. Starlette routes to `_handle_requests`

The `POST /` route is wired to `JSONRPCApplication._handle_requests()` (defined in `jsonrpc_app.py`). This is the HTTP entry point for all JSON-RPC calls.

**What happens here:**
- Reads and parses the JSON body
- Validates the base JSON-RPC structure (`JSONRPCRequest`)
- Looks up the method name (`"message/stream"`) in `METHOD_TO_MODEL` to find the specific request type (`SendStreamingMessageRequest`)
- Validates the full request against that model
- Builds a `ServerCallContext` via `DefaultCallContextBuilder` (extracts user auth, headers, requested extensions)
- Routes to `_process_streaming_request()` (for streaming methods) or `_process_non_streaming_request()` (for `message/send`, `tasks/get`, etc.)

**Logfire span:** `a2a.server.apps.jsonrpc.jsonrpc_app.JSONRPCApplication._handle_requests`

### 3. `_process_streaming_request` calls `JSONRPCHandler`

```python
handler_result = self.handler.on_message_send_stream(request_obj, context)
```

This calls `JSONRPCHandler.on_message_send_stream()` which is an **async generator**. It doesn't execute immediately — it returns a generator object that will be consumed by the SSE response.

**Logfire span:** `a2a.server.request_handlers.jsonrpc_handler.JSONRPCHandler.on_message_send_stream`

### 4. `JSONRPCHandler.on_message_send_stream` delegates to `DefaultRequestHandler`

```python
# Inside JSONRPCHandler.on_message_send_stream:
async for event in self.request_handler.on_message_send_stream(request.params, context):
    yield prepare_response_object(request.id, event, ...)
```

Before delegating, the `@validate` decorator checks that `agent_card.capabilities.streaming` is enabled. If not, it raises a `ServerError`.

The handler:
- Unwraps `request.params` (strips the JSON-RPC envelope, passes `MessageSendParams`)
- Delegates to `DefaultRequestHandler.on_message_send_stream()`
- Wraps each yielded event into a `SendStreamingMessageResponse` (adds back the JSON-RPC envelope)
- Catches `ServerError` and converts to `JSONRPCErrorResponse`

### 5. `DefaultRequestHandler.on_message_send_stream` orchestrates execution

This is where the heavy lifting happens. The handler:

1. **Calls `_setup_message_execution()`** which:
   - Creates or retrieves a `TaskManager` (wraps the `TaskStore`)
   - Creates an `EventQueue` (asyncio queue for producer → consumer communication)
   - Creates a `ResultAggregator` (processes events, updates task state)
   - Registers the queue with the `QueueManager` (keyed by task ID)
   - Spawns the `AgentExecutor.execute()` as a **background `asyncio.Task`** (the "producer")

2. **Calls `_run_event_stream()`** which:
   - Creates an `EventConsumer` that reads from the `EventQueue`
   - Enters a polling loop: calls `dequeue_event()` with a ~500ms timeout
   - On timeout → `CancelledError` (expected, loops back to poll again)
   - On event → yields the event back up the chain
   - On final event (terminal `TaskStatusUpdateEvent`, `Message`, or completed `Task`) → stops

**Logfire spans:**
- `a2a.server.request_handlers.default_request_handler.DefaultRequestHandler.on_message_send_stream`
- `a2a.server.request_handlers.default_request_handler.DefaultRequestHandler._setup_message_execution`
- `a2a.server.request_handlers.default_request_handler.DefaultRequestHandler._run_event_stream`
- `a2a.server.events.event_queue.EventQueue.dequeue_event` (repeats every ~500ms until event arrives)

### 6. `AgentExecutor.execute()` runs your agent logic

Your `JokeAgentExecutor.execute()` runs concurrently as a background task:

```python
result = await agent.run(user_text)  # calls the LLM via OpenRouter
await event_queue.enqueue_event(new_agent_text_message(response_text))
```

The `enqueue_event()` puts the event on the `asyncio.Queue`, which unblocks the `EventConsumer`'s `dequeue_event()` call.

**Logfire spans:**
- `JokeAgentExecutor.execute` (your manual span)
- `agent run` (Pydantic AI auto-instrumented)
- OpenAI/OpenRouter HTTP call (auto-instrumented by `logfire.instrument_openai()`)

### 7. Event flows back up the chain (and the Message vs Task decision)

The event type your `AgentExecutor` enqueues determines what the client receives. This is the **agent-driven decision point** — the SDK doesn't decide for you:

| What you enqueue | What the client gets | `kind` discriminator |
|---|---|---|
| `Message` (via `new_agent_text_message()`) | A direct reply — no task lifecycle | `"message"` |
| `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent` / `Task` | A stateful Task with lifecycle tracking | `"task"` |

On the **streaming path**, each event is forwarded individually as an SSE event, so the client sees the `kind` field on each event as it arrives. A `Message` event also acts as a **final event** — the stream closes after it.

In our `A2A_server_agent.py`, we call `new_agent_text_message()` which creates a `Message`, so the client always receives a message response (no task created):

```python
# In JokeAgentExecutor.execute():
await event_queue.enqueue_event(new_agent_text_message(response_text))  # → Message response
```

If we instead enqueued task events, the client would get a Task:

```python
# Alternative: would produce a Task response
await event_queue.enqueue_event(TaskStatusUpdateEvent(state=TaskState.working, ...))
await event_queue.enqueue_event(TaskArtifactUpdateEvent(artifact=...))
await event_queue.enqueue_event(TaskStatusUpdateEvent(state=TaskState.completed, final=True))
```

The event then flows back up unchanged through the layers:

```
EventQueue.enqueue_event(Message)
    ↓
EventConsumer.dequeue_event() → yields Message
    ↓
DefaultRequestHandler._run_event_stream() → yields Message
    ↓
JSONRPCHandler.on_message_send_stream() → yields SendStreamingMessageResponse
    ↓
JSONRPCApplication._process_streaming_request() → wraps in EventSourceResponse
    ↓
Starlette sends SSE event to client:
    data: {"jsonrpc":"2.0","id":"abc-123","result":{"kind":"message","parts":[...]}}
```

### 8. Cleanup

After the stream completes:
- `_cleanup_producer()` cancels the background `asyncio.Task` if still running
- `QueueManager.close(task_id)` removes the event queue
- The SSE connection closes

## Full Chain Summary (Streaming)

```
Client HTTP POST /
    │
    ▼
Starlette (uvicorn)
    │
    ▼
JSONRPCApplication._handle_requests()          ← parse JSON-RPC, validate, route
    │
    ▼
JSONRPCApplication._process_streaming_request() ← streaming dispatch
    │
    ▼
JSONRPCHandler.on_message_send_stream()         ← capability check, delegate, wrap responses
    │
    ▼
DefaultRequestHandler.on_message_send_stream()  ← orchestrate execution
    │
    ├──→ _setup_message_execution()             ← create queue, task manager, spawn producer
    │       │
    │       └──→ asyncio.Task: AgentExecutor.execute()   ← YOUR CODE (runs in background)
    │               │
    │               ├── agent.run(user_text)              ← Pydantic AI → LLM call
    │               └── event_queue.enqueue_event(msg)    ← push result to queue
    │
    └──→ _run_event_stream()                    ← poll EventQueue via EventConsumer
            │
            │  dequeue_event() every ~500ms
            │  (CancelledError until event arrives)
            │
            ▼
        yield event back up through:
            DefaultRequestHandler → JSONRPCHandler → SSE → Client
```

## Non-Streaming Path (message/send)

For non-streaming (`message/send`), the chain is similar but:
- Routes through `_process_non_streaming_request()` instead
- Calls `JSONRPCHandler.on_message_send()` (not the stream variant)
- Calls `DefaultRequestHandler.on_message_send()` which uses `ResultAggregator.consume_all()` instead of `_run_event_stream()`
- Returns a single `JSONResponse` instead of an SSE stream
- The `blocking` flag (from `params.configuration.blocking`) determines whether the handler awaits task completion or returns early with a partial Task

### Message vs Task decision on the non-streaming path

On this path, `ResultAggregator.consume_all()` is the explicit decision point (see [a2a-message-vs-task-decision.md](a2a-message-vs-task-decision.md) for the full breakdown):

```python
# ResultAggregator.consume_all() — simplified:
async for event in consumer.consume_all():
    if isinstance(event, Message):
        return event                          # → Message response, return immediately
    await self.task_manager.process(event)    # accumulate Task events
return await self.task_manager.get_task()     # → Task response
```

The first `Message` event wins and short-circuits. If only task-related events are enqueued, they're accumulated and the final `Task` object is returned. Either way, the result goes back through `JSONRPCHandler.on_message_send()` which wraps it in a `SendMessageSuccessResponse` — the response's `result` field will have `kind: "message"` or `kind: "task"` accordingly.

## Where Each Class Lives in the SDK

| Class | Module | Role |
|---|---|---|
| `A2AStarletteApplication` | `a2a.server.apps.jsonrpc.starlette_app` | Builds the Starlette app, defines HTTP routes |
| `JSONRPCApplication` | `a2a.server.apps.jsonrpc.jsonrpc_app` | Base class: JSON-RPC parsing, validation, routing, response formatting |
| `JSONRPCHandler` | `a2a.server.request_handlers.jsonrpc_handler` | Maps JSON-RPC methods to RequestHandler calls, wraps responses |
| `DefaultRequestHandler` | `a2a.server.request_handlers.default_request_handler` | Orchestrates agent execution, queues, task lifecycle |
| `RequestHandler` | `a2a.server.request_handlers.request_handler` | ABC interface for all request handlers |
| `AgentExecutor` | `a2a.server.agent_execution` | ABC you implement — your agent logic |
| `EventQueue` | `a2a.server.events.event_queue` | asyncio.Queue wrapper for producer/consumer |
| `EventConsumer` | `a2a.server.events.event_consumer` | Polls the EventQueue with timeout |
| `ResultAggregator` | `a2a.server.tasks.result_aggregator` | Processes events, decides Message vs Task response |
| `TaskManager` | `a2a.server.tasks.task_manager` | Updates task state in the TaskStore |
| `InMemoryTaskStore` | `a2a.server.tasks.in_memory_task_store` | In-memory task persistence |

## Telemetry (What You See in Logfire)

All classes decorated with `@trace_class(kind=SpanKind.SERVER)` get automatic spans for every public method. This is why you see spans like:

```
a2a.server.request_handlers.jsonrpc_handler.JSONRPCHandler.on_message_send_stream
a2a.server.request_handlers.default_request_handler.DefaultRequestHandler.on_message_send_stream
a2a.server.events.event_queue.EventQueue.dequeue_event
a2a.server.events.event_queue.EventQueue.enqueue_event
```

The `@trace_class` decorator (from `a2a.utils.telemetry`) wraps every non-excluded method with an OpenTelemetry span. The span name follows the pattern `module.ClassName.method_name`. This is SDK-internal instrumentation — you don't need to add any tracing for these yourself.

On top of that, `logfire.instrument_starlette(app)` adds spans for the HTTP layer (request/response), and `logfire.instrument_pydantic_ai()` / `logfire.instrument_openai()` add spans for the AI calls.

## References

- [a2a-sdk request handler architecture](a2a-sdk-request-handler-architecture.md) — class roles and design decisions
- [a2a-message-vs-task-decision](a2a-message-vs-task-decision.md) — how Message vs Task response is determined
- [a2a-event-polling](a2a-event-polling.md) — the ~500ms polling mechanism in detail
- [a2a-client-delivery-modes](a2a-client-delivery-modes.md) — streaming vs blocking vs polling from the client side
