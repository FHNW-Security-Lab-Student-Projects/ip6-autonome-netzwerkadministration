"""State Snapshot Agent

Periodically captures device state (routing table, ARP entries, interface status)
from Nokia SR Linux devices and persists snapshots to a JSON file. Enables
historical comparison during troubleshooting — e.g. "what did the routing table
look like 5 minutes before the incident?".

Architecture:
    state_snapshot_agent.py (Pydantic AI Agent + background refresh task)
        └── background refresh task
                └── SSH into each device in parallel (netmiko)

At startup, existing snapshots are loaded from disk. The background loop then
runs immediately and every REFRESH_INTERVAL seconds thereafter.

Import and use via agent delegation:
    from state_snapshot_agent import snapshot_agent, snapshot_lifespan
"""

import asyncio
import difflib
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import logfire
import yaml
from dotenv import load_dotenv
from netmiko import ConnectHandler
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

load_dotenv(Path(__file__).parent / '.env')

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not OPENROUTER_API_KEY:
    raise ValueError('OPENROUTER_API_KEY not found. Copy .env.example to .env and add your key.')


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INVENTORY_DIR = Path(__file__).parent / 'inventory'
SNAPSHOTS_FILE = Path(__file__).parent / 'state_snapshots.json'
REFRESH_INTERVAL = 120  # seconds between snapshot runs
MAX_SNAPSHOTS = 30      # per device per table — ~1 hour at 2-min intervals

# Static commands (not per-network-instance), keyed by table name used in the API
STATIC_SNAPSHOT_COMMANDS: dict[str, str] = {
    'arp': 'show arpnd arp-entries',
    'interfaces': 'show interface brief',
}


# ---------------------------------------------------------------------------
# Inventory + SSH helpers (same pattern as topology_agent.py)
# ---------------------------------------------------------------------------

def _load_inventory() -> tuple[dict, dict]:
    with open(INVENTORY_DIR / 'hosts.yaml') as f:
        hosts = yaml.safe_load(f)
    with open(INVENTORY_DIR / 'defaults.yaml') as f:
        defaults = yaml.safe_load(f)
    return hosts, defaults


def _get_connection_params(device_name: str) -> dict:
    hosts, defaults = _load_inventory()
    if device_name not in hosts:
        raise ValueError(
            f"Device '{device_name}' not found. Available: {list(hosts.keys())}"
        )
    device = hosts[device_name]
    return {
        'device_type': device['platform'],
        'host': device['hostname'],
        'username': defaults['username'],
        'password': defaults['password'],
    }


def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


def _parse_network_instances(output: str) -> list[str]:
    """Parse 'show network-instance' output and return NI names.
    Falls back to ['default'] if parsing fails or output is empty.
    """
    names = []
    in_data = False
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if 'Name' in stripped and 'Type' in stripped and 'Admin state' in stripped:
            in_data = True
            continue
        if stripped.startswith('-'):
            continue
        if in_data:
            parts = stripped.split()
            if parts:
                names.append(parts[0])
    return names if names else ['default']


def _ssh_capture_device(params: dict) -> dict[str, str]:
    """Open one SSH connection and collect all snapshot tables for a device."""
    results: dict[str, str] = {}
    with ConnectHandler(**params) as conn:
        # Discover network instances first
        ni_raw = _strip_ansi(conn.send_command('show network-instance'))
        ni_names = _parse_network_instances(ni_raw)

        for ni in ni_names:
            output = _strip_ansi(conn.send_command(f'show network-instance {ni} route-table'))
            results[f'route_table_{ni}'] = output

        for table, command in STATIC_SNAPSHOT_COMMANDS.items():
            results[table] = _strip_ansi(conn.send_command(command))

    return results


# ---------------------------------------------------------------------------
# In-memory store + disk persistence
# ---------------------------------------------------------------------------

