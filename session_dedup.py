"""Session-scoped dedup helpers for MCP tools.

Some tools return content that is *static within a session* — a reference file,
the YANG model, device inventory. Calling them repeatedly with the same arguments
re-sends an identical payload and floods the LLM context for no new information.

A `CallTracker` records prior calls (keyed by the tool's arguments) so a tool can
return a short pointer instead of the full payload on a repeat. The backing dict
is bounded so a long session can't grow it without limit.
"""

_DEFAULT_LIMIT = 32

# Sentinel key for tools that take no arguments (return-once behaviour).
NO_ARGS = "__no_args__"


class CallTracker:
    """Counts how often a key has been seen, with a bounded FIFO cache.

    `counts` is exposed directly so callers can `.clear()` it (handy in tests).
    """

    def __init__(self, limit: int = _DEFAULT_LIMIT):
        self.counts: dict = {}
        self.limit = limit

    def record(self, key) -> int:
        """Return the prior call count for `key` (0 if new), then record this call."""
        prior = self.counts.get(key, 0)
        self.counts[key] = prior + 1
        if len(self.counts) > self.limit:
            oldest = next(iter(self.counts))
            del self.counts[oldest]
        return prior

    def seen_before(self, key=NO_ARGS) -> bool:
        """Convenience for return-once tools: True if `key` was already recorded."""
        return self.record(key) > 0