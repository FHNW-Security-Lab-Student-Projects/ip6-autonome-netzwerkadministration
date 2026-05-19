#!/usr/bin/env python3
"""Experiment runner for LLM comparison research.

Runs queries through the full agent pipeline (orchestrator + all sub-agents) and
records complete token/cost/latency metrics for every turn — including the
orchestrator's own LLM calls, which are not available in the web UI path.

Results are appended to experiment_log.jsonl (same file as the web UI tracker).

Usage:
    # Interactive — type queries one by one, Ctrl-C or "exit" to stop
    uv run python experiment_runner.py --model anthropic/claude-sonnet-4.6 --scenario bgp-flap-01

    # Batch — run all queries from a plain-text file (one query per line, # = comment)
    uv run python experiment_runner.py --model z-ai/glm-5 --scenario bgp-flap-01 --file scenarios/bgp-flap.txt

    # Multi-turn — carry conversation history between queries in the same run
    uv run python experiment_runner.py --model z-ai/glm-5 --scenario bgp-multi-turn --file scenarios/bgp-flap.txt --multi-turn

    # Default model (z-ai/glm-5)
    uv run python experiment_runner.py --scenario quick-test

Available OpenRouter model IDs (examples):
    z-ai/glm-5                        anthropic/claude-sonnet-4.6
    z-ai/glm-5.1                      anthropic/claude-opus-4.7
    google/gemini-2.0-flash-001       openai/gpt-4o
    deepseek/deepseek-chat            mistralai/mistral-large
"""

import argparse
import asyncio
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import APITimeoutError
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

load_dotenv(Path(__file__).parent / '.env')

from client_agent import (
    DEFAULT_AGENT_MODEL,
    OPENROUTER_API_KEY,
    _active_model_name,
    _active_session,
    main_lifespan,
    orchestrator,
)
from experiment_tracker import (
    AgentRunRecord,
    ExperimentSession,
    begin_session,
    finish_session,
    record_agent_run,
)


def _build_model(model_name: str) -> OpenAIChatModel:
    return OpenAIChatModel(
        model_name,
        provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY),
        settings=ModelSettings(parallel_tool_calls=True, timeout=180),
    )


def _load_queries(path: str) -> list[str]:
    queries = []
    for line in Path(path).read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            queries.append(stripped)
    if not queries:
        raise ValueError(f'No queries found in {path} (empty file or all lines are comments)')
    return queries


def _print_turn_summary(turn: int, total: int, query: str, session: ExperimentSession) -> None:
    q_preview = query if len(query) <= 72 else query[:69] + '...'
    print(f'\n{"─" * 72}')
    print(f'  Turn {turn}/{total}  |  {q_preview}')
    print(f'{"─" * 72}')

    if session.agent_runs:
        header = f'  {"Agent":<20} {"Model":<32} {"In tok":>7} {"Out tok":>7} {"Tools":>5} {"s":>6}'
        print(header)
        print(f'  {"─"*20} {"─"*32} {"─"*7} {"─"*7} {"─"*5} {"─"*6}')
        for run in session.agent_runs:
            model_short = run.model[-32:] if len(run.model) > 32 else run.model
            print(
                f'  {run.agent_name:<20} {model_short:<32}'
                f' {run.input_tokens:>7,} {run.output_tokens:>7,}'
                f' {run.tool_calls:>5} {run.duration_s:>6.1f}'
            )

    print(f'{"─" * 72}')
    status = 'OK' if session.success else f'ERROR: {session.error[:50]}'
    cost_str = f'${session.total_cost_usd:.6f}' if session.total_cost_usd else '$ -.------'
    print(
        f'  Total: {session.total_input_tokens:,} in / {session.total_output_tokens:,} out'
        f'  |  {session.total_tool_calls} tool calls'
        f'  |  {session.duration_s:.1f}s'
        f'  |  {cost_str}'
        f'  |  {status}'
    )


