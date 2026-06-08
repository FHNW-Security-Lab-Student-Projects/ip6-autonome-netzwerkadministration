"""Network Agent

Read-only network monitoring agent for Nokia SR Linux devices.
Uses MCP tools (execute_show_command, get_device_info, list_all_devices) via a
FastMCP stdio subprocess.

Import and use via agent delegation:
    from network_agent import network_agent, network_lifespan
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

from agent_history import compact_tool_history


# Structured output disabled — tool_choice='required' is not supported by all OpenRouter providers.
# Using plain str output instead so tool_choice='auto' is used, which has broader model support.
# class NetworkAgentResult(BaseModel):
#     answer: str | None = None
#     needs_clarification: bool = False
#     clarifying_questions: list[str] = []
class NetworkAgentResult(BaseModel):
    answer: str | None = None
    needs_clarification: bool = False
    clarifying_questions: list[str] = []

load_dotenv(Path(__file__).parent / '.env')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')

llm = OpenAIChatModel(
    'z-ai/glm-5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
    settings=ModelSettings(timeout=180),
)

mcp_server = MCPServerStdio(
    command='uv',
    args=['run', 'mcp_server.py'],
    tool_prefix='network_',
    timeout=30,
)

# A/B experiment toggle: when NETWORK_AGENT_CMDREF=1, the SR Linux read-command
# reference cheat-sheet is appended to the system prompt (always in context).
# Default (unset/0) = no reference, matching current production behaviour. Used to
# measure whether the reference reduces invalid-command (syntax) rates.
_CMDREF_PATH = Path(__file__).parent / 'srlinux-read-command-reference.txt'
_cmdref_block = ''
if os.getenv('NETWORK_AGENT_CMDREF') == '1':
    _cmdref_block = (
        '\n\nSR LINUX READ-COMMAND REFERENCE (authoritative — use this exact syntax; '
        'do NOT invent commands or YANG paths from memory):\n'
        + _CMDREF_PATH.read_text()
    )

network_agent = Agent(
    model=llm,
    name='network_agent',
    toolsets=[mcp_server],
    output_type=str,
    # output_type=NetworkAgentResult,  # disabled: tool_choice='required' not supported by all OpenRouter providers
    # Collapse older oversized tool returns so the show/get payloads this agent
    # accumulates don't get re-sent verbatim every loop (see agent_history.py).
    capabilities=[ProcessHistory(processor=compact_tool_history)],
    instructions=f"""You are a read-only network monitoring assistant for Nokia SR Linux devices.

TOOL SELECTION (two routes to device data — pick deliberately):

1. network_get_state_path(device, path)  / network_get_config_path(device, path)
   - Preferred when you know the YANG path you want.
   - Accepts native YANG path notation, INCLUDING `[name=<value>]` list keys
     and slash-joined segments. No CLI parser quirks apply here.
   - Returns structured JSON. Best for precise leaf/container reads.

2. network_execute_show_command(device, command)
   - Use it when you want a formatted operational and consolidated view (`show interface brief`,
     `show network-instance default route-table`, `show version`, etc.) that has
     no clean YANG-path equivalent, or when a `ping` / `traceroute` is required.
   - The CLI parser does NOT accept `[name=<value>]` bracket syntax or
     slash-joined YANG paths — use get_*_path for those instead.
   - For ping, ALWAYS bound it with `-c <N>` or the RPC will time out.

WORKFLOW:
- If the request is missing required information (e.g. which device to query),
  do NOT guess. Say so explicitly and list the specific questions you need
  answered.
- IF the request is clear use the provided tools to fulfill it.
{_cmdref_block}""",
)


@asynccontextmanager
async def network_lifespan():
    """Start the MCP server subprocess for the network agent."""
    print('Starting network agent MCP server...')
    async with network_agent:
        print('Network agent MCP server running.')
        yield
    print('Network agent MCP server stopped.')
