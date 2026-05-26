#!/usr/bin/env python3
"""Generate a per-session figure from experiment_log.jsonl.

Unlike visualize_experiments.py which averages across runs, this script
shows every individual session as its own labelled bar — one row per
session — across four metric panels (duration, cost, tokens, tool calls).

Usage:
    uv run python visualize_raw_experiments.py
    uv run python visualize_raw_experiments.py --scenario bgp-01
    uv run python visualize_raw_experiments.py --format png
    uv run python visualize_raw_experiments.py --out paper/figs
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from analyze_experiments import DEFAULT_LOG, load
from visualize_experiments import _save  # reuse same save helper


sns.set_theme(context='paper', style='whitegrid', palette='colorblind')
plt.rcParams.update({
    'font.family':  'serif',
    'font.size':    9,
    'savefig.bbox': 'tight',
    'pdf.fonttype': 42,
})


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out['started_at'] = pd.to_datetime(out['started_at'], errors='coerce')
    out = out.sort_values('started_at', kind='stable').reset_index(drop=True)
    out['label'] = out['session_id'].astype(str).str[:8] + ' · ' + out['model']
    return out


def plot_raw_sessions(df: pd.DataFrame, out_dir: Path, ext: str) -> Path:
    df = _prepare(df)

    # Stable model→colour mapping so every panel uses the same colour per model.
    models = sorted(df['model'].dropna().unique())
    palette = dict(zip(models, sns.color_palette('colorblind', n_colors=len(models))))
    bar_colors = [palette[m] for m in df['model']]

    n_rows = len(df)
    fig_height = max(2.4, 0.32 * n_rows + 1.0)
    fig, axes = plt.subplots(
        1, 4,
        figsize=(11.0, fig_height),
        sharey=True,
    )

    y = range(n_rows)

    # 1. Duration
    ax = axes[0]
    ax.barh(y, df['duration_s'], color=bar_colors)
    ax.set_xlabel('Duration (s)')
    ax.invert_yaxis()
    ax.set_yticks(list(y))
    labels = [
        f'{lab} *' if not ok else lab
        for lab, ok in zip(df['label'], df['success'])
    ]
    ax.set_yticklabels(labels)
    for tick, ok in zip(ax.get_yticklabels(), df['success']):
        if not ok:
            tick.set_color('crimson')

    # 2. Cost
    ax = axes[1]
    ax.barh(y, df['total_cost_usd'], color=bar_colors)
    ax.set_xlabel('Cost (USD)')

    # 3. Tokens (stacked input + output)
    ax = axes[2]
    ax.barh(y, df['total_input_tokens'], color=bar_colors,
            label='input')
    ax.barh(y, df['total_output_tokens'], left=df['total_input_tokens'],
            color=bar_colors, alpha=0.45, label='output')
    ax.set_xlabel('Tokens (input + output)')

    # 4. Tool calls
    ax = axes[3]
    ax.barh(y, df['total_tool_calls'], color=bar_colors)
    ax.set_xlabel('Tool calls')

    # Legend: model colours + token-shade explanation.
    model_handles = [
        plt.Rectangle((0, 0), 1, 1, color=palette[m]) for m in models
    ]
    token_handles = [
        plt.Rectangle((0, 0), 1, 1, facecolor='gray', edgecolor='none'),
        plt.Rectangle((0, 0), 1, 1, facecolor='gray', edgecolor='none', alpha=0.45),
    ]
    fig.legend(
        handles=model_handles + token_handles,
        labels=models + ['input tokens', 'output tokens'],
        loc='lower center',
        ncol=min(6, len(models) + 2),
        bbox_to_anchor=(0.5, -0.02),
        frameon=False,
        fontsize=8,
    )

    fig.suptitle('Raw experiment sessions (one row per session)', y=1.0,
                 fontsize=10)
    fig.tight_layout(rect=(0, 0.04, 1, 0.98))

    return _save(fig, out_dir, 'raw_sessions_overview', ext)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Render every session in experiment_log.jsonl as one '
                    'labelled bar per metric (no averaging).',
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

    written = plot_raw_sessions(df, out_dir, args.format)
    print(f'Wrote 1 figure ({len(df)} session(s)) to {written}')


if __name__ == '__main__':
    main()
