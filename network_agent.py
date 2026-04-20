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
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings


class NetworkAgentResult(BaseModel):
    answer: str | None = None
    needs_clarification: bool = False
    clarifying_questions: list[str] = []

load_dotenv(Path(__file__).parent / '.env')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')

llm = OpenAIChatModel(
    'z-ai/glm-5.1',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
    settings=ModelSettings(parallel_tool_calls=True),
)

mcp_server = MCPServerStdio(
    command='uv',
    args=['run', 'mcp_server.py'],
    tool_prefix='network_',
)

network_agent = Agent(
    model=llm,
    name='network_agent',
    toolsets=[mcp_server],
    output_type=NetworkAgentResult,
    instructions=f"""You are a read-only network monitoring assistant for Nokia SR Linux devices.

AVAILABLE TOOLS:
- network_execute_show_command: Run a show/info command on a specific device
- network_get_device_info: Look up a device's hostname and platform from the inventory
- network_list_all_devices: List all devices in the inventory

WORKFLOW:
- For device queries or show commands: use the appropriate tool and report the result clearly.
- For greetings or capability questions: respond directly without using tools.
- Always format output in a readable way (use lists or tables where appropriate).
- If the request is missing required information (e.g. which device to query), do NOT guess.
  Set needs_clarification=true and list the specific questions in clarifying_questions.

OUTPUT FORMAT:
Always respond with a NetworkAgentResult:
- answer: your response or findings (null if needs_clarification is true)
- needs_clarification: true if required information is missing
- clarifying_questions: specific questions to ask the user (empty if needs_clarification is false)

""",
)


@asynccontextmanager
async def network_lifespan():
    """Start the MCP server subprocess for the network agent."""
    print('Starting network agent MCP server...')
    async with network_agent:
        print('Network agent MCP server running.')
        yield
    print('Network agent MCP server stopped.')
