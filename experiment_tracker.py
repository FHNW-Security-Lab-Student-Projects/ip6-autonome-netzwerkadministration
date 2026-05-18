"""Experiment tracker for LLM comparison research.

Records per-session metrics (tokens, cost, latency, tool calls) for each user turn.
Supports structured comparison of different LLMs on network troubleshooting tasks.

Outputs:
  - experiment_log.jsonl  — one JSON record per session, append-only, for offline analysis
  - Logfire span          — emitted per session so metrics are queryable via SQL dashboard

Typical usage (REPL path — full session):
    session = begin_session(user_query="...", model="z-ai/glm-5", scenario="bgp-flap")
    t0 = time.monotonic()
    result = await some_agent.run(request)
    record_agent_run(session, "network_agent", "z-ai/glm-5", result, time.monotonic() - t0)
    finish_session(session, success=True)

In web UI the session ContextVar is set per-request; sub-agent tool calls check it automatically.

Pricing table:
    Update MODEL_PRICING from https://openrouter.ai/models as prices change.
    Cost estimates are computed from token counts captured via Pydantic AI result.usage().
    To cross-check against actual OpenRouter billing:
        GET https://openrouter.ai/api/v1/auth/key  (returns total credits used)
"""

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import logfire
from pydantic_ai import AgentRunResult
from pydantic_ai.messages import ModelResponse, ToolCallPart

EXPERIMENT_LOG = Path(__file__).parent / 'experiment_log.jsonl'

# (input_usd_per_1m_tokens, output_usd_per_1m_tokens)
# Prices from https://openrouter.ai/models — update regularly.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    'z-ai/glm-5': (0.0, 0.0),
    'z-ai/glm-5.1': (0.0, 0.0),
    'anthropic/claude-sonnet-4-5': (3.0, 15.0),
    'anthropic/claude-sonnet-4.6': (3.0, 15.0),
    'anthropic/claude-opus-4-5': (15.0, 75.0),
    'anthropic/claude-opus-4.7': (15.0, 75.0),
    'openai/gpt-4o': (2.5, 10.0),
    'openai/gpt-4o-mini': (0.15, 0.6),
    'openai/o3-mini': (1.1, 4.4),
    'google/gemini-2.0-flash-001': (0.075, 0.3),
    'google/gemini-2.5-flash': (0.15, 0.6),
    'google/gemini-2.5-pro': (1.25, 10.0),
    'mistralai/mistral-large': (2.0, 6.0),
    'mistralai/mistral-small': (0.1, 0.3),
    'meta-llama/llama-3.3-70b-instruct': (0.12, 0.3),
    'meta-llama/llama-3.1-405b-instruct': (2.0, 2.0),
    'qwen/qwen-2.5-72b-instruct': (0.13, 0.4),
    'deepseek/deepseek-chat': (0.14, 0.28),
    'deepseek/deepseek-r1': (0.55, 2.19),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from token counts using the static pricing table."""
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        for k, v in MODEL_PRICING.items():
            if model.startswith(k):
                pricing = v
                break
    if pricing is None:
        return 0.0
    in_price, out_price = pricing
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


def _count_tool_calls(result: AgentRunResult) -> int:
    """Count MCP/tool calls made across all messages in an agent run."""
    count = 0
    for msg in result.all_messages():
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    count += 1
    return count


@dataclass
class AgentRunRecord:
    agent_name: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    llm_requests: int = 0   # number of LLM API round-trips (>1 when tool loops occur)
    tool_calls: int = 0     # number of MCP/tool call invocations
    duration_s: float = 0.0
    estimated_cost_usd: float = 0.0


@dataclass
class ExperimentSession:
    session_id: str
    started_at: str         # ISO 8601
    model: str              # model used for all agents in this session
    user_query: str
    scenario: str           # optional label for structured experiments (e.g. "bgp-flap-01")
    agent_runs: list[AgentRunRecord] = field(default_factory=list)
    # Filled by finish_session()
    duration_s: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_tool_calls: int = 0
    total_llm_requests: int = 0
    success: bool = True
    error: str = ''


_session_start_times: dict[str, float] = {}


def begin_session(
    user_query: str,
    model: str,
    scenario: str = '',
) -> ExperimentSession:
    """Start a new experiment session. Call before running the orchestrator."""
    session_id = uuid.uuid4().hex
    _session_start_times[session_id] = time.monotonic()
    if not scenario:
        scenario = os.getenv('EXPERIMENT_SCENARIO', '')
    return ExperimentSession(
        session_id=session_id,
        started_at=datetime.now(timezone.utc).isoformat(),
        model=model,
        user_query=user_query,
        scenario=scenario,
    )


def record_agent_run(
    session: ExperimentSession,
    agent_name: str,
    model: str,
    result: AgentRunResult,
    duration_s: float,
) -> None:
    """Record one sub-agent or orchestrator run into the active session."""
    usage = result.usage()
    tool_calls = _count_tool_calls(result)
    cost = estimate_cost(model, usage.input_tokens or 0, usage.output_tokens or 0)
    session.agent_runs.append(AgentRunRecord(
        agent_name=agent_name,
        model=model,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        llm_requests=usage.requests or 0,
        tool_calls=tool_calls,
        duration_s=round(duration_s, 3),
        estimated_cost_usd=round(cost, 8),
    ))


def finish_session(
    session: ExperimentSession,
    success: bool = True,
    error: str = '',
) -> None:
    """Aggregate totals, persist to JSONL, and emit a Logfire span."""
    t0 = _session_start_times.pop(session.session_id, None)
    session.duration_s = round(time.monotonic() - t0, 3) if t0 else 0.0
    session.success = success
    session.error = error

    for run in session.agent_runs:
        session.total_input_tokens += run.input_tokens
        session.total_output_tokens += run.output_tokens
        session.total_tool_calls += run.tool_calls
        session.total_llm_requests += run.llm_requests
        session.total_cost_usd += run.estimated_cost_usd
    session.total_cost_usd = round(session.total_cost_usd, 8)

    EXPERIMENT_LOG.parent.mkdir(exist_ok=True)
    with open(EXPERIMENT_LOG, 'a') as f:
        f.write(json.dumps(asdict(session)) + '\n')

    logfire.info(
        'experiment_session',
        session_id=session.session_id,
        scenario=session.scenario,
        model=session.model,
        user_query=session.user_query[:300],
        duration_s=session.duration_s,
        total_input_tokens=session.total_input_tokens,
        total_output_tokens=session.total_output_tokens,
        total_cost_usd=session.total_cost_usd,
        total_tool_calls=session.total_tool_calls,
        total_llm_requests=session.total_llm_requests,
        agent_count=len(session.agent_runs),
        agents_invoked=[r.agent_name for r in session.agent_runs],
        success=session.success,
        error=session.error,
    )