"""Shared history processing for token-heavy sub-agents.

A sub-agent run loops the model over its full message history on every request.
Each loop re-sends every prior tool return verbatim, so large `get`/`show` payloads
and YANG search results pile up: observed `network_agent` runs reached ~190k median
and ~340k peak input tokens for a single query. Beyond cost, that buries the salient
evidence in a haystack and dilutes the model's attention.

`compact_tool_history` collapses older oversized tool returns to a one-line stub —
but only once a run is actually approaching the model's context window. The model
still sees that the call happened (tool name + original size) and can re-issue it if
the body is needed again, while the bulk of the stale payload stops being re-sent.

Trigger and aggressiveness are pressure-driven, not count-driven:

- Nothing is touched until the estimated prompt is at or above ``TRIGGER_FRACTION``
  of *this model's* real context window (fetched per-model via
  ``model_config.context_window_for``). Easy runs pass through byte-for-byte.
- Once over the trigger, the oldest oversized returns are stubbed one-by-one until
  the estimate drops back under ``TARGET_FRACTION`` — keeping the most recent
  verbatim context that still fits.

The token estimate anchors on the *actual* OpenRouter native prompt-token count from
the last model response (we enable usage accounting in ``model_config.py``), plus a
rough estimate of whatever was appended since. This is far more accurate than a pure
char heuristic and avoids ``ctx.usage`` (which accumulates across the whole run and
overshoots the live window).

NOTE on persistence: a Pydantic AI history processor *replaces* the run's stored
message history (``_agent_graph.py`` does ``ctx.state.message_history[:] = messages``),
so ``result.all_messages()`` returns the stubbed version. Agents that persist their
history (e.g. ``syslog_investigations``) will therefore store stubbed older payloads;
this is an accepted tradeoff — the model can re-call any tool whose body it needs.

Register on an agent via:

    from pydantic_ai.capabilities import ProcessHistory
    from agent_history import compact_tool_history

    Agent(..., capabilities=[ProcessHistory(processor=compact_tool_history)])
"""

from __future__ import annotations

import math
from dataclasses import replace

import logfire
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolReturnPart,
)

from model_config import context_window_for

# Tool returns at or below this many characters are always kept verbatim. Small
# results are cheap to carry and are often the decisive evidence (a single oper-state
# leaf, an error string), so collapsing them would lose signal for no real saving.
KEEP_VERBATIM_BELOW = 600

# Always keep this many of the most recent oversized tool returns untouched. The
# latest results are what the model is actively reasoning over; only history beyond
# this window is eligible for stubbing.
KEEP_RECENT = 3

# Tool returns whose body contains any of these markers are device-reported failures.
# They carry the correction (what was wrong + valid options) the model needs to avoid
# repeating the same bad call, so they are kept verbatim regardless of size or age.
ERROR_MARKERS = ("Parsing error:", "Error reading")

# Start stubbing once the estimated prompt reaches this fraction of the model's
# context window; stub oldest-first until back under TARGET_FRACTION. The gap leaves
# headroom for the verbatim recent tail plus the next few tool calls.
TRIGGER_FRACTION = 0.70
TARGET_FRACTION = 0.60

# Rough chars-per-token for estimating content we don't have an API token count for.
CHARS_PER_TOKEN = 4
# Pad char-based estimates up to stay conservative (under-estimating the window would
# delay compaction past the real limit; over-estimating only compacts a little early).
ESTIMATE_PAD = 4 / 3


def _is_error_return(part: ToolReturnPart) -> bool:
    """True if `part` is a string tool return carrying a device failure message."""
    return isinstance(part.content, str) and any(m in part.content for m in ERROR_MARKERS)


def _stub_note(part: ToolReturnPart) -> str:
    """The short replacement text for an elided tool return body."""
    return (
        f'[earlier result for {part.tool_name} elided to save context '
        f'({len(part.content)} chars). Re-call the tool if you need this output again.]'
    )


def _stub(part: ToolReturnPart) -> ToolReturnPart:
    """Return a copy of `part` with its large string body replaced by a short note.

    Only `content` changes — `tool_call_id`, `tool_name`, and the surrounding parts
    list are preserved, so every tool call stays paired with its return (slicing them
    apart is what the Pydantic AI docs warn breaks the LLM).
    """
    if not isinstance(part.content, str):
        # Non-string content (multimodal etc.) is left as-is — we only know how to
        # safely measure and elide plain text.
        return part
    return replace(part, content=_stub_note(part))


def _char_len(messages: list[ModelMessage]) -> int:
    """Total characters of text and string tool-return content across `messages`."""
    total = 0
    for msg in messages:
        for part in msg.parts:
            content = getattr(part, 'content', None)
            if isinstance(content, str):
                total += len(content)
    return total


