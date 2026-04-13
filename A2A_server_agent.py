"""Agent A - Joke Generator (A2A Server)

Exposes a Pydantic AI joke agent as an A2A server using the official a2a-sdk.
Implements AgentExecutor to bridge Pydantic AI with the A2A protocol.

Run with: uv run uvicorn agent_a_server:app --port 8000
"""

from enum import Enum
from pathlib import Path
from uuid import uuid4

import logfire
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

from a2a_utils import BaseAgentExecutor, build_a2a_app, require_openrouter_key, setup_logfire

load_dotenv(Path(__file__).parent / '.env')
setup_logfire('A2A Server Agent')
OPENROUTER_API_KEY = require_openrouter_key()

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


# --- Routing agent: classifies user intent as message or task ---

class ResponseType(str, Enum):
    message = 'message'
    task = 'task'


class RoutingDecision(BaseModel):
    response_type: ResponseType
    reasoning: str


routing_agent = Agent(
    OpenAIChatModel(
        'z-ai/glm-5',
        provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
    ),
    output_type=RoutingDecision,
    instructions=(
        'You classify user messages as "message" or "task".\n\n'
        'Use "message" for: greetings, small talk, capability questions '
        '("what can you do?"), thanks, goodbyes.\n'
        'Use "task" for: joke requests, creative generation, anything '
        'goal-oriented or needing follow-up.\n\n'
        'When in doubt, prefer "task".'
    ),
)

conversational_agent = Agent(
    llm,
    instructions=(
        'You are a friendly joke agent assistant. Respond briefly to greetings, '
        'small talk, and simple questions. Keep responses short and natural.'
    ),
)


class JokeAgentExecutor(BaseAgentExecutor):
    """Bridges the Pydantic AI joke agent with the a2a-sdk AgentExecutor interface."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = context.get_user_input()
        if not user_text:
            await event_queue.enqueue_event(new_agent_text_message('No input provided.'))
            return

        with logfire.span('JokeAgentExecutor.execute', user_text=user_text, task_id=context.task_id):
            # Skip routing if continuing an existing task (e.g., answering a clarification)
            if context.current_task is None:
                routing_result = await routing_agent.run(user_text)
                decision = routing_result.output
                logfire.info('Routing decision', response_type=decision.response_type, reasoning=decision.reasoning)

                if decision.response_type == ResponseType.message:
                    conv_result = await conversational_agent.run(user_text)
                    await event_queue.enqueue_event(
                        new_agent_text_message(str(conv_result.output))
                    )
                    return

            # Task path — joke agent with lifecycle tracking
            updater = TaskUpdater(
                event_queue,
                task_id=context.task_id or uuid4().hex,
                context_id=context.context_id or uuid4().hex,
            )

            # Retrieve per-context Pydantic AI message history (tool calls, model turns, etc.)
            ctx_key = context.context_id or context.task_id
            history = self._context_history.get(ctx_key, [])
            logfire.info('Context history', ctx_key=ctx_key, messages=len(history))
            print(f'\n[HISTORY] ctx_key={ctx_key!r}  messages={len(history)}')
            for i, msg in enumerate(history):
                print(f'  [{i}] {msg}')

            result = await agent.run(user_text, message_history=history)
            output = result.output

            # Persist updated history for next turn in this context
            self._context_history[ctx_key] = result.all_messages()

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

skill = AgentSkill(
    id='joke_generator',
    name='Joke Generator',
    description='Generates a funny joke on any given topic',
    tags=['jokes', 'humor'],
    examples=['Tell me a joke about programming', 'Make a joke about cats'],
)

agent_card = AgentCard(
    name='Joke Agent',
    description='A conversational joke agent. Handles greetings and simple questions directly via messages, and creates tasks for joke generation and other goal-oriented requests.',
    url='http://localhost:8000/',
    version='1.0.0',
    default_input_modes=['text'],
    default_output_modes=['text'],
    capabilities=AgentCapabilities(streaming=True, state_transition_history=True),
    skills=[skill],
)

app = build_a2a_app(agent_card, JokeAgentExecutor())
