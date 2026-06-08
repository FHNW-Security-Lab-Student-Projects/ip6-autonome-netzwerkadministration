#!/usr/bin/env python3
import json
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv
import logfire
from fastmcp import FastMCP  # , Context
from failure_log import log_command_failure, log_transport_failure
from session_dedup import CallTracker
from srl_jsonrpc import SrlJsonRpcError, SrlTransportError, get_connection, jrpc_cli, jrpc_get

env_file = Path(__file__).parent / ".env"
if env_file.exists():
    load_dotenv(env_file)

LOGFIRE_TOKEN = os.getenv("LOGFIRE_TOKEN")
if LOGFIRE_TOKEN:
    logfire.configure(
        token=LOGFIRE_TOKEN,
        service_name='MCP Server',
        console=False,  # Disable console output, keep cloud monitoring
    )
    logfire.instrument_mcp()
else:
    print("️LOGFIRE_TOKEN not found. Running without Logfire observability.")


mcp = FastMCP("MCP Server")

INVENTORY_DIR = Path(__file__).parent / "inventory"

SHOW_FAILURE_PATTERNS = (
    "Parsing error: Unknown token",
    "syntax error",
    "Error:",
)

# Hard cap on the serialized body of a JSON-RPC `get` response. A bare container
# path (e.g. `/interface`) can return tens of KB and flood the LLM context, so we
# truncate and nudge the model toward a narrower query instead. Kept deliberately
# tight: these payloads are re-sent on every loop of the agent, so a smaller cap
# compounds across the whole run (see agent_history.py for the history-side trim).
_MAX_GET_CHARS = 3000


def _detect_show_failure(output: str) -> str | None:
    """Return the matched pattern if `output` contains a show-command failure signal, else None."""
    lowered = output.lower()
    for pattern in SHOW_FAILURE_PATTERNS:
        if pattern.lower() in lowered:
            return pattern
    return None


def _format_get_body(payload, path: str) -> str:
    """Serialize a `get` payload compactly and truncate if it exceeds the size cap.

    Compact separators (no indentation) roughly halve the token cost versus
    pretty-printing. On overflow, append a note steering the model to re-query a
    narrower path or a specific leaf.
    """
    if isinstance(payload, str):
        body = payload
    else:
        body = json.dumps(payload, separators=(",", ":"))
    if len(body) <= _MAX_GET_CHARS:
        return body
    return (
        body[:_MAX_GET_CHARS]
        + f"\n\n…[truncated: response for {path!r} exceeded {_MAX_GET_CHARS} chars. "
        "Re-query a narrower path or a specific leaf, e.g. '<container>[name=*]/<leaf>'.]"
    )


def load_inventory():
    """Load device inventory from YAML files."""
    with open(INVENTORY_DIR / "hosts.yaml") as f:
        hosts = yaml.safe_load(f)
    with open(INVENTORY_DIR / "defaults.yaml") as f:
        defaults = yaml.safe_load(f)
    return hosts, defaults


@mcp.tool()
async def execute_show_command(device_name: str, command: str) -> str:
    """
    Execute a show command on a network device.

    Args:
        device_name: Device name from inventory (e.g., 'Node1', 'Node2')
        command: CLI command to execute (e.g., 'show version', 'show interface brief')

    Returns:
        Command output as string
    """
    try:
        conn = get_connection(device_name)
    except ValueError as exc:
        return f"Error executing command on {device_name}: {exc}"

    try:
        results = await jrpc_cli(conn, [command], output_format='json')
        raw = results[0] if results else ''
        output = raw if isinstance(raw, str) else json.dumps(raw, separators=(",", ":"))
    except SrlJsonRpcError:
        try:
            results = await jrpc_cli(conn, [command], output_format='text')
            raw = results[0] if results else ''
            output = raw if isinstance(raw, str) else str(raw)
        except SrlTransportError as exc:
            log_transport_failure(
                agent='network-agent',
                device=device_name,
                command=command,
                error_text=str(exc),
            )
            return f"Error executing command on {device_name}: {exc}"
        except SrlJsonRpcError as exc:
            log_command_failure(
                agent='network-agent',
                device=device_name,
                command=command,
                error_type='jsonrpc_error',
                error_text=str(exc),
            )
            return f"Error executing command on {device_name}: {exc}"

    matched = _detect_show_failure(output)
    if matched:
        log_command_failure(
            agent='network-agent',
            device=device_name,
            command=command,
            error_type='parsing_error',
            error_text=output.strip(),
        )

    return f"Command: {command}\nDevice: {device_name}\n\n{output}"


