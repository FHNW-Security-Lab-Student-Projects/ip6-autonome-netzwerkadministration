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
from a2a.types import Message, Part, TextPart, TaskState

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


def extract_text(parts) -> str | None:
    """Extract text from a list of A2A message parts."""
    for part in parts:
        part_data = part.root if hasattr(part, 'root') else part
        if hasattr(part_data, 'text'):
            return part_data.text
    return None


async def send_and_process(client, message, logger) -> tuple[str | None, str | None, str | None, bool]:
    """Send a message and process the response.

    Returns (response_text, task_id, context_id, needs_input).
    If needs_input is True, the server requires further user input to continue.
    """
    response_text = None
    task_id = None
    context_id = None
    needs_input = False

    with logfire.span('A2A send_message', message_id=message.message_id):
        async for event in client.send_message(message):
            if isinstance(event, Message) and event.role == 'agent':
                logfire.info('A2A received Message', role=event.role, parts=[str(p) for p in event.parts])
                response_text = extract_text(event.parts)
            elif isinstance(event, tuple):
                task, update_event = event
                task_id = task.id
                context_id = task.context_id
                logger.info(f'Task: {task.id}, status: {task.status.state if task.status else "unknown"}, update: {type(update_event).__name__}')
                logfire.info('A2A received ClientEvent', task_id=task.id, status=str(task.status), update_type=type(update_event).__name__)

                if task.status and task.status.state == TaskState.input_required:
                    needs_input = True

                # Extract text from status message
                if task.status and task.status.message and task.status.message.parts:
                    response_text = extract_text(task.status.message.parts)

                # Fallback: check artifacts
                if not response_text and task.artifacts:
                    for artifact in task.artifacts:
                        text = extract_text(artifact.parts)
                        if text:
                            response_text = text
                            break

    return response_text, task_id, context_id, needs_input


async def main():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    async with httpx.AsyncClient(timeout=60) as http_client:
        client = await ClientFactory.connect(
            agent='http://localhost:8000',
            client_config=ClientConfig(httpx_client=http_client),
        )
        agent_card = await client.get_card()
        logger.info('Connected to agent: %s', agent_card.name)

        task_id = None
        context_id = None

        print('Type your message and press Enter. Press Ctrl+C or type "exit" to quit.\n')

        while True:
            try:
                user_text = input('You: ').strip()
            except (KeyboardInterrupt, EOFError):
                print('\nGoodbye!')
                break

            if not user_text:
                continue
            if user_text.lower() in ('exit', 'quit', '/exit', '/quit'):
                print('Goodbye!')
                break

            message = Message(
                role='user',
                parts=[Part(root=TextPart(text=user_text))],
                message_id=uuid4().hex,
                task_id=task_id,
                context_id=context_id,
            )

            print(f'Sending message to {agent_card.name}...')
            response_text, task_id, context_id, needs_input = await send_and_process(client, message, logger)

            if not response_text:
                print('No response received from server agent.')
                continue

            print(f'\n{agent_card.name}: {response_text}\n')

            if not needs_input:
                # Task completed — reset task_id so the next message starts a new task,
                # but keep context_id to maintain conversational continuity (same "chat window").
                task_id = None

if __name__ == '__main__':
    asyncio.run(main())
