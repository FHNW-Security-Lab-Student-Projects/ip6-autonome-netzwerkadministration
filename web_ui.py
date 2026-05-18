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
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.routing import Mount

from client_agent import (
    DEFAULT_AGENT_MODEL,
    UI_EXTRA_MODELS,
    _active_model_name,
    _active_session,
    main_lifespan,
    orchestrator,
)
from experiment_tracker import begin_session, finish_session


def _extract_user_query(body: bytes) -> str:
    """Extract the last user message text from the Pydantic AI web UI request body."""
    try:
        data = json.loads(body)
        messages = data.get('messages', [])
        for msg in reversed(messages):
            if msg.get('role') == 'user':
                for part in msg.get('parts', []):
                    if part.get('type') == 'text':
                        return part.get('text', '')
    except Exception:
        pass
    return ''


class _ModelContextMiddleware(BaseHTTPMiddleware):
    """Sets the active model and creates an experiment session for each POST /chat request.

    The session is finished via a BackgroundTask once the full SSE stream has been sent,
    ensuring all sub-agent run records have been recorded before finish_session() is called.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method == 'POST':
            body = await request.body()
            model_name = DEFAULT_AGENT_MODEL
            try:
                data = json.loads(body)
                model_id: str = data.get('model', '')
                if model_id.startswith('openrouter:'):
                    model_name = model_id.removeprefix('openrouter:')
                _active_model_name.set(model_name)
            except Exception:
                pass

            user_query = _extract_user_query(body)
            session = begin_session(user_query=user_query, model=model_name)
            _active_session.set(session)

            response = await call_next(request)

            # BackgroundTask runs after the full SSE stream is sent to the client,
            # by which point all sub-agent tool calls have completed and been recorded.
            bg = BackgroundTask(finish_session, session)
            if response.background is None:
                response.background = bg
            else:
                existing = response.background
                async def _both():
                    await existing()
                    await bg()
                response.background = BackgroundTask(_both)
            return response

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
