# Benchmarks

Scripts that measure how Heimdall performs, so the README can quote real numbers instead of claims. Run them from the repo root.

| Script | Question it answers |
|---|---|
| `generate_dataset.py` | Do queries stay fast with lots of stored data? Fills a DuckDB file with realistic fake traces and logs (1M or 10M spans). |
| `bench_ingest.py` | How much traffic does Heimdall accept? Floods `/v1/traces` and reports accepted spans/sec, response time, and how many 429s come back. |
| `writer_stats.py` | How much of that actually reaches disk? Runs the backend and records the writer's own counters: spans written per second, and whether any were lost. |
| `bench_duckdb_vs_sqlite.py` | Was columnar storage the right choice? Runs the same queries on DuckDB and an indexed SQLite and compares speed and storage. |

## Running them

**Big dataset for query testing** (no server needed):

```bash
uv run bench/generate_dataset.py --spans 1000000 --db-path bench_heimdall.duckdb
```

**Ingest throughput.** Start the backend through `writer_stats.py` so the written count is recorded, send load, then stop it (Ctrl+C) and summarize:

```bash
uv run --project backend python bench/writer_stats.py serve writer_stats.csv
uv run bench/bench_ingest.py --batches 500 --spans-per-batch 500 --concurrency 20 --gzip
uv run --project backend python bench/writer_stats.py summary writer_stats.csv
```

`bench_ingest.py` alone reports *accepted* spans/sec: a 200 means the spans were queued, not that they're on disk.

**DuckDB vs SQLite** (no server needed):

```bash
uv run bench/bench_duckdb_vs_sqlite.py --spans 1000000
```

## Results

Machine: Intel Core (20 logical CPUs), Windows 11, Python 3.12, DuckDB 2.0.0, SQLite 3.49.1. Results depend on the machine, data size and settings, so re-run before quoting them elsewhere.

### DuckDB vs indexed SQLite, 1,000,000 spans

SQLite gets indexes on `(trace_id, span_id)` and `start_time`; DuckDB gets none. Every query returns the same answer on both engines. Median of 7 warm runs.

| Query | DuckDB | SQLite | |
|---|---|---|---|
| Error rate per service | 4.8 ms | 512 ms | 108x faster |
| Exact p95/p99 latency per service | 19.1 ms | 2,193 ms | 115x faster |
| Service-map self-join | 85.1 ms | 1,747 ms | 20.5x faster |
| Errors in the last 5 minutes | 2.4 ms | 100 ms | 41x faster |
| Single trace lookup by id | 3.1 ms | 0.05 ms | 66x **slower** |
| Storage per span | 42 B | 293 B (incl. indexes) | 6.9x smaller |

DuckDB wins every scan and aggregation, which is what observability queries are. SQLite's index wins single-row lookups. Across runs the self-join speedup ranged from 20x to 25x.

### Ingest: 250,000 spans, 500 per request, 20 concurrent senders, gzip

| Setting | Accepted (HTTP 200) | Written to DuckDB | 429s | Lost |
|---|---|---|---|---|
| `HEIMDALL_BATCH_SIZE=500` (default) | 47,601 spans/s | **27,696 spans/s** (best second: 39,148) | 36% of requests | 0 |
| `HEIMDALL_BATCH_SIZE=5000` | 70,227 spans/s | **66,489 spans/s** (best second: 76,997) | 0% | 0 |

With the default batch size each 500-span request becomes its own DuckDB write, so under this load the writer falls behind: the queue fills to its 100,000-span limit and Heimdall answers 429 instead of growing memory. Every accepted span is still written. With 5,000-span batches the writer keeps up (backlog peaked at 50,000) and write throughput rises 2.4x.
