"""Nokia SR Linux JSON-RPC client.

Single source of truth for talking to SR Linux devices. Replaces the old
SSH/Netmiko transport. Connection details (hostname, credentials, port,
scheme) are read from inventory/hosts.yaml + inventory/defaults.yaml.

All four JSON-RPC methods are exposed:
  - jrpc_get      : YANG path reads (state or config)
  - jrpc_set      : structured config updates (atomic commit)
  - jrpc_validate : dry-run structured config updates
  - jrpc_cli      : escape hatch for arbitrary CLI commands

Endpoint: <scheme>://<host>:<port>/jsonrpc
Auth:     HTTP basic (admin / NokiaSrl1! in this lab)

The module owns a single shared httpx.AsyncClient so HTTP connections are
reused across calls. Close it on shutdown with close_client().
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import logfire
import yaml

INVENTORY_DIR = Path(__file__).parent / 'inventory'

_id_counter = itertools.count(1)
_client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------------------
# Errors and connection model
# ---------------------------------------------------------------------------

class SrlJsonRpcError(Exception):
    """Raised on any JSON-RPC failure: transport, HTTP, or JSON-RPC error field."""


@dataclass(frozen=True)
class SrlConnection:
    name: str
    host: str
    username: str
    password: str
    scheme: str = 'http'
    port: int = 80

    @property
    def url(self) -> str:
        return f'{self.scheme}://{self.host}:{self.port}/jsonrpc'


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _load_inventory() -> tuple[dict, dict]:
    with open(INVENTORY_DIR / 'hosts.yaml') as f:
        hosts = yaml.safe_load(f)
    with open(INVENTORY_DIR / 'defaults.yaml') as f:
        defaults = yaml.safe_load(f)
    return hosts, defaults


def get_connection(device_name: str) -> SrlConnection:
    """Resolve a device name to an SrlConnection. Raises ValueError if missing."""
    hosts, defaults = _load_inventory()
    if device_name not in hosts:
        raise ValueError(
            f"Device '{device_name}' not found in inventory. "
            f'Available: {list(hosts.keys())}'
        )
    device = hosts[device_name]
    return SrlConnection(
        name=device_name,
        host=device['hostname'],
        username=defaults['username'],
        password=defaults['password'],
        scheme=defaults.get('jsonrpc_scheme', 'http'),
        port=int(defaults.get('jsonrpc_port', 80)),
    )


def list_devices() -> list[str]:
    """Return all device names from inventory/hosts.yaml."""
    hosts, _ = _load_inventory()
    return list(hosts.keys())


# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def close_client() -> None:
    """Close the shared HTTP client. Call from app shutdown hooks if desired."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ---------------------------------------------------------------------------
# Core JSON-RPC call
# ---------------------------------------------------------------------------

async def _call(
    conn: SrlConnection,
    method: str,
    params: dict[str, Any],
) -> Any:
    """Send one JSON-RPC request and return the `result` field.

    Raises SrlJsonRpcError on transport failure, non-2xx HTTP, or any JSON-RPC
    `error` payload returned by the device.
    """
    payload = {
        'jsonrpc': '2.0',
        'id': next(_id_counter),
        'method': method,
        'params': params,
    }
    client = _get_client()
    with logfire.span(
        'srl_jsonrpc',
        device=conn.name,
        host=conn.host,
        method=method,
    ):
        try:
            response = await client.post(
                conn.url,
                json=payload,
                auth=(conn.username, conn.password),
            )
        except httpx.HTTPError as exc:
            logfire.error(
                'JSON-RPC transport failed',
                device=conn.name,
                method=method,
                error=str(exc),
            )
            raise SrlJsonRpcError(
                f'Transport error to {conn.host}: {exc}'
            ) from exc

        if response.status_code != 200:
            logfire.error(
                'JSON-RPC non-200 response',
                device=conn.name,
                method=method,
                status=response.status_code,
                body=response.text[:500],
            )
            raise SrlJsonRpcError(
                f'HTTP {response.status_code} from {conn.host}: {response.text[:300]}'
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise SrlJsonRpcError(
                f'Non-JSON response from {conn.host}: {response.text[:300]}'
            ) from exc

        if 'error' in body:
            err = body['error']
            msg = err.get('message', str(err)) if isinstance(err, dict) else str(err)
            logfire.error(
                'JSON-RPC error response',
                device=conn.name,
                method=method,
                error=err,
            )
            raise SrlJsonRpcError(f'JSON-RPC error on {conn.host}: {msg}')

        return body.get('result')


# ---------------------------------------------------------------------------
# Public methods
# ---------------------------------------------------------------------------

async def jrpc_get(
    conn: SrlConnection,
    paths: list[str],
    datastore: str = 'state',
) -> list:
    """Run a `get` request. Returns the list of per-path result objects."""
    params = {
        'commands': [{'path': p, 'datastore': datastore} for p in paths],
    }
    result = await _call(conn, 'get', params)
    return result if isinstance(result, list) else [result]


async def jrpc_set(
    conn: SrlConnection,
    updates: list[dict],
    datastore: str = 'candidate',
) -> Any:
    """Run a `set` request. updates = [{action,path,value?}, ...].

    Common actions: 'update', 'replace', 'delete'.
    """
    params = {
        'commands': updates,
        'datastore': datastore,
    }
    return await _call(conn, 'set', params)


async def jrpc_validate(
    conn: SrlConnection,
    updates: list[dict],
    datastore: str = 'candidate',
) -> Any:
    """Run a `validate` request (dry-run set). Same shape as jrpc_set."""
    params = {
        'commands': updates,
        'datastore': datastore,
    }
    return await _call(conn, 'validate', params)


async def jrpc_cli(
    conn: SrlConnection,
    commands: list[str],
    output_format: str = 'json',
) -> list:
    """Run arbitrary CLI commands via the JSON-RPC `cli` method.

    output_format: 'json', 'text', or 'table'. With 'json', the device
    returns already-decoded objects for show commands that support it.

    Returns the list of per-command results.
    """
    params: dict[str, Any] = {
        'commands': commands,
        'output-format': output_format,
    }
    result = await _call(conn, 'cli', params)
    return result if isinstance(result, list) else [result]
