#!/usr/bin/env python3
"""Generate a comparison report from experiment_log.jsonl.

Reads all session records produced by experiment_runner.py and prints a
formatted report to the terminal. Optionally exports the summary tables to CSV.

Usage:
    uv run python analyze_experiments.py                        # full report
    uv run python analyze_experiments.py --scenario bgp-01     # one scenario only
    uv run python analyze_experiments.py --csv report           # export to CSV
    uv run python analyze_experiments.py --file other.jsonl    # different log file
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DEFAULT_LOG = Path(__file__).parent / 'experiment_log.jsonl'
DEFAULT_EVAL_LOG = Path(__file__).parent / 'evaluation_log.jsonl'

WIDTH = 80


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load(path: Path, scenario: str = '') -> pd.DataFrame:
    records = []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f'  [warn] Skipping malformed line {lineno}: {exc}')
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df['scenario'] = df['scenario'].fillna('').replace('', '(none)')
    df['success'] = df['success'].fillna(True)
    df['invalid_commands'] = df['invalid_commands'].fillna(0).astype(int) if 'invalid_commands' in df.columns else 0

    # Keep token totals numeric. Native totals (from result.usage()) are always valid;
    # normalized totals (from the Generation API) go null (→ NaN) when a generation was
    # dropped. The plots/averages use the normalized track because it's comparable
    # across models that tokenize differently — at the cost that NaN sessions are
    # silently skipped by mean()/seaborn. (No back-compat for pre-normalized records —
    # those get deleted.)
    for col in ('total_input_tokens', 'total_output_tokens',
                'total_normalized_input_tokens', 'total_normalized_output_tokens'):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    if scenario:
        df = df[df['scenario'] == scenario]
    return df


def load_verdicts(path: Path) -> pd.DataFrame:
    """Load evaluation_log.jsonl as a DataFrame keyed by session_id.

    Returns an empty frame if the file doesn't exist yet — analysis still works,
    the found_issue column just stays empty.
    """
    if not path.exists():
        return pd.DataFrame(columns=['session_id', 'found_issue'])
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        return pd.DataFrame(columns=['session_id', 'found_issue'])
    return pd.DataFrame(records)


def join_verdicts(df: pd.DataFrame, verdicts: pd.DataFrame) -> pd.DataFrame:
    """Left-join the found_issue verdict onto session df. Un-evaluated sessions get NaN."""
    if verdicts.empty or 'found_issue' not in verdicts.columns:
        df = df.copy()
        df['found_issue'] = pd.NA
        return df
    return df.merge(verdicts[['session_id', 'found_issue']], on='session_id', how='left')


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------

def scenario_summary(df: pd.DataFrame) -> pd.DataFrame:
    summary = (
        df.groupby(['scenario', 'model'], sort=True)
        .agg(
            turns                = ('session_id',          'count'),
            avg_duration_s       = ('duration_s',           'mean'),
            avg_input_tokens     = ('total_input_tokens',   'mean'),
            avg_output_tokens    = ('total_output_tokens',  'mean'),
            avg_tool_calls       = ('total_tool_calls',     'mean'),
            avg_llm_requests     = ('total_llm_requests',   'mean'),
            avg_invalid_commands = ('invalid_commands',     'mean'),
            total_cost_usd       = ('total_cost_usd',       'sum'),
            success_rate         = ('success',              'mean'),
        )
    )

    # found_rate is computed only when verdicts are joined in. You evaluate only the
    # last (concluding) turn of each run, so the rate is over EVALUATED turns, not
    # all turns — evaluated_turns shows coverage.
    if 'found_issue' in df.columns:
        evaluated = df[df['found_issue'].notna()]
        if not evaluated.empty:
            verdict_summary = (
                evaluated.groupby(['scenario', 'model'], sort=True)
                .agg(
                    evaluated_turns = ('found_issue', 'count'),
                    found_rate      = ('found_issue', lambda s: s.astype(bool).mean()),
                )
            )
            summary = summary.join(verdict_summary, how='left')
            summary['evaluated_turns'] = summary['evaluated_turns'].fillna(0).astype(int)

    return summary.round({
        'avg_duration_s': 1, 'avg_input_tokens': 0, 'avg_output_tokens': 0,
        'avg_tool_calls': 1, 'avg_llm_requests': 1, 'avg_invalid_commands': 1,
        'total_cost_usd': 6, 'success_rate': 2, 'found_rate': 2,
    })


def agent_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    # Explode the agent_runs list into one row per agent-run record
    agents = df[['model', 'scenario', 'agent_runs']].explode('agent_runs').dropna(subset=['agent_runs'])
    details = pd.json_normalize(agents['agent_runs'].tolist())
    details['model']    = agents['model'].values
    details['scenario'] = agents['scenario'].values

    # cost_usd is the current field name; estimated_cost_usd was used in older records
    if 'cost_usd' not in details.columns and 'estimated_cost_usd' in details.columns:
        details['cost_usd'] = details['estimated_cost_usd']
    elif 'cost_usd' not in details.columns:
        details['cost_usd'] = 0.0

    breakdown = (
        details.groupby(['model', 'agent_name'], sort=True)
        .agg(
            runs              = ('llm_requests', 'count'),
            # Native tokens (from usage()) — always present, so no run is dropped from
            # the per-agent average.
            avg_input_tokens  = ('input_tokens',  'mean'),
            avg_output_tokens = ('output_tokens', 'mean'),
            avg_tool_calls    = ('tool_calls',    'mean'),
            avg_llm_requests  = ('llm_requests',  'mean'),
            avg_duration_s    = ('duration_s',    'mean'),
            avg_cost_usd      = ('cost_usd',      'mean'),
        )
    )
    return breakdown.round({
        'avg_input_tokens': 0, 'avg_output_tokens': 0,
        'avg_tool_calls': 1,   'avg_llm_requests': 1,
        'avg_duration_s': 1,   'avg_cost_usd': 8,
    })


def failures(df: pd.DataFrame) -> pd.DataFrame:
    failed = df[~df['success']][['model', 'scenario', 'user_query', 'error']].copy()
    failed['user_query'] = failed['user_query'].str[:80]
    failed['error']      = failed['error'].str[:80]
    return failed


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def _print_df(df: pd.DataFrame) -> None:
    print(df.to_string())


def _header(title: str) -> None:
    print(f'\n── {title} ' + '─' * (WIDTH - len(title) - 4))


def print_report(df: pd.DataFrame) -> None:
    now       = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    scenarios = sorted(df['scenario'].unique())
    models    = sorted(df['model'].unique())

    print()
    print('═' * WIDTH)
    print('  EXPERIMENT COMPARISON REPORT')
    print(f'  Generated : {now}')
    print(f'  Sessions  : {len(df)}')
    if 'found_issue' in df.columns:
        evaluated = int(df['found_issue'].notna().sum())
        print(f'  Evaluated : {evaluated}/{len(df)}'
              f'{" (no verdicts yet — run evaluate_experiments.py)" if evaluated == 0 else ""}')
    print(f'  Scenarios : {", ".join(scenarios)}')
    print(f'  Models    : {", ".join(models)}')
    print('═' * WIDTH)

    _header('SCENARIO SUMMARY  (averages per model per scenario)')
    summary = scenario_summary(df)
    print()
    _print_df(summary)

    _header('PER-AGENT BREAKDOWN  (averages across all scenarios)')
    breakdown = agent_breakdown(df)
    print()
    _print_df(breakdown)

    failed = failures(df)
    _header(f'FAILURES  ({len(failed)})')
    if failed.empty:
        print('  None.')
    else:
        print()
        _print_df(failed)

    print('\n' + '═' * WIDTH)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_csv(base: str, df: pd.DataFrame) -> None:
    p = Path(base)
    summary_path  = p.with_stem(p.stem + '_summary').with_suffix('.csv')
    agents_path   = p.with_stem(p.stem + '_agents').with_suffix('.csv')
    failures_path = p.with_stem(p.stem + '_failures').with_suffix('.csv')

    scenario_summary(df).to_csv(summary_path)
    print(f'  Exported scenario summary → {summary_path}')

    agent_breakdown(df).to_csv(agents_path)
    print(f'  Exported agent breakdown  → {agents_path}')

    fail_df = failures(df)
    if not fail_df.empty:
        fail_df.to_csv(failures_path, index=False)
        print(f'  Exported failures         → {failures_path}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Print a comparison report from experiment_log.jsonl.',
    )
    parser.add_argument(
        '--file', '-f',
        default=str(DEFAULT_LOG),
        metavar='PATH',
        help=f'Path to the JSONL log file (default: {DEFAULT_LOG.name})',
    )
    parser.add_argument(
        '--scenario', '-s',
        default='',
        metavar='LABEL',
        help='Filter to a single scenario label (default: show all)',
    )
    parser.add_argument(
        '--csv', '-c',
        default='',
        metavar='BASE',
        help='Export tables to <BASE>_summary.csv, <BASE>_agents.csv, <BASE>_failures.csv',
    )
    parser.add_argument(
        '--eval-file', '-e',
        default=str(DEFAULT_EVAL_LOG),
        metavar='PATH',
        help=f'Path to evaluation_log.jsonl with your manual verdicts '
             f'(default: {DEFAULT_EVAL_LOG.name}). Missing file is fine — '
             f'correctness columns are simply omitted.',
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f'Log file not found: {path}')

    df = load(path, args.scenario)
    if df.empty:
        print('No matching records found.')
        return

    verdicts = load_verdicts(Path(args.eval_file))
    df = join_verdicts(df, verdicts)

    print_report(df)

    if args.csv:
        print()
        export_csv(args.csv, df)


if __name__ == '__main__':
    main()