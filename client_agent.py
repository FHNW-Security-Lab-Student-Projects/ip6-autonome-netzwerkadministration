import asyncio
import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from pathlib import Path

import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext
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



@dataclass
class TaskRecord:
    task_id: str
    final_state: str      # e.g. "completed", "input_required", "failed"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ContextState:
    context_id: str
    tasks: dict[str, TaskRecord] = field(default_factory=dict)  # task_id -> TaskRecord
    active_task_id: str | None = None

    def record_task(self, record: TaskRecord) -> None:
        self.tasks[record.task_id] = record



@dataclass
class ServerState:
    url: str
    agent_name: str
    client: Any
    contexts: dict[str, ContextState] = field(default_factory=dict)  # context_id -> ContextState
    active_context_id: str | None = None

    def active_context(self) -> ContextState | None:
        return self.contexts.get(self.active_context_id) if self.active_context_id else None

    def apply_response(
        self,
        task_id: str,
        context_id: str,
        needs_input: bool,
        final_state: str,
    ) -> None:
        """Update context/task records after a server response."""
        if context_id not in self.contexts:
            self.contexts[context_id] = ContextState(context_id=context_id)
        ctx = self.contexts[context_id]
        self.active_context_id = context_id
        ctx.record_task(TaskRecord(task_id=task_id, final_state=final_state))
        ctx.active_task_id = task_id if needs_input else None

    def build_message(self, user_text: str) -> Message:
        """Build a Message correctly scoped to the active context and task."""
        ctx = self.active_context()
        return Message(
            role='user',
            parts=[Part(root=TextPart(text=user_text))],
            message_id=uuid4().hex,
            task_id=ctx.active_task_id if ctx else None,
            context_id=self.active_context_id,
        )

    def debug_summary(self) -> str:
        """Return a multi-line string showing all tracked contexts and tasks."""
        lines = [f'ServerState({self.agent_name} @ {self.url})']
        if not self.contexts:
            lines.append('  (no contexts yet)')
            return '\n'.join(lines)
        for cid, ctx in self.contexts.items():
            active_marker = ' [ACTIVE]' if cid == self.active_context_id else ''
            lines.append(f'  context {cid[:8]}{active_marker}  ({len(ctx.tasks)} task(s))')
            for tid, rec in ctx.tasks.items():
                task_marker = ' [active task]' if tid == ctx.active_task_id else ''
                lines.append(
                    f'    task {tid[:8]}{task_marker}  state={rec.final_state}'
                    f'  created={rec.created_at.strftime("%H:%M:%S")}'
                )
        return '\n'.join(lines)


def extract_text(parts) -> str | None:
    """Extract text from a list of A2A message parts."""
    for part in parts:
        part_data = part.root if hasattr(part, 'root') else part
        if hasattr(part_data, 'text'):
            return part_data.text
    return None


async def send_and_process(server: ServerState, message: Message, logger: logging.Logger) -> str | None:
    """Send a message to server.client, process response events, and update server state.

    All context/task ID bookkeeping is handled via server.apply_response().
    Returns the response text, or None if no text was received.
    """
    response_text = None
    task_id = None
    context_id = None
    needs_input = False
    final_state = 'unknown'

    with logfire.span('A2A send_message', message_id=message.message_id):
        async for event in server.client.send_message(message):
            if isinstance(event, Message) and event.role == 'agent':
                logfire.info('A2A received Message', role=event.role, parts=[str(p) for p in event.parts])
                response_text = extract_text(event.parts)
            elif isinstance(event, tuple):
                task, update_event = event
                task_id = task.id
                context_id = task.context_id
                if task.status:
                    final_state = task.status.state.value if hasattr(task.status.state, 'value') else str(task.status.state)
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

    if task_id and context_id:
        server.apply_response(task_id, context_id, needs_input, final_state)

    return response_text


@dataclass
class OrchestratorDeps:
    joke_server: ServerState
    network_server: ServerState


