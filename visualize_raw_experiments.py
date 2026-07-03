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
import matplotlib.transforms as mtransforms
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, MaxNLocator
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

# Fixed-order categorical palette (CVD-validated: worst adjacent-pair ΔE 24.2).
# Slots are assigned to models in sorted-name order so a model keeps its colour
# across regenerations regardless of which sessions are in the log.
CATEGORICAL_SLOTS = [
    '#2a78d6',  # blue
    '#1baf7a',  # aqua
    '#eda100',  # yellow
    '#008300',  # green
    '#4a3aa7',  # violet
    '#e34948',  # red
    '#e87ba4',  # magenta
    '#eb6834',  # orange
]

# Chart chrome: recessive greys so the bars are the loudest thing on the page.
INK = '#0b0b0b'
INK_SECONDARY = '#52514e'
GRIDLINE = '#e1e0d9'
BASELINE = '#c3c2b7'
TOKEN_KEY = '#767470'  # legend key for the input/output token shades

GROUP_GAP = 0.9  # empty rows of air between model groups (in row units)


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
    """Sort sessions into contiguous model groups (then scenario, then time).

    The model name moves out of the row label and into a group header, so the
    label only carries what varies inside a group: the session id, plus the
    scenario when more than one is shown.
    """
    out = df.copy()
    out['started_at'] = pd.to_datetime(out['started_at'], errors='coerce')
    out = out.sort_values(
        ['model', 'scenario', 'started_at'], kind='stable',
    ).reset_index(drop=True)
    out['label'] = out['session_id'].astype(str).str[:8]
    if out['scenario'].nunique() > 1:
        out['label'] += ' · ' + out['scenario']
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
    slots = list(CATEGORICAL_SLOTS)
    if len(models) > len(slots):  # never expected; keep colours deterministic anyway
        slots += sns.color_palette('husl', n_colors=len(models) - len(slots)).as_hex()
    palette = dict(zip(models, slots))
    bar_colors = [palette[m] for m in df['model']]

    # Vertical layout in row units: each model group gets a header slot, its
    # session rows, then GROUP_GAP of air before the next group.
    row_y: list[float] = []
    groups: list[tuple[str, float, int]] = []  # (model, header_y, n_sessions)
    y = 0.0
    for m in models:
        n = int((df['model'] == m).sum())
        groups.append((m, y, n))
        row_y.extend(y + 1 + i for i in range(n))
        y += n + 1 + GROUP_GAP
    df['y'] = row_y
    y_span = y - GROUP_GAP  # drop the trailing gap

    n_rows = len(df)
    top_in, bottom_in = 0.35, 0.55  # bands reserved for suptitle / legend
    fig_height = max(3.2, 0.235 * (y_span + 1.5) + top_in + bottom_in)
    fig, axes = plt.subplots(1, 4, figsize=(11.0, fig_height), sharey=True)

    styles = [_verdict_style(row) for _, row in df.iterrows()]

    # 1. Duration
    ax = axes[0]
    ax.barh(df['y'], df['duration_s'], height=0.62, color=bar_colors)
    ax.set_title('LLM inference time (s)')
    ax.set_yticks(list(df['y']))
    labels = [f'{lab}{suffix}' for lab, (suffix, _) in zip(df['label'], styles)]
    ax.set_yticklabels(labels, fontsize=7.5)
    for tick, (_, colour) in zip(ax.get_yticklabels(), styles):
        tick.set_color(colour)
    ax.tick_params(axis='y', length=0, pad=3)

    # 2. Cost
    ax = axes[1]
    ax.barh(df['y'], df['total_cost_usd'], height=0.62, color=bar_colors)
    ax.set_title('Cost (USD)')
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f'${x:g}' if x else '0'))

    # 3. Tokens (stacked input + output). Native counts (from usage()) — always valid,
    # so every session has a bar; fillna(0) just guards the left= stack defensively.
    # The white hairline edge keeps a surface gap between the two stacked segments.
    ax = axes[2]
    tok_in  = df['total_input_tokens'].fillna(0)
    tok_out = df['total_output_tokens'].fillna(0)
    ax.barh(df['y'], tok_in, height=0.62, color=bar_colors,
            edgecolor='white', linewidth=0.5)
    ax.barh(df['y'], tok_out, left=tok_in, height=0.62, color=bar_colors,
            alpha=0.45, edgecolor='white', linewidth=0.5)
    ax.set_title('Tokens (native, input + output)')
    # Abbreviate large token counts (e.g. 100000 -> 100k) so adjacent x-tick
    # labels don't collide.
    ax.xaxis.set_major_formatter(
        FuncFormatter(lambda x, _: f'{x / 1000:g}k' if x else '0')
    )

    # 4. Tool calls
    ax = axes[3]
    ax.barh(df['y'], df['total_tool_calls'], height=0.62, color=bar_colors)
    ax.set_title('Tool calls')

    # Shared axis chrome: recessive solid hairline grid on x only, single
    # baseline spine, y reversed so the first group reads from the top.
    for ax in axes:
        ax.set_ylim(y_span + 0.6, -0.8)
        ax.set_xlim(left=0)
        ax.set_axisbelow(True)
        ax.yaxis.grid(False)
        ax.xaxis.grid(True, color=GRIDLINE, linewidth=0.6)
        for side in ('top', 'right', 'left'):
            ax.spines[side].set_visible(False)
        ax.spines['bottom'].set_color(BASELINE)
        ax.spines['bottom'].set_linewidth(0.8)
        ax.xaxis.set_major_locator(MaxNLocator(4, min_n_ticks=3))
        ax.tick_params(axis='x', labelsize=7.5, colors=INK_SECONDARY,
                       length=2.5, width=0.6)
        if n_rows > 30:  # tall figure: repeat the x scale at the top
            ax.tick_params(axis='x', top=True, labeltop=True)
        ax.title.set_fontsize(8.5)
        ax.title.set_color(INK)
        ax.title.set_fontweight('bold')

    # Group headers (swatch + model name) in the empty header row of each group,
    # plus a hairline separator above every group after the first so the panels
    # without headers still show where a new model starts.
    for gi, (m, hy, n) in enumerate(groups):
        trans = mtransforms.blended_transform_factory(
            axes[0].transAxes, axes[0].transData)
        axes[0].scatter([0.008], [hy], transform=trans, marker='s', s=24,
                        color=palette[m], clip_on=False, zorder=5)
        axes[0].text(0.028, hy, f'{m.split("/", 1)[-1]}   (n={n})',
                     transform=trans, va='center', ha='left', fontsize=8,
                     fontweight='bold', color=INK, zorder=5)
        if gi:
            for ax in axes:
                ax.axhline(hy - GROUP_GAP / 2, color=GRIDLINE, linewidth=0.7)

    # Legend: token shades plus the y-label verdict key (the manual verdict in
    # evaluation_log.jsonl is the source of truth for success; it colours the
    # row labels). Model identity is carried by the group headers above.
    handles = [
        Patch(facecolor=TOKEN_KEY, edgecolor='none'),
        Patch(facecolor=TOKEN_KEY, edgecolor='none', alpha=0.45),
        Line2D([], [], linestyle='none', marker='s', markersize=6,
               color=VERDICT_FOUND),
        Line2D([], [], linestyle='none', marker='s', markersize=6,
               color=VERDICT_MISSED),
        Line2D([], [], linestyle='none', marker='s', markersize=6,
               color=VERDICT_UNEVAL),
        Line2D([], [], linestyle='none', marker=r'$*$', color=VERDICT_MISSED),
        Line2D([], [], linestyle='none', marker=r'$\dagger$', color=VERDICT_DNF),
    ]
    labels = ['input tokens', 'output tokens',
              r'label: found ($\checkmark$)', r'label: missed ($\times$)',
              'label: unevaluated', 'crashed (*)',
              r'DNF / time limit ($\dagger$)']
    fig.legend(handles=handles, labels=labels, loc='lower center',
               ncol=len(handles), bbox_to_anchor=(0.5, 0.04 / fig_height),
               frameon=False, fontsize=7.5, handletextpad=0.5, columnspacing=1.2)

    fig.suptitle('Raw experiment sessions — one row per session, grouped by model',
                 y=1 - 0.06 / fig_height, fontsize=10.5, color=INK)
    fig.tight_layout(rect=(0, bottom_in / fig_height, 1, 1 - top_in / fig_height))

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
