"""Topology Discovery Agent (A2A Server)

Discovers network topology from ContainerLab environments by SSHing directly
into each network device and querying LLDP neighbors.

Architecture:
  topology_agent.py (A2A server — no LLM in the request path)
      └── background refresh task
              ├── parse testlab.clab.yml
              └── SSH into each device in parallel (netmiko)

At startup and every REFRESH_INTERVAL seconds, topology discovery runs in the
background and the result is cached. The A2A execute() method simply returns
the cached result immediately — no SSH connections on the critical path.

Run with: uv run uvicorn topology_agent:app --port 8001
"""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import logfire
import yaml
from dotenv import load_dotenv
from netmiko import ConnectHandler
from pydantic import BaseModel

from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

from a2a_utils import BaseAgentExecutor, build_a2a_app, setup_logfire

load_dotenv(Path(__file__).parent / '.env')
setup_logfire('Topology Agent', instrument_llm=False)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Extend this set when adding support for a new vendor.
# Each kind must have a corresponding _query_<vendor> function below.
NETWORK_DEVICE_KINDS: frozenset[str] = frozenset({'nokia_srlinux'})

DEFAULT_CLAB_FILE = str(Path(__file__).parent / 'testlab.clab.yml')
INVENTORY_DIR = Path(__file__).parent / 'inventory'
REFRESH_INTERVAL = 60  # seconds between topology refreshes


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class TopologyNode(BaseModel):
    name: str
    kind: str
    role: str | None = None
    layer: str | None = None


class TopologyLink(BaseModel):
    node_a: str
    port_a: str
    node_b: str
    port_b: str


class TopologyResult(BaseModel):
    nodes: dict[str, TopologyNode]
    links: list[TopologyLink]
    mermaid: str
    summary: str


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@dataclass
class CachedTopology:
    result: TopologyResult
    collected_at: datetime

_cache: CachedTopology | None = None


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------

def _load_inventory() -> tuple[dict, dict]:
    """Load device inventory from YAML files. Returns (hosts, defaults)."""
    with open(INVENTORY_DIR / 'hosts.yaml') as f:
        hosts = yaml.safe_load(f)
    with open(INVENTORY_DIR / 'defaults.yaml') as f:
        defaults = yaml.safe_load(f)
    return hosts, defaults


def _get_connection_params(node_name: str) -> dict:
    """Look up a device in the inventory and return netmiko connection params.

    Raises ValueError if the device is not found.
    """
    hosts, defaults = _load_inventory()
    if node_name not in hosts:
        raise ValueError(
            f"Device '{node_name}' not found in inventory. "
            f"Available: {list(hosts.keys())}"
        )
    device = hosts[node_name]
    return {
        'device_type': device['platform'],
        'host': device['hostname'],
        'username': defaults['username'],
        'password': defaults['password'],
    }


# ---------------------------------------------------------------------------
# SSH + output parsing helpers
# ---------------------------------------------------------------------------