@mcp.tool()
async def get_state_path(device_name: str, path: str) -> str:
    """Read raw operational state at a YANG path via JSON-RPC `get`.

    Prefer this over execute_show_command when you already know the YANG path
    you want. Accepts native YANG path notation including `[name=<value>]` list
    keys — the syntax restrictions of the SR Linux CLI parser do NOT apply here.

    ALWAYS query the narrowest path that answers your question. Do NOT request a
    bare container like '/interface' — it returns every item with every counter
    and will be truncated at 6000 chars. For an overview across many items, query
    a specific leaf with a wildcard key (the YANG equivalent of a `brief` show),
    e.g. '/interface[name=*]/oper-state' rather than '/interface'.

    Args:
        device_name: Device name from inventory (e.g., 'router1', 'switch1').
        path: YANG state path. Good: '/interface[name=ethernet-1/1]/oper-state',
              '/interface[name=*]/oper-state', '/system/information'.
              Avoid bare containers like '/interface' or '/network-instance'.

    Returns:
        Compact JSON result (truncated past 6000 chars), or an error message.
    """
    try:
        conn = get_connection(device_name)
    except ValueError as exc:
        return f"Error reading {path} on {device_name}: {exc}"

    try:
        results = await jrpc_get(conn, [path], datastore='state')
    except SrlTransportError as exc:
        log_transport_failure(
            agent='network-agent',
            device=device_name,
            command=f'GET state {path}',
            error_text=str(exc),
        )
        return f"Error reading {path} on {device_name}: {exc}"
    except SrlJsonRpcError as exc:
        log_command_failure(
            agent='network-agent',
            device=device_name,
            command=f'GET state {path}',
            error_type='jsonrpc_get_error',
            error_text=str(exc),
        )
        return f"Error reading {path} on {device_name}: {exc}"

    payload = results[0] if results else None
    body = _format_get_body(payload, path)
    return f"GET state {path}\nDevice: {device_name}\n\n{body}"


@mcp.tool()
async def get_config_path(device_name: str, path: str) -> str:
    """Read raw running config at a YANG path via JSON-RPC `get`.

    Same notation as get_state_path; reads the `running` datastore instead
    of `state`. Use when you need the configured value (not the operational
    one) — e.g. confirming an admin-state or a configured neighbor exists.

    ALWAYS query the narrowest path that answers your question. Do NOT request a
    bare container like '/interface' — it returns every item and will be
    truncated at 6000 chars. For an overview across many items, query a specific
    leaf with a wildcard key, e.g. '/interface[name=*]/admin-state'.

    Args:
        device_name: Device name from inventory.
        path: YANG config path, e.g. '/network-instance[name=default]/protocols/bgp'.
              Avoid bare containers like '/interface' or '/network-instance'.

    Returns:
        Compact JSON result (truncated past 6000 chars), or an error message.
    """
    try:
        conn = get_connection(device_name)
    except ValueError as exc:
        return f"Error reading {path} on {device_name}: {exc}"

    try:
        results = await jrpc_get(conn, [path], datastore='running')
    except SrlTransportError as exc:
        log_transport_failure(
            agent='network-agent',
            device=device_name,
            command=f'GET running {path}',
            error_text=str(exc),
        )
        return f"Error reading {path} on {device_name}: {exc}"
    except SrlJsonRpcError as exc:
        log_command_failure(
            agent='network-agent',
            device=device_name,
            command=f'GET running {path}',
            error_type='jsonrpc_get_error',
            error_text=str(exc),
        )
        return f"Error reading {path} on {device_name}: {exc}"

    payload = results[0] if results else None
    body = _format_get_body(payload, path)
    return f"GET running {path}\nDevice: {device_name}\n\n{body}"


_device_info_tracker = CallTracker()


@mcp.tool()
def get_device_info(device_name: str) -> str:
    """
    Get information about a network device from inventory.

    Args:
        device_name: Device name from inventory (e.g., 'Node1', 'Node2')

    Returns:
        Device information as string
    """
    try:
        hosts, _ = load_inventory()
        if device_name not in hosts:
            return f"Device '{device_name}' not found. Available devices: {', '.join(hosts.keys())}"

        if _device_info_tracker.seen_before(device_name):
            return (
                f"(Info for {device_name!r} already returned this session — inventory is "
                "static. Re-read the earlier response in context instead of fetching it again.)"
            )

        device = hosts[device_name]
        return f"Device: {device_name}\nHostname: {device['hostname']}\nPlatform: {device['platform']}"
    except Exception as e:
        return f"Error getting device info: {str(e)}"


_list_devices_tracker = CallTracker()


@mcp.tool()
def list_all_devices() -> str:
    """
    Get a list of all available network devices in inventory.

    Returns:
        Complete list of devices with their platform and hostname information
    """
    try:
        if _list_devices_tracker.seen_before():
            return (
                "(Device inventory already returned this session — it is static. "
                "Re-read the earlier response in context instead of fetching it again.)"
            )
        hosts, _ = load_inventory()
        inventory_lines = ["Network Device Inventory:\n"]
        for name, info in hosts.items():
            inventory_lines.append(f"- {name}: {info['platform']} ({info['hostname']})")
        return "\n".join(inventory_lines)
    except Exception as e:
        return f"Error loading inventory: {str(e)}"



if __name__ == "__main__":
    mcp.run()
