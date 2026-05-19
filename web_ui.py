"""Web UI entry point — starts all sub-agent lifespans then serves the chat UI.

Run with:
    uv run python web_ui.py

Then open http://127.0.0.1:7932
"""

import json
import logging
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.routing import Mount

from client_agent import UI_EXTRA_MODELS, _active_model_name, main_lifespan, orchestrator


class _ModelContextMiddleware(BaseHTTPMiddleware):
    """Reads the selected model from each POST /chat request and stores it in a ContextVar
    so that sub-agent tool calls in the same request context pick it up automatically."""

    async def dispatch(self, request: Request, call_next):
        if request.method == 'POST':
            body = await request.body()
            try:
                model_id: str = json.loads(body).get('model', '')
                if model_id.startswith('openrouter:'):
                    _active_model_name.set(model_id.removeprefix('openrouter:'))
            except Exception:
                pass
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: Starlette):
    async with main_lifespan():
        yield


app = Starlette(
    routes=[Mount('/', app=orchestrator.to_web(models=UI_EXTRA_MODELS))],
    lifespan=lifespan,
)
app.add_middleware(_ModelContextMiddleware)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host='127.0.0.1', port=7932)