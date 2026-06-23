#!/usr/bin/env python3
"""Generate visualizations from experiment_log.jsonl for LaTeX inclusion.

Reads the same session records as analyze_experiments.py and writes
vector PDFs to a figures directory, sized for typical LaTeX paper inclusion.

Usage:
    uv run python visualize_experiments.py
    uv run python visualize_experiments.py --scenario bgp-01
    uv run python visualize_experiments.py --out paper/figs
    uv run python visualize_experiments.py --format png
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from analyze_experiments import DEFAULT_EVAL_LOG, DEFAULT_LOG, join_verdicts, load, load_verdicts


sns.set_theme(context='paper', style='whitegrid', palette='colorblind')
plt.rcParams.update({
    'font.family':    'serif',
    'font.size':      10,
    'figure.figsize': (6.0, 3.7),
    'savefig.bbox':   'tight',
    'pdf.fonttype':   42,
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save(fig: plt.Figure, out_dir: Path, name: str, ext: str) -> Path:
    path = out_dir / f'{name}.{ext}'
    fig.savefig(path)
    plt.close(fig)
    return path


def _rotate_xticks(ax: plt.Axes, degrees: int = 15) -> None:
    for label in ax.get_xticklabels():
        label.set_rotation(degrees)
        label.set_horizontalalignment('right')


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def plot_model_comparison(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    written: list[Path] = []

    metrics = [
        ('total_cost_usd',    'Cost (USD)',  'model_comparison_cost'),
        ('duration_s',        'LLM inference time, summed (s)', 'model_comparison_duration'),
        ('total_tool_calls',  'Tool calls',  'model_comparison_tool_calls'),
    ]
    for column, ylabel, name in metrics:
        fig, ax = plt.subplots()
        sns.barplot(data=df, x='model', y=column, ax=ax, errorbar='sd')
        ax.set_xlabel('Model')
        ax.set_ylabel(ylabel)
        _rotate_xticks(ax)
        written.append(_save(fig, out_dir, name, ext))

    # Normalized tokens (from the Generation API) — comparable across models that
    # tokenize differently, unlike native counts. The trade-off: normalized counts go
    # NaN when a generation is dropped, so those sessions are silently omitted from
    # these bars (native counts, by contrast, are always valid).
    tokens = df[['model', 'total_normalized_input_tokens', 'total_normalized_output_tokens']].melt(
        id_vars='model', var_name='kind', value_name='tokens',
    )
    tokens['kind'] = tokens['kind'].map({
        'total_normalized_input_tokens':  'Input',
        'total_normalized_output_tokens': 'Output',
    })
    fig, ax = plt.subplots()
    sns.barplot(data=tokens, x='model', y='tokens', hue='kind', ax=ax, errorbar='sd')
    ax.set_xlabel('Model')
    ax.set_ylabel('Tokens (normalized)')
    ax.legend(title='')
    _rotate_xticks(ax)
    written.append(_save(fig, out_dir, 'model_comparison_tokens', ext))

    return written


def plot_scenario_performance(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    written: list[Path] = []
    metrics = [
        ('duration_s',       'LLM inference time, summed (s)', 'scenario_performance_duration'),
        ('total_cost_usd',   'Cost (USD)',   'scenario_performance_cost'),
        ('total_tool_calls', 'Tool calls',   'scenario_performance_tool_calls'),
    ]
    for column, ylabel, name in metrics:
        fig, ax = plt.subplots()
        sns.barplot(data=df, x='scenario', y=column, hue='model', ax=ax, errorbar='sd')
        ax.set_xlabel('Scenario')
        ax.set_ylabel(ylabel)
        ax.legend(title='Model', fontsize=8)
        _rotate_xticks(ax)
        written.append(_save(fig, out_dir, name, ext))
    return written


def plot_invalid_commands(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    """Bar chart of invalid-command counts per scenario, split by model.

    Bars show the total number of invalid commands the agents issued (estimator=sum),
    so the height is a literal count, not a per-run average. If the column is missing
    or every run is zero, the chart is skipped rather than drawn empty.
    """
    if 'invalid_commands' not in df.columns:
        print('  [skip] invalid-commands chart: column not present.')
        return []
    if df['invalid_commands'].fillna(0).sum() == 0:
        print('  [skip] invalid-commands chart: no invalid commands recorded.')
        return []

    fig, ax = plt.subplots()
    sns.barplot(data=df, x='scenario', y='invalid_commands', hue='model',
                ax=ax, estimator='sum', errorbar=None)
    ax.set_xlabel('Scenario')
    ax.set_ylabel('Invalid commands (count)')
    ax.legend(title='Model', fontsize=8)
    _rotate_xticks(ax)
    return [_save(fig, out_dir, 'invalid_commands', ext)]


def plot_correctness(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    """Bar chart of found_rate — the fraction of evaluated runs the agent got right.

    The source of truth is your manual verdicts in evaluation_log.jsonl (the
    `found_issue` column joined onto the sessions), NOT the `success` field — `success`
    only means the run didn't crash. Only evaluated runs count toward the rate; runs
    you haven't labelled yet are excluded, and if nothing is labelled the chart is
    skipped rather than drawn empty.
    """
    if 'found_issue' not in df.columns:
        print('  [skip] correctness chart: no evaluation_log.jsonl joined.')
        return []
    evaluated = df[df['found_issue'].notna()].copy()
    if evaluated.empty:
        print('  [skip] correctness chart: no verdicts yet — run evaluate_experiments.py.')
        return []
    evaluated['found'] = evaluated['found_issue'].astype(bool).astype(int)

    fig, ax = plt.subplots()
    # errorbar=None: the bar height is the mean of found (0/1) per group = the found
    # rate. With few runs per cell, a CI would be noise, so show the rate cleanly.
    sns.barplot(data=evaluated, x='scenario', y='found', hue='model',
                ax=ax, errorbar=None)
    ax.set_xlabel('Scenario')
    ax.set_ylabel('Found rate')
    ax.set_ylim(0, 1)
    ax.legend(title='Model', fontsize=8)
    _rotate_xticks(ax)
    return [_save(fig, out_dir, 'correctness_found_rate', ext)]


def plot_distributions(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    written: list[Path] = []
    metrics = [
        ('duration_s',         'LLM inference time, summed (s)',  'distribution_duration'),
        ('total_normalized_input_tokens', 'Input tokens (normalized)',  'distribution_tokens'),
        ('total_cost_usd',     'Cost (USD)',    'distribution_cost'),
    ]
    for column, ylabel, name in metrics:
        fig, ax = plt.subplots()
        sns.boxplot(data=df, x='model', y=column, ax=ax)
        sns.stripplot(data=df, x='model', y=column, ax=ax,
                      color='black', size=3, alpha=0.5)
        ax.set_xlabel('Model')
        ax.set_ylabel(ylabel)
        _rotate_xticks(ax)
        written.append(_save(fig, out_dir, name, ext))
    return written


def plot_scenario_distributions(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    """Boxplots of cost, duration, and tokens per scenario (model as hue).

    Mirrors plot_distributions but pivots the x-axis to the scenario, so each box
    shows the run-to-run spread of a metric within one troubleshooting scenario.
    Individual runs are overlaid as points; with multiple models the boxes are split
    by model so per-scenario model differences stay visible.
    """
    written: list[Path] = []
    multi_model = df['model'].nunique() > 1
    hue = 'model' if multi_model else None

    metrics = [
        ('total_cost_usd',                'Cost (USD)',               'scenario_distribution_cost'),
        ('duration_s',                    'LLM inference time, summed (s)',             'scenario_distribution_duration'),
        ('total_normalized_input_tokens', 'Input tokens (normalized)', 'scenario_distribution_tokens'),
    ]
    for column, ylabel, name in metrics:
        fig, ax = plt.subplots()
        sns.boxplot(data=df, x='scenario', y=column, hue=hue, ax=ax)
        sns.stripplot(data=df, x='scenario', y=column, hue=hue,
                      ax=ax, color='black', size=3, alpha=0.5,
                      dodge=multi_model, legend=False)
        ax.set_xlabel('Scenario')
        ax.set_ylabel(ylabel)
        if hue is not None:
            ax.legend(title='Model', fontsize=8)
        _rotate_xticks(ax)
        written.append(_save(fig, out_dir, name, ext))
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Generate experiment visualizations from experiment_log.jsonl.',
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
        '--out', '-o',
        default='figures',
        metavar='DIR',
        help='Output directory for figures (default: figures)',
    )
    parser.add_argument(
        '--format',
        choices=['pdf', 'png'],
        default='pdf',
        help='Output format (default: pdf, recommended for LaTeX)',
    )
    parser.add_argument(
        '--eval-file', '-e',
        default=str(DEFAULT_EVAL_LOG),
        metavar='PATH',
        help=f'Path to evaluation_log.jsonl with your manual verdicts '
             f'(default: {DEFAULT_EVAL_LOG.name}). Missing file is fine — the '
             f'correctness chart is simply skipped.',
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

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    written += plot_model_comparison(df, out_dir, args.format)
    written += plot_scenario_performance(df, out_dir, args.format)
    written += plot_invalid_commands(df, out_dir, args.format)
    written += plot_correctness(df, out_dir, args.format)
    written += plot_distributions(df, out_dir, args.format)
    written += plot_scenario_distributions(df, out_dir, args.format)

    print(f'Wrote {len(written)} figure(s) to {out_dir}/')
    for p in written:
        print(f'  - {p}')


if __name__ == '__main__':
    main()
