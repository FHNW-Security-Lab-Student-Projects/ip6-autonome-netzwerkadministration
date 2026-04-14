"""Syslog Incident Agent

Monitors Nokia SR Linux syslog via Loki. Automatically opens incidents for
error/critical/alert/emergency events and runs background LLM investigation.
Supports listing, inspecting, and continuing troubleshooting of incidents.

Run with: uv run uvicorn syslog_agent:app --port 8003
"""

import asyncio
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

import httpx
import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

from a2a_utils import BaseAgentExecutor, build_a2a_app, require_openrouter_key, setup_logfire

load_dotenv(Path(__file__).parent / '.env')
setup_logfire('Syslog Incident Agent')
OPENROUTER_API_KEY = require_openrouter_key()

knowledge_base_file = Path(__file__).parent / 'sr_linux_knowledge.txt'
SR_LINUX_KNOWLEDGE = ''
if knowledge_base_file.exists():
    SR_LINUX_KNOWLEDGE = knowledge_base_file.read_text()
else:
    print(f'Warning: sr_linux_knowledge.txt not found at {knowledge_base_file}')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOKI_URL = 'http://172.20.20.101:3100'
LOKI_QUERY = '{vendor="nokia_srlinux", severity=~"error|critical|alert|emergency"}'
LOKI_POLL_INTERVAL = 30  # seconds

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class IncidentStatus(str, Enum):
    investigating = 'investigating'
    waiting = 'waiting'    # auto-investigation done, awaiting user
    resolved = 'resolved'


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
_last_checked_ns: int = 0
_http_client: httpx.AsyncClient | None = None

# ---------------------------------------------------------------------------
# LLM + MCP agent
# ---------------------------------------------------------------------------

llm = OpenAIChatModel(
    'z-ai/glm-5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
    settings=ModelSettings(parallel_tool_calls=True),
)

mcp_server = MCPServerStdio(
    command='uv',
    args=['run', 'mcp_server.py'],
    tool_prefix='network_',
)

_KNOWLEDGE_SECTION = (
    f"\n{'-' * 80}\nNOKIA SR LINUX KNOWLEDGE BASE (for interpreting output):\n{'-' * 80}\n"
    f"{SR_LINUX_KNOWLEDGE}\n{'-' * 80}\nEND OF KNOWLEDGE BASE\n{'-' * 80}\n"
    if SR_LINUX_KNOWLEDGE else ''
)

syslog_investigator = Agent(
    model=llm,
    toolsets=[mcp_server],
    instructions=(
        'You are an automated network incident investigator for Nokia SR Linux devices.\n\n'
        'AVAILABLE TOOLS:\n'
        '- query_loki: Query Nokia SR Linux syslog from Loki around a specific point in time.\n'
        '              Use this to find related log entries on this device and neighboring devices.\n'
        '              Pass the triggering event timestamp (provided in the investigation prompt) as time_anchor.\n'
        '- network_execute_show_command: Run a show/info command on a specific device\n'
        '- network_get_device_info: Look up a device from the inventory\n'
        '- network_list_all_devices: List all devices in the inventory\n\n'
        'INVESTIGATION STRATEGY:\n'
        '1. Call query_loki for the triggering device first (±5 min around the event timestamp).\n'
        '2. Based on what you find, call query_loki on neighboring or related devices.\n'
        '3. Use network_execute_show_command to check current live device state.\n'
        '4. Summarise root cause and recommend next steps.\n\n'
        'NOTE: Device names in the inventory use short names (e.g. router1, switch1). '
        'The syslog host label uses the full container name (clab-testlab-router1). '
        'Strip the "clab-testlab-" prefix when calling tools.\n'
        + _KNOWLEDGE_SECTION
    ),
)


