"""Shared OpenRouter model settings for all agents.

Centralises two cross-cutting concerns so they stay consistent across every
agent and experiment run:

- ``openrouter_reasoning`` — a *fixed* reasoning effort (not ``enabled``).
  A fixed level keeps model-vs-model experiments comparable: every model
  reasons at the same controlled depth instead of each provider picking its
  own default, which can also drift over time. Re-tune all agents at once by
  changing ``REASONING_EFFORT`` here.
- ``openrouter_usage`` — detailed usage/cost accounting in every response, so
  the experiment tracker logs real OpenRouter cost data rather than estimates.
"""

from pydantic_ai.models.openrouter import OpenRouterModelSettings, OpenRouterReasoning

# Fixed reasoning effort applied to every agent. One of:
# 'xhigh' | 'high' | 'medium' | 'low' | 'minimal' | 'none'.
REASONING: OpenRouterReasoning = {'effort': 'high'}

# Ask OpenRouter to return detailed usage/cost info on every response.
USAGE_ACCOUNTING = {'include': True}


def agent_model_settings(
    *, parallel_tool_calls: bool = False, timeout: float = 180
) -> OpenRouterModelSettings:
    """Return shared settings: fixed reasoning effort + usage accounting.

    Args:
        parallel_tool_calls: Allow the model to emit tool calls in parallel.
        timeout: Per-request timeout in seconds.
    """
    settings = OpenRouterModelSettings(
        timeout=timeout,
        openrouter_reasoning=REASONING,
        openrouter_usage=USAGE_ACCOUNTING,
    )
    if parallel_tool_calls:
        settings['parallel_tool_calls'] = True
    return settings
