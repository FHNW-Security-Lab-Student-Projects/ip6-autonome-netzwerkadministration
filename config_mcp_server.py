#!/usr/bin/env python3
"""MCP Server — Network Configuration Tools

Exposes configuration tools for Nokia SR Linux devices via FastMCP (stdio transport).
Consumed as a subprocess by config_agent.py via MCPServerStdio.

Tools:
  - list_all_devices     : list inventory devices
  - get_device_info      : device details from inventory
  - get_command_reference: SR Linux configuration command reference
  - validate_config      : enter candidate, run commands, show diff, discard (safe preview)
  - backup_config        : create timestamped JSON backup to config_backups/
  - list_backups         : list available backups for a device
  - apply_config         : auto-backup + enter candidate + apply commands + commit

User approval is handled at the orchestrator level — this server does NOT use MCP
elicitation. apply_config must only be called after the user has approved the diff
shown by validate_config.

Run standalone for testing:
  uv run config_mcp_server.py
"""

import json
import os
from datetime import datetime
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
        service_name='Config MCP Server',
        console=False,
    )
    logfire.instrument_mcp()
else:
    print('LOGFIRE_TOKEN not found. Running without Logfire observability.')

mcp = FastMCP('Config MCP Server')

INVENTORY_DIR = Path(__file__).parent / 'inventory'
BACKUP_DIR = Path(__file__).parent / 'config_backups'
SR_LINUX_KNOWLEDGE_PATH = Path(__file__).parent / 'sr_linux_knowledge.txt'
CORRECTIONS_PATH = Path(__file__).parent / 'command_corrections.md'


# ---------------------------------------------------------------------------
# Inventory + SSH helpers
# ---------------------------------------------------------------------------

def _load_inventory() -> tuple[dict, dict]:
    with open(INVENTORY_DIR / 'hosts.yaml') as f:
        hosts = yaml.safe_load(f)
    with open(INVENTORY_DIR / 'defaults.yaml') as f:
        defaults = yaml.safe_load(f)
    return hosts, defaults


def _connect(device_name: str) -> ConnectHandler:
    hosts, defaults = _load_inventory()
    if device_name not in hosts:
        raise ValueError(
            f"Device '{device_name}' not found. Available: {list(hosts.keys())}"
        )
    device = hosts[device_name]
    return ConnectHandler(
        device_type=device['platform'],
        host=device['hostname'],
        username=defaults['username'],
        password=defaults['password'],
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _backup_internal(device_name: str, conn: ConnectHandler) -> str:
    """Create a timestamped JSON backup of running config. Returns backup file path."""
    device_backup_dir = BACKUP_DIR / device_name
    device_backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    backup_path = device_backup_dir / f'{timestamp}_backup.json'

    raw_json = conn.send_command('info from running | as json', read_timeout=30)

    try:
        json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'Device returned invalid JSON for backup: {exc}') from exc

    hosts, _ = _load_inventory()
    hostname = hosts[device_name]['hostname']

    backup_path.write_text(
        f'# SR Linux Configuration Backup\n'
        f'# Device: {device_name}  Hostname: {hostname}\n'
        f'# Timestamp: {timestamp}\n'
        f'{"─" * 80}\n'
        f'JSON_START\n{raw_json}\nJSON_END\n'
    )
    logfire.info('Config backup created', device=device_name, path=str(backup_path))
    return str(backup_path)


def _validate_internal(
    conn: ConnectHandler, config_commands: list[str]
) -> tuple[str, list[str]]:
    """Enter candidate, run commands, capture diff, discard. Returns (diff, cmd_outputs)."""
    conn.send_command_timing('enter candidate')
    cmd_outputs = []
    for cmd in config_commands:
        out = conn.send_command_timing(cmd)
        out_str = out if isinstance(out, str) else str(out)
        cmd_outputs.append(f'  {cmd}\n  → {out_str.strip() or "(ok)"}')
    diff = conn.send_command_timing('diff')
    conn.send_command_timing('discard now')
    return diff, cmd_outputs


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
        CORRECTIONS_PATH.write_text('# SR Linux Command Reference Corrections\n\nEntries below were flagged by agents during operation.\n')
    entry = (
        f'\n## [{timestamp}] config-agent\n'
        f'**Command:** `{command}`\n'
        f'**Issue:** {issue}\n'
    )
    with open(CORRECTIONS_PATH, 'a') as f:
        f.write(entry)
    return 'Issue reported. A human will review this entry in command_corrections.md.'


