# A2A Event Polling Mechanism

How the a2a-sdk handles asynchronous agent execution and event delivery internally.

## The Players

When the A2A server receives a `message/send` request, three concurrent activities run:

### 1. The Agent (Producer)

The `DefaultRequestHandler` spawns the Pydantic AI agent in a **background task** (`agent run` span). The agent calls the LLM, waits for a response, and when done, **enqueues** the result onto an `EventQueue` via `enqueue_event()`.

### 2. The EventConsumer (Internal Poller)

The `DefaultRequestHandler` also starts an `EventConsumer` that sits in a loop calling `EventQueue.dequeue_event()`. Its job is to **wait for the agent to produce a result** and forward it back to the HTTP response.

`dequeue_event()` calls `await self.queue.get()` on an `asyncio.Queue`, but the a2a-sdk wraps this in a **~500ms timeout**. When the timeout expires with no event yet, asyncio **cancels** the awaiting coroutine, raising `CancelledError`. The consumer catches this, checks if the task is still running, and loops back to poll again.

This produces a repeating pattern in telemetry:

```
10:23:05.965  dequeue_event -> CancelledError  (no event yet, agent still working)
10:23:06.572  dequeue_event -> CancelledError  (still waiting...)
10:23:07.082  dequeue_event -> CancelledError  (still waiting...)
...every ~500ms...
10:23:16.183  dequeue_event -> OK              (event arrived!)
```

### 3. The HTTP Response (Stream)

The `_run_event_stream` span represents the server-side HTTP handler waiting for the `EventConsumer` to yield events. Once the consumer dequeues the agent's result, it flows back through the HTTP response to the client.

## Full Timeline

```
Time (s)   What happens
-------------------------------------------------------
0.000      Client sends POST / (JSON-RPC message)
0.001      Server receives request, creates EventQueue
0.002      Spawns background task: agent run (calls LLM)
0.003      EventConsumer starts polling dequeue_event()
0.006      Poll #1  -> timeout -> CancelledError
0.500      Poll #2  -> timeout -> CancelledError
1.000      Poll #3  -> timeout -> CancelledError
 ...       (LLM is still thinking...)
10.18      LLM responds -> agent enqueues result
10.18      Poll #21 -> got event -> OK!
10.18      EventConsumer forwards result to HTTP response
10.18      Cleanup: close queue, cleanup producer, done
```

## Why Polling Instead of a Bare Await?

The timeout-based polling lets the consumer **periodically check** if the producer task has crashed or if the connection was dropped. A bare `await queue.get()` could hang forever if the agent died silently. The 500ms timeout acts as a liveness check.

## Telemetry Noise

Each `CancelledError` gets recorded as an exception span even though it is intentional control flow. This is a known quirk of how the a2a-sdk instruments its internals. The `otel_status_code` stays `UNSET` (not `ERROR`), confirming the SDK does not consider these actual failures.

## Architecture Diagram

```
Client (Agent B)
    |
    | POST / (JSON-RPC)
    v
A2AStarletteApplication
    |
    v
DefaultRequestHandler.on_message_send()
    |
    +--> Background Task: AgentExecutor.execute()
    |        |
    |        | await LLM call
    |        | enqueue_event(result)
    |        v
    |    EventQueue  <----+
    |                     |
    +--> EventConsumer ---+
         (polls dequeue_event every ~500ms)
              |
              | event received
              v
         HTTP Response -> Client
```
