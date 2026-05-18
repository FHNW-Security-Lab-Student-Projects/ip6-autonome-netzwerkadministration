# Multi-Model Web UI

## Problem

Pydantic AI's `agent.to_web()` renders a model-selection dropdown in the chat UI. When a
model is selected there, Pydantic AI automatically uses it for the **orchestrator** — the
agent whose `to_web()` was called. However, sub-agents (`network_agent`, `config_agent`,
`snapshot_agent`) each have their own hardcoded `OpenAIChatModel` instance and are
completely unaffected by what the user picks in the UI.

The goal was to make model selection in the UI apply to every agent in the system.

---

## How Pydantic AI's `to_web()` works internally

```
Browser POST /chat  { "messages": [...], "model": "openrouter:z-ai/glm-5.1", ... }
       ↓
Pydantic AI route handler (api.py: post_chat)
       ↓
  model_ref = model_id_to_ref.get("openrouter:z-ai/glm-5.1")   ← looked up from models dict
       ↓
  VercelAIAdapter.dispatch_request(..., model=model_ref, ...)
       ↓
  orchestrator.run(user_message, model=model_ref)   ← only the orchestrator gets overridden
```

The `model` field in the JSON body uses the format `<provider>:<model_name>`, e.g.
`openrouter:z-ai/glm-5`. This comes from `Model.model_id` (`f'{self.system}:{self.model_name}'`).
For `OpenAIChatModel` with `OpenRouterProvider`, `system` is `'openrouter'`.

---

## Solution: Middleware + ContextVar

### 1. ContextVar in `client_agent.py`

```python
from contextvars import ContextVar
_active_model_name: ContextVar[str | None] = ContextVar('_active_model_name', default=None)
```

A `ContextVar` is per-asyncio-task and is automatically inherited by any task created from
the current context (via `asyncio.create_task` / anyio task groups). It holds the raw
OpenRouter model name string, e.g. `'z-ai/glm-5.1'`, or `None` when the default is in use.

### 2. Starlette middleware in `web_ui.py`

```python
class _ModelContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == 'POST':
            body = await request.body()  # cached by Starlette; safe to call again downstream
            try:
                model_id: str = json.loads(body).get('model', '')
                if model_id.startswith('openrouter:'):
                    _active_model_name.set(model_id.removeprefix('openrouter:'))
            except Exception:
                pass
        return await call_next(request)
```

`BaseHTTPMiddleware` runs the inner app via anyio's task group (`start_soon`). The new
task inherits the current asyncio context at the moment `start_soon` is called, which is
after `_active_model_name.set(...)`. So the ContextVar value is visible throughout the
entire request — including inside every tool function the orchestrator calls.

The middleware is registered after the app is built:

```python
app.add_middleware(_ModelContextMiddleware)
```

Starlette applies middleware in LIFO order, so this wraps the outermost ASGI layer.

### 3. Model resolution helper in `client_agent.py`

```python
_model_cache: dict[tuple[str, bool], OpenAIChatModel] = {}

def _get_agent_model(parallel_tools: bool = True) -> OpenAIChatModel | None:
    name = _active_model_name.get()
    if not name:
        return None
    key = (name, parallel_tools)
    if key not in _model_cache:
        _model_cache[key] = OpenAIChatModel(
            name,
            provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
            settings=ModelSettings(parallel_tool_calls=parallel_tools, timeout=180),
        )
    return _model_cache[key]
```

The `parallel_tools` flag exists because `config_agent` requires sequential tool calls
(`parallel_tool_calls=False`) for safety. A single model name can produce two cached
variants — one for parallel agents, one for the config agent.

The cache is process-global (not per-request). `OpenAIChatModel` creates an `AsyncOpenAI`
HTTP client internally, so caching avoids creating a new client on every tool invocation.

### 4. Sub-agent calls pass the override model

Each orchestrator tool function passes `model=_get_agent_model(...)` to the sub-agent's
`run()` call. When `model=None` (no UI selection), Pydantic AI uses the sub-agent's own
configured model — so the default behaviour is fully preserved.

