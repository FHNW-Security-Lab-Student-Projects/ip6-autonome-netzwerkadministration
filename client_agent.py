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
from a2a.types import Message, Part, TextPart

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

        print('Sending message to A2A Server Agent')

        # send_message yields ClientEvent (tuple[Task, UpdateEvent]) or Message
        joke_text = None
        with logfire.span('A2A send_message', message_id=message.message_id, user_text='Tell me a joke about programming'):  # manual instrumentation
            async for event in client.send_message(message):
                if isinstance(event, Message) and event.role == 'agent':
                    logfire.info('A2A received Message', role=event.role, parts=[str(p) for p in event.parts])  # manual instrumentation
                    for part in event.parts:
                        part_data = part.root if hasattr(part, 'root') else part
                        if hasattr(part_data, 'text'):
                            joke_text = part_data.text
                            break
                elif isinstance(event, tuple):
                    task, update_event = event
                    logger.info(f'Task: ${task} \n, update_event: ${update_event}')
                    logfire.info('A2A received ClientEvent', task_id=task.id, status=str(task.status), update_type=type(update_event).__name__)  # manual instrumentation
                    # Check status message (set by TaskUpdater.complete/failed/etc.)
                    if task.status and task.status.message and task.status.message.parts:
                        for part in task.status.message.parts:
                            part_data = part.root if hasattr(part, 'root') else part
                            if hasattr(part_data, 'text'):
                                joke_text = part_data.text
                                break
                    # Fallback: check artifacts
                    if not joke_text and task.artifacts:
                        for artifact in task.artifacts:
                            for part in artifact.parts:
                                part_data = part.root if hasattr(part, 'root') else part
                                if hasattr(part_data, 'text'):
                                    joke_text = part_data.text
                                    break
                            if joke_text:
                                break

    if not joke_text:
        print('Could not extract joke from A2A server Agent.')
        return

    print(f'\nOriginal joke from A2A Server agent:\n{joke_text}')

if __name__ == '__main__':
    asyncio.run(main())
