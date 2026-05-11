import asyncio
import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from config_agent import config_agent, config_lifespan
from network_agent import NetworkAgentResult, network_agent, network_lifespan
from topology_agent import get_topology_response, topology_lifespan
from state_snapshot_agent import snapshot_agent, snapshot_lifespan
from syslog_investigations import (
    syslog_lifespan,
    list_investigations,
    get_investigation_detail,
    open_manual_investigation,
    continue_investigation,
)

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
    'z-ai/glm-5.1',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
)


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
    '- Syslog Investigation Tools: Monitor Nokia SR Linux syslog via Loki. '
    'Investigations are opened automatically from Loki events or manually when the user reports a problem.\n'
    '  Tools:\n'
    '    - list_syslog_investigations: List all investigations (ID, device, status, summary).\n'
    '    - get_syslog_investigation(investigation_id): Full details of one investigation.\n'
    '    - open_syslog_investigation(description, device): Open a new persistent investigation '
    'written to disk. ONLY call this when the user\'s message starts with the exact prefix '
    '"/investigate". Strip the prefix and pass the remainder as the description. '
    'Never call this based on inferred intent alone.\n'
    '    - continue_syslog_investigation(investigation_id, follow_up, background): Resume '
    'LLM-driven troubleshooting. By default waits for the result (background=False). '
    'Set background=True only if the user explicitly asks to run it in the background.\n\n'
    'STATEFUL INVESTIGATIONS:\n'
    'open_syslog_investigation persists an investigation to disk. Only call it when the user\'s '
    'message starts with the exact prefix "/investigate". For all other syslog-related requests '
    '— questions, listing, status checks, continuations — use the read-only tools or answer '
    'conversationally. Never call open_syslog_investigation based on inferred intent alone.\n\n'
    '- call_snapshot_agent (Snapshot Agent): Historical device state captured every 2 minutes '
    '(routing table, ARP entries, interface status). Use for questions about how state evolved '
    'over time or what changed before an incident.\n'
    '  Skills:\n'
    '    - Retrieve device state at a specific point in time.\n'
    '    - Diff device state between two timestamps to identify what changed.\n'
    '    - Summarise snapshot coverage across all devices.\n\n'
    '- call_config_agent (Config Agent): Configure Nokia SR Linux devices.\n'
    '  ALWAYS follow this two-step workflow — never skip step 2:\n'
    '  Step 1: Call call_config_agent with "VALIDATE ONLY: <full request>"\n'
    '           → Returns a diff preview without applying anything.\n'
    '  Step 2: Show the diff to the user and ask for explicit approval.\n'
    '  Step 3: Only if the user says yes — call call_config_agent with\n'
    '           "APPLY (user approved): device=<name> commands=<exact commands from step 1>"\n'
    '  NEVER call call_config_agent with APPLY intent without prior user approval.\n'
    '  NEVER apply configuration changes based on inferred intent alone.\n\n'
    'Delegate requests to the appropriate sub-agent.\n\n'
    'When composing the `request` argument for any sub-agent call, write it as a '
    'self-contained message — the sub-agent has no access to the conversation history '
    'and depends entirely on what you include. Specifically:\n'
    '- Include all relevant context from the user\'s messages: their goal, preferences, '
    'constraints, or any details they mentioned that could help the sub-agent.\n'
    '- Include relevant results or outputs from other sub-agents called earlier in '
    'this conversation, if they inform the current task.'
)

orchestrator = Agent(llm, name='orchestrator', instructions=INSTRUCTIONS)


@orchestrator.tool_plain
async def call_network_agent(request: str) -> NetworkAgentResult:
    """Delegate a read-only network query to the Network Agent."""
    result = await network_agent.run(request)
    return result.output


@orchestrator.tool_plain
async def call_topology_agent() -> str:
    """Retrieve the cached network topology from the Topology Agent."""
    response = get_topology_response()
    if response is None:
        return 'Topology cache is still warming up — please retry in a moment.'
    return response


@orchestrator.tool_plain
async def sleep_test(seconds: int) -> str:
    """Sleep for the given number of seconds and return a confirmation. Used to test timeout behaviour."""
    await asyncio.sleep(seconds)
    return f'Slept {seconds}s successfully.'


@orchestrator.tool_plain
def list_syslog_investigations() -> str:
    """List all syslog investigations (ID, device, status, created, summary)."""
    return list_investigations()


@orchestrator.tool_plain
def get_syslog_investigation(investigation_id: str) -> str:
    """Get full details of a specific syslog investigation by ID or unique prefix."""
    return get_investigation_detail(investigation_id)


@orchestrator.tool_plain
async def open_syslog_investigation(description: str, device: str = '') -> str:
    """Open a new user-reported investigation and start LLM-driven analysis in the background.

    Use this when the user describes a new problem — not when they reference an existing investigation.

    Args:
        description: The user's problem description.
        device: Optional short device name hint (e.g. "router1"). Leave empty if unknown.
    """
    return await open_manual_investigation(description, device)


@orchestrator.tool_plain
async def continue_syslog_investigation(
    investigation_id: str,
    follow_up: str = '',
    background: bool = False,
) -> str:
    """Resume LLM-driven troubleshooting for an existing investigation.

    Args:
        investigation_id: Full ID or unique prefix of the investigation.
        follow_up: Optional focus instruction (e.g. "check neighboring devices").
        background: If True, start in the background and return immediately.
                    Only use when the user explicitly asks to run in the background.
    """
    return await continue_investigation(investigation_id, follow_up, synchronous=not background)


@orchestrator.tool_plain
async def call_config_agent(request: str) -> str:
    """Delegate a configuration request to the Config Agent.

    Prefix the request with "VALIDATE ONLY:" to preview changes (safe, no commit).
    Prefix with "APPLY (user approved):" to apply after the user has confirmed the diff.
    """
    result = await config_agent.run(request)
    return result.output


@orchestrator.tool_plain
async def call_snapshot_agent(request: str) -> str:
    """Delegate a historical state query to the Snapshot Agent.

    Use for questions about how device state evolved over time, e.g.:
    'how did the routing table on router1 change before the incident?'
    'what ARP entries were present on switch1 at 14:00?'
    """
    result = await snapshot_agent.run(request)
    return result.output


@asynccontextmanager
async def main_lifespan():
    """Compose all sub-agent lifespans: MCP servers, topology refresh, Loki poller."""
    async with network_lifespan(), syslog_lifespan(), topology_lifespan(), snapshot_lifespan(), config_lifespan():
        yield


async def main():
    logging.basicConfig(level=logging.INFO)

    async with main_lifespan():
        message_history = []

        print('Type your message and press Enter. Press Ctrl+C or type "exit" to quit.\n')

        while True:
            try:
                loop = asyncio.get_event_loop()
                user_text = (await loop.run_in_executor(None, input, 'You: ')).strip()
            except (KeyboardInterrupt, EOFError):
                print('\nGoodbye!')
                break

            if not user_text:
                continue
            if user_text.lower() in ('exit', 'quit', '/exit', '/quit'):
                print('Goodbye!')
                break

            result = await orchestrator.run(user_text, message_history=message_history)
            message_history = result.all_messages()
            print(f'\nOrchestrator: {result.output}\n')


if __name__ == '__main__':
    asyncio.run(main())
