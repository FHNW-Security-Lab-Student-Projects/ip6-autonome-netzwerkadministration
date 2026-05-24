#!/usr/bin/env python3
import json
import os
import yaml
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import logfire
from fastmcp import FastMCP  # , Context
import yang_index
from srl_jsonrpc import SrlJsonRpcError, get_connection, jrpc_cli

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
CORRECTIONS_PATH = Path(__file__).parent / "command_corrections.md"


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


@mcp.tool()
def search_yang_paths(keyword: str, domain: str = "") -> str:
    """Search SR Linux YANG model paths by keyword.

    Use this when you need to find the correct gNMI/CLI state path for a concept
    that isn't in the command reference cheat sheet.

    Args:
        keyword: Concept to search for, e.g. 'neighbor', 'oper-state', 'tx-power', 'bgp'.
        domain:  Optional folder filter to narrow results, e.g. 'interfaces', 'bgp',
                 'platform', 'routing-policy', 'network-instance'. Leave empty to search all.

    Returns:
        Matching paths with data type and config/state label.
        config = writable configuration leaf
        state  = read-only operational state leaf  (use with 'info from state <path>')
    """
    results = yang_index.search(keyword, domain or None, max_results=40)
    return yang_index.format_results(results)


@mcp.tool()
def report_command_issue(command: str, issue: str) -> str:
    """Report a command from the SR Linux command reference that did not work as documented.

    Call this when a command from the reference produces an unexpected error or behaves
    differently than the reference describes. Do NOT call for errors caused by wrong device
    state, missing config, or permission issues — only for commands that appear incorrect
    in the reference itself.

    Args:
        command: The exact command string that failed or behaved unexpectedly.
        issue: Description of what went wrong and what the actual device response was.
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if not CORRECTIONS_PATH.exists():
        CORRECTIONS_PATH.write_text("# SR Linux Command Reference Corrections\n\nEntries below were flagged by agents during operation.\n")
    entry = (
        f"\n## [{timestamp}] network-agent\n"
        f"**Command:** `{command}`\n"
        f"**Issue:** {issue}\n"
    )
    with open(CORRECTIONS_PATH, 'a') as f:
        f.write(entry)
    return "Issue reported. A human will review this entry in command_corrections.md."


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
            return f"Error executing command on {device_name}: {exc}"

    return f"Command: {command}\nDevice: {device_name}\n\n{output}"


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
