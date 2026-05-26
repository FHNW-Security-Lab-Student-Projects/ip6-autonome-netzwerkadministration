"""Append-only JSONL log of detected SR Linux command failures.

Written by the MCP servers when a device response indicates a command went wrong.
Replaces the old LLM-driven `report_command_issue` tool.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

FAILURE_LOG_PATH = Path(__file__).parent / 'command_failures.jsonl'


def log_command_failure(
    *,
    agent: str,
    device: str,
    command: str,
    error_type: str,
    error_text: str,
) -> None:
    """Append one failure record to command_failures.jsonl."""
    entry = {
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'agent': agent,
        'device': device,
        'command': command,
        'error_type': error_type,
        'error_text': error_text,
    }
    with open(FAILURE_LOG_PATH, 'a') as f:
        f.write(json.dumps(entry) + '\n')
