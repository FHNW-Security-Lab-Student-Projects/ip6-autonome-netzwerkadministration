#!/usr/bin/env python3
"""Syslog MCP Server

Provides Loki-specific tools for the Syslog Investigation Agent:
- query_loki: Query Nokia SR Linux syslog from Loki around a point in time

Network tools (execute_show_command, get_device_info, list_all_devices)
are provided by mcp_server.py.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import logfire
from dotenv import load_dotenv
from fastmcp import FastMCP

env_file = Path(__file__).parent / '.env'
if env_file.exists():
    load_dotenv(env_file)

LOGFIRE_TOKEN = os.getenv('LOGFIRE_TOKEN')
if LOGFIRE_TOKEN:
    logfire.configure(token=LOGFIRE_TOKEN, service_name='Syslog MCP Server', console=False)
    logfire.instrument_mcp()
else:
    print('LOGFIRE_TOKEN not found. Running without Logfire observability.')

mcp = FastMCP('Syslog MCP Server')

LOKI_URL = 'http://172.20.20.101:3100'


@mcp.tool()
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
                     Use the triggering event timestamp from the investigation prompt.
                     Example: "2026-04-14T14:30:00Z"
        minutes_before: Minutes before time_anchor to include (default 5).
        minutes_after: Minutes after time_anchor to include (default 2).
        severities: Comma-separated severity levels to include.
                    Available: emergency, alert, critical, error, warning, notice,
                    informational, debug. Default includes all except debug.
        text_filter: Optional substring — only return lines containing this string.
        limit: Maximum number of log lines to return (default 100, max 500).
    """
    severity_regex = '|'.join(s.strip() for s in severities.split(',') if s.strip())
    if device == 'all':
        stream = f'{{vendor="nokia_srlinux", severity=~"{severity_regex}"}}'
    else:
        host = f'clab-testlab-{device}'
        stream = f'{{vendor="nokia_srlinux", host="{host}", severity=~"{severity_regex}"}}'
    if text_filter:
        stream += f' |= "{text_filter}"'

    anchor_dt = datetime.fromisoformat(time_anchor.replace('Z', '+00:00'))
    start_ns = int((anchor_dt.timestamp() - minutes_before * 60) * 1e9)
    end_ns = int((anchor_dt.timestamp() + minutes_after * 60) * 1e9)
    limit = min(limit, 500)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
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
            lines.append(f'[{ts_dt.strftime("%H:%M:%S")}] [{host_label}] [{sev_label}] [{app_label}] {line}')

    if not lines:
        return (
            f'No log entries found for query: {stream} '
            f'in window {minutes_before}m before / {minutes_after}m after {time_anchor}.'
        )
    return '\n'.join(lines)



if __name__ == '__main__':
    mcp.run()
