"""Agent B - Translator (A2A Client)

Connects to Agent A's A2A server, requests a joke, then translates it to German
using its own Pydantic AI agent.

Prerequisites: Agent A must be running on port 8000
Run with: uv run python agent_b_client.py
"""

import asyncio
import os
import logging
from uuid import uuid4
from pathlib import Path

import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

import httpx
from a2a.client import ClientFactory, ClientConfig
from a2a.types import Message, Part, TextPart, Task

load_dotenv(Path(__file__).parent / '.env')

LOGFIRE_TOKEN = os.getenv('LOGFIRE_TOKEN')
if LOGFIRE_TOKEN:
    logfire.configure(
        token=LOGFIRE_TOKEN,
        service_name='Client Agent',
        console=False,
        distributed_tracing=True,  # manual instrumentation — links traces across Agent B → Agent A
    )
    logfire.instrument_pydantic_ai()
    logfire.instrument_openai()
    logfire.instrument_httpx(capture_headers=True, capture_request_body=True, capture_response_body=True)  # manual instrumentation — captures full HTTP payloads
else:
    print('LOGFIRE_TOKEN not found. Running without Logfire observability.')

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')

llm = OpenAIChatModel(
    'z-ai/glm-5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
)

translator_agent = Agent(
    llm,
    instructions='You are a translator. Translate the given text to German. Return only the translation, nothing else.',
)


async def main():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    async with httpx.AsyncClient(timeout=60) as http_client:
        # Connect to Agent A using ClientFactory (replaces deprecated A2AClient)
        client = await ClientFactory.connect(
            agent='http://localhost:8000',
            client_config=ClientConfig(httpx_client=http_client),
        )
        agent_card = await client.get_card()
        logger.info('Connected to agent: %s', agent_card.name)

        # Build the A2A message
        message = Message(
            role='user',
            parts=[Part(root=TextPart(text='Tell me a joke about programming'))],
            message_id=uuid4().hex,
        )

        print('Sending message to Agent A (Joke Agent)...')

        # send_message returns an async iterator of events
        joke_text = None
        with logfire.span('A2A send_message', message_id=message.message_id, user_text='Tell me a joke about programming'):  # manual instrumentation
            async for event in client.send_message(message):
                if isinstance(event, Message) and event.role == 'agent':
                    logfire.info('A2A received Message', role=event.role, parts=[str(p) for p in event.parts])  # manual instrumentation
                    # Direct message response
                    for part in event.parts:
                        part_data = part.root if hasattr(part, 'root') else part
                        if hasattr(part_data, 'text'):
                            joke_text = part_data.text
                            break
                elif isinstance(event, Task):
                    logfire.info('A2A received Task', task_id=event.id, status=str(event.status))  # manual instrumentation
                    # Task-based response — extract from artifacts or history
                    if event.artifacts:
                        for artifact in event.artifacts:
                            for part in artifact.parts:
                                part_data = part.root if hasattr(part, 'root') else part
                                if hasattr(part_data, 'text'):
                                    joke_text = part_data.text
                                    break
                            if joke_text:
                                break
                    if not joke_text and event.history:
                        for msg in reversed(event.history):
                            if msg.role == 'agent':
                                for part in msg.parts:
                                    part_data = part.root if hasattr(part, 'root') else part
                                    if hasattr(part_data, 'text'):
                                        joke_text = part_data.text
                                        break
                                if joke_text:
                                    break

    if not joke_text:
        print('Could not extract joke from Agent A response.')
        return

    print(f'\nOriginal joke from Agent A:\n{joke_text}')

if __name__ == '__main__':
    asyncio.run(main())
