"""Syslog Incident Agent

Monitors Nokia SR Linux syslog via Loki. Automatically opens incidents for
error/critical/alert/emergency events and runs background LLM investigation.
Supports listing, inspecting, and continuing troubleshooting of incidents.

Import and use via agent delegation:
    from syslog_agent import handle_syslog_request, syslog_lifespan
"""

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

import os

import httpx
import logfire
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent, ModelMessagesTypeAdapter
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

load_dotenv(Path(__file__).parent / '.env')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')

class SyslogAgentResult(BaseModel):
    answer: str | None = None
    needs_clarification: bool = False
    clarifying_questions: list[str] = []



# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOKI_URL = 'http://172.20.20.101:3100'
LOKI_QUERY = '{job="network-syslog", severity=~"error|critical|alert|emergency"}'
LOKI_POLL_INTERVAL = 30  # seconds

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


INCIDENTS_FILE = Path(__file__).parent / 'incidents.json'


class IncidentStatus(str, Enum):
    investigating = 'investigating'
    waiting = 'waiting'    # auto-investigation done, awaiting user
    resolved = 'resolved'


class IncidentRecord(BaseModel):
    incident_id: str
    created_at: datetime
    device: str
    triggering_event: str
    triggering_timestamp_ns: int
    status: IncidentStatus
    investigation_log: list[tuple[str, str]]
    summary: str
    message_history: list = []


def _persist_incidents() -> None:
    records = {
        iid: IncidentRecord(
            incident_id=inc.incident_id,
            created_at=inc.created_at,
            device=inc.device,
            triggering_event=inc.triggering_event,
            triggering_timestamp_ns=inc.triggering_timestamp_ns,
            status=inc.status,
            investigation_log=inc.investigation_log,
            summary=inc.summary,
            message_history=ModelMessagesTypeAdapter.dump_python(
                inc.message_history, mode='json'
            ) if inc.message_history else [],
        ).model_dump(mode='json')
        for iid, inc in _incidents.items()
    }
    INCIDENTS_FILE.write_text(json.dumps(records, indent=2))


@dataclass
class Incident:
    incident_id: str
    created_at: datetime
    device: str                       # e.g. "clab-testlab-router1"
    triggering_event: str             # raw log line from Loki
    triggering_timestamp_ns: int = 0  # unix nanoseconds from Loki — anchor for query_loki calls
    status: IncidentStatus = IncidentStatus.investigating
    investigation_log: list[tuple[str, str]] = field(default_factory=list)
    message_history: list = field(default_factory=list)  # Pydantic AI native messages
    summary: str = ''
    bg_task: asyncio.Task | None = field(default=None, repr=False)


_incidents: dict[str, Incident] = {}
_incident_key_map: dict[tuple[str, str], str] = {}  # (device, normalized_msg) -> incident_id
_last_checked_ns: int = 0

_DYNAMIC_RE = re.compile(
    r'\b(?:\d{1,3}\.){3}\d{1,3}\b'   # IPv4 addresses
    r'|\b[0-9a-f]{8,}\b'              # hex IDs / MACs
    r'|\b\d+\b'                        # standalone numbers
    r'|T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?'  # ISO timestamps
)


def _normalize(msg: str) -> str:
    return _DYNAMIC_RE.sub('*', msg).strip()

# ---------------------------------------------------------------------------
# LLM + MCP agent
# ---------------------------------------------------------------------------

llm = OpenAIChatModel(
    'z-ai/glm-5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
    settings=ModelSettings(parallel_tool_calls=True),
)

network_mcp_server = MCPServerStdio(
    command='uv',
    args=['run', 'mcp_server.py'],
)

syslog_mcp_server = MCPServerStdio(
    command='uv',
    args=['run', 'syslog_mcp_server.py'],
)

