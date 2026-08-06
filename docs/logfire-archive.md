# Logfire trace archive

> **Status (2026-08-06): this archive was never produced.** `logfire_archive/`
> does not exist and its `manifest.json` was never committed, and the retention
> deadline stated below (~2026-07-29) has passed — the full traces (prompts,
> responses, tool calls) are presumably no longer retrievable from Logfire.
> What survives independently of this archive: the small checked-in exports
> `logfire_root_spans.jsonl` and `logfire_generation_ids.jsonl`, plus the
> derived `wall_clock_durations.jsonl` / `join_verification.jsonl` — everything
> the report pipeline actually consumed (see
> [wall-clock-recovery.md](wall-clock-recovery.md)). The rest of this document
> describes how the export *would* work, and remains applicable if an archive
> ever turns up or a new experiment window is exported in time.

Logfire's free plan retains telemetry for ~30 days. All 300 experiment
sessions (2026-06-29 → 2026-07-06) were traced there — including the **full
prompts and responses of every LLM round-trip** and every tool/MCP call —
so without an export that evidence disappears around **2026-07-29**.

`export_logfire_archive.py` downloads the complete `records` table (every
column, every row) for the experiment window into a local Parquet archive
that stays queryable forever without Logfire:

```
logfire_archive/
    manifest.json                   # tracked in git: per-day counts + sha256
    records/day=YYYY-MM-DD.parquet  # all records columns, zstd-compressed
    metrics.parquet                 # gen_ai.client.token.usage histogram points
```

Only `manifest.json` is tracked in git (the Parquet files are ~100–250 MB).
Keep a copy of the archive somewhere durable, e.g. as a GitHub release asset:

```sh
tar czf logfire_archive.tar.gz logfire_archive/
gh release create logfire-archive-2026-07 logfire_archive.tar.gz \
    --title "Logfire trace archive (experiments 2026-06-29..07-06)" \
    --notes "Full records export; verify against logfire_archive/manifest.json sha256s."
```

## Creating / updating the archive

Needs `LOGFIRE_READ_TOKEN` in `.env` (Logfire project settings → Read tokens;
the MCP token is not accepted by the query API). Run inside the devcontainer:

```sh
uv run python export_logfire_archive.py            # export + verify
uv run python export_logfire_archive.py --verify   # re-verify an existing archive
```

The export window is derived from `experiment_log.jsonl` (same logic as
`fetch_logfire_exports.py`). Completed days are recorded in the manifest and
skipped on re-runs, so the script is safe to interrupt and resume.

Further flags: `--file` (experiment log to derive the window from), `--out`
(archive directory), `--page-size` (rows per query page, default 1000).
The metrics export has a hard `METRICS_LIMIT = 10_000` ceiling — if more
metric points exist than that, the script raises instead of exporting a
truncated table (paging for metrics is unimplemented).

Verification (`--verify`) would prove — had the archive been produced — that
it can replace live Logfire for everything the report pipeline used: per-day
row/trace counts matching live `COUNT(*)` at export time, and the checked-in
`logfire_root_spans.jsonl` / `logfire_generation_ids.jsonl` exports plus all
300 `wall_clock_durations.jsonl` trace IDs being exactly reproducible from
the archive alone.

## Querying the archive (no Logfire needed)

DuckDB reads the Parquet files directly, and its JSON operators match the
Logfire/DataFusion syntax (`attributes->>'key'`):

```sql
-- all spans/logs of one experiment run, in order (trace_id from
-- wall_clock_durations.jsonl / join_verification.jsonl):
SELECT start_timestamp, span_name, message, duration
FROM read_parquet('logfire_archive/records/*.parquet')
WHERE trace_id = '<trace_id>'
ORDER BY start_timestamp;

-- full LLM conversation of a run (prompts + responses live in the
-- attributes JSON of the chat spans):
SELECT start_timestamp,
       attributes->>'gen_ai.response.id' AS gen_id,
       attributes->>'events'             AS conversation
FROM read_parquet('logfire_archive/records/*.parquet')
WHERE trace_id = '<trace_id>' AND span_name LIKE 'chat %'
ORDER BY start_timestamp;

-- every tool call a run made, with arguments:
SELECT start_timestamp, message, attributes
FROM read_parquet('logfire_archive/records/*.parquet')
WHERE trace_id = '<trace_id>' AND span_name = 'running tool'
ORDER BY start_timestamp;
```

Or with pandas/pyarrow inside the devcontainer:

```python
import pyarrow.parquet as pq
t = pq.read_table('logfire_archive/records/day=2026-07-03.parquet',
                  columns=['trace_id', 'span_name', 'message', 'attributes'])
```

Common Logfire → archive query translations:

| Live Logfire (`/v1/query`)              | Archive (DuckDB)                                   |
| --------------------------------------- | -------------------------------------------------- |
| `FROM records`                           | `FROM read_parquet('logfire_archive/records/*.parquet')` |
| `attributes->>'key'`                     | identical                                          |
| `kind = 'span' AND parent_span_id IS NULL` | identical                                       |
| min/max_timestamp query params           | `WHERE start_timestamp BETWEEN ... AND ...`        |

The root-span and generation-ID queries in `fetch_logfire_exports.py`
translate 1:1 this way; `--verify` runs exactly those reconstructions.
