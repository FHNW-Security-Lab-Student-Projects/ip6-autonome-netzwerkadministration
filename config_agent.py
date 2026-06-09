"""Network Configuration Agent

Configures Nokia SR Linux devices via candidate mode + commit.
Backed by config_mcp_server.py (FastMCP stdio subprocess).

TWO-STEP APPROVAL WORKFLOW (enforced by orchestrator instructions):
  Step 1 — Orchestrator calls with "VALIDATE ONLY: <request>"
            → Agent calls validate_config, returns the diff preview.
  Step 2 — User reviews the diff and approves.
  Step 3 — Orchestrator calls with "APPLY (user approved): <device> <commands>"
            → Agent calls apply_config (commit).

Import and use via agent delegation:
    from config_agent import config_agent, config_lifespan
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from model_config import agent_model_settings

load_dotenv(Path(__file__).parent / '.env')

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')

llm = OpenRouterModel(
    'z-ai/glm-5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
    settings=agent_model_settings(parallel_tool_calls=True),  # Sequential — safety for config changes
)

config_mcp = MCPServerStdio(
    command='uv',
    args=['run', 'config_mcp_server.py'],
    tool_prefix='config_',
    timeout=30,
)

config_agent = Agent(
    model=llm,
    name='config_agent',
    toolsets=[config_mcp],
    output_type=str,
    instructions="""You are a Nokia SR Linux network configuration agent.

CRITICAL RULES:
1. Use ONLY Nokia SR Linux CLI syntax. NEVER use Cisco IOS, Juniper, or Arista syntax.
2. ALWAYS call config_get_command_reference before constructing configuration commands
   to verify the correct SR Linux path and syntax.
3. config_commands lists must contain ONLY 'set ...' commands.
   NEVER include: 'enter candidate', 'commit now', 'discard now' — the MCP server
   handles these automatically.
4. Tools run sequentially (parallel_tool_calls=False) for safety.

WORKFLOW — determined by the request prefix:

► If the request starts with "VALIDATE ONLY:":
  1. Call config_list_all_devices if you need to resolve the device name.
  2. Call config_get_command_reference to verify correct SR Linux syntax.
  3. Construct the config_commands list (only 'set ...' commands).
  4. Call config_validate_config(device_name, config_commands).
  5. Return the full diff output clearly. Do NOT call config_apply_config.
  6. End your response with the exact list of commands that would be applied,
     formatted so the orchestrator can pass them back in an APPLY call.

► If the request starts with "APPLY (user approved):":
  1. Extract the device name and exact config_commands from the request.
  2. Call config_apply_config(device_name, config_commands).
  3. Report: commands applied, diff, commit result.
  4. If commit fails, report the full error and do NOT retry automatically.

► If the request has no prefix or is ambiguous:
  Treat as VALIDATE ONLY — never apply without an explicit 'user approved' signal.

SR LINUX CONFIGURATION REMINDERS:
- All paths start with /: set / interface ethernet-1/1 admin-state enable
- Descriptions use quotes: set / interface ethernet-1/1 description "uplink"
- Subinterfaces: set / interface ethernet-1/1 subinterface 0 ipv4 address 10.0.0.1/30
- If a subinterface uses vlan encap, the parent must have vlan-tagging enabled
- Network instance binding: set / network-instance default interface ethernet-1/1.0
- BGP neighbor: set / network-instance default protocols bgp neighbor 10.0.0.2 ...
""",
)


@asynccontextmanager
async def config_lifespan():
    """Start the config MCP server subprocess."""
    print('Starting config agent MCP server...')
    async with config_agent:
        print('Config agent MCP server running.')
        yield
    print('Config agent MCP server stopped.')
