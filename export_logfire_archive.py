#!/usr/bin/env python3
"""Archive the complete Logfire records of the experiment window to Parquet.

Logfire's free-plan retention is ~30 days, after which the experiment traces
(the full prompts/responses of every LLM round-trip, every tool call, every
MCP request) are gone. This script downloads EVERY column of EVERY `records`
row in the experiment window — plus the small `metrics` table — into a local
Parquet archive that stays queryable forever (DuckDB, pandas, pyarrow):

    logfire_archive/
        manifest.json                     — per-day row/trace counts, sha256,
                                            verified against live COUNT(*)
        records/day=YYYY-MM-DD.parquet    — all records columns, zstd
        metrics.parquet                   — gen_ai.client.token.usage points

The window is derived from experiment_log.jsonl exactly like
fetch_logfire_exports.py does (1 min before the first session, 1 h after the
last), so the archive is a superset of the checked-in
logfire_root_spans.jsonl / logfire_generation_ids.jsonl exports.

Paging: per day, ordered by (start_timestamp, span_id), pages of --page-size
rows fetched as Arrow IPC streams. The cursor is the page's max
start_timestamp formatted at nanosecond precision from the raw integer (no
datetime round-trip, so no precision loss); rows already written are dropped
by (trace_id, span_id). Completed days are recorded in manifest.json and
skipped on re-runs, so the export is resumable.

Verification (runs automatically after export, or alone with --verify):
    1. Each Parquet file's row/trace counts match the live COUNT(*) taken at
       export time (stored in the manifest).
    2. The root 'orchestrator run' spans reconstructed FROM THE ARCHIVE match
       the checked-in logfire_root_spans.jsonl (by trace_id, timestamps
       within 1 ms).
    3. The generation-ID ranks reconstructed from the archive match
       logfire_generation_ids.jsonl (trace, position, generation count).
    4. Every trace_id in wall_clock_durations.jsonl exists in the archive.
Together these prove the archive can replace live Logfire for everything the
report pipeline ever used.

Auth: LOGFIRE_READ_TOKEN in .env (Logfire project settings -> Read tokens).
Run inside the devcontainer:
    uv run python export_logfire_archive.py
    uv run python export_logfire_archive.py --verify   # verify only, no fetch
"""

import argparse
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from fetch_logfire_exports import LOGFIRE_BASE, SSL_CONTEXT, query_logfire, query_window, read_token
from recover_wall_clock import EXPERIMENT_LOG, GEN_IDS_EXPORT, SPANS_EXPORT, parse_ts

HERE = Path(__file__).parent
ARCHIVE_DIR = HERE / 'logfire_archive'
WALL_CLOCK = HERE / 'wall_clock_durations.jsonl'
METRICS_LIMIT = 10_000  # far above the ~3.1k points that exist; paging unimplemented
TS_TOLERANCE_S = 0.001  # JSON exports carry µs; Arrow may carry ns

_UNIT_NS = {'s': 10**9, 'ms': 10**6, 'us': 10**3, 'ns': 1}


def fetch_arrow(sql: str, min_ts: str, max_ts: str, token: str) -> pa.Table:
    """Run a query, returning the Arrow IPC response as a table. Retries transient errors."""
    params = urllib.parse.urlencode({'sql': sql, 'min_timestamp': min_ts, 'max_timestamp': max_ts})
    req = urllib.request.Request(
        f'{LOGFIRE_BASE}/v1/query?{params}',
        headers={'Authorization': f'Bearer {token}',
                 'Accept': 'application/vnd.apache.arrow.stream'},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=300, context=SSL_CONTEXT) as resp:
                return pa.ipc.open_stream(io.BytesIO(resp.read())).read_all()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit(f'Logfire query failed ({e.code}): {e.read()[:300]}')
        except (TimeoutError, urllib.error.URLError):
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError('unreachable')


