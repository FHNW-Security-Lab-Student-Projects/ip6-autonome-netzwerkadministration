#!/usr/bin/env python3
"""MCP Server — Network Topology Discovery Tools

Exposes topology discovery tools via the MCP protocol (stdio transport).
Consumed as a subprocess by topology_agent.py via MCPServerStdio.

Tools:
  - load_clab_topology   : parse a ContainerLab YAML file, return network devices only
  - get_lldp_neighbors   : SSH into a network device and query LLDP neighbors

Connection details (hostname, credentials, platform/vendor) are read from
inventory/hosts.yaml and inventory/defaults.yaml — the same inventory used by
mcp_server.py. SSH works with any vendor, making it straightforward to add
support for a second vendor: add it to NETWORK_DEVICE_KINDS, add a
_query_<vendor> function, and point it at the right CLI command.

Only network device kinds listed in NETWORK_DEVICE_KINDS are processed.
End systems (Linux hosts, clients) are filtered out in load_clab_topology.

Run standalone for testing:
  uv run topology_mcp_server.py
"""

import asyncio
import json
import os
import re
from pathlib import Path

import logfire
import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP
from netmiko import ConnectHandler

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
INVENTORY_DIR = Path(__file__).parent / 'inventory'

# Extend this set when adding support for a new vendor.
# Each kind must have a corresponding _query_<vendor> function below.
NETWORK_DEVICE_KINDS: frozenset[str] = frozenset({
    'nokia_srlinux',
    # 'arista_ceos',
    # 'cisco_xrd',
    # 'juniper_crpd',
})


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

    Expected structure:
    {
      "system": {
        "lldp": {
          "interface": [
            {
              "name": "ethernet-1/1",
              "neighbor": [
                {"id": "...", "system-name": "switch1", "port-id": "ethernet-1/3"}
              ]
            }
          ]
        }
      }
    }
    """
    neighbors: list[dict] = []
    try:
        lldp = data.get('system', {}).get('lldp', {})
        for iface in lldp.get('interface', []):
            local_port = iface.get('name', 'unknown')
            for nbr in iface.get('neighbor', []):
                nbr_name = nbr.get('system-name', '')
                nbr_port = nbr.get('port-id', '')
                if nbr_name:
                    neighbors.append({
                        'local_port': local_port,
                        'neighbor_name': nbr_name,
                        'neighbor_port': nbr_port,
                    })
    except (KeyError, TypeError, AttributeError):
        pass
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
        return {'node': node_name, 'neighbors': [], 'error': f'SSH error: {e}'}

    clean = _strip_ansi(raw)
    json_start = clean.find('{')
    if json_start == -1:
        return {
            'node': node_name,
            'neighbors': [],
            'error': f'No JSON in output: {clean[:300]}',
        }

    try:
        data = json.loads(clean[json_start:])
    except json.JSONDecodeError as e:
        return {'node': node_name, 'neighbors': [], 'error': f'JSON parse error: {e}'}

    neighbors = _parse_srlinux_lldp(data)
    logfire.info('SR Linux LLDP via SSH', node=node_name, neighbor_count=len(neighbors))
    return {'node': node_name, 'neighbors': neighbors}


# Add new vendor functions here, e.g.:
#
# async def _query_arista(node_name: str) -> dict:
#     params = _get_connection_params(node_name)
#     raw = await asyncio.to_thread(_ssh_send_command, params, 'show lldp neighbors detail | json')
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
    """SSH into a network device and query its LLDP neighbors.

    Connection details (hostname, credentials, platform) are resolved from
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
