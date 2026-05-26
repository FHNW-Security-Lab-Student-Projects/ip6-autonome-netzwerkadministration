#!/usr/bin/env python3
import json
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv
import logfire
from fastmcp import FastMCP  # , Context
import yang_index
from failure_log import log_command_failure
from srl_jsonrpc import SrlJsonRpcError, get_connection, jrpc_cli, jrpc_get

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
COMMAND_REFERENCE_PATH = Path(__file__).parent / "command-references" / "srlinux-24.10.1-agent-context.txt"

SHOW_FAILURE_PATTERNS = (
    "Parsing error: Unknown token",
    "syntax error",
    "Error:",
)


def _detect_show_failure(output: str) -> str | None:
    """Return the matched pattern if `output` contains a show-command failure signal, else None."""
    lowered = output.lower()
    for pattern in SHOW_FAILURE_PATTERNS:
        if pattern.lower() in lowered:
            return pattern
    return None


def load_inventory():
    """Load device inventory from YAML files."""
    with open(INVENTORY_DIR / "hosts.yaml") as f:
        hosts = yaml.safe_load(f)
    with open(INVENTORY_DIR / "defaults.yaml") as f:
        defaults = yaml.safe_load(f)
    return hosts, defaults


@mcp.tool()
def get_command_reference() -> str:
    """
    Return the SR Linux read-only command reference.
    Call this before constructing any SR Linux CLI command to ensure correct syntax.
    """
    return COMMAND_REFERENCE_PATH.read_text()


_RECENT_SEARCH_KEYS: dict[tuple[str, str], int] = {}
_RECENT_SEARCH_LIMIT = 32


def _remember_search(key: tuple[str, str]) -> int:
    """Return the prior call count for `key` (0 if new), then record this call."""
    prior = _RECENT_SEARCH_KEYS.get(key, 0)
    _RECENT_SEARCH_KEYS[key] = prior + 1
    if len(_RECENT_SEARCH_KEYS) > _RECENT_SEARCH_LIMIT:
        oldest = next(iter(_RECENT_SEARCH_KEYS))
        del _RECENT_SEARCH_KEYS[oldest]
    return prior


def _search_yang_paths_impl(keyword: str, domain: str = "") -> str:
    """Plain (non-MCP) entrypoint for testing. Applies dedup + formatting."""
    key = (keyword.lower(), (domain or "").lower())
    prior = _remember_search(key)
    if prior > 0:
        return (
            f"(Already searched keyword={keyword!r} domain={domain!r} this session — "
            "results unchanged. Re-read the earlier response in context, or call "
            "list_yang_children(<path>) to drill into a specific container.)"
        )
    results = yang_index.search(keyword, domain or None, max_results=40)
    return yang_index.format_results(results)


@mcp.tool()
def search_yang_paths(keyword: str, domain: str = "") -> str:
    """Search SR Linux YANG model paths by keyword.

    Use this when you need to find the correct gNMI/CLI state path for a concept
    that isn't in the command reference cheat sheet.

    YANG uses kebab-case names (e.g. 'oper-state', 'route-table', 'admin-state').
    Search individual path segments, not full paths.

    For navigating *into* a known path (listing its child containers/lists),
    prefer list_yang_children — it's far cheaper than a broad keyword search.

    Args:
        keyword: YANG path segment or concept to search for.
                 Good examples: 'oper-state', 'neighbor', 'lldp', 'route-table',
                 'bgp', 'isis', 'ospf', 'prefix', 'mac-table', 'lag', 'vlan', 'mtu'.
                 Use short, specific terms — not full sentences.
        domain:  Optional folder to narrow results. Valid values:
                 acl, bfd, ethcfm, grpc, interfaces, network-instance, oam,
                 platform, qos, routing-policy, sync, system, transport-security,
                 tunnel, twamp.
                 Note: BGP, ISIS, OSPF, and MPLS live under 'network-instance'.
                 Leave empty to search all domains.

    Returns:
        Matching paths grouped by parent container, with type and config/state
        label. config = writable; state = read-only operational.
    """
    return _search_yang_paths_impl(keyword, domain)


