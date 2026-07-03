"""Topology Discovery Agent

Discovers network topology from ContainerLab environments by querying each
network device's LLDP neighbors over JSON-RPC.

Architecture:
  topology_agent.py (no LLM in the request path)
      └── background refresh task
              ├── parse testlab.clab.yml
              └── JSON-RPC into each device in parallel (httpx.AsyncClient)

At startup and every REFRESH_INTERVAL seconds, topology discovery runs in the
background and the result is cached. Callers use get_topology_response() to
read the cache — no device I/O on the hot path.

Import and use via agent delegation:
    from topology_agent import get_topology_response, topology_lifespan
"""

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import logfire
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

from srl_jsonrpc import SrlJsonRpcError, get_connection, jrpc_get

load_dotenv(Path(__file__).parent / '.env')


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Extend this set when adding support for a new vendor.
# Each kind must have a corresponding _query_<vendor> function below.
NETWORK_DEVICE_KINDS: frozenset[str] = frozenset({'nokia_srlinux'})

DEFAULT_CLAB_FILE = str(Path(__file__).parent / 'testlab.clab.yml')
REFRESH_INTERVAL = 60  # seconds between topology refreshes


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class TopologyNode(BaseModel):
    name: str
    kind: str
    role: str | None = None
    layer: str | None = None
    ip_address: str | None = None


class TopologyLink(BaseModel):
    node_a: str
    port_a: str
    node_b: str
    port_b: str


class TopologyDiff(BaseModel):
    missing_links: list[TopologyLink]   # in YAML but not seen via LLDP
    unexpected_links: list[TopologyLink]  # seen via LLDP but not in YAML
    missing_nodes: list[str]            # defined in YAML but LLDP query failed


class TopologyResult(BaseModel):
    nodes: dict[str, TopologyNode]
    links: list[TopologyLink]
    client_links: list[TopologyLink]  # static from YAML, not discovered via LLDP
    summary: str
    diff: TopologyDiff


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@dataclass
class CachedTopology:
    result: TopologyResult
    collected_at: datetime

_cache: CachedTopology | None = None


# ---------------------------------------------------------------------------
# LLDP parsing
# ---------------------------------------------------------------------------

def _extract_lldp_interfaces(result: object) -> list[dict]:
    """Pull the list of interface objects out of a JSON-RPC `get` result.

    Defensive across the shapes SR Linux may return for the queried path.
    """
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if 'interface' in result and isinstance(result['interface'], list):
            return result['interface']
        lldp = result.get('system', {}).get('lldp', {})
        if isinstance(lldp, dict) and isinstance(lldp.get('interface'), list):
            return lldp['interface']
    return []


def _parse_srlinux_lldp(result: object) -> list[dict]:
    """Convert a JSON-RPC LLDP get result into a flat list of neighbor entries."""
    neighbors: list[dict] = []
    for iface in _extract_lldp_interfaces(result):
        if not isinstance(iface, dict):
            continue
        local_port = iface.get('name', 'unknown')
        for nbr in iface.get('neighbor', []) or []:
            if not isinstance(nbr, dict):
                continue
            nbr_name = nbr.get('system-name', '')
            nbr_port = nbr.get('port-id', '')
            if nbr_name:
                neighbors.append({
                    'local_port': local_port,
                    'neighbor_name': nbr_name,
                    'neighbor_port': nbr_port,
                })
    return neighbors


# ---------------------------------------------------------------------------
# Per-vendor query functions
# ---------------------------------------------------------------------------

async def _query_srlinux(node_name: str) -> dict:
    """Query a Nokia SR Linux device via JSON-RPC and return its LLDP neighbors."""
    try:
        conn = get_connection(node_name)
    except ValueError as e:
        return {'node': node_name, 'neighbors': [], 'error': str(e)}

    try:
        results = await jrpc_get(
            conn,
            ['/system/lldp/interface[name=*]'],
            datastore='state',
        )
    except SrlJsonRpcError as e:
        logfire.error(
            'JSON-RPC LLDP query failed',
            node=node_name,
            host=conn.host,
            error=str(e),
        )
        return {'node': node_name, 'neighbors': [], 'error': f'JSON-RPC error: {e}'}

    raw = results[0] if results else {}
    neighbors = _parse_srlinux_lldp(raw)
    logfire.info(
        'SR Linux LLDP via JSON-RPC',
        node=node_name,
        host=conn.host,
        parsed_json=raw,
        neighbors=neighbors,
        neighbor_count=len(neighbors),
    )
    return {'node': node_name, 'neighbors': neighbors}


