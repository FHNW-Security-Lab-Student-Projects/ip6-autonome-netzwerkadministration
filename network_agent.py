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
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from agent_history import compact_tool_history
from model_config import agent_model_settings


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

llm = OpenRouterModel(
    'z-ai/glm-5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
    settings=agent_model_settings(),
)

mcp_server = MCPServerStdio(
    command='uv',
    args=['run', 'mcp_server.py'],
    tool_prefix='network_',
    timeout=30,
)

# The SR Linux read-command reference cheat-sheet is appended to the system
# prompt so it is always in context, giving the model authoritative command syntax.
_CMDREF_PATH = Path(__file__).parent / 'srlinux-read-command-reference.txt'
_cmdref_block = (
    '\n\nSR LINUX READ-COMMAND REFERENCE (authoritative — use this exact syntax; '
    'do NOT invent commands or YANG paths from memory):\n'
    + _CMDREF_PATH.read_text()
)

network_agent = Agent(
    model=llm,
    name='network_agent',
    toolsets=[mcp_server],
    # Stub older oversized tool returns once the run nears the model's context
    # window so they aren't re-sent verbatim every loop (see agent_history.py).
    capabilities=[ProcessHistory(processor=compact_tool_history)],
    output_type=str,
    # output_type=NetworkAgentResult,  # disabled: tool_choice='required' not supported by all OpenRouter providers
    instructions=f"""You are a read-only network monitoring assistant for Nokia SR Linux devices.

TOOL SELECTION (two routes to device data — pick deliberately; see each tool's
description for syntax rules):

1. network_get_state_path / network_get_config_path
   - Preferred when you already know the YANG path you want; returns structured JSON,
     best for precise leaf/container reads.

2. network_execute_show_command
   - Use for formatted, consolidated operational views (`show interface brief`,
     `show network-instance default route-table`, `show version`, etc.) that have no
     clean YANG-path equivalent, or when a `ping` / `traceroute` is required.

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
