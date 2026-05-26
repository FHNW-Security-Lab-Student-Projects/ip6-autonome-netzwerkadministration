"""Verify YANG paths via JSON-RPC `get` against a live SR Linux device.

Use this when you want to confirm a YANG path resolves and returns the
expected shape — independent of the SR Linux CLI parser, which is much
fussier than `get`.

Edit PATHS below to add your own. Defaults cover the syntax forms that
were proven to work in the 2026-05-26 verification round (slash-joined
paths, list keys with `[name=...]`, slashes inside list-key values, etc.).

Run inside the devcontainer:
    docker exec -w /workspaces/ip6 <devcontainer-name> .venv/bin/python _verify_jrpc_get.py
"""
from __future__ import annotations

import argparse
import asyncio
import json

from srl_jsonrpc import SrlJsonRpcError, close_client, get_connection, jrpc_get


# (device, path, datastore, note) — datastore is 'state' or 'running'
PATHS: list[tuple[str, str, str, str]] = [
    ("router1", "/system/information",                                  "state",   "bare slash-joined path"),
    ("router1", "/interface[name=ethernet-1/1]",                        "state",   "list key with slash in value"),
    ("router1", "/interface[name=ethernet-1/1]/oper-state",             "state",   "scalar leaf under list key"),
    ("router1", "/interface[name=ethernet-1/1]/statistics",             "state",   "container under list key"),
    ("router1", "/network-instance[name=default]",                      "state",   "list key, no leaf"),
    ("router1", "/network-instance[name=default]/protocols/bgp",        "state",   "nested container under list key"),
    ("router1", "/system/lldp/interface[name=ethernet-1/1]/neighbor",   "state",   "deep path with bracket key"),
    ("router1", "/interface",                                           "state",   "top-level list, no key"),
    ("router1", "/interface[name=mgmt0]",                               "running", "config datastore read"),
]


def _truncate(obj, limit: int = 200) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj)
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "..."


async def _run_one(device: str, path: str, datastore: str) -> tuple[str, str]:
    try:
        conn = get_connection(device)
    except ValueError as exc:
        return "BAD_DEVICE", str(exc)
    try:
        result = await jrpc_get(conn, [path], datastore=datastore)
        return "OK", _truncate(result[0] if result else None)
    except SrlJsonRpcError as exc:
        return "FAIL", _truncate(str(exc))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="override device for all PATHS entries")
    args = parser.parse_args()

    summary = {"OK": 0, "FAIL": 0, "BAD_DEVICE": 0}
    for device, path, ds, note in PATHS:
        dev = args.device or device
        status, msg = await _run_one(dev, path, ds)
        summary[status] += 1
        print(f"[{dev}] {note}")
        print(f"    path: {path}  (datastore={ds})")
        print(f"    {status} -> {msg}\n")

    print("--- Summary ---")
    for k, v in summary.items():
        print(f"  {k:<11}: {v}")

    await close_client()


if __name__ == "__main__":
    asyncio.run(main())
