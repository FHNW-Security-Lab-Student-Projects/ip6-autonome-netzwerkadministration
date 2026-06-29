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
from matplotlib.ticker import FuncFormatter
import pandas as pd
import seaborn as sns

from analyze_experiments import DEFAULT_EVAL_LOG, DEFAULT_LOG, join_verdicts, load, load_verdicts
from visualize_experiments import _save  # reuse same save helper

# Colours for the per-session verdict shown on each y-axis label. Mirrors the
# green/red/grey of raw_experiments_html.py — the manual verdict in
# evaluation_log.jsonl is the source of truth for what succeeded, so it drives
# the label colour; a crashed run with no verdict stays red, unevaluated is grey,
# and a DNF (hit the wall-clock time limit) gets its own amber category.
VERDICT_FOUND = '#1a7f37'
VERDICT_MISSED = 'crimson'
VERDICT_UNEVAL = '#888888'
VERDICT_DNF = '#d97706'


def _failure_kind(row: pd.Series) -> str:
    """Classify a failed run: 'dnf' (hit the time limit), 'crashed', or '' (ok).

    A run is only a failure when success is False. We separate a DNF — where the
    run was cut off at the wall-clock limit rather than erroring — by inspecting
    the recorded error text, so a timed-out run isn't mislabelled as a crash.
    """
    if bool(row.get('success', True)):
        return ''
    err = str(row.get('error') or '').lower()
    if 'dnf' in err or 'timeout' in err or 'time limit' in err:
        return 'dnf'
    return 'crashed'


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


def _verdict_style(row: pd.Series) -> tuple[str, str]:
    """Return (label_suffix, colour) encoding the manual verdict + failure state.

    Verdict (from evaluation_log.jsonl) is the source of truth and wins when present:
    ✓ found / ✗ missed. Otherwise the run is unevaluated. A failed run is flagged
    with a marker — `*` for a crash, `†` for a DNF (hit the time limit) — and, when
    unevaluated, takes its failure colour: red for a crash, amber for a DNF.
    """
    found = row.get('found_issue')
    kind = _failure_kind(row)
    marker = {'crashed': ' *', 'dnf': ' $\\dagger$'}.get(kind, '')
    # mathtext symbols render via matplotlib's own fonts, so they don't depend on
    # the serif face having a ✓/✗ glyph (DejaVu Serif lacks them).
    if pd.notna(found):
        if bool(found):
            return rf' $\checkmark${marker}', VERDICT_FOUND
        return rf' $\times${marker}', VERDICT_MISSED
    # Unevaluated: grey, unless the run failed — flag a crash red, a DNF amber.
    fail_colour = {'crashed': VERDICT_MISSED, 'dnf': VERDICT_DNF}.get(kind)
    return marker, (fail_colour or VERDICT_UNEVAL)


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
    ax.set_xlabel('LLM inference time, summed (s)')
    ax.invert_yaxis()
    ax.set_yticks(list(y))
    styles = [_verdict_style(row) for _, row in df.iterrows()]
    labels = [f'{lab}{suffix}' for lab, (suffix, _) in zip(df['label'], styles)]
    ax.set_yticklabels(labels)
    for tick, (_, colour) in zip(ax.get_yticklabels(), styles):
        tick.set_color(colour)

    # 2. Cost
    ax = axes[1]
    ax.barh(y, df['total_cost_usd'], color=bar_colors)
    ax.set_xlabel('Cost (USD)')

    # 3. Tokens (stacked input + output). Native counts (from usage()) — always valid,
    # so every session has a bar; fillna(0) just guards the left= stack defensively.
    ax = axes[2]
    tok_in  = df['total_input_tokens'].fillna(0)
    tok_out = df['total_output_tokens'].fillna(0)
    ax.barh(y, tok_in, color=bar_colors, label='input')
    ax.barh(y, tok_out, left=tok_in, color=bar_colors, alpha=0.45, label='output')
    ax.set_xlabel('Tokens (native, input + output)')
    # Abbreviate large token counts (e.g. 100000 -> 100k) so adjacent x-tick
    # labels don't collide.
    ax.xaxis.set_major_formatter(
        FuncFormatter(lambda x, _: f'{x / 1000:g}k' if x else '0')
    )

    # 4. Tool calls
    ax = axes[3]
    ax.barh(y, df['total_tool_calls'], color=bar_colors)
    ax.set_xlabel('Tool calls')

    # Legend: bar colours = model, token shades, and the y-label verdict key
    # (verdict is the source of truth for success; it colours the y-labels).
    model_handles = [
        plt.Rectangle((0, 0), 1, 1, color=palette[m]) for m in models
    ]
    token_handles = [
        plt.Rectangle((0, 0), 1, 1, facecolor='gray', edgecolor='none'),
        plt.Rectangle((0, 0), 1, 1, facecolor='gray', edgecolor='none', alpha=0.45),
    ]
    verdict_handles = [
        plt.Rectangle((0, 0), 1, 1, color=VERDICT_FOUND),
        plt.Rectangle((0, 0), 1, 1, color=VERDICT_MISSED),
        plt.Rectangle((0, 0), 1, 1, color=VERDICT_UNEVAL),
        plt.Line2D([], [], linestyle='none', marker='*', color=VERDICT_MISSED),
        plt.Line2D([], [], linestyle='none', marker=r'$\dagger$', color=VERDICT_DNF),
    ]
    fig.legend(
        handles=model_handles + token_handles + verdict_handles,
        labels=(models + ['input tokens', 'output tokens']
                + ['label: found', 'label: missed', 'label: unevaluated',
                   'crashed (*)', r'DNF / time limit ($\dagger$)']),
        loc='lower center',
        ncol=min(6, len(models) + 2),
        bbox_to_anchor=(0.5, -0.10),
        frameon=False,
        fontsize=8,
    )

    fig.suptitle('Raw experiment sessions (one row per session)', y=1.0,
                 fontsize=10)
    fig.tight_layout(rect=(0, 0.14, 1, 0.98))

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
    parser.add_argument(
        '--eval-file', '-e',
        default=str(DEFAULT_EVAL_LOG),
        metavar='PATH',
        help=f'Path to evaluation_log.jsonl with your manual verdicts '
             f'(default: {DEFAULT_EVAL_LOG.name}). Missing file is fine — '
             f'labels just show as unevaluated (grey).',
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

    written = plot_raw_sessions(df, out_dir, args.format)
    print(f'Wrote 1 figure ({len(df)} session(s)) to {written}')


if __name__ == '__main__':
    main()
