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
import matplotlib.transforms as transforms
import pandas as pd
import seaborn as sns

from analyze_experiments import (
    DEFAULT_EVAL_LOG, DEFAULT_LOG, DEFAULT_WALL_CLOCK_LOG,
    join_verdicts, join_wall_clock, load, load_verdicts, load_wall_clock,
)


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


def _rotate_xticks(ax: plt.Axes, degrees: int = 45) -> None:
    for label in ax.get_xticklabels():
        label.set_rotation(degrees)
        label.set_horizontalalignment('right')


def _model_legend_outside(ax: plt.Axes) -> None:
    """Place the model legend to the right of the axes, outside the plot area.

    Seaborn puts the legend inside the axes by default, where it covers bars once
    there are more than a few models. savefig.bbox='tight' grows the canvas to
    include the relocated legend, so nothing gets clipped.
    """
    ax.legend(title='Model', fontsize=8, loc='upper left',
              bbox_to_anchor=(1.02, 1), borderaxespad=0)


def _has_data(df: pd.DataFrame, column: str, name: str) -> bool:
    """True if the column exists and has at least one value; prints a skip note otherwise.

    Lets optional metrics (wall_clock_s comes from a separate join) drop out of the
    metric loops cleanly instead of producing empty charts.
    """
    if column in df.columns and df[column].notna().any():
        return True
    print(f'  [skip] {name}: no {column} data (run recover_wall_clock.py).')
    return False


def _cat_subplots(n_categories: int, height: float = 3.7):
    """Create a figure whose width grows with the number of x-axis categories.

    The default 6.0 in width crowds long categorical labels once there are more
    than a handful of groups (e.g. 10 scenarios). Scale width with the category
    count, but never go below the 6.0 in default so small charts stay compact.
    """
    width = max(6.0, 1.5 + 0.9 * n_categories)
    return plt.subplots(figsize=(width, height))


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def plot_model_comparison(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    written: list[Path] = []

    metrics = [
        ('total_cost_usd',    'Cost (USD)',  'model_comparison_cost'),
        ('duration_s',        'LLM inference time, summed (s)', 'model_comparison_duration'),
        ('wall_clock_s',      'Wall-clock time (s)', 'model_comparison_wall_clock'),
        ('total_tool_calls',  'Tool calls',  'model_comparison_tool_calls'),
    ]
    for column, ylabel, name in metrics:
        if not _has_data(df, column, name):
            continue
        fig, ax = _cat_subplots(df['model'].nunique())
        sns.barplot(data=df, x='model', y=column, ax=ax, errorbar='sd')
        ax.set_xlabel('Model')
        ax.set_ylabel(ylabel)
        _rotate_xticks(ax)
        written.append(_save(fig, out_dir, name, ext))

    # Both time metrics side by side. Summed inference time can EXCEED wall clock
    # because agents fire LLM requests in parallel — the gap between the two bars
    # shows each model's orchestration concurrency, which neither chart shows alone.
    if _has_data(df, 'wall_clock_s', 'model_comparison_time_metrics'):
        times = df[['model', 'duration_s', 'wall_clock_s']].melt(
            id_vars='model', var_name='kind', value_name='seconds',
        )
        times['kind'] = times['kind'].map({
            'duration_s':   'LLM inference, summed',
            'wall_clock_s': 'Wall clock',
        })
        fig, ax = _cat_subplots(df['model'].nunique())
        sns.barplot(data=times, x='model', y='seconds', hue='kind', ax=ax, errorbar='sd')
        ax.set_xlabel('Model')
        ax.set_ylabel('Time (s)')
        ax.legend(title='')
        _rotate_xticks(ax)
        written.append(_save(fig, out_dir, 'model_comparison_time_metrics', ext))

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
    fig, ax = _cat_subplots(df['model'].nunique())
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
        ('wall_clock_s',     'Wall-clock time (s)', 'scenario_performance_wall_clock'),
        ('total_cost_usd',   'Cost (USD)',   'scenario_performance_cost'),
        ('total_tool_calls', 'Tool calls',   'scenario_performance_tool_calls'),
    ]
    for column, ylabel, name in metrics:
        if not _has_data(df, column, name):
            continue
        fig, ax = _cat_subplots(df['scenario'].nunique())
        # errorbar=('pi', 100): whiskers span the observed min-max of the runs in
        # each group. With only 2-4 runs a symmetric SD interval extends below
        # zero, implying negative cost / tool calls.
        sns.barplot(data=df, x='scenario', y=column, hue='model', ax=ax,
                    errorbar=('pi', 100))
        ax.set_xlabel('Scenario')
        ax.set_ylabel(ylabel)
        _model_legend_outside(ax)
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

    fig, ax = _cat_subplots(df['scenario'].nunique())
    sns.barplot(data=df, x='scenario', y='invalid_commands', hue='model',
                ax=ax, estimator='sum', errorbar=None)
    ax.set_xlabel('Scenario')
    ax.set_ylabel('Invalid commands (count)')
    _model_legend_outside(ax)
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

    fig, ax = _cat_subplots(evaluated['scenario'].nunique())
    # errorbar=None: the bar height is the mean of found (0/1) per group = the found
    # rate. With few runs per cell, a CI would be noise, so show the rate cleanly.
    sns.barplot(data=evaluated, x='scenario', y='found', hue='model',
                ax=ax, errorbar=None)
    ax.set_xlabel('Scenario')
    ax.set_ylabel('Found rate')
    ax.set_ylim(0, 1)
    _model_legend_outside(ax)
    _rotate_xticks(ax)
    return [_save(fig, out_dir, 'correctness_found_rate', ext)]


