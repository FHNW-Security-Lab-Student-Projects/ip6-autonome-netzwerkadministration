"""Syslog Incident Agent

Monitors Nokia SR Linux syslog via Loki. Automatically opens incidents for
error/critical/alert/emergency events and runs background LLM investigation.
Supports listing, inspecting, and continuing troubleshooting of incidents.

Import and use via agent delegation:
    from syslog_agent import handle_syslog_request, syslog_lifespan
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

import os

import httpx
import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

load_dotenv(Path(__file__).parent / '.env')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')

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
    args=['run', 'syslog_mcp_server.py'],
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
        '- execute_show_command: Run a show/info command on a specific device\n'
        '- get_device_info: Look up a device from the inventory\n'
        '- list_all_devices: List all devices in the inventory\n\n'
        'INVESTIGATION STRATEGY:\n'
        '1. Call query_loki for the triggering device first (±5 min around the event timestamp).\n'
        '2. Based on what you find, call query_loki on neighboring or related devices.\n'
        '3. Use execute_show_command to check current live device state.\n'
        '4. Summarise root cause and recommend next steps.\n\n'
        'NOTE: Device names in the inventory use short names (e.g. router1, switch1). '
        'The syslog host label uses the full container name (clab-testlab-router1). '
        'Strip the "clab-testlab-" prefix when calling tools.\n'
        + _KNOWLEDGE_SECTION
    ),
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
# Public API
# ---------------------------------------------------------------------------

async def handle_syslog_request(
    user_text: str,
    ctx_history: list,
) -> tuple[str, list]:
    """Handle a user request about syslog incidents via intent-based dispatch.

    Args:
        user_text: The user's message.
        ctx_history: Pydantic AI message history for ad-hoc LLM queries
                     (pass [] for a new session; updated list is returned).

    Returns:
        (response_text, updated_ctx_history)
    """
    user_lower = user_text.lower()

    with logfire.span('handle_syslog_request', user_text=user_text):
        # --- List incidents ---
        if _intent_is_list(user_lower):
            return _format_incident_list(), ctx_history

        # --- Details or continue: need an ID fragment ---
        fragment = _extract_id_fragment(user_lower)

        if _intent_is_detail(user_lower) and fragment:
            inc = _find_incident(fragment)
            if inc is None:
                return (
                    f'No incident found matching "{fragment}".\n\n' + _format_incident_list(),
                    ctx_history,
                )
            return _format_incident_detail(inc), ctx_history

        if _intent_is_continue(user_lower) and fragment:
            inc = _find_incident(fragment)
            if inc is None:
                return (
                    f'No incident found matching "{fragment}".\n\n' + _format_incident_list(),
                    ctx_history,
                )

            # Race condition guard: background task still running
            if inc.bg_task and not inc.bg_task.done():
                return (
                    f'Incident `{inc.incident_id[:8]}` is still being investigated automatically.\n\n'
                    f'**Current findings so far:**\n{inc.summary or "(none yet)"}',
                    ctx_history,
                )

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
                return str(result.output), ctx_history
            except Exception as exc:
                inc.status = IncidentStatus.waiting
                logfire.error('User continuation failed', incident_id=inc.incident_id, error=str(exc))
                return f'Continuation failed: {exc}', ctx_history

        # --- Fallback: ad-hoc investigation query via LLM ---
        try:
            with logfire.span('incident_adhoc_query', user_text=user_text):
                result = await syslog_investigator.run(user_text, message_history=ctx_history)
            return str(result.output), result.all_messages()
        except Exception as exc:
            logfire.error('Ad-hoc query failed', error=str(exc))
            return f'Query failed: {exc}', ctx_history


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def syslog_lifespan():
    """Start the MCP subprocess and Loki poller for the syslog agent."""
    print('Starting syslog agent background tasks...')
    async with syslog_investigator:
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