@syslog_investigator.tool_plain
async def query_loki(
    device: str,
    time_anchor: str,
    minutes_before: int = 5,
    minutes_after: int = 2,
    severities: str = 'error,critical,alert,emergency,warning,notice,informational',
    text_filter: str = '',
    limit: int = 100,
) -> str:
    """Query Nokia SR Linux syslog entries from Loki around a specific point in time.

    Args:
        device: Short device name ("router1", "switch1") or "all" for all devices.
                Do NOT include the "clab-testlab-" prefix.
        time_anchor: ISO 8601 datetime string marking the center of the time window.
                     Use the triggering event timestamp provided in the investigation prompt.
                     Example: "2026-04-14T14:30:00Z"
        minutes_before: How many minutes before time_anchor to include (default 5).
        minutes_after: How many minutes after time_anchor to include (default 2).
        severities: Comma-separated list of severity levels to include.
                    Available: emergency, alert, critical, error, warning, notice, informational, debug.
                    Default includes all except debug.
        text_filter: Optional substring — only return lines containing this string.
        limit: Maximum number of log lines to return (default 100, max 500).
    """
    if _http_client is None:
        return 'Loki client not available (server still starting up).'

    # Build LogQL stream selector
    severity_regex = '|'.join(s.strip() for s in severities.split(',') if s.strip())
    if device == 'all':
        stream = f'{{vendor="nokia_srlinux", severity=~"{severity_regex}"}}'
    else:
        host = f'clab-testlab-{device}'
        stream = f'{{vendor="nokia_srlinux", host="{host}", severity=~"{severity_regex}"}}'
    if text_filter:
        stream += f' |= "{text_filter}"'

    # Compute time range from anchor
    anchor_dt = datetime.fromisoformat(time_anchor.replace('Z', '+00:00'))
    start_ns = int((anchor_dt.timestamp() - minutes_before * 60) * 1e9)
    end_ns = int((anchor_dt.timestamp() + minutes_after * 60) * 1e9)
    limit = min(limit, 500)

    try:
        resp = await _http_client.get(
            f'{LOKI_URL}/loki/api/v1/query_range',
            params={
                'query': stream,
                'start': start_ns,
                'end': end_ns,
                'limit': limit,
                'direction': 'forward',
            },
        )
        resp.raise_for_status()
    except Exception as exc:
        return f'Loki query failed: {exc}'

    lines = []
    for stream_obj in resp.json()['data']['result']:
        host_label = stream_obj['stream'].get('host', 'unknown')
        sev_label = stream_obj['stream'].get('severity', '')
        app_label = stream_obj['stream'].get('app', '')
        for ts_str, line in stream_obj['values']:
            ts_dt = datetime.fromtimestamp(int(ts_str) / 1e9, tz=timezone.utc)
            ts_fmt = ts_dt.strftime('%H:%M:%S')
            lines.append(f'[{ts_fmt}] [{host_label}] [{sev_label}] [{app_label}] {line}')

    if not lines:
        return (
            f'No log entries found for query: {stream} '
            f'in window {minutes_before}m before / {minutes_after}m after {time_anchor}.'
        )
    return '\n'.join(lines)


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
    """Open a new incident for the event if not already investigating this device."""
    device = event['host']
    for inc in _incidents.values():
        if inc.device == device and inc.status == IncidentStatus.investigating:
            logfire.info('Skipping duplicate incident', device=device)
            return

    inc = Incident(
        incident_id=uuid4().hex,
        created_at=datetime.now(timezone.utc),
        device=device,
        triggering_event=event['line'],
        triggering_timestamp_ns=event['timestamp_ns'],
    )
    _incidents[inc.incident_id] = inc
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
        f"A syslog incident has been triggered on device '{inc.device}'.\n"
        f"Triggering event timestamp: {anchor_iso}\n"
        f"Triggering syslog event: {inc.triggering_event}\n\n"
        f"Please investigate this incident using the available tools.\n"
        f"Start by calling query_loki with device='{short_device}' and time_anchor='{anchor_iso}' "
        f"to see surrounding log context, then check neighboring devices and run show commands "
        f"to confirm the issue and identify the root cause."
    )
    try:
        with logfire.span('incident_investigation', incident_id=inc.incident_id, device=inc.device):
            result = await syslog_investigator.run(prompt, message_history=inc.message_history)
        inc.investigation_log.append(('auto_investigation', str(result.output)))
        inc.message_history = result.all_messages()
        inc.summary = str(result.output)
        inc.status = IncidentStatus.waiting
        logfire.info('Incident investigation complete', incident_id=inc.incident_id)
        print(f'[syslog-agent] Incident {inc.incident_id[:8]} investigation complete.', flush=True)
    except Exception as exc:
        inc.investigation_log.append(('error', str(exc)))
        inc.summary = f'Investigation failed: {exc}'
        inc.status = IncidentStatus.waiting
        logfire.error('Incident investigation failed', incident_id=inc.incident_id, error=str(exc))
        print(f'[syslog-agent] Incident {inc.incident_id[:8]} investigation failed: {exc}', flush=True)


# ---------------------------------------------------------------------------
# Intent detection helpers
# ---------------------------------------------------------------------------


def _intent_is_list(text: str) -> bool:
    return any(kw in text for kw in ('list', 'incidents', 'active', 'show all', 'what incident'))


def _intent_is_detail(text: str) -> bool:
    return any(kw in text for kw in ('detail', 'info', 'show incident', 'get incident'))


def _intent_is_continue(text: str) -> bool:
    return any(kw in text for kw in ('continue', 'join', 'investigate', 'update', 'follow up', 'follow-up'))


def _extract_id_fragment(text: str) -> str | None:
    """Return first token that looks like a hex ID fragment (6+ hex chars)."""
    m = re.search(r'\b([0-9a-f]{6,})\b', text)
    return m.group(1) if m else None


def _find_incident(fragment: str) -> Incident | None:
    for inc in _incidents.values():
        if inc.incident_id.startswith(fragment):
            return inc
    return None


def _extract_follow_up_text(text: str, fragment: str) -> str:
    """Return the text that comes after the ID fragment, stripped."""
    idx = text.find(fragment)
    if idx == -1:
        return ''
    return text[idx + len(fragment):].strip()


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
# A2A Executor
# ---------------------------------------------------------------------------


