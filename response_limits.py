"""Shared size cap for tool responses sent to sub-agents.

Tool returns are re-sent on every loop of an agent run, so a single oversized
payload (a broad JSON-RPC `get`, a wide show command, a full route-table diff)
compounds across the whole run — flooding the LLM context and diluting attention.
This module holds one hard char cap and the truncation helper so every tool that
returns device data applies the same limit.

This is the immediate, per-call cap. It is complementary to the pressure-driven
history compaction in `agent_history.py`, which only acts once the whole prompt
nears the context window.
"""

# Hard cap on the body of any tool response. A bare container path (e.g.
# `/interface`), a broad show command, or a large diff can return tens of KB.
MAX_RESPONSE_CHARS = 15000


def truncate(body: str, overflow_note: str) -> str:
    """Return `body` unchanged if within the size cap, else truncated with `overflow_note`."""
    if len(body) <= MAX_RESPONSE_CHARS:
        return body
    return body[:MAX_RESPONSE_CHARS] + overflow_note