@mcp.tool()
def list_yang_children(path: str) -> str:
    """List the immediate child segments (containers, lists, leaves) of a YANG path.

    Use this for navigation when you already know a path and want to see what's
    underneath it — far cheaper than a broad keyword search, and the answer to
    questions like "what lives under /network-instance[name=*]/route-table?".

    Accepts native YANG path notation. Concrete list keys (e.g. `[name=default]`)
    are normalised internally to the wildcard form (`[name=*]`).

    Args:
        path: YANG container or list path. Examples:
              '/interface[name=*]'
              '/network-instance[name=default]/route-table'
              '/system/lldp'

    Returns:
        Each immediate child on its own line, annotated as list (`[]`),
        container (`/`) or leaf, with the count of leaves below it.
    """
    children = yang_index.list_children(path)
    return yang_index.format_children(path, children)


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
        output = raw if isinstance(raw, str) else json.dumps(raw, indent=2)
    except SrlJsonRpcError:
        try:
            results = await jrpc_cli(conn, [command], output_format='text')
            raw = results[0] if results else ''
            output = raw if isinstance(raw, str) else str(raw)
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

    Prefer this over execute_show_command when you already know (or have
    looked up via search_yang_paths) the YANG path you want. Accepts native
    YANG path notation including `[name=<value>]` list keys — the syntax
    restrictions of the SR Linux CLI parser do NOT apply here.

    Args:
        device_name: Device name from inventory (e.g., 'router1', 'switch1').
        path: YANG state path, e.g. '/interface[name=ethernet-1/1]/oper-state'
              or '/system/information'.

    Returns:
        JSON-formatted result, or an error message.
    """
    try:
        conn = get_connection(device_name)
    except ValueError as exc:
        return f"Error reading {path} on {device_name}: {exc}"

    try:
        results = await jrpc_get(conn, [path], datastore='state')
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
    body = json.dumps(payload, indent=2) if not isinstance(payload, str) else payload
    return f"GET state {path}\nDevice: {device_name}\n\n{body}"


@mcp.tool()
async def get_config_path(device_name: str, path: str) -> str:
    """Read raw running config at a YANG path via JSON-RPC `get`.

    Same notation as get_state_path; reads the `running` datastore instead
    of `state`. Use when you need the configured value (not the operational
    one) — e.g. confirming an admin-state or a configured neighbor exists.

    Args:
        device_name: Device name from inventory.
        path: YANG config path, e.g. '/network-instance[name=default]/protocols/bgp'.

    Returns:
        JSON-formatted result, or an error message.
    """
    try:
        conn = get_connection(device_name)
    except ValueError as exc:
        return f"Error reading {path} on {device_name}: {exc}"

    try:
        results = await jrpc_get(conn, [path], datastore='running')
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
    body = json.dumps(payload, indent=2) if not isinstance(payload, str) else payload
    return f"GET running {path}\nDevice: {device_name}\n\n{body}"


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

        device = hosts[device_name]
        return f"Device: {device_name}\nHostname: {device['hostname']}\nPlatform: {device['platform']}"
    except Exception as e:
        return f"Error getting device info: {str(e)}"


@mcp.tool()
def list_all_devices() -> str:
    """
    Get a list of all available network devices in inventory.

    Returns:
        Complete list of devices with their platform and hostname information
    """
    try:
        hosts, _ = load_inventory()
        inventory_lines = ["Network Device Inventory:\n"]
        for name, info in hosts.items():
            inventory_lines.append(f"- {name}: {info['platform']} ({info['hostname']})")
        return "\n".join(inventory_lines)
    except Exception as e:
        return f"Error loading inventory: {str(e)}"



if __name__ == "__main__":
    mcp.run()
