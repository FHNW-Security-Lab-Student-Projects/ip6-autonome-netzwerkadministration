#!/usr/bin/env python3
"""Generate an interactive HTML view of every session in experiment_log.jsonl.

Each row is a single session — no aggregation. Opens in any browser; click
column headers to sort, use the search box to filter.

Usage:
    uv run python raw_experiments_html.py
    uv run python raw_experiments_html.py --scenario client1-client3-communication
    uv run python raw_experiments_html.py --out figures/raw_experiments.html
"""

import argparse
import html
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from analyze_experiments import DEFAULT_LOG, load


COLUMNS = [
    'session_id', 'started_at', 'model', 'scenario', 'user_query',
    'duration_s', 'total_input_tokens', 'total_output_tokens',
    'total_cost_usd', 'total_tool_calls', 'total_llm_requests',
    'invalid_commands', 'success',
]


def _format(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out['session_id'] = out['session_id'].astype(str).str[:8]
    out['started_at'] = pd.to_datetime(out['started_at'], errors='coerce') \
        .dt.strftime('%Y-%m-%d %H:%M')
    out['user_query'] = out['user_query'].astype(str).str.slice(0, 60)
    out['duration_s'] = out['duration_s'].round(1)
    out['total_cost_usd'] = out['total_cost_usd'].map(lambda v: f'${v:.4f}')
    out['success'] = out['success'].map(lambda v: 'yes' if v else 'no')
    return out[COLUMNS]


def _render(df_formatted: pd.DataFrame, df_raw: pd.DataFrame) -> str:
    table_html = df_formatted.to_html(
        table_id='sessions',
        classes='display compact',
        index=False,
        escape=True,
        border=0,
    )

    # Tag each <tr> with a class so CSS can colour success/failure rows.
    success_flags = df_raw['success'].tolist()
    rows = table_html.split('<tr>')
    rebuilt = [rows[0]]
    body_rows = rows[1:]
    # First <tr> after split is the header row; data rows come after.
    if body_rows:
        rebuilt.append('<tr>' + body_rows[0])
        for raw_row, ok in zip(body_rows[1:], success_flags):
            cls = 'ok' if ok else 'fail'
            rebuilt.append(f'<tr class="{cls}">' + raw_row)
    table_html = ''.join(rebuilt)

    generated_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    n_sessions = len(df_raw)
    models = ', '.join(sorted(df_raw['model'].dropna().unique()))
    scenarios = ', '.join(sorted(df_raw['scenario'].dropna().unique()))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Raw experiment sessions</title>
<link rel="stylesheet"
      href="https://cdn.datatables.net/1.13.8/css/jquery.dataTables.min.css">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    margin: 2em;
    color: #222;
  }}
  h1 {{ font-size: 1.4em; margin-bottom: 0.2em; }}
  .meta {{ color: #555; font-size: 0.9em; margin-bottom: 1.5em; }}
  .meta div {{ margin: 0.15em 0; }}
  table.dataTable {{ font-size: 0.85em; }}
  table.dataTable thead th {{ background: #f3f4f6; }}
  table.dataTable tbody tr.ok td {{ background: #f1faf3; }}
  table.dataTable tbody tr.fail td {{ background: #fdecea; }}
  table.dataTable tbody tr:hover td {{ background: #eaf2ff !important; }}
  td, th {{ padding: 6px 10px !important; }}
</style>
</head>
<body>
<h1>Raw experiment sessions</h1>
<div class="meta">
  <div><b>Generated:</b> {html.escape(generated_at)}</div>
  <div><b>Sessions:</b> {n_sessions}</div>
  <div><b>Models:</b> {html.escape(models)}</div>
  <div><b>Scenarios:</b> {html.escape(scenarios)}</div>
</div>
{table_html}

<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
<script src="https://cdn.datatables.net/1.13.8/js/jquery.dataTables.min.js"></script>
<script>
  $(function() {{
    $('#sessions').DataTable({{
      pageLength: 25,
      order: [[1, 'desc']],
      lengthMenu: [[10, 25, 50, 100, -1], [10, 25, 50, 100, 'All']],
    }});
  }});
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Render every session in experiment_log.jsonl as an '
                    'interactive HTML table (one row per session).',
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
        default='figures/raw_experiments.html',
        metavar='PATH',
        help='Output HTML file (default: figures/raw_experiments.html)',
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f'Log file not found: {path}')

    df = load(path, args.scenario)
    if df.empty:
        print('No matching records found.')
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    formatted = _format(df)
    html_doc = _render(formatted, df)
    out_path.write_text(html_doc, encoding='utf-8')

    print(f'Wrote {len(df)} session(s) to {out_path.resolve()}')


if __name__ == '__main__':
    main()