# ---------------------------------------------------------------------------
# Topology discovery (no LLM — called by the background refresh loop)
# ---------------------------------------------------------------------------

async def _query_node(node_name: str, node_info: dict) -> dict:
    """Dispatch to the right per-vendor query function."""
    kind = node_info['kind']
    if kind == 'nokia_srlinux':
        return await _query_srlinux(node_name)
    return {'node': node_name, 'neighbors': [], 'error': f'Unsupported kind: {kind}'}


def _normalize_port(port: str) -> str:
    """Convert ContainerLab shorthand (e1-1) to SR Linux CLI format (ethernet-1/1)."""
    return re.sub(r'^e(\d+)-(\d+)$', r'ethernet-\1/\2', port)


def _parse_desired_links(clab_data: dict, network_nodes: set[str]) -> list[TopologyLink]:
    """Extract links from ContainerLab YAML, keeping only SR Linux ↔ SR Linux links."""
    desired: list[TopologyLink] = []
    for entry in clab_data.get('topology', {}).get('links', []):
        endpoints = entry.get('endpoints', [])
        if len(endpoints) != 2:
            continue
        node_a, port_a = endpoints[0].split(':', 1)
        node_b, port_b = endpoints[1].split(':', 1)
        if node_a not in network_nodes or node_b not in network_nodes:
            continue
        desired.append(TopologyLink(
            node_a=node_a,
            port_a=_normalize_port(port_a),
            node_b=node_b,
            port_b=_normalize_port(port_b),
        ))
    return desired


def _parse_client_nodes(clab_data: dict) -> dict[str, TopologyNode]:
    """Extract linux nodes labelled role=client from ContainerLab YAML."""
    clients: dict[str, TopologyNode] = {}
    for node_name, node_cfg in clab_data.get('topology', {}).get('nodes', {}).items():
        if node_cfg.get('kind') != 'linux':
            continue
        labels = node_cfg.get('labels', {})
        if labels.get('role') != 'client':
            continue
        ip_address = None
        for cmd in node_cfg.get('exec', []):
            m = re.search(r'ip addr add (\S+) dev', cmd)
            if m:
                ip_address = m.group(1)
                break
        clients[node_name] = TopologyNode(
            name=node_name,
            kind='linux',
            role='client',
            ip_address=ip_address,
        )
    return clients


def _parse_client_links(clab_data: dict, client_nodes: set[str]) -> list[TopologyLink]:
    """Extract links from ContainerLab YAML where at least one endpoint is a client node."""
    client_links: list[TopologyLink] = []
    for entry in clab_data.get('topology', {}).get('links', []):
        endpoints = entry.get('endpoints', [])
        if len(endpoints) != 2:
            continue
        node_a, port_a = endpoints[0].split(':', 1)
        node_b, port_b = endpoints[1].split(':', 1)
        if node_a in client_nodes or node_b in client_nodes:
            client_links.append(TopologyLink(
                node_a=node_a,
                port_a=_normalize_port(port_a),
                node_b=node_b,
                port_b=_normalize_port(port_b),
            ))
    return client_links


