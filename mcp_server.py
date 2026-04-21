#!/usr/bin/env python3
import os
import yaml
# import json
from pathlib import Path
# from datetime import datetime
from dotenv import load_dotenv
import logfire
from netmiko import ConnectHandler
from fastmcp import FastMCP  # , Context

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
# BACKUP_DIR = Path(__file__).parent / "config_backups"


def load_inventory():
    """Load device inventory from YAML files."""
    with open(INVENTORY_DIR / "hosts.yaml") as f:
        hosts = yaml.safe_load(f)
    with open(INVENTORY_DIR / "defaults.yaml") as f:
        defaults = yaml.safe_load(f)
    return hosts, defaults


def connect_to_device(device_name: str):
    """
    Establish connection to a network device.

    Args:
        device_name: Device name from inventory (e.g., 'switch1', 'router1')

    Returns:
        Netmiko connection object
    """
    hosts, defaults = load_inventory()

    if device_name not in hosts:
        raise ValueError(f"Device '{device_name}' not found in inventory. Available: {list(hosts.keys())}")

    device_info = hosts[device_name]

    connection_params = {
        'device_type': device_info['platform'],
        'host': device_info['hostname'],
        'username': defaults['username'],
        'password': defaults['password'],
    }

    return ConnectHandler(**connection_params)


@mcp.tool()
def get_command_reference() -> str:
    """
    Return the SR Linux read-only command reference.
    Call this before constructing any SR Linux CLI command to ensure correct syntax.
    """
    return COMMAND_REFERENCE_PATH.read_text()


@mcp.tool()
def execute_show_command(device_name: str, command: str) -> str:
    """
    Execute a show command on a network device.

    Args:
        device_name: Device name from inventory (e.g., 'Node1', 'Node2')
        command: CLI command to execute (e.g., 'show version', 'show interface brief')

    Returns:
        Command output as string
    """
    try:
        with connect_to_device(device_name) as conn:
            output = conn.send_command(command)
            return f"Command: {command}\nDevice: {device_name}\n\n{output}"
    except Exception as e:
        return f"Error executing command on {device_name}: {str(e)}"


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
