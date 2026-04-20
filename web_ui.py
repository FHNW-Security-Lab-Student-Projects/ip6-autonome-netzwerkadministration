"""Web UI entry point — starts all sub-agent lifespans then serves the chat UI.

Run with:
    uv run python web_ui.py

Then open http://127.0.0.1:7932
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

from client_agent import OrchestratorDeps, main_lifespan, orchestrator


@asynccontextmanager
async def lifespan(app: Starlette):
    async with main_lifespan():
        yield


app = Starlette(
    routes=[Mount('/', app=orchestrator.to_web(deps=OrchestratorDeps()))],
    lifespan=lifespan,
)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host='127.0.0.1', port=7932)