def max_start_ts_rfc3339(tbl: pa.Table) -> str:
    """Max start_timestamp as an RFC3339 string at full nanosecond precision.

    Works on the raw integer representation so no precision is lost to a
    datetime round-trip — the cursor must sort exactly like the stored value.
    """
    col = tbl.column('start_timestamp')
    ns = pc.max(col.cast(pa.int64())).as_py() * _UNIT_NS[col.type.unit]
    secs, frac = divmod(ns, 10**9)
    base = datetime.fromtimestamp(secs, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    return f'{base}.{frac:09d}Z'


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_manifest(archive_dir: Path) -> dict:
    path = archive_dir / 'manifest.json'
    if path.exists():
        return json.loads(path.read_text())
    return {'created_by': 'export_logfire_archive.py', 'logfire_base': LOGFIRE_BASE,
            'records': {}, 'metrics': None}


def save_manifest(archive_dir: Path, manifest: dict) -> None:
    tmp = archive_dir / 'manifest.json.tmp'
    tmp.write_text(json.dumps(manifest, indent=2) + '\n')
    tmp.replace(archive_dir / 'manifest.json')


def sha256_of(path: Path) -> str:
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def live_day_counts(d_lo: str, d_hi: str, token: str) -> tuple[int, int]:
    sql = ("SELECT COUNT(*) AS n, COUNT(DISTINCT trace_id) AS t FROM records "
           f"WHERE start_timestamp >= '{d_lo}' AND start_timestamp < '{d_hi}'")
    row = query_logfire(sql, d_lo, d_hi, token)[0]
    return int(row['n']), int(row['t'])


def export_day(day_lo: datetime, day_hi: datetime, token: str, out_path: Path,
               page_size: int) -> tuple[int, int]:
    """Page one day's records into out_path. Returns (rows, distinct traces) written."""
    d_lo, d_hi = iso_z(day_lo), iso_z(day_hi)
    seen: set[tuple[str, str]] = set()
    traces: set[str] = set()
    writer: pq.ParquetWriter | None = None
    schema: pa.Schema | None = None
    tmp = out_path.with_name(out_path.name + '.tmp')
    cursor = d_lo
    try:
        while True:
            sql = (f"SELECT * FROM records WHERE start_timestamp >= '{cursor}' "
                   f"AND start_timestamp < '{d_hi}' "
                   f"ORDER BY start_timestamp, span_id LIMIT {page_size}")
            tbl = fetch_arrow(sql, d_lo, d_hi, token)
            if tbl.num_rows:
                keys = list(zip(tbl.column('trace_id').to_pylist(),
                                tbl.column('span_id').to_pylist()))
                mask = [k not in seen for k in keys]
                seen.update(keys)
                traces.update(k[0] for k in keys)
                new = tbl.filter(pa.array(mask, pa.bool_()))
                if writer is None:
                    schema = tbl.schema
                    tmp.parent.mkdir(parents=True, exist_ok=True)
                    writer = pq.ParquetWriter(tmp, schema, compression='zstd',
                                              compression_level=9)
                if new.num_rows:
                    writer.write_table(new if new.schema == schema else new.cast(schema))
                elif tbl.num_rows == page_size:
                    raise SystemExit(f'paging stalled at cursor {cursor} — more than '
                                     f'{page_size} rows share one timestamp?')
                cursor = max_start_ts_rfc3339(tbl)
                print(f'  {out_path.name}: {len(seen)} rows...', end='\r')
            if tbl.num_rows < page_size:
                break
    finally:
        if writer is not None:
            writer.close()
    if seen:
        tmp.replace(out_path)
    return len(seen), len(traces)


def export_records(lo: datetime, hi: datetime, token: str, archive_dir: Path,
                   page_size: int, manifest: dict) -> None:
    day = lo.date()
    while day <= hi.date():
        day_lo = max(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc), lo)
        day_hi = min(datetime.combine(day + timedelta(days=1), datetime.min.time(),
                                      tzinfo=timezone.utc), hi)
        key = day.isoformat()
        out_path = archive_dir / 'records' / f'day={key}.parquet'
        prev = manifest['records'].get(key)
        if prev and (prev['rows'] == 0 or
                     (out_path.exists() and out_path.stat().st_size == prev['bytes'])):
            print(f'{key}: already exported ({prev["rows"]} rows), skipping')
            day += timedelta(days=1)
            continue

        n_live, t_live = live_day_counts(iso_z(day_lo), iso_z(day_hi), token)
        if n_live == 0:
            print(f'{key}: no records')
            manifest['records'][key] = {'rows': 0, 'traces': 0}
            save_manifest(archive_dir, manifest)
            day += timedelta(days=1)
            continue

        n_written, t_written = export_day(day_lo, day_hi, token, out_path, page_size)
        if (n_written, t_written) != (n_live, t_live):
            raise SystemExit(f'{key}: wrote {n_written} rows/{t_written} traces but Logfire '
                             f'reports {n_live}/{t_live} — export incomplete, not recorded.')
        manifest['records'][key] = {
            'rows': n_written, 'traces': t_written,
            'file': f'records/day={key}.parquet',
            'bytes': out_path.stat().st_size, 'sha256': sha256_of(out_path),
            'window': [iso_z(day_lo), iso_z(day_hi)],
            'exported_at': iso_z(datetime.now(timezone.utc)),
        }
        save_manifest(archive_dir, manifest)
        print(f'{key}: {n_written} rows, {t_written} traces, '
              f'{out_path.stat().st_size / 1e6:.1f} MB (matches live counts)')
        day += timedelta(days=1)