def _compute_diff(
    desired: list[TopologyLink],
    actual: list[TopologyLink],
    unreachable_nodes: list[str],
) -> TopologyDiff:
    """Compare desired (YAML) vs actual (LLDP) links using unordered endpoint pairs."""
    def link_key(link: TopologyLink) -> frozenset:
        return frozenset({(link.node_a, link.port_a), (link.node_b, link.port_b)})

    desired_keys = {link_key(l): l for l in desired}
    actual_keys = {link_key(l): l for l in actual}

    return TopologyDiff(
        missing_links=[desired_keys[k] for k in desired_keys if k not in actual_keys],
        unexpected_links=[actual_keys[k] for k in actual_keys if k not in desired_keys],
        missing_nodes=unreachable_nodes,
    )


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

    # 3. Build nodes dict (network devices only)
    nodes: dict[str, TopologyNode] = {}
    for node_name, node_info in nodes_data.items():
        labels = node_info.get('labels', {})
        nodes[node_name] = TopologyNode(
            name=node_name,
            kind=node_info['kind'],
            role=labels.get('role'),
            layer=labels.get('layer'),
        )

    # 3b. Add client nodes from YAML (no SSH — static definition only)
    client_nodes_data = _parse_client_nodes(clab_data)
    nodes.update(client_nodes_data)
    client_links = _parse_client_links(clab_data, set(client_nodes_data.keys()))

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

    # 5. Build summary + drift
    unreachable = [
        r['node'] for r in lldp_results
        if not isinstance(r, Exception) and 'error' in r
    ]
    net_device_count = len(nodes_data)
    summary = (
        f'Discovered {net_device_count} network devices and {len(client_nodes_data)} clients '
        f'with {len(links)} network links in lab "{lab_name}".'
    )
    if unreachable:
        summary += f' Unreachable: {", ".join(unreachable)}.'

    desired_links = _parse_desired_links(clab_data, set(nodes_data.keys()))
    diff = _compute_diff(desired_links, links, unreachable)

    return TopologyResult(
        nodes=nodes,
        links=links,
        client_links=client_links,
        summary=summary,
        diff=diff,
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
# Public API
# ---------------------------------------------------------------------------

def get_topology_response() -> str | None:
    """Return a formatted topology response string, or None if cache is warming up."""
    if _cache is None:
        return None
    return _format_response(_cache.result, _cache.collected_at)


def _format_response(topology: TopologyResult, collected_at: datetime) -> str:
    """Render topology as a readable markdown response with data freshness."""
    age_s = int((datetime.now(timezone.utc) - collected_at).total_seconds())
    freshness = f'{age_s}s ago' if age_s < 60 else f'{age_s // 60}m {age_s % 60}s ago'

    network_nodes = {n: v for n, v in topology.nodes.items() if v.role != 'client'}
    client_nodes = {n: v for n, v in topology.nodes.items() if v.role == 'client'}

    lines: list[str] = [
        '## Network Topology Discovery\n',
        f'_Data collected {freshness}_\n',
        topology.summary,
        f'\n### Network Devices ({len(network_nodes)})',
    ]
    for name, node in network_nodes.items():
        meta_parts = [node.kind]
        if node.role:
            meta_parts.append(f'role={node.role}')
        if node.layer:
            meta_parts.append(f'layer={node.layer}')
        lines.append(f'- **{name}**: {", ".join(meta_parts)}')

    lines.append(f'\n### Links ({len(topology.links)}) — verified via LLDP')
    for link in topology.links:
        lines.append(f'- {link.node_a}:{link.port_a} ↔ {link.node_b}:{link.port_b}')

    if topology.client_links:
        lines.append(f'\n### Client Connections ({len(topology.client_links)}) — static from topology definition')
        for link in topology.client_links:
            client_name = (
                link.node_a
                if client_nodes.get(link.node_a) is not None
                else link.node_b
            )
            client_node = client_nodes.get(client_name)
            ip_info = f' ({client_node.ip_address})' if client_node and client_node.ip_address else ''
            lines.append(f'- {link.node_a}:{link.port_a} ↔ {link.node_b}:{link.port_b}{ip_info}')

    diff = topology.diff
    has_drift = diff.missing_links or diff.unexpected_links or diff.missing_nodes
    lines.append('\n### Topology Drift (desired vs actual)')
    if not has_drift:
        lines.append('No drift detected — topology matches the ContainerLab definition.')
    else:
        if diff.missing_nodes:
            lines.append(f'\n**Unreachable nodes** ({len(diff.missing_nodes)}):')
            for node in diff.missing_nodes:
                lines.append(f'- {node}')
        if diff.missing_links:
            lines.append(f'\n**Missing links** — expected but not seen via LLDP ({len(diff.missing_links)}):')
            for link in diff.missing_links:
                lines.append(f'- {link.node_a}:{link.port_a} ↔ {link.node_b}:{link.port_b}')
        if diff.unexpected_links:
            lines.append(f'\n**Unexpected links** — seen via LLDP but not in topology definition ({len(diff.unexpected_links)}):')
            for link in diff.unexpected_links:
                lines.append(f'- {link.node_a}:{link.port_a} ↔ {link.node_b}:{link.port_b}')

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def topology_lifespan():
    """Start the background topology refresh loop."""
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
