#!/usr/bin/env python3
"""Derive the model prices ($ per million tokens) used in the experiments.

The experiment log stores each agent run's modeled cost (cost_usd) and native
token counts, but not the OpenRouter rates the cost was computed from (those
were fetched live at run time — see model_config.pricing_for). This script
recovers them from the log alone: for every model and calendar day it solves

    cost_usd = input_tokens * input_rate / 1e6 + output_tokens * output_rate / 1e6

for (input_rate, output_rate) by least squares over all runs, then merges
consecutive days with identical rates into date ranges. Because every run of a
day shares one rate pair, the fit is exact (residual ~0, up to the 8-decimal
rounding of cost_usd); a non-zero residual is flagged — it means the price
changed intra-day or the cost formula had extra terms (per-request fee,
separately priced reasoning tokens) for that model.

Runs with cost_estimated=true are excluded from the solve: their cost was
computed from normalized (not native) tokens, so mixing them in would skew the
derived rates. They are counted and reported.

Usage:
    uv run python derive_model_prices.py                     # overview table
    uv run python derive_model_prices.py --per-day           # no range merging
    uv run python derive_model_prices.py --file other.jsonl  # different log file
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

DEFAULT_LOG = Path(__file__).parent / 'experiment_log.jsonl'

# Two rate pairs count as "the same price" if they agree within this ($/Mtok).
RATE_TOL = 1e-4
# A day's fit is "exact" if no run's cost deviates by more than this ($). The log
# rounds cost_usd to 8 decimals, so a clean fit stays orders of magnitude below.
RESIDUAL_TOL = 1e-4


def load_samples(path: Path) -> tuple[dict, int]:
    """Group usable runs as (model, day) -> [(input_tokens, output_tokens, cost)].

    Returns the grouping and the count of excluded cost_estimated runs.
    """
    samples: dict[tuple[str, str], list] = defaultdict(list)
    estimated = 0
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                session = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f'  [warn] Skipping malformed line {lineno}: {exc}')
                continue
            day = (session.get('started_at') or '')[:10] or '(no date)'
            for run in session.get('agent_runs', []):
                if run.get('cost_estimated'):
                    estimated += 1
                    continue
                tin = run.get('input_tokens') or 0
                tout = run.get('output_tokens') or 0
                cost = float(run.get('cost_usd') or 0.0)
                if cost > 0 and (tin or tout):
                    samples[(run['model'], day)].append((tin, tout, cost))
    return samples, estimated


def solve_rates(runs: list) -> tuple[float, float, float] | None:
    """Least-squares solve for (input $/Mtok, output $/Mtok, max residual $).

    Returns None when the system is underdetermined — all runs share the same
    input/output token ratio, so the two rates can't be separated.
    """
    sxx = sxy = syy = sxc = syc = 0.0
    for tin, tout, cost in runs:
        x, y = tin / 1e6, tout / 1e6
        sxx += x * x
        sxy += x * y
        syy += y * y
        sxc += x * cost
        syc += y * cost
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-12:
        return None
    input_rate = (sxc * syy - syc * sxy) / det
    output_rate = (syc * sxx - sxc * sxy) / det
    residual = max_residual(runs, input_rate, output_rate)
    return input_rate, output_rate, residual


def max_residual(runs: list, input_rate: float, output_rate: float) -> float:
    """Largest |modeled - logged| cost across runs for the given rates."""
    return max(
        abs(tin / 1e6 * input_rate + tout / 1e6 * output_rate - cost)
        for tin, tout, cost in runs
    )


def build_ranges(days: list[str], per_day: dict) -> list[dict]:
    """Merge consecutive days whose rates agree into one date-range row.

    A day whose own fit is underdetermined is folded into the current range if
    the range's rates reproduce its costs; otherwise it becomes an 'unknown' row.
    """
    ranges: list[dict] = []
    for day in days:
        runs = per_day[day]
        fit = solve_rates(runs)
        current = ranges[-1] if ranges else None

        if current and current['rates']:
            in_rate, out_rate = current['rates']
            same = (
                abs(fit[0] - in_rate) < RATE_TOL and abs(fit[1] - out_rate) < RATE_TOL
                if fit
                else max_residual(runs, in_rate, out_rate) < RESIDUAL_TOL
            )
            if same:
                current['last'] = day
                current['runs'] += len(runs)
                current['residual'] = max(
                    current['residual'], fit[2] if fit else max_residual(runs, in_rate, out_rate)
                )
                continue

        ranges.append({
            'first': day,
            'last': day,
            'runs': len(runs),
            'rates': (fit[0], fit[1]) if fit else None,
            'residual': fit[2] if fit else 0.0,
        })
    return ranges


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--file', type=Path, default=DEFAULT_LOG, help='log file to read')
    parser.add_argument('--per-day', action='store_true',
                        help='show one row per day instead of merged date ranges')
    args = parser.parse_args()

    samples, estimated = load_samples(args.file)
    if not samples:
        print(f'No usable runs found in {args.file}')
        return

    models = sorted({model for model, _day in samples})
    total_runs = sum(len(runs) for runs in samples.values())
    print(f'Derived model prices from {args.file.name} '
          f'({total_runs} agent runs, {len(models)} models)\n')

    header = (f'{"model":40} {"period":25} {"runs":>5} '
              f'{"input $/M":>10} {"output $/M":>11}')
    print(header)
    print('-' * len(header))

    flagged: list[str] = []
    for model in models:
        per_day = {day: runs for (m, day), runs in samples.items() if m == model}
        days = sorted(per_day)
        if args.per_day:
            rows = [{
                'first': day, 'last': day, 'runs': len(per_day[day]),
                'rates': (fit[0], fit[1]) if (fit := solve_rates(per_day[day])) else None,
                'residual': fit[2] if fit else 0.0,
            } for day in days]
        else:
            rows = build_ranges(days, per_day)

        for row in rows:
            period = row['first'] if row['first'] == row['last'] \
                else f'{row["first"]} – {row["last"]}'
            if row['rates'] is None:
                print(f'{model:40} {period:25} {row["runs"]:>5} '
                      f'{"?":>10} {"?":>11}  (underdetermined)')
                flagged.append(f'{model} {period}: rates not solvable — all runs share '
                               'the same input/output ratio')
                continue
            in_rate, out_rate = row['rates']
            mark = ''
            if row['residual'] > RESIDUAL_TOL:
                mark = '  (!)'
                flagged.append(f'{model} {period}: fit residual '
                               f'${row["residual"]:.6f} — price changed intra-day or '
                               'cost included extra terms; rates are approximate')
            print(f'{model:40} {period:25} {row["runs"]:>5} '
                  f'{in_rate:>10.4f} {out_rate:>11.4f}{mark}')

    if estimated:
        print(f'\nExcluded {estimated} run(s) with cost_estimated=true '
              '(cost based on normalized, not native, tokens).')
    if flagged:
        print('\nWarnings:')
        for note in flagged:
            print(f'  (!) {note}')
    else:
        print('\nAll fits exact: every run\'s cost_usd is reproduced by its '
              'period\'s rate pair (within cost rounding).')


if __name__ == '__main__':
    main()