def export_metrics(lo: datetime, hi: datetime, token: str, archive_dir: Path,
                   manifest: dict) -> None:
    d_lo, d_hi = iso_z(lo), iso_z(hi)
    n_live = int(query_logfire('SELECT COUNT(*) AS n FROM metrics', d_lo, d_hi, token)[0]['n'])
    if n_live > METRICS_LIMIT:
        raise SystemExit(f'{n_live} metric points exceed the single-page limit '
                         f'{METRICS_LIMIT} — add paging to export_metrics.')
    tbl = fetch_arrow(f'SELECT * FROM metrics ORDER BY recorded_timestamp LIMIT {METRICS_LIMIT}',
                      d_lo, d_hi, token)
    if tbl.num_rows != n_live:
        raise SystemExit(f'metrics: fetched {tbl.num_rows} but Logfire reports {n_live}')
    out_path = archive_dir / 'metrics.parquet'
    pq.write_table(tbl, out_path, compression='zstd', compression_level=9)
    manifest['metrics'] = {
        'rows': tbl.num_rows, 'file': 'metrics.parquet',
        'bytes': out_path.stat().st_size, 'sha256': sha256_of(out_path),
        'window': [d_lo, d_hi], 'exported_at': iso_z(datetime.now(timezone.utc)),
    }
    save_manifest(archive_dir, manifest)
    print(f'metrics: {tbl.num_rows} points, {out_path.stat().st_size / 1e6:.1f} MB')


# --- verification ------------------------------------------------------------

def day_files(archive_dir: Path, manifest: dict) -> list[Path]:
    return [archive_dir / m['file'] for m in manifest['records'].values() if m['rows']]


def verify_counts(archive_dir: Path, manifest: dict, failures: list[str]) -> set[str]:
    """Recount every Parquet file against the manifest; return all trace_ids seen."""
    all_traces: set[str] = set()
    for key, meta in sorted(manifest['records'].items()):
        if meta['rows'] == 0:
            continue
        path = archive_dir / meta['file']
        if not path.exists():
            failures.append(f'{key}: file missing: {path}')
            continue
        if sha256_of(path) != meta['sha256']:
            failures.append(f'{key}: sha256 mismatch — file changed since export')
        t = pq.read_table(path, columns=['trace_id'])
        traces = set(t.column('trace_id').to_pylist())
        all_traces |= traces
        if t.num_rows != meta['rows'] or len(traces) != meta['traces']:
            failures.append(f'{key}: parquet has {t.num_rows} rows/{len(traces)} traces, '
                            f'manifest says {meta["rows"]}/{meta["traces"]}')
    return all_traces


def verify_root_spans(archive_dir: Path, manifest: dict, failures: list[str]) -> None:
    """The archive must reproduce the checked-in logfire_root_spans.jsonl."""
    want = {}
    with open(SPANS_EXPORT) as f:
        for line in f:
            start, end, trace_id = json.loads(line)
            want[trace_id] = (parse_ts(start), parse_ts(end))
    got = {}
    for path in day_files(archive_dir, manifest):
        t = pq.read_table(path, columns=['kind', 'span_name', 'message', 'parent_span_id',
                                         'trace_id', 'start_timestamp', 'end_timestamp'],
                          filters=[('span_name', '=', 'agent run'), ('kind', '=', 'span')])
        for row in t.to_pylist():
            if row['message'] == 'orchestrator run' and row['parent_span_id'] is None:
                got[row['trace_id']] = (row['start_timestamp'], row['end_timestamp'])
    if set(want) != set(got):
        failures.append(f'root spans: archive has {len(got)}, export file has {len(want)} '
                        f'(missing: {sorted(set(want) - set(got))[:3]}...)')
        return
    for trace_id, (ws, we) in want.items():
        gs, ge = got[trace_id]
        if (abs((gs - ws).total_seconds()) > TS_TOLERANCE_S
                or abs((ge - we).total_seconds()) > TS_TOLERANCE_S):
            failures.append(f'root spans: timestamps differ for trace {trace_id}')
    print(f'root spans: all {len(want)} reproduced from archive')