INVESTIGATOR_INSTRUCTIONS_CONTINUATION = (
    'You are a network incident investigator for Nokia SR Linux devices performing a deep-dive '
    'continuation of a prior triage. The conversation history contains initial findings — '
    'do NOT repeat queries or commands already executed.\n\n'
    'INVESTIGATION STRATEGY:\n'
    '1. Review the prior findings in the conversation history.\n'
    '2. Expand log coverage: query_loki on neighboring/related devices and widen the time window if needed.\n'
    '3. Run execute_show_command to verify live device state for any suspected component '
    '(call get_command_reference first to verify correct SR Linux syntax).\n'
    '4. Correlate log evidence with live state to identify the root cause.\n'
    '5. Provide a definitive conclusion: confirmed root cause, affected scope, and concrete remediation steps.\n\n'
    'NOTE: Device names in the inventory use short names (e.g. router1, switch1). '
    'The syslog host label uses the full container name (clab-testlab-router1). '
    'Strip the "clab-testlab-" prefix when calling tools.\n'
)

INVESTIGATOR_INSTRUCTIONS_USER_INITIATED = (
    'You are a network incident investigator for Nokia SR Linux devices. '
    'A user has reported a problem — there is no prior triage context.\n\n'
    'INVESTIGATION STRATEGY:\n'
    '1. Infer the relevant device(s) and approximate timeframe from the user description. '
    'If the device is ambiguous, call list_all_devices and query the most likely candidates.\n'
    '2. Call query_loki for the relevant device(s) covering the suspected timeframe '
    '(default to the last 30 minutes if no timeframe is given).\n'
    '3. Run execute_show_command to verify current live device state for any suspected component '
    '(call get_command_reference first to verify correct SR Linux syntax).\n'
    '4. Correlate log evidence with live state to identify the root cause.\n'
    '5. Provide a definitive conclusion: confirmed root cause, affected scope, and concrete remediation steps.\n\n'
    'NOTE: Device names in the inventory use short names (e.g. router1, switch1). '
    'The syslog host label uses the full container name (clab-testlab-router1). '
    'Strip the "clab-testlab-" prefix when calling tools.\n'
)

INVESTIGATOR_INSTRUCTIONS_TRIAGE = (
    'You are an automated network incident triager for Nokia SR Linux devices. '
    'Your job is a QUICK initial assessment only — DO NOT try to do a full root-cause analysis.\n\n'
    'TRIAGE STRATEGY (just do a quick initial assessment there is a HARD STOP implemented after 90 seconds):\n'
    '1. Call query_loki once for the triggering device (±5 min around the event timestamp).\n'
    '2. Optionally run one execute_show_command if the log context clearly points to a live state check '
    '(call get_command_reference first to verify correct SR Linux syntax).\n'
    '3. Write a 2-3 sentence summary: what happened, likely cause, suggested next step.\n'
    'Do NOT query neighboring devices or run multiple show commands. '
    'A human can trigger a deeper investigation if needed.\n\n'
    'NOTE: Device names in the inventory use short names (e.g. router1, switch1). '
    'The syslog host label uses the full container name (clab-testlab-router1). '
    'Strip the "clab-testlab-" prefix when calling tools.\n'
)

syslog_investigator = Agent(
    model=llm,
    name='syslog_investigator',
    toolsets=[network_mcp_server, syslog_mcp_server],
    instructions=INVESTIGATOR_INSTRUCTIONS_TRIAGE,
)

syslog_deep_investigator = Agent(
    model=llm,
    name='syslog_deep_investigator',
    toolsets=[network_mcp_server, syslog_mcp_server],
)


# ---------------------------------------------------------------------------
# Loki polling
# ---------------------------------------------------------------------------


async def _poll_loki(client: httpx.AsyncClient) -> list[dict]:
    """Fetch new important syslog events from Loki since last check."""
    global _last_checked_ns
    now_ns = time.time_ns()
    start_ns = _last_checked_ns + 1 if _last_checked_ns else now_ns - 30 * 10**9
    _last_checked_ns = now_ns

    resp = await client.get(
        f'{LOKI_URL}/loki/api/v1/query_range',
        params={
            'query': LOKI_QUERY,
            'start': start_ns,
            'end': now_ns,
            'limit': 100,
            'direction': 'forward',
        },
    )
    resp.raise_for_status()
    events = []
    for stream_obj in resp.json()['data']['result']:
        host = stream_obj['stream'].get('host', 'unknown')
        severity = stream_obj['stream'].get('severity', '')
        for ts_str, line in stream_obj['values']:
            events.append({
                'timestamp_ns': int(ts_str),
                'host': host,
                'severity': severity,
                'line': line,
            })
    return events


