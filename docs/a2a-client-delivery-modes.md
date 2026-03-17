# A2A Client Delivery Modes & Configuration

How the A2A client receives responses from the server, and which mode to use when.

## The Three Delivery Modes

The A2A protocol supports three ways for a client to receive results:

| Mode | JSON-RPC Method | `ClientConfig` | How It Works |
|---|---|---|---|
| **Blocking** | `message/send` | `streaming=False, polling=False` | Server holds the HTTP connection open until the task completes, then returns the final `Message` or `Task` in one response. |
| **Streaming (SSE)** | `message/stream` | `streaming=True, polling=False` | Server pushes events (status updates, partial artifacts) over a long-lived SSE connection as they happen. |
| **Polling** | `message/send` + `tasks/get` | `streaming=False, polling=True` | Server returns immediately with a `Task` (status=`submitted`/`working`). Client calls `tasks/get` repeatedly to check for completion. |

Additionally, **push notifications** (webhooks) can be layered on top of any mode via `push_notification_config`.

## How `blocking` and `polling` Relate

In the SDK client (`base_client.py`), the `blocking` field sent in the JSON payload is derived from `polling`:

```python
blocking = not self._config.polling
```

- `polling=False` (default) -> `blocking=True` in the payload
- `polling=True` -> `blocking=False` in the payload

The `blocking` flag tells the server whether to hold the connection open (`message/send` path only). On the streaming path (`message/stream`), the server **ignores** the `blocking` flag entirely.

## What `blocking` Does on the Server Side

In `DefaultRequestHandler.on_message_send` (the non-streaming handler):

```python
blocking = True  # Default to blocking behavior
if params.configuration and params.configuration.blocking is False:
    blocking = False
```

This is passed to `result_aggregator.consume_and_break_on_interrupt(blocking=...)`:

- **`blocking=True`**: The handler awaits until the task reaches a terminal state before returning the HTTP response.
- **`blocking=False`**: The handler returns early with a partial `Task` object (status may be `submitted` or `working`). The agent continues executing in the background.

**Important**: In both cases, the agent runs as an `asyncio.Task`. The event loop is never blocked -- other requests are served concurrently. "Blocking" refers to the HTTP connection lifecycle, not the server's ability to handle other work.

## What Happens on the Streaming Path

When streaming is used (`message/stream`), the server:

1. Creates the agent execution as an `asyncio.Task` (producer)
2. Creates an `EventQueue` where the producer pushes events
3. An `EventConsumer` watches the queue and yields events back to the client over SSE

The `blocking` flag in the payload is **completely ignored** on this path. Events are pushed in real-time regardless.

If the client **disconnects** (accidentally or intentionally), the server catches the `CancelledError`/`GeneratorExit` and:
- Continues consuming and persisting events in the background
- Schedules cleanup of the producer task and queue

## Resubscribe (Streaming Recovery)

If the SSE connection drops while a task is still running, the client can call `resubscribe(task_id)` to reconnect to the event stream.

**Caveat**: Resubscribe only works if the task is **still running**. After the task completes, `_cleanup_producer` calls `queue_manager.close(task_id)`, which removes the queue. A late resubscribe would fail with `TaskNotFoundError`. For this reason, resubscribe is designed for **accidental** disconnects, not intentional "come back later" patterns.

## Recommendations by Use Case

### Short-Lived Tasks (seconds) -- e.g., joke generation, translation

**Recommended: Streaming or Blocking**

- Streaming gives real-time feedback with minimal risk of timeout.
- Blocking is the simplest option -- one request, one response.
- Connection risk is minimal since the task completes quickly.

```python
# Streaming (preferred for UI feedback)
ClientConfig(httpx_client=http_client, streaming=True)

# Blocking (simplest)
ClientConfig(httpx_client=http_client)  # defaults: streaming=False, polling=False
```

### Long-Lived Tasks (minutes+) -- e.g., research, multi-step workflows

**Recommended: Streaming + Push Notifications as fallback**

- Streaming provides real-time progress updates to the user.
- Push notifications (webhooks) ensure the client gets notified even if the SSE connection drops and the task completes before a resubscribe.
- Polling can serve as an additional fallback if webhooks aren't possible.

```python
# Streaming with push notification fallback
from a2a.types import PushNotificationConfig

ClientConfig(
    httpx_client=http_client,
    streaming=True,
    push_notification_configs=[
        PushNotificationConfig(url='https://my-client/webhook', ...)
    ],
)
```

Recovery pattern:
1. Start `message/stream` -> receive SSE events
2. Connection drops -> client still has `task_id` from first event
3. If task still running: `resubscribe(task_id)` to reconnect
4. If task already finished: `tasks/get` to retrieve final result, or rely on the push notification

### Fire-and-Forget Tasks -- e.g., background processing, batch jobs

**Recommended: Polling + Push Notifications**

- Client submits work and disconnects immediately.
- Push notification delivers the result when ready.
- Polling (`tasks/get`) as fallback if webhook fails.

```python
ClientConfig(
    httpx_client=http_client,
    polling=True,
    push_notification_configs=[
        PushNotificationConfig(url='https://my-client/webhook', ...)
    ],
)
```

### Direct Message Responses (no task created)

When the server responds with a `Message` instead of a `Task`, delivery mode doesn't matter much -- the response is immediate and complete. This happens when the agent logic is simple enough that no task tracking is needed.

## Summary Table

| Scenario | Streaming | Polling | Push Notifications | Notes |
|---|---|---|---|---|
| Short task, simple | - | - | - | Blocking default is fine |
| Short task, with UI | Yes | - | - | Real-time feedback |
| Long task, with UI | Yes | - | Yes (fallback) | Streaming + resubscribe + webhook |
| Long task, no UI | - | Yes | Yes | Fire-and-forget |
| Unreliable network | - | Yes | Yes | Avoid long-lived connections |
| Serverless / edge client | - | Yes | Yes | Can't hold connections open |
| Multiple concurrent tasks | - | Yes | Yes | Avoids holding N connections |

## SDK Source References

- Client-side `blocking` derivation: `a2a/client/base_client.py:89`
- Streaming vs non-streaming branch: `a2a/client/base_client.py:109`
- Server `on_message_send` (blocking logic): `a2a/server/request_handlers/default_request_handler.py:289-362`
- Server `on_message_send_stream` (ignores blocking): `a2a/server/request_handlers/default_request_handler.py:364-406`
- Server disconnect handling: `a2a/server/request_handlers/default_request_handler.py:393-399`
- Client `resubscribe`: `a2a/client/base_client.py:239-273`
