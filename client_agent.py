import asyncio
import os
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from network_agent import NetworkAgentResult, network_agent, network_lifespan
from topology_agent import get_topology_response, topology_lifespan
from syslog_agent import handle_syslog_request, syslog_lifespan

load_dotenv(Path(__file__).parent / '.env')

LOGFIRE_TOKEN = os.getenv('LOGFIRE_TOKEN')
if LOGFIRE_TOKEN:
    logfire.configure(
        token=LOGFIRE_TOKEN,
        service_name='Orchestrator',
        console=False,
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


@dataclass
class OrchestratorDeps:
    syslog_history: dict[str, list] = field(default_factory=dict)


INSTRUCTIONS = (
    'You are an orchestrator agent that coordinates between specialised sub-agents '
    'to fulfil user requests.\n\n'
    'Available sub-agents:\n'
    '- call_network_agent (Network Agent): Read-only network monitoring agent for Nokia SR Linux '
    'devices. Executes show commands, queries device state, and retrieves inventory information.\n'
    '  Skills:\n'
    '    - Show Network State: Execute read-only show commands on Nokia SR Linux devices. '
    'Query interface status, routing tables, device info, and backup listings.\n'
    '  Return type: NetworkAgentResult. If needs_clarification is true, ask the user the '
    'clarifying_questions before calling again with the complete information.\n\n'
    '- call_topology_agent (Topology Discovery Agent): Returns a pre-built, cached network topology '
    'discovered from ContainerLab devices via LLDP. Topology is refreshed every 60 seconds.\n'
    '  Skills:\n'
    '    - Discover Network Topology: Returns the latest cached network topology with a Mermaid '
    'diagram and link/node details.\n\n'
    '- call_syslog_agent (Syslog Incident Agent): Monitors Nokia SR Linux syslog via Loki. '
    'Automatically opens incidents for error/critical/alert/emergency events and runs '
    'LLM-driven investigation. Supports listing, inspecting, and continuing troubleshooting.\n'
    '  Skills:\n'
    '    - Syslog Incident Management: List active syslog incidents, get full investigation '
    'details, or continue LLM-driven troubleshooting for a specific incident.\n\n'
    'Delegate requests to the appropriate sub-agent.\n\n'
    'When composing the `request` argument for any sub-agent call, write it as a '
    'self-contained message — the sub-agent has no access to the conversation history '
    'and depends entirely on what you include. Specifically:\n'
    '- Include all relevant context from the user\'s messages: their goal, preferences, '
    'constraints, or any details they mentioned that could help the sub-agent.\n'
    '- Include relevant results or outputs from other sub-agents called earlier in '
    'this conversation, if they inform the current task.'
)

orchestrator = Agent(llm, deps_type=OrchestratorDeps, instructions=INSTRUCTIONS)


@orchestrator.tool
async def call_network_agent(ctx: RunContext[OrchestratorDeps], request: str) -> NetworkAgentResult:
    """Delegate a read-only network query to the Network Agent."""
    result = await network_agent.run(request)
    return result.output


@orchestrator.tool
async def call_topology_agent(ctx: RunContext[OrchestratorDeps], request: str) -> str:
    """Retrieve the cached network topology from the Topology Agent."""
    response = get_topology_response()
    if response is None:
        return 'Topology cache is still warming up — please retry in a moment.'
    return response


@orchestrator.tool
async def call_syslog_agent(ctx: RunContext[OrchestratorDeps], request: str) -> str:
    """Query the Syslog Incident Agent — list incidents, get details, or continue troubleshooting."""
    history = ctx.deps.syslog_history.get('default', [])
    text, new_history = await handle_syslog_request(request, history)
    ctx.deps.syslog_history['default'] = new_history
    return text


@asynccontextmanager
async def main_lifespan():
    """Compose all sub-agent lifespans: MCP servers, topology refresh, Loki poller."""
    async with network_lifespan(), syslog_lifespan(), topology_lifespan():
        yield


async def main():
    logging.basicConfig(level=logging.INFO)

    async with main_lifespan():
        deps = OrchestratorDeps()
        message_history = []

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

            result = await orchestrator.run(user_text, deps=deps, message_history=message_history)
            message_history = result.all_messages()
            print(f'\nOrchestrator: {result.output}\n')


if __name__ == '__main__':
    asyncio.run(main())
