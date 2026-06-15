"""Investigation Orchestrator

A mini-orchestrator dedicated to autonomous network troubleshooting. It runs the
same delegation pattern as the user-facing orchestrator in client_agent.py, but
with a curated, READ-ONLY roster of sub-agents:

    - call_network_agent   -> network_agent (live show commands / device state)
    - call_snapshot_agent  -> snapshot_agent (historical state, what changed)
    - call_topology_agent  -> cached LLDP topology
    - query_loki           -> direct syslog MCP tool (no agent wrapper exists)

It deliberately EXCLUDES config_agent (write access) and the open/continue
investigation tools (recursion risk) — those stay on the user orchestrator.

Used via syslog_investigations.py, which feeds it auto-triage, user-initiated,
and continuation runs by passing mode-specific instructions= at run time. Those
per-run instructions COMBINE with the base INSTRUCTIONS preamble below.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import APITimeoutError
from pydantic_ai import Agent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from agent_history import compact_tool_history
from model_config import agent_model_settings
from network_agent import network_agent
from state_snapshot_agent import snapshot_agent
from topology_agent import get_topology_response


load_dotenv(Path(__file__).parent / '.env')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')

llm = OpenRouterModel(
    'z-ai/glm-5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
    settings=agent_model_settings(parallel_tool_calls=True),
)

# This orchestrator owns direct Loki access (query_loki is a single MCP tool — no
# agent wrapper exists for it). Network/snapshot/topology go through delegation tools.
syslog_mcp_server = MCPServerStdio(
    command='uv',
    args=['run', 'syslog_mcp_server.py'],
    timeout=30,
)


INSTRUCTIONS = (
    'You are an investigation orchestrator for Nokia SR Linux devices. You coordinate '
    'a set of read-only sub-agents and a direct log-query tool (query_loki) to troubleshoot '
    'network problems. The specific investigation task (triage / user-reported / continuation) '
    'is provided in the run-specific instructions that follow.\n\n'
    'Each tool\'s description explains when and how to use it. The read-only capabilities are '
    'safe to call in parallel when the lookups are independent.\n'
)

investigation_orchestrator = Agent(
    model=llm,
    name='investigation_orchestrator',
    toolsets=[syslog_mcp_server],
    # Stub older oversized tool returns once the run nears the model's context
    # window so they aren't re-sent verbatim each loop (see agent_history.py).
    capabilities=[ProcessHistory(processor=compact_tool_history)],
    instructions=INSTRUCTIONS,
)


@investigation_orchestrator.tool_plain
async def call_network_agent(request: str) -> str:
    """Delegate a read-only LIVE device-state query to the Network Agent.

    Use for show commands, live interface/routing/BGP state, and device inventory.
    The sub-agent has no access to this conversation, so write a self-contained
    request: include the device, the symptom, and any relevant findings so far.
    """
    try:
        result = await network_agent.run(request)
        return result.output
    except APITimeoutError:
        return 'The network agent timed out after 3 minutes.'


@investigation_orchestrator.tool_plain
async def call_snapshot_agent(request: str) -> str:
    """Delegate a HISTORICAL state query to the Snapshot Agent.

    Use for how device state evolved over time, e.g. 'how did router1's routing
    table change before the incident?' or 'what ARP entries existed at 14:00?'.
    State (routing table, ARP, interface status) is snapshotted every 2 minutes.
    The sub-agent has no access to this conversation, so write a self-contained
    request: include the device, the timeframe, and the symptom.
    """
    try:
        result = await snapshot_agent.run(request)
        return result.output
    except APITimeoutError:
        return 'The snapshot agent timed out after 3 minutes.'


@investigation_orchestrator.tool_plain
async def call_topology_agent() -> str:
    """Retrieve the cached network topology: nodes, links, LLDP neighbors, and
    drift vs. the desired ContainerLab definition. Takes no arguments."""
    response = get_topology_response()
    if response is None:
        return 'Topology cache is still warming up — please retry in a moment.'
    return response
