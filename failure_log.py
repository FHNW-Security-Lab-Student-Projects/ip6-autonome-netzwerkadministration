"""Append-only JSONL log of detected SR Linux command failures.

Written by the MCP servers when a device response indicates a command went wrong.
Replaces the old LLM-driven `report_command_issue` tool.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

FAILURE_LOG_PATH = Path(__file__).parent / 'command_failures.jsonl'
# Infrastructure failures (connection/transport/HTTP) go here, kept OUT of
# command_failures.jsonl so they never count as 'invalid commands'. Lets an
# experiment flag a lab-flakiness-contaminated run rather than scoring it clean.
TRANSPORT_LOG_PATH = Path(__file__).parent / 'transport_failures.jsonl'


def _append(path: Path, entry: dict) -> None:
    with open(path, 'a') as f:
        f.write(json.dumps(entry) + '\n')


def log_command_failure(
    *,
    agent: str,
    device: str,
    command: str,
    error_type: str,
    error_text: str,
) -> None:
    """Append one command-content failure record to command_failures.jsonl.

    For device-rejected commands/paths only (the caller's fault). Connection and
    transport problems must go to log_transport_failure instead.
    """
    _append(FAILURE_LOG_PATH, {
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'agent': agent,
        'device': device,
        'command': command,
        'error_type': error_type,
        'error_text': error_text,
    })


def log_transport_failure(
    *,
    agent: str,
    device: str,
    command: str,
    error_text: str,
) -> None:
    """Append one infrastructure failure record to transport_failures.jsonl.

    Connection/transport/HTTP problems — NOT the caller's fault, never counted as
    an invalid command.
    """
    _append(TRANSPORT_LOG_PATH, {
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'agent': agent,
        'device': device,
        'command': command,
        'error_type': 'transport_error',
        'error_text': error_text,
    })