def verify_gen_ids(archive_dir: Path, manifest: dict, failures: list[str]) -> None:
    """The archive must reproduce the checked-in logfire_generation_ids.jsonl ranks."""
    want = {}
    with open(GEN_IDS_EXPORT) as f:
        for line in f:
            gen_id, trace_id, pos, n_gens = json.loads(line)
            want[gen_id] = (trace_id, pos, n_gens)
    # min raw timestamp per (trace, gen_id), across both span flavors per generation
    first_ts: dict[tuple[str, str], int] = {}
    for path in day_files(archive_dir, manifest):
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(columns=['trace_id', 'start_timestamp', 'attributes'],
                                     batch_size=2000):
            ts_raw = batch.column('start_timestamp').cast(pa.int64()).to_pylist()
            for trace_id, ts, attrs in zip(batch.column('trace_id').to_pylist(), ts_raw,
                                           batch.column('attributes').to_pylist()):
                if not attrs or 'gen_ai.response.id' not in attrs:
                    continue
                gen_id = json.loads(attrs).get('gen_ai.response.id')
                if gen_id:
                    key = (trace_id, gen_id)
                    first_ts[key] = min(ts, first_ts.get(key, ts))
    by_trace: dict[str, list[tuple[int, str]]] = {}
    for (trace_id, gen_id), ts in first_ts.items():
        by_trace.setdefault(trace_id, []).append((ts, gen_id))
    got = {}
    for trace_id, gens in by_trace.items():
        gens.sort()
        for pos, (_, gen_id) in enumerate(gens, start=1):
            got[gen_id] = (trace_id, pos, len(gens))
    bad = [g for g, meta in want.items() if got.get(g) != meta]
    if bad:
        failures.append(f'generation IDs: {len(bad)}/{len(want)} rank mismatches '
                        f'(first: {bad[0]}: archive {got.get(bad[0])} vs export {want[bad[0]]})')
    else:
        print(f'generation IDs: all {len(want)} ranks reproduced from archive')


def verify_wall_clock_traces(all_traces: set[str], failures: list[str]) -> None:
    with open(WALL_CLOCK) as f:
        wanted = {json.loads(line)['trace_id'] for line in f}
    missing = wanted - all_traces
    if missing:
        failures.append(f'wall clock: {len(missing)} session traces missing from archive '
                        f'(first: {sorted(missing)[0]})')
    else:
        print(f'session traces: all {len(wanted)} wall-clock traces present in archive')


def verify(archive_dir: Path) -> None:
    manifest = load_manifest(archive_dir)
    if not manifest['records']:
        raise SystemExit('nothing to verify — manifest is empty; run the export first')
    failures: list[str] = []
    all_traces = verify_counts(archive_dir, manifest, failures)
    if not failures:
        print(f'row counts: all {len(manifest["records"])} days match the manifest')
    verify_root_spans(archive_dir, manifest, failures)
    verify_gen_ids(archive_dir, manifest, failures)
    verify_wall_clock_traces(all_traces, failures)
    if failures:
        for f in failures:
            print(f'FAIL: {f}')
        raise SystemExit(f'{len(failures)} verification failures')
    print('archive verified: counts, root spans, generation IDs, session traces all match')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--file', type=Path, default=EXPERIMENT_LOG,
                        help='experiment log defining the export window')
    parser.add_argument('--out', type=Path, default=ARCHIVE_DIR, help='archive directory')
    parser.add_argument('--page-size', type=int, default=1000, help='rows per query page')
    parser.add_argument('--verify', action='store_true',
                        help='verify an existing archive only; fetch nothing')
    args = parser.parse_args()

    if args.verify:
        verify(args.out)
        return

    sessions = [json.loads(line) for line in args.file.open()]
    lo_s, hi_s = query_window(sessions)
    lo, hi = parse_ts(lo_s), parse_ts(hi_s)
    print(f'export window: {iso_z(lo)} -> {iso_z(hi)} ({len(sessions)} sessions)')

    token = read_token()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(args.out)
    manifest['window'] = [iso_z(lo), iso_z(hi)]
    export_records(lo, hi, token, args.out, args.page_size, manifest)
    export_metrics(lo, hi, token, args.out, manifest)
    print()
    verify(args.out)


if __name__ == '__main__':
    main()
