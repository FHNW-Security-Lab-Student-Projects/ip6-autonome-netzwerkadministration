# Client Agent — Orchestrator Architecture

## Overview

The client agent is built on two layers that work together:

| Layer | Technology | Responsibility |
|---|---|---|
| Orchestration | Pydantic AI `Agent` | Understands user intent, decides which remote agent to call, maintains conversation memory |
| Transport | A2A protocol (`a2a-sdk`) | Sends messages to remote agents, tracks task/context IDs, handles multi-turn task continuity |

The user interacts only with the orchestrator. The orchestrator delegates to remote agents by calling Pydantic AI tools. Each tool is a thin wrapper around an A2A call.

---

## Layer 1 — Pydantic AI Orchestrator

### What it is

`orchestrator` is a standard Pydantic AI `Agent`. It has no MCP server and no direct access to tools that do work — its only capability is calling other agents via its registered tools.

```python
orchestrator = Agent(
    llm,
    deps_type=OrchestratorDeps,
    instructions=('You are an orchestrator agent...'),
)
```

### How it handles conversation memory

Every time the user sends a message, the loop calls:

```python
result = await orchestrator.run(user_text, deps=deps, message_history=message_history)
message_history = result.all_messages()
```

`result.all_messages()` returns the full Pydantic AI message trace for the entire session — every user message, every assistant message, every tool call request, and every tool result. This list is passed back into the next `orchestrator.run()` call as `message_history`, giving the LLM a complete, natively-structured view of everything that has happened.

**This is the key difference from the previous approach.** Previously, prior conversation history was serialised to plain text and prepended to each new message manually. Now it is preserved as structured chat messages in the exact format the LLM natively understands — the same mechanism used in `network_agent.py` from IP5.

### What the message history contains after two exchanges

```
[user]       "Tell me a joke about cats"
[assistant]  tool_call: call_joke_agent("Tell me a joke about cats")
[tool]       result: "Why don't cats play poker?..."
[assistant]  "Why don't cats play poker? Because they always show their hand!"

[user]       "Now make it about dogs"
[assistant]  tool_call: call_joke_agent("Make a joke about dogs")   ← LLM knows the context
[tool]       result: "Why did the dog sit in the shade?..."
[assistant]  "Why did the dog sit in the shade? It didn't want to be a hot dog!"
```

Every tool call and its result are first-class entries in this history, not flattened text.

### OrchestratorDeps

`OrchestratorDeps` is a dataclass that holds references to all connected remote agent servers. It is passed to every tool call via `RunContext`:

```python
@dataclass
class OrchestratorDeps:
    joke_server: ServerState
```

The orchestrator receives a populated `deps` instance at runtime and passes it through to tools automatically. This is how tools reach the correct `ServerState` without using globals.

---

## Layer 2 — A2A Transport (`ServerState`)

### What it is

`ServerState` manages everything related to one remote A2A server: the HTTP client, and the context/task ID state needed for multi-turn task continuity.

```
ServerState (one per remote agent)
  └─ contexts: dict[context_id → ContextState]
                  └─ tasks: dict[task_id → TaskRecord]
                  └─ active_task_id
```

### Why task/context ID tracking is still needed

The A2A protocol is stateful at the server side. When a remote agent asks a clarification question it returns state `input_required` along with a `task_id`. The follow-up answer **must** include the same `task_id`, otherwise the server creates a brand new task and loses the conversational context of the clarification.

`ServerState.apply_response()` records whether the last response was `input_required` and stores the `task_id` as `active_task_id`. `build_message()` then automatically includes that ID in the next outgoing message:

```python
def build_message(self, user_text: str) -> Message:
    ctx = self.active_context()
    return Message(
        ...
        task_id=ctx.active_task_id if ctx else None,  # continues the task if input_required
        context_id=self.active_context_id,
    )
```

When the task completes (`final_state=completed`), `active_task_id` is reset to `None` so the next message starts a fresh task within the same context.

### Multi-turn clarification flow (end to end)

```
User:         "Tell me a joke"
Orchestrator: calls call_joke_agent("Tell me a joke")
  → build_message("Tell me a joke")            task_id=None (new task)
  → server returns input_required, task_id=abc
  → apply_response(task_id=abc, needs_input=True)
  → active_task_id = abc
  ← tool returns: "What topic would you like?"

Orchestrator: relays question to user
User:         "programming"

Orchestrator: calls call_joke_agent("programming")
  → build_message("programming")               task_id=abc (continues same task)
  → server returns completed, task_id=abc
  → apply_response(task_id=abc, needs_input=False)
  → active_task_id = None
  ← tool returns: "Why do Java developers wear glasses?..."

Orchestrator: relays joke to user
```

The orchestrator LLM does not know about `task_id` at all — it just calls the tool twice. `ServerState` handles the continuity silently.

### send_and_process

`send_and_process` handles the raw A2A streaming event loop. It iterates events from `server.client.send_message(message)`, extracts the response text (from status message or artifact fallback), detects `input_required`, and calls `server.apply_response()` at the end.

It returns only the response text string — the tool function returns this to the orchestrator as the tool result.

---

## How context flows between turns (compared to before)

| Concern | Old approach | New approach |
|---|---|---|
| Prior conversation context | Manually serialised to text, prepended to outgoing message | `message_history=result.all_messages()` — structured, native |
| Tool call results | Not applicable (no tool calls at client level) | Natively in message history as `tool` role entries |
| Multi-turn task continuity (input_required) | `ServerState.active_task_id` | Same — unchanged |
| Who decides which agent to call | Hardcoded loop | Orchestrator LLM decides autonomously |

---

## Adding a second remote agent

1. Connect to the new server in `main()`:
   ```python
   topology_client = await ClientFactory.connect(agent='http://localhost:8001', ...)
   topology_server = ServerState(url='http://localhost:8001', agent_name=topology_card.name, client=topology_client)
   ```

2. Add it to `OrchestratorDeps`:
   ```python
   @dataclass
   class OrchestratorDeps:
       joke_server: ServerState
       topology_server: ServerState
   ```

3. Register a tool:
   ```python
   @orchestrator.tool
   async def get_network_topology(ctx: RunContext[OrchestratorDeps], query: str) -> str:
       """Fetch the current network topology from the Topology Agent."""
       logger = logging.getLogger(__name__)
       server = ctx.deps.topology_server
       message = server.build_message(query)
       result = await send_and_process(server, message, logger)
       return result or 'No response received from topology agent.'
   ```

4. Update the orchestrator's instructions to describe the new agent.

The orchestrator LLM will now autonomously decide when to call the topology agent and when to call another agent. If a second agent needs the topology result, the LLM already has it in `message_history` as a prior tool result and can include it in the next delegation — no manual wiring needed.

---

## `/state` debug command

Typing `/state` in the REPL prints the A2A-level state (contexts and tasks) for each connected server. This shows the protocol-level view — not the LLM conversation history.

Example output:
```
ServerState(Joke Agent @ http://localhost:8000)
  context a3f2b1c0 [ACTIVE]  (2 task(s))
    task 9d8e7f6a  state=completed  created=14:02:11
    task 1b2c3d4e [active task]  state=input_required  created=14:03:45
```
