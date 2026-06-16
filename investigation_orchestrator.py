"""Investigation Orchestrator

A mini-orchestrator dedicated to autonomous network troubleshooting. It runs the
same delegation pattern as the user-facing orchestrator in client_agent.py, but
with a curated, READ-ONLY roster of sub-agents:

    - call_network_agent   -> network_agent (live show commands / device state)
    - call_snapshot_agent  -> snapshot_agent (historical state, what changed)
    - call_topology_agent  -> cached LLDP topology
    - call_syslog_agent    -> syslog_agent (Loki syslog query + analysis)

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
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from agent_history import compact_tool_history
from model_config import agent_model_settings
from network_agent import network_agent
from state_snapshot_agent import snapshot_agent
from syslog_agent import syslog_agent
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

INSTRUCTIONS = (
    'You are an investigation orchestrator for Nokia SR Linux devices. You coordinate '
    'a set of read-only sub-agents to troubleshoot network problems. The specific investigation task (triage / user-reported '
    '/ continuation) is provided in the run-specific instructions that follow.\n\n'
    'Each tool\'s description explains when and how to use it. The read-only capabilities are '
    'safe to call in parallel when the lookups are independent.\n'
)

investigation_orchestrator = Agent(
    model=llm,
    name='investigation_orchestrator',
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
    """This agent provides historical "runtime" information which are not present in the syslog or currently visible on the device. The agent does snapshots of device state captured every 2 minutes, covering
    ARP entries, interface status, and per-network-instance routing tables.
    Describe any time window in RELATIVE terms ("last 10 minutes", "around now"), or
    pass through a specific incident timestamp if known — the sub-agent resolves the
    actual time. Do not compute absolute timestamps yourself.
    """
    try:
        result = await snapshot_agent.run(request)
        return result.output
    except APITimeoutError:
        return 'The snapshot agent timed out after 3 minutes.'


@investigation_orchestrator.tool_plain
async def call_syslog_agent(request: str) -> str:
    """Delegate to the syslog agent for information from the syslog. This agent has access to the central syslog collection for the whole network.

    Use to inspect Nokia SR Linux syslog from Loki around the incident, e.g. what
    events fired on a device in a time window. Include the device and the symptom. For
    the time window, pass through the triggering-event timestamp (from the
    investigation prompt) when one is given, otherwise describe it relatively ("last 30
    minutes", "around now") — the sub-agent resolves the actual time. Do not compute
    absolute timestamps yourself.
    """
    try:
        result = await syslog_agent.run(request)
        return result.output
    except APITimeoutError:
        return 'The syslog agent timed out after 3 minutes.'


@investigation_orchestrator.tool_plain
async def call_topology_agent() -> str:
    """Retrieve the cached network topology: nodes, links, LLDP neighbors, and
    drift vs. the desired ContainerLab definition. Takes no arguments."""
    response = get_topology_response()
    if response is None:
        return 'Topology cache is still warming up — please retry in a moment.'
    return response