def plot_cost_vs_correctness(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    """Scatter of mean cost per run (log x) against found rate, one dot per model.

    The price-to-performance view: models toward the top-left are cheap AND
    correct, and the dashed step line traces the Pareto frontier (models where no
    other model is both cheaper and more accurate). Cost is averaged over all runs
    of a model, while the found rate only covers the manually evaluated runs (same
    verdict source as plot_correctness), so the two denominators differ.
    """
    if 'found_issue' not in df.columns:
        print('  [skip] cost-vs-correctness chart: no evaluation_log.jsonl joined.')
        return []
    evaluated = df[df['found_issue'].notna()].copy()
    if evaluated.empty:
        print('  [skip] cost-vs-correctness chart: no verdicts yet — run evaluate_experiments.py.')
        return []
    evaluated['found'] = evaluated['found_issue'].astype(bool).astype(int)

    models = list(df['model'].unique())
    colors = dict(zip(models, sns.color_palette(n_colors=len(models))))
    stats = pd.DataFrame({
        'cost':       df.groupby('model')['total_cost_usd'].mean(),
        'found_rate': evaluated.groupby('model')['found'].mean(),
    }).dropna()

    fig, ax = plt.subplots()

    # Pareto frontier: walk the models cheapest-first and keep those that raise
    # the best found rate seen so far; steps-post joins them into the boundary.
    best = -1.0
    frontier = []
    for _, row in stats.sort_values('cost').iterrows():
        if row['found_rate'] > best:
            best = row['found_rate']
            frontier.append(row)
    frontier = pd.DataFrame(frontier)
    ax.plot(frontier['cost'], frontier['found_rate'], drawstyle='steps-post',
            linestyle='--', linewidth=1, color='0.6', zorder=1)

    for model, row in stats.iterrows():
        ax.scatter(row['cost'], row['found_rate'], s=45,
                   color=colors[model], zorder=2)
        ax.annotate(model.split('/', 1)[-1], (row['cost'], row['found_rate']),
                    xytext=(6, 4), textcoords='offset points', fontsize=8)

    ax.set_xscale('log')
    ax.set_xlabel('Mean cost per run (USD, log scale)')
    ax.set_ylabel('Found rate')
    ax.set_ylim(0, 1.05)
    return [_save(fig, out_dir, 'cost_vs_correctness', ext)]


def plot_time_to_diagnosis(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    """Wall-clock time to a CORRECT diagnosis per model — the time-savings metric.

    Includes only runs that finished within the matrix cap AND whose output was
    judged correct (found_issue). DNF runs are excluded rather than entered at
    their 600 s cap value: they are right-censored — the run was cut off, so it
    has a failure, not a troubleshooting time. (A few DNF runs carry
    found_issue=True because the partial output already named the root cause;
    they still never delivered a diagnosis, so they are excluded too.)

    The per-model n differs, so each group is annotated with correct/evaluated
    counts. Read this chart together with the found-rate chart: a model can look
    fast here simply because only its easy runs succeed.
    """
    if 'found_issue' not in df.columns or df['found_issue'].notna().sum() == 0:
        print('  [skip] time-to-diagnosis chart: no verdicts yet — run evaluate_experiments.py.')
        return []
    if not _has_data(df, 'wall_clock_s', 'time_to_diagnosis'):
        return []

    evaluated = df[df['found_issue'].notna()]
    finished = evaluated[~evaluated['error'].fillna('').str.startswith('DNF')]
    correct = finished[finished['found_issue'].astype(bool) & finished['wall_clock_s'].notna()]
    if correct.empty:
        print('  [skip] time-to-diagnosis chart: no correctly diagnosed completed runs.')
        return []

    # Keep the global model order (and palette) even though some models may
    # contribute few points; models with zero correct runs drop out.
    order = [m for m in df['model'].unique() if (correct['model'] == m).any()]
    fig, ax = _cat_subplots(len(order))
    sns.boxplot(data=correct, x='model', y='wall_clock_s', order=order,
                ax=ax, showfliers=False)
    sns.stripplot(data=correct, x='model', y='wall_clock_s', order=order,
                  ax=ax, color='black', size=3, alpha=0.5)

    n_correct = correct.groupby('model').size()
    n_evaluated = evaluated.groupby('model').size()
    trans = transforms.blended_transform_factory(ax.transData, ax.transAxes)
    for i, model in enumerate(order):
        ax.text(i, 0.98, f'n={n_correct[model]}/{n_evaluated[model]}',
                transform=trans, ha='center', va='top', fontsize=7, color='0.3')

    ax.set_xlabel('Model')
    ax.set_ylabel('Wall-clock time to correct diagnosis (s)')
    _rotate_xticks(ax)
    return [_save(fig, out_dir, 'time_to_diagnosis', ext)]


def plot_distributions(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    written: list[Path] = []
    metrics = [
        ('duration_s',         'LLM inference time, summed (s)',  'distribution_duration'),
        ('wall_clock_s',       'Wall-clock time (s)',  'distribution_wall_clock'),
        ('total_normalized_input_tokens', 'Input tokens (normalized)',  'distribution_tokens'),
        ('total_cost_usd',     'Cost (USD)',    'distribution_cost'),
    ]
    for column, ylabel, name in metrics:
        if not _has_data(df, column, name):
            continue
        fig, ax = _cat_subplots(df['model'].nunique())
        # showfliers=False: outlier runs are already drawn by the stripplot below;
        # the boxplot's own flier circles would render the same runs twice.
        sns.boxplot(data=df, x='model', y=column, ax=ax, showfliers=False)
        sns.stripplot(data=df, x='model', y=column, ax=ax,
                      color='black', size=3, alpha=0.5)
        ax.set_xlabel('Model')
        ax.set_ylabel(ylabel)
        _rotate_xticks(ax)
        written.append(_save(fig, out_dir, name, ext))
    return written


def plot_scenario_distributions(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    """Per-model panels of run-to-run spread per scenario (one dot = one run).

    Replaces the earlier single-axes grouped boxplots: 10 scenarios x 6 models
    packed 60 boxes into one plot, and with only 2-4 runs per group the quartile
    boxes suggested distributions that aren't there. Each model now gets its own
    panel with the raw runs as dots and a tick at the per-scenario median.
    Panels share one scenario order (by median over all models, largest on top)
    and one x-scale, so positions stay comparable across models.
    """
    written: list[Path] = []
    models = list(df['model'].unique())
    # Same palette order seaborn uses for hue='model' in the other charts, so
    # each model keeps its established color here.
    colors = dict(zip(models, sns.color_palette(n_colors=len(models))))
    n_cols = min(3, len(models))
    n_rows = -(-len(models) // n_cols)

    metrics = [
        ('total_cost_usd',                'Cost (USD)',               'scenario_distribution_cost'),
        ('duration_s',                    'LLM inference time, summed (s)',             'scenario_distribution_duration'),
        ('wall_clock_s',                  'Wall-clock time (s)',      'scenario_distribution_wall_clock'),
        ('total_normalized_input_tokens', 'Input tokens (normalized)', 'scenario_distribution_tokens'),
    ]
    for column, xlabel, name in metrics:
        if not _has_data(df, column, name):
            continue
        data = df[df[column].notna()]
        order = (data.groupby('scenario')[column].median()
                 .sort_values(ascending=False).index.tolist())
        fig, axes = plt.subplots(
            n_rows, n_cols, sharex=True, sharey=True, squeeze=False,
            figsize=(3.4 * n_cols, 0.3 * len(order) * n_rows + 1.4),
            layout='constrained',
        )
        for ax, model in zip(axes.flat, models):
            sub = data[data['model'] == model]
            sns.stripplot(data=sub, y='scenario', x=column, order=order,
                          ax=ax, color=colors[model], size=4, alpha=0.75)
            medians = sub.groupby('scenario')[column].median().reindex(order)
            ax.plot(medians.to_numpy(), range(len(order)), linestyle='',
                    marker='|', color='0.2', markersize=9, markeredgewidth=1.3)
            ax.set_title(model, fontsize=9)
            ax.set_xlabel('')
            ax.set_ylabel('')
        for ax in axes.flat[len(models):]:
            ax.set_visible(False)
        fig.supxlabel(xlabel, fontsize=10)
        written.append(_save(fig, out_dir, name, ext))
    return written


def plot_scenario_time_to_diagnosis(df: pd.DataFrame, out_dir: Path, ext: str) -> list[Path]:
    """Per-model panels of wall-clock time to a CORRECT diagnosis per scenario.

    Same layout as plot_scenario_distributions, but restricted to runs that
    finished within the matrix cap and were judged correct (see
    plot_time_to_diagnosis for the censoring rationale). With at most 5 runs
    per cell the dot count doubles as the per-cell n: a scenario row with no
    dots means the model never produced a correct diagnosis there — that
    absence is the difficulty-ceiling evidence, so empty rows are kept visible
    rather than dropped.
    """
    if 'found_issue' not in df.columns or df['found_issue'].notna().sum() == 0:
        print('  [skip] scenario time-to-diagnosis chart: no verdicts yet.')
        return []
    if not _has_data(df, 'wall_clock_s', 'scenario_time_to_diagnosis'):
        return []

    evaluated = df[df['found_issue'].notna()]
    finished = evaluated[~evaluated['error'].fillna('').str.startswith('DNF')]
    correct = finished[finished['found_issue'].astype(bool) & finished['wall_clock_s'].notna()]
    if correct.empty:
        print('  [skip] scenario time-to-diagnosis chart: no correctly diagnosed completed runs.')
        return []

    models = list(df['model'].unique())
    colors = dict(zip(models, sns.color_palette(n_colors=len(models))))
    n_cols = min(3, len(models))
    n_rows = -(-len(models) // n_cols)

    # One scenario order for all panels; include every scenario so a model's
    # empty rows stay visible.
    order = (correct.groupby('scenario')['wall_clock_s'].median()
             .reindex(df['scenario'].unique()).sort_values(ascending=False, na_position='last')
             .index.tolist())
    fig, axes = plt.subplots(
        n_rows, n_cols, sharex=True, sharey=True, squeeze=False,
        figsize=(3.4 * n_cols, 0.3 * len(order) * n_rows + 1.4),
        layout='constrained',
    )
    for ax, model in zip(axes.flat, models):
        sub = correct[correct['model'] == model]
        sns.stripplot(data=sub, y='scenario', x='wall_clock_s', order=order,
                      ax=ax, color=colors[model], size=4, alpha=0.75)
        medians = sub.groupby('scenario')['wall_clock_s'].median().reindex(order)
        ax.plot(medians.to_numpy(), range(len(order)), linestyle='',
                marker='|', color='0.2', markersize=9, markeredgewidth=1.3)
        ax.set_title(model, fontsize=9)
        ax.set_xlabel('')
        ax.set_ylabel('')
    for ax in axes.flat[len(models):]:
        ax.set_visible(False)
    fig.supxlabel('Wall-clock time to correct diagnosis (s)', fontsize=10)
    return [_save(fig, out_dir, 'scenario_time_to_diagnosis', ext)]


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
    parser.add_argument(
        '--wall-clock-file', '-w',
        default=str(DEFAULT_WALL_CLOCK_LOG),
        metavar='PATH',
        help=f'Path to wall_clock_durations.jsonl from recover_wall_clock.py '
             f'(default: {DEFAULT_WALL_CLOCK_LOG.name}). Missing file is fine — '
             f'wall-clock charts are simply skipped.',
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
    df = join_wall_clock(df, load_wall_clock(Path(args.wall_clock_file)))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    written += plot_model_comparison(df, out_dir, args.format)
    written += plot_scenario_performance(df, out_dir, args.format)
    written += plot_invalid_commands(df, out_dir, args.format)
    written += plot_correctness(df, out_dir, args.format)
    written += plot_cost_vs_correctness(df, out_dir, args.format)
    written += plot_time_to_diagnosis(df, out_dir, args.format)
    written += plot_distributions(df, out_dir, args.format)
    written += plot_scenario_distributions(df, out_dir, args.format)
    written += plot_scenario_time_to_diagnosis(df, out_dir, args.format)

    print(f'Wrote {len(written)} figure(s) to {out_dir}/')
    for p in written:
        print(f'  - {p}')


if __name__ == '__main__':
    main()
