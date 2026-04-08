"""Network A2A Server Agent

Exposes the IP5 network agent (Nokia SR Linux + MCP tools) as an A2A server.
Read-only operations only: show commands, device info, topology, backup listings.
Adapted from /IP5/Repo/ip-5-autonome-netzwerkadministration.

Run with: uv run uvicorn network_A2A_server_agent:app --port 8001
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

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
        service_name='Network A2A Server Agent',
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

knowledge_base_file = Path(__file__).parent / 'sr_linux_knowledge.txt'
SR_LINUX_KNOWLEDGE = ''
if knowledge_base_file.exists():
    SR_LINUX_KNOWLEDGE = knowledge_base_file.read_text()
else:
    print(f'Warning: sr_linux_knowledge.txt not found at {knowledge_base_file}')

llm = OpenAIChatModel(
    'anthropic/claude-opus-4.5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
    settings=ModelSettings(parallel_tool_calls=False),
)

mcp_server = MCPServerStdio(
    command='uv',
    args=['run', 'mcp_server.py'],
    tool_prefix='network_',
)

network_agent = Agent(
    model=llm,
    toolsets=[mcp_server],
    system_prompt=f"""You are a read-only network monitoring assistant for Nokia SR Linux devices.

ALLOWED TOOLS (read-only):
- network_execute_show_command: Run show/info commands on a device
- network_get_device_info: Look up a device in the inventory
- network_list_all_devices: List all devices in the inventory
- network_get_topology: Read the ContainerLab topology file
- network_list_backups: List available configuration backups for a device

FORBIDDEN: You must NEVER call network_configure_device, network_validate_config,
network_backup_config, or network_restore_config. These tools make changes and are
disabled in this read-only server.

WORKFLOW:
- For device queries, show commands, or topology questions: use the appropriate tool and
  report the result clearly.
- For greetings or capability questions: respond directly without using tools.
- Always format output in a readable way (use lists or tables where appropriate).

{'-' * 80}
NOKIA SR LINUX KNOWLEDGE BASE (for interpreting output):
{'-' * 80}
{SR_LINUX_KNOWLEDGE}
{'-' * 80}
END OF KNOWLEDGE BASE
{'-' * 80}
""",
)


class NetworkAgentExecutor(AgentExecutor):
    """Bridges the read-only network agent with the a2a-sdk AgentExecutor interface."""

    def __init__(self) -> None:
        self._context_history: dict[str, list] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = context.get_user_input()
        if not user_text:
            await event_queue.enqueue_event(new_agent_text_message('No input provided.'))
            return

        with logfire.span('NetworkAgentExecutor.execute', user_text=user_text, task_id=context.task_id):
            updater = TaskUpdater(
                event_queue,
                task_id=context.task_id or uuid4().hex,
                context_id=context.context_id or uuid4().hex,
            )

            ctx_key = context.context_id or context.task_id
            history = self._context_history.get(ctx_key, [])
            logfire.info('Context history', ctx_key=ctx_key, messages=len(history))

            try:
                result = await network_agent.run(user_text, message_history=history)
            except Exception as exc:
                logfire.error('Network agent error', error=str(exc))
                await updater.failed(message=new_agent_text_message(f'Agent error: {exc}'))
                return

            self._context_history[ctx_key] = result.all_messages()
            logfire.info('Network query completed')
            await updater.complete(message=new_agent_text_message(str(result.output)))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise Exception('Cancel not supported')


skill_show = AgentSkill(
    id='network_show',
    name='Show Network State',
    description=(
        'Execute read-only show commands on Nokia SR Linux devices. '
        'Query interface status, routing tables, device info, topology, and backup listings.'
    ),
    tags=['network', 'show', 'read-only', 'nokia', 'sr-linux'],
    examples=[
        'Show all interfaces on switch1',
        'List all devices in the inventory',
        'Get the network topology',
        'Show routing table on router1',
    ],
)

agent_card = AgentCard(
    name='Network Agent',
    description=(
        'Read-only network monitoring agent for Nokia SR Linux devices. '
        'Executes show commands, queries device state, and retrieves topology information.'
    ),
    url='http://localhost:8001/',
    version='1.0.0',
    default_input_modes=['text'],
    default_output_modes=['text'],
    capabilities=AgentCapabilities(streaming=True, state_transition_history=True),
    skills=[skill_show],
)

task_store = InMemoryTaskStore()

request_handler = DefaultRequestHandler(
    agent_executor=NetworkAgentExecutor(),
    task_store=task_store,
)

server = A2AStarletteApplication(
    agent_card=agent_card,
    http_handler=request_handler,
)


@asynccontextmanager
async def lifespan(_):
    """Start the MCP server subprocess alongside the A2A server."""
    print('Starting MCP server...')
    async with network_agent:
        print('MCP server running. Network A2A server ready on port 8001.')
        yield
    print('MCP server stopped.')


app = server.build(lifespan=lifespan)

if LOGFIRE_TOKEN:
    logfire.instrument_starlette(app)
