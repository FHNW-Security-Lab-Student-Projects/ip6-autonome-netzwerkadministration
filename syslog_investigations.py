"""Syslog Investigation Agent

Monitors Nokia SR Linux syslog via Loki. Automatically opens investigations for
error/critical/alert/emergency events and runs background LLM investigation.
Supports listing, inspecting, and continuing troubleshooting of investigations.

Import and use via agent delegation:
    from syslog_investigations import syslog_lifespan, list_investigations, get_investigation_detail
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
import uvicorn
from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent, ModelMessagesTypeAdapter
from pydantic_ai.mcp import MCPServerStdio
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

load_dotenv(Path(__file__).parent / '.env')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOKI_URL = 'http://172.20.20.101:3100'
LOKI_QUERY = '{job="network-syslog", severity=~"error|critical|alert|emergency"}'
LOKI_POLL_INTERVAL = 30  # seconds

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


INVESTIGATIONS_FILE = Path(__file__).parent / 'investigations.json'
INVESTIGATION_STATUS_URL = f"http://127.0.0.1:{os.getenv('INVESTIGATION_STATUS_PORT', '7933')}"
AGENT_API_PORT = int(os.getenv('AGENT_API_PORT', '7934'))


class InvestigationStatus(str, Enum):
    investigating = 'investigating'
    waiting = 'waiting'    # auto-investigation done, awaiting user
    resolved = 'resolved'


class InvestigationRecord(BaseModel):
    investigation_id: str
    created_at: datetime
    device: str
    triggering_event: str
    triggering_timestamp_ns: int
    status: InvestigationStatus
    investigation_log: list[tuple[str, str]]
    summary: str
    message_history: list = []


def _persist_investigations() -> None:
    records = {
        iid: InvestigationRecord(
            investigation_id=inv.investigation_id,
            created_at=inv.created_at,
            device=inv.device,
            triggering_event=inv.triggering_event,
            triggering_timestamp_ns=inv.triggering_timestamp_ns,
            status=inv.status,
            investigation_log=inv.investigation_log,
            summary=inv.summary,
            message_history=ModelMessagesTypeAdapter.dump_python(
                inv.message_history, mode='json'
            ) if inv.message_history else [],
        ).model_dump(mode='json')
        for iid, inv in _investigations.items()
    }
    INVESTIGATIONS_FILE.write_text(json.dumps(records, indent=2))


@dataclass
class Investigation:
    investigation_id: str
    created_at: datetime
    device: str                       # e.g. "clab-testlab-router1"
    triggering_event: str             # raw log line from Loki
    triggering_timestamp_ns: int = 0  # unix nanoseconds from Loki — anchor for query_loki calls
    status: InvestigationStatus = InvestigationStatus.investigating
    investigation_log: list[tuple[str, str]] = field(default_factory=list)
    message_history: list = field(default_factory=list)  # Pydantic AI native messages
    summary: str = ''
    bg_task: asyncio.Task | None = field(default=None, repr=False)


_investigations: dict[str, Investigation] = {}
_investigation_key_map: dict[tuple[str, str], str] = {}  # (device, normalized_msg) -> investigation_id
_last_checked_ns: int = 0

_DYNAMIC_RE = re.compile(
    r'\b(?:\d{1,3}\.){3}\d{1,3}\b'   # IPv4 addresses
    r'|\b[0-9a-f]{8,}\b'              # hex IDs / MACs
    r'|\b\d+\b'                        # standalone numbers
    r'|T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?'  # ISO timestamps
)


def _normalize(msg: str) -> str:
    return _DYNAMIC_RE.sub('*', msg).strip()


def _load_investigations() -> None:
    if not INVESTIGATIONS_FILE.exists():
        return
    try:
        raw = json.loads(INVESTIGATIONS_FILE.read_text())
    except Exception as exc:
        logfire.warning('Failed to load investigations from disk', error=str(exc))
        return
    for iid, data in raw.items():
        try:
            record = InvestigationRecord.model_validate(data)
            inv = Investigation(
                investigation_id=record.investigation_id,
                created_at=record.created_at,
                device=record.device,
                triggering_event=record.triggering_event,
                triggering_timestamp_ns=record.triggering_timestamp_ns,
                status=record.status,
                investigation_log=record.investigation_log,
                summary=record.summary,
                message_history=ModelMessagesTypeAdapter.validate_python(record.message_history) if record.message_history else [],
            )
            _investigations[iid] = inv
            _investigation_key_map[(inv.device, _normalize(inv.triggering_event))] = iid
        except Exception as exc:
            logfire.warning('Skipping corrupt investigation record', investigation_id=iid, error=str(exc))


_load_investigations()

# ---------------------------------------------------------------------------
# LLM + MCP agent
# ---------------------------------------------------------------------------

llm = OpenAIChatModel(
    'z-ai/glm-5.1',
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
    'You are a network investigator for Nokia SR Linux devices performing a further '
    'analysis of a prior triage. The conversation history contains initial findings — '
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
    'You are a network investigator for Nokia SR Linux devices. '
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
    'You are an automated network triager for Nokia SR Linux devices. '
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


_ENV_FILE = Path(__file__).parent / '.env'


def _maybe_open_investigation(event: dict) -> None:
    """Open a new investigation unless a non-resolved investigation for the same device+message exists."""
    if dotenv_values(_ENV_FILE).get('AUTO_INVESTIGATIONS_ENABLED', 'true').lower() == 'false':
        logfire.info('Auto-investigation creation disabled via .env — skipping event')
        return
    device = event['host']
    key = (device, _normalize(event['line']))

    existing_id = _investigation_key_map.get(key)
    if existing_id is not None:
        existing = _investigations.get(existing_id)
        if existing is not None and existing.status != InvestigationStatus.resolved:
            logfire.info('Skipping duplicate investigation', device=device, investigation_id=existing_id)
            return
        del _investigation_key_map[key]

    inv = Investigation(
        investigation_id=uuid4().hex,
        created_at=datetime.now(timezone.utc),
        device=device,
        triggering_event=event['line'],
        triggering_timestamp_ns=event['timestamp_ns'],
    )
    _investigations[inv.investigation_id] = inv
    _investigation_key_map[key] = inv.investigation_id
    _persist_investigations()
    logfire.info('Opening investigation', investigation_id=inv.investigation_id, device=device, severity=event['severity'])
    print(
        f'[syslog-agent] New investigation {inv.investigation_id[:8]} on {device}: {event["line"][:80]}',
        flush=True,
    )
    inv.bg_task = asyncio.create_task(
        _run_troubleshooting(inv),
        name=f'troubleshoot-{inv.investigation_id[:8]}',
    )


async def _loki_poll_loop(client: httpx.AsyncClient) -> None:
    """Background loop: poll Loki every LOKI_POLL_INTERVAL seconds for new events."""
    while True:
        try:
            events = await _poll_loki(client)
            for event in events:
                _maybe_open_investigation(event)
        except Exception as exc:
            logfire.error('Loki poll failed', error=str(exc))
            print(f'[syslog-agent] Loki poll error: {exc}', flush=True)
        await asyncio.sleep(LOKI_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Background troubleshooting
# ---------------------------------------------------------------------------


async def _run_troubleshooting(inv: Investigation) -> None:
    """LLM-driven investigation launched as a background asyncio task."""
    anchor_iso = datetime.fromtimestamp(
        inv.triggering_timestamp_ns / 1e9, tz=timezone.utc
    ).strftime('%Y-%m-%dT%H:%M:%SZ')
    short_device = inv.device.removeprefix('clab-testlab-')
    prompt = (
        f"Device: {inv.device} (short name: {short_device})\n"
        f"Event timestamp: {anchor_iso}\n"
        f"Syslog event: {inv.triggering_event}"
    )
    try:
        with logfire.span('investigation', investigation_id=inv.investigation_id, device=inv.device):
            async with asyncio.timeout(90):
                result = await syslog_investigator.run(prompt, message_history=inv.message_history, instructions=INVESTIGATOR_INSTRUCTIONS_TRIAGE)
        inv.investigation_log.append(('auto_investigation', str(result.output)))
        inv.message_history = result.all_messages()
        inv.summary = str(result.output)
        inv.status = InvestigationStatus.waiting
        _persist_investigations()
        logfire.info('Investigation complete', investigation_id=inv.investigation_id)
        print(f'[syslog-agent] Investigation {inv.investigation_id[:8]} complete.', flush=True)
    except TimeoutError:
        inv.investigation_log.append(('error', 'Auto-investigation timed out after 90 s'))
        inv.summary = 'Triage timed out — trigger a manual continuation for deeper analysis.'
        inv.status = InvestigationStatus.waiting
        _persist_investigations()
        logfire.warning('Investigation timed out', investigation_id=inv.investigation_id)
        print(f'[syslog-agent] Investigation {inv.investigation_id[:8]} triage timed out.', flush=True)
    except Exception as exc:
        inv.investigation_log.append(('error', str(exc)))
        inv.summary = f'Investigation failed: {exc}'
        inv.status = InvestigationStatus.waiting
        _persist_investigations()
        logfire.error('Investigation failed', investigation_id=inv.investigation_id, error=str(exc))
        print(f'[syslog-agent] Investigation {inv.investigation_id[:8]} failed: {exc}', flush=True)


# ---------------------------------------------------------------------------
# Investigation lookup and formatting helpers
# ---------------------------------------------------------------------------


def _find_investigation(fragment: str) -> Investigation | None:
    for inv in _investigations.values():
        if inv.investigation_id.startswith(fragment):
            return inv
    return None


async def _api_resolve_investigation(request: Request) -> JSONResponse:
    inv_id = request.path_params['investigation_id']
    inv = _find_investigation(inv_id)
    if inv is None:
        return JSONResponse({'error': f'No investigation matching "{inv_id}"'}, status_code=404)
    if inv.bg_task and not inv.bg_task.done():
        return JSONResponse({'error': 'Investigation is still running'}, status_code=409)
    inv.status = InvestigationStatus.resolved
    _persist_investigations()
    logfire.info('Investigation resolved via UI', investigation_id=inv.investigation_id)
    return JSONResponse({'ok': True})


_agent_api = Starlette(routes=[
    Route('/investigations/{investigation_id}/resolve', _api_resolve_investigation, methods=['POST']),
])


def _format_investigation_list() -> str:
    if not _investigations:
        return 'No investigations recorded yet.'
    lines = [
        '| ID (short) | Device | Status | Created | Summary |',
        '|---|---|---|---|---|',
    ]
    for inv in sorted(_investigations.values(), key=lambda i: i.created_at, reverse=True):
        ts = inv.created_at.strftime('%H:%M:%S UTC')
        summary_raw = inv.summary or '(investigating...)'
        summary = (summary_raw[:60] + '...') if len(summary_raw) > 60 else summary_raw
        lines.append(f'| {inv.investigation_id[:8]} | {inv.device} | {inv.status.value} | {ts} | {summary} |')
    return '\n'.join(lines)


def _format_investigation_detail(inv: Investigation) -> str:
    lines = [
        f'## Investigation {inv.investigation_id[:8]}',
        f'**Device:** {inv.device}',
        f'**Status:** {inv.status.value}',
        f'**Created:** {inv.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")}',
        f'**Full ID:** `{inv.investigation_id}`',
        '',
        '### Triggering Event',
        f'```\n{inv.triggering_event}\n```',
        '',
        '### Investigation Log',
    ]
    if inv.investigation_log:
        for step, result in inv.investigation_log:
            lines.append(f'**{step}:**')
            lines.append(result)
            lines.append('')
    else:
        lines.append('*(no steps recorded yet)*')
    lines.extend([
        '',
        '### Current Summary',
        inv.summary or '*(investigation in progress...)*',
    ])
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Investigation query / action functions (called directly by the orchestrator)
# ---------------------------------------------------------------------------

def list_investigations() -> str:
    """List all known syslog investigations.

    Returns:
        Markdown table with columns: ID (short 8-char hex), Device, Status,
        Created (UTC time), and a truncated Summary. Returns a plain message
        if no investigations have been recorded yet.
    """
    return _format_investigation_list()


def get_investigation_detail(investigation_id: str) -> str:
    """Get full details of a specific investigation.

    Args:
        investigation_id: Full investigation ID or any unique hex prefix (e.g. "abc12345").
                          Call list_investigations first if you don't know the ID.

    Returns:
        Markdown-formatted investigation report including device, status, triggering
        syslog event, full investigation log, and current summary.
        If no match is found, returns an error message followed by the investigation list.
    """
    inv = _find_investigation(investigation_id)
    if inv is None:
        return f'No investigation found matching "{investigation_id}".\n\n' + _format_investigation_list()
    return _format_investigation_detail(inv)


async def _run_manual_investigation(inv: Investigation, prompt: str) -> None:
    """LLM-driven manual investigation launched as a background asyncio task."""
    try:
        with logfire.span('manual_investigation', investigation_id=inv.investigation_id):
            result = await syslog_investigator.run(
                prompt,
                instructions=INVESTIGATOR_INSTRUCTIONS_USER_INITIATED,
            )
        inv.investigation_log.append(('user_initiated', str(result.output)))
        inv.message_history = result.all_messages()
        inv.summary = str(result.output)
        inv.status = InvestigationStatus.waiting
        _persist_investigations()
        logfire.info('Manual investigation complete', investigation_id=inv.investigation_id)
        print(f'[syslog-agent] Investigation {inv.investigation_id[:8]} manual investigation complete.', flush=True)
    except Exception as exc:
        inv.investigation_log.append(('error', str(exc)))
        inv.summary = f'Investigation failed: {exc}'
        inv.status = InvestigationStatus.waiting
        _persist_investigations()
        logfire.error('Manual investigation failed', investigation_id=inv.investigation_id, error=str(exc))
        print(f'[syslog-agent] Investigation {inv.investigation_id[:8]} manual investigation failed: {exc}', flush=True)


async def _run_continuation(inv: Investigation, follow_up_text: str) -> None:
    """LLM-driven continuation launched as a background asyncio task."""
    try:
        with logfire.span('investigation_user_continuation', investigation_id=inv.investigation_id):
            result = await syslog_investigator.run(
                follow_up_text,
                message_history=inv.message_history,
                instructions=INVESTIGATOR_INSTRUCTIONS_CONTINUATION,
            )
        inv.investigation_log.append(('user_continuation', str(result.output)))
        inv.message_history = result.all_messages()
        inv.summary = str(result.output)
        inv.status = InvestigationStatus.waiting
        _persist_investigations()
        logfire.info('User continuation complete', investigation_id=inv.investigation_id)
        print(f'[syslog-agent] Investigation {inv.investigation_id[:8]} continuation complete.', flush=True)
    except Exception as exc:
        inv.investigation_log.append(('error', str(exc)))
        inv.summary = f'Continuation failed: {exc}'
        inv.status = InvestigationStatus.waiting
        _persist_investigations()
        logfire.error('User continuation failed', investigation_id=inv.investigation_id, error=str(exc))
        print(f'[syslog-agent] Investigation {inv.investigation_id[:8]} continuation failed: {exc}', flush=True)


async def continue_investigation(
    investigation_id: str,
    follow_up: str = '',
    synchronous: bool = True,
) -> str:
    """Resume LLM-driven troubleshooting for an existing investigation.

    Args:
        investigation_id: Full investigation ID or any unique hex prefix.
        follow_up: Optional instruction or question for the investigator
                   (e.g. "check neighboring devices" or "focus on BGP").
                   Defaults to a generic continue-and-summarise prompt.
        synchronous: If True (default), wait for the investigation to finish
                     and return the full result directly. If False, start the
                     continuation in the background and return immediately —
                     use this only when the user explicitly asks to run in
                     the background.

    Returns:
        If synchronous=False: confirmation that the continuation has started.
        If synchronous=True: the full investigation result once complete.
        If an investigation is already running, returns the current findings.
    """
    inv = _find_investigation(investigation_id)
    if inv is None:
        return f'No investigation found matching "{investigation_id}".\n\n' + _format_investigation_list()
    if inv.bg_task and not inv.bg_task.done():
        return (
            f'Investigation `{inv.investigation_id[:8]}` is still running.\n\n'
            f'**Current findings so far:**\n{inv.summary or "(none yet)"}'
        )
    follow_up_text = follow_up or 'Please continue the investigation and provide updated findings.'
    inv.status = InvestigationStatus.investigating
    _persist_investigations()
    if synchronous:
        await _run_continuation(inv, follow_up_text)
        return (
            f'## Investigation `{inv.investigation_id[:8]}` — continuation complete\n\n'
            + _format_investigation_detail(inv)
        )
    inv.bg_task = asyncio.create_task(
        _run_continuation(inv, follow_up_text),
        name=f'continuation-{inv.investigation_id[:8]}',
    )
    return (
        f'Continuation started for investigation `{inv.investigation_id[:8]}`. '
        f'The investigation is running in the background. '
        f'The user can follow progress at: {INVESTIGATION_STATUS_URL}/?id={inv.investigation_id} '
        f'— tell them to open that URL in a browser (requires investigation_status.py to be running; '
        f'if it is not, they can start it and visit the link then).'
    )


async def open_manual_investigation(description: str, device: str = '') -> str:
    """Open a new investigation from a user-reported problem and start investigating in the background.

    Use this when the user describes a problem rather than referencing an existing investigation.
    The investigation runs in the background — use get_investigation_detail once complete.

    Args:
        description: User's description of the problem (e.g. "BGP session to router2 keeps flapping").
        device: Optional device name hint (short name, e.g. "router1"). Leave empty if unknown
                or if multiple devices may be involved — the investigator will infer from context.

    Returns:
        Confirmation that the investigation was opened and has started.
        Use get_investigation_detail in ~60s to retrieve the findings.
    """
    prompt = description if not device else f'Device hint: {device}\n\nProblem: {description}'
    inv = Investigation(
        investigation_id=uuid4().hex,
        created_at=datetime.now(timezone.utc),
        device=device or 'user-reported',
        triggering_event=description,
        triggering_timestamp_ns=time.time_ns(),
        status=InvestigationStatus.investigating,
    )
    _investigations[inv.investigation_id] = inv
    _persist_investigations()
    logfire.info('Opening manual investigation', investigation_id=inv.investigation_id, device=inv.device)
    inv.bg_task = asyncio.create_task(
        _run_manual_investigation(inv, prompt),
        name=f'manual-{inv.investigation_id[:8]}',
    )
    return (
        f'Investigation `{inv.investigation_id[:8]}` opened and started. '
        f'The investigation is running in the background. '
        f'The user can follow progress at: {INVESTIGATION_STATUS_URL}/?id={inv.investigation_id} '
        f'— tell them to open that URL in a browser (requires investigation_status.py to be running; '
        f'if it is not, they can start it and visit the link then).'
    )


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def syslog_lifespan():
    """Start the MCP subprocess, Loki poller, and agent API server for the syslog agent."""
    print('Starting syslog agent background tasks...')
    api_config = uvicorn.Config(_agent_api, host='127.0.0.1', port=AGENT_API_PORT, log_level='warning')
    api_server = uvicorn.Server(api_config)
    api_server.install_signal_handlers = lambda: None  # signal handling owned by the main process
    async with syslog_investigator:
        async with httpx.AsyncClient(timeout=30) as http_client:
            poll_task = asyncio.create_task(_loki_poll_loop(http_client))
            api_task = asyncio.create_task(api_server.serve())
            try:
                print(f'Syslog agent MCP server running. Loki poller active. Agent API on port {AGENT_API_PORT}.')
                yield
            finally:
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
                api_server.should_exit = True
                await api_task
                running = [
                    inv.bg_task for inv in _investigations.values()
                    if inv.bg_task and not inv.bg_task.done()
                ]
                if running:
                    print(
                        f'[syslog-agent] Waiting for {len(running)} investigation(s) to finish...'
                        ' (Ctrl+C again to force quit)',
                        flush=True,
                    )
                    try:
                        await asyncio.gather(*running, return_exceptions=True)
                    except asyncio.CancelledError:
                        for t in running:
                            t.cancel()
                        print('[syslog-agent] Force shutdown — investigations cancelled.', flush=True)
    print('Syslog agent stopped.')