@mcp.tool()
def validate_config(device_name: str, config_commands: list[str]) -> str:
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
        with _connect(device_name) as conn:
            diff, cmd_outputs = _validate_internal(conn, config_commands)
        result = f'Validation preview for {device_name}\n'
        result += 'Commands:\n' + '\n'.join(cmd_outputs) + '\n\n'
        result += f'Diff (what would change):\n{diff or "(no changes detected)"}\n'
        result += 'Status: NOT applied — changes discarded safely.\n'
        logfire.info('Config validated', device=device_name, commands=config_commands)
        return result
    except Exception as exc:
        return f'Validation error on {device_name}: {exc}'


@mcp.tool()
def backup_config(device_name: str) -> str:
    """Create a timestamped JSON backup of the current running configuration.

    Args:
        device_name: Device name from inventory.

    Returns:
        Path to the created backup file.
    """
    try:
        with _connect(device_name) as conn:
            path = _backup_internal(device_name, conn)
        return f'Backup created: {path}'
    except Exception as exc:
        return f'Backup error on {device_name}: {exc}'


@mcp.tool()
def list_backups(device_name: str) -> str:
    """List available configuration backups for a device, newest first.

    Args:
        device_name: Device name from inventory.
    """
    device_backup_dir = BACKUP_DIR / device_name
    if not device_backup_dir.exists():
        return f'No backups found for {device_name}.'
    files = sorted(
        device_backup_dir.glob('*_backup.json'),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return f'No backups found for {device_name}.'
    lines = [f'Backups for {device_name} ({len(files)} total):']
    for f in files:
        ts = datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
        lines.append(f'  {f.name}  [{ts}]  {f}')
    return '\n'.join(lines)


@mcp.tool()
def apply_config(device_name: str, config_commands: list[str]) -> str:
    """Apply configuration commands to a device and commit.

    Automatically creates a backup before making any changes.

    IMPORTANT: Only call this after the user has explicitly approved the validated
    diff returned by validate_config. Do NOT call this speculatively.

    Args:
        device_name: Device name from inventory.
        config_commands: List of SR Linux 'set ...' commands (exact same commands
                         that were validated). Do NOT include 'enter candidate',
                         'commit now', or 'discard'.

    Returns:
        Result including backup path, commands applied, diff, and commit status.
    """
    try:
        with _connect(device_name) as conn:
            # Auto-backup before any changes
            try:
                backup_path = _backup_internal(device_name, conn)
                backup_msg = f'Backup created: {backup_path}'
            except Exception as be:
                backup_msg = f'Warning: backup failed: {be}'

            # Apply in candidate mode
            conn.send_command_timing('enter candidate')
            cmd_outputs = []
            for cmd in config_commands:
                out = conn.send_command_timing(cmd)
                out_str = out if isinstance(out, str) else str(out)
                cmd_outputs.append(f'  {cmd}\n  → {out_str.strip() or "(ok)"}')

            diff = conn.send_command_timing('diff')
            commit_out = conn.send_command_timing('commit now')
            commit_str = commit_out if isinstance(commit_out, str) else str(commit_out)

            success_marker = 'All changes have been committed.'
            if success_marker not in commit_str:
                conn.send_command_timing('discard now')
                logfire.error('Config commit failed', device=device_name)
                return (
                    f'COMMIT FAILED on {device_name}\n\n'
                    f'{backup_msg}\n\n'
                    f'Commands:\n' + '\n'.join(cmd_outputs) + '\n\n'
                    f'Diff:\n{diff}\n\n'
                    f'Commit output:\n{commit_str}\n\n'
                    f'Changes discarded. Device is in a clean state.\n'
                    f'Review the error and correct the commands before retrying.'
                )

        logfire.info('Config applied', device=device_name, commands=config_commands)
        return (
            f'Configuration applied successfully to {device_name}\n\n'
            f'{backup_msg}\n\n'
            f'Commands:\n' + '\n'.join(cmd_outputs) + '\n\n'
            f'Changes applied (diff):\n{diff}\n\n'
            f'Commit result:\n{commit_str}'
        )
    except Exception as exc:
        return f'Error configuring {device_name}: {exc}'


if __name__ == '__main__':
    mcp.run()