class SyslogAgentExecutor(BaseAgentExecutor):
    """Handles user requests about syslog incidents via intent-based dispatch."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = (context.get_user_input() or '').strip()
        user_lower = user_text.lower()

        with logfire.span('SyslogAgentExecutor.execute', user_text=user_text, task_id=context.task_id):
            updater = TaskUpdater(
                event_queue,
                task_id=context.task_id or uuid4().hex,
                context_id=context.context_id or uuid4().hex,
            )

            # --- List incidents ---
            if _intent_is_list(user_lower):
                await updater.complete(message=new_agent_text_message(_format_incident_list()))
                return

            # --- Details or continue: need an ID fragment ---
            fragment = _extract_id_fragment(user_lower)

            if _intent_is_detail(user_lower) and fragment:
                inc = _find_incident(fragment)
                if inc is None:
                    await updater.complete(message=new_agent_text_message(
                        f'No incident found matching "{fragment}".\n\n' + _format_incident_list()
                    ))
                    return
                await updater.complete(message=new_agent_text_message(_format_incident_detail(inc)))
                return

            if _intent_is_continue(user_lower) and fragment:
                inc = _find_incident(fragment)
                if inc is None:
                    await updater.complete(message=new_agent_text_message(
                        f'No incident found matching "{fragment}".\n\n' + _format_incident_list()
                    ))
                    return

                # Race condition guard: background task still running
                if inc.bg_task and not inc.bg_task.done():
                    await updater.complete(message=new_agent_text_message(
                        f'Incident `{inc.incident_id[:8]}` is still being investigated automatically.\n\n'
                        f'**Current findings so far:**\n{inc.summary or "(none yet)"}'
                    ))
                    return

                # Continue with stored message_history from background investigation
                follow_up = _extract_follow_up_text(user_lower, fragment) or (
                    'Please continue the investigation and provide updated findings.'
                )
                inc.status = IncidentStatus.investigating
                try:
                    with logfire.span('incident_user_continuation', incident_id=inc.incident_id):
                        result = await syslog_investigator.run(
                            follow_up, message_history=inc.message_history
                        )
                    inc.investigation_log.append(('user_continuation', str(result.output)))
                    inc.message_history = result.all_messages()
                    inc.summary = str(result.output)
                    inc.status = IncidentStatus.waiting
                    await updater.complete(message=new_agent_text_message(str(result.output)))
                except Exception as exc:
                    inc.status = IncidentStatus.waiting
                    logfire.error('User continuation failed', incident_id=inc.incident_id, error=str(exc))
                    await updater.failed(message=new_agent_text_message(f'Continuation failed: {exc}'))
                return

            # --- Fallback: ad-hoc investigation query via LLM ---
            ctx_key = context.context_id or context.task_id
            history = self._context_history.get(ctx_key, [])
            try:
                with logfire.span('incident_adhoc_query', user_text=user_text):
                    result = await syslog_investigator.run(user_text, message_history=history)
                self._context_history[ctx_key] = result.all_messages()
                await updater.complete(message=new_agent_text_message(str(result.output)))
            except Exception as exc:
                logfire.error('Ad-hoc query failed', error=str(exc))
                await updater.failed(message=new_agent_text_message(f'Query failed: {exc}'))


# ---------------------------------------------------------------------------
# Agent card + lifespan + app
# ---------------------------------------------------------------------------

agent_card = AgentCard(
    name='Syslog Incident Agent',
    description=(
        'Monitors Nokia SR Linux syslog via Loki. Automatically opens incidents for '
        'error/critical/alert/emergency events and runs LLM-driven investigation. '
        'Supports listing, inspecting, and continuing troubleshooting of incidents.'
    ),
    url='http://localhost:8003/',
    version='1.0.0',
    default_input_modes=['text'],
    default_output_modes=['text'],
    capabilities=AgentCapabilities(streaming=True, state_transition_history=True),
    skills=[AgentSkill(
        id='syslog_incidents',
        name='Syslog Incident Management',
        description=(
            'List active syslog incidents, get full investigation details, '
            'or continue LLM-driven troubleshooting for a specific incident.'
        ),
        tags=['syslog', 'incidents', 'network', 'nokia', 'monitoring'],
        examples=[
            'List all incidents',
            'Show details for incident abc12345',
            'Continue investigating incident abc12345 — what is the BGP status?',
        ],
    )],
)


@asynccontextmanager
async def lifespan(_):
    """Start the MCP subprocess and Loki poller alongside the A2A server."""
    global _http_client
    print('Starting syslog agent background tasks...')
    async with syslog_investigator:
        async with httpx.AsyncClient(timeout=30) as http_client:
            _http_client = http_client
            poll_task = asyncio.create_task(_loki_poll_loop(http_client))
            try:
                print('MCP server running. Syslog Incident Agent ready on port 8003.')
                yield
            finally:
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
                _http_client = None
    print('Syslog agent stopped.')


app = build_a2a_app(agent_card, SyslogAgentExecutor(), lifespan=lifespan)
