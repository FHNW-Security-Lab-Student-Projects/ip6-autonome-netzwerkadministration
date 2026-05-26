"""Re-run commands from command_failures.jsonl against live devices.

Use this to figure out whether each failure was:
  - a genuinely invalid command (model hallucination / bad doc)  -> still fails
  - a transient issue (transport, timeout, device down)          -> now succeeds
  - fixable with a syntax tweak                                  -> add to
    FIX_CANDIDATES below and see whether the rewrite succeeds

Run from inside the devcontainer (it owns the clab network):
    docker exec -w /workspaces/ip6 <devcontainer-name> .venv/bin/python verify_failed_commands.py

Optional CLI flags:
    --no-failures        skip replaying command_failures.jsonl
    --candidates-only    only run FIX_CANDIDATES (alias for --no-failures)
    --max N              cap how many failures to replay (default: all)
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from srl_jsonrpc import SrlJsonRpcError, close_client, get_connection, jrpc_cli

FAILURES_FILE = Path(__file__).parent / "command_failures.jsonl"

# Add (device, command, note) tuples here to test syntax-fix hypotheses
# alongside the replay of command_failures.jsonl.
FIX_CANDIDATES: list[tuple[str, str, str]] = [
    # Examples kept from the 2026-05-26 round of fixes — feel free to edit.
    ("router1", "info from state /interface ethernet-1/1",
     "space-separated key replaces [name=ethernet-1/1]"),
    ("router1", "info detail /network-instance default",
     "space-separated key replaces [name=default]"),
    ("router1", "info from state system information",
     "replacement for hallucinated `show system information`"),
    ("switch2", "ping 172.20.20.6 network-instance mgmt -c 2",
     "bounded ping (avoids httpx 30s timeout that surfaced as Transport error)"),
]


def _truncate(text: str, limit: int = 180) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


async def _run_one(device: str, command: str) -> tuple[str, str]:
    """Return (status, message). status ∈ {'OK','OK(text)','FAIL','BAD_DEVICE'}."""
    try:
        conn = get_connection(device)
    except ValueError as exc:
        return "BAD_DEVICE", str(exc)

    try:
        results = await jrpc_cli(conn, [command], output_format="json")
        raw = results[0] if results else ""
        return "OK", _truncate(raw if isinstance(raw, str) else json.dumps(raw))
    except SrlJsonRpcError as exc:
        # Fall back to text output — some commands have no JSON form
        try:
            results = await jrpc_cli(conn, [command], output_format="text")
            raw = results[0] if results else ""
            return "OK(text)", _truncate(raw if isinstance(raw, str) else str(raw))
        except SrlJsonRpcError as exc2:
            return "FAIL", _truncate(str(exc2))


def _load_failures(path: Path, max_entries: int | None) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if max_entries is not None:
        entries = entries[:max_entries]
    return entries


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-failures", action="store_true",
                        help="skip replaying command_failures.jsonl")
    parser.add_argument("--candidates-only", action="store_true",
                        help="alias for --no-failures")
    parser.add_argument("--max", type=int, default=None,
                        help="cap on number of failure entries replayed")
    args = parser.parse_args()

    skip_failures = args.no_failures or args.candidates_only

    summary = {"OK": 0, "OK(text)": 0, "FAIL": 0, "BAD_DEVICE": 0}

    if not skip_failures:
        failures = _load_failures(FAILURES_FILE, args.max)
        print(f"--- Replaying {len(failures)} entries from {FAILURES_FILE.name} ---\n")
        for entry in failures:
            device = entry.get("device", "?")
            command = entry.get("command", "")
            ts = entry.get("timestamp", "")
            original_err_type = entry.get("error_type", "?")
            status, msg = await _run_one(device, command)
            summary[status] += 1
            verdict = {
                "OK":         "STILL OK     -- transient failure, command itself is fine",
                "OK(text)":   "STILL OK     -- transient failure (text-only output)",
                "FAIL":       "STILL FAILS  -- command genuinely invalid",
                "BAD_DEVICE": "BAD DEVICE   -- not in inventory",
            }[status]
            print(f"[{ts}] {device}  ({original_err_type})")
            print(f"    $ {command}")
            print(f"    {verdict}")
            print(f"    -> {msg}\n")

    if FIX_CANDIDATES:
        print(f"--- Running {len(FIX_CANDIDATES)} fix candidates ---\n")
        for device, command, note in FIX_CANDIDATES:
            status, msg = await _run_one(device, command)
            summary[status] += 1
            print(f"[{device}]  {note}")
            print(f"    $ {command}")
            print(f"    {status}")
            print(f"    -> {msg}\n")

    print("--- Summary ---")
    for k, v in summary.items():
        print(f"  {k:<11}: {v}")

    await close_client()


if __name__ == "__main__":
    asyncio.run(main())
