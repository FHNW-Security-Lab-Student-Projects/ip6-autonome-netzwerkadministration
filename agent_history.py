"""Shared history processing for token-heavy sub-agents.

A sub-agent run loops the model over its full message history on every request.
Each loop re-sends every prior tool return verbatim, so large `get`/show payloads
pile up: observed `network_agent` runs reached ~190k median and ~340k peak input
tokens for a single query. Beyond cost, that buries the salient evidence in a
haystack and dilutes the model's attention.

`compact_tool_history` keeps recent and small tool returns intact but collapses older
oversized ones to a one-line stub. The model still sees that the call happened (tool
name + original size) and can re-issue it if the body is needed again, while the bulk
of the stale payload stops being re-sent every turn.

Register on an agent via:

    from pydantic_ai.capabilities import ProcessHistory
    from agent_history import compact_tool_history

    Agent(..., capabilities=[ProcessHistory(processor=compact_tool_history)])
"""

from __future__ import annotations

from dataclasses import replace

import logfire
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart

# Tool returns at or below this many characters are always kept verbatim. Small
# results are cheap to carry and are often the decisive evidence (a single oper-state
# leaf, an error string), so collapsing them would lose signal for no real saving.
KEEP_VERBATIM_BELOW = 600

# Always keep this many of the most recent oversized tool returns untouched. The
# latest results are what the model is actively reasoning over; only history beyond
# this window gets stubbed.
KEEP_RECENT = 3

# Tool returns whose body contains any of these markers are device-reported failures.
# They carry the correction (what was wrong + valid options) the model needs to avoid
# repeating the same bad call, so they are kept verbatim regardless of size or age.
ERROR_MARKERS = ("Parsing error:", "Error reading")


def _is_error_return(part: ToolReturnPart) -> bool:
    """True if `part` is a string tool return carrying a device failure message."""
    return isinstance(part.content, str) and any(m in part.content for m in ERROR_MARKERS)


def _stub(part: ToolReturnPart) -> ToolReturnPart:
    """Return a copy of `part` with its large string body replaced by a short note."""
    body = part.content
    if not isinstance(body, str):
        # Non-string content (multimodal etc.) is left as-is — we only know how to
        # safely measure and elide plain text.
        return part
    note = (
        f'[earlier result for {part.tool_name} elided to save context '
        f'({len(body)} chars). Re-call the tool if you need this output again.]'
    )
    return replace(part, content=note)


def compact_tool_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Collapse older oversized tool-return payloads to a one-line stub.

    Every small tool return (<= KEEP_VERBATIM_BELOW chars), every device error
    return (see ERROR_MARKERS), and the most recent KEEP_RECENT oversized returns
    are left untouched. Older oversized returns are replaced with a stub naming the
    tool and the elided size.

    A new message list is returned; the input messages are not mutated, so persisted
    histories (e.g. syslog investigations) are unaffected.

    Emits a Logfire log per call describing what was compacted. Because this runs
    before each model request, those logs nest under the agent run in the trace tree,
    so you can see exactly which tool outputs were stubbed and how much was saved.
    """
    # Locate oversized string tool returns in chronological order.
    oversized: list[tuple[int, int]] = []  # (message_index, part_index)
    for mi, msg in enumerate(messages):
        if not isinstance(msg, ModelRequest):
            continue
        for pi, part in enumerate(msg.parts):
            if (
                isinstance(part, ToolReturnPart)
                and isinstance(part.content, str)
                and len(part.content) > KEEP_VERBATIM_BELOW
                and not _is_error_return(part)
            ):
                oversized.append((mi, pi))

    if len(oversized) <= KEEP_RECENT:
        # Nothing trimmed — emit a debug breadcrumb so "no-op" turns are still visible
        # (debug is filtered out by default, so this stays quiet unless you look for it).
        logfire.debug(
            'compact_tool_history: no-op ({oversized} oversized <= keep_recent={keep})',
            oversized=len(oversized),
            keep=KEEP_RECENT,
        )
        return messages

    to_stub = set(oversized[:-KEEP_RECENT])

    # Build the new history and record, per stubbed return, what was elided.
    stubbed: list[dict] = []
    chars_before = 0
    chars_after = 0
    new_messages: list[ModelMessage] = []
    for mi, msg in enumerate(messages):
        if not isinstance(msg, ModelRequest):
            new_messages.append(msg)
            continue
        new_parts = []
        for pi, part in enumerate(msg.parts):
            if (mi, pi) in to_stub:
                stub = _stub(part)
                stubbed.append({
                    'tool': part.tool_name,
                    'chars_before': len(part.content),
                    'chars_after': len(stub.content),
                    'preview': part.content[:120],
                })
                chars_before += len(part.content)
                chars_after += len(stub.content)
                new_parts.append(stub)
            else:
                new_parts.append(part)
        new_messages.append(replace(msg, parts=new_parts))

    logfire.info(
        'compacted {stubbed_count} tool outputs (~{chars_saved} chars saved, '
        '{kept} most-recent kept verbatim)',
        stubbed_count=len(stubbed),
        chars_saved=chars_before - chars_after,
        chars_before=chars_before,
        chars_after=chars_after,
        kept=KEEP_RECENT,
        oversized_total=len(oversized),
        stubbed=stubbed,
    )
    return new_messages