def _print_final_summary(sessions: list[ExperimentSession], model: str, scenario: str) -> None:
    if not sessions:
        return

    successful = [s for s in sessions if s.success]
    print(f'\n{"═" * 72}')
    print(f'  EXPERIMENT SUMMARY')
    print(f'  Model:    {model}')
    print(f'  Scenario: {scenario or "(none)"}')
    print(f'  Turns:    {len(sessions)}  ({len(successful)} successful)')
    print(f'{"═" * 72}')

    if successful:
        avg_duration  = sum(s.duration_s for s in successful) / len(successful)
        total_in      = sum(s.total_input_tokens for s in successful)
        total_out     = sum(s.total_output_tokens for s in successful)
        total_tools   = sum(s.total_tool_calls for s in successful)
        total_cost    = sum(s.total_cost_usd for s in successful)

        print(f'  Avg duration:    {avg_duration:.1f}s')
        print(f'  Total tokens:    {total_in:,} in  /  {total_out:,} out')
        print(f'  Total tool calls:{total_tools:,}')
        cost_str = f'${total_cost:.6f}' if total_cost else '$ -.------ (model not in pricing table)'
        print(f'  Estimated cost:  {cost_str}')

    print(f'{"═" * 72}')
    print(f'  Results appended to: experiment_log.jsonl')
    print()


async def _run_turn(
    query: str,
    model_name: str,
    model: OpenAIChatModel,
    scenario: str,
    message_history: list,
) -> tuple[ExperimentSession, list]:
    """Run one query through the full pipeline and return (session, updated_history)."""
    session = begin_session(user_query=query, model=model_name, scenario=scenario)
    _active_session.set(session)

    success, error = True, ''
    try:
        t0 = time.monotonic()
        result = await orchestrator.run(query, model=model, message_history=message_history)
        duration = time.monotonic() - t0
        # Record the orchestrator's own LLM usage (not available via web UI path).
        record_agent_run(session, 'orchestrator', model_name, result, duration)
        print(f'\n  {result.output}')
        return session, result.all_messages()
    except APITimeoutError:
        success, error = False, 'Orchestrator timed out after 3 minutes'
        print(f'\n  [timeout] The orchestrator did not respond within 3 minutes.')
        return session, message_history
    except Exception as exc:
        success, error = False, str(exc)
        print(f'\n  [error] {exc}')
        return session, message_history
    finally:
        finish_session(session, success=success, error=error)


async def run(model_name: str, scenario: str, queries: list[str], multi_turn: bool) -> None:
    _active_model_name.set(model_name)
    model = _build_model(model_name)
    sessions: list[ExperimentSession] = []
    message_history: list = []
    total = len(queries)

    print(f'\nModel:    {model_name}')
    print(f'Scenario: {scenario or "(none)"}')
    print(f'Queries:  {total if total else "interactive"}')
    print(f'Multi-turn history: {"yes" if multi_turn else "no"}')

    async with main_lifespan():
        turn = 0
        for query in queries:
            turn += 1
            print(f'\n[{turn}/{total}] {query}')
            session, message_history = await _run_turn(
                query, model_name, model, scenario,
                message_history if multi_turn else [],
            )
            sessions.append(session)
            _print_turn_summary(turn, total, query, session)

        # Interactive mode when no file was provided (queries is empty here)
        if not queries:
            print('\nInteractive mode — type your query and press Enter. Type "exit" to stop.\n')
            loop = asyncio.get_event_loop()
            while True:
                try:
                    query = (await loop.run_in_executor(None, input, 'Query: ')).strip()
                except (KeyboardInterrupt, EOFError):
                    print('\nStopped.')
                    break
                if not query:
                    continue
                if query.lower() in ('exit', 'quit'):
                    break
                turn += 1
                session, message_history = await _run_turn(
                    query, model_name, model, scenario,
                    message_history if multi_turn else [],
                )
                sessions.append(session)
                _print_turn_summary(turn, turn, query, session)

    _print_final_summary(sessions, model_name, scenario)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Run network troubleshooting queries and record full LLM metrics.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--model', '-m',
        default=DEFAULT_AGENT_MODEL,
        metavar='MODEL_ID',
        help=f'OpenRouter model ID (default: {DEFAULT_AGENT_MODEL})',
    )
    parser.add_argument(
        '--scenario', '-s',
        default=os.getenv('EXPERIMENT_SCENARIO', ''),
        metavar='LABEL',
        help='Scenario label written to every session record (default: $EXPERIMENT_SCENARIO or "")',
    )
    parser.add_argument(
        '--file', '-f',
        metavar='PATH',
        help='Plain-text file of queries — one per line, # lines are ignored. '
             'Omit for interactive mode.',
    )
    parser.add_argument(
        '--multi-turn',
        action='store_true',
        help='Carry conversation history between queries in the same run.',
    )
    args = parser.parse_args()

    queries: list[str] = []
    if args.file:
        queries = _load_queries(args.file)
        print(f'Loaded {len(queries)} queries from {args.file}')

    asyncio.run(run(
        model_name=args.model,
        scenario=args.scenario,
        queries=queries,
        multi_turn=args.multi_turn,
    ))


if __name__ == '__main__':
    main()