def _maybe_open_incident(event: dict) -> None:
    """Open a new incident unless a non-resolved incident for the same device+message exists."""
    device = event['host']
    key = (device, _normalize(event['line']))

    existing_id = _incident_key_map.get(key)
    if existing_id is not None:
        existing = _incidents.get(existing_id)
        if existing is not None and existing.status != IncidentStatus.resolved:
            logfire.info('Skipping duplicate incident', device=device, incident_id=existing_id)
            return
        del _incident_key_map[key]

    inc = Incident(
        incident_id=uuid4().hex,
        created_at=datetime.now(timezone.utc),
        device=device,
        triggering_event=event['line'],
        triggering_timestamp_ns=event['timestamp_ns'],
    )
    _incidents[inc.incident_id] = inc
    _incident_key_map[key] = inc.incident_id
    _persist_incidents()
    logfire.info('Opening incident', incident_id=inc.incident_id, device=device, severity=event['severity'])
    print(
        f'[syslog-agent] New incident {inc.incident_id[:8]} on {device}: {event["line"][:80]}',
        flush=True,
    )
    inc.bg_task = asyncio.create_task(
        _run_troubleshooting(inc),
        name=f'troubleshoot-{inc.incident_id[:8]}',
    )


async def _loki_poll_loop(client: httpx.AsyncClient) -> None:
    """Background loop: poll Loki every LOKI_POLL_INTERVAL seconds for new events."""
    while True:
        try:
            events = await _poll_loki(client)
            for event in events:
                _maybe_open_incident(event)
        except Exception as exc:
            logfire.error('Loki poll failed', error=str(exc))
            print(f'[syslog-agent] Loki poll error: {exc}', flush=True)
        await asyncio.sleep(LOKI_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Background troubleshooting
# ---------------------------------------------------------------------------


async def _run_troubleshooting(inc: Incident) -> None:
    """LLM-driven investigation launched as a background asyncio task."""
    anchor_iso = datetime.fromtimestamp(
        inc.triggering_timestamp_ns / 1e9, tz=timezone.utc
    ).strftime('%Y-%m-%dT%H:%M:%SZ')
    short_device = inc.device.removeprefix('clab-testlab-')
    prompt = (
        f"Device: {inc.device} (short name: {short_device})\n"
        f"Event timestamp: {anchor_iso}\n"
        f"Syslog event: {inc.triggering_event}"
    )
    try:
        with logfire.span('incident_investigation', incident_id=inc.incident_id, device=inc.device):
            async with asyncio.timeout(90):
                result = await syslog_investigator.run(prompt, message_history=inc.message_history)
        inc.investigation_log.append(('auto_investigation', str(result.output)))
        inc.message_history = result.all_messages()
        inc.summary = str(result.output)
        inc.status = IncidentStatus.waiting
        _persist_incidents()
        logfire.info('Incident investigation complete', incident_id=inc.incident_id)
        print(f'[syslog-agent] Incident {inc.incident_id[:8]} investigation complete.', flush=True)
    except TimeoutError:
        inc.investigation_log.append(('error', 'Auto-investigation timed out after 90 s'))
        inc.summary = 'Triage timed out — trigger a manual continuation for deeper analysis.'
        inc.status = IncidentStatus.waiting
        _persist_incidents()
        logfire.warning('Incident investigation timed out', incident_id=inc.incident_id)
        print(f'[syslog-agent] Incident {inc.incident_id[:8]} triage timed out.', flush=True)
    except Exception as exc:
        inc.investigation_log.append(('error', str(exc)))
        inc.summary = f'Investigation failed: {exc}'
        inc.status = IncidentStatus.waiting
        _persist_incidents()
        logfire.error('Incident investigation failed', incident_id=inc.incident_id, error=str(exc))
        print(f'[syslog-agent] Incident {inc.incident_id[:8]} investigation failed: {exc}', flush=True)


# ---------------------------------------------------------------------------
# Incident lookup and formatting helpers
# ---------------------------------------------------------------------------


def _find_incident(fragment: str) -> Incident | None:
    for inc in _incidents.values():
        if inc.incident_id.startswith(fragment):
            return inc
    return None


def _format_incident_list() -> str:
    if not _incidents:
        return 'No incidents recorded yet.'
    lines = [
        '| ID (short) | Device | Status | Created | Summary |',
        '|---|---|---|---|---|',
    ]
    for inc in sorted(_incidents.values(), key=lambda i: i.created_at, reverse=True):
        ts = inc.created_at.strftime('%H:%M:%S UTC')
        summary_raw = inc.summary or '(investigating...)'
        summary = (summary_raw[:60] + '...') if len(summary_raw) > 60 else summary_raw
        lines.append(f'| {inc.incident_id[:8]} | {inc.device} | {inc.status.value} | {ts} | {summary} |')
    return '\n'.join(lines)


def _format_incident_detail(inc: Incident) -> str:
    lines = [
        f'## Incident {inc.incident_id[:8]}',
        f'**Device:** {inc.device}',
        f'**Status:** {inc.status.value}',
        f'**Created:** {inc.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")}',
        f'**Full ID:** `{inc.incident_id}`',
        '',
        '### Triggering Event',
        f'```\n{inc.triggering_event}\n```',
        '',
        '### Investigation Log',
    ]
    if inc.investigation_log:
        for step, result in inc.investigation_log:
            lines.append(f'**{step}:**')
            lines.append(result)
            lines.append('')
    else:
        lines.append('*(no steps recorded yet)*')
    lines.extend([
        '',
        '### Current Summary',
        inc.summary or '*(investigation in progress...)*',
    ])
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Syslog Dispatch Agent — LLM-driven user request handler
# ---------------------------------------------------------------------------

syslog_agent = Agent(
    model=llm,
    name='syslog_agent',
    toolsets=[network_mcp_server, syslog_mcp_server],
    output_type=SyslogAgentResult,
    instructions=(
        'You are a syslog incident management assistant for Nokia SR Linux network devices. '
        'Always use tools to answer — never guess incident IDs, statuses, or findings. '
        'When the user mentions a partial ID, pass it as-is to the relevant tool.\n\n'
        'OUTPUT FORMAT:\n'
        'Always respond with a SyslogAgentResult:\n'
        '- answer: your response or findings (null if needs_clarification is true)\n'
        '- needs_clarification: true if required information is missing to fulfil the request\n'
        '- clarifying_questions: specific questions to ask the user (empty if needs_clarification is false)'
    ),
)


@syslog_agent.tool_plain
def list_incidents() -> str:
    """List all known syslog incidents.

    Returns:
        Markdown table with columns: ID (short 8-char hex), Device, Status,
        Created (UTC time), and a truncated Summary. Returns a plain message
        if no incidents have been recorded yet.
    """
    return _format_incident_list()


@syslog_agent.tool_plain
def get_incident_detail(incident_id: str) -> str:
    """Get full details of a specific incident.

    Args:
        incident_id: Full incident ID or any unique hex prefix (e.g. "abc12345").
                     Call list_incidents first if you don't know the ID.

    Returns:
        Markdown-formatted incident report including device, status, triggering
        syslog event, full investigation log, and current summary.
        If no match is found, returns an error message followed by the incident list.
    """
    inc = _find_incident(incident_id)
    if inc is None:
        return f'No incident found matching "{incident_id}".\n\n' + _format_incident_list()
    return _format_incident_detail(inc)


@syslog_agent.tool_plain
async def continue_investigation(incident_id: str, follow_up: str = '') -> str:
    """Resume LLM-driven troubleshooting for an existing incident.

    Args:
        incident_id: Full incident ID or any unique hex prefix.
        follow_up: Optional instruction or question for the investigator
                   (e.g. "check neighboring devices" or "focus on BGP").
                   Defaults to a generic continue-and-summarise prompt.

    Returns:
        Updated investigation findings from the LLM investigator.
        If the background auto-investigation is still running, returns the
        current partial findings without launching a new run.
        Changes the incident status to 'investigating' while running,
        then back to 'waiting' when done.
    """
    inc = _find_incident(incident_id)
    if inc is None:
        return f'No incident found matching "{incident_id}".\n\n' + _format_incident_list()
    if inc.bg_task and not inc.bg_task.done():
        return (
            f'Incident `{inc.incident_id[:8]}` is still being investigated automatically.\n\n'
            f'**Current findings so far:**\n{inc.summary or "(none yet)"}'
        )
    follow_up_text = follow_up or 'Please continue the investigation and provide updated findings.'
    inc.status = IncidentStatus.investigating
    try:
        with logfire.span('incident_user_continuation', incident_id=inc.incident_id):
            result = await syslog_deep_investigator.run(
                follow_up_text,
                message_history=inc.message_history,
                instructions=INVESTIGATOR_INSTRUCTIONS_CONTINUATION,
            )
        inc.investigation_log.append(('user_continuation', str(result.output)))
        inc.message_history = result.all_messages()
        inc.summary = str(result.output)
        inc.status = IncidentStatus.waiting
        _persist_incidents()
        return str(result.output)
    except Exception as exc:
        inc.status = IncidentStatus.waiting
        _persist_incidents()
        logfire.error('User continuation failed', incident_id=inc.incident_id, error=str(exc))
        return f'Continuation failed: {exc}'


@syslog_agent.tool_plain
async def open_manual_incident(description: str, device: str = '') -> str:
    """Open a new incident from a user-reported problem and investigate it immediately.

    Use this when the user describes a problem rather than referencing an existing incident.
    The investigation runs to completion before returning results.

    Args:
        description: User's description of the problem (e.g. "BGP session to router2 keeps flapping").
        device: Optional device name hint (short name, e.g. "router1"). Leave empty if unknown
                or if multiple devices may be involved — the investigator will infer from context.

    Returns:
        Full investigation findings including root cause and remediation steps.
        Also creates a persistent incident record that can be listed and continued later.
    """
    prompt = description if not device else f'Device hint: {device}\n\nProblem: {description}'
    inc = Incident(
        incident_id=uuid4().hex,
        created_at=datetime.now(timezone.utc),
        device=device or 'user-reported',
        triggering_event=description,
        triggering_timestamp_ns=time.time_ns(),
        status=IncidentStatus.investigating,
    )
    _incidents[inc.incident_id] = inc
    _persist_incidents()
    logfire.info('Opening manual incident', incident_id=inc.incident_id, device=inc.device)
    try:
        with logfire.span('incident_manual_investigation', incident_id=inc.incident_id):
            result = await syslog_deep_investigator.run(
                prompt,
                instructions=INVESTIGATOR_INSTRUCTIONS_USER_INITIATED,
            )
        inc.investigation_log.append(('user_initiated', str(result.output)))
        inc.message_history = result.all_messages()
        inc.summary = str(result.output)
        inc.status = IncidentStatus.waiting
        _persist_incidents()
        return f'**Incident `{inc.incident_id[:8]}` opened.**\n\n{result.output}'
    except Exception as exc:
        inc.status = IncidentStatus.waiting
        _persist_incidents()
        logfire.error('Manual investigation failed', incident_id=inc.incident_id, error=str(exc))
        return f'Investigation failed: {exc}'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def handle_syslog_request(user_text: str) -> SyslogAgentResult:
    """Handle a user request about syslog incidents via the syslog_agent LLM."""
    with logfire.span('handle_syslog_request', user_text=user_text):
        result = await syslog_agent.run(user_text)
    return result.output


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def syslog_lifespan():
    """Start the MCP subprocess and Loki poller for the syslog agent."""
    print('Starting syslog agent background tasks...')
    async with syslog_investigator, syslog_deep_investigator:
        async with httpx.AsyncClient(timeout=30) as http_client:
            poll_task = asyncio.create_task(_loki_poll_loop(http_client))
            try:
                print('Syslog agent MCP server running. Loki poller active.')
                yield
            finally:
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
    print('Syslog agent stopped.')
