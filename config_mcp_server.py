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
from session_dedup import CallTracker
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
) -> tuple[str, list[str], str]:
    """Execute enter candidate → set ... → diff → <finalize> as one CLI batch.

    finalize is either 'discard now' (validation) or 'commit now' (apply).

    Returns (diff_text, formatted_cmd_outputs, finalize_output).
    """
    commands = ['enter candidate', *config_commands, 'diff', finalize]
    results = await jrpc_cli(conn, commands, output_format='text')

    n = len(config_commands)
    cmd_results = results[1:1 + n]
    diff_raw = results[1 + n] if len(results) > 1 + n else ''
    finalize_raw = results[2 + n] if len(results) > 2 + n else ''

    cmd_outputs: list[str] = []
    for cmd, raw in zip(config_commands, cmd_results):
        out_str = _cli_result_to_text(raw).strip() or '(ok)'
        cmd_outputs.append(f'  {cmd}\n  → {out_str}')

    return _cli_result_to_text(diff_raw), cmd_outputs, _cli_result_to_text(finalize_raw)


async def _validate_internal(
    conn: SrlConnection, config_commands: list[str]
) -> tuple[str, list[str]]:
    """Run commands in candidate, capture diff, discard. Returns (diff, cmd_outputs)."""
    diff, cmd_outputs, _ = await _run_candidate_sequence(
        conn, config_commands, finalize='discard now'
    )
    return diff, cmd_outputs


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

_list_devices_tracker = CallTracker()
_device_info_tracker = CallTracker()
_command_reference_tracker = CallTracker()


@mcp.tool()
def list_all_devices() -> str:
    """Return all network devices from inventory with hostname and platform."""
    try:
        if _list_devices_tracker.seen_before():
            return (
                '(Device inventory already returned this session — it is static. '
                'Re-read the earlier response in context instead of fetching it again.)'
            )
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
        if _device_info_tracker.seen_before(device_name):
            return (
                f'(Info for {device_name!r} already returned this session — inventory is '
                'static. Re-read the earlier response in context instead of fetching it again.)'
            )
        d = hosts[device_name]
        return f'Device: {device_name}\nHostname: {d["hostname"]}\nPlatform: {d["platform"]}'
    except Exception as exc:
        return f'Error: {exc}'


@mcp.tool()
def get_command_reference() -> str:
    """Return the SR Linux CLI and configuration command reference.

    Call this before constructing any configuration commands to verify correct syntax.
    """
    if _command_reference_tracker.seen_before():
        return (
            '(Command reference already returned this session — contents are static. '
            'Re-read the earlier response in context instead of fetching it again.)'
        )
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
        diff, cmd_outputs = await _validate_internal(conn, config_commands)
        result = f'Validation preview for {device_name}\n'
        result += 'Commands:\n' + '\n'.join(cmd_outputs) + '\n\n'
        result += f'Diff (what would change):\n{diff or "(no changes detected)"}\n'
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

        diff, cmd_outputs, commit_str = await _run_candidate_sequence(
            conn, config_commands, finalize='commit now'
        )

        success_marker = 'All changes have been committed.'
        if success_marker not in commit_str:
            try:
                await jrpc_cli(conn, ['discard now'], output_format='text')
            except SrlJsonRpcError:
                pass
            log_command_failure(
                agent='config-agent',
                device=device_name,
                command='; '.join(config_commands),
                error_type='commit_failed',
                error_text=commit_str.strip(),
            )
            logfire.error('Config commit failed', device=device_name)
            return (
                f'COMMIT FAILED on {device_name}\n\n'
                f'Commands:\n' + '\n'.join(cmd_outputs) + '\n\n'
                f'Diff:\n{diff}\n\n'
                f'Commit output:\n{commit_str}\n\n'
                f'Changes discarded. Device is in a clean state.\n'
                f'Review the error and correct the commands before retrying.'
            )

        logfire.info('Config applied', device=device_name, commands=config_commands)
        return (
            f'Configuration applied successfully to {device_name}\n\n'
            f'Commands:\n' + '\n'.join(cmd_outputs) + '\n\n'
            f'Changes applied (diff):\n{diff}\n\n'
            f'Commit result:\n{commit_str}'
        )
    except Exception as exc:
        return f'Error configuring {device_name}: {exc}'


if __name__ == '__main__':
    mcp.run()
