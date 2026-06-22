#!/usr/bin/env python3
"""MCP Server — Network Configuration Tools

Exposes configuration tools for Nokia SR Linux devices via FastMCP (stdio transport).
Consumed as a subprocess by config_agent.py via MCPServerStdio.

Tools:
  - list_all_devices     : list inventory devices
  - get_device_info      : device details from inventory
  - get_command_reference: SR Linux configuration command reference
  - validate_config      : enter candidate, run commands, show diff, discard (safe preview)
  - apply_config         : enter candidate + apply commands + commit

User approval is handled at the orchestrator level — this server does NOT use MCP
elicitation. apply_config must only be called after the user has approved the diff
shown by validate_config.

Run standalone for testing:
  uv run config_mcp_server.py
"""

import json
import os
from pathlib import Path

import logfire
import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP

from failure_log import log_command_failure
from srl_jsonrpc import SrlConnection, SrlJsonRpcError, get_connection, jrpc_cli

env_file = Path(__file__).parent / '.env'
if env_file.exists():
    load_dotenv(env_file)

LOGFIRE_TOKEN = os.getenv('LOGFIRE_TOKEN')
if LOGFIRE_TOKEN:
    logfire.configure(
        token=LOGFIRE_TOKEN,
        service_name='Config MCP Server',
        console=False,
    )
    logfire.instrument_mcp()
else:
    print('LOGFIRE_TOKEN not found. Running without Logfire observability.')

mcp = FastMCP('Config MCP Server')

INVENTORY_DIR = Path(__file__).parent / 'inventory'
SR_LINUX_KNOWLEDGE_PATH = Path(__file__).parent / 'sr_linux_knowledge.txt'


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------

def _load_inventory() -> tuple[dict, dict]:
    with open(INVENTORY_DIR / 'hosts.yaml') as f:
        hosts = yaml.safe_load(f)
    with open(INVENTORY_DIR / 'defaults.yaml') as f:
        defaults = yaml.safe_load(f)
    return hosts, defaults


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cli_result_to_text(raw: object) -> str:
    """JSON-RPC cli with output-format=text returns strings; be defensive anyway."""
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ''
    return json.dumps(raw, indent=2)


async def _run_candidate_sequence(
    conn: SrlConnection,
    config_commands: list[str],
    finalize: str,
) -> str:
    """Execute enter candidate → set ... → diff → <finalize> as one CLI batch.

    finalize is either 'discard now' (validation) or 'commit now' (apply).

    The JSON-RPC `cli` method with output-format=text returns the WHOLE batch as a
    single concatenated text blob (one result element, not one per command). The
    diff lines and the finalize banner (e.g. 'All changes have been committed.')
    all land in that blob, so we join everything and return it as one string rather
    than trying to index per command.
    """
    commands = ['enter candidate', *config_commands, 'diff', finalize]
    results = await jrpc_cli(conn, commands, output_format='text')
    return '\n'.join(_cli_result_to_text(r) for r in results if _cli_result_to_text(r)).strip()


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_all_devices() -> str:
    """Return all network devices from inventory with hostname and platform."""
    try:
        hosts, _ = _load_inventory()
        lines = ['Available devices:']
        for name, info in hosts.items():
            lines.append(f'  {name}: {info["platform"]} @ {info["hostname"]}')
        return '\n'.join(lines)
    except Exception as exc:
        return f'Error loading inventory: {exc}'


@mcp.tool()
def get_device_info(device_name: str) -> str:
    """Return hostname and platform for a specific device from inventory.

    Args:
        device_name: Device name from inventory (e.g. 'router1', 'switch1').
    """
    try:
        hosts, _ = _load_inventory()
        if device_name not in hosts:
            return f"Device '{device_name}' not found. Available: {list(hosts.keys())}"
        d = hosts[device_name]
        return f'Device: {device_name}\nHostname: {d["hostname"]}\nPlatform: {d["platform"]}'
    except Exception as exc:
        return f'Error: {exc}'


@mcp.tool()
def get_command_reference() -> str:
    """Return the SR Linux CLI and configuration command reference.

    Call this before constructing any configuration commands to verify correct syntax.
    """
    return SR_LINUX_KNOWLEDGE_PATH.read_text()


@mcp.tool()
async def validate_config(device_name: str, config_commands: list[str]) -> str:
    """Preview configuration changes in candidate mode WITHOUT committing.

    Enters candidate mode, applies the commands, captures the diff, then discards
    all changes. Safe to call — no changes are persisted to the device.

    Args:
        device_name: Device name from inventory (e.g. 'router1', 'switch1').
        config_commands: List of SR Linux 'set ...' commands.
                         Do NOT include 'enter candidate', 'commit now', or 'discard'.

    Returns:
        A summary of commands and the configuration diff showing what would change.
    """
    try:
        conn = get_connection(device_name)
        output = await _run_candidate_sequence(
            conn, config_commands, finalize='discard now'
        )
        result = f'Validation preview for {device_name}\n'
        result += 'Commands:\n  ' + '\n  '.join(config_commands) + '\n\n'
        result += f'Diff (what would change):\n{output or "(no changes detected)"}\n'
        result += 'Status: NOT applied — changes discarded safely.\n'
        logfire.info('Config validated', device=device_name, commands=config_commands)
        return result
    except Exception as exc:
        return f'Validation error on {device_name}: {exc}'


@mcp.tool()
async def apply_config(device_name: str, config_commands: list[str]) -> str:
    """Apply configuration commands to a device and commit.

    IMPORTANT: Only call this after the user has explicitly approved the validated
    diff returned by validate_config. Do NOT call this speculatively.

    Args:
        device_name: Device name from inventory.
        config_commands: List of SR Linux 'set ...' commands (exact same commands
                         that were validated). Do NOT include 'enter candidate',
                         'commit now', or 'discard'.

    Returns:
        Result including commands applied, diff, and commit status.
    """
    try:
        conn = get_connection(device_name)

        output = await _run_candidate_sequence(
            conn, config_commands, finalize='commit now'
        )

        # On success the device emits 'All changes have been committed.' in the batch
        # output blob. A real commit rejection is raised as SrlJsonRpcError by _call
        # (caught below). If neither the success banner nor an exception appears, treat
        # it as a failure rather than silently claiming success.
        success_marker = 'All changes have been committed.'
        if success_marker not in output:
            try:
                await jrpc_cli(conn, ['discard now'], output_format='text')
            except SrlJsonRpcError:
                pass
            log_command_failure(
                agent='config-agent',
                device=device_name,
                command='; '.join(config_commands),
                error_type='commit_failed',
                error_text=output.strip(),
            )
            logfire.error('Config commit failed', device=device_name)
            return (
                f'COMMIT FAILED on {device_name}\n\n'
                f'Commands:\n  ' + '\n  '.join(config_commands) + '\n\n'
                f'Device output:\n{output}\n\n'
                f'Changes discarded. Device is in a clean state.\n'
                f'Review the error and correct the commands before retrying.'
            )

        logfire.info('Config applied', device=device_name, commands=config_commands)
        return (
            f'Configuration applied successfully to {device_name}\n\n'
            f'Commands:\n  ' + '\n  '.join(config_commands) + '\n\n'
            f'Device output (diff + commit):\n{output}'
        )
    except Exception as exc:
        return f'Error configuring {device_name}: {exc}'


if __name__ == '__main__':
    mcp.run()
