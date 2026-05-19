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
    _tracked_http_client,
    main_lifespan,
    orchestrator,
)
from experiment_tracker import (
    AgentRunRecord,
    ExperimentSession,
    begin_session,
    capture_generation_ids,
    finalize_costs,
    finish_session,
    record_agent_run,
)


def _build_model(model_name: str) -> OpenAIChatModel:
    return OpenAIChatModel(
        model_name,
        provider=OpenRouterProvider(api_key=OPENROUTER_API_KEY, http_client=_tracked_http_client),
        settings=ModelSettings(timeout=180),
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


def _print_results(
    sessions: list[ExperimentSession],
    outputs: list[str],
    model: str,
    scenario: str,
) -> None:
    W = 72
    successful = [s for s in sessions if s.success]

    print(f'\n{"═" * W}')
    print(f'  RESULTS  |  model: {model}  |  scenario: {scenario or "(none)"}')
    print(f'{"═" * W}')

    for i, (session, output) in enumerate(zip(sessions, outputs), 1):
        q = session.user_query
        q_preview = q if len(q) <= W - 4 else q[:W - 7] + '...'
        print(f'\n  [{i}] {q_preview}')
        print(f'  {"─" * (W - 2)}')

        # Per-agent run breakdown
        for run in session.agent_runs:
            print(
                f'  {run.agent_name:<20}'
                f'  {run.input_tokens:>7,} in  {run.output_tokens:>6,} out'
                f'  {run.tool_calls} tools'
                f'  {run.llm_requests} reqs'
                f'  {run.duration_s:.1f}s'
            )

        # Session totals (tokens + cost from OpenRouter)
        status = 'OK' if session.success else f'ERROR: {session.error[:40]}'
        cost_str = f'${session.total_cost_usd:.6f}' if session.total_cost_usd else '$ -.------'
        print(f'  {"─" * (W - 2)}')
        print(
            f'  {session.total_input_tokens:,} in / {session.total_output_tokens:,} out'
            f'  |  {session.total_tool_calls} tools'
            f'  |  {session.duration_s:.1f}s'
            f'  |  {cost_str}'
            f'  |  {status}'
        )

        # LLM response
        if output:
            print()
            for line in output.splitlines():
                print(f'    {line}')

    # Overall summary
    print(f'\n{"═" * W}')
    print(f'  SUMMARY  |  {len(sessions)} turns  ({len(successful)} successful)')
    print(f'{"─" * W}')
    if successful:
        print(f'  Avg duration:  {sum(s.duration_s for s in successful) / len(successful):.1f}s')
        print(f'  Total tokens:  {sum(s.total_input_tokens for s in successful):,} in'
              f'  /  {sum(s.total_output_tokens for s in successful):,} out')
        print(f'  Total tools:   {sum(s.total_tool_calls for s in successful):,}')
        total_cost = sum(s.total_cost_usd for s in successful)
        print(f'  Total cost:    {"$" + f"{total_cost:.6f}" if total_cost else "unavailable"}')
    print(f'{"═" * W}')
    print(f'  Logged to: experiment_log.jsonl')
    print()


async def _run_turn(
    query: str,
    model_name: str,
    model: OpenAIChatModel,
    scenario: str,
    message_history: list,
) -> tuple[ExperimentSession, list[str], bool, str, str, list]:
    """Run one query. Returns (session, success, error, output, updated_history).

    Does NOT print anything — caller prints everything at the end.
    Does NOT call finish_session — caller batches cost fetching first.
    Gen_ids are stored inside each AgentRunRecord via record_agent_run().
    """
    session = begin_session(user_query=query, model=model_name, scenario=scenario)
    _active_session.set(session)

    try:
        with capture_generation_ids() as gen_ids:
            result = await orchestrator.run(query, model=model, message_history=message_history)
        record_agent_run(session, 'orchestrator', model_name, result, gen_ids)
        return session, True, '', result.output, result.all_messages()
    except APITimeoutError:
        return session, False, 'Orchestrator timed out after 3 minutes', '', message_history
    except Exception as exc:
        return session, False, str(exc), '', message_history


async def run(model_name: str, scenario: str, queries: list[str], multi_turn: bool) -> None:
    _active_model_name.set(model_name)
    model = _build_model(model_name)

    # (session, success, error, output)
    pending: list[tuple[ExperimentSession, bool, str, str]] = []
    message_history: list = []
    total = len(queries)

    print(f'Running {total} {"query" if total == 1 else "queries"}'
          f' with {model_name}'
          f'{f" [{scenario}]" if scenario else ""}...')

    async with main_lifespan():
        for i, query in enumerate(queries, 1):
            print(f'  [{i}/{total}] {query[:60]}{"..." if len(query) > 60 else ""}')
            session, success, error, output, message_history = await _run_turn(
                query, model_name, model, scenario,
                message_history if multi_turn else [],
            )
            pending.append((session, success, error, output))

        if not queries:
            print('\nInteractive mode — type your query and press Enter. Type "exit" to stop.\n')
            loop = asyncio.get_event_loop()
            turn = 0
            while True:
                try:
                    query = (await loop.run_in_executor(None, input, 'Query: ')).strip()
                except (KeyboardInterrupt, EOFError):
                    print('\nStopped.')
                    break
                if not query or query.lower() in ('exit', 'quit'):
                    break
                turn += 1
                session, success, error, output, message_history = await _run_turn(
                    query, model_name, model, scenario,
                    message_history if multi_turn else [],
                )
                pending.append((session, success, error, output))

    print('Fetching costs from OpenRouter...')
    sessions = [s for s, _, _, _ in pending]
    await finalize_costs(sessions, OPENROUTER_API_KEY)

    for session, success, error, _ in pending:
        finish_session(session, success=success, error=error)

    outputs = [o for _, _, _, o in pending]
    _print_results(sessions, outputs, model_name, scenario)


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