@dataclass
class RemoteAgentResponse:
    text: str
    input_required: bool


async def _call_joke_agent(ctx: RunContext[OrchestratorDeps], request: str) -> RemoteAgentResponse:
    """Delegate a request to the remote Joke Agent."""
    logger = logging.getLogger(__name__)
    server = ctx.deps.joke_server
    message = server.build_message(request)
    result = await send_and_process(server, message, logger)
    ctx_state = server.active_context()
    needs_input = ctx_state is not None and ctx_state.active_task_id is not None
    return RemoteAgentResponse(
        text=result or 'No response received from joke agent.',
        input_required=needs_input,
    )


async def _call_network_agent(ctx: RunContext[OrchestratorDeps], request: str) -> RemoteAgentResponse:
    """Delegate a read-only network query to the remote Network Agent."""
    logger = logging.getLogger(__name__)
    server = ctx.deps.network_server
    message = server.build_message(request)
    result = await send_and_process(server, message, logger)
    ctx_state = server.active_context()
    needs_input = ctx_state is not None and ctx_state.active_task_id is not None
    return RemoteAgentResponse(
        text=result or 'No response received from network agent.',
        input_required=needs_input,
    )


def build_orchestrator(joke_card, network_card) -> Agent:
    def _skill_lines(card) -> str:
        return '\n'.join(f'    - {s.name}: {s.description}' for s in card.skills)

    instructions = (
        'You are an orchestrator agent that coordinates between specialised remote agents '
        'to fulfil user requests.\n\n'
        'Available remote agents:\n'
        f'- call_joke_agent ({joke_card.name}): {joke_card.description}\n'
        f'  Skills:\n{_skill_lines(joke_card)}\n\n'
        f'- call_network_agent ({network_card.name}): {network_card.description}\n'
        f'  Skills:\n{_skill_lines(network_card)}\n\n'
        'Delegate requests to the appropriate agent.\n\n'
        'When composing the `request` argument for any remote agent call, include relevant '
        'responses or results from other remote agents that were called earlier in this '
        'conversation — so each agent has the context it needs to do its job well.\n\n'
        'IMPORTANT: When a tool returns input_required=True, the remote agent is waiting for '
        'the user to answer a clarification question. You MUST output the text field verbatim '
        'as your final response — do NOT answer the question yourself, do NOT call any tool again.'
    )
    agent = Agent(llm, deps_type=OrchestratorDeps, instructions=instructions)
    agent.tool(_call_joke_agent)
    agent.tool(_call_network_agent)
    return agent


async def main():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    async with httpx.AsyncClient(timeout=60) as http_client:
        joke_client = await ClientFactory.connect(
            agent='http://localhost:8000',
            client_config=ClientConfig(httpx_client=http_client),
        )
        joke_card = await joke_client.get_card()
        logger.info('Connected to agent: %s', joke_card.name)

        network_client = await ClientFactory.connect(
            agent='http://localhost:8001',
            client_config=ClientConfig(httpx_client=http_client),
        )
        network_card = await network_client.get_card()
        logger.info('Connected to agent: %s', network_card.name)

        orchestrator = build_orchestrator(joke_card, network_card)

        joke_server = ServerState(
            url='http://localhost:8000',
            agent_name=joke_card.name,
            client=joke_client,
        )
        network_server = ServerState(
            url='http://localhost:8001',
            agent_name=network_card.name,
            client=network_client,
        )

        deps = OrchestratorDeps(joke_server=joke_server, network_server=network_server)
        message_history = []

        print('Type your message and press Enter. Press Ctrl+C or type "exit" to quit.')
        print('Type "/state" to show tracked contexts and tasks.\n')

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
            if user_text == '/state':
                print(joke_server.debug_summary())
                print(network_server.debug_summary())
                continue

            result = await orchestrator.run(user_text, deps=deps, message_history=message_history)
            message_history = result.all_messages()
            print(f'\nOrchestrator: {result.output}\n')


if __name__ == '__main__':
    asyncio.run(main())
