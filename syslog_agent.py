"""Syslog Agent

Read-only syslog analysis agent for Nokia SR Linux devices. Queries syslog from
Loki via MCP tools (query_loki) provided by syslog_mcp_server.py, and produces a
concise analysis of what the logs show.

Import and use via agent delegation (the MCP subprocess lifecycle is managed by
syslog_investigations.syslog_lifespan, which enters the agent's context):
    from syslog_agent import syslog_agent
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from agent_history import compact_tool_history
from model_config import agent_model_settings


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
    args=['run', 'syslog_mcp_server.py'],
    env=dict(os.environ),
    tool_prefix='syslog_',
    timeout=30,
)

syslog_agent = Agent(
    model=llm,
    name='syslog_agent',
    toolsets=[mcp_server],
    # Stub older oversized tool returns once the run nears the model's context
    # window so they aren't re-sent verbatim every loop (see agent_history.py).
    capabilities=[ProcessHistory(processor=compact_tool_history)],
    output_type=str,
    instructions="""You are a read-only syslog analysis assistant for Nokia SR Linux devices.
You answer questions about device syslog by querying Loki (the syslog_query_loki tool)
and interpreting the results. See the tool description for its arguments.

CHOOSING THE TIME WINDOW:
- If the request carries a SPECIFIC incident/triggering timestamp (e.g. an
  investigation prompt), pass that ISO timestamp as time_anchor and use a tight window.
- If the request is an ad-hoc "current" / "recent" question with NO specific time,
  pass time_anchor="now" and WIDEN minutes_before (e.g. 30-60) so recent history is
  covered, keeping minutes_after small.
- For "N minutes/hours ago" pass a relative offset (e.g. time_anchor="20m"). Never
  compute an absolute timestamp yourself — the tool resolves "now" at query time.

WORKFLOW:
- When the user only cares about problems, narrow severities to
  "error,critical,alert,emergency" rather than querying all levels.
- If the request is missing required information (e.g. which device), do NOT guess —
  say so explicitly and list the specific questions you need answered.
- Otherwise query Loki, then return a CONCISE ANALYSIS of what the logs show
  (notable events, severities, timing, affected device/interface) — not a raw dump
  of every line. Quote the few most relevant lines as evidence.""",
)
