"""Shared utilities for A2A server agents.

Centralises:
- Logfire setup (configure, instrument_pydantic_ai, instrument_openai)
- OPENROUTER_API_KEY loading
- A2A server wiring (InMemoryTaskStore + DefaultRequestHandler + A2AStarletteApplication)
- BaseAgentExecutor with _context_history and cancel() stub
"""

import os

import logfire
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard


def setup_logfire(service_name: str, *, instrument_llm: bool = True) -> None:
    """Configure Logfire if LOGFIRE_TOKEN is set.

    Args:
        service_name: Shown in Logfire traces as the originating service.
        instrument_llm: If True (default), also calls instrument_pydantic_ai()
                        and instrument_openai(). Pass False for agents that
                        have no LLM in the request path (e.g. topology agent).
    """
    token = os.getenv('LOGFIRE_TOKEN')
    if token:
        logfire.configure(
            token=token,
            service_name=service_name,
            console=False,
            distributed_tracing=True,
        )
        if instrument_llm:
            logfire.instrument_pydantic_ai()
            logfire.instrument_openai()
    else:
        print('LOGFIRE_TOKEN not found. Running without Logfire observability.')


def require_openrouter_key() -> str:
    """Return OPENROUTER_API_KEY from the environment, or raise ValueError."""
    key = os.getenv('OPENROUTER_API_KEY')
    if not key:
        raise ValueError(
            'OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.'
        )
    return key


def build_a2a_app(agent_card: AgentCard, executor: AgentExecutor, *, lifespan=None):
    """Wire up the standard A2A server stack and return the ASGI app.

    Creates InMemoryTaskStore + DefaultRequestHandler + A2AStarletteApplication,
    then instruments Starlette with Logfire when LOGFIRE_TOKEN is set.
    """
    task_store = InMemoryTaskStore()
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )
    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )
    app = server.build(lifespan=lifespan)
    if os.getenv('LOGFIRE_TOKEN'):
        logfire.instrument_starlette(app)
    return app


class BaseAgentExecutor(AgentExecutor):
    """Base class for Pydantic AI–backed A2A executors.

    Provides:
    - _context_history: per-context Pydantic AI message history dict
    - cancel(): raises an exception (no cancellation support)
    """

    def __init__(self) -> None:
        self._context_history: dict[str, list] = {}

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception('Cancel not supported')
