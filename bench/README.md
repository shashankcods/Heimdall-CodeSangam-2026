# Benchmarks

Scripts that measure how Heimdall performs, so the README can quote real numbers instead of claims. Run them from the repo root.

| Script | Question it answers |
|---|---|
| `generate_dataset.py` | Do queries stay fast with lots of stored data? Fills a DuckDB file with realistic fake traces and logs (1M or 10M spans). |
| `bench_ingest.py` | How much traffic can Heimdall take? Floods `/v1/traces` and reports spans/sec, response time, and how many 429s come back. |
| `bench_duckdb_vs_sqlite.py` | Was columnar storage the right choice? Runs the same queries on DuckDB and SQLite and compares speed and storage. |

## Running them

**Big dataset for query testing** (no server needed):

```bash
uv run bench/generate_dataset.py --spans 1000000 --db-path bench_heimdall.duckdb
```

Then point the backend at that file (`HEIMDALL_DB_PATH=bench_heimdall.duckdb`) and time the API.

**Ingest throughput** (start the backend first):

```bash
uv run bench/bench_ingest.py --batches 200 --concurrency 10
```

**DuckDB vs SQLite** (no server needed):

```bash
uv run bench/bench_duckdb_vs_sqlite.py --spans 100000
```

## Results

Not recorded yet. When you run these, note the machine, data size and settings next to the numbers, since results depend on all three.