def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes that some devices emit over SSH."""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


def _ssh_send_command(params: dict, command: str) -> str:
    """Open an SSH session and run a single command. Returns raw output.

    Synchronous — must be called via asyncio.to_thread().
    """
    with ConnectHandler(**params) as conn:
        return conn.send_command(command)


def _parse_srlinux_lldp(data: dict) -> list[dict]:
    """Parse SR Linux 'show system lldp neighbor | as json' output.

    The CLI JSON format uses a flat 'LLDP-Interface' list where each entry
    represents one neighbor (one row per neighbor, not per interface):
      {
        "LLDP-Interface": [
          {"Name": "ethernet-1/1", "Neighbor System Name": "router1", "Neighbor Port": "ethernet-1/2"},
          ...
        ]
      }
    """
    neighbors: list[dict] = []
    try:
        for entry in data.get('LLDP-Interface', []):
            local_port = entry.get('Name', 'unknown')
            nbr_name = entry.get('Neighbor System Name', '')
            nbr_port = entry.get('Neighbor Port', '')
            if nbr_name:
                neighbors.append({
                    'local_port': local_port,
                    'neighbor_name': nbr_name,
                    'neighbor_port': nbr_port,
                })
    except (KeyError, TypeError, AttributeError) as exc:
        print(f'[DEBUG][_parse_srlinux_lldp] parse exception: {exc}', flush=True)
    return neighbors


# ---------------------------------------------------------------------------
# Per-vendor SSH query functions
# ---------------------------------------------------------------------------

async def _query_srlinux(node_name: str) -> dict:
    """SSH into a Nokia SR Linux device and return its LLDP neighbors."""
    try:
        params = _get_connection_params(node_name)
    except ValueError as e:
        return {'node': node_name, 'neighbors': [], 'error': str(e)}

    try:
        raw = await asyncio.to_thread(
            _ssh_send_command,
            params,
            'show system lldp neighbor | as json',
        )
    except Exception as e:
        logfire.error(
            'SSH command failed',
            node=node_name,
            host=params['host'],
            error=str(e),
        )
        return {'node': node_name, 'neighbors': [], 'error': f'SSH error: {e}'}

    clean = _strip_ansi(raw)
    json_start = clean.find('{')
    if json_start == -1:
        logfire.warning(
            'No JSON in LLDP output',
            node=node_name,
            host=params['host'],
            raw_output=raw,
        )
        return {
            'node': node_name,
            'neighbors': [],
            'error': f'No JSON in output: {clean[:300]}',
        }

    try:
        data = json.loads(clean[json_start:])
    except json.JSONDecodeError as e:
        logfire.error(
            'LLDP JSON parse failed',
            node=node_name,
            host=params['host'],
            raw_output=raw,
            error=str(e),
        )
        return {'node': node_name, 'neighbors': [], 'error': f'JSON parse error: {e}'}

    neighbors = _parse_srlinux_lldp(data)
    logfire.info(
        'SR Linux LLDP via SSH',
        node=node_name,
        host=params['host'],
        raw_output=raw,
        parsed_json=data,
        neighbors=neighbors,
        neighbor_count=len(neighbors),
    )
    return {'node': node_name, 'neighbors': neighbors}


# Add new vendor functions here, e.g.:
#
# async def _query_arista(node_name: str) -> dict:
#     params = _get_connection_params(node_name)
#     raw = await asyncio.to_thread(_ssh_send_command, params, 'show lldp neighbors detail | json')
#     ...


# ---------------------------------------------------------------------------
# Topology discovery (no LLM — called by the background refresh loop)
# ---------------------------------------------------------------------------

async def _query_node(node_name: str, node_info: dict) -> dict:
    """Dispatch to the right per-vendor query function."""
    kind = node_info['kind']
    if kind == 'nokia_srlinux':
        return await _query_srlinux(node_name)
    return {'node': node_name, 'neighbors': [], 'error': f'Unsupported kind: {kind}'}


async def _discover_topology(clab_file: str = DEFAULT_CLAB_FILE) -> TopologyResult:
    """Discover network topology directly via SSH — no LLM involved.

    1. Parse ContainerLab YAML to get the network device list.
    2. Query all devices in parallel.
    3. Deduplicate links and return a TopologyResult.
    """
    # 1. Parse topology YAML
    path = Path(clab_file)
    if not path.is_absolute():
        path = Path(__file__).parent / path

    with open(path) as f:
        clab_data = yaml.safe_load(f)

    lab_name = clab_data.get('name', 'unknown')
    nodes_data: dict[str, dict] = {}
    for node_name, node_cfg in clab_data.get('topology', {}).get('nodes', {}).items():
        kind = node_cfg.get('kind', 'unknown')
        if kind in NETWORK_DEVICE_KINDS:
            nodes_data[node_name] = {
                'kind': kind,
                'labels': node_cfg.get('labels', {}),
            }

    # 2. Query all devices in parallel
    lldp_results: list[dict] = list(await asyncio.gather(
        *[_query_node(name, info) for name, info in nodes_data.items()],
        return_exceptions=True,
    ))

    # 3. Build nodes dict
    nodes: dict[str, TopologyNode] = {}
    for node_name, node_info in nodes_data.items():
        labels = node_info.get('labels', {})
        nodes[node_name] = TopologyNode(
            name=node_name,
            kind=node_info['kind'],
            role=labels.get('role'),
            layer=labels.get('layer'),
        )

    # 4. Collect and deduplicate links
    seen_pairs: set[frozenset] = set()
    links: list[TopologyLink] = []
    node_names = set(nodes.keys())

    for result in lldp_results:
        if isinstance(result, Exception):
            continue
        node_a = result['node']
        for nbr in result.get('neighbors', []):
            node_b = nbr['neighbor_name']
            if node_b not in node_names:
                continue
            pair = frozenset({node_a, node_b})
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                links.append(TopologyLink(
                    node_a=node_a,
                    port_a=nbr['local_port'],
                    node_b=node_b,
                    port_b=nbr['neighbor_port'],
                ))

    # 5. Build Mermaid diagram
    labeled: set[str] = set()
    mermaid_lines = ['graph TD']

    def _node_label(name: str) -> str:
        n = nodes[name]
        label = f'{name}\\n{n.role}' if n.role else name
        return f'{name}["{label}"]'

    for link in links:
        a = _node_label(link.node_a) if link.node_a not in labeled else link.node_a
        b = _node_label(link.node_b) if link.node_b not in labeled else link.node_b
        labeled.update({link.node_a, link.node_b})
        mermaid_lines.append(f'  {a} --- {b}')

    # 6. Build summary
    unreachable = [
        r['node'] for r in lldp_results
        if not isinstance(r, Exception) and 'error' in r
    ]
    summary = f'Discovered {len(nodes)} network devices and {len(links)} links in lab "{lab_name}".'
    if unreachable:
        summary += f' Unreachable: {", ".join(unreachable)}.'

    return TopologyResult(
        nodes=nodes,
        links=links,
        mermaid='\n'.join(mermaid_lines),
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Background refresh loop
# ---------------------------------------------------------------------------

async def _refresh_loop(interval: int = REFRESH_INTERVAL) -> None:
    """Build topology at startup, then refresh every `interval` seconds."""
    global _cache
    while True:
        try:
            with logfire.span('topology_refresh'):
                result = await _discover_topology()
            _cache = CachedTopology(result=result, collected_at=datetime.now(timezone.utc))
            logfire.info(
                'Topology cache refreshed',
                nodes=len(result.nodes),
                links=len(result.links),
            )
            print(
                f'[topology-agent] Cache refreshed: {len(result.nodes)} nodes, '
                f'{len(result.links)} links',
                flush=True,
            )
        except Exception as exc:
            logfire.error('Topology refresh failed', error=str(exc))
            print(f'[topology-agent] Refresh failed: {exc}', flush=True)
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# A2A Executor
# ---------------------------------------------------------------------------

class TopologyAgentExecutor(BaseAgentExecutor):
    """Returns the latest cached topology. No LLM or SSH on the request path."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(
            event_queue,
            task_id=context.task_id or uuid4().hex,
            context_id=context.context_id or uuid4().hex,
        )

        if _cache is None:
            await updater.complete(
                message=new_agent_text_message(
                    'Topology cache is still warming up — please retry in a moment.'
                )
            )
            return

        with logfire.span('TopologyAgentExecutor.execute', task_id=context.task_id):
            response = _format_response(_cache.result, _cache.collected_at)
        await updater.complete(message=new_agent_text_message(response))

