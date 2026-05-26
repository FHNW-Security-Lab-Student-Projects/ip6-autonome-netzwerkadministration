"""Round 2 of fix verification — refining based on Round 1 results.

Round 1 takeaways:
- bracket key syntax [name=...] doesn't work in CLI (quoted or unquoted)
- space-separated key works: `info detail /network-instance default`
- `/system/information` as a single token fails — slashes inside the path
  aren't parsed; need to break into separate words.
- ping with no count probably hits httpx 30s timeout -> "Transport error".
"""
import asyncio
from srl_jsonrpc import get_connection, jrpc_cli, SrlJsonRpcError, close_client


CASES: list[tuple[str, str, str]] = [
    # /system/information variants
    ("router1", "info from state system information",        "system info: no leading slash, space-separated"),
    ("router1", "info from state / system information",      "system info: explicit root + spaces"),
    ("router1", "info from state /system",                   "system info: just /system, see what's under it"),

    # Multi-segment with slashes — does CLI accept slashes between containers?
    ("router1", "info from state /interface ethernet-1/1 oper-state",       "interface oper-state via space chain"),
    ("router1", "info from state /interface ethernet-1/1 statistics",       "interface statistics via space chain"),

    # network-instance nested
    ("router1", "info from state /network-instance default oper-state",     "NI oper-state via space chain"),
    ("router1", "info from state /network-instance default protocols bgp neighbor", "NI bgp neighbors"),

    # ping with explicit count -- does it succeed when bounded?
    ("switch2", "ping 172.20.20.6 network-instance mgmt -c 2",              "ping with -c 2 (bounded)"),
    ("router1", "ping 172.20.20.2 network-instance mgmt -c 2",              "ping bounded on router1 too"),
]


async def main() -> None:
    for device, cmd, note in CASES:
        try:
            conn = get_connection(device)
        except ValueError as exc:
            print(f"[{device}] inventory error: {exc}\n")
            continue

        print(f"=== [{device}] {note}")
        print(f"    $ {cmd}")
        try:
            results = await jrpc_cli(conn, [cmd], output_format="json")
            raw = results[0] if results else ""
            text = raw if isinstance(raw, str) else str(raw)
            preview = text.strip().splitlines()[:8]
            print("    OK -> " + ("\n           ".join(preview) if preview else "<empty>"))
        except SrlJsonRpcError as exc:
            try:
                results = await jrpc_cli(conn, [cmd], output_format="text")
                raw = results[0] if results else ""
                text = raw if isinstance(raw, str) else str(raw)
                preview = text.strip().splitlines()[:8]
                print("    OK(text) -> " + ("\n             ".join(preview) if preview else "<empty>"))
            except SrlJsonRpcError as exc2:
                print(f"    FAIL -> {exc2}")
        print()

    await close_client()


if __name__ == "__main__":
    asyncio.run(main())