# device → table → list of {"ts": ISO str, "output": str}, oldest first
_snapshots: dict[str, dict[str, list[dict]]] = {}


def _load_from_disk() -> None:
    global _snapshots
    if not SNAPSHOTS_FILE.exists():
        return
    try:
        _snapshots = json.loads(SNAPSHOTS_FILE.read_text())
        logfire.info('State snapshots loaded from disk', path=str(SNAPSHOTS_FILE))
    except Exception as exc:
        logfire.warning('Failed to load snapshots from disk', error=str(exc))
        _snapshots = {}


def _save_to_disk() -> None:
    try:
        SNAPSHOTS_FILE.write_text(json.dumps(_snapshots, indent=2))
    except Exception as exc:
        logfire.error('Failed to save snapshots to disk', error=str(exc))


def _store(device: str, table: str, output: str, ts: datetime) -> None:
    _snapshots.setdefault(device, {}).setdefault(table, [])
    _snapshots[device][table].append({'ts': ts.isoformat(), 'output': output})
    if len(_snapshots[device][table]) > MAX_SNAPSHOTS:
        _snapshots[device][table] = _snapshots[device][table][-MAX_SNAPSHOTS:]


# ---------------------------------------------------------------------------
# Snapshot collection
# ---------------------------------------------------------------------------

async def _snapshot_device(device_name: str) -> None:
    try:
        params = _get_connection_params(device_name)
    except ValueError as exc:
        logfire.error('Device not in inventory', device=device_name, error=str(exc))
        return

    ts = datetime.now(timezone.utc)
    try:
        tables = await asyncio.to_thread(_ssh_capture_device, params)
    except Exception as exc:
        logfire.error('Snapshot SSH session failed', device=device_name, error=str(exc))
        return

    for table, output in tables.items():
        _store(device_name, table, output, ts)


async def _snapshot_all() -> None:
    hosts, _ = _load_inventory()
    await asyncio.gather(
        *[_snapshot_device(name) for name in hosts],
        return_exceptions=True,
    )
    _save_to_disk()


# ---------------------------------------------------------------------------
# Background refresh loop
# ---------------------------------------------------------------------------

async def _refresh_loop(interval: int = REFRESH_INTERVAL) -> None:
    while True:
        try:
            with logfire.span('state_snapshot_refresh'):
                await _snapshot_all()
            logfire.info('State snapshots refreshed', devices=list(_snapshots.keys()))
            print(
                f'[snapshot-agent] Snapshots updated for: {", ".join(_snapshots.keys())}',
                flush=True,
            )
        except Exception as exc:
            logfire.error('Snapshot refresh failed', error=str(exc))
            print(f'[snapshot-agent] Refresh failed: {exc}', flush=True)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# LLM Agent
# ---------------------------------------------------------------------------

llm = OpenAIChatModel(
    'z-ai/glm-5',
    provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
    settings=ModelSettings(parallel_tool_calls=True, timeout=180),
)

snapshot_agent = Agent(
    model=llm,
    name='snapshot_agent',
    output_type=str,
    instructions="""You are a network state history agent for Nokia SR Linux devices.

You have access to periodic snapshots of device state captured every 2 minutes.
Available tables per device:
  - route_table_<ni> : IP routing table per network-instance (e.g. route_table_default, route_table_mgmt)
  - arp              : ARP entries       (show arpnd arp-entries)
  - interfaces       : Interface status  (show interface brief)

Call snapshot_status to discover which route_table_<ni> keys exist for each device.

Available devices: router1, router2, switch1, switch2.

WORKFLOW:
- If asked what data is available, call snapshot_status first.
- To retrieve historical state, call state_before with the relevant device, table,
  and a timestamp close to the moment of interest.
- To detect what changed between two points in time, call state_diff.
- When the request covers multiple devices or tables, call the tools in parallel.
- Summarise findings clearly: highlight added/removed routes, ARP changes,
  or interface state transitions. Do not dump raw output unless asked.
- If the request is ambiguous (e.g. no timestamp given), ask for clarification.
""",
)


