"""Agent A - Joke Generator (A2A Server)

Exposes a Pydantic AI joke agent as an A2A server using the official a2a-sdk.
Implements AgentExecutor to bridge Pydantic AI with the A2A protocol.

Run with: uv run uvicorn agent_a_server:app --port 8000
"""

import os
from pathlib import Path
from uuid import uuid4

import logfire
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

load_dotenv(Path(__file__).parent / '.env')

LOGFIRE_TOKEN = os.getenv('LOGFIRE_TOKEN')
if LOGFIRE_TOKEN:
    logfire.configure(
        token=LOGFIRE_TOKEN,
        service_name='A2A Server Agent',
        console=False,
        distributed_tracing=True,
    )
    logfire.instrument_pydantic_ai()
    logfire.instrument_openai()
else:
    print('LOGFIRE_TOKEN not found. Running without Logfire observability.')

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')

llm = OpenAIChatModel(
    'z-ai/glm-5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
)

class AgentResult(BaseModel):
    response_text: str
    needs_input: bool
    clarification_question: str | None = None


agent = Agent(
    llm,
    output_type=AgentResult,
    instructions=(
        'You are a joke generator. When given a topic, respond with a funny joke.\n\n'
        'If the user\'s request is clear enough to generate a joke, set needs_input=False '
        'and put the joke in response_text.\n'
        'If you need more information (e.g. the topic is too vague, or you want to clarify '
        'the style of humor), set needs_input=True, put your clarification question in '
        'clarification_question, and leave response_text empty.'
    ),
)


class JokeAgentExecutor(AgentExecutor):
    """Bridges the Pydantic AI joke agent with the a2a-sdk AgentExecutor interface."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = context.get_user_input()
        if not user_text:
            await event_queue.enqueue_event(new_agent_text_message('No input provided.'))
            return

        updater = TaskUpdater(
            event_queue,
            task_id=context.task_id or uuid4().hex,
            context_id=context.context_id or uuid4().hex,
        )

        with logfire.span('JokeAgentExecutor.execute', user_text=user_text, task_id=context.task_id):
            result = await agent.run(user_text)
            output = result.output

            if output.needs_input:
                logfire.info('Clarification needed', question=output.clarification_question)
                await updater.requires_input(
                    message=new_agent_text_message(output.clarification_question or 'Could you clarify?'),
                    final=True,
                )
            else:
                logfire.info('Joke generated', response_text=output.response_text)
                await updater.complete(
                    message=new_agent_text_message(output.response_text),
                )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception('Cancel not supported')


skill = AgentSkill(
    id='joke_generator',
    name='Joke Generator',
    description='Generates a funny joke on any given topic',
    tags=['jokes', 'humor'],
    examples=['Tell me a joke about programming', 'Make a joke about cats'],
)

agent_card = AgentCard(
    name='Joke Agent',
    description='Generates jokes on any topic via A2A protocol',
    url='http://localhost:8000/',
    version='1.0.0',
    default_input_modes=['text'],
    default_output_modes=['text'],
    capabilities=AgentCapabilities(streaming=True, state_transition_history=True),
    skills=[skill],
)

request_handler = DefaultRequestHandler(
    agent_executor=JokeAgentExecutor(),
    task_store=InMemoryTaskStore(),
)

server = A2AStarletteApplication(
    agent_card=agent_card,
    http_handler=request_handler,
)

app = server.build()

if LOGFIRE_TOKEN:
    logfire.instrument_starlette(app)
