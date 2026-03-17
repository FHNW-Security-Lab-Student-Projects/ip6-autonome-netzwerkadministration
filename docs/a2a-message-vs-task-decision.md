# A2A SDK: Message vs Task — Where the Decision Is Made

The A2A spec defines two possible response types for a `message/send` request: a **Message** (simple reply) or a **Task** (stateful, trackable operation). The decision of which one the client receives is **agent-driven** — determined by what events your `AgentExecutor` enqueues.

## The Decision Point

The core logic lives in `ResultAggregator.consume_all()` (`src/a2a/server/tasks/result_aggregator.py`):

```python
async def consume_all(self, consumer: EventConsumer) -> Task | Message | None:
    async for event in consumer.consume_all():
        if isinstance(event, Message):
            self._message = event
            return event          # Message? Return immediately
        await self.task_manager.process(event)
    return await self.task_manager.get_task()  # Otherwise return Task
```

**The rule:**
1. If the agent enqueues a **`Message`** event — the SDK returns that Message directly to the client.
2. If the agent only enqueues **Task-related events** (`Task`, `TaskStatusUpdateEvent`, `TaskArtifactUpdateEvent`) — the SDK accumulates them and returns the final Task.

## Request Flow

```
Client sends message/send request
    |
    v
JSONRPCHandler.on_message_send()
    |
    v
DefaultRequestHandler.on_message_send()
    |
    v
_setup_message_execution() creates:
    - TaskManager (manages task state)
    - EventQueue (buffer for agent events)
    - ResultAggregator (processes events)
    - Producer task (runs agent.execute())
    |
    v
AgentExecutor.execute() runs and enqueues events:
    - Message           --> results in Message response
    - Task              --> results in Task response
    - TaskStatusUpdateEvent --> results in Task response
    - TaskArtifactUpdateEvent --> results in Task response
    |
    v
EventConsumer reads from queue, yields events
    |
    v
ResultAggregator.consume_all() processes:
    IF Message -> return Message immediately
    ELSE      -> accumulate Task-related events, return Task
    |
    v
Response sent back to client
```

## Where Events Originate

Your `AgentExecutor.execute()` implementation pushes events onto the `EventQueue`. The event type you push determines the response type:

```python
# This -> client gets a Message response
await event_queue.enqueue_event(Message(role='agent', parts=[...]))

# This -> client gets a Task response
await event_queue.enqueue_event(TaskStatusUpdateEvent(state=TaskState.completed, ...))
```

The possible event types are defined in `src/a2a/server/events/event_queue.py`:

```python
Event = Message | Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent
```

## Message Is Always a "Final" Event

In the `EventConsumer` (`src/a2a/server/events/event_consumer.py`), a `Message` is always treated as a final event that closes the stream. Only the **first** Message matters — anything enqueued after it won't be consumed.

Other final events include:
- A `TaskStatusUpdateEvent` with `final=True`
- A `Task` with a terminal state (`completed`, `canceled`, `failed`, `rejected`, `unknown`, `input_required`)

## Response Types

**Message** (`types.py`):
- `kind: 'message'` discriminator
- Direct reply from the agent
- Contains `parts` (text, images, etc.)
- Has optional `task_id`

**Task** (`types.py`):
- `kind: 'task'` discriminator
- Stateful operation with lifecycle
- Contains `status` with `state` (completed, running, failed, etc.)
- Has `history` (message conversation history)
- Has `artifacts` (generated outputs)

Both are valid results for `SendMessageSuccessResponse`:

```python
result: Task | Message  # Can be either type
```

## Practical Example

In our `agent_a_server.py`, the `AgentExecutor` enqueues `TaskStatusUpdateEvent` and `TaskArtifactUpdateEvent`, so clients always receive a **Task** response. To get a simpler request/response without task lifecycle, enqueue a `Message` instead.