@snapshot_agent.tool_plain
def snapshot_status() -> str:
    """Return a summary of available snapshots: which devices and tables are covered
    and the time range of stored entries."""
    if not _snapshots:
        return 'No snapshots collected yet — the background loop may still be warming up.'
    lines = ['Snapshot coverage:']
    for device, tables in sorted(_snapshots.items()):
        for table, entries in sorted(tables.items()):
            if entries:
                lines.append(
                    f'  {device}/{table}: {len(entries)} snapshots '
                    f'({entries[0]["ts"]} → {entries[-1]["ts"]})'
                )
    return '\n'.join(lines)


@snapshot_agent.tool_plain
def state_before(device: str, table: str, timestamp_iso: str) -> str:
    """Return the snapshot of a device table closest to but not after a given timestamp.

    Args:
        device: Device name (e.g. 'router1', 'switch1').
        table: One of 'route_table', 'arp', 'interfaces'.
        timestamp_iso: ISO 8601 timestamp (e.g. '2026-05-11T14:00:00+00:00').
    """
    try:
        before = datetime.fromisoformat(timestamp_iso)
    except ValueError:
        return f"Invalid timestamp '{timestamp_iso}'. Use ISO 8601 format."
    entries = _snapshots.get(device, {}).get(table, [])
    if not entries:
        return f'No snapshots found for {device}/{table}.'
    before_ts = before.isoformat()
    candidates = [e for e in entries if e['ts'] <= before_ts]
    if not candidates:
        return f'No snapshot found before {timestamp_iso} for {device}/{table}.'
    snap = candidates[-1]
    return f'Snapshot at {snap["ts"]}:\n\n{snap["output"]}'


@snapshot_agent.tool_plain
def state_diff(device: str, table: str, t1_iso: str, t2_iso: str) -> str:
    """Return a unified diff of a device table between two points in time.

    Args:
        device: Device name (e.g. 'router1').
        table: One of 'route_table', 'arp', 'interfaces'.
        t1_iso: Earlier timestamp in ISO 8601.
        t2_iso: Later timestamp in ISO 8601.
    """
    try:
        t1 = datetime.fromisoformat(t1_iso)
        t2 = datetime.fromisoformat(t2_iso)
    except ValueError as exc:
        return f'Invalid timestamp: {exc}. Use ISO 8601 format.'

    def _nearest(ts: datetime) -> dict | None:
        entries = _snapshots.get(device, {}).get(table, [])
        candidates = [e for e in entries if e['ts'] <= ts.isoformat()]
        return candidates[-1] if candidates else None

    s1, s2 = _nearest(t1), _nearest(t2)
    if s1 is None and s2 is None:
        return f'No snapshots found for {device}/{table}.'
    if s1 is None:
        return f'No snapshot found before {t1_iso} for {device}/{table}.'
    if s2 is None:
        return f'No snapshot found before {t2_iso} for {device}/{table}.'
    if s1['ts'] == s2['ts']:
        return f'Both timestamps resolve to the same snapshot ({s1["ts"]}) — no diff.'

    diff_lines = list(difflib.unified_diff(
        s1['output'].splitlines(keepends=True),
        s2['output'].splitlines(keepends=True),
        fromfile=f'{device}/{table} @ {s1["ts"]}',
        tofile=f'{device}/{table} @ {s2["ts"]}',
    ))
    if not diff_lines:
        return f'No changes in {device}/{table} between {s1["ts"]} and {s2["ts"]}.'
    return ''.join(diff_lines)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def snapshot_lifespan():
    """Load persisted snapshots from disk, then run the background refresh loop."""
    _load_from_disk()
    print('[snapshot-agent] Starting state snapshot loop...', flush=True)
    task = asyncio.create_task(_refresh_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        print('[snapshot-agent] Stopped.', flush=True)