def _format_response(topology: TopologyResult, collected_at: datetime) -> str:
    """Render topology as a readable markdown response with data freshness."""
    age_s = int((datetime.now(timezone.utc) - collected_at).total_seconds())
    freshness = f'{age_s}s ago' if age_s < 60 else f'{age_s // 60}m {age_s % 60}s ago'

    lines: list[str] = [
        '## Network Topology Discovery\n',
        f'_Data collected {freshness}_\n',
        topology.summary,
        f'\n### Network Devices ({len(topology.nodes)})',
    ]
    for name, node in topology.nodes.items():
        meta_parts = [node.kind]
        if node.role:
            meta_parts.append(f'role={node.role}')
        if node.layer:
            meta_parts.append(f'layer={node.layer}')
        lines.append(f'- **{name}**: {", ".join(meta_parts)}')

    lines.append(f'\n### Links ({len(topology.links)})')
    for link in topology.links:
        lines.append(f'- {link.node_a}:{link.port_a} ↔ {link.node_b}:{link.port_b}')

    lines.append(f'\n### Mermaid Diagram\n```mermaid\n{topology.mermaid}\n```')

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# A2A server setup
# ---------------------------------------------------------------------------

skill = AgentSkill(
    id='discover_topology',
    name='Discover Network Topology',
    description=(
        'Returns the latest cached network topology discovered from ContainerLab '
        'devices via LLDP. The topology is refreshed every 60 seconds in the '
        'background — responses are always immediate.'
    ),
    tags=['network', 'topology', 'lldp', 'containerlab'],
    examples=[
        'Discover the network topology',
        'Show me the topology from testlab.clab.yml',
        'What is the current network topology?',
    ],
)

agent_card = AgentCard(
    name='Topology Discovery Agent',
    description=(
        'Returns a pre-built, cached network topology discovered from ContainerLab '
        'devices via LLDP. Topology is refreshed every 60 seconds in the background.'
    ),
    url='http://localhost:8002/',
    version='2.0.0',
    default_input_modes=['text'],
    default_output_modes=['text'],
    capabilities=AgentCapabilities(streaming=True, state_transition_history=True),
    skills=[skill],
)

@asynccontextmanager
async def lifespan(_):
    """Start the background refresh loop alongside the A2A server."""
    print(f'Starting topology background refresh (interval={REFRESH_INTERVAL}s)...')
    refresh_task = asyncio.create_task(_refresh_loop())
    try:
        yield
    finally:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        print('Topology refresh stopped.')


app = build_a2a_app(agent_card, TopologyAgentExecutor(), lifespan=lifespan)
