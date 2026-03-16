"""Agent A - Joke Generator (A2A Server)

Exposes a Pydantic AI joke agent as an A2A server using the official a2a-sdk.
Implements AgentExecutor to bridge Pydantic AI with the A2A protocol.

Run with: uv run uvicorn agent_a_server:app --port 8000
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

load_dotenv(Path(__file__).parent / '.env')

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')

llm = OpenAIChatModel(
    'z-ai/glm-5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
)

agent = Agent(
    llm,
    instructions='You are a joke generator. When given a topic, respond with a single funny joke about it.',
)


class JokeAgentExecutor(AgentExecutor):
    """Bridges the Pydantic AI joke agent with the a2a-sdk AgentExecutor interface."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()
        if not user_input:
            await event_queue.enqueue_event(new_agent_text_message('No input provided.'))
            return

        # Extract text from the user message parts
        user_text = ''
        if hasattr(user_input, 'parts') and user_input.parts:
            for part in user_input.parts:
                if hasattr(part, 'root') and hasattr(part.root, 'text'):
                    user_text += part.root.text
                elif hasattr(part, 'text'):
                    user_text += part.text

        if not user_text:
            user_text = str(user_input)

        result = await agent.run(user_text)
        await event_queue.enqueue_event(new_agent_text_message(result.output))

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
    capabilities=AgentCapabilities(),
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