```python
# network_agent and snapshot_agent — parallel tool calls OK
result = await network_agent.run(request, model=_get_agent_model())

# config_agent — must stay sequential for safety
result = await config_agent.run(request, model=_get_agent_model(parallel_tools=False))
```

### 5. Dropdown models in `web_ui.py`

```python
orchestrator.to_web(models=UI_EXTRA_MODELS)
```

`UI_EXTRA_MODELS` is a `dict[str, OpenAIChatModel]` defined in `client_agent.py`. The
dict keys are the display labels shown in the dropdown; the values are `OpenAIChatModel`
instances (without `ModelSettings`, since `to_web` only needs them for capability detection
and model ID resolution — the per-agent settings are applied by `_get_agent_model()`).

```python
AVAILABLE_OPENROUTER_MODELS: dict[str, str] = {
    'GLM5':              'z-ai/glm-5',
    'Claude Sonnet 4.6': 'anthropic/claude-sonnet-4.6',
    'Claude Opus 4.7':   'anthropic/claude-opus-4.7',
    'GLM5.1':            'z-ai/glm-5.1',
}

UI_EXTRA_MODELS: dict[str, OpenAIChatModel] = {
    label: OpenAIChatModel(name, provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY))
    for label, name in AVAILABLE_OPENROUTER_MODELS.items()
}
```

The orchestrator's own model (`z-ai/glm-5`) is always included by Pydantic AI regardless,
so it appears in the dropdown even if not listed in `AVAILABLE_OPENROUTER_MODELS`.

---

## Request lifecycle (full picture)

```
1. User selects "Claude Sonnet 4.6" in the UI and sends a message.

2. Browser POST /chat  { "model": "openrouter:anthropic/claude-sonnet-4.6", "messages": [...] }

3. _ModelContextMiddleware.dispatch() runs in the request's asyncio task:
   - Reads body (cached; not consumed)
   - Strips prefix → _active_model_name.set("anthropic/claude-sonnet-4.6")

4. call_next(request) runs the Pydantic AI route handler in a child task that
   inherits the ContextVar value set in step 3.

5. Pydantic AI uses "anthropic/claude-sonnet-4.6" for the orchestrator run.

6. Orchestrator calls call_network_agent("..."):
   - _get_agent_model() reads "anthropic/claude-sonnet-4.6" from ContextVar
   - Returns (or creates and caches) OpenAIChatModel("anthropic/claude-sonnet-4.6",
       settings=ModelSettings(parallel_tool_calls=True, timeout=180))
   - network_agent.run(request, model=<that model>) uses Claude Sonnet for the sub-agent.

7. Same for call_snapshot_agent and call_config_agent (the latter gets parallel_tools=False).

8. Request ends. ContextVar value is scoped to this task's context and does not affect
   other concurrent requests.
```

---

## What is NOT covered

**Syslog investigator** (`syslog_investigations.py`): The `syslog_investigator` agent runs
LLM analysis in background asyncio tasks that are launched independently of the web
request lifecycle. There is no reliable way to propagate a per-request ContextVar into a
long-lived background task. The syslog investigator always uses its own hardcoded model.

---

## Adding or removing models

Edit `AVAILABLE_OPENROUTER_MODELS` in `client_agent.py`:

```python
AVAILABLE_OPENROUTER_MODELS: dict[str, str] = {
    'My Label': 'provider/model-name',   # add a model
    # remove a line to remove a model from the dropdown
}
```

The model name must be a valid OpenRouter model ID. No changes to `web_ui.py` or the tool
functions are needed.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Dropdown shows only GLM 5 | `UI_EXTRA_MODELS` is empty or import failed |
| Sub-agents still use GLM 5 | Middleware not registered, or `model_id` format changed (check prefix) |
| Sub-agent call fails with unknown model | OpenRouter model name in `AVAILABLE_OPENROUTER_MODELS` is wrong |
| Config agent runs tools in parallel | `_get_agent_model(parallel_tools=False)` not used in `call_config_agent` |
| Concurrent requests interfere | ContextVar isolation broken — should not happen with asyncio tasks |