#!/usr/bin/env python3
"""MCP Server — Network Topology Discovery Tools

Exposes topology discovery tools via the MCP protocol (stdio transport).
Consumed as a subprocess by topology_agent.py via MCPServerStdio.

Tools:
  - load_clab_topology   : parse a ContainerLab YAML file, return network devices only
  - get_lldp_neighbors   : query a network device's LLDP neighbors via JSON-RPC

Connection details (hostname, credentials) are read from inventory/hosts.yaml
and inventory/defaults.yaml — the same inventory used by mcp_server.py. To
add a new vendor, extend NETWORK_DEVICE_KINDS and add a _query_<vendor>
function.

Only network device kinds listed in NETWORK_DEVICE_KINDS are processed.
End systems (Linux hosts, clients) are filtered out in load_clab_topology.

Run standalone for testing:
  uv run topology_mcp_server.py
"""

import json
import os
from pathlib import Path

import logfire
import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP

from srl_jsonrpc import SrlJsonRpcError, get_connection, jrpc_get

env_file = Path(__file__).parent / '.env'
if env_file.exists():
    load_dotenv(env_file)

LOGFIRE_TOKEN = os.getenv('LOGFIRE_TOKEN')
if LOGFIRE_TOKEN:
    logfire.configure(
        token=LOGFIRE_TOKEN,
        service_name='Topology MCP Server',
        console=False,
    )
    logfire.instrument_mcp()
else:
    print('LOGFIRE_TOKEN not found. Running without Logfire observability.')

mcp = FastMCP('Topology MCP Server')

DEFAULT_CLAB_FILE = str(Path(__file__).parent / 'testlab.clab.yml')

# Extend this set when adding support for a new vendor.
# Each kind must have a corresponding _query_<vendor> function below.
NETWORK_DEVICE_KINDS: frozenset[str] = frozenset({
    'nokia_srlinux',
    # 'arista_ceos',
    # 'cisco_xrd',
    # 'juniper_crpd',
})


# ---------------------------------------------------------------------------
# LLDP parsing
# ---------------------------------------------------------------------------

def _extract_lldp_interfaces(result: object) -> list[dict]:
    """Pull the list of interface objects out of a JSON-RPC `get` result.

    SR Linux returns the data at the queried path. Depending on version /
    path form, this may be either:
      - {"interface": [{...}, {...}]}            (wrapped)
      - [{"name": "...", ...}, ...]              (bare list)
      - {"system": {"lldp": {"interface": [...]}}}  (queried at /system/lldp)
    Be defensive across all of them.
    """
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if 'interface' in result and isinstance(result['interface'], list):
            return result['interface']
        lldp = result.get('system', {}).get('lldp', {}) if isinstance(result, dict) else {}
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
        return {'node': node_name, 'neighbors': [], 'error': f'JSON-RPC error: {e}'}

    raw = results[0] if results else {}
    neighbors = _parse_srlinux_lldp(raw)
    logfire.info('SR Linux LLDP via JSON-RPC', node=node_name, neighbor_count=len(neighbors))
    return {'node': node_name, 'neighbors': neighbors}


# Add new vendor functions here, e.g.:
#
# async def _query_arista(node_name: str) -> dict:
#     ...


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def load_clab_topology(file_path: str = DEFAULT_CLAB_FILE) -> str:
    """Parse a ContainerLab YAML topology file and return only network device nodes.

    End systems (linux hosts, clients) are filtered out so the topology agent
    never attempts to connect to them.

    Args:
        file_path: Absolute or relative path to the .clab.yml file.
                   Defaults to testlab.clab.yml next to this script.

    Returns:
        JSON string: {"lab_name": str, "nodes": {name: {"kind": str, "labels": dict}}}
        Only nodes whose kind is in NETWORK_DEVICE_KINDS are included.
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(__file__).parent / path

    if not path.exists():
        return json.dumps({'error': f'File not found: {path}'})

    with open(path) as f:
        data = yaml.safe_load(f)

    lab_name = data.get('name', 'unknown')
    nodes: dict[str, dict] = {}
    skipped: list[str] = []

    for node_name, node_cfg in data.get('topology', {}).get('nodes', {}).items():
        kind = node_cfg.get('kind', 'unknown')
        if kind in NETWORK_DEVICE_KINDS:
            nodes[node_name] = {
                'kind': kind,
                'labels': node_cfg.get('labels', {}),
            }
        else:
            skipped.append(f'{node_name} (kind={kind})')

    logfire.info('Loaded clab topology', lab=lab_name, network_devices=list(nodes), skipped=skipped)
    print(f'[topology-mcp] lab={lab_name}  devices: {list(nodes)}  skipped: {skipped}', flush=True)

    return json.dumps({'lab_name': lab_name, 'nodes': nodes})


@mcp.tool()
async def get_lldp_neighbors(lab_name: str, node_name: str, node_kind: str) -> str:
    """Query a network device's LLDP neighbors via JSON-RPC.

    Connection details (hostname, credentials) are resolved from
    inventory/hosts.yaml using node_name as the lookup key.

    Args:
        lab_name:  Lab name returned by load_clab_topology (kept for context).
        node_name: Node name as defined in the topology file (inventory key).
        node_kind: ContainerLab kind of the node (e.g. "nokia_srlinux").

    Returns:
        JSON string: {"node": str, "neighbors": [{"local_port", "neighbor_name", "neighbor_port"}], "error"?: str}
    """
    logfire.info('LLDP query', lab=lab_name, node=node_name, kind=node_kind)
    print(f'[topology-mcp] querying {node_name} ({node_kind}) in lab {lab_name}', flush=True)

    if node_kind == 'nokia_srlinux':
        result = await _query_srlinux(node_name)
        return json.dumps(result)

    # Add elif branches here as new vendors are supported.
    return json.dumps({
        'node': node_name,
        'neighbors': [],
        'error': f'LLDP query not implemented for kind: {node_kind}',
    })


if __name__ == '__main__':
    mcp.run()
