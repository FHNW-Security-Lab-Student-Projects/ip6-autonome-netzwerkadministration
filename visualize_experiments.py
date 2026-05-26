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

from analyze_experiments import DEFAULT_LOG, load


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
        ('duration_s',        'Duration (s)', 'model_comparison_duration'),
        ('total_tool_calls',  'Tool calls',  'model_comparison_tool_calls'),
    ]
    for column, ylabel, name in metrics:
        fig, ax = plt.subplots()
        sns.barplot(data=df, x='model', y=column, ax=ax, errorbar='sd')
        ax.set_xlabel('Model')
        ax.set_ylabel(ylabel)
        _rotate_xticks(ax)
        written.append(_save(fig, out_dir, name, ext))

    tokens = df[['model', 'total_input_tokens', 'total_output_tokens']].melt(
        id_vars='model', var_name='kind', value_name='tokens',
    )
    tokens['kind'] = tokens['kind'].map({
        'total_input_tokens':  'Input',
        'total_output_tokens': 'Output',
    })
    fig, ax = plt.subplots()
    sns.barplot(data=tokens, x='model', y='tokens', hue='kind', ax=ax, errorbar='sd')
    ax.set_xlabel('Model')
    ax.set_ylabel('Tokens')
    ax.legend(title='')
    _rotate_xticks(ax)
    written.append(_save(fig, out_dir, 'model_comparison_tokens', ext))

    return written


def plot_scenario_performance(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    written: list[Path] = []
    metrics = [
        ('duration_s',       'Duration (s)', 'scenario_performance_duration'),
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


def plot_distributions(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    written: list[Path] = []
    metrics = [
        ('duration_s',         'Duration (s)',  'distribution_duration'),
        ('total_input_tokens', 'Input tokens',  'distribution_tokens'),
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
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f'Log file not found: {path}')

    df = load(path, args.scenario)
    if df.empty:
        print('No matching records found.')
        return

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    written += plot_model_comparison(df, out_dir, args.format)
    written += plot_scenario_performance(df, out_dir, args.format)
    written += plot_distributions(df, out_dir, args.format)

    print(f'Wrote {len(written)} figure(s) to {out_dir}/')
    for p in written:
        print(f'  - {p}')


if __name__ == '__main__':
    main()