def _estimate_prompt_tokens(messages: list[ModelMessage], model_name: str = '') -> int:
    """Estimate the token size of `messages` as a prompt.

    Anchors on the actual native prompt-token count from the most recent
    `ModelResponse` (OpenRouter usage accounting fills `usage.input_tokens`), which
    exactly covers everything up to and including the request that produced it, then
    adds a padded char/4 estimate of whatever was appended afterwards.

    Falls back to a padded char/4 estimate of the whole history when no usable
    response usage is available (e.g. the first request, or usage missing). The
    fallback is logged so the degraded accuracy is visible — `model_name` is only
    used to tag those logs.
    """
    last_resp_index = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], ModelResponse):
            last_resp_index = i
            break

    if last_resp_index is not None:
        anchor = getattr(messages[last_resp_index].usage, 'input_tokens', 0) or 0
        if anchor:
            tail_chars = _char_len(messages[last_resp_index + 1:])
            est = anchor + math.ceil(tail_chars / CHARS_PER_TOKEN * ESTIMATE_PAD)
            # TEMP(anchor-debug): verify `anchor` is the native prompt-token count.
            # Compare this against OpenRouter's reported usage for the same turn.
            # Remove once confirmed.
            logfire.info(
                'TEMP anchor={anchor} tail_chars={tail_chars} est={est}',
                anchor=anchor,
                tail_chars=tail_chars,
                est=est,
                model=model_name,
                full_usage=getattr(messages[last_resp_index], 'usage', None),
            )
            return est

    # No anchor available — estimate the whole history from characters instead.
    fallback = math.ceil(_char_len(messages) / CHARS_PER_TOKEN * ESTIMATE_PAD)
    if last_resp_index is None:
        # Expected on the first request of a run (no response yet). Benign — the
        # history is tiny, so the char estimate is fine and won't mis-trigger.
        logfire.debug(
            'compact_tool_history: no anchor (no ModelResponse yet) — '
            'whole-history char estimate ~{est} tokens',
            est=fallback,
            messages=len(messages),
            model=model_name,
        )
    else:
        # A response exists but carries no usable `input_tokens`. Unexpected when
        # usage accounting is on (model_config.USAGE_ACCOUNTING) — surface it,
        # because the trigger silently degrades to a less-accurate char estimate
        # over the WHOLE history (where per-model tokenizer drift actually bites).
        logfire.warning(
            'compact_tool_history: last ModelResponse has no usage.input_tokens — '
            'falling back to whole-history char estimate ~{est} tokens. Check that '
            'OpenRouter usage accounting is enabled for this model.',
            est=fallback,
            messages=len(messages),
            model=model_name,
        )
    return fallback


async def compact_tool_history(
    ctx: RunContext, messages: list[ModelMessage]
) -> list[ModelMessage]:
    """Stub older oversized tool-return payloads once a run nears the context window.

    Returns a new message list; the input is not mutated. When the estimated prompt is
    below `TRIGGER_FRACTION` of the model's context window, the input list is returned
    unchanged. Otherwise the oldest oversized non-error returns (beyond the most recent
    `KEEP_RECENT`) are stubbed oldest-first until the estimate drops under
    `TARGET_FRACTION`.

    Emits a Logfire log per call. Because this runs before each model request, the log
    nests under the agent run in the trace tree.
    """
    window = await context_window_for(ctx.model.model_name)
    est_before = _estimate_prompt_tokens(messages, ctx.model.model_name)
    trigger = TRIGGER_FRACTION * window

    if est_before < trigger:
        logfire.debug(
            'compact_tool_history: no-op (est {est} < trigger {trigger} of {window})',
            est=est_before,
            trigger=int(trigger),
            window=window,
            model=ctx.model.model_name,
        )
        return messages

    # Locate oversized, non-error string tool returns in chronological order.
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

    # Never touch the most recent KEEP_RECENT oversized returns.
    candidates = oversized[:-KEEP_RECENT] if len(oversized) > KEEP_RECENT else []

    # Walk oldest-first, stubbing until the estimate drops back under the target.
    target = TARGET_FRACTION * window
    est = float(est_before)
    to_stub: set[tuple[int, int]] = set()
    for mi, pi in candidates:
        if est <= target:
            break
        part = messages[mi].parts[pi]
        to_stub.add((mi, pi))
        est -= max(0.0, (len(part.content) - len(_stub_note(part))) / CHARS_PER_TOKEN)

    if not to_stub:
        # Over the trigger but nothing eligible to stub (all oversized returns are
        # within the protected recent window). Surface it — this is the case where
        # the run is genuinely large and may still hit the limit.
        logfire.info(
            'compact_tool_history: over trigger but nothing eligible '
            '(est {est}, {oversized} oversized, keep_recent={keep})',
            est=est_before,
            oversized=len(oversized),
            keep=KEEP_RECENT,
            window=window,
            model=ctx.model.model_name,
        )
        return messages

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
        'est {est_before}->{est_after} of {window}, {kept} most-recent kept verbatim)',
        stubbed_count=len(stubbed),
        chars_saved=chars_before - chars_after,
        chars_before=chars_before,
        chars_after=chars_after,
        est_before=est_before,
        est_after=int(est),
        window=window,
        model=ctx.model.model_name,
        kept=KEEP_RECENT,
        oversized_total=len(oversized),
        stubbed=stubbed,
    )
    return new_messages
